"""Billing invariants, FK RESTRICT, idempotency keys (P0-02, P0-04, P1-01, P1-08).

This migration adds:
1. P1-08: Unique constraint on ledger credit entries per payment (prevents double-credit)
2. P1-08: CHECK constraint on payment amount (must be positive)
3. P1-08: Snapshot columns on payments (credited_vnd, approved_by, approval_reason)
4. P1-08: Change FK ondelete from CASCADE to RESTRICT for accounting tables
5. P1-01: idempotency_keys table for durable request deduplication
6. P1-01: idempotency_status enum

Revision ID: 0003_billing_invariants_and_idempotency
Revises: 0002_add_user_auth
Create Date: 2024-01-03 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_billing_and_idempotency"
down_revision: Union[str, None] = "0002_add_user_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# New enum for idempotency status
idempotency_status = postgresql.ENUM(
    "in_progress", "completed", "failed", name="idempotency_status"
)

_UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    bind = op.get_bind()

    # --- P1-08: Add unique constraint for ledger credit per payment ---
    # This ensures each payment can only create one credit entry (idempotency)
    # Using a partial unique index: only applies to credit entries with payment reference
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_credit_per_payment
        ON ledger_entries (reference_type, reference_id)
        WHERE reference_type = 'payment' AND type = 'credit'
    """)

    # --- P1-08: Add CHECK constraint for positive payment amount ---
    # Note: We need to handle existing data that might violate this
    op.execute("""
        ALTER TABLE payments
        ADD CONSTRAINT ck_payments_amount_positive
        CHECK (amount_vnd > 0)
        NOT VALID
    """)
    # Validate the constraint (will fail if existing data violates it)
    op.execute("ALTER TABLE payments VALIDATE CONSTRAINT ck_payments_amount_positive")

    # --- P1-08: Add snapshot columns to payments ---
    op.add_column(
        "payments",
        sa.Column("credited_vnd", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "payments",
        sa.Column("approval_reason", sa.String(length=500), nullable=True),
    )

    # --- P1-08: Change FK ondelete from CASCADE to RESTRICT for accounting tables ---
    # This prevents accidental deletion of accounting history when a user is deleted
    # Note: This requires dropping and recreating the FK constraints

    # ledger_entries.user_id
    op.drop_constraint(
        "fk_ledger_entries_user_id_users", "ledger_entries", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_ledger_entries_user_id_users",
        "ledger_entries",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # payments.user_id
    op.drop_constraint("fk_payments_user_id_users", "payments", type_="foreignkey")
    op.create_foreign_key(
        "fk_payments_user_id_users",
        "payments",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- P1-01: Create idempotency_status enum ---
    idempotency_status.create(bind, checkfirst=True)

    # --- P1-01: Create idempotency_keys table ---
    op.create_table(
        "idempotency_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=_UUID_DEFAULT,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="idempotency_status", create_type=False),
            server_default="in_progress",
            nullable=False,
        ),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("response_summary", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_idempotency_keys_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_keys"),
        sa.UniqueConstraint(
            "user_id", "operation", "key", name="uq_idempotency_keys_user_op_key"
        ),
    )
    op.create_index(
        "ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()

    # --- Drop idempotency_keys table ---
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")

    # --- Drop idempotency_status enum ---
    idempotency_status.drop(bind, checkfirst=True)

    # --- Restore FK ondelete to CASCADE ---
    op.drop_constraint("fk_payments_user_id_users", "payments", type_="foreignkey")
    op.create_foreign_key(
        "fk_payments_user_id_users",
        "payments",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        "fk_ledger_entries_user_id_users", "ledger_entries", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_ledger_entries_user_id_users",
        "ledger_entries",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # --- Drop snapshot columns from payments ---
    op.drop_column("payments", "approval_reason")
    op.drop_column("payments", "approved_by")
    op.drop_column("payments", "credited_vnd")

    # --- Drop CHECK constraint ---
    op.drop_constraint("ck_payments_amount_positive", "payments", type_="check")

    # --- Drop unique index for ledger credit per payment ---
    op.execute("DROP INDEX IF EXISTS uq_ledger_credit_per_payment")
