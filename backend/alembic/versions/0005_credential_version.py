"""Add credential_version to inboxes (P1-07).

This migration adds the credential_version column to the inboxes table.
This column is used to invalidate payload cache when upstream credentials
are rotated.

Revision ID: 0005_credential_version
Revises: 0004_refresh_tokens_and_proofs
Create Date: 2024-01-05 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0005_credential_version"
down_revision: Union[str, None] = "0004_refresh_tokens_and_proofs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add credential_version column with default value 1
    op.add_column(
        "inboxes",
        sa.Column(
            "credential_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade() -> None:
    op.drop_column("inboxes", "credential_version")
