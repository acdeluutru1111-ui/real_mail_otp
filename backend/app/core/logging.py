"""Structured JSON logging with strict redaction (plan 14.1 / 14.2).

Only the *safe* fields listed in plan 14.1 are emitted. A redaction filter drops
or masks any field or substring resembling a secret (cookie, key, payload,
authorization, body, otp, password, token). Sensitive data must never reach the
log sink.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

from app.core.context import get_request_id

# Fields explicitly allowed in a structured log record (plan 14.1).
SAFE_FIELDS: tuple[str, ...] = (
    "timestamp",
    "level",
    "service",
    "version",
    "request_id",
    "route",
    "user_id",  # hashed upstream, never raw
    "inbox_id",
    "provider",
    "domain_type",
    "status",
    "stage",
    "reason_code",
    "elapsed_ms",
    "latency_ms",
    "cache",
    "retry_count",
    "error_code",
    "message",
)

# Substrings that mark a key/value as sensitive; anything matching is dropped.
SENSITIVE_PATTERNS: tuple[str, ...] = (
    "cookie",
    "key",
    "payload",
    "authorization",
    "auth",
    "body",
    "otp",
    "password",
    "secret",
    "token",
)

_SENSITIVE_RE = re.compile("|".join(re.escape(p) for p in SENSITIVE_PATTERNS), re.I)

_REDACTED = "[REDACTED]"

# Matches `?param=value` / `&param=value` where param is a sensitive name.
# The value is masked so URLs logged by libraries (e.g. httpx) never leak
# payload/key/token/cookie query strings. (plan invariants 2 & 6)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:" + "|".join(SENSITIVE_PATTERNS) + r")=)[^&\s\"']+",
    re.I,
)


def _is_sensitive_key(name: str) -> bool:
    return bool(_SENSITIVE_RE.search(name))


def _sanitize_message(message: str) -> str:
    """Mask sensitive query-param values embedded in a free-form log message."""
    return _QUERY_SECRET_RE.sub(r"\1" + _REDACTED, message)


def _redact(value: Any, depth: int = 0) -> Any:
    """Recursively drop/mask sensitive keys within nested structures."""
    if depth > 6:
        return _REDACTED
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, val in value.items():
            if _is_sensitive_key(str(key)):
                cleaned[str(key)] = _REDACTED
            else:
                cleaned[str(key)] = _redact(val, depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value]
    return value


class RedactionFilter(logging.Filter):
    """Filter that scrubs sensitive attributes from every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            record.extra_fields = _redact(extra)  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object of safe fields only."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "message": _sanitize_message(record.getMessage()),
            "request_id": get_request_id(),
        }

        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for key, val in _redact(extra).items():
                if key in SAFE_FIELDS:
                    payload[key] = val

        # Keep only safe fields; guarantees no accidental leakage.
        safe_payload = {k: v for k, v in payload.items() if k in SAFE_FIELDS}
        return json.dumps(safe_payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging with JSON formatting and redaction."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Quiet noisy libraries; they may emit sensitive URLs/bodies.
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger."""
    return logging.getLogger(name)
