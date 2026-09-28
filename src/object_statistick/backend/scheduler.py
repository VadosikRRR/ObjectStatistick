"""Schedules application services without coupling them to FastAPI routes."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..config.schemas import ProjectsConfig
from .recording_service import RecordingService
from .reporting_service import ReportService
from .retention_service import RetentionService


def create_scheduler(projects: ProjectsConfig, recordings: RecordingService, reports: ReportService, retention: RetentionService) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    for project in projects.projects:
        zone = ZoneInfo(project.timezone)
        for shift in project.shifts:
            scheduler.add_job(
                recordings.start_shift,
                CronTrigger(hour=shift.start.hour, minute=shift.start.minute, timezone=zone),
                args=[project, shift],
                id=f"record-{project.id}-{shift.number}",
                replace_existing=True,
                misfire_grace_time=60,
            )
    scheduler.add_job(reports.publish_ready_reports, "interval", minutes=1, id="publish-reports", replace_existing=True)
    scheduler.add_job(retention.cleanup, "interval", hours=24, id="retention-cleanup", replace_existing=True)
    return scheduler
