"""Async Sonjj adapter (Bước 3).

Re-implements the ``_fetch_sonjj_messages`` (list) and ``get_message_detail``
logic from ``smailpro_logic_full.py`` in an async, non-blocking way. Responses
are normalized via :mod:`app.integrations.normalizers`; upstream failures are
mapped to the error taxonomy by :mod:`app.integrations.http_client`.

Never logs URL / payload / body. Only safe metadata (provider, domain_type,
counts) may be logged.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.errors import UpstreamBadResponseError
from app.core.logging import get_logger
from app.integrations import http_client, normalizers
from app.integrations.domains import get_sonjj_endpoint

logger = get_logger("integrations.sonjj")


def _sonjj_headers() -> Dict[str, str]:
    """Headers for api.sonjj.com requests (ported from ``_sonjj_headers``)."""
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.8",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }


class SonjjAdapter:
    """Async client for the Sonjj temp-mail read API."""

    async def list_messages(
        self, payload: str, address: str
    ) -> List[Dict[str, str]]:
        """List inbox messages for an opaque ``payload`` + email ``address``.

        Returns a list of normalized ``{mid, subject, sender, date, snippet}``.
        The endpoint is resolved from the address domain. The ``payload`` is sent
        as a query param but never logged.
        """
        inbox_url, _message_url, domain_type = get_sonjj_endpoint(address)

        data = await http_client.request(
            "GET",
            inbox_url,
            headers=_sonjj_headers(),
            params={"payload": payload},
        )

        messages = normalizers.normalize_list(data)
        logger.info(
            "sonjj list ok",
            extra={
                "extra_fields": {
                    "provider": "sonjj",
                    "domain_type": domain_type,
                    "status": "ok",
                }
            },
        )
        return messages

    async def get_message_detail(
        self, payload: str, mid: str, address: str
    ) -> Dict[str, str]:
        """Fetch and normalize a single message's detail.

        Returns ``{mid, subject, sender, to, date, body_html, body_text, body}``.
        Raises :class:`UpstreamBadResponseError` when the response is empty or not
        an object. The ``payload``/``mid`` are query params, never logged.
        """
        _inbox_url, message_url, domain_type = get_sonjj_endpoint(address)

        data = await http_client.request(
            "GET",
            message_url,
            headers=_sonjj_headers(),
            params={"payload": payload, "mid": mid},
        )

        if not isinstance(data, dict) or not data:
            raise UpstreamBadResponseError()

        detail = normalizers.normalize_detail(data, mid=mid)

        # A detail with no body at all is treated as an invalid response.
        if not detail["body"] and not detail["subject"] and not detail["sender"]:
            raise UpstreamBadResponseError()

        logger.info(
            "sonjj detail ok",
            extra={
                "extra_fields": {
                    "provider": "sonjj",
                    "domain_type": domain_type,
                    "status": "ok",
                }
            },
        )
        return detail
