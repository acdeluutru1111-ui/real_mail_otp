"""Async SmailPro adapter (Bước 3).

Re-implements the ``create`` and ``get_inbox`` (SmailPro hop) logic from the
synchronous ``smailpro_logic_full.py`` in an **async, non-blocking** way:

* Cookies come from :class:`CookieManager` which supports auto-refresh via Gmail
  magic link flow. Falls back to ``get_settings().upstream_cookies()`` if
  CookieManager has no valid cookies.
* ``create`` uses a **finite total deadline** with short jittered retries instead
  of the original blocking ``sleep(2)`` x5 loop. When the deadline is exhausted it
  raises the mapped :class:`AppError`.
* No URL / payload / cookie / key / body is ever logged.

The Sonjj hop (list + detail) lives in :mod:`app.integrations.sonjj`.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Dict, Optional

from app.core.config import get_settings
from app.core.errors import AppError, UpstreamAuthError, UpstreamBadResponseError
from app.core.logging import get_logger
from app.integrations import http_client
from app.integrations.cookie_manager import get_cookie_manager

logger = get_logger("integrations.smailpro")

SMAILPRO_BASE = "https://smailpro.com"
SMAILPRO_CREATE_URL = f"{SMAILPRO_BASE}/app/create"
SMAILPRO_INBOX_URL = f"{SMAILPRO_BASE}/app/inbox"


def _smailpro_headers(origin: bool = False) -> Dict[str, str]:
    """Headers for smailpro.com requests (ported from ``_smailpro_headers``)."""
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.8",
        "content-type": "application/json",
        "priority": "u=1, i",
        "referer": f"{SMAILPRO_BASE}/temporary-email",
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if origin:
        headers["origin"] = SMAILPRO_BASE
    return headers


class SmailProAdapter:
    """Async client for the SmailPro temp-email service.

    Cookies are read from settings on construction and kept in memory only; they
    are never emitted to logs.
    """

    def __init__(self) -> None:
        # Secret: from settings JSON blob, never logged.
        self._settings = get_settings()
        self._cookie_manager = get_cookie_manager()
        # Initialize with env cookies as fallback
        self._cookies: Dict[str, str] = {
            k: v for k, v in self._settings.upstream_cookies().items() if v
        }

    async def _get_cookies(self) -> Dict[str, str]:
        """Get cookies from CookieManager, falling back to env if unavailable.
        
        Prefers CookieManager cookies (which support auto-refresh) over static
        env cookies. Never logs cookie values.
        """
        try:
            cookie_data = await self._cookie_manager.get_current_cookies()
            if cookie_data.is_valid():
                return cookie_data.as_dict_for_requests()
        except Exception:
            # Silently fall back to env cookies on any error
            pass
        
        # Fallback to env cookies
        return self._cookies

    async def _request_with_auth_recovery(self, method: str, url: str, **kwargs: Any) -> Any:
        """Replay exactly once after an auth failure, sharing refresh by generation."""
        kwargs["cookies"] = await self._get_cookies()
        generation = self._cookie_manager.generation
        try:
            return await http_client.request(method, url, **kwargs)
        except UpstreamAuthError:
            if not self._settings.cookie_auto_refresh_enabled:
                raise
            refreshed = await self._cookie_manager.refresh_cookies(
                force=True, stale_generation=generation
            )
            kwargs["cookies"] = refreshed.as_dict_for_requests()
            return await http_client.request(method, url, **kwargs)

    # -- Retry helpers -------------------------------------------------------

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Short jittered backoff (seconds) for retry ``attempt`` (1-based)."""
        base = min(0.25 * (2 ** (attempt - 1)), 1.5)
        return base + random.uniform(0, 0.25)

    # -- create --------------------------------------------------------------

    async def create(
        self,
        domain: str = "outlook.com",
        username: str = "random",
        email_type: str = "real",
        server: str = "1",
    ) -> Dict[str, Any]:
        """Create a temporary email via SmailPro.

        Retries within a **finite total deadline** (``upstream_timeout`` budget,
        with a small ceiling) using short jittered sleeps. Returns
        ``{"address", "key", "timestamp"}`` or raises :class:`AppError` on
        exhaustion. Never logs the address/key/cookies.
        """
        settings = self._settings
        params = {
            "username": username,
            "type": email_type,
            "domain": domain,
            "server": server,
        }

        # Finite deadline: overall budget derived from settings, not unbounded.
        max_attempts = max(1, settings.upstream_max_retries + 1)
        total_budget = max(settings.upstream_timeout * (max_attempts + 1), 5.0)
        deadline = time.monotonic() + total_budget

        last_error: Optional[AppError] = None
        attempt = 0
        while attempt < max_attempts and time.monotonic() < deadline:
            attempt += 1
            try:
                data = await self._request_with_auth_recovery(
                    "GET",
                    SMAILPRO_CREATE_URL,
                    headers=_smailpro_headers(),
                    params=params,
                )
            except AppError as exc:
                last_error = exc
                if not exc.retryable:
                    # e.g. auth failure — no point retrying.
                    raise
                logger.info(
                    "smailpro create retrying",
                    extra={
                        "extra_fields": {
                            "provider": "smailpro",
                            "retry_count": attempt,
                            "error_code": exc.code,
                        }
                    },
                )
            else:
                parsed = self._parse_create(data)
                if parsed is not None:
                    logger.info(
                        "smailpro create ok",
                        extra={
                            "extra_fields": {
                                "provider": "smailpro",
                                "retry_count": attempt,
                                "status": "ok",
                            }
                        },
                    )
                    return parsed
                last_error = UpstreamBadResponseError()

            # Respect the finite deadline: only sleep if budget remains.
            delay = self._backoff_delay(attempt)
            if time.monotonic() + delay >= deadline or attempt >= max_attempts:
                break
            await asyncio.sleep(delay)

        raise last_error or UpstreamBadResponseError()

    @staticmethod
    def _parse_create(data: Any) -> Optional[Dict[str, Any]]:
        """Parse the create response (top-level or nested ``data``)."""
        if not isinstance(data, dict):
            return None
        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        address = data.get("address") or data.get("email") or nested.get("address")
        key = data.get("key") or nested.get("key")
        timestamp = data.get("timestamp") or nested.get("timestamp")
        if address and key:
            return {"address": str(address), "key": str(key), "timestamp": timestamp}
        return None

    # -- inbox (SmailPro hop) -----------------------------------------------

    async def get_inbox_payload(
        self, address: str, timestamp: Any, key: str
    ) -> Optional[str]:
        """POST smailpro.com/app/inbox and return the opaque ``payload`` string.

        Returns ``None`` when the inbox has no payload yet (empty inbox). Never
        logs the address/key/payload. Body is list-wrapped per upstream contract.
        """
        try:
            ts = int(timestamp) if timestamp else 0
        except (TypeError, ValueError):
            ts = 0

        body = [{"address": address, "timestamp": ts, "key": key}]

        data = await self._request_with_auth_recovery(
            "POST",
            SMAILPRO_INBOX_URL,
            headers=_smailpro_headers(origin=True),
            json_body=body,
        )

        payload: Optional[str] = None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            payload = data[0].get("payload")
        elif isinstance(data, dict):
            payload = data.get("payload")

        if not payload:
            return None
        return str(payload)
