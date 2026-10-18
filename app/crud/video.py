from datetime import datetime, timezone
from typing import Optional, List, Tuple

from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models.video import Video, VideoStatus, VideoVisibility
from app.models.video_public_id import VideoPublicId
from app.schemas.video import VideoUpdate


class CRUDVideo:
    def __init__(self, model=Video):
        self.model = model

    async def get(self, db: AsyncSession, id: int) -> Optional[Video]:
        result = await db.execute(
            select(Video)
            .options(selectinload(Video.channel))
            .where(Video.id == id)
        )
        return result.scalar_one_or_none()

    async def get_by_public_id(self, db: AsyncSession, public_id: str):
        result = await db.execute(
            select(Video)
            .join(VideoPublicId, VideoPublicId.internal_id == Video.id)
            .options(
                joinedload(Video.public_id),
                joinedload(Video.stats),
                joinedload(Video.channel)
            )
            .filter(VideoPublicId.public_id == public_id)
            .filter(Video.deleted_at.is_(None))
        )
        return result.unique().scalar_one_or_none()

    async def get_published(self, db: AsyncSession, id: int) -> Optional[Video]:
        """Get published video by ID"""
        result = await db.execute(
            select(self.model).filter(
                self.model.id == id,
                self.model.deleted_at.is_(None),
                self.model.is_published == True
            )
        )
        return result.scalar_one_or_none()

    async def exists(self, db: AsyncSession, id: int) -> bool:
        """Check if video exists (simple existence check)"""
        result = await db.execute(
            select(self.model.id).filter(
                self.model.id == id,
                self.model.deleted_at.is_(None)
            )
        )
        return result.first() is not None

    @staticmethod
    def _public_feed_filter(category_id: Optional[int], is_short: Optional[bool]):
        """
        Shared WHERE clause for the public listing feed:
        - not soft-deleted
        - finished processing (ready)
        - actually visible: PUBLIC, or SCHEDULED whose time has arrived
        """
        now = datetime.now(timezone.utc)
        visible = or_(
            Video.visibility == VideoVisibility.PUBLIC,
            and_(
                Video.visibility == VideoVisibility.SCHEDULED,
                Video.scheduled_at.isnot(None),
                Video.scheduled_at <= now,
            ),
        )

        conditions = [
            Video.deleted_at.is_(None),
            Video.status == VideoStatus.ready,
            visible,
        ]
        if category_id is not None:
            conditions.append(Video.category_id == category_id)
        if is_short is not None:
            conditions.append(Video.is_short == is_short)

        return and_(*conditions)

    @staticmethod
    def _channel_feed_filter(
        channel_id: int,
        status: Optional[VideoStatus] = None,
        visibility: Optional[VideoVisibility] = None,
    ):
        """
        Shared WHERE clause for a channel owner's own listing:
        - not soft-deleted
        - belongs to the given channel
        - NO visibility/readiness filtering — owners see everything
          (draft, processing, failed, private, scheduled, etc.)
        """
        conditions = [
            Video.deleted_at.is_(None),
            Video.channel_id == channel_id,
        ]
        if status is not None:
            conditions.append(Video.status == status)
        if visibility is not None:
            conditions.append(Video.visibility == visibility)

        return and_(*conditions)

    async def get_by_channel(
        self,
        db: AsyncSession,
        channel_id: int,
        skip: int = 0,
        limit: int = 24,
        status: Optional[VideoStatus] = None,
        visibility: Optional[VideoVisibility] = None,
    ) -> List[Video]:
        """Paginated feed of a channel's own videos (any status/visibility), newest first."""
        query = (
            select(Video)
            .options(
                joinedload(Video.channel),
                joinedload(Video.stats),
                joinedload(Video.public_id),
            )
            .where(self._channel_feed_filter(channel_id, status, visibility))
            .order_by(Video.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(query)
        return list(result.unique().scalars().all())

    async def count_by_channel(
        self,
        db: AsyncSession,
        channel_id: int,
        status: Optional[VideoStatus] = None,
        visibility: Optional[VideoVisibility] = None,
    ) -> int:
        query = select(func.count(Video.id)).where(
            self._channel_feed_filter(channel_id, status, visibility)
        )
        result = await db.execute(query)
        return result.scalar_one()
    
    async def get_public_feed(
        self,
        db: AsyncSession,
        skip: int = 0,
        limit: int = 24,
        category_id: Optional[int] = None,
        is_short: Optional[bool] = None,
    ) -> List[Video]:
        """Paginated feed of publicly visible, ready videos, newest first."""
        query = (
            select(Video)
            .options(
                joinedload(Video.channel),
                joinedload(Video.stats),
                joinedload(Video.public_id),
            )
            .where(self._public_feed_filter(category_id, is_short))
            .order_by(Video.published_at.desc().nullslast(), Video.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(query)
        return list(result.unique().scalars().all())

    async def count_public_feed(
        self,
        db: AsyncSession,
        category_id: Optional[int] = None,
        is_short: Optional[bool] = None,
    ) -> int:
        query = select(func.count(Video.id)).where(
            self._public_feed_filter(category_id, is_short)
        )
        result = await db.execute(query)
        return result.scalar_one()

    async def put_video(
        self,
        db: AsyncSession,
        video_id: int,          # required to identify the video
        video: VideoUpdate,
    ) -> Optional[Video]:
        """
        Update an existing video record with the provided fields.
        Only fields that are explicitly set in `video` will be updated.
        Returns the updated VideoModel instance or None if not found.
        """
        # Fetch the existing video
        result = await db.execute(
            select(Video).where(Video.id == video_id)
        )
        db_video = result.scalar_one_or_none()
        if not db_video:
            return None

        # Get only the fields that were provided in the update
        update_data = video.model_dump(exclude_unset=True)

        # Apply updates to the model instance
        for key, value in update_data.items():
            setattr(db_video, key, value)

        # Update the timestamp (if your model has it)
        db_video.updated_at = datetime.now(timezone.utc)

        # Commit and refresh to get the latest state
        await db.commit()
        await db.refresh(db_video)

        return db_video

video = CRUDVideo(Video)