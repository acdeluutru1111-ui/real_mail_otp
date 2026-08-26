"""Async repository layer (plan section 5/7).

Each repository wraps an :class:`sqlalchemy.ext.asyncio.AsyncSession` and exposes
focused data-access methods only — no business rules. The read-and-charge
transaction (plan 7.2) is composed by the service layer from:
``billing_repo.insert_read`` (ON CONFLICT DO NOTHING dedupe) +
``wallets_repo.get_for_update`` / ``apply_debit`` + ``ledger_repo.append``.
"""

from __future__ import annotations

from app.repositories.billing_repo import BillingRepository
from app.repositories.inbox_repo import InboxRepository
from app.repositories.ledger_repo import LedgerRepository
from app.repositories.message_repo import MessageRepository
from app.repositories.payments_repo import PaymentsRepository
from app.repositories.users_repo import UsersRepository
from app.repositories.wallets_repo import WalletsRepository

__all__ = [
    "UsersRepository",
    "WalletsRepository",
    "LedgerRepository",
    "InboxRepository",
    "MessageRepository",
    "BillingRepository",
    "PaymentsRepository",
]
