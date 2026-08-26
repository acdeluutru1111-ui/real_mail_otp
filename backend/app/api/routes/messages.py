"""Message routes — list metadata (no charge) and read detail (charged).

Nested under an inbox: ``/inboxes/{inbox_id}/messages``. These endpoints are
thin HTTP adapters over :class:`MessageService`. Listing metadata never charges;
the detail read performs the plan §7.2 read-and-charge transaction inside the
service. Billing errors (e.g. BILLING_INSUFFICIENT) propagate to the global
error handler.

The router is fully self-contained and importable without coupling to the app
object; it is mounted onto the v1 router in a later step.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, rate_limit
from app.db.models import User
from app.db.session import get_session
from app.domain.models import MessageDetailDTO, MessageMetaDTO
from app.domain.services import MessageService

# P0-01: MessageList wrapper for OpenAPI contract compliance
from pydantic import BaseModel


class MessageListResponse(BaseModel):
    """P0-01: Message list wrapped in {items:[...]} per OpenAPI contract."""

    items: list[MessageMetaDTO]


router = APIRouter(prefix="/inboxes/{inbox_id}/messages", tags=["messages"])


@router.get("", response_model=MessageListResponse)
async def list_messages(
    inbox_id: str,
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    _rl=Depends(rate_limit("list")),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageListResponse:
    # NO CHARGE on listing message metadata.
    svc = MessageService(session)
    items = await svc.list_messages(str(user.id), inbox_id, cursor, limit)
    return MessageListResponse(items=items)


@router.get("/{mid}", response_model=MessageDetailDTO)
async def read_message_detail(
    inbox_id: str,
    mid: str,
    _rl=Depends(rate_limit("detail")),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageDetailDTO:
    # Charge happens inside the service (plan §7.2); billing errors like
    # BILLING_INSUFFICIENT propagate to the global handler.
    svc = MessageService(session)
    return await svc.read_message_detail(str(user.id), inbox_id, mid)
