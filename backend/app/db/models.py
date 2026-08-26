"""SQLAlchemy 2.0 typed ORM models for the 7-table schema (plan section 7).

Design invariants (plan 7 / 7.1):
- ``wallets`` is the single source of money with ``CHECK balance_vnd >= 0`` and an
  optimistic ``version`` column.
- ``ledger_entries`` is an immutable, append-only log; corrections are reversal
  entries, never edits/deletes.
- ``messages`` stores only sanitized metadata (no body) with UNIQUE(inbox_id, mid).
- ``billing_reads`` records each charged read; the billing dedupe UNIQUE key is
  ``(provider, domain_type, inbox_id, mid, user_id)`` — NEVER the payload.
- ``payments`` uses UNIQUE(provider, provider_ref) for provider idempotency.

UUID primary keys default to PostgreSQL ``gen_random_uuid()`` (pgcrypto), with a
Python ``uuid4`` fallback so ORM inserts work without a DB round-trip.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

# Server-side default expression for UUID primary keys (needs pgcrypto).
_UUID_SERVER_DEFAULT = text("gen_random_uuid()")


def _uuid_column() -> Mapped[uuid.UUID]:
    """Standard UUID primary key column (server default + python fallback)."""
    return mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=_UUID_SERVER_DEFAULT,
        default=uuid.uuid4,
    )


def _created_at_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


# --- Enums ------------------------------------------------------------------


class UserStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"
    deleted = "deleted"


class InboxStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    deleted = "deleted"


class LedgerEntryType(str, enum.Enum):
    credit = "credit"  # top-up / promo grant
    debit = "debit"  # successful read charge
    reversal = "reversal"  # correction / refund adjustment


class BillingSource(str, enum.Enum):
    read = "read"  # a normal charged detail read


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    pending_review = "pending_review"
    paid = "paid"
    rejected = "rejected"
    expired = "expired"


class IdempotencyStatus(str, enum.Enum):
    """Status of an idempotency key (P1-01)."""
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"


# --- Tables -----------------------------------------------------------------


class User(Base):
    """Authenticated end user."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_column()
    email: Mapped[str] = mapped_column(
        String(320),
        unique=True,
        index=True,
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="user_status"),
        nullable=False,
        server_default=UserStatus.active.value,
    )
    created_at: Mapped[datetime] = _created_at_column()

    wallet: Mapped["Wallet"] = relationship(
        back_populates="user", uselist=False
    )


class Wallet(Base):
    """Single credit wallet per user — the only source of money.

    ``version`` supports optimistic locking; the debit/credit path uses
    ``SELECT ... FOR UPDATE`` (see wallets_repo) plus this version bump.
    """

    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance_vnd >= 0", name="ck_wallets_balance_non_negative"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    balance_vnd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    user: Mapped["User"] = relationship(back_populates="wallet")


