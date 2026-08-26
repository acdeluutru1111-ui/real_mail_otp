"""Security primitives: JWT tokens, password hashing, and value encryption.

This module centralises all cryptographic operations for the service:

* Access/refresh JWTs signed with the shared ``jwt_secret`` (HS256).
* Password hashing via bcrypt (passlib).
* Symmetric encryption of sensitive values at rest via Fernet, keyed by a
  SHA-256 digest of ``encryption_key``.
* Deterministic address hashing for lookup/dedup.

Plaintext values, secrets, tokens and keys are NEVER logged.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError
from passlib.context import CryptContext

from app.core.config import get_settings
from app.core.errors import AuthTokenExpiredError, AuthUnauthenticatedError

# P2-03: Use jwt_algorithm from config instead of hardcoding
# Note: We keep HS256 as the default and only supported algorithm for now.
# The config field exists for documentation but changing it is not recommended
# without also updating the token verification logic.
JWT_ALGO = "HS256"  # Default algorithm, config.jwt_algorithm should match

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# --- JWT tokens -------------------------------------------------------------
def create_access_token(subject: str, extra_claims: dict | None = None) -> str:
    """Create a signed short-lived access token for ``subject``."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    claims: dict = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_access_ttl_seconds),
        "type": "access",
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, settings.jwt_secret, algorithm=JWT_ALGO)


def create_refresh_token(
    subject: str,
    jti: str | None = None,
    family_id: str | None = None,
) -> tuple[str, str, str, datetime]:
    """Create a signed long-lived refresh token for ``subject`` (P1-02).

    Returns:
        tuple: (token, jti, family_id, expires_at)
            - token: The encoded JWT refresh token
            - jti: The unique JWT ID for this token
            - family_id: The family ID for token rotation tracking
            - expires_at: When the token expires
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.jwt_refresh_ttl_seconds)

    # Generate jti and family_id if not provided
    if jti is None:
        jti = uuid.uuid4().hex
    if family_id is None:
        family_id = uuid.uuid4().hex

    claims: dict = {
        "sub": subject,
        "iat": now,
        "exp": expires_at,
        "type": "refresh",
        "jti": jti,
        "fid": family_id,  # family_id for rotation tracking
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=JWT_ALGO)
    return token, jti, family_id, expires_at


def hash_token(token: str) -> str:
    """Create a SHA-256 hash of a token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def decode_token(token: str) -> dict:
    """Decode and verify a JWT, returning its claims.

    Raises :class:`AuthTokenExpiredError` if the token has expired and
    :class:`AuthUnauthenticatedError` for any other validation failure.
    """
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGO])
    except ExpiredSignatureError as exc:
        raise AuthTokenExpiredError() from exc
    except JWTError as exc:
        raise AuthUnauthenticatedError() from exc


# --- Password hashing -------------------------------------------------------
def hash_password(pw: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return _pwd_context.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return _pwd_context.verify(pw, hashed)


# --- Symmetric value encryption --------------------------------------------
def _fernet() -> Fernet:
    """Build a Fernet instance from the configured ``encryption_key``."""
    settings = get_settings()
    digest = hashlib.sha256(settings.encryption_key.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string, returning a Fernet token."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(token: str) -> str:
    """Decrypt a Fernet token back into its plaintext string."""
    return _fernet().decrypt(token.encode()).decode()


# --- Deterministic hashing --------------------------------------------------
def address_hash(value: str) -> str:
    """Return a deterministic SHA-256 hex digest of ``value``."""
    return hashlib.sha256(value.encode()).hexdigest()
