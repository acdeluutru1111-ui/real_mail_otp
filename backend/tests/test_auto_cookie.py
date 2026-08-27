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


async def test_extract_cookies_from_signin_cross_domain_forwarding_and_fallback(monkeypatch):
    import respx
    import httpx

    settings = FakeSettings()
    monkeypatch.setattr(cm_mod, "get_settings", lambda: settings)
    manager = CookieManager()

    async with respx.mock(assert_all_called=True) as respx_mock:
        # Step 3: Signin link sets ghost cookie
        respx_mock.get("https://sonjj.com/members/?token=valid_token").respond(
            status_code=200,
            headers={"Set-Cookie": "ghost-members-ssr=ghost_val; Domain=sonjj.com; Path=/"},
        )
        # Step 4: Session endpoint returns JWT
        respx_mock.get("https://sonjj.com/members/api/session").respond(
            status_code=200,
            json={"jwt": "header.payload.signature_xyz"},
        )
        # Step 5: SSO endpoint
        respx_mock.get("https://my.sonjj.com/auth/sonjj?session=header.payload.signature_xyz").respond(
            status_code=200,
            headers={"Set-Cookie": "my_session=sso_val; Domain=my.sonjj.com; Path=/"},
        )
        # Step 6: SmailPro endpoint receives forwarded cookies and sets XSRF-TOKEN and sonjj_session
        def smailpro_handler(request: httpx.Request):
            cookie_header = request.headers.get("cookie", "")
            # Ensure ghost and sso cookies were forwarded across domains in cookie header
            assert "ghost-members-ssr=ghost_val" in cookie_header
            assert "my_session=sso_val" in cookie_header
            return httpx.Response(
                status_code=200,
                headers=[
                    ("Set-Cookie", "XSRF-TOKEN=test%20xsrf%20token; Path=/"),
                    ("Set-Cookie", "sonjj_session=test_session_token_123; Path=/"),
                ],
            )

        respx_mock.get("https://smailpro.com/temporary-email").mock(side_effect=smailpro_handler)

        xsrf, session = await manager._extract_cookies_from_signin("https://sonjj.com/members/?token=valid_token")
        assert xsrf == "test xsrf token"  # unquoted
        assert session == "test_session_token_123"


async def test_extract_cookies_from_signin_set_cookie_header_fallback(monkeypatch):
    import respx
    import httpx

    settings = FakeSettings()
    monkeypatch.setattr(cm_mod, "get_settings", lambda: settings)
    manager = CookieManager()

    async with respx.mock(assert_all_called=True) as respx_mock:
        respx_mock.get("https://sonjj.com/members/?token=fallback_token").respond(status_code=200)
        respx_mock.get("https://sonjj.com/members/api/session").respond(
            status_code=200,
            json={"jwt": "jwt.test.token"},
        )
        respx_mock.get("https://my.sonjj.com/auth/sonjj?session=jwt.test.token").respond(status_code=200)
        # SmailPro responds with raw Set-Cookie header string containing both cookies
        respx_mock.get("https://smailpro.com/temporary-email").respond(
            status_code=200,
            headers={"Set-Cookie": "XSRF-TOKEN=encoded%20token%20abc; Path=/, sonjj_session=fallback_session_999; Path=/"},
        )

        xsrf, session = await manager._extract_cookies_from_signin("https://sonjj.com/members/?token=fallback_token")
        assert xsrf == "encoded token abc"
        assert session == "fallback_session_999"


async def test_cookie_refresh_failure_maps_to_upstream_auth_error(monkeypatch):
    adapter, manager = make_adapter(monkeypatch)

    async def failing_refresh(**kwargs):
        raise cm_mod.CookieRefreshError(
            cm_mod.CookieRefreshStage.MAGIC_LINK_REQUEST,
            cm_mod.CookieRefreshReason.UPSTREAM_REJECTED,
        )

    monkeypatch.setattr(manager, "refresh_cookies", failing_refresh)

    async def request(method, url, **kwargs):
        raise UpstreamAuthError()

    monkeypatch.setattr(smail_mod.http_client, "request", request)

    with pytest.raises(UpstreamAuthError):
        await adapter._request_with_auth_recovery("GET", "https://example.test")


async def test_error_handlers_attach_cors_headers(monkeypatch):
    from starlette.requests import Request
    from app.core.errors import app_error_handler, unhandled_exception_handler, AuthUnauthenticatedError

    settings = FakeSettings()
    settings.cors_origins = ["http://localhost:5173"]
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [(b"origin", b"http://localhost:5173")],
    }
    request = Request(scope)

    # 1. AppError handler
    res1 = await app_error_handler(request, AuthUnauthenticatedError())
    assert res1.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert res1.headers.get("access-control-allow-credentials") == "true"

    # 2. Unhandled Exception handler
    res2 = await unhandled_exception_handler(request, RuntimeError("unhandled"))
    assert res2.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert res2.headers.get("access-control-allow-credentials") == "true"


