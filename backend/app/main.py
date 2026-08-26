"""FastAPI application entrypoint.

Boot with::

    uvicorn app.main:app

Wires up structured logging, request-id middleware, CORS, the common error
handler, health endpoints, and the (initially empty) v1 API router.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.context import RequestIdMiddleware
from app.core.errors import AppError, app_error_handler, unhandled_exception_handler
from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)

# Timeout for DB health check (seconds)
_DB_HEALTH_CHECK_TIMEOUT = 2.0


def create_app() -> FastAPI:
    """Application factory."""
    settings: Settings = get_settings()
    setup_logging(level="DEBUG" if settings.debug else "INFO")

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # --- Middleware ---------------------------------------------------------
    app.add_middleware(RequestIdMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )

    # --- Exception handlers -------------------------------------------------
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # --- Health endpoints ---------------------------------------------------
    @app.get("/health/live", tags=["health"])
    async def health_live() -> JSONResponse:
        """Liveness probe: always 200 if the process is running.

        Does NOT depend on DB or any external service (P1-05).
        """
        return JSONResponse(status_code=200, content={"status": "live"})

    @app.get("/health/ready", tags=["health"])
    async def health_ready() -> JSONResponse:
        """Readiness probe: checks DB connectivity with SELECT 1 (P1-05).

        - Runs SELECT 1 with a short timeout to verify DB is actually available.
        - Does NOT check upstream services (SmailPro/Sonjj).
        - Returns 503 if DB is unavailable or config is missing.
        """
        from app.db.session import get_sessionmaker

        checks: dict[str, bool | str] = {
            "database_url": bool(settings.database_url),
            "jwt_secret": bool(settings.jwt_secret),
            "database_connection": False,
        }

        # Config check first
        if not settings.database_url or not settings.jwt_secret:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "checks": checks},
            )

        # DB connectivity check with timeout (P1-05)
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                # Use asyncio.wait_for for timeout
                await asyncio.wait_for(
                    session.execute(text("SELECT 1")),
                    timeout=_DB_HEALTH_CHECK_TIMEOUT,
                )
                checks["database_connection"] = True
        except asyncio.TimeoutError:
            checks["database_connection"] = "timeout"
            logger.warning("health/ready: DB check timed out")
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "checks": checks},
            )
        except Exception as exc:
            checks["database_connection"] = "error"
            logger.warning("health/ready: DB check failed", extra={"error": str(exc)})
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "checks": checks},
            )

        return JSONResponse(
            status_code=200,
            content={"status": "ready", "checks": checks},
        )

    # --- v1 API router (fail-fast: must not boot without /v1) ---------------
    try:
        from app.api.routes.api_v1 import api_router

        app.include_router(api_router)
    except Exception:
        # Do NOT swallow: booting without /v1 is a broken app. Log and re-raise.
        logger.error("v1 API router failed to import; aborting boot", exc_info=True)
        raise

    # --- DB engine lifecycle (Step 2) --------------------------------------
    @app.on_event("startup")
    async def _on_startup() -> None:
        from app.db.session import init_engine

        if settings.database_url:
            await init_engine()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        from app.db.session import dispose_engine
        from app.integrations.http_client import close_client

        # P1-05: Close HTTP client cleanly to avoid connection leaks
        await close_client()
        await dispose_engine()

    return app


app = create_app()
