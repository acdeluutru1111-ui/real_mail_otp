"""Payment data access (manual QR flow, v1).

P0-02: Added get_for_update for atomic payment approval with row locking.
P1-08: Added mark_paid_with_audit for recording approval metadata.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import Payment, PaymentStatus
from app.repositories.base import BaseRepository


class PaymentsRepository(BaseRepository):
    """CRUD + status transition for :class:`app.db.models.Payment`.

    UNIQUE(provider, provider_ref) makes ``create`` idempotent per provider ref.
    Actually granting credit on ``paid`` is service-layer logic (transaction),
    not done here.
    """

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        provider: str,
        provider_ref: str,
        amount_vnd: int,
        package_code: str | None = None,
        status: PaymentStatus = PaymentStatus.pending,
    ) -> Payment:
        payment = Payment(
            user_id=user_id,
            provider=provider,
            provider_ref=provider_ref,
            amount_vnd=amount_vnd,
            package_code=package_code,
            status=status,
        )
        self.session.add(payment)
        await self.session.flush()
        return payment

    async def get(self, payment_id: uuid.UUID) -> Payment | None:
        return await self.session.get(Payment, payment_id)

    async def get_for_update(self, payment_id: uuid.UUID) -> Payment | None:
        """``SELECT ... FOR UPDATE`` on the payment row (P0-02).

        Must run inside an open transaction. Serializes concurrent approvals
        so only one transaction can approve a payment at a time.
        """
        stmt = select(Payment).where(Payment.id == payment_id).with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_owned(
        self, payment_id: uuid.UUID, user_id: uuid.UUID
    ) -> Payment | None:
        stmt = select(Payment).where(
            Payment.id == payment_id, Payment.user_id == user_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_provider_ref(
        self, provider: str, provider_ref: str
    ) -> Payment | None:
        stmt = select(Payment).where(
            Payment.provider == provider, Payment.provider_ref == provider_ref
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_paid(self, payment: Payment) -> Payment:
        """Transition a payment to ``paid`` and stamp ``paid_at``.

        Idempotent guard for the transition itself: re-marking a paid payment is
        a no-op here (credit grant idempotency is enforced by the service via the
        ledger reference).

        DEPRECATED: Use mark_paid_with_audit for new code (P0-02/P1-08).
        """
        if payment.status != PaymentStatus.paid:
            payment.status = PaymentStatus.paid
            payment.paid_at = datetime.now(timezone.utc)
            await self.session.flush()
        return payment

    async def mark_paid_with_audit(
        self,
        payment: Payment,
        *,
        credited_vnd: int,
        approved_by: uuid.UUID,
        approval_reason: str | None = None,
    ) -> Payment:
        """Transition a payment to ``paid`` with full audit trail (P0-02/P1-08).

        Records:
        - credited_vnd: The actual credit amount granted (snapshot for audit)
        - approved_by: The admin user who approved the payment
        - approval_reason: Optional reason/note for the approval
        - paid_at: Timestamp of approval

        Idempotent: re-marking a paid payment is a no-op.
        """
        if payment.status != PaymentStatus.paid:
            payment.status = PaymentStatus.paid
            payment.paid_at = datetime.now(timezone.utc)
            payment.credited_vnd = credited_vnd
            payment.approved_by = approved_by
            payment.approval_reason = approval_reason
            await self.session.flush()
        return payment
