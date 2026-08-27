"""In-memory, single-flight SmailPro cookie acquisition."""

from __future__ import annotations

import asyncio
import email
import imaplib
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import aiofiles

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("integrations.cookie_manager")

# Constants
SONJJ_BASE = "https://sonjj.com"
MY_SONJJ_BASE = "https://my.sonjj.com"
SMAILPRO_BASE = "https://smailpro.com"

# Cookie persistence file path
COOKIE_CACHE_FILE = Path(__file__).parent.parent.parent / ".cookie_cache.json"


class CookieRefreshStage(str, Enum):
    """Non-sensitive refresh stages suitable for telemetry."""

    MAGIC_LINK_REQUEST = "magic_link_request"
    IMAP_CONNECT = "imap_connect"
    IMAP_AUTH = "imap_auth"
    IMAP_POLL = "imap_poll"
    SIGNIN = "signin"
    SESSION = "session"
    SSO = "sso"
    SMAILPRO_COOKIE = "smailpro_cookie"


class CookieRefreshReason(str, Enum):
    """Stable, non-sensitive failure categories."""

    INVALID_CONFIGURATION = "invalid_configuration"
    COOLDOWN_ACTIVE = "cooldown_active"
    NETWORK_ERROR = "network_error"
    UPSTREAM_REJECTED = "upstream_rejected"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    IMAP_AUTH_FAILED = "imap_auth_failed"
    MAILBOX_SELECT_FAILED = "mailbox_select_failed"
    POLL_TIMEOUT = "poll_timeout"
    MESSAGE_NOT_FOUND = "message_not_found"
    MAGIC_LINK_INVALID = "magic_link_invalid"
    SESSION_REJECTED = "session_rejected"
    SESSION_INVALID = "session_invalid"
    SSO_REJECTED = "sso_rejected"
    COOKIE_PAIR_MISSING = "cookie_pair_missing"
    INTERNAL_ERROR = "internal_error"


