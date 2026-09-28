"""Runtime settings. Secrets are supplied only through environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    redis_url: str
    projects_config: str
    storage_root: str
    log_level: str
    retention_days: int

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            database_url=os.environ.get("DATABASE_URL", "postgresql+psycopg://object_statistick:object_statistick@postgres:5432/object_statistick"),
            redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            projects_config=os.environ.get("PROJECTS_CONFIG", "/app/config/projects.yaml"),
            storage_root=os.environ.get("STORAGE_ROOT", "/app/storage"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            retention_days=max(1, int(os.environ.get("RETENTION_DAYS", "7"))),
        )
