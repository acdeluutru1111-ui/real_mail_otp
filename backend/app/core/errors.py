"""Error taxonomy and FastAPI exception handling (plan section 14.2).

Every error carries a stable ``code``, an HTTP status, a ``retryable`` flag and a
*safe* message (no cookie/key/payload/body/OTP). The exception handler renders the
common envelope::

    {"error": {"code", "message", "retryable"}, "request_id"}
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.context import get_request_id


@dataclass(frozen=True)
class ErrorSpec:
    """Immutable descriptor for an error code."""

    code: str
    http_status: int
    retryable: bool
    message: str


# --- Full taxonomy (plan 14.2) ---------------------------------------------
# code -> (http_status, retryable, safe default message)
ERROR_SPECS: dict[str, ErrorSpec] = {
    "AUTH_UNAUTHENTICATED": ErrorSpec(
        "AUTH_UNAUTHENTICATED", 401, False, "Authentication required."
    ),
    "AUTH_TOKEN_EXPIRED": ErrorSpec(
        "AUTH_TOKEN_EXPIRED", 401, False, "Authentication token has expired."
    ),
    "AUTH_FORBIDDEN": ErrorSpec(
        "AUTH_FORBIDDEN", 403, False, "You do not have access to this resource."
    ),
    # P1-04: NOT_FOUND for missing/not-owned resources (avoids enumeration)
    "NOT_FOUND": ErrorSpec(
        "NOT_FOUND", 404, False, "Resource not found."
    ),
    "VALIDATION_ERROR": ErrorSpec(
        "VALIDATION_ERROR", 422, False, "Request validation failed."
    ),
    "UPSTREAM_AUTH": ErrorSpec(
        "UPSTREAM_AUTH", 502, False, "Upstream authentication failed."
    ),
    "UPSTREAM_TIMEOUT": ErrorSpec(
        "UPSTREAM_TIMEOUT", 504, True, "Upstream request timed out."
    ),
    "UPSTREAM_RATE_LIMIT": ErrorSpec(
        "UPSTREAM_RATE_LIMIT", 429, True, "Upstream rate limit reached."
    ),
    "UPSTREAM_BAD_RESPONSE": ErrorSpec(
        "UPSTREAM_BAD_RESPONSE", 502, False, "Upstream returned an invalid response."
    ),
    "UPSTREAM_UNAVAILABLE": ErrorSpec(
        "UPSTREAM_UNAVAILABLE", 503, True, "Upstream service is unavailable."
    ),
    "CACHE_ERROR": ErrorSpec("CACHE_ERROR", 500, True, "A cache error occurred."),
    "DB_ERROR": ErrorSpec("DB_ERROR", 500, True, "A database error occurred."),
    "BILLING_INSUFFICIENT": ErrorSpec(
        "BILLING_INSUFFICIENT", 402, False, "Insufficient credit balance."
    ),
    "BILLING_CONFLICT": ErrorSpec(
        "BILLING_CONFLICT", 409, False, "Billing conflict for this operation."
    ),
    "PAYMENT_ERROR": ErrorSpec(
        "PAYMENT_ERROR", 400, False, "Payment could not be processed."
    ),
    "ABUSE_BLOCKED": ErrorSpec(
        "ABUSE_BLOCKED", 429, True, "Request blocked due to abuse protection."
    ),
    "INTERNAL_ERROR": ErrorSpec(
        "INTERNAL_ERROR", 500, False, "An internal error occurred."
    ),
}


class AppError(Exception):
    """Base application error mapped to a taxonomy code.

    ``message`` must always be safe to expose; never pass secrets or raw upstream
    bodies. Concrete subclasses bind a ``code`` from :data:`ERROR_SPECS`.
    """

    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str | None = None) -> None:
        spec = ERROR_SPECS[self.code]
        self.http_status: int = spec.http_status
        self.retryable: bool = spec.retryable
        self.message: str = message or spec.message
        super().__init__(self.message)

    def to_envelope(self, request_id: str | None) -> dict[str, object]:
        """Render the public error envelope."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            },
            "request_id": request_id,
        }


def _make(code: str) -> type[AppError]:
    """Build a concrete AppError subclass bound to ``code``."""

    return type(_class_name(code), (AppError,), {"code": code})


def _class_name(code: str) -> str:
    return "".join(part.capitalize() for part in code.split("_")) + "Error"


# --- Concrete exception classes --------------------------------------------
AuthUnauthenticatedError = _make("AUTH_UNAUTHENTICATED")
AuthTokenExpiredError = _make("AUTH_TOKEN_EXPIRED")
AuthForbiddenError = _make("AUTH_FORBIDDEN")
NotFoundError = _make("NOT_FOUND")  # P1-04: 404 for missing/not-owned
ValidationErrorError = _make("VALIDATION_ERROR")
UpstreamAuthError = _make("UPSTREAM_AUTH")
UpstreamTimeoutError = _make("UPSTREAM_TIMEOUT")
UpstreamRateLimitError = _make("UPSTREAM_RATE_LIMIT")
UpstreamBadResponseError = _make("UPSTREAM_BAD_RESPONSE")
UpstreamUnavailableError = _make("UPSTREAM_UNAVAILABLE")
CacheErrorError = _make("CACHE_ERROR")
DbErrorError = _make("DB_ERROR")
BillingInsufficientError = _make("BILLING_INSUFFICIENT")
BillingConflictError = _make("BILLING_CONFLICT")
PaymentErrorError = _make("PAYMENT_ERROR")
AbuseBlockedError = _make("ABUSE_BLOCKED")
InternalErrorError = _make("INTERNAL_ERROR")


def _add_cors_headers(request: Request, response: JSONResponse) -> None:
    """Ensure CORS headers are attached to error responses even when handled outside CORSMiddleware."""
    try:
        from app.core.config import get_settings

        settings = get_settings()
        origin = request.headers.get("origin")
        if origin and settings.cors_origins:
            if origin in settings.cors_origins or "*" in settings.cors_origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Expose-Headers"] = "X-Request-ID"
    except Exception:
        pass


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """FastAPI handler rendering the common error envelope.

    P1-06: Adds Retry-After header for 429 responses when retry_after is available.
    """
    request_id = get_request_id()
    response = JSONResponse(
        status_code=exc.http_status,
        content=exc.to_envelope(request_id),
    )
    if request_id:
        response.headers["X-Request-ID"] = request_id

    # P1-06: Add Retry-After header for rate limit errors
    if exc.http_status == 429:
        # Check if the exception has retry_after attribute (set by rate limiter)
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None and retry_after > 0:
            # Round up to nearest second
            response.headers["Retry-After"] = str(int(retry_after) + 1)
        else:
            # Default to 60 seconds if not specified
            response.headers["Retry-After"] = "60"

    _add_cors_headers(request, response)
    return response


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Fallback handler: never leak internals, always the safe envelope."""
    request_id = get_request_id()
    internal = InternalErrorError()
    response = JSONResponse(
        status_code=internal.http_status,
        content=internal.to_envelope(request_id),
    )
    if request_id:
        response.headers["X-Request-ID"] = request_id

    _add_cors_headers(request, response)
    return response

