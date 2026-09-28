"""Safe artifact retention: only already-delivered reports are eligible for deletion."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..persistence.models import ProcessingJobRow

LOGGER = logging.getLogger(__name__)


class RetentionService:
    def __init__(self, sessions: sessionmaker[Session], storage_root: str, retention_days: int) -> None:
        self._sessions = sessions
        self._root = Path(storage_root).resolve()
        self._cutoff_days = retention_days

    def cleanup(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._cutoff_days)
        with self._sessions() as session:
            jobs = list(session.scalars(select(ProcessingJobRow).where(
                ProcessingJobRow.reported_at.is_not(None), ProcessingJobRow.reported_at < cutoff,
            )))
            for job in jobs:
                self._unlink_if_inside(Path(job.recording.source_path))
                for artifact in (job.artifacts or {}).values():
                    self._unlink_if_inside(Path(artifact))

    def _unlink_if_inside(self, path: Path) -> None:
        """Never follow a DB value outside the owned storage volume."""
        try:
            resolved = path.resolve()
            resolved.relative_to(self._root)
            if resolved.is_file():
                resolved.unlink()
        except (OSError, ValueError):
            LOGGER.warning("Retention skipped unsafe or unavailable path: %s", path)
