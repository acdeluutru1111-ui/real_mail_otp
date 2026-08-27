"""Application configuration via pydantic-settings.

Loads all settings from environment variables (and an optional ``.env`` file).
Secrets (cookies, keys) are NEVER logged. Use :func:`get_settings` as the single
cached accessor throughout the app.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    All fields map 1:1 to environment variables (case-insensitive).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Service metadata ---------------------------------------------------
    service_name: str = Field(default="real-mail-otp-backend")
    service_version: str = Field(default="0.1.0")
    environment: str = Field(default="development")
    debug: bool = Field(default=False)

    # --- Database -----------------------------------------------------------
    # e.g. postgresql+asyncpg://user:pass@host:5432/dbname
    database_url: str = Field(default="")

    # --- Auth / crypto secrets ---------------------------------------------
    jwt_secret: str = Field(default="")
    jwt_algorithm: str = Field(default="HS256")
    jwt_access_ttl_seconds: int = Field(default=900)
    jwt_refresh_ttl_seconds: int = Field(default=1209600)
    # Fernet/AES key used to encrypt inbox key/address at rest.
    encryption_key: str = Field(default="")
    # JSON blob holding the single upstream cookie set. Treated as a secret;
    # never logged or returned to clients.
    smailpro_cookies: str = Field(default="{}")

    # --- CORS ---------------------------------------------------------------
    # Comma-separated list or JSON array of allowed origins.
    cors_origins: list[str] = Field(default_factory=list)

    # --- Rate limiting (in-process token bucket, v1) -----------------------
    rate_limit_create_per_minute: int = Field(default=30)
    rate_limit_list_per_minute: int = Field(default=120)
    rate_limit_detail_per_minute: int = Field(default=60)
    rate_limit_refresh_per_minute: int = Field(default=60)
    # Max active inboxes per user (fair-use).
    max_active_inboxes_per_user: int = Field(default=20)
    # Comma-separated list of user IDs granted admin privileges.
    admin_user_ids: str = Field(default="")
    # Global cap on concurrent upstream calls per replica.
    upstream_max_concurrency: int = Field(default=20)

    # --- Trusted proxies (P1-06) -------------------------------------------
    # Comma-separated list of IP addresses or CIDR ranges that are trusted to
    # set X-Forwarded-For. If empty, X-Forwarded-For is NOT trusted and
    # client.host is used directly. Example: "10.0.0.0/8,172.16.0.0/12"
    trusted_proxies: list[str] = Field(default_factory=list)

    # --- Upstream timeouts (seconds) ---------------------------------------
    upstream_timeout: float = Field(default=10.0)
    upstream_connect_timeout: float = Field(default=5.0)
    upstream_read_timeout: float = Field(default=10.0)
    upstream_max_retries: int = Field(default=2)

    # --- RAM cache TTLs (seconds) ------------------------------------------
    cache_list_ttl: float = Field(default=5.0)
    cache_list_negative_ttl: float = Field(default=3.0)
    cache_payload_ttl: float = Field(default=20.0)
    cache_detail_ttl: float = Field(default=180.0)

    # --- Billing ------------------------------------------------------------
    # Fixed price per successful message read, in VND.
    read_price_vnd: int = Field(default=200)

    # --- Gmail credentials for auto cookie refresh -------------------------
    # Required for automatic cookie refresh via magic link flow.
    gmail_email: str = Field(default="")
    gmail_app_password: str = Field(default="")
    cookie_auto_refresh_enabled: bool = Field(default=True)
    cookie_bootstrap_on_startup: bool = Field(default=False)
    cookie_persistence: str = Field(default="none")
    cookie_ttl_hours: float = Field(default=12.0, gt=0, le=168)
    cookie_refresh_cooldown_seconds: float = Field(default=30.0, ge=0, le=3600)
    cookie_refresh_max_wait_seconds: float = Field(default=60.0, gt=0, le=600)
    cookie_refresh_poll_interval_seconds: float = Field(default=5.0, gt=0, le=60)
    cookie_imap_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def _validate_cookie_refresh(self) -> "Settings":
        if self.cookie_persistence not in {"none", "legacy-read-only"}:
            raise ValueError("cookie_persistence must be 'none' or 'legacy-read-only'")
        if self.cookie_refresh_poll_interval_seconds > self.cookie_refresh_max_wait_seconds:
            raise ValueError("cookie refresh poll interval must not exceed max wait")
        return self

    # --- Feature flags / kill switches (P0-04) -----------------------------
    # Master switch for billing charges. When False, detail reads succeed but
    # do NOT charge the wallet (useful for incident response / testing).
    # Fail-safe: in production (environment != "development"), if critical
    # billing config is missing, this defaults to False.
    billing_charge_enabled: bool = Field(default=True)
    # Master switch for payment approval. When False, admin approve endpoint
    # returns 503 (service unavailable) instead of granting credit.
    payment_approval_enabled: bool = Field(default=True)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: Any) -> Any:
        """Accept a JSON array, a comma-separated string, or a list."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _parse_trusted_proxies(cls, value: Any) -> Any:
        """Accept a JSON array, a comma-separated string, or a list (P1-06)."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    def upstream_cookies(self) -> dict[str, Any]:
        """Parse the SMAILPRO_COOKIES JSON secret into a dict.

        Returns an empty dict on parse failure. Never log the result.
        """
        try:
            parsed = json.loads(self.smailpro_cookies or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton."""
    return Settings()


def is_billing_enabled() -> bool:
    """Return whether billing charges are enabled (fail-safe logic).

    In non-development environments, if critical billing config is missing
    (e.g., database_url, jwt_secret), this returns False to prevent accidental
    charges in a misconfigured production deployment.
    """
    settings = get_settings()
    # Fail-safe: in production, require critical config to be present
    if settings.environment != "development":
        if not settings.database_url or not settings.jwt_secret:
            return False
    return settings.billing_charge_enabled


def is_payment_approval_enabled() -> bool:
    """Return whether payment approval is enabled (fail-safe logic)."""
    settings = get_settings()
    # Fail-safe: in production, require critical config to be present
    if settings.environment != "development":
        if not settings.database_url or not settings.jwt_secret:
            return False
    return settings.payment_approval_enabled
