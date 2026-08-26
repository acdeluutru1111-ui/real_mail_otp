"""In-process RAM cache with TTL and negative-caching support.

A tiny async-safe TTL cache used to reduce upstream calls. Values may hold
message payloads and other sensitive data, so values are NEVER logged.

Default TTLs are read from the application settings
(:func:`app.core.config.get_settings`) via the ``*_ttl`` helpers, falling back
to conservative defaults if a field is missing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from app.core.config import get_settings


class TTLCache:
    """A minimal async-safe cache with lazy TTL expiry and negative caching.

    Internal state:
      * ``_store``: key -> (expires_at_monotonic, value)
      * ``_negative``: key -> expires_at_monotonic

    Expiry is lazy: entries are checked (and deleted) on access. A simple size
    cap evicts expired then oldest entries when ``max_size`` is exceeded.
    """

    def __init__(self, max_size: int = 10000) -> None:
        self._max_size = max_size
        self._store: dict[str, tuple[float, Any]] = {}
        self._negative: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    async def get(self, key: str) -> Optional[Any]:
        """Return the cached value, or ``None`` if missing or expired.

        Expired entries are deleted lazily.
        """
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= self._now():
                self._store.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: Any, ttl: float) -> None:
        """Store ``value`` under ``key`` for ``ttl`` seconds."""
        async with self._lock:
            self._store[key] = (self._now() + ttl, value)
            if len(self._store) > self._max_size:
                self._evict_locked()

    async def delete(self, key: str) -> None:
        """Remove ``key`` from both the positive and negative caches."""
        async with self._lock:
            self._store.pop(key, None)
            self._negative.pop(key, None)

    async def set_negative(self, key: str, ttl: float) -> None:
        """Mark ``key`` as a known-negative (miss) for ``ttl`` seconds."""
        async with self._lock:
            self._negative[key] = self._now() + ttl
            if len(self._negative) > self._max_size:
                self._evict_negative_locked()

    async def is_negative(self, key: str) -> bool:
        """Return ``True`` if ``key`` is currently negatively cached.

        Expired negative entries are deleted lazily.
        """
        async with self._lock:
            expires_at = self._negative.get(key)
            if expires_at is None:
                return False
            if expires_at <= self._now():
                self._negative.pop(key, None)
                return False
            return True

    # --- internal eviction helpers (call while holding the lock) -----------

    def _evict_locked(self) -> None:
        """Drop expired entries first, then oldest until under the cap."""
        now = self._now()
        expired = [k for k, (exp, _) in self._store.items() if exp <= now]
        for k in expired:
            self._store.pop(k, None)
        if len(self._store) <= self._max_size:
            return
        # Evict entries with the soonest expiry (approx. oldest).
        ordered = sorted(self._store.items(), key=lambda kv: kv[1][0])
        overflow = len(self._store) - self._max_size
        for k, _ in ordered[:overflow]:
            self._store.pop(k, None)

    def _evict_negative_locked(self) -> None:
        now = self._now()
        expired = [k for k, exp in self._negative.items() if exp <= now]
        for k in expired:
            self._negative.pop(k, None)
        if len(self._negative) <= self._max_size:
            return
        ordered = sorted(self._negative.items(), key=lambda kv: kv[1])
        overflow = len(self._negative) - self._max_size
        for k, _ in ordered[:overflow]:
            self._negative.pop(k, None)


# Module-level singleton cache instance.
cache = TTLCache()


# --- key builders ----------------------------------------------------------

def list_key(inbox_id: str) -> str:
    return f"list:{inbox_id}"


def payload_key(inbox_id: str, credential_version: int = 1) -> str:
    """Build cache key for payload, including credential version (P1-07).

    When credentials are rotated, the version increments and old cache
    entries naturally expire without serving stale data.
    """
    return f"payload:{inbox_id}:v{credential_version}"


def detail_key(inbox_id: str, mid: str) -> str:
    return f"detail:{inbox_id}:{mid}"


# --- default TTL helpers (read from settings, with fallbacks) --------------

def list_ttl() -> float:
    return float(getattr(get_settings(), "cache_list_ttl", 5.0))


def payload_ttl() -> float:
    return float(getattr(get_settings(), "cache_payload_ttl", 20.0))


def detail_ttl() -> float:
    return float(getattr(get_settings(), "cache_detail_ttl", 180.0))


def negative_ttl() -> float:
    return float(getattr(get_settings(), "cache_list_negative_ttl", 3.0))
