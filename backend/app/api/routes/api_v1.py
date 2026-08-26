"""v1 API router mount point.

Mounts every feature sub-router under the ``/v1`` parent. Each sub-router
declares its own prefix (``/auth``, ``/billing``, ``/payments``, ``/admin``,
``/inboxes``, and messages nested at ``/inboxes/{inbox_id}/messages``), so the
final paths are ``/v1/auth/...``, ``/v1/billing/...`` etc. with no duplicated
segment. Importing this module must always succeed so the app boots cleanly.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    admin,
    auth,
    billing,
    inboxes,
    messages,
    payments,
)

api_router = APIRouter(prefix="/v1")

api_router.include_router(auth.router)
api_router.include_router(billing.router)
api_router.include_router(payments.router)
api_router.include_router(admin.router)
api_router.include_router(inboxes.router)
api_router.include_router(messages.router)
