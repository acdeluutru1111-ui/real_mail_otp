"""Contract tests for the async SmailPro / Sonjj adapters (Bước 3).

Uses :class:`httpx.MockTransport` to feed static fixtures into the shared client
so no real network is touched. Verifies:

* normalization correctness across create/list/detail field variants;
* correct :class:`AppError` codes for malformed / empty / 4xx / 5xx;
* a redaction guard: captured logs never contain any fixture secret
  (cookie / key / payload) substring.

Run with: ``pytest backend/tests/contract -q``
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

import httpx
import pytest

# Note: httpx.MockTransport is used directly (respx is available too but not
# required here) so tests need no live network.

from app.core.errors import (
    AppError,
    UpstreamAuthError,
    UpstreamBadResponseError,
    UpstreamRateLimitError,
)
import app.integrations.smailpro as smailpro_mod
from app.integrations import http_client, normalizers
from app.integrations.smailpro import SmailProAdapter
from app.integrations.sonjj import SonjjAdapter

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeSettings:
    """Minimal settings stub carrying a fake cookie secret (redaction test)."""

    def __init__(self) -> None:
        self.upstream_timeout = 10.0
        self.upstream_connect_timeout = 5.0
        self.upstream_read_timeout = 10.0
        self.upstream_max_retries = 2
        self.upstream_max_concurrency = 20

    def upstream_cookies(self):
        return {"sonjj_session": "SONJJ_SESSION_COOKIE_SECRET"}


# --- helpers ----------------------------------------------------------------


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load(name: str):
    return json.loads(_read(name))


def _install_transport(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Replace the shared client with one bound to a MockTransport."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://mock")
    # Bypass the lazy factory: force the module-level client.
    http_client._client = client  # type: ignore[attr-defined]

    async def _get_client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(http_client, "get_client", _get_client)
    # Unbounded semaphore so tests never block on the concurrency cap.
    http_client.reset_semaphore(1000)


def _json_response(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=_read(name), headers={"content-type": "application/json"})


@pytest.fixture(autouse=True)
def _reset_client():
    """Ensure each test starts/ends without a leaked shared client."""
    http_client._client = None  # type: ignore[attr-defined]
    yield
    http_client._client = None  # type: ignore[attr-defined]


# --- pure normalizer tests --------------------------------------------------


def test_normalize_list_variants():
    for name, key in (
        ("sonjj_list_messages_key.json", "messages"),
        ("sonjj_list_data_key.json", "data"),
        ("sonjj_list_bare.json", "bare"),
    ):
        items = normalizers.normalize_list(_load(name))
        assert items, f"{key} should yield items"
        for item in items:
            assert set(item) == {"mid", "subject", "sender", "date", "snippet"}
            assert item["mid"]
            assert item["subject"]
            assert item["sender"]


def test_normalize_detail_body_variants():
    for name in (
        "detail_message.json",
        "detail_body.json",
        "detail_htmlBody.json",
        "detail_content.json",
        "detail_textBody.json",
    ):
        detail = normalizers.normalize_detail(_load(name), mid="X")
        assert detail["body"], f"{name} should resolve a body"
        assert detail["subject"]


def test_extractors():
    assert normalizers.extract_otp_code("Your code is 481920") == "481920"
    assert normalizers.extract_otp_code("PIN 1234", digits=4) == "1234"
    assert (
        normalizers.extract_verification_token(
            "go https://x/verify-email?token=ABC123 now"
        )
        == "ABC123"
    )
    assert normalizers.extract_oob_code("reset?oobCode=OOB_9xY") == "OOB_9xY"
    links = normalizers.extract_links("see https://a.example/x and http://b.example")
    assert links == ["https://a.example/x", "http://b.example"]


# --- SmailPro create --------------------------------------------------------


async def test_create_top_level(monkeypatch):
    _install_transport(monkeypatch, lambda req: _json_response("create_top_level.json"))
    result = await SmailProAdapter().create(domain="outlook.com")
    assert result["address"] == "alice.top@outlook.com"
    assert result["key"] == "CREATE_KEY_TOPLEVEL_SECRET"
    assert result["timestamp"] == 1710000000


async def test_create_nested_data(monkeypatch):
    _install_transport(monkeypatch, lambda req: _json_response("create_nested_data.json"))
    result = await SmailProAdapter().create()
    assert result["address"] == "bob.nested@outlook.com"
    assert result["key"] == "CREATE_KEY_NESTED_SECRET"


async def test_create_exhausts_on_bad_response(monkeypatch):
    _install_transport(monkeypatch, lambda req: httpx.Response(200, json={"nope": True}))
    with pytest.raises(AppError) as ei:
        await SmailProAdapter().create()
    assert ei.value.code == "UPSTREAM_BAD_RESPONSE"


# --- SmailPro inbox payload -------------------------------------------------


async def test_inbox_payload_list(monkeypatch):
    _install_transport(monkeypatch, lambda req: _json_response("inbox_payload_list.json"))
    payload = await SmailProAdapter().get_inbox_payload("a@outlook.com", 0, "k")
    assert payload == "PAYLOAD_LIST_WRAPPED_SECRET_XYZ"


async def test_inbox_payload_dict(monkeypatch):
    _install_transport(monkeypatch, lambda req: _json_response("inbox_payload_dict.json"))
    payload = await SmailProAdapter().get_inbox_payload("a@outlook.com", 123, "k")
    assert payload == "PAYLOAD_DICT_SHAPE_SECRET_ABC"


async def test_inbox_payload_empty(monkeypatch):
    _install_transport(monkeypatch, lambda req: _json_response("inbox_empty.json"))
    payload = await SmailProAdapter().get_inbox_payload("a@outlook.com", 0, "k")
    assert payload is None


# --- Sonjj list -------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    ["sonjj_list_messages_key.json", "sonjj_list_data_key.json", "sonjj_list_bare.json"],
)
async def test_sonjj_list_shapes(monkeypatch, fixture):
    _install_transport(monkeypatch, lambda req: _json_response(fixture))
    items = await SonjjAdapter().list_messages("PAYLOAD_XYZ", "a@outlook.com")
    assert items
    assert all(i["mid"] for i in items)


# --- Sonjj detail -----------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    [
        "detail_message.json",
        "detail_body.json",
        "detail_htmlBody.json",
        "detail_content.json",
        "detail_textBody.json",
    ],
)
async def test_sonjj_detail_body_variants(monkeypatch, fixture):
    _install_transport(monkeypatch, lambda req: _json_response(fixture))
    detail = await SonjjAdapter().get_message_detail("PAYLOAD_XYZ", "MID-1", "a@outlook.com")
    assert detail["body"]
    assert detail["mid"] == "MID-1"


async def test_sonjj_detail_empty_is_bad_response(monkeypatch):
    _install_transport(monkeypatch, lambda req: _json_response("detail_empty.json"))
    with pytest.raises(UpstreamBadResponseError):
        await SonjjAdapter().get_message_detail("PAYLOAD_XYZ", "MID-1", "a@outlook.com")


async def test_sonjj_detail_malformed_is_bad_response(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_read("malformed.txt"), headers={"content-type": "application/json"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(UpstreamBadResponseError):
        await SonjjAdapter().get_message_detail("PAYLOAD_XYZ", "MID-1", "a@outlook.com")


# --- error status mapping ---------------------------------------------------


@pytest.mark.parametrize(
    "status,fixture,expected",
    [
        (401, "error_401.json", UpstreamAuthError),
        (403, "error_401.json", UpstreamAuthError),
        (429, "error_429.json", UpstreamRateLimitError),
        (500, "error_500.json", UpstreamBadResponseError),
    ],
)
async def test_status_code_mapping(monkeypatch, status, fixture, expected):
    _install_transport(monkeypatch, lambda req: _json_response(fixture, status=status))
    with pytest.raises(expected):
        await SonjjAdapter().list_messages("PAYLOAD_XYZ", "a@outlook.com")


async def test_timeout_maps_to_upstream_timeout(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=req)

    _install_transport(monkeypatch, handler)
    with pytest.raises(AppError) as ei:
        await SonjjAdapter().list_messages("PAYLOAD_XYZ", "a@outlook.com")
    assert ei.value.code == "UPSTREAM_TIMEOUT"


async def test_connect_error_maps_to_unavailable(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    _install_transport(monkeypatch, handler)
    with pytest.raises(AppError) as ei:
        await SonjjAdapter().list_messages("PAYLOAD_XYZ", "a@outlook.com")
    assert ei.value.code == "UPSTREAM_UNAVAILABLE"


# --- redaction guard --------------------------------------------------------


async def test_redaction_guard_no_secret_in_logs(monkeypatch, caplog):
    """Drive create + list + detail and assert no fixture secret is logged."""
    from app.core.logging import JsonFormatter, RedactionFilter

    secrets = [
        "CREATE_KEY_TOPLEVEL_SECRET",
        "PAYLOAD_LIST_WRAPPED_SECRET_XYZ",
        "PAYLOAD_DICT_SHAPE_SECRET_ABC",
        "PAYLOAD_XYZ",
        "SONJJ_SESSION_COOKIE_SECRET",
    ]

    # Feed the upstream cookie as a fake secret and make sure it never leaks.
    _fake = _FakeSettings()
    monkeypatch.setattr(smailpro_mod, "get_settings", lambda: _fake)

    formatter = JsonFormatter()
    caplog.set_level(logging.DEBUG)

    def route(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/app/create"):
            return _json_response("create_top_level.json")
        if path.endswith("/app/inbox"):
            return _json_response("inbox_payload_list.json")
        if path.endswith("/inbox"):
            return _json_response("sonjj_list_messages_key.json")
        if path.endswith("/message"):
            return _json_response("detail_message.json")
        return httpx.Response(404, json={})

    _install_transport(monkeypatch, route)

    # Construct after patching so the fake cookie is picked up (never logged).
    adapter = SmailProAdapter()
    email = await adapter.create()
    payload = await adapter.get_inbox_payload(
        email["address"], email["timestamp"], email["key"]
    )
    assert payload
    sonjj = SonjjAdapter()
    messages = await sonjj.list_messages(payload, email["address"])
    assert messages
    await sonjj.get_message_detail(payload, messages[0]["mid"], email["address"])

    # Render every captured record through the real JSON formatter + redaction.
    redactor = RedactionFilter()
    rendered = []
    for record in caplog.records:
        redactor.filter(record)
        rendered.append(formatter.format(record))
    blob = "\n".join(rendered)

    for secret in secrets:
        assert secret not in blob, f"secret leaked into logs: {secret}"


def test_normalize_requested_domain():
    from app.integrations.domains import normalize_requested_domain

    assert normalize_requested_domain("outlook") == "outlook.com"
    assert normalize_requested_domain("outlook.com") == "outlook.com"
    assert normalize_requested_domain("gmail") == "gmail.com"
    assert normalize_requested_domain("gmail.com") == "gmail.com"
    assert normalize_requested_domain("@hotmail") == "hotmail.com"
    assert normalize_requested_domain("") == "outlook.com"
    assert normalize_requested_domain(None) == "outlook.com"


async def test_smailpro_headers_has_user_agent_and_normalizes_domain(monkeypatch):
    from app.integrations.smailpro import _smailpro_headers

    headers = _smailpro_headers()
    assert "user-agent" in headers
    assert "Mozilla" in headers["user-agent"]

    recorded_params = []

    def route(req: httpx.Request) -> httpx.Response:
        recorded_params.append(dict(req.url.params))
        assert "user-agent" in req.headers
        return _json_response("create_top_level.json")

    _install_transport(monkeypatch, route)
    adapter = SmailProAdapter()
    await adapter.create(domain="outlook")
    assert recorded_params[0]["domain"] == "outlook.com"

