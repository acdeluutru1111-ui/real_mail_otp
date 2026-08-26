"""Async SQLAlchemy engine, session factory, and DI session dependency.

The single source of truth for money (wallets/ledger) lives in PostgreSQL, so the
DB layer favours **short transactions** and a pooled async engine. Configuration
is read from :func:`app.core.config.get_settings` (``DATABASE_URL``), consistent
with Step 1 conventions.

Usage in FastAPI routes::

    from app.db.session import get_session

    @router.get(...)
    async def handler(session: AsyncSession = Depends(get_session)):
        ...

Lifecycle helpers :func:`init_engine` / :func:`dispose_engine` are wired into the
app startup/shutdown (or used by scripts/tests) to create and tear down the pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models (SQLAlchemy 2.0 style)."""


# Module-level singletons, created lazily on first use / at startup.
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _build_engine() -> AsyncEngine:
    """Create the async engine from settings with a pooled connection.

    ``pool_pre_ping`` guards against stale Neon connections; ``pool_recycle``
    keeps connections fresh. Transactions are kept short by the session
    dependency (commit/rollback per request), not by long-lived engine state.
    """
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        future=True,
    )


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def init_engine() -> None:
    """Eagerly initialise the engine + session factory (app startup)."""
    get_sessionmaker()


async def dispose_engine() -> None:
    """Dispose the engine and clear singletons (app shutdown / tests)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a short-lived async session.

    Commits on success, rolls back on any exception, and always closes the
    session. Keeps transactions short: one request == one transaction.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