class LedgerEntry(Base):
    """Immutable, append-only money movement log.

    Every credit/debit/reversal is recorded here. Rows are never updated or
    deleted; corrections are new ``reversal`` entries.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index(
            "ix_ledger_entries_user_created",
            "user_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[LedgerEntryType] = mapped_column(
        SAEnum(LedgerEntryType, name="ledger_entry_type"), nullable=False
    )
    # Signed amount in VND (debits negative, credits positive) by convention.
    amount_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = _created_at_column()


class Inbox(Base):
    """Temporary inbox owned by a user. Encrypted address/key at rest."""

    __tablename__ = "inboxes"
    __table_args__ = (
        Index(
            "ix_inboxes_user_status_created",
            "user_id",
            "status",
            text("created_at DESC"),
        ),
        Index("ix_inboxes_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    domain_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Deterministic hash for lookups; never stores the raw address.
    address_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # Encrypted blobs (never returned to the browser).
    address_encrypted: Mapped[str] = mapped_column(String, nullable=False)
    key_encrypted: Mapped[str] = mapped_column(String, nullable=False)
    # Upstream-provided credential timestamp.
    timestamp: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # P1-07: Credential version for cache invalidation on rotation.
    # Incremented when upstream credentials are rotated.
    credential_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    status: Mapped[InboxStatus] = mapped_column(
        SAEnum(InboxStatus, name="inbox_status"),
        nullable=False,
        server_default=InboxStatus.active.value,
    )
    created_at: Mapped[datetime] = _created_at_column()
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    messages: Mapped[list["Message"]] = relationship(back_populates="inbox")


class Message(Base):
    """Sanitized message metadata only (no body). UNIQUE(inbox_id, mid)."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("inbox_id", "mid", name="uq_messages_inbox_mid"),
        Index(
            "ix_messages_inbox_received",
            "inbox_id",
            text("received_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_column()
    inbox_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("inboxes.id", ondelete="CASCADE"),
        nullable=False,
    )
    mid: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_sanitized: Mapped[str | None] = mapped_column(String, nullable=True)
    sender_sanitized: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discovered_at: Mapped[datetime] = _created_at_column()

    inbox: Mapped["Inbox"] = relationship(back_populates="messages")


class BillingRead(Base):
    """Record of a charged read. The UNIQUE key IS the billing dedupe key.

    Dedupe key: (provider, domain_type, inbox_id, mid, user_id) — plan 2.2 / 7.1.
    The charge transaction relies on this constraint via
    ``INSERT ... ON CONFLICT DO NOTHING RETURNING id``.
    """

    __tablename__ = "billing_reads"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "domain_type",
            "inbox_id",
            "mid",
            "user_id",
            name="uq_billing_reads_dedupe",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    inbox_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("inboxes.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    domain_type: Mapped[str] = mapped_column(String(64), nullable=False)
    mid: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[BillingSource] = mapped_column(
        SAEnum(BillingSource, name="billing_source"),
        nullable=False,
        server_default=BillingSource.read.value,
    )
    created_at: Mapped[datetime] = _created_at_column()


class Payment(Base):
    """Manual QR payment (v1). UNIQUE(provider, provider_ref) for idempotency.

    P1-08: Added credited_vnd snapshot column to record the actual credit granted
    at approval time (may differ from amount_vnd due to package bonuses).
    P1-03: Added proof_note, proof_reference, proof_submitted_at for manual proof.
    """

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_ref", name="uq_payments_provider_ref"
        ),
        CheckConstraint("amount_vnd > 0", name="ck_payments_amount_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    package_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # P1-08: Snapshot of credits granted at approval time (audit trail)
    credited_vnd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # P0-02: Admin who approved the payment (audit trail)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    approval_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # P1-03: Manual proof fields
    proof_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    proof_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    proof_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status"),
        nullable=False,
        server_default=PaymentStatus.pending.value,
    )
    created_at: Mapped[datetime] = _created_at_column()
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class IdempotencyKey(Base):
    """Durable idempotency keys for request deduplication (P1-01).

    Stores idempotency keys with their status and associated resource IDs.
    Used primarily for create_inbox to ensure concurrent/retry requests
    with the same key return the same inbox.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "operation", "key", name="uq_idempotency_keys_user_op_key"
        ),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Hash of request parameters (domain, etc.) to detect conflicting requests
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IdempotencyStatus] = mapped_column(
        SAEnum(IdempotencyStatus, name="idempotency_status"),
        nullable=False,
        server_default=IdempotencyStatus.in_progress.value,
    )
    # ID of the created resource (e.g., inbox_id)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Summary of the response (for debugging, never contains secrets)
    response_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = _created_at_column()
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RefreshToken(Base):
    """Refresh token for JWT rotation/revocation (P1-02).

    Each refresh token has a unique jti (JWT ID) and belongs to a family.
    When a token is used, it's revoked and a new one is issued in the same family.
    If a revoked token is reused (replay attack), the entire family is revoked.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_created", "user_id", text("created_at DESC")),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_column()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Unique JWT ID for this token
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # SHA-256 hash of the token (for verification without storing the token)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # Family ID groups tokens from the same login session for bulk revocation
    family_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # When the token was revoked (null if still valid)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at_column()
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


__all__ = [
    "Base",
    "User",
    "Wallet",
    "LedgerEntry",
    "Inbox",
    "Message",
    "BillingRead",
    "Payment",
    "IdempotencyKey",
    "RefreshToken",
    "UserStatus",
    "InboxStatus",
    "LedgerEntryType",
    "BillingSource",
    "PaymentStatus",
    "IdempotencyStatus",
]
