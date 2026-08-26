"""Idempotency key data access (P1-01 durable inbox idempotency).

Provides atomic claim/complete/fail operations for idempotency keys stored in
the ``idempotency_keys`` table. Used by InboxService to ensure that concurrent
or retried create_inbox requests with the same key return the same inbox.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import IdempotencyKey, IdempotencyStatus
from app.repositories.base import BaseRepository


class IdempotencyRepository(BaseRepository):
    """Atomic idempotency key operations for durable request deduplication."""

    async def claim_key(
        self,
        *,
        user_id: uuid.UUID,
        operation: str,
        key: str,
        request_fingerprint: str,
        ttl_seconds: int = 86400,  # 24 hours default
    ) -> tuple[IdempotencyKey | None, str]:
        """Attempt to claim an idempotency key atomically.

        Returns:
            (existing_record, status) where:
            - If key is new: (new_record with status=in_progress, "claimed")
            - If key exists and completed: (existing_record, "completed")
            - If key exists and in_progress: (existing_record, "in_progress")
            - If key exists but fingerprint differs: (existing_record, "conflict")
            - If key exists and failed: (existing_record, "failed") - can retry
        """
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        # Try to insert a new key with ON CONFLICT DO NOTHING
        stmt = (
            pg_insert(IdempotencyKey)
            .values(
                user_id=user_id,
                operation=operation,
                key=key,
                request_fingerprint=request_fingerprint,
                status=IdempotencyStatus.in_progress,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_keys_user_op_key")
            .returning(IdempotencyKey.id)
        )
        result = await self.session.execute(stmt)
        new_id = result.scalar_one_or_none()

        if new_id is not None:
            # Successfully claimed a new key
            record = await self.session.get(IdempotencyKey, new_id)
            return record, "claimed"

        # Key already exists - fetch it
        existing = await self._get_key(user_id, operation, key)
        if existing is None:
            # Race condition: key was deleted between insert and select
            return None, "retry"

        # Check fingerprint match
        if existing.request_fingerprint != request_fingerprint:
            return existing, "conflict"

        # Same fingerprint - return based on status
        status_str = existing.status.value if hasattr(existing.status, "value") else str(existing.status)
        return existing, status_str

    async def mark_completed(
        self,
        key_id: uuid.UUID,
        resource_id: str,
        response_summary: str | None = None,
    ) -> IdempotencyKey | None:
        """Mark an in_progress key as completed with the created resource ID."""
        stmt = (
            update(IdempotencyKey)
            .where(
                IdempotencyKey.id == key_id,
                IdempotencyKey.status == IdempotencyStatus.in_progress,
            )
            .values(
                status=IdempotencyStatus.completed,
                resource_id=resource_id,
                response_summary=response_summary,
            )
            .returning(IdempotencyKey.id)
        )
        result = await self.session.execute(stmt)
        updated_id = result.scalar_one_or_none()
        if updated_id:
            return await self.session.get(IdempotencyKey, updated_id)
        return None

    async def mark_failed(
        self,
        key_id: uuid.UUID,
        response_summary: str | None = None,
    ) -> IdempotencyKey | None:
        """Mark an in_progress key as failed (allows retry with same key)."""
        stmt = (
            update(IdempotencyKey)
            .where(
                IdempotencyKey.id == key_id,
                IdempotencyKey.status == IdempotencyStatus.in_progress,
            )
            .values(
                status=IdempotencyStatus.failed,
                response_summary=response_summary,
            )
            .returning(IdempotencyKey.id)
        )
        result = await self.session.execute(stmt)
        updated_id = result.scalar_one_or_none()
        if updated_id:
            return await self.session.get(IdempotencyKey, updated_id)
        return None

    async def _get_key(
        self, user_id: uuid.UUID, operation: str, key: str
    ) -> IdempotencyKey | None:
        """Fetch an idempotency key by its unique tuple."""
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.operation == operation,
            IdempotencyKey.key == key,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, key_id: uuid.UUID) -> IdempotencyKey | None:
        """Fetch an idempotency key by ID."""
        return await self.session.get(IdempotencyKey, key_id)
