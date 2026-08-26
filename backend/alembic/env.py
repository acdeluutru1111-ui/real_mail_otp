"""Alembic environment — async-aware, driven by app settings.

- Reads ``DATABASE_URL`` from :func:`app.core.config.get_settings` (never from
  alembic.ini), so migrations and the app share one config source.
- ``target_metadata = Base.metadata`` (imports ``app.db.models`` so every table
  is registered) to support autogenerate.
- Runs migrations through an async engine (asyncpg).
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig

# Ensure the backend/ dir (containing the `app` package) is importable when
# alembic is invoked from the backend/ working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

# Import Base and ensure all models are registered on its metadata.
from app.db.session import Base
import app.db.models  # noqa: F401  (registers tables on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not configured; set it in the environment/.env "
            "before running alembic."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to script output without a live DB connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Create an async engine and run migrations within a connection."""
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
