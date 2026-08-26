"""Inbox routes — create / list / get / refresh / delete temporary inboxes.

These endpoints are thin HTTP adapters over :class:`InboxService`; all
business logic (quota, idempotency, encryption, caching, single-flight) lives in
the service layer. Refresh performs NO charge (plan §2.1 / §6.1).

The router is fully self-contained and importable without coupling to the app
object; it is mounted onto the v1 router in a later step.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, rate_limit
from app.db.models import User
from app.db.session import get_session
from app.domain.models import InboxDTO, InboxPage, RefreshResultDTO
from app.domain.services import InboxService

router = APIRouter(prefix="/inboxes", tags=["inboxes"])


class CreateInboxBody(BaseModel):
    """Request body for creating a temporary inbox."""

    domain: str


@router.post("", response_model=InboxDTO, status_code=201)
async def create_inbox(
    body: CreateInboxBody,
    idem: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
    _rl=Depends(rate_limit("create")),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> InboxDTO:
    svc = InboxService(session)
    return await svc.create_inbox(str(user.id), body.domain, idempotency_key=idem)


@router.get("", response_model=InboxPage)
async def list_inboxes(
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    _rl=Depends(rate_limit("list")),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> InboxPage:
    svc = InboxService(session)
    return await svc.list_inboxes(str(user.id), cursor, limit)


@router.get("/{inbox_id}", response_model=InboxDTO)
async def get_inbox(
    inbox_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> InboxDTO:
    svc = InboxService(session)
    return await svc.get_inbox(str(user.id), inbox_id)


@router.post("/{inbox_id}/refresh", response_model=RefreshResultDTO)
async def refresh_inbox(
    inbox_id: str,
    _rl=Depends(rate_limit("refresh")),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RefreshResultDTO:
    # NO CHARGE on refresh (plan §2.1 / §6.1).
    svc = InboxService(session)
    return await svc.refresh_inbox(str(user.id), inbox_id)


@router.delete("/{inbox_id}", status_code=204)
async def delete_inbox(
    inbox_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    svc = InboxService(session)
    await svc.delete_inbox(str(user.id), inbox_id)
    return Response(status_code=204)
