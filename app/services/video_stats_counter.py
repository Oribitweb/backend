import logging
from typing import Optional
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.video_stats import VideoStats
from app.models.video_reaction import VideoReaction, ReactionType

logger = logging.getLogger(__name__)


# ── TTLs ──────────────────────────────────────────────────────────────────────

_DEDUP_TTL = 60 * 60 * 24 * 90   # 90 days  (likes)
_VIEW_DEDUP_TTL = 60 * 60 * 12   # 12h window (views)
_HOT_TTL   = 60 * 60 * 24 * 7    # 7 days


# ── Redis key scheme ──────────────────────────────────────────────────────────
#
#   stats:hot:{stat}:{video_id}               unflushed delta (int, can be –ve)
#   stats:dedup:{stat}:{video_id}:{user_id}   per-user presence key
#   stats:pending                             set of video_ids needing a flush
#
# All four sub-counters write to the same stats:pending set so the Celery beat
# only has to drain one structure.

PENDING_KEY = "stats:pending"

def _hot_key(stat: str, video_id: int) -> str:
    return f"stats:hot:{stat}:{video_id}"

def _dedup_key(stat: str, video_id: int, user_id: int) -> str:
    return f"stats:dedup:{stat}:{video_id}:{user_id}"


# ── Lua scripts ───────────────────────────────────────────────────────────────
#
# Each script is a single atomic Redis operation.
# KEYS[1] = dedup key   KEYS[2] = hot key   KEYS[3] = pending set
# ARGV[1] = dedup TTL   ARGV[2] = hot TTL   ARGV[3] = video_id (str)

_DEDUP_INCR_SCRIPT = """
local set = redis.call("SET", KEYS[1], "1", "NX", "EX", ARGV[1])
if set then
    redis.call("INCR",   KEYS[2])
    redis.call("EXPIRE", KEYS[2], ARGV[2])
    redis.call("SADD",   KEYS[3], ARGV[3])
    return 1
end
return 0
"""
# 1 = new entry recorded; 0 = dedup key already present (cache hit — skip DB)

_DEDUP_DECR_SCRIPT = """
local val = redis.call("GETDEL", KEYS[1])
if val then
    redis.call("DECR",   KEYS[2])
    redis.call("EXPIRE", KEYS[2], ARGV[1])
    redis.call("SADD",   KEYS[3], ARGV[2])
    return 1
end
return 0
"""
# 1 = removed; 0 = dedup key absent (may be expired — fall back to DB)


# ── Stats that clamp to zero in the DB flush ──────────────────────────────────
# Views can only go up, so no clamp needed there.
_CLAMP_ZERO = {"likes"}

# Ordered list used by _flush_one to pipeline all GETDEL calls predictably
_ALL_STATS = ["likes", "views"]


# ── Internal sub-counters ─────────────────────────────────────────────────────

