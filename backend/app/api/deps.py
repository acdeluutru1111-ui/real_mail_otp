import ipaddress
import uuid

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.db.models import User, UserStatus
from app.repositories.users_repo import UsersRepository
from app.core.security import decode_token
from app.core.errors import (
    AuthUnauthenticatedError,
    AuthForbiddenError,
    AbuseBlockedError,
)
from app.core.config import get_settings
from app.core.rate_limit import rate_limiter, InProcessRateLimiter, limits_for_route

# Cache parsed CIDR networks for trusted proxy check (P1-06)
_trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None

__all__ = [
    "get_current_user_id",
    "get_current_user",
    "client_ip",
    "require_admin",
    "rate_limit",
    "rate_limit_auth",
]


async def get_current_user_id(
    authorization: str | None = Header(default=None),
) -> str:
    if not authorization:
        raise AuthUnauthenticatedError()
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthUnauthenticatedError()
    token = parts[1].strip()
    claims = decode_token(token)
    sub = claims.get("sub")
    if not sub:
        raise AuthUnauthenticatedError()
    return sub


async def get_current_user(
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> User:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise AuthUnauthenticatedError()
    user = await UsersRepository(session).get(uid)
    if user is None:
        raise AuthUnauthenticatedError()
    if user.status != UserStatus.active:
        raise AuthForbiddenError()
    return user


def _get_trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse and cache trusted proxy networks from config (P1-06)."""
    global _trusted_networks
    if _trusted_networks is not None:
        return _trusted_networks

    settings = get_settings()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in settings.trusted_proxies:
        entry = entry.strip()
        if not entry:
            continue
        try:
            # Try parsing as a network (CIDR notation)
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            try:
                # Try parsing as a single IP address
                addr = ipaddress.ip_address(entry)
                # Convert to /32 or /128 network
                if isinstance(addr, ipaddress.IPv4Address):
                    networks.append(ipaddress.ip_network(f"{entry}/32"))
                else:
                    networks.append(ipaddress.ip_network(f"{entry}/128"))
            except ValueError:
                # Invalid entry, skip
                pass
    _trusted_networks = networks
    return _trusted_networks


def _is_trusted_proxy(ip_str: str) -> bool:
    """Check if an IP address is in the trusted proxy list (P1-06)."""
    networks = _get_trusted_networks()
    if not networks:
        return False
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in networks)
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """Extract the real client IP address (P1-06).

    Only trusts X-Forwarded-For if the direct connection is from a trusted proxy.
    If TRUSTED_PROXIES is empty, X-Forwarded-For is NOT trusted and client.host
    is used directly.
    """
    direct_ip = request.client.host if request.client else "unknown"

    # If no trusted proxies configured, never trust XFF
    if not _get_trusted_networks():
        return direct_ip

    # Only trust XFF if the direct connection is from a trusted proxy
    if not _is_trusted_proxy(direct_ip):
        return direct_ip

    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Return the leftmost (client) IP from the chain
        return xff.split(",")[0].strip()
    return direct_ip


async def require_admin(user: User = Depends(get_current_user)) -> User:
    settings = get_settings()
    allow = {
        s.strip()
        for s in (getattr(settings, "admin_user_ids", "") or "").split(",")
        if s.strip()
    }
    if str(user.id) not in allow:
        raise AuthForbiddenError()
    return user


def rate_limit(route: str):
    async def _dep(
        request: Request,
        user_id: str = Depends(get_current_user_id),
    ):
        ip = client_ip(request)
        key = InProcessRateLimiter.build_key(user_id, ip, route)
        capacity, refill = limits_for_route(route)
        result = await rate_limiter.check(key, capacity, refill)
        if not result.allowed:
            # P1-06: Attach retry_after to the exception for Retry-After header
            exc = AbuseBlockedError()
            exc.retry_after = result.retry_after  # type: ignore[attr-defined]
            raise exc
        return result

    return _dep


def rate_limit_auth(route: str):
    """Rate-limit dependency for unauthenticated auth endpoints.

    Auth endpoints (register/login/refresh) run BEFORE authentication, so they
    key the bucket on the client IP alone (there is no user id yet). They use
    intentionally lower per-minute limits sourced from config via
    :func:`limits_for_route`.
    """

    async def _dep(request: Request):
        ip = client_ip(request)
        key = InProcessRateLimiter.build_key("anon", ip, route)
        capacity, refill = limits_for_route(route)
        result = await rate_limiter.check(key, capacity, refill)
        if not result.allowed:
            # P1-06: Attach retry_after to the exception for Retry-After header
            exc = AbuseBlockedError()
            exc.retry_after = result.retry_after  # type: ignore[attr-defined]
            raise exc
        return result

    return _dep
