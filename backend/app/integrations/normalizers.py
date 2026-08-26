"""Pure normalizers and extractors for upstream payloads (Bước 3).

All functions here are **side-effect free and log-free**: they take raw upstream
dicts/strings and return normalized plain dicts / values. They never touch the
network, never mutate their inputs, and never log (redaction invariant). Field
name variants observed across Sonjj responses are collapsed here:

* subject:  ``textSubject`` | ``subject``
* sender:   ``textFrom``    | ``from``
* body/html: ``message`` | ``body`` | ``htmlBody`` | ``content``
* body/text: ``textBody`` | ``text`` | ``snippet``
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _s(value: Any) -> str:
    """Coerce to a stripped string; ``None`` -> ``""``."""
    if value is None:
        return ""
    return str(value)


def _first(*values: Any) -> str:
    """Return the first non-empty string among ``values``."""
    for value in values:
        text = _s(value)
        if text:
            return text
    return ""


def normalize_list_item(msg: Dict[str, Any]) -> Dict[str, str]:
    """Normalize one raw Sonjj inbox list item.

    Returns keys: ``mid``, ``subject``, ``sender``, ``date``, ``snippet``.
    """
    if not isinstance(msg, dict):
        return {"mid": "", "subject": "", "sender": "", "date": "", "snippet": ""}
    return {
        "mid": _s(msg.get("mid")),
        "subject": _first(msg.get("textSubject"), msg.get("subject")),
        "sender": _first(msg.get("textFrom"), msg.get("from")),
        "date": _first(msg.get("date"), msg.get("textDate")),
        "snippet": _first(msg.get("snippet"), msg.get("textSnippet")),
    }


def normalize_list(data: Any) -> List[Dict[str, str]]:
    """Normalize a full Sonjj inbox-list response into a list of items.

    Accepts the observed shapes: ``{"messages": [...]}``, ``{"data": [...]}`` or
    a bare ``[...]`` list. Non-dict entries are skipped.
    """
    raw: List[Any] = []
    if isinstance(data, dict):
        candidate = data.get("messages")
        if candidate is None:
            candidate = data.get("data")
        if isinstance(candidate, list):
            raw = candidate
    elif isinstance(data, list):
        raw = data
    return [normalize_list_item(item) for item in raw if isinstance(item, dict)]


def normalize_detail(data: Dict[str, Any], mid: str = "") -> Dict[str, str]:
    """Normalize a raw Sonjj message-detail dict.

    Returns keys: ``mid``, ``subject``, ``sender``, ``to``, ``date``,
    ``body_html``, ``body_text``, ``body`` (html preferred, else text).
    """
    if not isinstance(data, dict):
        data = {}
    body_html = _first(
        data.get("message"),
        data.get("body"),
        data.get("htmlBody"),
        data.get("content"),
    )
    body_text = _first(
        data.get("textBody"),
        data.get("text"),
        data.get("snippet"),
    )
    return {
        "mid": _s(mid) or _s(data.get("mid")),
        "subject": _first(data.get("subject"), data.get("textSubject")),
        "sender": _first(data.get("from"), data.get("textFrom")),
        "to": _first(data.get("to"), data.get("textTo")),
        "date": _first(data.get("date"), data.get("textDate")),
        "body_html": body_html,
        "body_text": body_text,
        "body": body_html or body_text,
    }


# --- Extractors (pure) ------------------------------------------------------


def extract_otp_code(text: str, digits: int = 6) -> Optional[str]:
    """Extract an OTP code from email content (4-8 digits, ``digits`` preferred).

    Never logs the code.
    """
    if not text:
        return None

    # Pattern 1: labelled code (mã / code / otp / verification / verify).
    match = re.search(
        r"(?:m\u00e3|code|otp|verification|verify|code is|is)[:\s]*(\d{" + str(digits) + r"})",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)

    # Pattern 2: standalone N-digit number.
    match = re.search(r"\b(\d{" + str(digits) + r"})\b", text)
    if match:
        return match.group(1)

    # Pattern 3: any 4-8 digit run.
    match = re.search(r"\b(\d{4,8})\b", text)
    if match:
        return match.group(1)

    return None


def extract_verification_token(text: str) -> Optional[str]:
    """Extract a ``verify-email?token=...`` token, or ``None``."""
    if not text:
        return None
    match = re.search(r"verify-email\?token=([A-Za-z0-9_\-]+)", text)
    return match.group(1) if match else None


def extract_oob_code(text: str) -> Optional[str]:
    """Extract a Firebase ``oobCode`` (raw or URL-encoded), or ``None``."""
    if not text:
        return None

    match = re.search(r"oobCode=([A-Za-z0-9_-]+)", text)
    if match:
        return match.group(1)

    match = re.search(r"oobCode[=%]3D?([A-Za-z0-9_-]+)", text)
    if match:
        code = match.group(1)
        if code.startswith("3D") and len(code) > 2:
            candidate = code[2:]
            if re.match(r"^[A-Za-z0-9_-]+$", candidate):
                return candidate
        return code

    return None


def extract_links(text: str) -> List[str]:
    """Extract all http(s) URLs from email content."""
    if not text:
        return []
    return re.findall(r"https?://[^\s<>\"')\]]+", text)
