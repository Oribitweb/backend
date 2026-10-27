from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

from app.api.deps import get_db
from app.schemas.video import VideoDetailResponse, VideoListResponse, VideoResponse, VideoUpdate
from app.models.user import User
from app.services.auth_service import get_optional_current_user_from_cookie
from app.services.video_service import VideoService, get_video_by_public_id
from app.services.video_stats_counter import VideoStatsCounter
from app.crud.video import video as video_crud
from app.crud.reaction import reaction as reaction_crud
from app.core.redis import get_redis

router = APIRouter(prefix="/videos")

def get_video_service(db: AsyncSession = Depends(get_db)) -> VideoService:
    return VideoService(db)

@router.get("", response_model=VideoListResponse)
async def list_videos(
    page: int = Query(1, ge=1, description="1-indexed page number"),
    page_size: int = Query(24, ge=1, le=50, description="Videos per page"),
    category_id: Optional[int] = Query(None, description="Filter by category"),
    is_short: Optional[bool] = Query(None, description="Filter shorts vs. regular videos"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """
    List publicly visible videos (paginated), newest first.

    Only returns videos that are:
    - not soft-deleted
    - finished processing (status == ready)
    - visibility == public, or visibility == scheduled with scheduled_at in the past
    """
    skip = (page - 1) * page_size

    # NOTE: these must run sequentially, not via asyncio.gather(), because
    # they share the same AsyncSession (`db`) for this request.
    # SQLAlchemy's AsyncSession is not safe for concurrent use — running
    # two queries on it at once can corrupt results or raise
    # "InvalidRequestError: session is already flushing" under load.
    # (Concurrency across *different* requests is unaffected, since each
    # request gets its own session from get_db.)
    videos = await video_crud.get_public_feed(
        db, skip=skip, limit=page_size, category_id=category_id, is_short=is_short
    )
    total = await video_crud.count_public_feed(db, category_id=category_id, is_short=is_short)

    if not videos:
        return VideoListResponse(items=[], total=total, page=page, page_size=page_size, has_next=False)

    # Like counts also touch `db` on a cache miss, so these are sequential
    # for the same reason. Each lookup is defensively wrapped so one bad
    # video (e.g. a Redis hiccup) can't fail the whole page.
    stats_counter = VideoStatsCounter(redis=redis, db=db)
    like_counts = []
    for v in videos:
        try:
            like_counts.append(await stats_counter.likes.get(v.id))
        except Exception:
            like_counts.append(0)

    items = [
        VideoResponse(
            id=v.id,
            public_id=v.public_id.public_id if v.public_id else None,
            status=v.status,
            visibility=v.visibility,
            title=v.title,
            description=v.description,
            channel_name=v.channel.name if v.channel else "",
            duration_seconds=v.duration_seconds,
            thumbnail_url=v.thumbnail_url,
            hls_manifest_url=v.hls_manifest_url,
            sprite_url=v.sprite_url,
            sprite_vtt_url=v.sprite_vtt_url,
            sprite_tile_width=v.sprite_tile_width,
            sprite_tile_height=v.sprite_tile_height,
            sprite_columns=v.sprite_columns,
            sprite_rows=v.sprite_rows,
            created_at=v.created_at,
            channel_id=v.channel_id,
            file_url=v.file_url,
            like_count=like_count,
            view_count=v.stats.views_count if v.stats else 0,
            published_at=v.published_at,
            updated_at=v.updated_at,
        )
        for v, like_count in zip(videos, like_counts)
    ]

    has_next = skip + len(videos) < total

    return VideoListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_next=has_next,
    )


@router.get("/{public_id}", response_model=VideoDetailResponse)
async def get_video(
    public_id: str,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: Optional[User] = Depends(get_optional_current_user_from_cookie),
):
    """
    Get complete video details including HLS manifest URL, thumbnail, etc.
    """

    video = await get_video_by_public_id(db, public_id, current_user.id if current_user else None)

    # `video.channel` is a lazily-loaded relationship. Accessing it directly
    # here (after the await above has returned) triggers a synchronous
    # lazy-load on what is actually an AsyncSession, which raises
    # sqlalchemy.exc.MissingGreenlet ("greenlet_spawn has not been called").
    # Explicitly awaiting db.refresh(..., attribute_names=["channel"]) loads
    # the relationship safely within an async context before we touch it.
    if "channel" not in video.__dict__:
        await db.refresh(video, attribute_names=["channel"])
    if "stats" not in video.__dict__:
        await db.refresh(video, attribute_names=["stats"])

    current_user_reaction = None
    if current_user:
        user_reaction = await reaction_crud.get_by_video_and_user(db=db, user_id=current_user.id, video_id=video.id)
        if user_reaction:
            current_user_reaction = user_reaction.type.value

    like_count = await VideoStatsCounter(redis=redis, db=db).likes.get(video.id)

    # Return all fields required by VideoDetailResponse
    return VideoDetailResponse(
        id=video.id,
        status=video.status,
        title=video.title,
        description=video.description,
        channel_name=video.channel.name,
        duration_seconds=video.duration_seconds,
        thumbnail_url=video.thumbnail_url,
        hls_manifest_url=video.hls_manifest_url,
        sprite_url=video.sprite_url,
        sprite_vtt_url=video.sprite_vtt_url,
        sprite_tile_width=video.sprite_tile_width,
        sprite_tile_height=video.sprite_tile_height,
        sprite_columns=video.sprite_columns,
        sprite_rows=video.sprite_rows,
        created_at=video.created_at,
        channel_id=video.channel_id,
        file_url=video.file_url,
        like_count=like_count,
        comment_count=video.stats.comments_count if video.stats else 0,
        view_count=video.stats.views_count if video.stats else 0,
        current_user_reaction=current_user_reaction,
        is_published=video.is_published,
        can_edit=(current_user.id == video.channel.user_id if current_user else False),
        published_at=video.published_at,
        updated_at=video.updated_at,
    )


@router.put("/{public_id}")
async def update_video(
    public_id: str,
    update_form: VideoUpdate,
    db: AsyncSession = Depends(get_db),
    video_service: VideoService = Depends(get_video_service),
    current_user: Optional[User] = Depends(get_optional_current_user_from_cookie),
):
    return await video_service.update_video(
        db,
        public_id,
        current_user,
        update_form
    )
    