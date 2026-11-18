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

    # Hard cap on a single job. Set to ~3-4x the expected worst-case job time on the
    # target server (and cap upload length accordingly). Applies to every task that
    # does not set its own limits.
    task_soft_time_limit=15 * 60,
    task_time_limit=16 * 60,

    # Reserve only one message per worker process. With long ffmpeg jobs the default
    # (4x concurrency) makes the worker hoard queued tasks while one job runs.
    worker_prefetch_multiplier=1,

    # Results are kept in Redis; expire them after 1h instead of the 1-day default
    # to keep Redis memory small. (If results are never read via AsyncResult, use
    # task_ignore_result=True instead and drop task_track_started.)
    result_expires=60 * 60,

    # Celery >= 5.3: keep retrying the broker connection on startup (silences the
    # deprecation warning and survives Redis starting after the worker).
    broker_connection_retry_on_startup=True,
)