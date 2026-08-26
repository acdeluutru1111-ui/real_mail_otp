"""Pure policy functions: authorization, quota, pricing and HTML sanitization.

This module holds *pure* domain policy helpers. INVARIANTS:
- No database access, no network/file I/O, no framework objects.
- No logging of sensitive data (never log an email body or payload).
- Errors are raised as concrete :class:`AppError` subclasses from
  :mod:`app.core.errors` so the exception handler renders the safe envelope.

The HTML sanitizer prefers the ``bleach`` library when it is already
importable; otherwise it falls back to a conservative stdlib
``html.parser``-based allowlist sanitizer.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

from app.core.config import get_settings
from app.core.errors import (
    AuthForbiddenError,
    PaymentErrorError,
    ValidationErrorError,
)
from app.domain import models

# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def ensure_owner(entity_user_id: str, requesting_user_id: str) -> None:
    """Ensure the requesting user owns the entity (inbox / payment).

    Raises:
        AuthForbiddenError: if the two user ids differ.
    """
    if entity_user_id != requesting_user_id:
        raise AuthForbiddenError()


# ---------------------------------------------------------------------------
# Quota / fair-use
# ---------------------------------------------------------------------------


def ensure_active_inbox_quota(active_count: int) -> None:
    """Ensure the user is under the active-inbox fair-use cap.

    Raises:
        ValidationErrorError: if ``active_count`` is at or above
            ``settings.max_active_inboxes_per_user``.
    """
    limit = get_settings().max_active_inboxes_per_user
    if active_count >= limit:
        raise ValidationErrorError(
            f"Active inbox limit reached (max {limit})."
        )


# ---------------------------------------------------------------------------
# Pricing / billing
# ---------------------------------------------------------------------------


def resolve_topup_amount(
    package_code: str | None,
    amount_vnd: int | None,
) -> tuple[int, int]:
    """Resolve a top-up request to ``(amount_vnd, credits)``.

    Delegates to :func:`models.resolve_topup` and converts its
    ``ValueError`` (unknown package / missing amount) into a
    :class:`PaymentErrorError`.

    Raises:
        PaymentErrorError: if the top-up request is invalid.
    """
    try:
        return models.resolve_topup(package_code, amount_vnd)
    except ValueError:
        raise PaymentErrorError()


def read_charge_amount() -> int:
    """Return the fixed per-read charge (VND), sourced from runtime config."""
    return models.read_price()


def can_afford(balance_vnd: int, amount: int) -> bool:
    """Return whether ``balance_vnd`` covers ``amount``."""
    return balance_vnd >= amount


# ---------------------------------------------------------------------------
# HTML / text sanitization
# ---------------------------------------------------------------------------

# Allowlisted tags for rendered email HTML.
_ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "br",
        "a",
        "b",
        "i",
        "strong",
        "em",
        "ul",
        "ol",
        "li",
        "blockquote",
        "span",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        "pre",
        "code",
        "img",
    }
)

# Void/self-closing tags that must not emit a closing tag.
_VOID_TAGS: frozenset[str] = frozenset({"br", "img"})

# Tags whose entire contents must be dropped (never rendered).
_DROP_CONTENT_TAGS: frozenset[str] = frozenset(
    {"script", "style", "iframe", "object", "embed", "form", "link", "meta"}
)

# Per-tag allowlist of safe attributes. Everything else is stripped.
# ``img`` intentionally does NOT allow ``src`` (remote tracking disabled).
_ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"alt", "title", "width", "height"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan"}),
}

# Only these URL schemes are permitted on <a href>.
_SAFE_URL_RE = re.compile(r"^(?:https?:|mailto:)", re.IGNORECASE)
# Control chars except tab (\t), newline (\n), carriage return (\r).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _is_safe_href(value: str) -> bool:
    """Return whether a href value uses an allowed, non-javascript scheme."""
    stripped = value.strip().replace("\t", "").replace("\n", "").replace("\r", "")
    if stripped.startswith("#") or stripped.startswith("/"):
        return True
    return bool(_SAFE_URL_RE.match(stripped))


class _SanitizingParser(HTMLParser):
    """Conservative allowlist sanitizer built on :class:`HTMLParser`.

    Unknown tags are dropped but their (sanitized) text content is kept.
    Dangerous containers (script/style/iframe/...) have their content dropped
    entirely. Event-handler (``on*``) attributes, ``javascript:`` URLs and
    remote ``img`` sources are removed.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        # Depth counter while inside a drop-content container.
        self._suppress_depth = 0

    # -- helpers ----------------------------------------------------------
    def _clean_attrs(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> list[tuple[str, str]]:
        allowed = _ALLOWED_ATTRS.get(tag, frozenset())
        cleaned: list[tuple[str, str]] = []
        for name, value in attrs:
            lname = name.lower()
            if lname.startswith("on"):  # event handlers
                continue
            if lname not in allowed:
                continue
            val = value or ""
            if lname == "href" and not _is_safe_href(val):
                continue
            cleaned.append((lname, val))
        return cleaned

    def _emit_start(self, tag: str, attrs: list[tuple[str, str]], void: bool) -> None:
        parts = [tag]
        for name, value in attrs:
            parts.append(f'{name}="{escape(value, quote=True)}"')
        joined = " ".join(parts)
        self._out.append(f"<{joined} />" if void else f"<{joined}>")

    # -- HTMLParser hooks -------------------------------------------------
    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self._suppress_depth += 1
            return
        if self._suppress_depth:
            return
        if tag not in _ALLOWED_TAGS:
            return  # drop tag, keep inner text
        self._emit_start(tag, self._clean_attrs(tag, attrs), tag in _VOID_TAGS)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS or self._suppress_depth:
            return
        if tag not in _ALLOWED_TAGS:
            return
        self._emit_start(tag, self._clean_attrs(tag, attrs), void=True)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            if self._suppress_depth:
                self._suppress_depth -= 1
            return
        if self._suppress_depth:
            return
        if tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        self._out.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:  # pragma: no cover
        if not self._suppress_depth:
            self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:  # pragma: no cover
        if not self._suppress_depth:
            self._out.append(f"&#{name};")

    def get_html(self) -> str:
        return "".join(self._out)


def _sanitize_with_stdlib(html: str) -> str:
    parser = _SanitizingParser()
    parser.feed(html)
    parser.close()
    return parser.get_html()


try:  # Prefer bleach only if it is already importable in the environment.
    import bleach as _bleach  # type: ignore

    _HAVE_BLEACH = True
except Exception:  # pragma: no cover - bleach not installed
    _bleach = None  # type: ignore
    _HAVE_BLEACH = False


def _sanitize_with_bleach(html: str) -> str:
    # img src is intentionally omitted so remote tracking pixels are disabled.
    attrs = {
        "a": ["href", "title"],
        "img": ["alt", "title", "width", "height"],
        "td": ["colspan", "rowspan"],
        "th": ["colspan", "rowspan"],
    }
    return _bleach.clean(  # type: ignore[union-attr]
        html,
        tags=list(_ALLOWED_TAGS),
        attributes=attrs,
        protocols=["http", "https", "mailto"],
        strip=True,
        strip_comments=True,
    )


def sanitize_email_html(html: str) -> str:
    """Return a sanitized, render-safe version of untrusted email HTML.

    Neutralizes script/iframe/object/embed/form/link/meta elements, event
    handler (``on*``) attributes, ``javascript:`` URLs, and remote image
    tracking (``img src`` is stripped). Uses ``bleach`` when available,
    otherwise a stdlib ``HTMLParser`` allowlist sanitizer.

    Never log the input or output of this function.
    """
    if not html:
        return ""
    if _HAVE_BLEACH:
        return _sanitize_with_bleach(html)
    return _sanitize_with_stdlib(html)


def sanitize_text(s: str) -> str:
    """Strip control characters (except tab/newline/carriage return)."""
    if not s:
        return ""
    return _CONTROL_CHARS_RE.sub("", s)
