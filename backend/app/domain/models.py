"""Domain models (DTOs) and pricing helpers.

These pydantic v2 models describe the shapes exchanged with clients and
between layers. INVARIANT: DTOs must NEVER carry secrets — no upstream
cookie, no inbox encryption key, and no raw upstream payload. Only safe,
client-facing projections are represented here.

Pricing:
- Fixed per-read price is sourced at runtime from ``get_settings().read_price_vnd``
  via :func:`read_price` (never hardcoded).
- Prepaid packages live in :data:`PACKAGES`.
- Pay-as-you-go top-ups grant ``credits == amount_vnd`` (200 VND = 1 read,
  matching the read price), resolved via :func:`resolve_topup`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.config import get_settings


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------
class InboxDTO(BaseModel):
    """Client-facing inbox projection.

    INVARIANT: never includes the inbox key or any upstream payload.
    """

    id: str
    address: str
    domain_type: str
    status: str
    created_at: datetime
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
class MessageMetaDTO(BaseModel):
    """Lightweight message summary (list view)."""

    mid: str
    subject: str | None = None
    sender: str | None = None
    received_at: datetime | None = None
    snippet: str | None = None


class BillingInfo(BaseModel):
    """Billing outcome attached to a message read.

    P0-01: source is now 'upstream' | 'cache' per OpenAPI contract.
    """

    charged: bool
    amount: int
    source: str  # 'upstream' | 'cache' per OpenAPI


class MessageDetailDTO(BaseModel):
    """Full message detail returned after a (possibly billed) read.

    P0-01: Field names aligned with OpenAPI contract:
    - html_sanitized (was body_html)
    - otp_candidates (was otp)
    - received_at (was date)
    """

    mid: str
    subject: str | None = None
    sender: str | None = None
    received_at: datetime | None = None
    html_sanitized: str | None = None
    otp_candidates: list[str] = Field(default_factory=list)
    billing: BillingInfo


# ---------------------------------------------------------------------------
# Billing / ledger / payments
# ---------------------------------------------------------------------------
class BalanceDTO(BaseModel):
    """Current wallet balance."""

    balance_vnd: int


class LedgerEntryDTO(BaseModel):
    """A single wallet ledger entry."""

    id: str
    type: str
    amount_vnd: int
    reference_type: str | None = None
    reference_id: str | None = None
    created_at: datetime


class PaymentDTO(BaseModel):
    """A payment/top-up record.

    P0-01: qr_content (was qr_payload) per OpenAPI contract.
    """

    id: str
    package_code: str | None = None
    amount_vnd: int
    status: str
    provider_ref: str
    created_at: datetime
    paid_at: datetime | None = None
    qr_content: str | None = None


# ---------------------------------------------------------------------------
# Refresh / pagination
# ---------------------------------------------------------------------------
class RefreshResultDTO(BaseModel):
    """Result of a mailbox refresh."""

    messages: list[MessageMetaDTO] = Field(default_factory=list)
    next_poll_after_seconds: int
    refreshed_at: datetime


class InboxPage(BaseModel):
    """Cursor-paginated page of inboxes."""

    items: list[InboxDTO] = Field(default_factory=list)
    next_cursor: str | None = None


class LedgerPage(BaseModel):
    """Cursor-paginated page of ledger entries."""

    items: list[LedgerEntryDTO] = Field(default_factory=list)
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
# Prepaid packages: code -> {"amount_vnd": int, "credits": int}
PACKAGES: dict[str, dict[str, int]] = {
    "starter": {"amount_vnd": 19000, "credits": 150},
    "popular": {"amount_vnd": 29000, "credits": 350},
    "pro": {"amount_vnd": 49000, "credits": 800},
}


def read_price() -> int:
    """Return the fixed per-read price (VND), sourced from runtime config."""
    return get_settings().read_price_vnd


# READ_PRICE constant is resolved at runtime from config (not hardcoded).
READ_PRICE: int = read_price()


def resolve_package(code: str) -> tuple[int, int]:
    """Resolve a prepaid package ``code`` to ``(amount_vnd, credits)``.

    Raises:
        ValueError: if the package code is unknown. The service/policy layer
            is expected to translate this into a proper AppError.
    """
    package = PACKAGES.get(code)
    if package is None:
        raise ValueError(f"Unknown package code: {code!r}")
    return package["amount_vnd"], package["credits"]


def resolve_topup(
    package_code: str | None,
    amount_vnd: int | None,
) -> tuple[int, int]:
    """Resolve a top-up request to ``(amount_vnd, credits)``.

    If ``package_code`` is given, the prepaid package pricing applies.
    Otherwise this is a pay-as-you-go top-up where ``credits == amount_vnd``
    (200 VND == 1 read, matching :data:`READ_PRICE`).

    Raises:
        ValueError: if the package code is unknown, or if neither a package
            code nor a positive amount is provided.
    """
    if package_code is not None:
        return resolve_package(package_code)
    if amount_vnd is None or amount_vnd <= 0:
        raise ValueError("A package_code or a positive amount_vnd is required")
    # Pay-as-you-go: 1 credit per VND (so amount_vnd VND == amount_vnd credits).
    return amount_vnd, amount_vnd
