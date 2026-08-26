"""Billing routes — read-only wallet balance + ledger history.

Thin HTTP adapters over :class:`BillingService`. Both endpoints are
authenticated (``get_current_user_id``) and throttled with the ``list`` rate
class. No money moves here; charging happens only on a detail read.

The router is self-contained and importable without coupling to the app object;
it is mounted onto the v1 router in a later step.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, rate_limit
from app.db.session import get_session
from app.domain.models import BalanceDTO, LedgerPage
from app.domain.services import BillingService

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/balance", response_model=BalanceDTO)
async def get_balance(
    _rl=Depends(rate_limit("list")),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> BalanceDTO:
    svc = BillingService(session)
    return await svc.get_balance(user_id)


@router.get("/ledger", response_model=LedgerPage)
async def get_ledger(
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    _rl=Depends(rate_limit("list")),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> LedgerPage:
    svc = BillingService(session)
    return await svc.get_ledger(user_id, cursor, limit)
