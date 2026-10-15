"""JWT helpers + FastAPI dependency for desktop bearer auth.

JWT payload is `{discordUserId, guildId, exp}` — the guild is baked
into the token at link time (user picks one guild per link per the
chosen architecture), so every authenticated endpoint already knows
the active guild context without a separate lookup.

We use PyJWT directly rather than fastapi-jwt-auth or similar — the
surface is tiny and one extra dep would only add config sprawl.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import DeskApiSettings

log = logging.getLogger("akim-bot.desk-api.auth")

# Single algorithm — HS256. Don't accept a header-supplied "alg" or we'd
# be vulnerable to the classic alg-confusion attack.
_JWT_ALG = "HS256"

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class DesktopIdentity:
    """Resolved request identity. Attached to the request via the
    `require_desktop_identity` dependency."""

    discord_user_id: int
    guild_id: int


def issue_token(
    settings: DeskApiSettings,
    *,
    discord_user_id: int,
    guild_id: int,
    issued_at: datetime | None = None,
) -> str:
    """Mint a JWT. When `issued_at` is supplied the token is fully
    deterministic for that timestamp — needed by the link flow, where
    /confirm and /status both have to mint the same JWT bytes so the
    stored hash matches on the /status side (otherwise every status
    poll would return 'consumed'). Defaults to now for other callers
    that don't need replay-equality."""
    base = issued_at or datetime.now(timezone.utc)
    exp = base + timedelta(days=settings.desktop_token_ttl_days)
    payload = {
        "sub": str(discord_user_id),
        "gid": str(guild_id),
        "iat": int(base.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.token_secret, algorithm=_JWT_ALG)


def decode_token(settings: DeskApiSettings, token: str) -> DesktopIdentity:
    try:
        payload = jwt.decode(
            token,
            settings.token_secret,
            algorithms=[_JWT_ALG],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": True, "code": "LINK_EXPIRED", "message": "Token expired."},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": True, "code": "UNAUTHORIZED", "message": "Invalid token."},
        )

    try:
        return DesktopIdentity(
            discord_user_id=int(payload["sub"]),
            guild_id=int(payload["gid"]),
        )
    except (KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": True, "code": "UNAUTHORIZED", "message": "Invalid token payload."},
        )


def hash_token(token: str) -> str:
    """SHA-256 hex of the issued JWT — stored on the link session so a
    later attacker can't recover the token from the row."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- Link-code generation ---------------------------------------------------

# Crockford base32 minus the visually ambiguous I/L/O/U so codes are
# safer to read aloud and type. 28 chars × 6 positions = ~480M codes,
# more than enough for a 10-minute TTL.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"


def generate_link_code() -> str:
    """Return a fresh 6-char link code with the AKIM- prefix."""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
    return f"AKIM-{body}"


# --- Bot-secret dependency --------------------------------------------------


def require_bot_secret(settings: DeskApiSettings):
    """Factory for the FastAPI dependency that checks the x-bot-secret
    header. Must wrap settings via closure because dependencies can't
    capture them via Depends() without an extra indirection layer."""

    def _checker(x_bot_secret: str | None = Header(default=None)) -> None:
        if not settings.bot_api_secret:
            # The bot-secret header guards the confirm endpoint against
            # forgery from outside the bot. If no secret is configured
            # we refuse rather than silently allowing — easier to spot
            # the misconfig in dev than to ship a wide-open endpoint.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": True,
                    "code": "BOT_SECRET_NOT_CONFIGURED",
                    "message": "DISCORD_BOT_API_SECRET is not set on the API.",
                },
            )
        if not x_bot_secret or not secrets.compare_digest(
            x_bot_secret, settings.bot_api_secret
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": True,
                    "code": "UNAUTHORIZED",
                    "message": "Invalid bot secret.",
                },
            )

    return _checker


# --- Desktop bearer dependency ----------------------------------------------


def require_desktop_identity(settings: DeskApiSettings):
    """Factory for the bearer auth dependency. Returns DesktopIdentity."""

    def _checker(
        creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> DesktopIdentity:
        if creds is None or creds.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": True,
                    "code": "UNAUTHORIZED",
                    "message": "Missing bearer token.",
                },
            )
        return decode_token(settings, creds.credentials)

    return _checker
