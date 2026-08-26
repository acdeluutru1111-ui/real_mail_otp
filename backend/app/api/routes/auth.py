"""Auth routes — register / login / refresh / logout (credential auth).

Thin HTTP adapters over the security + repository layers. These endpoints run
BEFORE authentication, so they are throttled by IP via ``rate_limit_auth``.

Security invariants:
- Passwords and tokens are NEVER logged.
- bcrypt silently truncates input beyond 72 bytes, so a password longer than
  72 bytes is rejected up front with a VALIDATION_ERROR rather than being
  quietly truncated.
- Login never reveals whether the email exists (single AUTH_UNAUTHENTICATED for
  both unknown-email and wrong-password).

P1-02: Refresh token rotation with reuse detection:
- Each refresh token has a unique jti and belongs to a family.
- On refresh, the old token is revoked and a new one is issued in the same family.
- If a revoked token is reused (replay attack), the entire family is revoked.

The router is self-contained and importable without coupling to the app object;
it is mounted onto the v1 router in a later step.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import rate_limit_auth
from app.core import security
from app.core.errors import (
    AuthForbiddenError,
    AuthUnauthenticatedError,
    BillingConflictError,
    ValidationErrorError,
)
from app.db.models import UserStatus
from app.db.session import get_session
from app.repositories.refresh_token_repo import RefreshTokenRepository
from app.repositories.users_repo import UsersRepository
from app.repositories.wallets_repo import WalletsRepository

router = APIRouter(prefix="/auth", tags=["auth"])

# bcrypt only considers the first 72 bytes of the password; anything longer is
# silently truncated. Reject up front so two distinct long passwords can never
# collapse to the same hash.
_BCRYPT_MAX_PASSWORD_BYTES = 72


# --- Request / response models ---------------------------------------------
class RegisterBody(BaseModel):
    """Registration payload."""

    email: EmailStr
    password: str


class LoginBody(BaseModel):
    """Login payload."""

    email: EmailStr
    password: str


class RefreshBody(BaseModel):
    """Refresh payload carrying a previously issued refresh token."""

    refresh_token: str


class LogoutBody(BaseModel):
    """Logout payload carrying the refresh token to revoke."""

    refresh_token: str


class TokenPair(BaseModel):
    """Issued access + refresh token pair."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LogoutResponse(BaseModel):
    """Response for logout endpoint."""

    message: str = "Logged out successfully"


async def _issue_tokens_with_db(
    user_id: str,
    session: AsyncSession,
    family_id: str | None = None,
) -> TokenPair:
    """Mint a fresh access + refresh token pair and store refresh token in DB (P1-02)."""
    # Create refresh token with jti and family_id
    refresh_token, jti, fid, expires_at = security.create_refresh_token(
        subject=user_id,
        family_id=family_id,
    )

    # Store refresh token hash in DB
    token_hash = security.hash_token(refresh_token)
    repo = RefreshTokenRepository(session)
    await repo.create(
        user_id=uuid.UUID(user_id),
        jti=jti,
        token_hash=token_hash,
        family_id=fid,
        expires_at=expires_at,
    )

    return TokenPair(
        access_token=security.create_access_token(subject=user_id),
        refresh_token=refresh_token,
    )


def _ensure_password_length(password: str) -> None:
    """Reject passwords longer than bcrypt's 72-byte limit."""
    if len(password.encode("utf-8")) > _BCRYPT_MAX_PASSWORD_BYTES:
        raise ValidationErrorError("Password is too long.")


