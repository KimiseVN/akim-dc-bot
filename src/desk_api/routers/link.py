"""Link-code routes — create/status/confirm.

Confirm is bot-secret-gated for SPEC compliance and would be the path
a separate Node process or a worker uses to attach an identity. In
the in-process arch the slash command calls link_service directly
without going through HTTP.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ..config import DeskApiSettings
from ..link_service import (
    LinkError,
    confirm_link_session,
    create_link_session,
    fetch_status,
)
from ..schemas import (
    ApiError,
    LinkConfirmRequest,
    LinkConfirmResponse,
    LinkCreateResponse,
    LinkStatusResponse,
)

log = logging.getLogger("akim-bot.desk-api.routers.link")


def build_link_router(
    settings: DeskApiSettings, db_session_factory, bot_secret_dependency,
) -> APIRouter:
    """Build the link router with closures over runtime deps. We pass
    them in rather than reaching for module-level globals so tests can
    spin up an isolated app + DB if needed later."""
    router = APIRouter(prefix="/api/desktop/link", tags=["desktop-link"])

    @router.post(
        "/create",
        response_model=LinkCreateResponse,
        responses={500: {"model": ApiError}},
    )
    async def create() -> LinkCreateResponse:
        async with db_session_factory() as s:
            try:
                created = await create_link_session(s, settings)
            except LinkError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"error": True, "code": e.code, "message": e.message},
                )
        return LinkCreateResponse(code=created.code, expires_at=created.expires_at)

    @router.get(
        "/status",
        response_model=LinkStatusResponse,
        responses={400: {"model": ApiError}},
    )
    async def get_status(code: str) -> LinkStatusResponse:
        if not code or len(code) > 32:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": True,
                    "code": "VALIDATION_ERROR",
                    "message": "Missing or invalid `code`.",
                },
            )
        async with db_session_factory() as s:
            state, delivered = await fetch_status(s, settings, code=code)
        return LinkStatusResponse(
            status=state,
            access_token=delivered.access_token if delivered else None,
            linked_at=delivered.linked_at if delivered else None,
        )

    @router.post(
        "/confirm",
        response_model=LinkConfirmResponse,
        responses={
            401: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
            410: {"model": ApiError},
            503: {"model": ApiError},
        },
        dependencies=[Depends(bot_secret_dependency)],
    )
    async def confirm(payload: LinkConfirmRequest) -> LinkConfirmResponse:
        try:
            discord_user_id = int(payload.discord_user_id)
            guild_id = int(payload.guild_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": True,
                    "code": "VALIDATION_ERROR",
                    "message": "discordUserId and guildId must be numeric.",
                },
            )
        async with db_session_factory() as s:
            try:
                confirmed = await confirm_link_session(
                    s, settings,
                    code=payload.code,
                    discord_user_id=discord_user_id,
                    guild_id=guild_id,
                )
            except LinkError as e:
                # Map the link error code to a sensible HTTP status so
                # the bot gets actionable feedback rather than a generic
                # 500. Default to 400 for anything unmapped.
                http_status = {
                    "LINK_NOT_FOUND": status.HTTP_404_NOT_FOUND,
                    "LINK_EXPIRED": status.HTTP_410_GONE,
                    "LINK_ALREADY_USED": status.HTTP_409_CONFLICT,
                }.get(e.code, status.HTTP_400_BAD_REQUEST)
                raise HTTPException(
                    status_code=http_status,
                    detail={"error": True, "code": e.code, "message": e.message},
                )
        return LinkConfirmResponse(
            code=confirmed.code,
            discord_user_id=str(confirmed.discord_user_id),
            guild_id=str(confirmed.guild_id),
        )

    return router
