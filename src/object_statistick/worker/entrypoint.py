"""RQ entry point for GPU processing workers."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..config.schemas import load_projects_config
from ..persistence.database import make_session_factory
from ..persistence.models import ProcessingJobRow
from .processor import VideoProcessor

LOGGER = logging.getLogger(__name__)


def process_video_job(job_id: str, database_url: str, projects_config_path: str, storage_root: str) -> None:
    """Process exactly one recording. RQ retry policy can safely rerun failed jobs."""
    sessions = make_session_factory(database_url)
    with sessions() as session:
        job = session.scalar(select(ProcessingJobRow).where(ProcessingJobRow.id == job_id))
        if job is None:
            raise KeyError(f"Processing job {job_id} does not exist")
        if job.status == "completed":
            return
        job.status, job.error, job.started_at = "processing", None, datetime.now(timezone.utc)
        session.commit()
        recording = job.recording
        recording_path, project_id, started_at, recording_id = recording.source_path, recording.project_id, recording.starts_at, recording.id

    try:
        project = load_projects_config(projects_config_path).by_id(project_id)
        result, artifacts = VideoProcessor(project, Path(storage_root) / "artifacts").process(Path(recording_path), started_at, recording_id)
        with sessions() as session:
            job = session.get(ProcessingJobRow, job_id)
            job.status, job.result, job.artifacts = "completed", result, artifacts
            job.completed_at = datetime.now(timezone.utc)
            job.recording.status = "processed"
            session.commit()
    except Exception as exc:
        LOGGER.exception("Processing job %s failed", job_id)
        with sessions() as session:
            job = session.get(ProcessingJobRow, job_id)
            job.status, job.error = "failed", str(exc)
            job.recording.status, job.recording.error = "failed", str(exc)
            session.commit()
        raise
