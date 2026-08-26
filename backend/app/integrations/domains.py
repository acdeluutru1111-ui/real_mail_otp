"""Domain -> Sonjj endpoint mapping (ported from ``smailpro_logic_full.py``).

Pure, side-effect free and log-free: given an email address, resolve the Sonjj
inbox/message endpoints and the ``domain_type`` used for billing dedupe and
metrics. Never logs the address.
"""

from __future__ import annotations

from typing import Tuple

SONJJ_API_BASE = "https://api.sonjj.com"

# Mapping domain -> sonjj endpoint prefix (aka ``domain_type``).
# Ported verbatim from ``_DOMAIN_ENDPOINT_MAP`` in smailpro_logic_full.py.
_DOMAIN_ENDPOINT_MAP: dict[str, str] = {
    "outlook.com": "temp_outlook",
    "hotmail.com": "temp_outlook",
    "live.com": "temp_outlook",
    "msn.com": "temp_outlook",
    "gmail.com": "temp_gmail",
    "googlemail.com": "temp_gmail",
    "yahoo.com": "temp_yahoo",
    "ymail.com": "temp_yahoo",
    "mail.ru": "temp_mailru",
    "icloud.com": "temp_icloud",
    "me.com": "temp_icloud",
    "mac.com": "temp_icloud",
    # Custom domains from smailpro
    "spyboys.com": "temp_other",
    "spyboy.net": "temp_other",
    "spyboy.org": "temp_other",
    "nqminh.com": "temp_other",
}

# Default endpoint for any unmapped (custom) domain.
_DEFAULT_ENDPOINT = "temp_other"


def domain_type_for(email_address: str) -> str:
    """Return the Sonjj ``domain_type`` for an email address (no logging)."""
    domain = (
        email_address.split("@")[-1].lower() if "@" in (email_address or "") else ""
    )
    return _DOMAIN_ENDPOINT_MAP.get(domain, _DEFAULT_ENDPOINT)


def get_sonjj_endpoint(email_address: str) -> Tuple[str, str, str]:
    """Resolve Sonjj endpoints for an email address.

    Returns:
        Tuple ``(inbox_url, message_url, domain_type)``.
    """
    endpoint = domain_type_for(email_address)
    inbox_url = f"{SONJJ_API_BASE}/v1/{endpoint}/inbox"
    message_url = f"{SONJJ_API_BASE}/v1/{endpoint}/message"
    return inbox_url, message_url, endpoint
