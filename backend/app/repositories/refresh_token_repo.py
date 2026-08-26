"""Refresh token data access (P1-02).

Repository for managing refresh tokens with rotation and revocation support.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db.models import RefreshToken
from app.repositories.base import BaseRepository


class RefreshTokenRepository(BaseRepository):
    """CRUD + revocation for :class:`app.db.models.RefreshToken`."""

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        jti: str,
        token_hash: str,
        family_id: str,
        expires_at: datetime,
    ) -> RefreshToken:
        """Create a new refresh token record."""
        token = RefreshToken(
            user_id=user_id,
            jti=jti,
            token_hash=token_hash,
            family_id=family_id,
            expires_at=expires_at,
        )
        self.session.add(token)
        await self.session.flush()
        return token

    async def find_by_jti(self, jti: str) -> RefreshToken | None:
        """Find a refresh token by its JWT ID."""
        stmt = select(RefreshToken).where(RefreshToken.jti == jti)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def revoke_by_jti(self, jti: str) -> bool:
        """Revoke a single token by its JWT ID.

        Returns True if a token was revoked, False if not found or already revoked.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        result = await self.session.execute(stmt)
        return result.rowcount > 0

    async def revoke_family(self, family_id: str) -> int:
        """Revoke all tokens in a family (reuse detection).

        Returns the number of tokens revoked.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        result = await self.session.execute(stmt)
        return result.rowcount

    async def is_revoked(self, jti: str) -> bool:
        """Check if a token is revoked.

        Returns True if the token is revoked or not found.
        """
        token = await self.find_by_jti(jti)
        if token is None:
            return True  # Not found = treat as revoked
        return token.revoked_at is not None

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Revoke all tokens for a user (logout all sessions).

        Returns the number of tokens revoked.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        result = await self.session.execute(stmt)
        return result.rowcount
