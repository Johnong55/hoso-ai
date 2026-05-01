# app/worker/celery_app.py
from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "admin_procedure_ai",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Ho_Chi_Minh",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    beat_schedule={
        "daily-crawl": {
            "task": "app.worker.tasks.scheduled_crawl",
            "schedule": crontab(
                hour=settings.CRAWL_SCHEDULE_HOUR,
                minute=settings.CRAWL_SCHEDULE_MINUTE,
            ),
        },
        "retry-failed-embeddings": {
            "task": "app.worker.tasks.retry_failed_embeddings",
            "schedule": crontab(minute=0),  # every hour
        },
    },
)