@router.post("/register", response_model=TokenPair, status_code=201)
async def register(
    body: RegisterBody,
    _rl=Depends(rate_limit_auth("create")),
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    """Create a user + zero-balance wallet and return a token pair."""
    _ensure_password_length(body.password)

    email = str(body.email).strip().lower()
    users = UsersRepository(session)

    # Pre-check keeps the common duplicate case as a clean 409; the DB UNIQUE
    # index on email remains the ultimate backstop for a race.
    if await users.get_by_email(email) is not None:
        raise BillingConflictError("Email is already registered.")

    password_hash = security.hash_password(body.password)
    user = await users.create(email=email, password_hash=password_hash)
    await WalletsRepository(session).create(user.id, balance_vnd=0)

    # Issue tokens with DB storage for refresh token
    tokens = await _issue_tokens_with_db(str(user.id), session)

    # get_session commits the transaction on success.
    return tokens


@router.post("/login", response_model=TokenPair)
async def login(
    body: LoginBody,
    _rl=Depends(rate_limit_auth("create")),
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    """Verify credentials and return a token pair on success.

    P1-02: Check user status before issuing tokens. Only active users can login.
    """
    email = str(body.email).strip().lower()
    user = await UsersRepository(session).get_by_email(email)

    # Do NOT reveal whether the email exists: same error for both branches.
    if user is None or not security.verify_password(
        body.password, user.password_hash
    ):
        raise AuthUnauthenticatedError()

    # P1-02: Check user status - only active users can login
    if user.status != UserStatus.active:
        raise AuthForbiddenError("Account is not active.")

    # Issue tokens with DB storage for refresh token
    return await _issue_tokens_with_db(str(user.id), session)


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshBody,
    _rl=Depends(rate_limit_auth("refresh")),
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    """Exchange a valid refresh token for a new access + refresh token pair.

    P1-02: Implements refresh token rotation with reuse detection:
    - Validates the token and checks it's not revoked
    - If token is revoked (reuse detected), revokes entire family and returns 401
    - If valid, revokes old token and issues new one in same family

    ``decode_token`` raises AUTH_TOKEN_EXPIRED / AUTH_UNAUTHENTICATED (both 401)
    for expired / invalid tokens; those propagate to the global handler.
    """
    claims = security.decode_token(body.refresh_token)
    if claims.get("type") != "refresh":
        raise AuthUnauthenticatedError()

    subject = claims.get("sub")
    jti = claims.get("jti")
    family_id = claims.get("fid")

    if not subject or not jti:
        raise AuthUnauthenticatedError()

    # P1-02: Check if token exists and is not revoked
    repo = RefreshTokenRepository(session)
    token_record = await repo.find_by_jti(jti)

    if token_record is None:
        # Token not in DB - could be old token before P1-02 migration
        # For backward compatibility, issue new tokens with new family
        return await _issue_tokens_with_db(str(subject), session)

    # P1-02: Reuse detection - if token is already revoked, revoke entire family
    if token_record.revoked_at is not None:
        # Token reuse detected! Revoke entire family (security breach)
        if family_id:
            await repo.revoke_family(family_id)
        await session.commit()
        raise AuthUnauthenticatedError("Token has been revoked.")

    # P1-02: Load user and check status
    users_repo = UsersRepository(session)
    user = await users_repo.get(token_record.user_id)
    if user is None or user.status != UserStatus.active:
        # Revoke the token since user is no longer valid
        await repo.revoke_by_jti(jti)
        await session.commit()
        raise AuthForbiddenError("Account is not active.")

    # P1-02: Revoke old token
    await repo.revoke_by_jti(jti)

    # P1-02: Issue new tokens in the same family (rotation)
    tokens = await _issue_tokens_with_db(
        str(subject),
        session,
        family_id=family_id,  # Keep same family for rotation tracking
    )

    await session.commit()
    return tokens


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    body: LogoutBody,
    _rl=Depends(rate_limit_auth("refresh")),
    session: AsyncSession = Depends(get_session),
) -> LogoutResponse:
    """Revoke the provided refresh token (P1-02).

    This endpoint allows users to explicitly logout by revoking their refresh token.
    The access token will remain valid until it expires (short-lived).
    """
    try:
        claims = security.decode_token(body.refresh_token)
    except Exception:
        # Even if token is invalid/expired, return success (idempotent logout)
        return LogoutResponse()

    jti = claims.get("jti")
    if jti:
        repo = RefreshTokenRepository(session)
        await repo.revoke_by_jti(jti)
        await session.commit()

    return LogoutResponse()
