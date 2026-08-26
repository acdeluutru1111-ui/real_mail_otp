"""Initial schema: 7 tables + constraints/indexes (plan section 7 / 7.1).

Creates users, wallets, ledger_entries, inboxes, messages, billing_reads,
payments with all foreign keys, the CHECK balance_vnd >= 0, and every index in
7.1. Enables pgcrypto for gen_random_uuid().

Forward-compatible (expand only): no drops/renames. The two critical UNIQUE
constraints — billing dedupe (provider, domain_type, inbox_id, mid, user_id) and
messages(inbox_id, mid) — are created here, BEFORE any charge code ships
(plan 17: billing unique constraint must exist before enabling charge).

Revision ID: 0001_initial
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Enum types (created explicitly so downgrade can drop them cleanly).
user_status = postgresql.ENUM(
    "active", "suspended", "deleted", name="user_status"
)
inbox_status = postgresql.ENUM(
    "active", "expired", "deleted", name="inbox_status"
)
ledger_entry_type = postgresql.ENUM(
    "credit", "debit", "reversal", name="ledger_entry_type"
)
billing_source = postgresql.ENUM("read", name="billing_source")
payment_status = postgresql.ENUM(
    "pending", "pending_review", "paid", "rejected", "expired",
    name="payment_status",
)

_UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    # pgcrypto provides gen_random_uuid().
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    bind = op.get_bind()
    for enum_type in (
        user_status,
        inbox_status,
        ledger_entry_type,
        billing_source,
        payment_status,
    ):
        enum_type.create(bind, checkfirst=True)

    # --- users --------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=_UUID_DEFAULT,
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="user_status", create_type=False),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )

    # --- wallets ------------------------------------------------------------
    op.create_table(
        "wallets",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "balance_vnd",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_wallets_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_wallets"),
        sa.CheckConstraint(
            "balance_vnd >= 0", name="ck_wallets_balance_non_negative"
        ),
    )

    # --- ledger_entries -----------------------------------------------------
    op.create_table(
        "ledger_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=_UUID_DEFAULT,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM(name="ledger_entry_type", create_type=False),
            nullable=False,
        ),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("reference_type", sa.String(length=64), nullable=True),
        sa.Column("reference_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ledger_entries_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_entries"),
    )
    op.create_index(
        "ix_ledger_entries_user_created",
        "ledger_entries",
        ["user_id", sa.text("created_at DESC")],
    )

    # --- inboxes ------------------------------------------------------------
    op.create_table(
        "inboxes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=_UUID_DEFAULT,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("domain_type", sa.String(length=64), nullable=False),
        sa.Column("address_hash", sa.String(length=128), nullable=False),
        sa.Column("address_encrypted", sa.String(), nullable=False),
        sa.Column("key_encrypted", sa.String(), nullable=False),
        sa.Column("timestamp", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="inbox_status", create_type=False),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_inboxes_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_inboxes"),
    )
    op.create_index(
        "ix_inboxes_user_status_created",
        "inboxes",
        ["user_id", "status", sa.text("created_at DESC")],
    )
    op.create_index("ix_inboxes_expires_at", "inboxes", ["expires_at"])

    # --- messages -----------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=_UUID_DEFAULT,
            nullable=False,
        ),
        sa.Column("inbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mid", sa.String(length=255), nullable=False),
        sa.Column("subject_sanitized", sa.String(), nullable=True),
        sa.Column("sender_sanitized", sa.String(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["inbox_id"],
            ["inboxes.id"],
            name="fk_messages_inbox_id_inboxes",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        # CRITICAL UNIQUE #1: one metadata row per (inbox, mid).
        sa.UniqueConstraint("inbox_id", "mid", name="uq_messages_inbox_mid"),
    )
    op.create_index(
        "ix_messages_inbox_received",
        "messages",
        ["inbox_id", sa.text("received_at DESC")],
    )

    # --- billing_reads ------------------------------------------------------
    op.create_table(
        "billing_reads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=_UUID_DEFAULT,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("domain_type", sa.String(length=64), nullable=False),
        sa.Column("mid", sa.String(length=255), nullable=False),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM(name="billing_source", create_type=False),
            server_default="read",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_billing_reads_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["inbox_id"],
            ["inboxes.id"],
            name="fk_billing_reads_inbox_id_inboxes",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_billing_reads"),
        # CRITICAL UNIQUE #2: the billing dedupe key. Never uses payload.
        sa.UniqueConstraint(
            "provider",
            "domain_type",
            "inbox_id",
            "mid",
            "user_id",
            name="uq_billing_reads_dedupe",
        ),
    )

    # --- payments -----------------------------------------------------------
    op.create_table(
        "payments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=_UUID_DEFAULT,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_ref", sa.String(length=128), nullable=False),
        sa.Column("package_code", sa.String(length=64), nullable=True),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="payment_status", create_type=False),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_payments_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.UniqueConstraint(
            "provider", "provider_ref", name="uq_payments_provider_ref"
        ),
    )


def downgrade() -> None:
    op.drop_table("payments")
    op.drop_table("billing_reads")
    op.drop_index("ix_messages_inbox_received", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_inboxes_expires_at", table_name="inboxes")
    op.drop_index("ix_inboxes_user_status_created", table_name="inboxes")
    op.drop_table("inboxes")
    op.drop_index(
        "ix_ledger_entries_user_created", table_name="ledger_entries"
    )
    op.drop_table("ledger_entries")
    op.drop_table("wallets")
    op.drop_table("users")

    bind = op.get_bind()
    for enum_type in (
        payment_status,
        billing_source,
        ledger_entry_type,
        inbox_status,
        user_status,
    ):
        enum_type.drop(bind, checkfirst=True)
