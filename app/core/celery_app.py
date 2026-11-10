from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "video_processor",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
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

    # Hard cap on a single job. Portfolio: videos capped at 60s, so 8 min is plenty.
    task_soft_time_limit=8 * 60,
    task_time_limit=9 * 60,

    # Single worker process, no prefetch, recycle to reclaim memory fragmentation.
    worker_concurrency=1,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=5,

    # Re-queue if worker dies mid-task (OOM, crash, restart).
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Results expire after 1h to keep Redis memory small.
    result_expires=60 * 60,

    # Celery >= 5.3: keep retrying broker connection on startup.
    broker_connection_retry_on_startup=True,
)

celery_app.conf.beat_schedule = {
    "flush_video_stats": {
        "task": "flush_video_stats",
        "schedule": 45.0,  # every 45s
    },
}
