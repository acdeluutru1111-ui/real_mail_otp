"""Admin routes — privileged payment approval, reversal, and cookie management.

Gated behind ``require_admin`` (admin user IDs come from config). Approving a
payment grants wallet credit idempotently inside the service transaction.

P0-02: approve_payment now uses FOR UPDATE locking and records audit trail.
P0-04: Added reverse_payment endpoint for idempotent reversal workflow.

The router is self-contained and importable without coupling to the app object;
it is mounted onto the v1 router in a later step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.models import User
from app.db.session import get_session
from app.domain.models import LedgerEntryDTO, PaymentDTO
from app.domain.services import PaymentService
from app.integrations.cookie_manager import get_cookie_manager

router = APIRouter(prefix="/admin", tags=["admin"])


class ApprovePaymentBody(BaseModel):
    """Optional body for payment approval with audit reason."""
    reason: str | None = None


class ReversePaymentBody(BaseModel):
    """Required body for payment reversal."""
    reason: str


@router.post("/payments/{payment_id}/approve", response_model=PaymentDTO)
async def approve_payment(
    payment_id: str,
    body: ApprovePaymentBody | None = None,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PaymentDTO:
    """Approve a payment and grant credit to the user's wallet.

    P0-02: Uses SELECT ... FOR UPDATE to prevent concurrent double-credit.
    Only payments in 'pending_review' status can be approved.
    Re-approving an already-paid payment is idempotent (returns existing result).

    P0-04: Respects PAYMENT_APPROVAL_ENABLED kill switch.
    """
    svc = PaymentService(session)
    reason = body.reason if body else None
    return await svc.approve_payment(payment_id, admin_id=admin.id, reason=reason)


@router.post("/payments/{payment_id}/reverse", response_model=LedgerEntryDTO)
async def reverse_payment(
    payment_id: str,
    body: ReversePaymentBody,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> LedgerEntryDTO:
    """Create a reversal entry for a paid payment (P0-04).

    This creates a new ledger entry of type 'reversal' that offsets the
    original credit. It does NOT modify or delete the original entries.

    Idempotent: If a reversal already exists for this payment, returns
    the existing reversal entry instead of creating a duplicate.

    Requires a reason for audit trail.
    """
    svc = PaymentService(session)
    return await svc.reverse_payment(
        payment_id, admin_id=admin.id, reason=body.reason
    )


# ═══════════════════════════════════════════════════════════════════════════
#                         COOKIE MANAGEMENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

class RefreshCookiesBody(BaseModel):
    """Bounded controls for a manual refresh."""
    force: bool = False
    max_wait: int = Field(default=60, ge=1, le=600)
    poll_interval: int = Field(default=5, ge=1, le=60)


class CookieStatusResponse(BaseModel):
    """Metadata-only cookie status; never includes secret-derived fields."""
    status: str
    has_cookies: bool
    generation: int
    fetched_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    message: Optional[str] = None


@router.post("/refresh-cookies", response_model=CookieStatusResponse)
async def refresh_cookies(
    body: RefreshCookiesBody | None = None,
    admin: User = Depends(require_admin),
) -> CookieStatusResponse:
    """Trigger a bounded refresh and return metadata only."""
    manager = get_cookie_manager()
    
    force = body.force if body else False
    max_wait = body.max_wait if body else 60
    poll_interval = body.poll_interval if body else 5
    
    try:
        cookies = await manager.refresh_cookies(
            max_wait=max_wait,
            poll_interval=poll_interval,
            force=force,
        )
        
        return CookieStatusResponse(
            status="success",
            has_cookies=cookies.is_valid(),
            generation=manager.generation,
            fetched_at=cookies.fetched_at,
            expires_at=cookies.expires_at,
            message="Cookies refreshed successfully",
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail={"code": "COOKIE_REFRESH_FAILED", "message": "Cookie refresh failed."},
        )


@router.get("/cookies/status", response_model=CookieStatusResponse)
async def get_cookie_status(
    admin: User = Depends(require_admin),
) -> CookieStatusResponse:
    """Get current cookie status without triggering refresh.

    Returns the current state of cached cookies including:
    - Whether valid cookies are available
    - Masked previews of cookie values
    - When cookies were fetched
    - When cookies will expire

    Does NOT trigger automatic refresh. Use POST /refresh-cookies for that.
    """
    manager = get_cookie_manager()
    cookies = manager.get_cached_cookies()

    return CookieStatusResponse(
        status="ok",
        has_cookies=cookies.is_valid(),
        generation=manager.generation,
        fetched_at=cookies.fetched_at,
        expires_at=cookies.expires_at,
        message="Current cookie status" if cookies.is_valid() else "No valid cookies available",
    )


@router.delete("/cookies/clear")
async def clear_cookies(
    admin: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Clear in-memory cookies and any explicitly enabled legacy cache."""
    manager = get_cookie_manager()
    await manager.delete_cached_cookies()
    
    return {
        "status": "success",
        "message": "Cookies cleared",
    }


