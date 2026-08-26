"""Message metadata data access (no body ever stored)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Message
from app.repositories.base import BaseRepository


class MessageRepository(BaseRepository):
    """Upsert + list for :class:`app.db.models.Message` (UNIQUE inbox_id, mid)."""

    async def upsert_metadata(
        self,
        *,
        inbox_id: uuid.UUID,
        mid: str,
        subject_sanitized: str | None = None,
        sender_sanitized: str | None = None,
        received_at: datetime | None = None,
    ) -> Message:
        """Insert or update sanitized metadata keyed by UNIQUE(inbox_id, mid).

        Uses PostgreSQL ``ON CONFLICT (inbox_id, mid) DO UPDATE`` so repeated
        list refreshes keep metadata fresh without duplicating rows.
        """
        stmt = (
            pg_insert(Message)
            .values(
                inbox_id=inbox_id,
                mid=mid,
                subject_sanitized=subject_sanitized,
                sender_sanitized=sender_sanitized,
                received_at=received_at,
            )
            .on_conflict_do_update(
                constraint="uq_messages_inbox_mid",
                set_={
                    "subject_sanitized": subject_sanitized,
                    "sender_sanitized": sender_sanitized,
                    "received_at": received_at,
                },
            )
            .returning(Message.id)
        )
        result = await self.session.execute(stmt)
        message_id = result.scalar_one()
        await self.session.flush()
        return await self.session.get(Message, message_id)  # type: ignore[return-value]

    async def get_by_inbox_and_mid(
        self, inbox_id: uuid.UUID, mid: str
    ) -> Message | None:
        stmt = select(Message).where(
            Message.inbox_id == inbox_id, Message.mid == mid
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_inbox(
        self, inbox_id: uuid.UUID, *, limit: int = 50
    ) -> list[Message]:
        """List an inbox's messages, newest first (uses (inbox_id,received_at))."""
        stmt = (
            select(Message)
            .where(Message.inbox_id == inbox_id)
            .order_by(Message.received_at.desc().nullslast(), Message.id.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
