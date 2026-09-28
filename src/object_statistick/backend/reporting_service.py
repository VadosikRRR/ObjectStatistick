"""Daily report assembly and delivery polling."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config.schemas import ProjectConfig, ProjectsConfig
from ..integrations.telegram_bot import TelegramNotifier
from ..persistence.models import ProcessingJobRow, RecordingRow, SubscriberRow

LOGGER = logging.getLogger(__name__)


class ReportService:
    """Publishes a day only after every configured shift has completed successfully."""

    def __init__(self, session_factory: sessionmaker[Session], projects: ProjectsConfig, storage_root: str) -> None:
        self._sessions = session_factory
        self._projects = projects
        self._reports_root = Path(storage_root) / "reports"
        self._notifier = TelegramNotifier()

    def publish_ready_reports(self) -> None:
        for project in self._projects.projects:
            try:
                self._publish_project(project)
            except Exception:
                LOGGER.exception("Could not publish report for project %s", project.id)

    def _publish_project(self, project: ProjectConfig) -> None:
        with self._sessions() as session:
            jobs = list(session.scalars(
                select(ProcessingJobRow)
                .join(RecordingRow)
                .where(RecordingRow.project_id == project.id, ProcessingJobRow.status == "completed")
                .order_by(RecordingRow.starts_at)
            ))
            by_day: dict[date, list[ProcessingJobRow]] = defaultdict(list)
            zone = ZoneInfo(project.timezone)
            for job in jobs:
                by_day[job.recording.starts_at.astimezone(zone).date()].append(job)

            expected = {shift.number for shift in project.shifts}
            for report_date, daily_jobs in by_day.items():
                completed = {job.recording.shift_number for job in daily_jobs}
                unreported = [job for job in daily_jobs if job.reported_at is None]
                if not unreported or not expected.issubset(completed):
                    continue
                # One result per shift. Multiple attempts are resolved by the latest completed job.
                by_shift = {job.recording.shift_number: job for job in daily_jobs}
                report_jobs = [by_shift[number] for number in sorted(expected)]
                report_path = self._write_excel(project, report_date, report_jobs)
                text = self._render_message(project, report_date, report_jobs)
                chat_ids = list(session.scalars(select(SubscriberRow.chat_id).where(SubscriberRow.project_id == project.id)))
                chart_paths = [job.artifacts.get("personnel_chart", "") for job in report_jobs if job.artifacts]
                if project.report_attachment_path and Path(project.report_attachment_path).is_file():
                    chart_paths.append(project.report_attachment_path)
                self._notifier.send(project.telegram_bot_token(), chat_ids, text, [str(report_path), *chart_paths])
                now = datetime.now(timezone.utc)
                for job in daily_jobs:
                    if job.reported_at is None:
                        job.reported_at = now
                session.commit()

    def _write_excel(self, project: ProjectConfig, report_date: date, jobs: list[ProcessingJobRow]) -> Path:
        rows: list[dict[str, object]] = []
        for job in jobs:
            for class_name, stats in (job.result or {}).get("classes", {}).items():
                rows.append({"date": report_date.isoformat(), "project": project.name, "shift": job.recording.shift_number, "class": class_name, **stats})
        destination = self._reports_root / project.id / f"{report_date.isoformat()}.xlsx"
        destination.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_excel(destination, index=False)
        return destination

    @staticmethod
    def _render_message(project: ProjectConfig, report_date: date, jobs: list[ProcessingJobRow]) -> str:
        lines = [f"{project.name}", f"Отчёт за {report_date:%d.%m.%Y}"]
        for job in jobs:
            result = job.result or {}
            lines.extend(["", f"Смена {job.recording.shift_number}"])
            for class_name in ("Technik", "Person", "Car"):
                stats = result.get("classes", {}).get(class_name, {})
                lines.append(f"{class_name}: вход {stats.get('entries', 0)}, выход {stats.get('exits', 0)}, среднее {stats.get('average_hour', 0)}")
        return "\n".join(lines)
