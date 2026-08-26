"""Refresh tokens and payment proofs (P1-02, P1-03).

This migration adds:
1. P1-02: refresh_tokens table for JWT refresh token rotation/revocation
2. P1-03: proof columns on payments table for manual proof persistence

Revision ID: 0004_refresh_tokens_and_proofs
Revises: 0003_billing_invariants_and_idempotency
Create Date: 2024-01-04 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_refresh_tokens_and_proofs"
down_revision: Union[str, None] = "0003_billing_and_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    # --- P1-02: Create refresh_tokens table ---
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=_UUID_DEFAULT,
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("family_id", sa.String(length=64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_refresh_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
    )
    # Unique index on jti for fast lookup
    op.create_index(
        "ix_refresh_tokens_jti", "refresh_tokens", ["jti"], unique=True
    )
    # Index on family_id for family revocation
    op.create_index(
        "ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"]
    )
    # Index on user_id + created_at for listing user's tokens
    op.create_index(
        "ix_refresh_tokens_user_created",
        "refresh_tokens",
        ["user_id", sa.text("created_at DESC")],
    )
    # Index on expires_at for cleanup jobs
    op.create_index(
        "ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"]
    )

    # --- P1-03: Add proof columns to payments table ---
    op.add_column(
        "payments",
        sa.Column("proof_note", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("proof_reference", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("proof_submitted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # --- Drop proof columns from payments ---
    op.drop_column("payments", "proof_submitted_at")
    op.drop_column("payments", "proof_reference")
    op.drop_column("payments", "proof_note")

    # --- Drop refresh_tokens table ---
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_created", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_jti", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
