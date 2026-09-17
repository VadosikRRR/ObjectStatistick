"""A deliberately small queue boundary. The web app never imports ML code."""
from __future__ import annotations

from redis import Redis
from rq import Queue, Retry

from ..config.settings import Settings


def processing_queue(settings: Settings) -> Queue:
    return Queue("video-processing", connection=Redis.from_url(settings.redis_url), default_timeout="12h")


def enqueue_processing(settings: Settings, job_id: str) -> str:
    # Import path keeps the Backend image free of a direct dependency on processor objects.
    job = processing_queue(settings).enqueue(
        "object_statistick.worker.entrypoint.process_video_job",
        job_id,
        settings.database_url,
        settings.projects_config,
        settings.storage_root,
        job_timeout="12h",
        result_ttl=86_400,
        failure_ttl=604_800,
        retry=Retry(max=3, interval=[60, 300, 900]),
    )
    return job.id
