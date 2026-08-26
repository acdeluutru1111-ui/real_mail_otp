"""Shared base for async repositories.

Repositories are thin data-access objects around an :class:`AsyncSession`. They
contain NO business rules (pricing, ownership policy, rate limiting) — only CRUD
and the exact SQL primitives the service layer composes into transactions.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """Base repository holding the injected async session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
