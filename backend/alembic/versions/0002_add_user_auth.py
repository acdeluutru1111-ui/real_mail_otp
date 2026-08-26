"""Add auth columns to users: email + password_hash (expand only).

Adds ``email`` and ``password_hash`` to the ``users`` table and a UNIQUE index
on ``email`` so users can register/log in with credentials. Expand-only: no
drops/renames of existing columns; this is additive over 0001_initial.

New columns are created NULLABLE first (there may be pre-existing rows without
credentials), then — for a fresh v1 DB with no rows — this is effectively a
plain not-null add. The service layer always supplies both values on insert.

Revision ID: 0002_add_user_auth
Revises: 0001_initial
Create Date: 2024-01-02 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_add_user_auth"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # (a) Add columns as NULLABLE first so pre-existing rows don't violate NOT NULL.
    op.add_column(
        "users",
        sa.Column("email", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(), nullable=True),
    )

    # (b) Backfill safe values for any legacy rows:
    #  - email: a unique, obviously-placeholder address derived from the row id.
    #  - password_hash: a non-usable sentinel ('x' is not a valid bcrypt hash),
    #    forcing a password reset before login can ever succeed.
    op.execute(
        "UPDATE users "
        "SET email = 'user+' || id || '@placeholder.invalid' "
        "WHERE email IS NULL"
    )
    op.execute("UPDATE users SET password_hash = 'x' WHERE password_hash IS NULL")

    # (c) Now enforce NOT NULL.
    op.alter_column("users", "email", existing_type=sa.String(length=320), nullable=False)
    op.alter_column("users", "password_hash", existing_type=sa.String(), nullable=False)

    op.create_index(
        "ix_users_email",
        "users",
        ["email"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "email")
