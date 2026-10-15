from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


_PROD_ENV_HINTS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "FLY_APP_NAME",
    "RENDER",
    "KUBERNETES_SERVICE_HOST",
)
_TRUTHY = {"1", "true", "yes", "on"}


def _is_production() -> bool:
    return any(os.environ.get(name) for name in _PROD_ENV_HINTS)


def _normalize_db_url(url: str) -> str:
    # Railway gives postgres:// but SQLAlchemy 2.x wants postgresql+asyncpg://
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_app_id: int | None
    database_url: str
    dev_guild_ids: tuple[int, ...]
    # Guild-ID whitelist for private bots. When non-empty the bot:
    #   • leaves any guild it starts in that isn't on the list, and
    #   • auto-leaves any guild that invites it later.
    # Empty tuple (default) = no enforcement — bot accepts every guild
    # it's invited into, matching legacy behavior. Kept as an opt-in
    # so an accidentally-blank env var can't strand the bot.
    allowed_guild_ids: tuple[int, ...]
    # Bot-level super-users: Discord user IDs that have the same
    # permissions as the Discord server owner in every guild this bot
    # is in.  Set via SUPER_USER_IDS (comma-separated).  Empty = nobody
    # extra has owner-level access (default / safest).
    super_user_ids: tuple[int, ...]


def load_settings() -> Settings:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required")

    app_id_raw = os.environ.get("DISCORD_APP_ID", "").strip()
    app_id = int(app_id_raw) if app_id_raw else None

    # Production guard: SQLite inside a container has no volume by default,
    # so every redeploy wipes the file. Refuse to start unless explicitly
    # opted in with ALLOW_SQLITE_IN_PROD=true.
    raw_db_url = os.environ.get("DATABASE_URL", "").strip()
    allow_sqlite_in_prod = (
        os.environ.get("ALLOW_SQLITE_IN_PROD", "").strip().lower() in _TRUTHY
    )
    in_prod = _is_production()

    if not raw_db_url:
        if in_prod and not allow_sqlite_in_prod:
            raise RuntimeError(
                "DATABASE_URL is not set on this production deploy. "
                "Reference Postgres.DATABASE_URL in this service's Variables, "
                "or set ALLOW_SQLITE_IN_PROD=true to opt out (data will be "
                "wiped on every redeploy)."
            )
        raw_db_url = "sqlite+aiosqlite:///./bot.db"

    db_url = _normalize_db_url(raw_db_url)

    if in_prod and db_url.startswith("sqlite") and not allow_sqlite_in_prod:
        raise RuntimeError(
            "DATABASE_URL points at SQLite on a production environment. "
            "SQLite inside a container has no persistent volume — data is "
            "lost on every redeploy. Reference Postgres.DATABASE_URL "
            "instead, or set ALLOW_SQLITE_IN_PROD=true to opt in explicitly."
        )

    dev_ids_raw = os.environ.get("DEV_GUILD_IDS", "").strip()
    dev_ids = tuple(
        int(x) for x in dev_ids_raw.replace(";", ",").split(",") if x.strip()
    )

    allowed_ids_raw = os.environ.get("ALLOWED_GUILD_IDS", "").strip()
    allowed_ids = tuple(
        int(x)
        for x in allowed_ids_raw.replace(";", ",").split(",")
        if x.strip()
    )

    super_ids_raw = os.environ.get("SUPER_USER_IDS", "").strip()
    super_ids = tuple(
        int(x)
        for x in super_ids_raw.replace(";", ",").split(",")
        if x.strip()
    )

    return Settings(
        discord_token=token,
        discord_app_id=app_id,
        database_url=db_url,
        dev_guild_ids=dev_ids,
        allowed_guild_ids=allowed_ids,
        super_user_ids=super_ids,
    )
