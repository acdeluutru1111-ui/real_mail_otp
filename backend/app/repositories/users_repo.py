"""User data access."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models import User, UserStatus
from app.repositories.base import BaseRepository


class UsersRepository(BaseRepository):
    """CRUD for :class:`app.db.models.User`."""

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Look up a user by email (case-insensitive on the stored value).

        Emails are stored normalized (lower-cased) by :meth:`create`, so an
        equality match on the normalized input is sufficient.
        """
        stmt = select(User).where(User.email == email)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        email: str,
        password_hash: str,
        status: UserStatus = UserStatus.active,
    ) -> User:
        user = User(
            email=email.strip().lower(),
            password_hash=password_hash,
            status=status,
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def list_active(self, limit: int = 100) -> list[User]:
        stmt = (
            select(User)
            .where(User.status == UserStatus.active)
            .order_by(User.created_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