class CookieRefreshError(Exception):
    """Internal refresh error carrying only safe diagnostic metadata."""

    def __init__(
        self,
        stage: CookieRefreshStage,
        reason_code: CookieRefreshReason,
        message: str = "Cookie refresh failed",
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.reason_code = reason_code


@dataclass
class CookieData:
    """Container for cookie data with metadata."""
    xsrf_token: Optional[str] = None
    sonjj_session: Optional[str] = None
    fetched_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    
    def is_valid(self) -> bool:
        """Check if cookies are present and not expired."""
        if not self.xsrf_token or not self.sonjj_session:
            return False
        if self.expires_at and datetime.now(timezone.utc) > self.expires_at:
            return False
        return True
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CookieData":
        """Create from dictionary."""
        return cls(
            xsrf_token=data.get("xsrf_token"),
            sonjj_session=data.get("sonjj_session"),
            fetched_at=datetime.fromisoformat(data["fetched_at"]) if data.get("fetched_at") else None,
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
        )
    
    def as_dict_for_requests(self) -> Dict[str, str]:
        """Return cookies as dict for httpx/requests."""
        cookies = {}
        if self.xsrf_token:
            cookies["XSRF-TOKEN"] = self.xsrf_token
        if self.sonjj_session:
            cookies["sonjj_session"] = self.sonjj_session
        return cookies


class CookieManager:
    """Manage complete cookie pairs in memory with single-flight refresh."""

    def __init__(
        self,
        gmail_email: Optional[str] = None,
        gmail_password: Optional[str] = None,
        cookie_ttl_hours: Optional[float] = None,
        telemetry_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        settings = get_settings()
        self._settings = settings
        self._gmail_email = gmail_email or settings.gmail_email
        self._gmail_password = gmail_password or settings.gmail_app_password
        self._cookie_ttl_hours = (
            cookie_ttl_hours if cookie_ttl_hours is not None else settings.cookie_ttl_hours
        )
        # Temporary migration support only: old cache files may be read when
        # explicitly enabled, but this implementation never writes secrets.
        self._legacy_read = settings.cookie_persistence == "legacy-read-only"

        self._cookies: Optional[CookieData] = None
        self._generation = 0
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_attempt: Optional[datetime] = None
        self._telemetry_sink = telemetry_sink
        self._flow_started_at: Optional[datetime] = None
        
        # Load from env if available (SMAILPRO_COOKIES)
        self._load_from_env(settings)
    
    def _load_from_env(self, settings) -> None:
        """Load cookies from SMAILPRO_COOKIES env var if available."""
        env_cookies = settings.upstream_cookies()
        if env_cookies.get("XSRF-TOKEN") and env_cookies.get("sonjj_session"):
            self._cookies = CookieData(
                xsrf_token=env_cookies.get("XSRF-TOKEN"),
                sonjj_session=env_cookies.get("sonjj_session"),
                fetched_at=datetime.now(timezone.utc),
                expires_at=None,  # Unknown expiry for env cookies
            )
            logger.info("Loaded cookies from SMAILPRO_COOKIES env")
    
    def _telemetry(
        self,
        stage: CookieRefreshStage,
        status: str,
        reason_code: str,
        started: float,
    ) -> None:
        """Emit a strictly allow-listed event without exception or secret data."""
        event = {
            "stage": stage.value,
            "status": status,
            "reason_code": reason_code,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        }
        logger.info("cookie_refresh_stage", extra={"extra_fields": event})
        if self._telemetry_sink is not None:
            self._telemetry_sink(dict(event))

    @property
    def generation(self) -> int:
        """Current in-memory credential generation (never secret)."""
        return self._generation

    def get_cached_cookies(self) -> CookieData:
        """Return the current in-memory snapshot without I/O or refresh."""
        return self._cookies or CookieData()

    async def get_current_cookies(self) -> CookieData:
        """Return valid cookies, lazily bootstrapping when configured."""
        if self._cookies and self._cookies.is_valid():
            return self._cookies

        if self._legacy_read:
            loaded = await self._load_from_file()
            if loaded and loaded.is_valid():
                self._cookies = loaded
                self._generation += 1
                return loaded

        if self._settings.cookie_auto_refresh_enabled:
            return await self.refresh_cookies(force=False)
        return self._cookies or CookieData()
    
    async def refresh_cookies(
        self,
        max_wait: Optional[float] = None,
        poll_interval: Optional[float] = None,
        force: bool = False,
        stale_generation: Optional[int] = None,
    ) -> CookieData:
        """Refresh cookies via one generation-aware single-flight flow.
        
        Args:
            max_wait: Max seconds to wait for magic link email.
            poll_interval: Seconds between email checks.
            force: Force refresh even if current cookies are valid.
        
        Returns:
            CookieData with new cookies.
        
        Raises:
            CookieRefreshError: If refresh fails.
        """
        max_wait = max_wait or self._settings.cookie_refresh_max_wait_seconds
        poll_interval = poll_interval or self._settings.cookie_refresh_poll_interval_seconds
        if max_wait <= 0 or poll_interval <= 0 or poll_interval > max_wait:
            raise CookieRefreshError(
                CookieRefreshStage.MAGIC_LINK_REQUEST,
                CookieRefreshReason.INVALID_CONFIGURATION,
            )
        if not force and self._cookies and self._cookies.is_valid():
            return self._cookies

        async with self._refresh_lock:
            # A caller that observed generation N must reuse generation N+1
            # produced by another caller instead of starting another flow.
            if stale_generation is not None and self._generation != stale_generation:
                if self._cookies and self._cookies.is_valid():
                    return self._cookies
            if not force and self._cookies and self._cookies.is_valid():
                return self._cookies

            if self._last_refresh_attempt and stale_generation is None:
                elapsed = (datetime.now(timezone.utc) - self._last_refresh_attempt).total_seconds()
                if elapsed < self._settings.cookie_refresh_cooldown_seconds:
                    raise CookieRefreshError(
                        CookieRefreshStage.MAGIC_LINK_REQUEST,
                        CookieRefreshReason.COOLDOWN_ACTIVE,
                    )
            self._last_refresh_attempt = datetime.now(timezone.utc)

            if not self._gmail_email or not self._gmail_password:
                raise CookieRefreshError(
                    CookieRefreshStage.IMAP_AUTH,
                    CookieRefreshReason.INVALID_CONFIGURATION,
                )

            try:
                xsrf_token, sonjj_session = await self._execute_cookie_flow(
                    max_wait=max_wait, poll_interval=poll_interval
                )
                if not xsrf_token or not sonjj_session:
                    raise CookieRefreshError(
                        CookieRefreshStage.SMAILPRO_COOKIE,
                        CookieRefreshReason.COOKIE_PAIR_MISSING,
                    )
                now = datetime.now(timezone.utc)
                self._cookies = CookieData(
                    xsrf_token=xsrf_token,
                    sonjj_session=sonjj_session,
                    fetched_at=now,
                    expires_at=now + timedelta(hours=self._cookie_ttl_hours),
                )
                self._generation += 1
                logger.info("cookie refresh completed")
                return self._cookies
            except CookieRefreshError:
                raise
            except Exception as exc:
                raise CookieRefreshError(
                    CookieRefreshStage.SMAILPRO_COOKIE,
                    CookieRefreshReason.INTERNAL_ERROR,
                ) from exc
    
    async def _execute_cookie_flow(
        self,
        max_wait: float = 60,
        poll_interval: float = 5,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Execute the staged cookie flow, preserving safe failure metadata."""
        self._flow_started_at = datetime.now(timezone.utc)
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Origin": SONJJ_BASE,
                "Referer": f"{SONJJ_BASE}/redirect-auth/",
            },
        ) as client:
            # Stage 1: Get Ghost integrity token
            integrity_token = await self._get_integrity_token(client)

            # Stage 2: Send magic link request with integrity token and includeOTC=True
            otc_ref = await self._request_magic_link(client, integrity_token)

            # Stage 3: Poll Gmail for sign-in link and 6-digit OTC verification code
            signin_url, otc_code = await self._read_gmail_auth_async(max_wait, poll_interval)

            # Stage 4: Authenticate with Ghost CMS (Direct link primary, OTC fallback)
            await self._authenticate(
                client=client,
                signin_url=signin_url,
                otc_code=otc_code,
                otc_ref=otc_ref,
            )

            # Stage 5 & 6: Session JWT, SSO, and SmailPro cookie pair extraction
            return await self._extract_cookies_from_session(client)

    async def _get_integrity_token(self, client: httpx.AsyncClient) -> str:
        """Fetch Ghost integrity token prior to magic link / verify-otc request."""
        started = time.monotonic()
        try:
            resp = await client.get(
                f"{SONJJ_BASE}/members/api/integrity-token/",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                    "Origin": SONJJ_BASE,
                    "Referer": f"{SONJJ_BASE}/redirect-auth/",
                },
            )
            if resp.status_code != 200:
                if resp.status_code == 429:
                    reason = CookieRefreshReason.UPSTREAM_RATE_LIMITED
                elif resp.status_code >= 500:
                    reason = CookieRefreshReason.UPSTREAM_UNAVAILABLE
                else:
                    reason = CookieRefreshReason.UPSTREAM_REJECTED
                self._telemetry(
                    CookieRefreshStage.MAGIC_LINK_REQUEST,
                    "failed",
                    reason.value,
                    started,
                )
                raise CookieRefreshError(
                    CookieRefreshStage.MAGIC_LINK_REQUEST,
                    reason,
                )
            token = resp.text.strip()
            if not token:
                raise CookieRefreshError(
                    CookieRefreshStage.MAGIC_LINK_REQUEST,
                    CookieRefreshReason.UPSTREAM_REJECTED,
                )
            return token
        except CookieRefreshError:
            raise
        except Exception as exc:
            self._telemetry(
                CookieRefreshStage.MAGIC_LINK_REQUEST,
                "failed",
                CookieRefreshReason.NETWORK_ERROR.value,
                started,
            )
            raise CookieRefreshError(
                CookieRefreshStage.MAGIC_LINK_REQUEST,
                CookieRefreshReason.NETWORK_ERROR,
            ) from exc

    async def _request_magic_link(
        self, client: httpx.AsyncClient, integrity_token: str
    ) -> Optional[str]:
        """Request a magic link with integrity token and return otc_ref if available."""
        started = time.monotonic()
        try:
            now_ms = int(time.time() * 1000)
            payload = {
                "email": self._gmail_email,
                "emailType": "signin",
                "requestSrc": "portal",
                "integrityToken": integrity_token,
                "autoRedirect": True,
                "includeOTC": True,
                "urlHistory": [
                    {
                        "path": "/redirect-auth/",
                        "time": now_ms,
                        "referrerUrl": f"{MY_SONJJ_BASE}/",
                    }
                ],
            }
            resp = await client.post(
                f"{SONJJ_BASE}/members/api/send-magic-link/",
                json=payload,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                    "Origin": SONJJ_BASE,
                    "Referer": f"{SONJJ_BASE}/redirect-auth/",
                },
            )
            if resp.status_code not in (200, 201):
                if resp.status_code == 429:
                    reason = CookieRefreshReason.UPSTREAM_RATE_LIMITED
                elif resp.status_code >= 500:
                    reason = CookieRefreshReason.UPSTREAM_UNAVAILABLE
                else:
                    reason = CookieRefreshReason.UPSTREAM_REJECTED
                self._telemetry(
                    CookieRefreshStage.MAGIC_LINK_REQUEST,
                    "failed",
                    reason.value,
                    started,
                )
                raise CookieRefreshError(
                    CookieRefreshStage.MAGIC_LINK_REQUEST,
                    reason,
                )
            self._telemetry(CookieRefreshStage.MAGIC_LINK_REQUEST, "ok", "ok", started)
            otc_ref = None
            try:
                data = resp.json()
                if isinstance(data, dict):
                    otc_ref = data.get("otc_ref") or data.get("otcRef")
                    if not otc_ref and "data" in data and isinstance(data["data"], dict):
                        otc_ref = data["data"].get("otc_ref") or data["data"].get("otcRef")
            except Exception:
                pass
            return otc_ref
        except CookieRefreshError:
            raise
        except Exception as exc:
            self._telemetry(
                CookieRefreshStage.MAGIC_LINK_REQUEST,
                "failed",
                CookieRefreshReason.NETWORK_ERROR.value,
                started,
            )
            raise CookieRefreshError(
                CookieRefreshStage.MAGIC_LINK_REQUEST,
                CookieRefreshReason.NETWORK_ERROR,
            ) from exc
    
    async def _read_gmail_auth_async(
        self, max_wait: float, poll_interval: float
    ) -> Tuple[Optional[str], Optional[str]]:
        """Run blocking Gmail IMAP polling in a worker with a finite timeout."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._read_gmail_auth_sync, max_wait, poll_interval),
                timeout=max_wait + self._settings.cookie_imap_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise CookieRefreshError(
                CookieRefreshStage.IMAP_POLL,
                CookieRefreshReason.POLL_TIMEOUT,
            ) from exc

    async def _read_signin_link_async(
        self, max_wait: float, poll_interval: float
    ) -> str:
        """Legacy helper returning signin_url."""
        signin_url, _ = await self._read_gmail_auth_async(max_wait, poll_interval)
        if not signin_url:
            raise CookieRefreshError(
                CookieRefreshStage.IMAP_POLL,
                CookieRefreshReason.MESSAGE_NOT_FOUND,
            )
        return signin_url

    def _read_gmail_auth_sync(
        self, max_wait: float, poll_interval: float
    ) -> Tuple[Optional[str], Optional[str]]:
        """Poll Gmail INBOX for signin URL and 6-digit OTC verification code."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            mail = None
            started = time.monotonic()
            try:
                try:
                    mail = imaplib.IMAP4_SSL(
                        "imap.gmail.com",
                        timeout=self._settings.cookie_imap_timeout_seconds,
                    )
                    self._telemetry(CookieRefreshStage.IMAP_CONNECT, "ok", "ok", started)
                except Exception as exc:
                    self._telemetry(
                        CookieRefreshStage.IMAP_CONNECT,
                        "failed",
                        CookieRefreshReason.NETWORK_ERROR.value,
                        started,
                    )
                    raise CookieRefreshError(
                        CookieRefreshStage.IMAP_CONNECT,
                        CookieRefreshReason.NETWORK_ERROR,
                    ) from exc

                auth_started = time.monotonic()
                try:
                    mail.login(self._gmail_email, self._gmail_password)
                    self._telemetry(CookieRefreshStage.IMAP_AUTH, "ok", "ok", auth_started)
                except imaplib.IMAP4.error as exc:
                    self._telemetry(
                        CookieRefreshStage.IMAP_AUTH,
                        "failed",
                        CookieRefreshReason.IMAP_AUTH_FAILED.value,
                        auth_started,
                    )
                    raise CookieRefreshError(
                        CookieRefreshStage.IMAP_AUTH,
                        CookieRefreshReason.IMAP_AUTH_FAILED,
                    ) from exc

                select_status, _ = mail.select("INBOX", readonly=True)
                if select_status != "OK":
                    raise CookieRefreshError(
                        CookieRefreshStage.IMAP_POLL,
                        CookieRefreshReason.MAILBOX_SELECT_FAILED,
                    )

                poll_started = time.monotonic()
                # Search all matching mail, not only UNSEEN: Gmail links can be
                # marked read by another client before this poll observes them.
                status, messages = mail.uid("search", None, 'FROM "sonjj.com"')
                if status != "OK":
                    raise CookieRefreshError(
                        CookieRefreshStage.IMAP_POLL,
                        CookieRefreshReason.INTERNAL_ERROR,
                    )
                ids = messages[0].split() if messages and messages[0] else []
                # Inspect a small newest-first window and require mail generated
                # for this refresh, avoiding stale magic links.
                for message_id in reversed(ids[-10:]):
                    status, msg_data = mail.uid("fetch", message_id, "(BODY.PEEK[])")
                    if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])
                    try:
                        sent_at = parsedate_to_datetime(msg.get("Date", ""))
                        if sent_at.tzinfo is None:
                            sent_at = sent_at.replace(tzinfo=timezone.utc)
                        if self._flow_started_at and sent_at < self._flow_started_at - timedelta(minutes=2):
                            continue
                    except (TypeError, ValueError, OverflowError):
                        continue

                    subject = msg.get("Subject", "")
                    body = self._get_email_body(msg)

                    signin_url = None
                    otc_code = None

                    match_url = re.search(
                        r'(https://sonjj\.com/members/\?token=[^\s\)"\'>\]]+)',
                        body,
                    )
                    if match_url:
                        signin_url = match_url.group(1).rstrip(")")

                    match_otc = re.search(r'\b(\d{6})\b', subject)
                    if match_otc:
                        otc_code = match_otc.group(1)
                    else:
                        match_otc_body = re.search(r'\b(\d{6})\b', body)
                        if match_otc_body:
                            otc_code = match_otc_body.group(1)

                    if signin_url or otc_code:
                        self._telemetry(CookieRefreshStage.IMAP_POLL, "ok", "ok", poll_started)
                        return signin_url, otc_code
            finally:
                if mail is not None:
                    try:
                        mail.logout()
                    except Exception:
                        pass
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_interval, remaining))

        self._telemetry(
            CookieRefreshStage.IMAP_POLL,
            "failed",
            CookieRefreshReason.MESSAGE_NOT_FOUND.value,
            time.monotonic() - max_wait,
        )
        raise CookieRefreshError(
            CookieRefreshStage.IMAP_POLL,
            CookieRefreshReason.MESSAGE_NOT_FOUND,
        )

    def _read_signin_link_sync(self, max_wait: float, poll_interval: float) -> str:
        """Legacy helper returning signin_url."""
        signin_url, _ = self._read_gmail_auth_sync(max_wait, poll_interval)
        if not signin_url:
            raise CookieRefreshError(
                CookieRefreshStage.IMAP_POLL,
                CookieRefreshReason.MESSAGE_NOT_FOUND,
            )
        return signin_url
    
    def _get_email_body(self, msg) -> str:
        """Extract email body (prefer plain text, fallback to HTML)."""
        body_plain = ""
        body_html = ""
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_plain = payload.decode(errors="replace")
                elif content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_html = payload.decode(errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body_plain = payload.decode(errors="replace")
        
        return body_plain if body_plain else body_html

    async def _authenticate(
        self,
        client: httpx.AsyncClient,
        signin_url: Optional[str] = None,
        otc_code: Optional[str] = None,
        otc_ref: Optional[str] = None,
    ) -> None:
        """Authenticate with Ghost CMS via direct sign-in link (Branch A) or OTC fallback (Branch B)."""
        started = time.monotonic()

        # Branch A: Direct Sign-in link
        if signin_url:
            try:
                resp = await client.get(signin_url)
                if resp.status_code < 400:
                    self._telemetry(CookieRefreshStage.SIGNIN, "ok", "ok", started)
                    return
            except Exception:
                pass

        # Branch B: OTC Code Fallback
        if otc_code and otc_ref:
            try:
                fresh_integrity_token = await self._get_integrity_token(client)
                otc_payload = {
                    "otc": otc_code,
                    "otcRef": otc_ref,
                    "integrityToken": fresh_integrity_token,
                }
                resp = await client.post(
                    f"{SONJJ_BASE}/members/api/verify-otc/",
                    json=otc_payload,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"
                        ),
                        "Origin": SONJJ_BASE,
                        "Referer": f"{SONJJ_BASE}/redirect-auth/",
                    },
                )
                if resp.status_code in (200, 201):
                    self._telemetry(CookieRefreshStage.SIGNIN, "ok", "ok", started)
                    return
                self._telemetry(
                    CookieRefreshStage.SIGNIN,
                    "failed",
                    CookieRefreshReason.MAGIC_LINK_INVALID.value,
                    started,
                )
                raise CookieRefreshError(
                    CookieRefreshStage.SIGNIN,
                    CookieRefreshReason.MAGIC_LINK_INVALID,
                )
            except CookieRefreshError:
                raise
            except Exception as exc:
                self._telemetry(
                    CookieRefreshStage.SIGNIN,
                    "failed",
                    CookieRefreshReason.NETWORK_ERROR.value,
                    started,
                )
                raise CookieRefreshError(
                    CookieRefreshStage.SIGNIN,
                    CookieRefreshReason.NETWORK_ERROR,
                ) from exc

        # If neither branch succeeded or was available
        self._telemetry(
            CookieRefreshStage.SIGNIN,
            "failed",
            CookieRefreshReason.MAGIC_LINK_INVALID.value,
            started,
        )
        raise CookieRefreshError(
            CookieRefreshStage.SIGNIN,
            CookieRefreshReason.MAGIC_LINK_INVALID,
        )
    
    async def _extract_cookies_from_signin(self, signin_url: str) -> Tuple[str, str]:
        """Legacy helper running sign-in and session cookie extraction."""
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Origin": SONJJ_BASE,
                "Referer": f"{SONJJ_BASE}/redirect-auth/",
            },
        ) as client:
            await self._authenticate(client, signin_url=signin_url)
            return await self._extract_cookies_from_session(client)

    async def _extract_cookies_from_session(self, client: httpx.AsyncClient) -> Tuple[str, str]:
        """Run session JWT, SSO, and SmailPro stages on the authenticated client."""
        started = time.monotonic()
        try:
            session_resp = await client.get(f"{SONJJ_BASE}/members/api/session")
            if session_resp.status_code != 200:
                raise CookieRefreshError(
                    CookieRefreshStage.SESSION,
                    CookieRefreshReason.SESSION_REJECTED,
                )
            jwt_token = None
            try:
                data = session_resp.json()
                if isinstance(data, dict):
                    jwt_token = data.get("jwt")
            except Exception:
                text = session_resp.text.strip()
                if text.count(".") >= 2 and len(text) > 50:
                    jwt_token = text
            if not jwt_token:
                raise CookieRefreshError(
                    CookieRefreshStage.SESSION,
                    CookieRefreshReason.SESSION_INVALID,
                )
            self._telemetry(CookieRefreshStage.SESSION, "ok", "ok", started)
        except CookieRefreshError as exc:
            self._telemetry(
                CookieRefreshStage.SESSION,
                "failed",
                exc.reason_code.value,
                started,
            )
            raise
        except Exception as exc:
            self._telemetry(
                CookieRefreshStage.SESSION,
                "failed",
                CookieRefreshReason.NETWORK_ERROR.value,
                started,
            )
            raise CookieRefreshError(
                CookieRefreshStage.SESSION,
                CookieRefreshReason.NETWORK_ERROR,
            ) from exc

        started = time.monotonic()
        try:
            sso_resp = await client.get(
                f"{MY_SONJJ_BASE}/auth/sonjj",
                params={"session": jwt_token},
            )
            if sso_resp.status_code >= 400:
                raise CookieRefreshError(
                    CookieRefreshStage.SSO,
                    CookieRefreshReason.SSO_REJECTED,
                )
            self._telemetry(CookieRefreshStage.SSO, "ok", "ok", started)
        except CookieRefreshError:
            self._telemetry(
                CookieRefreshStage.SSO,
                "failed",
                CookieRefreshReason.SSO_REJECTED.value,
                started,
            )
            raise
        except Exception as exc:
            self._telemetry(
                CookieRefreshStage.SSO,
                "failed",
                CookieRefreshReason.NETWORK_ERROR.value,
                started,
            )
            raise CookieRefreshError(
                CookieRefreshStage.SSO,
                CookieRefreshReason.NETWORK_ERROR,
            ) from exc

        started = time.monotonic()
        try:
            # Forward accumulated cookies (from .sonjj.com, my.sonjj.com)
            # to bridge the session across to smailpro.com domain
            cookie_parts = []
            for cookie in client.cookies.jar:
                cookie_parts.append(f"{cookie.name}={cookie.value}")
            cookie_header = "; ".join(cookie_parts)

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
            }
            if cookie_header:
                headers["cookie"] = cookie_header

            smail_resp = await client.get(
                f"{SMAILPRO_BASE}/temporary-email",
                headers=headers,
            )
            if smail_resp.status_code >= 400:
                raise CookieRefreshError(
                    CookieRefreshStage.SMAILPRO_COOKIE,
                    CookieRefreshReason.UPSTREAM_REJECTED,
                )
            xsrf_token = None
            sonjj_session = None

            # Primary extraction: client cookie jar and response cookies
            for cookie in client.cookies.jar:
                if cookie.name == "XSRF-TOKEN" and not xsrf_token:
                    xsrf_token = urllib.parse.unquote(cookie.value)
                elif cookie.name == "sonjj_session" and not sonjj_session:
                    sonjj_session = cookie.value

            for name, value in smail_resp.cookies.items():
                if name == "XSRF-TOKEN" and not xsrf_token:
                    xsrf_token = urllib.parse.unquote(value)
                elif name == "sonjj_session" and not sonjj_session:
                    sonjj_session = value

            # Fallback extraction: parse Set-Cookie headers directly
            for header_val in smail_resp.headers.get_list("set-cookie"):
                for part in header_val.split(","):
                    if "XSRF-TOKEN=" in part and not xsrf_token:
                        m = re.search(r"XSRF-TOKEN=([^;]+)", part)
                        if m:
                            xsrf_token = urllib.parse.unquote(m.group(1).strip())
                    if "sonjj_session=" in part and not sonjj_session:
                        m = re.search(r"sonjj_session=([^;]+)", part)
                        if m:
                            sonjj_session = m.group(1).strip()

            if not xsrf_token or not sonjj_session:
                raise CookieRefreshError(
                    CookieRefreshStage.SMAILPRO_COOKIE,
                    CookieRefreshReason.COOKIE_PAIR_MISSING,
                )
            self._telemetry(CookieRefreshStage.SMAILPRO_COOKIE, "ok", "ok", started)
            return xsrf_token, sonjj_session
        except CookieRefreshError as exc:
            self._telemetry(
                CookieRefreshStage.SMAILPRO_COOKIE,
                "failed",
                exc.reason_code.value,
                started,
            )
            raise
        except Exception as exc:
            self._telemetry(
                CookieRefreshStage.SMAILPRO_COOKIE,
                "failed",
                CookieRefreshReason.NETWORK_ERROR.value,
                started,
            )
            raise CookieRefreshError(
                CookieRefreshStage.SMAILPRO_COOKIE,
                CookieRefreshReason.NETWORK_ERROR,
            ) from exc
    
    async def _load_from_file(self) -> Optional[CookieData]:
        """Read a pre-existing legacy plaintext cache for migration only."""
        if not COOKIE_CACHE_FILE.exists():
            return None
        
        try:
            async with aiofiles.open(COOKIE_CACHE_FILE, "r") as f:
                data = json.loads(await f.read())
            cookies = CookieData.from_dict(data)
            logger.info("Loaded cookies from cache file")
            return cookies
        except Exception:
            logger.warning("legacy cookie cache could not be loaded")
            return None
    
    def clear_cookies(self) -> None:
        """Clear in-memory cookies (does not delete file)."""
        self._cookies = None
        logger.info("In-memory cookies cleared")
    
    async def delete_cached_cookies(self) -> None:
        """Delete both in-memory and file-cached cookies."""
        self._cookies = None
        if COOKIE_CACHE_FILE.exists():
            try:
                COOKIE_CACHE_FILE.unlink()
                logger.info("Cookie cache file deleted")
            except Exception:
                logger.warning("legacy cookie cache could not be deleted")


# Singleton instance
_cookie_manager: Optional[CookieManager] = None


def get_cookie_manager() -> CookieManager:
    """Get the singleton CookieManager instance."""
    global _cookie_manager
    if _cookie_manager is None:
        _cookie_manager = CookieManager()
    return _cookie_manager


def reset_cookie_manager() -> None:
    """Reset the singleton (useful for testing)."""
    global _cookie_manager
    _cookie_manager = None
