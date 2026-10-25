import logging
import asyncio

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core.celery_app import celery_app
from app.core.config import settings
from app.services.video_stats_counter import VideoStatsCounter

logger = logging.getLogger(__name__)


def _make_redis() -> aioredis.Redis:
    """Fresh Redis client per task — avoids closed-loop connection reuse."""
    return aioredis.from_url(
        settings.CELERY_BROKER_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _make_session() -> async_sessionmaker:
    engine = create_async_engine(
        settings.SQLALCHEMY_DATABASE_URL,
        pool_size=1,
        max_overflow=0,
    )
    return async_sessionmaker(engine, expire_on_commit=False)


@celery_app.task(name="flush_video_stats")
def flush_video_stats() -> None:
    """
    Celery beat task — flushes all pending video stat deltas to Postgres.

    Drains likes, views, saves, comments, and shares in a single pass:
      1. Atomically claims stats:pending via RENAME.
      2. For each video: pipelines GETDEL across all five hot keys → one UPSERT.
      3. On DB failure: restores deltas to Redis and re-queues the video_id
         so the next beat retries without losing data.

    Beat schedule example (celery_app.py):
        "flush-video-stats": {
            "task": "flush_video_stats",
            "schedule": 60.0,   # every 60 seconds
        }
    """
    asyncio.run(_flush_async())


async def _flush_async() -> None:
    redis = _make_redis()
    SessionLocal = _make_session()

    try:
        async with SessionLocal() as db:
            counter = VideoStatsCounter(redis, db)
            await counter.flush_pending()
    except Exception:
        logger.error("flush_video_stats task failed", exc_info=True)
    finally:
        await redis.aclose()

import time

@celery_app.task
def add(x: int, y: int) -> int:
    """Simple synchronous task — great for verifying the worker works."""
    return x + y

@celery_app.task
def slow_task(seconds: int = 5) -> dict:
    """Long-running task to simulate real background work."""
    time.sleep(seconds)
    return {"status": "done", "slept_for": seconds}