"""Application service that turns a scheduled shift into one durable ML job."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config.schemas import ProjectConfig, ShiftConfig
from ..config.settings import Settings
from ..integrations.redis_queue import enqueue_processing
from ..integrations.rtsp_recorder import RTSPRecorder
from ..persistence.models import ProcessingJobRow, RecordingRow

LOGGER = logging.getLogger(__name__)
SessionFactory: TypeAlias = sessionmaker[Session]


class RecordingService:
    """Owns recorder lifecycle; it never performs inference itself."""

    def __init__(self, sessions: SessionFactory, settings: Settings) -> None:
        self._sessions = sessions
        self._settings = settings
        self._active: dict[str, RTSPRecorder] = {}

    def start_shift(self, project: ProjectConfig, shift: ShiftConfig) -> None:
        zone = ZoneInfo(project.timezone)
        now = datetime.now(zone)
        starts_at = datetime.combine(now.date(), shift.start, tzinfo=zone)
        ends_at = datetime.combine(now.date(), shift.end, tzinfo=zone)
        with self._sessions() as session:
            existing = session.scalar(select(RecordingRow).where(
                RecordingRow.project_id == project.id,
                RecordingRow.shift_number == shift.number,
                RecordingRow.starts_at == starts_at,
            ))
            if existing is not None:
                LOGGER.info("Shift already exists: %s/%s/%s", project.id, starts_at.date(), shift.number)
                return
            recording_id = str(uuid4())
            destination = Path(self._settings.storage_root) / "recordings" / project.id / starts_at.strftime("%Y-%m-%d") / f"shift-{shift.number}-{recording_id}.mp4"
            session.add(RecordingRow(
                id=recording_id, project_id=project.id, shift_number=shift.number,
                starts_at=starts_at, ends_at=ends_at, source_path=str(destination),
                status="recording", created_at=datetime.now(timezone.utc),
            ))
            session.commit()
        try:
            recorder = RTSPRecorder(project.camera_source(), destination, ends_at, lambda path, error: self._finish(recording_id, path, error))
            self._active[recording_id] = recorder
            recorder.start()
            LOGGER.info("Recording started: %s", recording_id)
        except Exception as exc:
            self._finish(recording_id, None, str(exc))

    def _finish(self, recording_id: str, path: Path | None, error: str | None) -> None:
        self._active.pop(recording_id, None)
        job_id: str | None = None
        with self._sessions() as session:
            recording = session.get(RecordingRow, recording_id)
            if recording is None:
                return
            if error or path is None:
                recording.status, recording.error = "failed", error or "Unknown recording error"
            else:
                recording.status = "recorded"
                job = ProcessingJobRow(recording_id=recording_id, status="queued")
                session.add(job)
                session.flush()
                job_id = job.id
            session.commit()
        if job_id:
            try:
                enqueue_processing(self._settings, job_id)
                LOGGER.info("Processing queued: %s", job_id)
            except Exception as exc:
                LOGGER.exception("Could not enqueue processing job %s", job_id)
                with self._sessions() as session:
                    job = session.get(ProcessingJobRow, job_id)
                    job.status, job.error = "failed", f"Queue enqueue failed: {exc}"
                    session.commit()

    def stop_all(self) -> None:
        for recorder in list(self._active.values()):
            recorder.stop()
