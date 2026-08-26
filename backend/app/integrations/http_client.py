"""Shared async HTTP client for upstream calls (plan section 3 / Bước 3).

Provides:

* A process-wide :class:`httpx.AsyncClient` factory with connect / read / total
  timeouts and a bounded connection pool derived from :func:`get_settings`.
* A module-level :class:`asyncio.Semaphore` that caps concurrent upstream calls
  per replica.
* :func:`request` — a thin wrapper that acquires the semaphore, performs the
  request, and maps every ``httpx`` failure (and non-2xx / bad-JSON response) to
  the corresponding :class:`AppError` from the error taxonomy.
* P1-07: Retry with exponential backoff + jitter for safe errors (5xx, timeout,
  connection error). Respects Retry-After header from upstream.

Redaction invariant: this module never logs URL, query, payload, request/response
body, cookies or key material. Only safe metadata (status code, retry count,
provider) may be logged by callers.
"""

from __future__ import annotations

import asyncio
import random
from json import JSONDecodeError
from typing import Any, Mapping, Optional

import httpx

from app.core.config import get_settings
from app.core.errors import (
    UpstreamAuthError,
    UpstreamBadResponseError,
    UpstreamRateLimitError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.core.logging import get_logger

logger = get_logger("integrations.http")

# P1-07: Retry configuration
_BASE_BACKOFF_SECONDS = 0.5  # Initial backoff
_MAX_BACKOFF_SECONDS = 10.0  # Cap on backoff
_JITTER_FACTOR = 0.5  # Random jitter factor (0.5 = ±50%)

# --- Bounded concurrency across all upstream calls on this replica ----------
_settings = get_settings()
_semaphore: asyncio.Semaphore = asyncio.Semaphore(
    max(1, _settings.upstream_max_concurrency)
)

# --- Lazily-created shared client -------------------------------------------
_client: Optional[httpx.AsyncClient] = None
_client_lock: asyncio.Lock = asyncio.Lock()


def _build_timeout() -> httpx.Timeout:
    """Build an :class:`httpx.Timeout` from settings.

    ``upstream_timeout`` is the overall (total) budget; connect/read are the
    finer-grained phases.
    """
    settings = get_settings()
    return httpx.Timeout(
        timeout=settings.upstream_timeout,
        connect=settings.upstream_connect_timeout,
        read=settings.upstream_read_timeout,
        write=settings.upstream_connect_timeout,
        pool=settings.upstream_connect_timeout,
    )


def _build_limits() -> httpx.Limits:
    """Bounded connection pool sized from the concurrency cap."""
    settings = get_settings()
    max_conns = max(1, settings.upstream_max_concurrency)
    return httpx.Limits(
        max_connections=max_conns,
        max_keepalive_connections=max_conns,
        keepalive_expiry=30.0,
    )


async def get_client() -> httpx.AsyncClient:
    """Return the process-wide shared :class:`httpx.AsyncClient` (create once)."""
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=_build_timeout(),
                limits=_build_limits(),
                follow_redirects=True,
                trust_env=False,
            )
    return _client


