"""Focused, fully mocked acceptance tests for automatic cookie recovery."""

from __future__ import annotations

import asyncio
import email
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes import admin
from app.core.config import Settings
from app.core.errors import (
    UpstreamAuthError,
    UpstreamBadResponseError,
    UpstreamRateLimitError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.integrations import cookie_manager as cm_mod
from app.integrations import smailpro as smail_mod
from app.integrations.cookie_manager import CookieData, CookieManager, CookieRefreshError

pytestmark = pytest.mark.asyncio

XSRF = "SENTINEL_XSRF_COOKIE"
SESSION = "SENTINEL_SESSION_COOKIE"
NEW_XSRF = "SENTINEL_NEW_XSRF"
NEW_SESSION = "SENTINEL_NEW_SESSION"


class FakeSettings:
    gmail_email = "fake-refresh@example.test"
    gmail_app_password = "FAKE_APP_PASSWORD"
    cookie_ttl_hours = 30.0
    cookie_persistence = "none"
    cookie_auto_refresh_enabled = True
    cookie_refresh_max_wait_seconds = 5.0
    cookie_refresh_poll_interval_seconds = 0.01
    cookie_refresh_cooldown_seconds = 0.0
    cookie_imap_timeout_seconds = 1.0
    upstream_timeout = 0.1
    upstream_max_retries = 0

    def upstream_cookies(self):
        return {}


def make_manager(monkeypatch, *, ttl=30.0) -> CookieManager:
    settings = FakeSettings()
    settings.cookie_ttl_hours = ttl
    monkeypatch.setattr(cm_mod, "get_settings", lambda: settings)
    return CookieManager()


async def test_lazy_bootstrap_and_expiry_crosses_day(monkeypatch):
    manager = make_manager(monkeypatch, ttl=30.0)
    calls = 0

    async def flow(**kwargs):
        nonlocal calls
        calls += 1
        return XSRF, SESSION

    monkeypatch.setattr(manager, "_execute_cookie_flow", flow)
    cookies = await manager.get_current_cookies()
    assert calls == 1
    assert cookies.is_valid()
    assert cookies.expires_at - cookies.fetched_at == timedelta(hours=30)
    assert cookies.expires_at.date() >= cookies.fetched_at.date() + timedelta(days=1)


async def test_concurrent_get_bootstrap_is_single_flight(monkeypatch):
    manager = make_manager(monkeypatch)
    calls = 0

    async def flow(**kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return XSRF, SESSION

    monkeypatch.setattr(manager, "_execute_cookie_flow", flow)
    results = await asyncio.gather(*(manager.get_current_cookies() for _ in range(8)))
    assert calls == 1
    assert {item.sonjj_session for item in results} == {SESSION}


async def test_concurrent_refresh_same_generation_is_single_flight(monkeypatch):
    manager = make_manager(monkeypatch)
    manager._cookies = CookieData(XSRF, SESSION)  # noqa: SLF001 - focused state test
    observed = manager.generation
    calls = 0

    async def flow(**kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return NEW_XSRF, NEW_SESSION

    monkeypatch.setattr(manager, "_execute_cookie_flow", flow)
    results = await asyncio.gather(
        *(manager.refresh_cookies(force=True, stale_generation=observed) for _ in range(8))
    )
    assert calls == 1
    assert manager.generation == 1
    assert {item.sonjj_session for item in results} == {NEW_SESSION}


@pytest.mark.parametrize("partial", [(XSRF, None), (None, SESSION), (None, None)])
async def test_partial_cookie_result_is_not_published(monkeypatch, partial):
    manager = make_manager(monkeypatch)
    good = CookieData(XSRF, SESSION)
    manager._cookies = good  # noqa: SLF001

    async def flow(**kwargs):
        return partial

    monkeypatch.setattr(manager, "_execute_cookie_flow", flow)
    with pytest.raises(CookieRefreshError):
        await manager.refresh_cookies(force=True, stale_generation=0)
    assert manager.get_cached_cookies() is good
    assert manager.generation == 0


async def test_refresh_exception_keeps_good_cache_and_redacts_logs(monkeypatch, caplog):
    manager = make_manager(monkeypatch)
    good = CookieData(XSRF, SESSION)
    manager._cookies = good  # noqa: SLF001
    secrets = [
        XSRF,
        SESSION,
        "SENTINEL.JWT.VALUE",
        "https://sonjj.com/members/?token=SENTINEL_MAGIC_URL",
        "SENTINEL_RAW_EXCEPTION",
    ]

    async def flow(**kwargs):
        raise RuntimeError(" ".join(secrets))

    monkeypatch.setattr(manager, "_execute_cookie_flow", flow)
    with pytest.raises(CookieRefreshError):
        await manager.refresh_cookies(force=True, stale_generation=0)
    assert manager.get_cached_cookies() is good
    log_text = caplog.text
    assert all(secret not in log_text for secret in secrets)


async def test_safe_telemetry_allowlist_and_reason_code(monkeypatch):
    events = []
    settings = FakeSettings()
    monkeypatch.setattr(cm_mod, "get_settings", lambda: settings)
    manager = CookieManager(telemetry_sink=events.append)
    started = cm_mod.time.monotonic()
    manager._telemetry(  # noqa: SLF001 - verify the telemetry boundary
        cm_mod.CookieRefreshStage.IMAP_POLL,
        "failed",
        cm_mod.CookieRefreshReason.MESSAGE_NOT_FOUND.value,
        started,
    )
    assert len(events) == 1
    assert set(events[0]) == {"stage", "status", "reason_code", "elapsed_ms"}
    assert events[0]["stage"] == "imap_poll"
    assert events[0]["reason_code"] == "message_not_found"
    assert isinstance(events[0]["elapsed_ms"], int)
    rendered = json.dumps(events)
    assert settings.gmail_email not in rendered
    assert settings.gmail_app_password not in rendered


async def test_gmail_search_uses_uid_readonly_and_body_peek(monkeypatch):
    settings = FakeSettings()
    monkeypatch.setattr(cm_mod, "get_settings", lambda: settings)
    events = []
    manager = CookieManager(telemetry_sink=events.append)
    manager._flow_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)  # noqa: SLF001
    message = email.message.EmailMessage()
    message["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    message.set_content("https://sonjj.com/members/?token=SAFE_TEST_VALUE")

    class FakeIMAP:
        def __init__(self, *args, **kwargs):
            self.calls = []

        def login(self, *_):
            return "OK", []

        def select(self, mailbox, readonly=False):
            assert mailbox == "INBOX" and readonly is True
            return "OK", []

        def uid(self, command, *args):
            self.calls.append((command, args))
            if command == "search":
                assert args[-1] == 'FROM "sonjj.com"'
                return "OK", [b"10 11"]
            assert command == "fetch" and args[-1] == "(BODY.PEEK[])"
            return "OK", [(b"11", message.as_bytes())]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr(cm_mod.imaplib, "IMAP4_SSL", FakeIMAP)
    link = manager._read_signin_link_sync(1.0, 0.01)  # noqa: SLF001
    assert link.startswith("https://sonjj.com/members/")
    assert any(event["stage"] == "imap_poll" and event["status"] == "ok" for event in events)


async def test_internal_exception_has_safe_metadata_only(monkeypatch):
    manager = make_manager(monkeypatch)
    secret = "SENTINEL_RAW_EXCEPTION_WITH_SECRET"

    async def flow(**kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(manager, "_execute_cookie_flow", flow)
    with pytest.raises(CookieRefreshError) as caught:
        await manager.refresh_cookies(force=True)
    assert str(caught.value) == "Cookie refresh failed"
    assert caught.value.stage.value == "smailpro_cookie"
    assert caught.value.reason_code.value == "internal_error"
    assert secret not in str(caught.value)


async def test_default_persistence_never_creates_plaintext_file(monkeypatch, tmp_path):
    cache_file = tmp_path / "cookie-cache.json"
    monkeypatch.setattr(cm_mod, "COOKIE_CACHE_FILE", cache_file)
    manager = make_manager(monkeypatch)

    async def flow(**kwargs):
        return XSRF, SESSION

    monkeypatch.setattr(manager, "_execute_cookie_flow", flow)
    await manager.refresh_cookies()
    assert not cache_file.exists()
    assert not hasattr(manager, "_save_to_file")


class FakeCookieManager:
    def __init__(self):
        self.generation = 0
        self.refresh_calls = 0
        self.current = CookieData(XSRF, SESSION)

    async def get_current_cookies(self):
        return self.current

    async def refresh_cookies(self, **kwargs):
        self.refresh_calls += 1
        self.generation += 1
        self.current = CookieData(NEW_XSRF, NEW_SESSION)
        return self.current


def make_adapter(monkeypatch):
    settings = FakeSettings()
    manager = FakeCookieManager()
    monkeypatch.setattr(smail_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(smail_mod, "get_cookie_manager", lambda: manager)
    return smail_mod.SmailProAdapter(), manager


async def test_first_401_refreshes_and_replays_once(monkeypatch):
    adapter, manager = make_adapter(monkeypatch)
    seen = []

    async def request(method, url, **kwargs):
        seen.append(kwargs["cookies"])
        if len(seen) == 1:
            raise UpstreamAuthError()
        return {"ok": True}

    monkeypatch.setattr(smail_mod.http_client, "request", request)
    assert await adapter._request_with_auth_recovery("GET", "https://example.test") == {"ok": True}
    assert manager.refresh_calls == 1
    assert len(seen) == 2
    assert seen[1]["sonjj_session"] == NEW_SESSION


async def test_repeated_401_stops_after_exactly_one_replay(monkeypatch):
    adapter, manager = make_adapter(monkeypatch)
    calls = 0

    async def request(method, url, **kwargs):
        nonlocal calls
        calls += 1
        raise UpstreamAuthError()

    monkeypatch.setattr(smail_mod.http_client, "request", request)
    with pytest.raises(UpstreamAuthError):
        await adapter._request_with_auth_recovery("GET", "https://example.test")
    assert calls == 2
    assert manager.refresh_calls == 1


@pytest.mark.parametrize(
    "error_type",
    [UpstreamTimeoutError, UpstreamRateLimitError, UpstreamUnavailableError, UpstreamBadResponseError],
)
async def test_non_auth_failures_never_refresh(monkeypatch, error_type):
    adapter, manager = make_adapter(monkeypatch)

    async def request(method, url, **kwargs):
        raise error_type()

    monkeypatch.setattr(smail_mod.http_client, "request", request)
    with pytest.raises(error_type):
        await adapter._request_with_auth_recovery("GET", "https://example.test")
    assert manager.refresh_calls == 0


async def test_admin_status_is_metadata_only_and_does_not_bootstrap(monkeypatch):
    class Manager(FakeCookieManager):
        def get_cached_cookies(self):
            return self.current

        async def get_current_cookies(self):
            raise AssertionError("status must not trigger lazy bootstrap")

    manager = Manager()
    monkeypatch.setattr(admin, "get_cookie_manager", lambda: manager)
    response = await admin.get_cookie_status(admin=object())
    payload = response.model_dump()
    assert set(payload) == {"status", "has_cookies", "generation", "fetched_at", "expires_at", "message"}
    rendered = str(payload)
    assert XSRF not in rendered and SESSION not in rendered
    assert "prefix" not in rendered and "length" not in rendered


async def test_admin_refresh_error_is_fixed_and_safe(monkeypatch):
    secret = "SENTINEL_ADMIN_RAW_EXCEPTION"

    class Manager:
        async def refresh_cookies(self, **kwargs):
            raise RuntimeError(secret)

    monkeypatch.setattr(admin, "get_cookie_manager", lambda: Manager())
    with pytest.raises(HTTPException) as caught:
        await admin.refresh_cookies(admin=object())
    assert caught.value.status_code == 503
    assert caught.value.detail == {
        "code": "COOKIE_REFRESH_FAILED",
        "message": "Cookie refresh failed.",
    }
    assert secret not in str(caught.value.detail)


async def test_admin_request_and_config_bounds():
    with pytest.raises(ValidationError):
        admin.RefreshCookiesBody(max_wait=0)
    with pytest.raises(ValidationError):
        admin.RefreshCookiesBody(max_wait=601)
    with pytest.raises(ValidationError):
        admin.RefreshCookiesBody(poll_interval=61)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, cookie_ttl_hours=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cookie_refresh_max_wait_seconds=601)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            cookie_refresh_max_wait_seconds=5,
            cookie_refresh_poll_interval_seconds=6,
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cookie_persistence="plaintext")


async def test_no_unauthenticated_dev_cookie_routes_remain():
    paths = {route.path for route in admin.router.routes}
    assert not any("/dev/" in path for path in paths)
