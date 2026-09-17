"""Thin HTTP adapter over Backend application services."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...integrations.redis_queue import enqueue_processing
from ...persistence.models import ProcessingJobRow, RecordingRow, SubscriberRow

router = APIRouter()


class SubscribeRequest(BaseModel):
    chat_id: int = Field(gt=0)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/projects")
def projects(request: Request) -> list[dict[str, Any]]:
    return [project.model_dump(exclude={"camera_source_env", "telegram_bot_token_env"}) for project in request.app.state.projects.projects]


@router.post("/projects/{project_id}/subscribers", status_code=201)
def subscribe(project_id: str, payload: SubscribeRequest, request: Request) -> dict[str, str]:
    try:
        request.app.state.projects.by_id(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    with request.app.state.sessions() as session:
        existing = session.scalar(select(SubscriberRow).where(SubscriberRow.project_id == project_id, SubscriberRow.chat_id == payload.chat_id))
        if existing is None:
            session.add(SubscriberRow(project_id=project_id, chat_id=payload.chat_id, created_at=datetime.now(timezone.utc)))
            session.commit()
    return {"status": "subscribed"}


@router.get("/recordings")
def recordings(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    with request.app.state.sessions() as session:
        rows = list(session.scalars(select(RecordingRow).order_by(RecordingRow.created_at.desc()).limit(min(max(limit, 1), 500))))
        return [{"id": row.id, "project_id": row.project_id, "shift": row.shift_number, "status": row.status, "starts_at": row.starts_at, "error": row.error} for row in rows]


@router.get("/jobs")
def jobs(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    with request.app.state.sessions() as session:
        rows = list(session.scalars(select(ProcessingJobRow).order_by(ProcessingJobRow.started_at.desc()).limit(min(max(limit, 1), 500))))
        return [{"id": row.id, "recording_id": row.recording_id, "status": row.status, "result": row.result, "error": row.error} for row in rows]


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, request: Request) -> dict[str, str]:
    with request.app.state.sessions() as session:
        job = session.get(ProcessingJobRow, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Processing job not found")
        if job.status not in {"failed", "queued"}:
            raise HTTPException(status_code=409, detail=f"Job in status '{job.status}' cannot be retried")
        if not Path(job.recording.source_path).is_file():
            raise HTTPException(status_code=409, detail="Source recording no longer exists")
        job.status, job.error = "queued", None
        job.recording.status, job.recording.error = "recorded", None
        session.commit()
    try:
        enqueue_processing(request.app.state.settings, job_id)
    except Exception as exc:
        with request.app.state.sessions() as session:
            job = session.get(ProcessingJobRow, job_id)
            job.status, job.error = "failed", f"Queue enqueue failed: {exc}"
            session.commit()
        raise HTTPException(status_code=503, detail="Redis queue is unavailable") from exc
    return {"status": "queued", "job_id": job_id}
