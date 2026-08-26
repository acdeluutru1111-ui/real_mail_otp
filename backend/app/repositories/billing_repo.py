"""Billing dedupe data access — the core double-charge guard.

The atomic ``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` on the dedupe
UNIQUE key ``(provider, domain_type, inbox_id, mid, user_id)`` is what makes
concurrent duplicate reads charge exactly once (plan 2.2 / 7.1 / 7.2 step 5).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import BillingRead, BillingSource
from app.repositories.base import BaseRepository


class BillingRepository(BaseRepository):
    """Dedupe-insert + existence check for :class:`app.db.models.BillingRead`."""

    async def insert_read(
        self,
        *,
        user_id: uuid.UUID,
        inbox_id: uuid.UUID,
        provider: str,
        domain_type: str,
        mid: str,
        amount_vnd: int,
        source: BillingSource = BillingSource.read,
    ) -> uuid.UUID | None:
        """Atomic dedupe insert. Returns the new id, or ``None`` on conflict.

        This is THE billing gate: the wallet is only debited when a non-None id
        is returned. On conflict the row already exists (already charged or a
        concurrent duplicate), so the caller must NOT charge again.
        """
        stmt = (
            pg_insert(BillingRead)
            .values(
                user_id=user_id,
                inbox_id=inbox_id,
                provider=provider,
                domain_type=domain_type,
                mid=mid,
                amount_vnd=amount_vnd,
                source=source,
            )
            .on_conflict_do_nothing(constraint="uq_billing_reads_dedupe")
            .returning(BillingRead.id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def exists_read(
        self,
        *,
        user_id: uuid.UUID,
        inbox_id: uuid.UUID,
        provider: str,
        domain_type: str,
        mid: str,
    ) -> bool:
        """Cheap pre-check (plan 7.2 step 2): has this read already been charged?

        A cache miss never means "not charged"; always consult this / the insert.
        """
        stmt = select(BillingRead.id).where(
            BillingRead.provider == provider,
            BillingRead.domain_type == domain_type,
            BillingRead.inbox_id == inbox_id,
            BillingRead.mid == mid,
            BillingRead.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None
