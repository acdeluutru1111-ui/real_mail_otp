"""Domain service layer — orchestration of repositories, cache, upstream, and
the money-moving read-and-charge transaction (plan sections 6, 7, 7.2, 9).

Each service accepts an :class:`AsyncSession` and constructs the repositories it
needs from that session, so the whole request runs in a single, short,
request-scoped transaction (see ``app.db.session.get_session``, which commits on
success and rolls back on any exception).

INVARIANTS (SYSTEM_BUILD_PLAN_v2.md §0 — non-negotiable):
- Never log cookie / key / payload / body / OTP at any level.
- The browser never receives an upstream cookie / key / payload.
- No ``wait_*`` / ``full_flow`` in a web request; endpoints stay stateless/fast.
- Payload is NEVER part of the billing dedupe key. The dedupe key is exactly
  ``(provider, domain_type, inbox_id, mid, user_id)``.
- Charge only on a genuinely successful, committed detail read. NEVER charge on
  refresh / poll / list / cache-hit / reopen / any error.
- The billing dedupe insert AND the wallet debit happen in the SAME transaction
  so a rollback (e.g. insufficient balance) removes the billing_read row and no
  charge is persisted.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from app.cache.memory import (
    cache,
    detail_key,
    detail_ttl,
    list_key,
    list_ttl,
    negative_ttl,
)
from app.cache.singleflight import single_flight
from app.core.config import is_billing_enabled, is_payment_approval_enabled
from app.core.errors import (
    BillingConflictError,
    BillingInsufficientError,
    NotFoundError,
    UpstreamBadResponseError,
    UpstreamUnavailableError,
    ValidationErrorError,
)
from app.core.security import address_hash, decrypt_value, encrypt_value
from app.db.models import LedgerEntryType, PaymentStatus
from app.domain import models as dto
from app.domain import policies
from app.integrations import normalizers
from app.integrations.domains import domain_type_for
from app.integrations.smailpro import SmailProAdapter
from app.integrations.sonjj import SonjjAdapter
from app.repositories.billing_repo import BillingRepository
from app.repositories.inbox_repo import InboxRepository
from app.repositories.ledger_repo import LedgerRepository
from app.repositories.message_repo import MessageRepository
from app.repositories.payments_repo import PaymentsRepository
from app.repositories.wallets_repo import WalletsRepository

# Provider constant for this service (billing dedupe + inbox provenance).
PROVIDER = "smailpro"

# Small, constant client back-off hint (seconds) for the refresh endpoint. The
# authoritative polling schedule lives on the client (plan §8); this is only a
# lower-bound "don't hammer me" hint.
_NEXT_POLL_AFTER_SECONDS = 3


# ---------------------------------------------------------------------------
# In-process idempotency map for create_inbox (v1 ONLY).
# ---------------------------------------------------------------------------
# NOTE (v1 limitation): this is a *single-process, in-RAM* idempotency store. It
# maps f"{user_id}:{idempotency_key}" -> inbox_id so that a client retry with the
# same Idempotency-Key returns the same inbox instead of creating a duplicate.
# It does NOT survive a restart and is NOT shared across replicas — acceptable
# for the v1 single-replica deployment (plan §4). A durable idempotency table is
# a Phase-2 concern. Guarded by an asyncio.Lock for concurrent-safe access.
_create_inbox_idempotency: dict[str, str] = {}
_create_inbox_idempotency_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """Coerce a value to ``uuid.UUID`` (accepts an already-parsed UUID)."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _parse_received_at(value: str | None) -> datetime | None:
    """Best-effort parse of an upstream date string into an aware datetime.

    Returns ``None`` when the value is missing or cannot be parsed; the message
    metadata simply stores a null ``received_at`` in that case.
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _inbox_to_dto(inbox) -> dto.InboxDTO:
    """Project an ORM :class:`Inbox` to a client-safe DTO.

    INVARIANT: the address is the decrypted plaintext for display only; the key
    and any payload are NEVER exposed.
    """
    return dto.InboxDTO(
        id=str(inbox.id),
        address=decrypt_value(inbox.address_encrypted),
        domain_type=inbox.domain_type,
        status=inbox.status.value if hasattr(inbox.status, "value") else str(inbox.status),
        created_at=inbox.created_at,
        expires_at=inbox.expires_at,
    )


def _message_to_meta_dto(message) -> dto.MessageMetaDTO:
    """Project an ORM :class:`Message` (metadata only) to a DTO."""
    return dto.MessageMetaDTO(
        mid=message.mid,
        subject=message.subject_sanitized,
        sender=message.sender_sanitized,
        received_at=message.received_at,
        snippet=None,
    )


def _normalized_item_to_meta_dto(item: dict[str, str]) -> dto.MessageMetaDTO:
    """Project a normalized upstream list item to a client-safe DTO."""
    return dto.MessageMetaDTO(
        mid=item.get("mid", ""),
        subject=item.get("subject") or None,
        sender=item.get("sender") or None,
        received_at=_parse_received_at(item.get("date")),
        snippet=item.get("snippet") or None,
    )


# ---------------------------------------------------------------------------
# InboxService
# ---------------------------------------------------------------------------
class InboxService:
    """Create / list / read / refresh temporary inboxes.

    Refresh performs NO charge (plan §2.1 / §6.1).
    """

    def __init__(self, session) -> None:
        self.session = session
        self.inbox_repo = InboxRepository(session)
        self.message_repo = MessageRepository(session)
        self.smailpro = SmailProAdapter()
        self.sonjj = SonjjAdapter()

    async def create_inbox(
        self,
        user_id: uuid.UUID | str,
        domain: str,
        idempotency_key: str | None = None,
    ) -> dto.InboxDTO:
        """Create a temporary inbox for ``user_id`` on ``domain``.

        Enforces the active-inbox fair-use quota, supports an in-process (v1)
        idempotency key, creates the upstream mailbox, and persists the address
        and key ENCRYPTED at rest. Returns a client-safe DTO (never the key).
        """
        uid = _to_uuid(user_id)

        # --- v1 in-process idempotency: short-circuit on a known key ---------
        idem_map_key: str | None = None
        if idempotency_key:
            idem_map_key = f"{uid}:{idempotency_key}"
            async with _create_inbox_idempotency_lock:
                existing_id = _create_inbox_idempotency.get(idem_map_key)
            if existing_id is not None:
                existing = await self.inbox_repo.get_owned(_to_uuid(existing_id), uid)
                if existing is not None:
                    return _inbox_to_dto(existing)

        # --- fair-use quota: count active (non-deleted) inboxes --------------
        active = await self.inbox_repo.list_by_user(uid, limit=1000)
        active_count = sum(
            1
            for ib in active
            if getattr(ib.status, "value", str(ib.status)) == "active"
        )
        policies.ensure_active_inbox_quota(active_count)

        # --- create upstream mailbox (never logs address/key) ----------------
        created = await self.smailpro.create(domain=domain)
        address = created["address"]
        key = created["key"]
        timestamp = created.get("timestamp")

        # domain_type drives Sonjj endpoint + billing dedupe; derived from the
        # actual address returned by upstream (not the requested domain).
        computed_domain_type = domain_type_for(address)

        inbox = await self.inbox_repo.create(
            user_id=uid,
            provider=PROVIDER,
            domain_type=computed_domain_type,
            address_hash=address_hash(address),
            address_encrypted=encrypt_value(address),
            key_encrypted=encrypt_value(key),
            timestamp=str(timestamp) if timestamp is not None else None,
        )

        # --- record idempotency mapping (v1 in-process only) -----------------
        if idem_map_key is not None:
            async with _create_inbox_idempotency_lock:
                _create_inbox_idempotency[idem_map_key] = str(inbox.id)

        return _inbox_to_dto(inbox)

    async def list_inboxes(
        self,
        user_id: uuid.UUID | str,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dto.InboxPage:
        """Return a cursor-paginated page of the user's inboxes."""
        uid = _to_uuid(user_id)
        cursor_uuid = _to_uuid(cursor) if cursor else None
        inboxes = await self.inbox_repo.list_by_user(
            uid, cursor=cursor_uuid, limit=limit
        )
        items = [_inbox_to_dto(ib) for ib in inboxes]
        next_cursor = str(inboxes[-1].id) if len(inboxes) == limit else None
        return dto.InboxPage(items=items, next_cursor=next_cursor)

    async def get_inbox(
        self, user_id: uuid.UUID | str, inbox_id: uuid.UUID | str
    ) -> dto.InboxDTO:
        """Return a single owned inbox, or raise if not found / not owned."""
        inbox = await self._require_owned_inbox(user_id, inbox_id)
        return _inbox_to_dto(inbox)

    async def refresh_inbox(
        self, user_id: uuid.UUID | str, inbox_id: uuid.UUID | str
    ) -> dto.RefreshResultDTO:
        """Refresh the inbox message list. NO CHARGE (plan §2.1 / §6.1).

        Uses a RAM list cache + single-flight so N concurrent refreshes for the
        same inbox trigger exactly one upstream fetch. Empty inboxes are
        negatively cached briefly. Message metadata is upserted for the list
        view. NEVER logs or returns the payload.
        """
        inbox = await self._require_owned_inbox(user_id, inbox_id)
        inbox_id_str = str(inbox.id)
        lkey = list_key(inbox_id_str)

        # Positive cache hit -> reuse normalized list without any upstream call.
        cached = await cache.get(lkey)
        if cached is not None:
            items = cached
        elif await cache.is_negative(lkey):
            items = []
        else:
            # Decrypt credentials in-memory only; never logged.
            address = decrypt_value(inbox.address_encrypted)
            key = decrypt_value(inbox.key_encrypted)
            timestamp = inbox.timestamp

            async def _fetch() -> list[dict[str, str]]:
                payload = await self.smailpro.get_inbox_payload(
                    address, timestamp, key
                )
                if not payload:
                    return []
                return await self.sonjj.list_messages(payload, address)

            items = await single_flight.do(lkey, _fetch)

            if items:
                await cache.set(lkey, items, list_ttl())
            else:
                # Negative-cache an empty inbox to dampen polling storms.
                await cache.set_negative(lkey, negative_ttl())

        # Persist sanitized metadata for each message (idempotent upsert).
        messages: list[dto.MessageMetaDTO] = []
        for item in items:
            mid = item.get("mid")
            if not mid:
                continue
            received_at = _parse_received_at(item.get("date"))
            subject = policies.sanitize_text(item.get("subject") or "") or None
            sender = policies.sanitize_text(item.get("sender") or "") or None
            await self.message_repo.upsert_metadata(
                inbox_id=inbox.id,
                mid=mid,
                subject_sanitized=subject,
                sender_sanitized=sender,
                received_at=received_at,
            )
            messages.append(_normalized_item_to_meta_dto(item))

        return dto.RefreshResultDTO(
            messages=messages,
            next_poll_after_seconds=_NEXT_POLL_AFTER_SECONDS,
            refreshed_at=_now(),
        )

    async def _require_owned_inbox(
        self, user_id: uuid.UUID | str, inbox_id: uuid.UUID | str
    ):
        """Load an inbox and enforce ownership, or raise NOT_FOUND (P1-04)."""
        uid = _to_uuid(user_id)
        try:
            iid = _to_uuid(inbox_id)
        except ValueError:
            # P1-04: malformed UUID -> 404 (not 500)
            raise NotFoundError("Inbox not found.")
        inbox = await self.inbox_repo.get_owned(iid, uid)
        if inbox is None:
            # P1-04: get_owned already scopes to the owner; a miss is a not-found
            # for this user. Surface as 404 (safe, no leakage).
            raise NotFoundError("Inbox not found.")
        return inbox


