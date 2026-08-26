"""In-process token-bucket rate limiter (v1) with TTL cleanup (P1-06).

A dependency-free, async-safe rate limiter used to throttle per-user/route
traffic within a single replica. It never raises and never imports FastAPI;
callers inspect the returned :class:`RateLimitResult` and decide how to react
(e.g. translate a blocked result into an HTTP 429 at the edge).

Limits are sourced from :class:`app.core.config.Settings` via
:func:`limits_for_route`, with sane fallbacks when a field is absent.

P1-06 improvements:
- Buckets have a TTL and are cleaned up to prevent unbounded memory growth.
- RateLimitResult includes retry_after for Retry-After header support.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Tuple

try:  # pragma: no cover - config import is best-effort with fallbacks.
    from app.core.config import get_settings
except Exception:  # pragma: no cover
    get_settings = None  # type: ignore[assignment]

# Bucket TTL: buckets not accessed for this duration are eligible for cleanup (P1-06)
_BUCKET_TTL_SECONDS = 3600.0  # 1 hour
# Cleanup interval: how often to run the cleanup task
_CLEANUP_INTERVAL_SECONDS = 300.0  # 5 minutes
# Max buckets before forced cleanup
_MAX_BUCKETS = 100000


@dataclass
class RateLimitResult:
    """Outcome of a rate-limit check.

    Attributes:
        allowed: Whether the request may proceed.
        remaining: Approximate tokens left in the bucket after the check.
        retry_after: Seconds until enough tokens exist (0.0 when allowed).
    """

    allowed: bool
    remaining: float
    retry_after: float


class TokenBucket:
    """A classic token bucket.

    Tokens refill continuously at ``refill_per_sec`` up to ``capacity``.
    The bucket starts full. All time math uses a monotonic clock so it is
    immune to wall-clock adjustments.
    """

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self.capacity: float = float(capacity)
        self.refill_per_sec: float = float(refill_per_sec)
        self.tokens: float = float(capacity)
        self.last: float = time.monotonic()

    def _refill(self) -> None:
        """Add tokens accrued since the last update, capped at capacity."""
        now = time.monotonic()
        elapsed = now - self.last
        if elapsed > 0:
            self.last = now
            if self.refill_per_sec > 0:
                self.tokens = min(
                    self.capacity, self.tokens + elapsed * self.refill_per_sec
                )

    def consume(self, cost: float = 1.0) -> bool:
        """Refill then attempt to take ``cost`` tokens.

        Returns True and subtracts the cost when enough tokens are present,
        otherwise returns False and leaves the bucket unchanged.
        """
        self._refill()
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def retry_after_seconds(self, cost: float = 1.0) -> float:
        """Seconds until the bucket holds at least ``cost`` tokens.

        Returns 0.0 when the tokens are already available. Returns
        ``float("inf")`` when refilling is disabled and tokens are short.
        """
        self._refill()
        deficit = cost - self.tokens
        if deficit <= 0:
            return 0.0
        if self.refill_per_sec <= 0:
            return float("inf")
        return deficit / self.refill_per_sec


class InProcessRateLimiter:
    """Async-safe registry of token buckets keyed by user/ip/route (P1-06).

    Includes TTL-based cleanup to prevent unbounded memory growth.
    """

    def __init__(self) -> None:
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()
        self._last_cleanup: float = time.monotonic()

    @staticmethod
    def build_key(user_id: str, ip: str, route: str) -> str:
        """Compose a stable bucket key from identity + route."""
        return f"{user_id}|{ip}|{route}"

    async def check(
        self, key: str, capacity: float, refill_per_sec: float
    ) -> RateLimitResult:
        """Consume one token for ``key`` and report the outcome.

        Gets or creates the bucket for ``key`` under a lock, consumes a
        single token, and returns a :class:`RateLimitResult` describing
        whether the caller may proceed. Never raises.

        P1-06: Periodically cleans up stale buckets to prevent memory growth.
        """
        async with self._lock:
            # Periodic cleanup (P1-06)
            now = time.monotonic()
            if (
                now - self._last_cleanup > _CLEANUP_INTERVAL_SECONDS
                or len(self._buckets) > _MAX_BUCKETS
            ):
                self._cleanup_stale_buckets_locked(now)
                self._last_cleanup = now

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(capacity, refill_per_sec)
                self._buckets[key] = bucket
            allowed = bucket.consume(1.0)
            remaining = bucket.tokens
            retry_after = 0.0 if allowed else bucket.retry_after_seconds(1.0)
        return RateLimitResult(
            allowed=allowed, remaining=remaining, retry_after=retry_after
        )

    def _cleanup_stale_buckets_locked(self, now: float) -> None:
        """Remove buckets not accessed within TTL (P1-06).

        Must be called while holding the lock.
        """
        cutoff = now - _BUCKET_TTL_SECONDS
        stale_keys = [
            key for key, bucket in self._buckets.items() if bucket.last < cutoff
        ]
        for key in stale_keys:
            del self._buckets[key]

    def bucket_count(self) -> int:
        """Return the current number of buckets (for monitoring/testing)."""
        return len(self._buckets)


# Module-level singleton shared across the process.
rate_limiter = InProcessRateLimiter()


# --- Route -> limits mapping ------------------------------------------------
# Fallbacks mirror the defaults in Settings so this module works standalone.
_DEFAULT_PER_MINUTE: Dict[str, int] = {
    "create": 10,
    "list": 60,
    "detail": 30,
    "refresh": 20,
}

_ROUTE_TO_FIELD: Dict[str, str] = {
    "create": "rate_limit_create_per_minute",
    "list": "rate_limit_list_per_minute",
    "detail": "rate_limit_detail_per_minute",
    "refresh": "rate_limit_refresh_per_minute",
}


def limits_for_route(route: str) -> Tuple[float, float]:
    """Return ``(capacity, refill_per_sec)`` for a route class.

    ``route`` is a coarse class such as ``"create"``, ``"list"``,
    ``"detail"`` or ``"refresh"``. Per-minute limits from config define the
    bucket capacity (burst == one minute's allowance); refill is that same
    allowance spread evenly across 60 seconds. Unknown routes fall back to
    the most restrictive ("create") allowance.
    """
    field = _ROUTE_TO_FIELD.get(route, _ROUTE_TO_FIELD["create"])
    default = _DEFAULT_PER_MINUTE.get(route, _DEFAULT_PER_MINUTE["create"])

    per_minute = default
    if get_settings is not None:
        try:
            settings = get_settings()
            value = getattr(settings, field, None)
            if value is not None:
                per_minute = int(value)
        except Exception:
            per_minute = default

    if per_minute <= 0:
        per_minute = default

    capacity = float(per_minute)
    refill_per_sec = capacity / 60.0
    return capacity, refill_per_sec
