"""Request-ID context propagation.

Stores the current request id in a :class:`contextvars.ContextVar` so structured
logs and error envelopes can attach it without threading it through call stacks.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

_request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """Return the request id bound to the current context, if any."""
    return _request_id_ctx.get()


def set_request_id(request_id: str) -> None:
    """Bind ``request_id`` to the current context."""
    _request_id_ctx.set(request_id)


def new_request_id() -> str:
    """Generate a fresh request id."""
    return uuid.uuid4().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generate/propagate ``X-Request-ID`` for every request.

    Reuses an incoming ``X-Request-ID`` header when present, otherwise generates
    a new one, binds it to the context, and echoes it on the response.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming.strip() if incoming else new_request_id()
        set_request_id(request_id)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