class _DeduplicatedCounter:
    """
    Per-user dedup, bidirectional (increment + decrement).
    Used for: likes.

    Dedup-key expiry guard
    ----------------------
    increment: Lua SETNX winning (→ 1) means "apparently new". We then do a
               cheap DB EXISTS check. If the row is already there (key had
               expired), we roll back the hot-counter increment and re-hydrate
               the dedup key. Two concurrent requests that both pass the miss
               check are still safe: Lua SETNX is atomic, so only one wins;
               the loser gets 0 and falls straight through to get().

    decrement: Lua GETDEL returning 0 (key absent) falls back to DB. If the
               row exists, the key had expired; we apply the delta directly
               without a dedup key to delete.
    """

    def __init__(
        self,
        stat: str,
        reaction_type: ReactionType,
        db_column,                  # e.g. VideoStats.likes_count
        redis: Redis,
        db: AsyncSession,
    ):
        self._stat          = stat
        self._reaction_type = reaction_type
        self._db_column     = db_column
        self._redis         = redis
        self._db            = db
        # register_script caches the SHA → subsequent calls use EVALSHA not EVAL
        self._incr_script   = redis.register_script(_DEDUP_INCR_SCRIPT)
        self._decr_script   = redis.register_script(_DEDUP_DECR_SCRIPT)

    async def _db_has(self, video_id: int, user_id: int) -> bool:
        res = await self._db.execute(
            select(VideoReaction.id)
            .where(
                VideoReaction.video_id == video_id,
                VideoReaction.user_id  == user_id,
                VideoReaction.type     == self._reaction_type,
            )
            .limit(1)
        )
        return res.scalar_one_or_none() is not None

    async def _rollback_stale_incr(self, video_id: int, user_id: int) -> None:
        """Undo a false increment caused by an expired dedup key."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.decr(_hot_key(self._stat, video_id))
            pipe.expire(_hot_key(self._stat, video_id), _HOT_TTL)
            pipe.sadd(PENDING_KEY, video_id)
            pipe.set(_dedup_key(self._stat, video_id, user_id), "1", ex=_DEDUP_TTL)
            await pipe.execute()

    async def increment(self, video_id: int, user_id: int) -> int:
        result = await self._incr_script(
            keys=[
                _dedup_key(self._stat, video_id, user_id),
                _hot_key(self._stat, video_id),
                PENDING_KEY,
            ],
            args=[_DEDUP_TTL, _HOT_TTL, video_id],
        )
        if result == 1 and await self._db_has(video_id, user_id):
            await self._rollback_stale_incr(video_id, user_id)
        return await self.get(video_id)

    async def decrement(self, video_id: int, user_id: int) -> Optional[int]:
        result = await self._decr_script(
            keys=[
                _dedup_key(self._stat, video_id, user_id),
                _hot_key(self._stat, video_id),
                PENDING_KEY,
            ],
            args=[_HOT_TTL, video_id],
        )
        if result == 0:
            if not await self._db_has(video_id, user_id):
                return None  # genuinely never acted — not just an expired key
            # Dedup key had expired but DB confirms the row — apply delta
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.decr(_hot_key(self._stat, video_id))
                pipe.expire(_hot_key(self._stat, video_id), _HOT_TTL)
                pipe.sadd(PENDING_KEY, video_id)
                await pipe.execute()
        return await self.get(video_id)

    async def get(self, video_id: int) -> int:
        raw = await self._redis.get(_hot_key(self._stat, video_id))
        delta = int(raw) if raw else 0
        res = await self._db.execute(
            select(self._db_column).where(VideoStats.video_id == video_id)
        )
        db_count = res.scalar_one_or_none() or 0
        return max(0, db_count + delta)


class _ViewCounter:
    """
    Per-user dedup, unidirectional (increment only).
    Used for: views.

    No DB fallback on dedup miss — a view re-counted after the 12h window
    expires is acceptable (it's a short TTL by design, not a persistent state).
    """

    def __init__(self, redis: Redis, db: AsyncSession):
        self._redis = redis
        self._db    = db

    async def increment(self, video_id: int, user_id: int) -> int:
        is_new = await self._redis.set(
            _dedup_key("views", video_id, user_id), "1",
            nx=True, ex=_VIEW_DEDUP_TTL,
        )
        if is_new:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(_hot_key("views", video_id))
                pipe.sadd(PENDING_KEY, video_id)
                await pipe.execute()
        return await self.get(video_id)

    async def get(self, video_id: int) -> int:
        raw = await self._redis.get(_hot_key("views", video_id))
        delta = int(raw) if raw else 0
        res = await self._db.execute(
            select(VideoStats.views_count).where(VideoStats.video_id == video_id)
        )
        db_count = res.scalar_one_or_none() or 0
        return db_count + delta


class _SimpleCounter:
    """
    No per-user dedup, bidirectional.
    Used for: comments (deleted comment → decrement), shares (increment only
    in practice, but decrement exposed for completeness).
    """

    def __init__(self, stat: str, db_column, redis: Redis, db: AsyncSession):
        self._stat      = stat
        self._db_column = db_column
        self._redis     = redis
        self._db        = db

    async def _apply(self, video_id: int, amount: int) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incrby(_hot_key(self._stat, video_id), amount)
            pipe.expire(_hot_key(self._stat, video_id), _HOT_TTL)
            pipe.sadd(PENDING_KEY, video_id)
            await pipe.execute()

    async def increment(self, video_id: int, count: int = 1) -> int:
        await self._apply(video_id, count)
        return await self.get(video_id)

    async def decrement(self, video_id: int, count: int = 1) -> int:
        await self._apply(video_id, -count)
        return await self.get(video_id)

    async def get(self, video_id: int) -> int:
        raw = await self._redis.get(_hot_key(self._stat, video_id))
        delta = int(raw) if raw else 0
        res = await self._db.execute(
            select(self._db_column).where(VideoStats.video_id == video_id)
        )
        db_count = res.scalar_one_or_none() or 0
        return max(0, db_count + delta)


# ── Public class ──────────────────────────────────────────────────────────────

class VideoStatsCounter:
    """
    Unified counter for all video stats.

    Usage
    -----
        counter = VideoStatsCounter(redis, db)

        await counter.likes.increment(video_id, user_id)
        await counter.likes.decrement(video_id, user_id)   # → int | None

        await counter.views.increment(video_id, user_id)



    Celery beat
    -----------
        @app.task
        async def flush_video_stats():
            counter = VideoStatsCounter(redis, db)
            await counter.flush_pending()

    Flush design
    ------------
    flush_pending() atomically claims the entire stats:pending set via RENAME
    (any writes that arrive during the flush land in a fresh key and are picked
    up next beat). For each video it then pipelines GETDEL across all three hot
    keys and issues a single UPSERT to DB — one DB round-trip per video regardless
    of how many stats changed.
    """

    def __init__(self, redis: Redis, db: AsyncSession):
        self.likes    = _DeduplicatedCounter(
            "likes", ReactionType.LIKE, VideoStats.likes_count, redis, db
        )
        self.views    = _ViewCounter(redis, db)
        self._redis   = redis
        self._db      = db

    # ── Flush ─────────────────────────────────────────────────────────────── #

    async def flush_pending(self) -> None:
        """
        Celery beat entry point — drains all pending video deltas to Postgres.

        Uses RENAME to atomically claim the pending set before iterating,
        so concurrent writes during the flush are queued for the next beat
        rather than silently dropped.
        """
        processing_key = "stats:processing"
        try:
            await self._redis.rename(PENDING_KEY, processing_key)
        except Exception:
            return  # stats:pending didn't exist — nothing to flush

        video_ids = await self._redis.smembers(processing_key)
        await self._redis.delete(processing_key)

        for raw_id in video_ids:
            await self._flush_one(int(raw_id))

    async def _flush_one(self, video_id: int) -> None:
        """
        Atomically drain all hot counters for one video (pipeline GETDEL),
        then issue a single UPSERT.

        GETDEL claims the delta atomically — any increments that arrive after
        the pipeline executes land in a fresh key and are caught next beat.

        UPSERT (INSERT ... ON CONFLICT DO UPDATE) is used instead of a bare
        UPDATE so that a video_stats row is created on first flush rather than
        silently doing nothing.

        On DB failure all claimed deltas are restored to their hot keys and the
        video_id is re-added to the pending set so the next beat retries.
        """
        async with self._redis.pipeline(transaction=True) as pipe:
            for stat in _ALL_STATS:
                pipe.getdel(_hot_key(stat, video_id))
            results = await pipe.execute()

        deltas: dict[str, int] = {
            stat: int(raw) if raw else 0
            for stat, raw in zip(_ALL_STATS, results)
        }

        if not any(deltas.values()):
            return  # nothing changed — skip the DB round-trip

        def _conflict_expr(stat: str):
            """Expression used in the ON CONFLICT UPDATE clause."""
            col  = getattr(VideoStats, f"{stat}_count")
            expr = col + deltas[stat]
            return func.greatest(0, expr) if stat in _CLAMP_ZERO else expr

        try:
            await self._db.execute(
                pg_insert(VideoStats)
                .values(
                    video_id=video_id,
                    # INSERT initial values: clamp negatives to 0 (e.g. net
                    # unlikes before the row exists should not create a -N row)
                    **{f"{stat}_count": max(0, deltas[stat]) for stat in _ALL_STATS},
                )
                .on_conflict_do_update(
                    index_elements=["video_id"],
                    set_={f"{stat}_count": _conflict_expr(stat) for stat in _ALL_STATS},
                )
            )
            await self._db.commit()
            logger.info("Flushed stats for video %s: %s", video_id, deltas)

        except Exception:
            await self._db.rollback()
            logger.error(
                "Failed to flush stats for video %s — restoring deltas to Redis",
                video_id,
                exc_info=True,
            )
            # Restore all non-zero deltas so the next beat picks them up
            async with self._redis.pipeline(transaction=True) as pipe:
                for stat, delta in deltas.items():
                    if delta:
                        pipe.incrby(_hot_key(stat, video_id), delta)
                pipe.sadd(PENDING_KEY, video_id)
                await pipe.execute()