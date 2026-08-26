"""Inbox data access."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import Inbox, InboxStatus
from app.repositories.base import BaseRepository


class InboxRepository(BaseRepository):
    """CRUD + cursor listing + soft delete for :class:`app.db.models.Inbox`."""

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        provider: str,
        domain_type: str,
        address_hash: str,
        address_encrypted: str,
        key_encrypted: str,
        timestamp: str | None = None,
        expires_at: datetime | None = None,
    ) -> Inbox:
        inbox = Inbox(
            user_id=user_id,
            provider=provider,
            domain_type=domain_type,
            address_hash=address_hash,
            address_encrypted=address_encrypted,
            key_encrypted=key_encrypted,
            timestamp=timestamp,
            status=InboxStatus.active,
            expires_at=expires_at,
        )
        self.session.add(inbox)
        await self.session.flush()
        return inbox

    async def get(self, inbox_id: uuid.UUID) -> Inbox | None:
        return await self.session.get(Inbox, inbox_id)

    async def get_owned(
        self, inbox_id: uuid.UUID, user_id: uuid.UUID
    ) -> Inbox | None:
        """Return the inbox only if it belongs to ``user_id`` (ownership check)."""
        stmt = select(Inbox).where(
            Inbox.id == inbox_id,
            Inbox.user_id == user_id,
            Inbox.deleted_at.is_(None),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        cursor: uuid.UUID | None = None,
        limit: int = 20,
        include_deleted: bool = False,
    ) -> list[Inbox]:
        """List a user's inboxes, newest first (uses (user_id,status,created_at))."""
        conditions = [Inbox.user_id == user_id]
        if not include_deleted:
            conditions.append(Inbox.deleted_at.is_(None))
        if cursor is not None:
            anchor = await self.session.get(Inbox, cursor)
            if anchor is not None:
                conditions.append(Inbox.created_at < anchor.created_at)
        stmt = (
            select(Inbox)
            .where(*conditions)
            .order_by(Inbox.created_at.desc(), Inbox.id.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def soft_delete(
        self, inbox_id: uuid.UUID, user_id: uuid.UUID
    ) -> Inbox | None:
        """Mark an owned inbox deleted; returns None if not owned/found."""
        inbox = await self.get_owned(inbox_id, user_id)
        if inbox is None:
            return None
        inbox.status = InboxStatus.deleted
        inbox.deleted_at = datetime.now(timezone.utc)
        await self.session.flush()
        return inbox
