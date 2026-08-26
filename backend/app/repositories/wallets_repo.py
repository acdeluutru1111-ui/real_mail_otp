"""Wallet data access — the money-moving primitives.

The service layer composes these inside the read-and-charge transaction
(plan 7.2). This repo only performs the locked read and balance mutation; it
does NOT decide *whether* to charge (that's business logic + billing dedupe).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models import Wallet
from app.repositories.base import BaseRepository


class WalletsRepository(BaseRepository):
    """CRUD + locked mutation for :class:`app.db.models.Wallet`."""

    async def get(self, user_id: uuid.UUID) -> Wallet | None:
        return await self.session.get(Wallet, user_id)

    async def get_for_update(self, user_id: uuid.UUID) -> Wallet | None:
        """``SELECT ... FOR UPDATE`` on the wallet row.

        Must run inside an open transaction. Serializes concurrent debits so the
        balance check + decrement is race-free (plan 7.2 step 6).
        """
        stmt = select(Wallet).where(Wallet.user_id == user_id).with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, user_id: uuid.UUID, balance_vnd: int = 0) -> Wallet:
        wallet = Wallet(user_id=user_id, balance_vnd=balance_vnd, version=0)
        self.session.add(wallet)
        await self.session.flush()
        return wallet

    async def apply_debit(self, wallet: Wallet, amount_vnd: int) -> Wallet:
        """Subtract ``amount_vnd`` and bump ``version`` on a locked wallet.

        Caller MUST have obtained ``wallet`` via :meth:`get_for_update` and
        already verified ``wallet.balance_vnd >= amount_vnd``. The DB CHECK
        constraint is the final backstop against a negative balance.
        """
        wallet.balance_vnd -= amount_vnd
        wallet.version += 1
        await self.session.flush()
        return wallet

    async def apply_credit(self, wallet: Wallet, amount_vnd: int) -> Wallet:
        """Add ``amount_vnd`` and bump ``version`` on a locked wallet."""
        wallet.balance_vnd += amount_vnd
        wallet.version += 1
        await self.session.flush()
        return wallet
