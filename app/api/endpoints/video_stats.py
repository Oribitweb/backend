import logging
from typing import Literal
from sqlalchemy.orm import Session
from redis.asyncio import Redis
from redis.exceptions import RedisError

from fastapi import APIRouter, Depends, HTTPException, status, Path

from app.models.user import User
from app.models.video_reaction import ReactionType
from app.schemas.reaction import ReactionResponse
from app.services.auth_service import get_current_user_from_cookie
from app.services.video_service import get_video_by_public_id
from app.services.video_stats_counter import VideoStatsCounter
from app.tasks.reactions_tasks import upsert_video_reaction, delsert_video_reaction
from app.api.deps import get_db
from app.core.redis import get_redis

router = APIRouter(prefix="/videos", tags=["video_stats"])

logger = logging.getLogger(__name__)


# ── Reaction Endpoints ────────────────────────────────────────────────────────

@router.post(
    "/{video_public_id}/react/{reaction_type}",
    response_model=ReactionResponse,
    status_code=status.HTTP_200_OK,
)
async def create_update_reaction(
    reaction_type: Literal["like", "dislike"] = Path(...),
    video_public_id: str = Path(...),
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    counter = VideoStatsCounter(redis, db)
    video = await get_video_by_public_id(
        db,
        public_id=video_public_id,
        requesting_user_id=current_user.id,
    )

    try:
        if reaction_type == ReactionType.LIKE:
            new_count = await counter.likes.increment(video.id, current_user.id)
        else:
            # Switching to dislike — decrement like if one exists
            new_count = await counter.likes.decrement(video.id, current_user.id)
            if new_count is None:
                # User never liked — no counter change needed
                new_count = await counter.likes.get(video.id)
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reaction service temporarily unavailable. Please retry.",
        ) from exc

    try:
        upsert_video_reaction.delay(current_user.id, reaction_type, video.id)
    except Exception as exc:
        logger.error(
            "Celery broker unreachable during upsert_reaction",
            extra={"user_id": current_user.id, "video_id": video.id, "exc": str(exc)},
        )
        # Compensate Redis — reverse the operation we just performed.
        # Skip compensation when reaction_type is dislike and new_count was None
        # (no counter was touched, nothing to reverse).
        try:
            if reaction_type == ReactionType.LIKE:
                await counter.likes.decrement(video.id, current_user.id)
            elif new_count is not None:
                await counter.likes.increment(video.id, current_user.id)
        except RedisError:
            logger.critical(
                "Redis compensation failed — counter is now inconsistent",
                extra={"video_id": video.id, "user_id": current_user.id},
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not queue reaction. Please retry.",
        )

    return ReactionResponse(video_public_id=video.public_id_str, likes_count=new_count)


@router.delete(
    "/{video_public_id}/react/{reaction_type}",
    response_model=ReactionResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_reaction(
    reaction_type: Literal["like", "dislike"] = Path(...),
    video_public_id: str = Path(...),
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    counter = VideoStatsCounter(redis, db)
    video = await get_video_by_public_id(
        db,
        public_id=video_public_id,
        requesting_user_id=current_user.id,
    )

    try:
        if reaction_type == ReactionType.LIKE:
            new_count = await counter.likes.decrement(video.id, current_user.id)
            if new_count is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Reaction type mismatch or no reaction found.",
                )
        else:
            new_count = await counter.likes.get(video.id)
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reaction service temporarily unavailable. Please retry.",
        ) from exc

    try:
        delsert_video_reaction.delay(reaction_type, current_user.id, video.id)
    except Exception as exc:
        logger.error(
            "Celery broker unreachable during delsert_reaction",
            extra={"user_id": current_user.id, "video_id": video.id},
            exc_info=exc,
        )
        # Compensate Redis — restore the like we just removed
        try:
            if reaction_type == ReactionType.LIKE:
                await counter.likes.increment(video.id, current_user.id)
        except RedisError:
            logger.critical(
                "Redis compensation failed — counter is now inconsistent",
                extra={"video_id": video.id, "user_id": current_user.id},
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not queue reaction removal. Please retry.",
        )

    return ReactionResponse(video_public_id=video.public_id_str, likes_count=new_count)


# ── View Endpoints ────────────────────────────────────────────────────────────

@router.post(
    "/{video_public_id}/view",
    status_code=status.HTTP_200_OK,
)
async def view_video(
    video_public_id: str = Path(...),
    current_user: User = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    counter = VideoStatsCounter(redis, db)
    video = await get_video_by_public_id(
        db,
        public_id=video_public_id,
        requesting_user_id=current_user.id,
    )

    try:
        await counter.views.increment(video.id, current_user.id)
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="View service temporarily unavailable. Please retry.",
        ) from exc

    return {"message": "View submitted"}