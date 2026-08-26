"""Ledger data access — append-only.

Only appends and reads. The ledger is immutable: never update or delete rows
(corrections are new ``reversal`` entries recorded by the service layer).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models import LedgerEntry, LedgerEntryType
from app.repositories.base import BaseRepository


class LedgerRepository(BaseRepository):
    """Append + cursor-list for :class:`app.db.models.LedgerEntry`."""

    async def append(
        self,
        *,
        user_id: uuid.UUID,
        type: LedgerEntryType,
        amount_vnd: int,
        reference_type: str | None = None,
        reference_id: str | None = None,
    ) -> LedgerEntry:
        """Append an immutable ledger entry within the current transaction."""
        entry = LedgerEntry(
            user_id=user_id,
            type=type,
            amount_vnd=amount_vnd,
            reference_type=reference_type,
            reference_id=reference_id,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        cursor: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[LedgerEntry]:
        """List a user's entries, newest first. ``cursor`` is the last seen id."""
        stmt = (
            select(LedgerEntry)
            .where(LedgerEntry.user_id == user_id)
            .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
            .limit(limit)
        )
        if cursor is not None:
            anchor = await self.session.get(LedgerEntry, cursor)
            if anchor is not None:
                stmt = (
                    select(LedgerEntry)
                    .where(
                        LedgerEntry.user_id == user_id,
                        LedgerEntry.created_at < anchor.created_at,
                    )
                    .order_by(
                        LedgerEntry.created_at.desc(), LedgerEntry.id.desc()
                    )
                    .limit(limit)
                )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_reference(
        self,
        user_id: uuid.UUID,
        reference_type: str,
        reference_id: str,
    ) -> LedgerEntry | None:
        """Find a ledger entry by its reference (for idempotency checks)."""
        stmt = select(LedgerEntry).where(
            LedgerEntry.user_id == user_id,
            LedgerEntry.reference_type == reference_type,
            LedgerEntry.reference_id == reference_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