# ---------------------------------------------------------------------------
# MessageService
# ---------------------------------------------------------------------------
class MessageService:
    """List message metadata (no charge) and read message detail (charged).

    The detail read implements the plan §7.2 read-and-charge transaction exactly.
    """

    def __init__(self, session) -> None:
        self.session = session
        self.inbox_repo = InboxRepository(session)
        self.message_repo = MessageRepository(session)
        self.billing_repo = BillingRepository(session)
        self.wallets_repo = WalletsRepository(session)
        self.ledger_repo = LedgerRepository(session)
        self.sonjj = SonjjAdapter()

    async def list_messages(
        self,
        user_id: uuid.UUID | str,
        inbox_id: uuid.UUID | str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[dto.MessageMetaDTO]:
        """Return stored message metadata for an owned inbox. NO CHARGE."""
        inbox = await self._require_owned_inbox(user_id, inbox_id)
        messages = await self.message_repo.list_by_inbox(inbox.id, limit=limit)
        return [_message_to_meta_dto(m) for m in messages]

    async def read_message_detail(
        self,
        user_id: uuid.UUID | str,
        inbox_id: uuid.UUID | str,
        mid: str,
    ) -> dto.MessageDetailDTO:
        """Read a single message's detail, charging exactly once (plan §7.2).

        Steps (see module docstring for the invariants they enforce):
          1. Ownership + verify ``mid`` belongs to this inbox.
          2. If already charged (dedupe pre-check) -> return detail, charged=False.
          3. Single-flight fetch + validate + sanitize + cache the detail.
          4/5. Atomic dedupe insert (ON CONFLICT DO NOTHING RETURNING id).
          6. Only on a fresh insert: FOR UPDATE wallet, affordability check,
             debit + ledger append — in the SAME transaction as the insert.
          7. Any error -> rollback (no charge persisted); re-raise.
        """
        uid = _to_uuid(user_id)
        inbox = await self._require_owned_inbox(uid, inbox_id)
        amount = policies.read_charge_amount()

        # --- Step 1: verify the mid belongs to this inbox -------------------
        # This prevents fetching arbitrary mids and guarantees the billing
        # dedupe tuple refers to a real, owned message.
        known = await self.message_repo.get_by_inbox_and_mid(inbox.id, mid)
        if known is None:
            raise ValidationErrorError("Message not found for this inbox.")

        # --- Step 2: dedupe pre-check (cache miss != not charged) -----------
        already_charged = await self.billing_repo.exists_read(
            user_id=uid,
            inbox_id=inbox.id,
            provider=PROVIDER,
            domain_type=inbox.domain_type,
            mid=mid,
        )
        if already_charged:
            detail, from_cache = await self._get_detail(inbox, mid)
            return self._detail_to_dto(
                detail, mid, charged=False, amount=amount, from_cache=from_cache
            )

        # --- Step 3: fetch + validate + sanitize + cache the detail ----------
        # Done BEFORE opening the billing transaction so an upstream failure
        # never reaches the charge path (no charge on error / bad response).
        detail, from_cache = await self._get_detail(inbox, mid)

        # --- Steps 4-7: the atomic read-and-charge transaction --------------
        try:
            # Step 4/5: atomic dedupe insert. On conflict -> None (already
            # charged / concurrent duplicate) => do NOT charge.
            billing_id = await self.billing_repo.insert_read(
                user_id=uid,
                inbox_id=inbox.id,
                provider=PROVIDER,
                domain_type=inbox.domain_type,
                mid=mid,
                amount_vnd=amount,
            )
            if billing_id is None:
                # Concurrent duplicate won the race; commit the (no-op) tx and
                # return the detail without charging.
                await self.session.commit()
                return self._detail_to_dto(
                    detail, mid, charged=False, amount=amount, from_cache=from_cache
                )

            # Step 6: lock the wallet, verify affordability, debit + ledger.
            # insert_read + debit share this transaction, so a rollback below
            # (e.g. BILLING_INSUFFICIENT) also removes the billing_read row —
            # no charge is ever left behind.
            wallet = await self.wallets_repo.get_for_update(uid)
            balance = wallet.balance_vnd if wallet is not None else 0
            if wallet is None or not policies.can_afford(balance, amount):
                # Rolls back the billing_read insert above -> nothing persisted.
                raise BillingInsufficientError()

            await self.wallets_repo.apply_debit(wallet, amount)
            await self.ledger_repo.append(
                user_id=uid,
                type=LedgerEntryType.debit,
                amount_vnd=-amount,
                reference_type="billing_read",
                reference_id=str(billing_id),
            )

            # Step 8: commit, THEN report charged=True.
            await self.session.commit()
            return self._detail_to_dto(detail, mid, charged=True, amount=amount, from_cache=from_cache)
        except Exception:
            # Step 7: any failure rolls back the whole transaction so the
            # billing_read insert + any debit are undone; never charge on error.
            await self.session.rollback()
            raise

    async def _get_detail(self, inbox, mid: str) -> tuple[dict[str, str], bool]:
        """Return a sanitized message detail from cache or upstream.

        Uses single-flight so concurrent readers of the same (inbox, mid) hit
        upstream once. The cached value is the SANITIZED detail (safe to reuse);
        the raw payload/body is never cached to logs. Validates that the
        response is non-empty and matches ``mid`` (else UPSTREAM_BAD_RESPONSE).

        P0-01: Returns (detail, from_cache) tuple to track billing source.
        """
        inbox_id_str = str(inbox.id)
        dkey = detail_key(inbox_id_str, mid)

        cached = await cache.get(dkey)
        if cached is not None:
            return cached, True  # P0-01: from cache

        address = decrypt_value(inbox.address_encrypted)
        key = decrypt_value(inbox.key_encrypted)
        timestamp = inbox.timestamp

        async def _fetch() -> dict[str, str]:
            payload = await SmailProAdapter().get_inbox_payload(
                address, timestamp, key
            )
            if not payload:
                raise UpstreamBadResponseError()
            raw = await self.sonjj.get_message_detail(payload, mid, address)
            # Validate: non-empty and matching mid.
            if not raw or (raw.get("mid") and str(raw.get("mid")) != str(mid)):
                raise UpstreamBadResponseError()
            body_html = raw.get("body_html") or raw.get("body") or ""
            sanitized_html = policies.sanitize_email_html(body_html)
            body_text = raw.get("body_text") or ""
            # Extract OTP/links from the message body when easily available
            # (best-effort, never logged).
            otp = normalizers.extract_otp_code(body_text or body_html or "")
            links = normalizers.extract_links(body_text or body_html or "")
            return {
                "mid": str(raw.get("mid") or mid),
                "subject": policies.sanitize_text(raw.get("subject") or ""),
                "sender": policies.sanitize_text(raw.get("sender") or ""),
                "to": policies.sanitize_text(raw.get("to") or ""),
                "date": raw.get("date") or "",
                "body_html": sanitized_html,
                "body_text": body_text,
                "otp": otp or "",
                "links": links,
            }

        detail = await single_flight.do(dkey, _fetch)
        # Cache only AFTER successful validation + sanitization.
        await cache.set(dkey, detail, detail_ttl())
        return detail, False  # P0-01: from upstream

    def _detail_to_dto(
        self,
        detail: dict,
        mid: str,
        *,
        charged: bool,
        amount: int,
        from_cache: bool = False,
    ) -> dto.MessageDetailDTO:
        """Build the client-facing detail DTO with billing info attached.

        P0-01: Field names aligned with OpenAPI contract:
        - html_sanitized (was body_html)
        - otp_candidates (was otp) - now a list
        - received_at (was date)
        - billing.source is 'upstream' | 'cache' (was 'read')
        """
        # P0-01: Extract OTP candidates as a list
        otp_raw = detail.get("otp") or ""
        otp_candidates = [otp_raw] if otp_raw else []

        # P0-01: billing.source is 'cache' if from_cache, else 'upstream'
        billing_source = "cache" if from_cache else "upstream"

        return dto.MessageDetailDTO(
            mid=detail.get("mid") or mid,
            subject=(detail.get("subject") or None),
            sender=(detail.get("sender") or None),
            received_at=_parse_received_at(detail.get("date")),
            html_sanitized=(detail.get("body_html") or None),
            otp_candidates=otp_candidates,
            billing=dto.BillingInfo(charged=charged, amount=amount, source=billing_source),
        )

    async def _require_owned_inbox(
        self, user_id: uuid.UUID | str, inbox_id: uuid.UUID | str
    ):
        """P1-04: Load an inbox and enforce ownership, or raise NOT_FOUND."""
        uid = _to_uuid(user_id)
        try:
            iid = _to_uuid(inbox_id)
        except ValueError:
            # P1-04: malformed UUID -> 404 (not 500)
            raise NotFoundError("Inbox not found.")
        inbox = await self.inbox_repo.get_owned(iid, uid)
        if inbox is None:
            raise NotFoundError("Inbox not found.")
        return inbox


# ---------------------------------------------------------------------------
# BillingService
# ---------------------------------------------------------------------------
class BillingService:
    """Read-only wallet balance + ledger history."""

    def __init__(self, session) -> None:
        self.session = session
        self.wallets_repo = WalletsRepository(session)
        self.ledger_repo = LedgerRepository(session)

    async def get_balance(self, user_id: uuid.UUID | str) -> dto.BalanceDTO:
        """Return the user's current balance, treating no-wallet as 0 VND."""
        uid = _to_uuid(user_id)
        wallet = await self.wallets_repo.get(uid)
        balance = wallet.balance_vnd if wallet is not None else 0
        return dto.BalanceDTO(balance_vnd=balance)

    async def get_ledger(
        self,
        user_id: uuid.UUID | str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dto.LedgerPage:
        """Return a cursor-paginated page of ledger entries, newest first."""
        uid = _to_uuid(user_id)
        cursor_uuid = _to_uuid(cursor) if cursor else None
        entries = await self.ledger_repo.list_by_user(
            uid, cursor=cursor_uuid, limit=limit
        )
        items = [
            dto.LedgerEntryDTO(
                id=str(e.id),
                type=e.type.value if hasattr(e.type, "value") else str(e.type),
                amount_vnd=e.amount_vnd,
                reference_type=e.reference_type,
                reference_id=e.reference_id,
                created_at=e.created_at,
            )
            for e in entries
        ]
        next_cursor = str(entries[-1].id) if len(entries) == limit else None
        return dto.LedgerPage(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# PaymentService
# ---------------------------------------------------------------------------
class PaymentService:
    """Manual QR top-up flow (v1): create QR, submit proof, get, admin approve.

    Credit is granted ONLY inside the admin approval transaction, and approval
    is idempotent (re-approving a paid payment never double-credits).
    """

    def __init__(self, session) -> None:
        self.session = session
        self.payments_repo = PaymentsRepository(session)
        self.wallets_repo = WalletsRepository(session)
        self.ledger_repo = LedgerRepository(session)

    async def create_qr(
        self,
        user_id: uuid.UUID | str,
        package_code: str | None = None,
        amount_vnd: int | None = None,
    ) -> dto.PaymentDTO:
        """Create a pending payment + a deterministic v1 QR payload string.

        Resolves the amount/credits via policy, generates a unique provider_ref,
        and builds a simple VietQR-like transfer string carrying the amount and
        the provider_ref as the transfer memo. No auto-verification.
        """
        uid = _to_uuid(user_id)
        resolved_amount, _credits = policies.resolve_topup_amount(
            package_code, amount_vnd
        )
        provider_ref = f"PMT-{uuid.uuid4().hex}"

        payment = await self.payments_repo.create(
            user_id=uid,
            provider=PROVIDER,
            provider_ref=provider_ref,
            amount_vnd=resolved_amount,
            package_code=package_code,
        )
        await self.session.commit()

        qr_content = self._build_qr_payload(resolved_amount, provider_ref)
        result = self._payment_to_dto(payment)
        result.qr_content = qr_content  # P0-01: renamed from qr_payload
        return result

    async def submit_manual_proof(
        self,
        user_id: uuid.UUID | str,
        payment_id: uuid.UUID | str,
        note: str = "",
        reference: str = "",
    ) -> dto.PaymentDTO:
        """Mark an owned pending payment as ``pending_review`` (proof submitted).

        P1-03: Now accepts note and reference fields for manual proof persistence.
        - note: max 1000 chars, user's description of the payment
        - reference: max 500 chars, transaction reference number

        We mutate the loaded ORM model + flush/commit rather than adding a new
        repo method (the repo intentionally only exposes ``mark_paid``).
        """
        from datetime import datetime, timezone as tz  # local import

        uid = _to_uuid(user_id)
        payment = await self._require_owned_payment(uid, payment_id)
        # Only move a not-yet-finalized payment into review.
        from app.db.models import PaymentStatus  # local import: avoid cycle risk

        if payment.status not in (PaymentStatus.pending, PaymentStatus.pending_review):
            raise ValidationErrorError(
                f"Cannot submit proof for payment with status '{payment.status.value}'. "
                "Only pending or pending_review payments can receive proof."
            )

        payment.status = PaymentStatus.pending_review
        # P1-03: Store proof data
        if note:
            payment.proof_note = note[:1000]  # Enforce max length
        if reference:
            payment.proof_reference = reference[:500]  # Enforce max length
        payment.proof_submitted_at = datetime.now(tz.utc)

        await self.session.flush()
        await self.session.commit()
        return self._payment_to_dto(payment)

    async def get_payment(
        self,
        user_id: uuid.UUID | str,
        payment_id: uuid.UUID | str,
    ) -> dto.PaymentDTO:
        """Return an owned payment record."""
        uid = _to_uuid(user_id)
        payment = await self._require_owned_payment(uid, payment_id)
        return self._payment_to_dto(payment)

    async def approve_payment(
        self,
        payment_id: uuid.UUID | str,
        admin_id: uuid.UUID | str,
        reason: str | None = None,
    ) -> dto.PaymentDTO:
        """ADMIN: approve a payment and grant credit, idempotently (P0-02).

        P0-02 fixes:
        - Uses SELECT ... FOR UPDATE to lock the payment row first
        - Only allows transition from pending_review -> paid
        - Re-approving an already-paid payment returns the existing result (idempotent)
        - Records audit trail: admin_id, reason, credited_vnd snapshot

        P0-04: Respects payment_approval_enabled kill switch.
        """
        # P0-04: Check kill switch
        if not is_payment_approval_enabled():
            raise UpstreamUnavailableError("Payment approval is currently disabled.")

        pid = _to_uuid(payment_id)
        admin_uid = _to_uuid(admin_id)

        try:
            # P0-02: Lock the payment row FIRST to prevent concurrent approvals
            payment = await self.payments_repo.get_for_update(pid)
            if payment is None:
                raise ValidationErrorError("Payment not found.")

            # Idempotent: already paid -> return without crediting again.
            if payment.status == PaymentStatus.paid:
                return self._payment_to_dto(payment)

            # P0-02: Only allow transition from pending_review -> paid
            if payment.status != PaymentStatus.pending_review:
                raise ValidationErrorError(
                    f"Payment cannot be approved from status '{payment.status.value}'. "
                    "Only payments in 'pending_review' status can be approved."
                )

            # Credits are derived from the package (if any) or the paid amount.
            _amount, credits = policies.resolve_topup_amount(
                payment.package_code, payment.amount_vnd
            )

            # P0-02: Lock wallet FOR UPDATE, then credit
            wallet = await self.wallets_repo.get_for_update(payment.user_id)
            if wallet is None:
                wallet = await self.wallets_repo.create(payment.user_id)

            await self.wallets_repo.apply_credit(wallet, credits)

            # P1-08: Record ledger entry with payment reference
            # The unique index uq_ledger_credit_per_payment ensures only one credit per payment
            await self.ledger_repo.append(
                user_id=payment.user_id,
                type=LedgerEntryType.credit,
                amount_vnd=credits,
                reference_type="payment",
                reference_id=str(payment.id),
            )

            # P0-02/P1-08: Mark paid with full audit trail
            await self.payments_repo.mark_paid_with_audit(
                payment,
                credited_vnd=credits,
                approved_by=admin_uid,
                approval_reason=reason,
            )

            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        return self._payment_to_dto(payment)

    async def reverse_payment(
        self,
        payment_id: uuid.UUID | str,
        admin_id: uuid.UUID | str,
        reason: str,
    ) -> dto.LedgerEntryDTO:
        """ADMIN: Create a reversal entry for a payment (P0-04).

        This creates a new ledger entry of type 'reversal' that offsets the
        original credit. It does NOT modify or delete the original entries.

        Idempotent: If a reversal already exists for this payment, returns
        the existing reversal entry instead of creating a duplicate.

        Args:
            payment_id: The payment to reverse
            admin_id: The admin performing the reversal
            reason: Required reason for the reversal (audit trail)

        Returns:
            The reversal ledger entry DTO
        """
        pid = _to_uuid(payment_id)
        admin_uid = _to_uuid(admin_id)

        if not reason or not reason.strip():
            raise ValidationErrorError("Reversal reason is required.")

        try:
            # Lock the payment to prevent concurrent reversals
            payment = await self.payments_repo.get_for_update(pid)
            if payment is None:
                raise ValidationErrorError("Payment not found.")

            # Can only reverse paid payments
            if payment.status != PaymentStatus.paid:
                raise ValidationErrorError(
                    f"Cannot reverse payment with status '{payment.status.value}'. "
                    "Only paid payments can be reversed."
                )

            # Check if reversal already exists (idempotency)
            existing_reversal = await self.ledger_repo.find_by_reference(
                user_id=payment.user_id,
                reference_type="reversal_payment",
                reference_id=str(payment.id),
            )
            if existing_reversal is not None:
                # Idempotent: return existing reversal
                return dto.LedgerEntryDTO(
                    id=str(existing_reversal.id),
                    type=existing_reversal.type.value,
                    amount_vnd=existing_reversal.amount_vnd,
                    reference_type=existing_reversal.reference_type,
                    reference_id=existing_reversal.reference_id,
                    created_at=existing_reversal.created_at,
                )

            # Get the original credit amount (use snapshot if available)
            credit_amount = payment.credited_vnd or payment.amount_vnd

            # Lock wallet and debit the reversal amount
            wallet = await self.wallets_repo.get_for_update(payment.user_id)
            if wallet is None:
                raise ValidationErrorError("User wallet not found.")

            # Check if wallet has sufficient balance for reversal
            if wallet.balance_vnd < credit_amount:
                raise BillingInsufficientError(
                    f"Insufficient balance for reversal. "
                    f"Wallet: {wallet.balance_vnd}, Reversal: {credit_amount}"
                )

            await self.wallets_repo.apply_debit(wallet, credit_amount)

            # Create reversal ledger entry
            # Note: reversal amount is negative (debit from user's perspective)
            reversal_entry = await self.ledger_repo.append(
                user_id=payment.user_id,
                type=LedgerEntryType.reversal,
                amount_vnd=-credit_amount,
                reference_type="reversal_payment",
                reference_id=str(payment.id),
            )

            await self.session.commit()

            return dto.LedgerEntryDTO(
                id=str(reversal_entry.id),
                type=reversal_entry.type.value,
                amount_vnd=reversal_entry.amount_vnd,
                reference_type=reversal_entry.reference_type,
                reference_id=reversal_entry.reference_id,
                created_at=reversal_entry.created_at,
            )
        except Exception:
            await self.session.rollback()
            raise

    @staticmethod
    def _build_qr_payload(amount_vnd: int, provider_ref: str) -> str:
        """Build a deterministic, human-readable v1 QR/transfer string.

        A real VietQR string is out of scope for v1; a stable string carrying
        the exact amount and the unique reference (used as the transfer memo)
        is sufficient for manual reconciliation.
        """
        return f"VIETQR|amount={amount_vnd}|memo={provider_ref}"

    def _payment_to_dto(self, payment) -> dto.PaymentDTO:
        """P0-01: qr_content (was qr_payload) per OpenAPI contract."""
        return dto.PaymentDTO(
            id=str(payment.id),
            package_code=payment.package_code,
            amount_vnd=payment.amount_vnd,
            status=payment.status.value
            if hasattr(payment.status, "value")
            else str(payment.status),
            provider_ref=payment.provider_ref,
            created_at=payment.created_at,
            paid_at=payment.paid_at,
            qr_content=None,
        )

    async def _require_owned_payment(
        self, user_id: uuid.UUID, payment_id: uuid.UUID | str
    ):
        """P1-04: Load a payment and enforce ownership, or raise NOT_FOUND."""
        try:
            pid = _to_uuid(payment_id)
        except ValueError:
            # P1-04: malformed UUID -> 404 (not 500)
            raise NotFoundError("Payment not found.")
        payment = await self.payments_repo.get_owned(pid, user_id)
        if payment is None:
            raise NotFoundError("Payment not found.")
        return payment


__all__ = [
    "PROVIDER",
    "InboxService",
    "MessageService",
    "BillingService",
    "PaymentService",
]
