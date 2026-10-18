# app/crud/channel.py
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.channel import Channel


class CRUDChannel:
    def __init__(self, model=Channel):
        self.model = model

    async def get(self, db: AsyncSession, id: int) -> Optional[Channel]:
        result = await db.execute(
            select(self.model).where(self.model.id == id)
        )
        return result.scalar_one_or_none()

    async def get_by_user_id(self, db: AsyncSession, user_id: int) -> Optional[Channel]:
        """
        Look up the channel owned by a given user.

        Assumes a 1:1 user -> channel relationship (Channel.user_id is
        unique). If your product allows a user to own multiple channels,
        this should become get_channels_by_user_id() returning a list,
        and the dashboard route would need a channel_id param to pick one.
        """
        result = await db.execute(
            select(self.model).where(self.model.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def exists_for_user(self, db: AsyncSession, user_id: int) -> bool:
        result = await db.execute(
            select(self.model.id).where(self.model.user_id == user_id)
        )
        return result.first() is not None


channel = CRUDChannel(Channel)