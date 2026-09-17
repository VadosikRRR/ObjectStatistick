"""FastAPI control plane: validated configuration, scheduling, recordings, and reports."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from ..config.schemas import ProjectsConfig, load_projects_config
from ..config.settings import Settings
from ..integrations.telegram_bot import TelegramSubscriptionPoller
from ..persistence.database import initialize_database
from ..persistence.models import ProjectRow
from .api.routes import router
from .recording_service import RecordingService
from .reporting_service import ReportService
from .retention_service import RetentionService
from .scheduler import create_scheduler

LOGGER = logging.getLogger(__name__)


def sync_projects(session_factory: sessionmaker[Session], projects: ProjectsConfig) -> None:
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        for project in projects.projects:
            row = session.get(ProjectRow, project.id)
            payload = project.model_dump(mode="json")
            if row is None:
                session.add(ProjectRow(id=project.id, name=project.name, config=payload, updated_at=now))
            else:
                row.name, row.config, row.updated_at = project.name, payload, now
        session.commit()


def create_app() -> FastAPI:
    settings = Settings.from_environment()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        projects = load_projects_config(settings.projects_config)
        sessions = initialize_database(settings.database_url)
        sync_projects(sessions, projects)
        manager = RecordingService(sessions, settings)
        reports = ReportService(sessions, projects, settings.storage_root)
        retention = RetentionService(sessions, settings.storage_root, settings.retention_days)
        scheduler = create_scheduler(projects, manager, reports, retention)
        scheduler.start()
        telegram_pollers = [
            TelegramSubscriptionPoller(token, project.id, project.name, sessions)
            for project in projects.projects
            if (token := project.telegram_bot_token())
        ]
        for poller in telegram_pollers:
            poller.start()
        app.state.projects, app.state.sessions, app.state.manager, app.state.settings = projects, sessions, manager, settings
        yield
        scheduler.shutdown(wait=False)
        manager.stop_all()
        for poller in telegram_pollers:
            poller.stop()

    app = FastAPI(title="ObjectStatistick Backend", version="2.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