async def test_get_integrity_token_success_and_failure(monkeypatch):
    import httpx
    import respx

    manager = make_manager(monkeypatch)

    async with respx.mock(assert_all_called=True) as respx_mock:
        respx_mock.get("https://sonjj.com/members/api/integrity-token/").respond(
            status_code=200,
            text="sample_integrity_token_abc123\n",
        )
        async with httpx.AsyncClient() as client:
            token = await manager._get_integrity_token(client)
            assert token == "sample_integrity_token_abc123"

    # Test failure on upstream error
    async with respx.mock(assert_all_called=True) as respx_mock:
        respx_mock.get("https://sonjj.com/members/api/integrity-token/").respond(status_code=500)
        async with httpx.AsyncClient() as client:
            with pytest.raises(CookieRefreshError) as exc_info:
                await manager._get_integrity_token(client)
            assert exc_info.value.stage == cm_mod.CookieRefreshStage.MAGIC_LINK_REQUEST


async def test_request_magic_link_with_integrity_and_otc_payload(monkeypatch):
    import httpx
    import respx

    manager = make_manager(monkeypatch)

    async with respx.mock(assert_all_called=True) as respx_mock:
        def match_payload(request: httpx.Request):
            data = json.loads(request.content)
            assert data["email"] == "fake-refresh@example.test"
            assert data["emailType"] == "signin"
            assert data["requestSrc"] == "portal"
            assert data["integrityToken"] == "my_integrity_token"
            assert data["autoRedirect"] is True
            assert data["includeOTC"] is True
            assert isinstance(data["urlHistory"], list)
            assert len(data["urlHistory"]) > 0
            assert data["urlHistory"][0]["path"] == "/redirect-auth/"
            assert data["urlHistory"][0]["referrerUrl"] == "https://my.sonjj.com/"
            assert isinstance(data["urlHistory"][0]["time"], int)
            return httpx.Response(
                status_code=201,
                json={"otc_ref": "test_otc_ref_999"},
            )

        respx_mock.post("https://sonjj.com/members/api/send-magic-link/").mock(side_effect=match_payload)

        async with httpx.AsyncClient() as client:
            otc_ref = await manager._request_magic_link(client, "my_integrity_token")
            assert otc_ref == "test_otc_ref_999"


