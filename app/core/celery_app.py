from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "video_processor",
    broker= settings.CELERY_BROKER_URL,
    backend= settings.CELERY_RESULT_BACKEND,
    include=[
        'app.tasks.video_tasks',
        'app.tasks.reactions_tasks',
        'app.tasks.video_stats_tasks'
    ]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,
    task_soft_time_limit=25 * 60,
)