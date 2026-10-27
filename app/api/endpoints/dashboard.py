# app/api/endpoints/dashboard.py
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

from app.api.deps import get_db
from app.schemas.video import VideoListResponse, VideoResponse
from app.models.user import User
from app.models.video import VideoStatus, VideoVisibility
from app.services.auth_service import get_current_user_from_cookie
from app.services.video_stats_counter import VideoStatsCounter
from app.crud.video import video as video_crud
from app.crud.channel import channel as channel_crud
from app.core.redis import get_redis

router = APIRouter(prefix="/me/videos", tags=["dashboard"])


@router.get("", response_model=VideoListResponse)
async def list_my_videos(
    page: int = Query(1, ge=1, description="1-indexed page number"),
    page_size: int = Query(24, ge=1, le=50, description="Videos per page"),
    status_filter: Optional[VideoStatus] = Query(None, alias="status", description="Filter by processing status"),
    visibility: Optional[VideoVisibility] = Query(None, description="Filter by visibility"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user_from_cookie),  # required, not optional
):
    """
    List the current user's own videos (paginated), newest first.

    Unlike the public feed, this returns videos regardless of status or
    visibility (draft, processing, failed, private, scheduled, etc.) —
    the owner is allowed to see and manage everything on their channel.
    """
    channel = await channel_crud.get_by_user_id(db, current_user.id)
    if not channel:
        # No channel yet == no videos yet, not an error.
        return VideoListResponse(items=[], total=0, page=page, page_size=page_size, has_next=False)

    skip = (page - 1) * page_size

    # Same sequential-access constraint as the public feed: these all
    # share `db` (one AsyncSession per request), so they must not be
    # run concurrently via asyncio.gather().
    videos = await video_crud.get_by_channel(
        db, channel.id, skip=skip, limit=page_size, status=status_filter, visibility=visibility
    )
    total = await video_crud.count_by_channel(
        db, channel.id, status=status_filter, visibility=visibility
    )

    if not videos:
        return VideoListResponse(items=[], total=total, page=page, page_size=page_size, has_next=False)

    # Like counts touch `db` on a cache miss too — sequential for the
    # same reason, and defensively wrapped so one bad video can't fail
    # the whole page.
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