async def test_imap_extracts_both_signin_url_and_otc_code(monkeypatch):
    settings = FakeSettings()
    monkeypatch.setattr(cm_mod, "get_settings", lambda: settings)
    manager = CookieManager()
    manager._flow_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    message = email.message.EmailMessage()
    message["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    message["Subject"] = "987654 is your verification code"
    message.set_content("Please sign in using this link: https://sonjj.com/members/?token=TOKEN_URL_123")

    class FakeIMAP:
        def __init__(self, *args, **kwargs):
            pass
        def login(self, *_):
            return "OK", []
        def select(self, mailbox, readonly=False):
            return "OK", []
        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1 2"]
            return "OK", [(b"2", message.as_bytes())]
        def logout(self):
            return "BYE", []

    monkeypatch.setattr(cm_mod.imaplib, "IMAP4_SSL", FakeIMAP)
    signin_url, otc_code = manager._read_gmail_auth_sync(1.0, 0.01)
    assert signin_url == "https://sonjj.com/members/?token=TOKEN_URL_123"
    assert otc_code == "987654"


async def test_imap_extracts_otc_code_only_when_url_missing(monkeypatch):
    settings = FakeSettings()
    monkeypatch.setattr(cm_mod, "get_settings", lambda: settings)
    manager = CookieManager()
    manager._flow_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    message = email.message.EmailMessage()
    message["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    message["Subject"] = "Your OTC login code"
    message.set_content("Your code is 112233. It expires in 10 minutes.")

    class FakeIMAP:
        def __init__(self, *args, **kwargs):
            pass
        def login(self, *_):
            return "OK", []
        def select(self, mailbox, readonly=False):
            return "OK", []
        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"5"]
            return "OK", [(b"5", message.as_bytes())]
        def logout(self):
            return "BYE", []

    monkeypatch.setattr(cm_mod.imaplib, "IMAP4_SSL", FakeIMAP)
    signin_url, otc_code = manager._read_gmail_auth_sync(1.0, 0.01)
    assert signin_url is None
    assert otc_code == "112233"


async def test_imap_extracts_url_only_when_otc_missing(monkeypatch):
    settings = FakeSettings()
    monkeypatch.setattr(cm_mod, "get_settings", lambda: settings)
    manager = CookieManager()
    manager._flow_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    message = email.message.EmailMessage()
    message["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    message["Subject"] = "Sign in to Sonjj"
    message.set_content("Click here: https://sonjj.com/members/?token=ONLY_URL_TOKEN")

    class FakeIMAP:
        def __init__(self, *args, **kwargs):
            pass
        def login(self, *_):
            return "OK", []
        def select(self, mailbox, readonly=False):
            return "OK", []
        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"8"]
            return "OK", [(b"8", message.as_bytes())]
        def logout(self):
            return "BYE", []

    monkeypatch.setattr(cm_mod.imaplib, "IMAP4_SSL", FakeIMAP)
    signin_url, otc_code = manager._read_gmail_auth_sync(1.0, 0.01)
    assert signin_url == "https://sonjj.com/members/?token=ONLY_URL_TOKEN"
    assert otc_code is None


async def test_authenticate_branch_a_signin_url_success(monkeypatch):
    import httpx
    import respx

    manager = make_manager(monkeypatch)

    async with respx.mock(assert_all_called=True) as respx_mock:
        respx_mock.get("https://sonjj.com/members/?token=direct_link").respond(status_code=200)

        async with httpx.AsyncClient() as client:
            await manager._authenticate(
                client=client,
                signin_url="https://sonjj.com/members/?token=direct_link",
                otc_code="123456",
                otc_ref="ref_xyz",
            )


async def test_authenticate_branch_b_otc_fallback_success(monkeypatch):
    import httpx
    import respx

    manager = make_manager(monkeypatch)

    async with respx.mock(assert_all_called=True) as respx_mock:
        # Branch A fails with 400
        respx_mock.get("https://sonjj.com/members/?token=expired_token").respond(status_code=400)
        # Branch B fetches fresh integrity token
        respx_mock.get("https://sonjj.com/members/api/integrity-token/").respond(
            status_code=200,
            text="fresh_token_777",
        )
        # Branch B calls verify-otc
        def match_verify_otc(request: httpx.Request):
            data = json.loads(request.content)
            assert data["otc"] == "654321"
            assert data["otcRef"] == "ref_abc"
            assert data["integrityToken"] == "fresh_token_777"
            return httpx.Response(status_code=200, json={"status": "ok"})

        respx_mock.post("https://sonjj.com/members/api/verify-otc/").mock(side_effect=match_verify_otc)

        async with httpx.AsyncClient() as client:
            await manager._authenticate(
                client=client,
                signin_url="https://sonjj.com/members/?token=expired_token",
                otc_code="654321",
                otc_ref="ref_abc",
            )


async def test_authenticate_fails_when_both_branches_fail(monkeypatch):
    import httpx
    import respx

    manager = make_manager(monkeypatch)

    async with respx.mock(assert_all_called=True) as respx_mock:
        respx_mock.get("https://sonjj.com/members/?token=bad_token").respond(status_code=400)
        respx_mock.get("https://sonjj.com/members/api/integrity-token/").respond(
            status_code=200,
            text="fresh_token_888",
        )
        respx_mock.post("https://sonjj.com/members/api/verify-otc/").respond(status_code=400)

        async with httpx.AsyncClient() as client:
            with pytest.raises(CookieRefreshError) as exc_info:
                await manager._authenticate(
                    client=client,
                    signin_url="https://sonjj.com/members/?token=bad_token",
                    otc_code="999999",
                    otc_ref="bad_ref",
                )
            assert exc_info.value.stage == cm_mod.CookieRefreshStage.SIGNIN
            assert exc_info.value.reason_code == cm_mod.CookieRefreshReason.MAGIC_LINK_INVALID


async def test_full_cookie_flow_e2e_direct_signin(monkeypatch):
    import httpx
    import respx

    manager = make_manager(monkeypatch)

    async def mock_imap_poll(max_wait, poll_interval):
        return "https://sonjj.com/members/?token=valid_direct_link", "112233"

    monkeypatch.setattr(manager, "_read_gmail_auth_async", mock_imap_poll)

    async with respx.mock(assert_all_called=True) as respx_mock:
        # 1. Integrity token
        respx_mock.get("https://sonjj.com/members/api/integrity-token/").respond(
            status_code=200,
            text="integrity_token_val_1",
        )
        # 2. Send magic link
        respx_mock.post("https://sonjj.com/members/api/send-magic-link/").respond(
            status_code=201,
            json={"otc_ref": "ref_otc_direct"},
        )
        # 3. Direct signin link
        respx_mock.get("https://sonjj.com/members/?token=valid_direct_link").respond(
            status_code=200,
            headers={"Set-Cookie": "ghost-members-ssr=direct_ghost; Domain=sonjj.com; Path=/"},
        )
        # 4. Session JWT
        respx_mock.get("https://sonjj.com/members/api/session").respond(
            status_code=200,
            json={"jwt": "jwt_direct_session"},
        )
        # 5. SSO
        respx_mock.get("https://my.sonjj.com/auth/sonjj?session=jwt_direct_session").respond(
            status_code=200,
            headers={"Set-Cookie": "my_session=direct_sso; Domain=my.sonjj.com; Path=/"},
        )
        # 6. SmailPro temporary-email
        respx_mock.get("https://smailpro.com/temporary-email").respond(
            status_code=200,
            headers=[
                ("Set-Cookie", "XSRF-TOKEN=direct_xsrf_val; Path=/"),
                ("Set-Cookie", "sonjj_session=direct_session_val; Path=/"),
            ],
        )

        xsrf, session = await manager._execute_cookie_flow()
        assert xsrf == "direct_xsrf_val"
        assert session == "direct_session_val"


async def test_full_cookie_flow_e2e_otc_fallback(monkeypatch):
    import httpx
    import respx

    manager = make_manager(monkeypatch)

    async def mock_imap_poll(max_wait, poll_interval):
        return "https://sonjj.com/members/?token=failed_link", "654321"

    monkeypatch.setattr(manager, "_read_gmail_auth_async", mock_imap_poll)

    async with respx.mock(assert_all_called=True) as respx_mock:
        # 1. Initial integrity token
        respx_mock.get("https://sonjj.com/members/api/integrity-token/").respond(
            status_code=200,
            text="integrity_token_initial",
        )
        # 2. Send magic link
        respx_mock.post("https://sonjj.com/members/api/send-magic-link/").respond(
            status_code=201,
            json={"otc_ref": "ref_fallback_99"},
        )
        # 3. Direct signin fails
        respx_mock.get("https://sonjj.com/members/?token=failed_link").respond(status_code=400)
        # 4. Fresh integrity token for OTC verify
        respx_mock.get("https://sonjj.com/members/api/integrity-token/").respond(
            status_code=200,
            text="integrity_token_fresh_otc",
        )
        # 5. Verify OTC
        def match_verify(request: httpx.Request):
            data = json.loads(request.content)
            assert data["otc"] == "654321"
            assert data["otcRef"] == "ref_fallback_99"
            assert data["integrityToken"] == "integrity_token_fresh_otc"
            return httpx.Response(
                status_code=200,
                headers={"Set-Cookie": "ghost-members-ssr=otc_ghost_val; Domain=sonjj.com; Path=/"},
                json={"status": "ok"},
            )

        respx_mock.post("https://sonjj.com/members/api/verify-otc/").mock(side_effect=match_verify)
        # 6. Session JWT
        respx_mock.get("https://sonjj.com/members/api/session").respond(
            status_code=200,
            json={"jwt": "jwt_otc_session"},
        )
        # 7. SSO
        respx_mock.get("https://my.sonjj.com/auth/sonjj?session=jwt_otc_session").respond(
            status_code=200,
            headers={"Set-Cookie": "my_session=otc_sso_val; Domain=my.sonjj.com; Path=/"},
        )
        # 8. SmailPro temporary-email
        respx_mock.get("https://smailpro.com/temporary-email").respond(
            status_code=200,
            headers=[
                ("Set-Cookie", "XSRF-TOKEN=otc_xsrf_val; Path=/"),
                ("Set-Cookie", "sonjj_session=otc_session_val; Path=/"),
            ],
        )

        xsrf, session = await manager._execute_cookie_flow()
        assert xsrf == "otc_xsrf_val"
        assert session == "otc_session_val"


async def test_full_cookie_flow_integrity_token_rejected(monkeypatch):
    import httpx
    import respx

    manager = make_manager(monkeypatch)

    async with respx.mock(assert_all_called=True) as respx_mock:
        respx_mock.get("https://sonjj.com/members/api/integrity-token/").respond(status_code=403)
        with pytest.raises(CookieRefreshError) as exc_info:
            await manager._execute_cookie_flow()
        assert exc_info.value.stage == cm_mod.CookieRefreshStage.MAGIC_LINK_REQUEST