async def close_client() -> None:
    """Dispose the shared client (wire into app shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def reset_semaphore(limit: int | None = None) -> None:
    """Reset the concurrency semaphore (used in tests / after settings change)."""
    global _semaphore
    max_conc = limit if limit is not None else get_settings().upstream_max_concurrency
    _semaphore = asyncio.Semaphore(max(1, max_conc))


def _raise_for_status(status_code: int) -> None:
    """Map a non-2xx status code to the appropriate :class:`AppError`.

    Never includes URL/body in the raised message.
    """
    if 200 <= status_code < 300:
        return
    if status_code in (401, 403):
        raise UpstreamAuthError()
    if status_code == 429:
        raise UpstreamRateLimitError()
    if 500 <= status_code < 600:
        # Upstream server-side failure — treat as a bad/unavailable response.
        raise UpstreamBadResponseError()
    # Any other non-2xx (4xx we do not special-case) => bad response.
    raise UpstreamBadResponseError()


async def request(
    method: str,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    cookies: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, Any]] = None,
    json_body: Any = None,
    expect_json: bool = True,
) -> Any:
    """Perform an upstream request and return parsed JSON (or the response).

    Concurrency is bounded by the module semaphore. All failures are mapped to
    the error taxonomy:

    * :class:`httpx.TimeoutException`            -> ``UPSTREAM_TIMEOUT``
    * :class:`httpx.ConnectError` / transport    -> ``UPSTREAM_UNAVAILABLE``
    * response 401 / 403                          -> ``UPSTREAM_AUTH``
    * response 429                                -> ``UPSTREAM_RATE_LIMIT``
    * other non-2xx / invalid JSON                -> ``UPSTREAM_BAD_RESPONSE``

    Returns the decoded JSON body when ``expect_json`` is True, otherwise the raw
    :class:`httpx.Response`.
    """
    client = await get_client()

    async with _semaphore:
        try:
            response = await client.request(
                method,
                url,
                headers=dict(headers) if headers else None,
                cookies=dict(cookies) if cookies else None,
                params=dict(params) if params else None,
                json=json_body,
            )
        except httpx.TimeoutException as exc:  # includes connect/read/pool timeouts
            # Safe: log only the error class name, never the URL.
            logger.warning("upstream timeout", extra={"extra_fields": {"error_code": "UPSTREAM_TIMEOUT"}})
            raise UpstreamTimeoutError() from exc
        except httpx.ConnectError as exc:
            logger.warning("upstream connect error", extra={"extra_fields": {"error_code": "UPSTREAM_UNAVAILABLE"}})
            raise UpstreamUnavailableError() from exc
        except httpx.TransportError as exc:
            # Any other transport-level problem (read/write/protocol).
            logger.warning("upstream transport error", extra={"extra_fields": {"error_code": "UPSTREAM_UNAVAILABLE"}})
            raise UpstreamUnavailableError() from exc

    # Map non-2xx to taxonomy (outside the semaphore is fine).
    _raise_for_status(response.status_code)

    if not expect_json:
        return response

    try:
        return response.json()
    except (ValueError, JSONDecodeError) as exc:  # bad/empty JSON body
        logger.warning(
            "upstream bad json",
            extra={"extra_fields": {"error_code": "UPSTREAM_BAD_RESPONSE"}},
        )
        raise UpstreamBadResponseError() from exc


async def request_text(
    method: str,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    cookies: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, Any]] = None,
    json_body: Any = None,
) -> str:
    """Like :func:`request` but returns the response text body (2xx only)."""
    response = await request(
        method,
        url,
        headers=headers,
        cookies=cookies,
        params=params,
        json_body=json_body,
        expect_json=False,
    )
    return response.text


def _calculate_backoff(attempt: int, retry_after: float | None = None) -> float:
    """Calculate backoff with exponential increase and jitter (P1-07).

    Args:
        attempt: The current attempt number (0-indexed).
        retry_after: Optional Retry-After value from upstream (in seconds).

    Returns:
        Backoff duration in seconds.
    """
    if retry_after is not None and retry_after > 0:
        # Respect upstream's Retry-After, but cap it
        base = min(retry_after, _MAX_BACKOFF_SECONDS)
    else:
        # Exponential backoff: 0.5, 1, 2, 4, ... capped at max
        base = min(_BASE_BACKOFF_SECONDS * (2 ** attempt), _MAX_BACKOFF_SECONDS)

    # Add jitter: ±50% of base
    jitter = base * _JITTER_FACTOR * (2 * random.random() - 1)
    return max(0.1, base + jitter)


def _is_retryable_error(exc: Exception) -> bool:
    """Check if an error is safe to retry (P1-07).

    Retryable errors:
    - Timeout errors (transient)
    - Connection errors (transient)
    - Transport errors (transient)
    - 5xx server errors (transient)
    - 429 rate limit (with backoff)

    Non-retryable errors:
    - 4xx client errors (except 429)
    - Auth errors (401, 403)
    - Bad response format
    """
    return isinstance(exc, (UpstreamTimeoutError, UpstreamUnavailableError, UpstreamRateLimitError))


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After header from response (P1-07).

    Returns seconds to wait, or None if not present/parseable.
    """
    retry_after = response.headers.get("retry-after")
    if not retry_after:
        return None
    try:
        # Try parsing as integer seconds
        return float(retry_after)
    except ValueError:
        # Could be HTTP-date format, but we'll skip that complexity
        return None


