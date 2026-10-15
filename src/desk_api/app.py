"""FastAPI app factory + uvicorn server runner.

The server runs in the bot's asyncio loop as a background task — see
src.main._run for how it's spawned. Same loop → same DB session
factory, no extra connection pool or process boundary.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..db.session import Database
from .auth import require_bot_secret, require_desktop_identity
from .config import DeskApiSettings
from .routers.link import build_link_router
from .routers.pet import build_pet_router

log = logging.getLogger("akim-bot.desk-api")


def build_app(settings: DeskApiSettings, db: Database) -> FastAPI:
    app = FastAPI(
        title="akim-desk-pet API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # Electron loads pages from file:// or app:// — CORS for those is
    # opaque to the browser, so for the MVP we accept whatever the env
    # configures (defaults to "*"). Tighten in prod via env.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "x-bot-secret"],
    )

    @app.get("/api/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    bot_secret_dep = require_bot_secret(settings)
    desktop_auth_dep = require_desktop_identity(settings)
    app.include_router(
        build_link_router(settings, db.session, bot_secret_dep)
    )
    app.include_router(
        build_pet_router(settings, db.session, desktop_auth_dep)
    )
    return app


async def run_uvicorn_in_background(
    settings: DeskApiSettings, db: Database,
) -> asyncio.Task[None]:
    """Spawn uvicorn as an asyncio task and return it so the caller
    can cancel on shutdown. Uses uvicorn.Server programmatically so we
    don't fork a new process / event loop — the API shares the bot's
    loop and DB connection pool.
    """
    import uvicorn  # local import: uvicorn isn't needed when API is disabled

    app = build_app(settings, db)
    config = uvicorn.Config(
        app=app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        # We already configure the root logger; uvicorn's default access
        # log is noisy at /api/health poll cadence — disable.
        access_log=False,
        loop="asyncio",
        lifespan="off",
    )
    server = uvicorn.Server(config)

    async def _serve() -> None:
        try:
            await server.serve()
        except asyncio.CancelledError:
            # Clean shutdown when the bot is closing.
            server.should_exit = True
            with suppress(BaseException):
                await server.shutdown()
            raise

    log.info(
        "Starting desk-api on http://%s:%s (cors=%s)",
        settings.host, settings.port, ",".join(settings.cors_origins),
    )
    return asyncio.create_task(_serve(), name="desk-api-uvicorn")
