"""Shared link-session lifecycle. Both the HTTP confirm endpoint and
the /link-desktop slash command go through these helpers so the bot
doesn't have to make an HTTP round-trip to itself.

State machine for a DesktopLinkSession row:

    create_link_session()  →  status='pending'   (discord_user_id=NULL)
    confirm_link_session() →  status='linked'    (discord_user_id+guild_id set, token hash stored)
    deliver_token()        →  token returned once, hash cleared

`status` is computed from columns rather than persisted as a string —
that's the only way to keep things consistent with concurrent confirm
+ poll without locking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import DesktopLinkSession
from .auth import generate_link_code, hash_token, issue_token
from .config import DeskApiSettings

log = logging.getLogger("akim-bot.desk-api.link")


class LinkError(Exception):
    """Generic link-flow error with a stable error code for the API
    layer to surface to clients. HTTP status mapping happens in the
    router so the slash command can use the same exception without
    pulling in HTTP imports."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CreatedSession:
    code: str
    expires_at: datetime


@dataclass(frozen=True)
class ConfirmedSession:
    code: str
    discord_user_id: int
    guild_id: int


@dataclass(frozen=True)
class DeliveredToken:
    access_token: str
    linked_at: datetime


async def create_link_session(
    session: AsyncSession, settings: DeskApiSettings,
) -> CreatedSession:
    """Generate a fresh code + expiry and persist it. Retries on the
    astronomically unlikely event that the random code collides with
    an existing row."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.link_code_ttl_seconds)
    # Retry up to a few times in case of a code collision. With ~480M
    # possibilities and link sessions cleared in minutes, collision
    # rate is effectively zero — this is paranoia.
    for _ in range(5):
        code = generate_link_code()
        existing = await session.scalar(
            select(DesktopLinkSession).where(DesktopLinkSession.code == code)
        )
        if existing is None:
            # Explicitly NULL the nullable columns so a stray column-
            # default in the schema (legacy column from an earlier
            # migration we forgot, or a Postgres trigger) can't sneak
            # a value in. Defensive — the model defaults are right,
            # but a session that gets "consumed" right after create
            # means SOMETHING is putting a value in discord_user_id
            # before /status fires, and ruling that out cheap.
            row = DesktopLinkSession(
                code=code,
                expires_at=expires_at,
                discord_user_id=None,
                guild_id=None,
                access_token_hash=None,
                token_delivered_at=None,
                linked_at=None,
            )
            session.add(row)
            await session.commit()
            return CreatedSession(code=code, expires_at=expires_at)
    # If we genuinely failed 5 times something is very wrong — surface
    # it loudly rather than silently looping forever.
    raise LinkError(
        "INTERNAL_ERROR", "Failed to generate a unique link code."
    )


async def confirm_link_session(
    session: AsyncSession,
    settings: DeskApiSettings,
    *,
    code: str,
    discord_user_id: int,
    guild_id: int,
) -> ConfirmedSession:
    """Mark a pending code as linked to a specific user + guild and
    pre-compute the JWT hash so the next /status poll can hand the
    token out exactly once.

    Idempotent for the SAME user + guild — re-confirming after a
    successful link is a no-op so a flaky slash command retry doesn't
    invalidate the desktop's pending token fetch.
    """
    now = datetime.now(timezone.utc)
    row = await session.scalar(
        select(DesktopLinkSession).where(DesktopLinkSession.code == code)
    )
    if row is None:
        raise LinkError("LINK_NOT_FOUND", "Link code doesn't exist.")
    if row.expires_at <= now:
        raise LinkError("LINK_EXPIRED", "Link code has expired.")
    if row.discord_user_id is not None:
        # Already linked. Allow only same user re-confirming the same
        # guild — otherwise reject so a stale code can't be hijacked.
        if (
            row.discord_user_id == discord_user_id
            and row.guild_id == guild_id
        ):
            return ConfirmedSession(
                code=row.code,
                discord_user_id=discord_user_id,
                guild_id=guild_id,
            )
        raise LinkError(
            "LINK_ALREADY_USED",
            "Link code was already used by another account.",
        )

    # First confirm — issue a deterministic token (iat = now, also
    # persisted as linked_at) so a later /status poll can mint a JWT
    # with the same bytes and verify the hash. Without iat = linked_at
    # the two mints would have different timestamps → different JWTs
    # → hash mismatch → /status would always report consumed.
    token = issue_token(
        settings,
        discord_user_id=discord_user_id,
        guild_id=guild_id,
        issued_at=now,
    )
    row.discord_user_id = discord_user_id
    row.guild_id = guild_id
    row.access_token_hash = hash_token(token)
    row.linked_at = now
    # token_delivered_at stays NULL — it gets set when the desktop
    # picks up the token via /status. We deliberately don't return
    # the token here because confirm_link_session is called from the
    # bot side (slash command), and the bot doesn't need the token.
    await session.commit()
    return ConfirmedSession(
        code=code,
        discord_user_id=discord_user_id,
        guild_id=guild_id,
    )


async def fetch_status(
    session: AsyncSession, settings: DeskApiSettings, *, code: str,
) -> tuple[str, DeliveredToken | None]:
    """Status poll — returns ('pending'|'linked'|'expired'|'consumed',
    token-or-None). Token is included exactly once on the first poll
    after confirm; subsequent polls return ('linked', None)."""
    now = datetime.now(timezone.utc)
    row = await session.scalar(
        select(DesktopLinkSession).where(DesktopLinkSession.code == code)
    )
    if row is None:
        return ("expired", None)
    if row.discord_user_id is None:
        if row.expires_at <= now:
            return ("expired", None)
        return ("pending", None)
    # Already linked.
    if row.token_delivered_at is not None:
        # Token was handed out previously. Tell desktop the session is
        # done; if they lost the token, they must re-link.
        return ("consumed", None)
    if row.access_token_hash is None:
        # Defensive: somehow we have linked_at but no hash. Treat as
        # consumed rather than crashing.
        return ("consumed", None)
    # First poll after confirm — re-mint the SAME JWT bytes (iat =
    # row.linked_at so the timestamp matches what confirm used) and
    # verify the hash. A mismatch only happens when token_secret has
    # rotated between confirm and poll; otherwise the deterministic
    # mint guarantees they match.
    token = issue_token(
        settings,
        discord_user_id=row.discord_user_id,
        guild_id=row.guild_id or 0,
        issued_at=row.linked_at,
    )
    if hash_token(token) != row.access_token_hash:
        log.warning(
            "status %s: hash mismatch (secret rotated?). Forcing re-link.",
            code,
        )
        return ("consumed", None)
    row.token_delivered_at = now
    # Clear the hash so a replay of /status doesn't re-hand it out.
    row.access_token_hash = None
    await session.commit()
    return (
        "linked",
        DeliveredToken(access_token=token, linked_at=row.linked_at or now),
    )