async def request_with_retry(
    method: str,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    cookies: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, Any]] = None,
    json_body: Any = None,
    expect_json: bool = True,
    max_retries: int | None = None,
    deadline: float | None = None,
) -> Any:
    """Perform an upstream request with retry on transient errors (P1-07).

    Retries on:
    - Timeout errors
    - Connection/transport errors
    - 5xx server errors
    - 429 rate limit (respects Retry-After header)

    Does NOT retry on:
    - 4xx client errors (except 429)
    - Auth errors (401, 403)
    - Bad response format

    Args:
        method: HTTP method.
        url: Request URL.
        headers: Optional headers.
        cookies: Optional cookies.
        params: Optional query params.
        json_body: Optional JSON body.
        expect_json: Whether to parse response as JSON.
        max_retries: Max retry attempts (default from config).
        deadline: Total time budget in seconds (default from config).

    Returns:
        Parsed JSON or raw response.
    """
    settings = get_settings()
    if max_retries is None:
        max_retries = settings.upstream_max_retries
    if deadline is None:
        deadline = settings.upstream_timeout * (max_retries + 1)

    client = await get_client()
    start_time = asyncio.get_event_loop().time()
    last_exception: Exception | None = None
    retry_after_hint: float | None = None

    for attempt in range(max_retries + 1):
        # Check deadline
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= deadline:
            break

        # Wait before retry (not on first attempt)
        if attempt > 0:
            backoff = _calculate_backoff(attempt - 1, retry_after_hint)
            # Don't wait longer than remaining deadline
            remaining = deadline - elapsed
            wait_time = min(backoff, remaining)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            retry_after_hint = None  # Reset for next attempt

        try:
            async with _semaphore:
                response = await client.request(
                    method,
                    url,
                    headers=dict(headers) if headers else None,
                    cookies=dict(cookies) if cookies else None,
                    params=dict(params) if params else None,
                    json=json_body,
                )

            # Check for retryable status codes
            if response.status_code == 429:
                retry_after_hint = _parse_retry_after(response)
                if attempt < max_retries:
                    logger.warning(
                        "upstream rate limit, will retry",
                        extra={"extra_fields": {"attempt": attempt + 1, "max_retries": max_retries}},
                    )
                    last_exception = UpstreamRateLimitError()
                    continue
                raise UpstreamRateLimitError()

            if 500 <= response.status_code < 600:
                retry_after_hint = _parse_retry_after(response)
                if attempt < max_retries:
                    logger.warning(
                        "upstream 5xx, will retry",
                        extra={"extra_fields": {"attempt": attempt + 1, "status": response.status_code}},
                    )
                    last_exception = UpstreamBadResponseError()
                    continue
                raise UpstreamBadResponseError()

            # Non-retryable status codes
            _raise_for_status(response.status_code)

            # Success - parse response
            if not expect_json:
                return response

            try:
                return response.json()
            except (ValueError, JSONDecodeError) as exc:
                logger.warning(
                    "upstream bad json",
                    extra={"extra_fields": {"error_code": "UPSTREAM_BAD_RESPONSE"}},
                )
                raise UpstreamBadResponseError() from exc

        except httpx.TimeoutException as exc:
            logger.warning(
                "upstream timeout",
                extra={"extra_fields": {"attempt": attempt + 1, "error_code": "UPSTREAM_TIMEOUT"}},
            )
            last_exception = UpstreamTimeoutError()
            if attempt >= max_retries:
                raise UpstreamTimeoutError() from exc
            continue

        except httpx.ConnectError as exc:
            logger.warning(
                "upstream connect error",
                extra={"extra_fields": {"attempt": attempt + 1, "error_code": "UPSTREAM_UNAVAILABLE"}},
            )
            last_exception = UpstreamUnavailableError()
            if attempt >= max_retries:
                raise UpstreamUnavailableError() from exc
            continue

        except httpx.TransportError as exc:
            logger.warning(
                "upstream transport error",
                extra={"extra_fields": {"attempt": attempt + 1, "error_code": "UPSTREAM_UNAVAILABLE"}},
            )
            last_exception = UpstreamUnavailableError()
            if attempt >= max_retries:
                raise UpstreamUnavailableError() from exc
            continue

    # Exhausted retries or deadline
    if last_exception is not None:
        raise last_exception
    raise UpstreamTimeoutError()
