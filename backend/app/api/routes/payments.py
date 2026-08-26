"""Payment routes — manual QR top-up flow (v1).

Thin HTTP adapters over :class:`PaymentService`. All endpoints are
authenticated; ownership is enforced inside the service by passing ``user_id``
(a payment owned by another user surfaces as not-found / forbidden, never
leaked). ``create`` uses the ``create`` rate class; reads use ``list``.

P1-03: Manual proof submission now accepts note and reference fields with validation.

The router is self-contained and importable without coupling to the app object;
it is mounted onto the v1 router in a later step.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, rate_limit
from app.core.errors import ValidationErrorError
from app.db.session import get_session
from app.domain.models import PaymentDTO
from app.domain.services import PaymentService

router = APIRouter(prefix="/payments", tags=["payments"])

# P1-03: Pattern to detect potentially dangerous content
_DANGEROUS_PATTERN = re.compile(
    r"<\s*script|javascript:|on\w+\s*=|<\s*iframe|<\s*object|<\s*embed",
    re.IGNORECASE,
)


class CreateQrBody(BaseModel):
    """Top-up request: either a prepaid ``package_code`` or a raw ``amount_vnd``."""

    package_code: str | None = None
    amount_vnd: int | None = None


class ManualProofBody(BaseModel):
    """P1-03: Manual proof submission with note and reference."""

    note: str = Field(default="", max_length=1000)
    reference: str = Field(default="", max_length=500)

    @field_validator("note", "reference")
    @classmethod
    def validate_no_dangerous_content(cls, v: str) -> str:
        """P1-03: Reject content with script tags or dangerous patterns."""
        if v and _DANGEROUS_PATTERN.search(v):
            raise ValueError("Content contains potentially dangerous characters")
        return v.strip()


@router.post("/qr", response_model=PaymentDTO, status_code=201)
async def create_qr(
    body: CreateQrBody,
    _rl=Depends(rate_limit("create")),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> PaymentDTO:
    svc = PaymentService(session)
    return await svc.create_qr(user_id, body.package_code, body.amount_vnd)


@router.post("/{payment_id}/manual-proof", response_model=PaymentDTO)
async def submit_manual_proof(
    payment_id: str,
    body: ManualProofBody | None = None,
    _rl=Depends(rate_limit("create")),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> PaymentDTO:
    """Submit manual payment proof (P1-03).

    Accepts optional note (max 1000 chars) and reference (max 500 chars).
    Content is validated to reject script tags and dangerous patterns.
    Only payments in 'pending' or 'pending_review' status can receive proof.
    """
    svc = PaymentService(session)
    note = body.note if body else ""
    reference = body.reference if body else ""
    return await svc.submit_manual_proof(user_id, payment_id, note=note, reference=reference)


@router.get("/{payment_id}", response_model=PaymentDTO)
async def get_payment(
    payment_id: str,
    _rl=Depends(rate_limit("list")),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> PaymentDTO:
    # Ownership enforced by passing user_id (403/404 if not owner).
    svc = PaymentService(session)
    return await svc.get_payment(user_id, payment_id)
