"""Environment knobs for the desktop API. Read once at startup.

Distinct from src.config.Settings (which loads the bot's own env) so a
deploy that doesn't want the API can leave these unset without crashing
load_settings(). The API is enabled iff DESKTOP_TOKEN_SECRET is set —
explicit opt-in, no default secret.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DeskApiSettings:
    enabled: bool
    host: str
    port: int
    token_secret: str
    bot_api_secret: str
    link_code_ttl_seconds: int
    desktop_token_ttl_days: int
    cors_origins: tuple[str, ...]


def _truthy(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


def load_desk_api_settings() -> DeskApiSettings:
    token_secret = os.environ.get("DESKTOP_TOKEN_SECRET", "").strip()
    bot_api_secret = os.environ.get("DISCORD_BOT_API_SECRET", "").strip()

    # API is disabled by default — must opt in with a real secret.
    # Bot secret can stay empty in dev (the slash command path doesn't
    # use it; only the public HTTP confirm endpoint requires it).
    enabled = bool(token_secret)

    host = os.environ.get("DESK_API_HOST", "0.0.0.0").strip() or "0.0.0.0"
    # Railway exposes PORT for the primary HTTP service; fall back to
    # DESK_API_PORT so the bot can run side-by-side with another web
    # service in a dev container.
    port_raw = os.environ.get("DESK_API_PORT", os.environ.get("PORT", "8080"))
    try:
        port = int(port_raw.strip()) if isinstance(port_raw, str) else 8080
    except ValueError:
        port = 8080

    link_ttl_raw = os.environ.get("LINK_CODE_EXPIRE_MINUTES", "10").strip()
    try:
        link_ttl_minutes = max(1, int(link_ttl_raw)) if link_ttl_raw else 10
    except ValueError:
        link_ttl_minutes = 10

    token_ttl_raw = os.environ.get("DESKTOP_TOKEN_EXPIRE_DAYS", "30").strip()
    try:
        token_ttl_days = max(1, int(token_ttl_raw)) if token_ttl_raw else 30
    except ValueError:
        token_ttl_days = 30

    cors_raw = os.environ.get("DESK_API_CORS_ORIGINS", "").strip()
    cors_origins = tuple(
        x.strip() for x in cors_raw.split(",") if x.strip()
    ) if cors_raw else ("*",)

    return DeskApiSettings(
        enabled=enabled,
        host=host,
        port=port,
        token_secret=token_secret,
        bot_api_secret=bot_api_secret,
        link_code_ttl_seconds=link_ttl_minutes * 60,
        desktop_token_ttl_days=token_ttl_days,
        cors_origins=cors_origins,
    )
