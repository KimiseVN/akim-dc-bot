from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

log = logging.getLogger("akim-bot.db")


# === Schema-change policy ====================================================
# init_models() MUST be safe to run on every bot start without losing data.
# Allowed operations on existing schema:
#   - Base.metadata.create_all (additive — only creates missing tables/columns
#     defined in models that don't yet exist)
#   - ALTER TABLE ... ADD COLUMN IF NOT EXISTS (via _INCREMENTAL_COLUMNS)
#   - UPDATE / backfill statements (data migrations)
#
# Forbidden — do NOT add any of these to this module:
#   - Base.metadata.drop_all
#   - DROP TABLE, DROP COLUMN, TRUNCATE
#   - ALTER COLUMN that changes type in a non-widening way
#   - Renames (do "add new, backfill, deprecate old" across releases)
#
# To add a column to an existing table, just declare it on the model AND
# add a row to _INCREMENTAL_COLUMNS so older deploys pick it up next start.
# For more invasive changes, do them across two releases (add-then-drop) so
# rollback never wipes a column that still has live data.
# ============================================================================


# Lightweight schema patches applied after create_all. Add an entry for each
# column added to an existing table so older deployments don't need a manual
# migration. SQL type is in PostgreSQL form; SQLite gets a translation pass.
# Format: (table_name, column_name, column_type_sql)
_INCREMENTAL_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("lfg_parties", "voice_channel_id", "BIGINT"),
    ("guild_configs", "lfg_role_ids", "VARCHAR(512)"),
    # /baodanh approval flow
    ("players", "is_guild_member", "BOOLEAN DEFAULT FALSE"),
    ("players", "approval_status", "VARCHAR(16) DEFAULT 'approved'"),
    ("players", "approved_by", "BIGINT"),
    ("players", "approved_role_id", "BIGINT"),
    ("players", "approved_at", "TIMESTAMP WITH TIME ZONE"),
    ("players", "pending_channel_id", "BIGINT"),
    ("players", "pending_message_id", "BIGINT"),
    ("guild_configs", "unverified_role_id", "BIGINT"),
    ("guild_configs", "approval_channel_id", "BIGINT"),
    ("guild_configs", "approver_role_ids", "VARCHAR(512)"),
    # /setup-baodanh — owner-controlled approval requirement.
    #   NULL/TRUE → legacy flow (post pending card, wait for approver).
    #   FALSE     → Save is auto-approved; grant baodanh_auto_role_id.
    ("guild_configs", "baodanh_require_approval", "BOOLEAN"),
    ("guild_configs", "baodanh_auto_role_id", "BIGINT"),
    # NULL/TRUE → auto-rename on save (legacy). FALSE → skip nickname sync.
    ("guild_configs", "auto_rename_enabled", "BOOLEAN"),
    ("guild_configs", "guild_name", "VARCHAR(64)"),
    # In-game server selection on /baodanh
    ("players", "server_id", "BIGINT"),
    # /sync from WWM Workshop embed
    ("players", "level", "INTEGER"),
    ("players", "elegance", "INTEGER"),
    ("players", "last_synced_at", "TIMESTAMP WITH TIME ZONE"),
    ("guild_configs", "sync_source_bot_id", "BIGINT"),
    ("guild_configs", "quiz_channel_id", "BIGINT"),
    ("guild_configs", "quiz_announce_channel_id", "BIGINT"),
    ("guild_configs", "quiz_winner_role_id", "BIGINT"),
    ("guild_configs", "quiz_wins_for_role", "INTEGER"),
    # /sohoc (number-guessing) config
    ("guild_configs", "sohoc_channel_id", "BIGINT"),
    ("guild_configs", "sohoc_announce_channel_id", "BIGINT"),
    ("guild_configs", "sohoc_creator_role_ids", "VARCHAR(512)"),
    ("guild_configs", "sohoc_winner_role_id", "BIGINT"),
    ("guild_configs", "sohoc_default_seconds", "INTEGER"),
    ("guild_configs", "sohoc_end_time", "VARCHAR(5)"),
    ("guild_configs", "sohoc_tz_offset", "INTEGER"),
    # /caro (12x12) config
    ("guild_configs", "caro_channel_id", "BIGINT"),
    ("guild_configs", "caro_announce_channel_id", "BIGINT"),
    ("guild_configs", "caro_winner_role_id", "BIGINT"),
    ("guild_configs", "caro_wins_for_role", "INTEGER"),
    # Per-minigame entry fee (coin deduction on join)
    ("economy_minigame_configs", "entry_fee", "INTEGER DEFAULT 0"),
    # Top-3 placement rewards (replace win/runner_up bucket model)
    ("economy_minigame_configs", "top1_reward", "INTEGER DEFAULT 0"),
    ("economy_minigame_configs", "top2_reward", "INTEGER DEFAULT 0"),
    ("economy_minigame_configs", "top3_reward", "INTEGER DEFAULT 0"),
    # /oantuti + /tournament config
    ("guild_configs", "oantuti_icon_bua", "VARCHAR(64)"),
    ("guild_configs", "oantuti_icon_bao", "VARCHAR(64)"),
    ("guild_configs", "oantuti_icon_keo", "VARCHAR(64)"),
    ("guild_configs", "oantuti_creator_role_ids", "VARCHAR(512)"),
    ("guild_configs", "oantuti_channel_id", "BIGINT"),
    ("guild_configs", "tournament_bracket_channel_id", "BIGINT"),
    ("guild_configs", "tournament_match_channel_id", "BIGINT"),
    ("guild_configs", "tournament_announce_channel_id", "BIGINT"),
    # Rematch link for /oantuti
    ("oantuti_games", "rematch_game_id", "BIGINT"),
    # Vinh danh channel for /oantuti winner announcements
    ("guild_configs", "oantuti_announce_channel_id", "BIGINT"),
    # /diemdanh (daily check-in) config
    ("economy_guild_configs", "checkin_channel_id", "BIGINT"),
    ("economy_guild_configs", "checkin_reminder_enabled", "BOOLEAN DEFAULT FALSE"),
    ("economy_guild_configs", "checkin_tz_offset", "INTEGER DEFAULT 0"),
    ("economy_guild_configs", "checkin_reminder_hour_utc", "INTEGER"),
    # Server-boost reward
    ("economy_guild_configs", "boost_reward_amount", "INTEGER DEFAULT 0"),
    ("economy_guild_configs", "boost_reward_enabled", "BOOLEAN DEFAULT TRUE"),
    # /baodanh approval reward
    ("economy_guild_configs", "approval_reward_amount", "INTEGER DEFAULT 0"),
    ("economy_guild_configs", "approval_reward_enabled", "BOOLEAN DEFAULT TRUE"),
    # /vuatiengviet config
    ("guild_configs", "vtv_channel_id", "BIGINT"),
    ("guild_configs", "vtv_announce_channel_id", "BIGINT"),
    ("guild_configs", "vtv_creator_role_ids", "VARCHAR(512)"),
    ("guild_configs", "vtv_winner_role_id", "BIGINT"),
    ("guild_configs", "vtv_wins_for_role", "INTEGER"),
    # Pet House
    ("pet_guild_configs", "pet_house_channel_id", "BIGINT"),
    ("pet_guild_configs", "pet_house_message_id", "BIGINT"),
    ("pet_guild_configs", "pet_share_channel_id", "BIGINT"),
    ("pet_guild_configs", "pet_house_server_name", "VARCHAR(128)"),
    # Pet action coin costs
    ("pet_guild_configs", "pet_feed_cost", "INTEGER DEFAULT 10"),
    ("pet_guild_configs", "pet_water_cost", "INTEGER DEFAULT 5"),
    ("pet_guild_configs", "pet_rename_cost", "INTEGER DEFAULT 50"),
    ("pet_guild_configs", "pet_mission_tz_offset", "INTEGER DEFAULT 7"),
    ("pet_guild_configs", "pet_mission_reset_hour", "INTEGER DEFAULT 0"),
    # Pet action cooldown timestamps
    ("user_pets", "last_play_at", "TIMESTAMP WITH TIME ZONE"),
    ("user_pets", "last_clean_at", "TIMESTAMP WITH TIME ZONE"),
    ("user_pets", "last_rest_at", "TIMESTAMP WITH TIME ZONE"),
    ("user_pets", "last_train_at", "TIMESTAMP WITH TIME ZONE"),
    # Pet passive income (granted by hourly decay loop)
    ("user_pets", "daily_income_credits", "INTEGER DEFAULT 0"),
    ("user_pets", "daily_income_date", "VARCHAR(10)"),
    # Show pet on /info card (toggled from Pet House)
    ("user_pets", "show_in_info", "BOOLEAN DEFAULT FALSE"),
    # Persistent /baodanh button card
    ("guild_configs", "baodanh_card_channel_id", "BIGINT"),
    ("guild_configs", "baodanh_card_message_id", "BIGINT"),
    # Optional Link-UID slash mention on the registration card
    ("guild_configs", "baodanh_link_bot_id", "BIGINT"),
    ("guild_configs", "baodanh_link_command_id", "BIGINT"),
    ("guild_configs", "baodanh_link_command_name", "VARCHAR(64)"),
    # akim-desk-pet bridge — map bot's per-guild species to the desktop
    # app's bundled asset pool. NULL means species is Discord-only.
    ("pet_species", "desktop_asset_id", "VARCHAR(64)"),
    # /sohoc — owner opts into auto-spawning the next game whenever
    # the previous one finishes.
    ("guild_configs", "sohoc_autostart", "BOOLEAN DEFAULT FALSE"),
    # /sohoc — per-guild default language (NULL → falls back to 'ru')
    ("guild_configs", "sohoc_default_language", "VARCHAR(4)"),
    # /sohoc — per-entry escrowed bet ("Bet" button). Debited on
    # placement, refunded plus bonus on win, kept on lose.
    ("sohoc_entries", "bet_amount", "INTEGER DEFAULT 0"),
    # /sohoc — second roll for consolation tier (owner sets top2_reward).
    ("sohoc_sessions", "rolled_number_2", "INTEGER"),
    ("sohoc_sessions", "winner_2_id", "BIGINT"),
    # /quaythuong — channel where the giveaway card is posted.
    ("guild_configs", "giveaway_channel_id", "BIGINT"),
    # Per-card language toggle. Existing rows default to 'ru' via the
    # column default; a click on the 🌐 language toggle switches it.
    ("sohoc_sessions", "language", "VARCHAR(4) DEFAULT 'ru'"),
    ("oantuti_games", "language", "VARCHAR(4) DEFAULT 'vi'"),
    ("giveaway_games", "language", "VARCHAR(4) DEFAULT 'vi'"),
    # Persistent /player help card (posted via /setup-player-card).
    ("guild_configs", "player_card_channel_id", "BIGINT"),
    ("guild_configs", "player_card_message_id", "BIGINT"),
    # /gvg manager-buttons split — optional channel that hosts the
    # [Assign/Move/Teams/Refresh] card so managers can gate visibility
    # via Discord's own channel permissions (see GvgGuildSettings +
    # GvgRosterMessage.kind).
    ("gvg_guild_settings", "manager_channel_id", "BIGINT"),
    ("gvg_roster_messages", "kind", "VARCHAR(16) DEFAULT 'BOARD' NOT NULL"),
    # /gvg roster board layout switch. 1 = legacy grouped-per-position
    # view; 2 = flat per-player view. See GvgGuildSettings.view_mode.
    ("gvg_guild_settings", "view_mode", "INTEGER DEFAULT 1 NOT NULL"),
    # view_mode=4 grid renderer — configurable corner badges drawn on
    # each player's avatar so commander + team leader are visible at a
    # glance. Nullable; NULL falls back to 👑 / 🎖️ defaults in the
    # renderer. See GvgGuildSettings.grid_commander_icon / grid_leader_icon.
    ("gvg_guild_settings", "grid_commander_icon", "VARCHAR(128)"),
    ("gvg_guild_settings", "grid_leader_icon", "VARCHAR(128)"),
    # Cell top / bottom padding (px) around the content on the grid.
    # See GvgGuildSettings.grid_top_padding / grid_bottom_padding.
    ("gvg_guild_settings", "grid_top_padding",
        "INTEGER DEFAULT 18 NOT NULL"),
    ("gvg_guild_settings", "grid_bottom_padding",
        "INTEGER DEFAULT 10 NOT NULL"),
    # Optional cell-background highlight colors for Commander / Leader
    # roles in view_mode=4. NULL = no override (default card BG used).
    # See GvgGuildSettings.grid_commander_bg_hex / grid_leader_bg_hex.
    ("gvg_guild_settings", "grid_commander_bg_hex", "VARCHAR(7)"),
    ("gvg_guild_settings", "grid_leader_bg_hex", "VARCHAR(7)"),
    # Glass / frosted-glass visual settings for view_mode=4 grid cells.
    # grid_glass_enabled: toggle the glass look on/off.
    # grid_glass_blur: when ON, GaussianBlur the region behind each cell
    #   (frosted-glass backdrop) before drawing the translucent overlay.
    # grid_glass_opacity: 0-100 — alpha of the glass card overlay.
    # grid_commander_bg_alpha / grid_leader_bg_alpha: 0-100 — alpha of
    #   the role-color tint; ignored when glass is off and the cell uses
    #   a solid role-color background instead.
    ("gvg_guild_settings", "grid_glass_enabled",
        "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("gvg_guild_settings", "grid_glass_blur",
        "BOOLEAN DEFAULT TRUE NOT NULL"),
    ("gvg_guild_settings", "grid_glass_opacity",
        "INTEGER DEFAULT 80 NOT NULL"),
    ("gvg_guild_settings", "grid_commander_bg_alpha",
        "INTEGER DEFAULT 100 NOT NULL"),
    ("gvg_guild_settings", "grid_leader_bg_alpha",
        "INTEGER DEFAULT 100 NOT NULL"),
    # /lfg guild-war day-voting model: main roster cap + per-member day + reserve flag
    ("lfg_parties", "main_limit", "INTEGER"),
    ("lfg_members", "day", "VARCHAR(4)"),
    ("lfg_members", "is_reserve", "BOOLEAN DEFAULT FALSE NOT NULL"),
    # role_selection toggle: False = no role picker (day-only voting)
    ("lfg_parties", "gw_role_selection", "BOOLEAN DEFAULT TRUE NOT NULL"),
    # Persistent Guild War Card (posted via /setup-gw-card)
    ("guild_configs", "gw_card_channel_id", "BIGINT"),
    ("guild_configs", "gw_card_message_id", "BIGINT"),
    # LFG ↔ GVG integration: GW party auto-creates a linked GVG match
    ("lfg_parties", "gvg_match_id", "INT"),
    ("gvg_matches", "lfg_party_id", "INT"),
    ("gvg_matches", "lfg_pool_team_id", "INT"),
    # Persistent LFG Card (Hero Realm / Sword Trial / Custom buttons)
    ("guild_configs", "lfg_card_channel_id", "BIGINT"),
    ("guild_configs", "lfg_card_message_id", "BIGINT"),
    # Per-type LFG post channels (fallback to lfg_channel_id if NULL)
    ("guild_configs", "lfg_hero_realm_channel_id", "BIGINT"),
    ("guild_configs", "lfg_sword_trial_channel_id", "BIGINT"),
    ("guild_configs", "lfg_custom_channel_id", "BIGINT"),
    # LFG party language toggle (default 'ru')
    ("lfg_parties", "language", "VARCHAR(4) DEFAULT 'ru' NOT NULL"),
    # Optional activity start time shown below the party embed title
    ("lfg_parties", "start_time", "VARCHAR(11)"),
    # UI language for all user-facing economy embeds: "ru" (default) or "en".
    ("economy_guild_configs", "language", "VARCHAR(2) DEFAULT 'ru' NOT NULL"),
    # Per-board display language for GameWinBoard leaderboards.
    ("game_win_boards", "language", "VARCHAR(4) DEFAULT 'ru' NOT NULL"),
)


# One-shot cleanup for the SPEC rewrite. The old /gvg feature (event,
# team, position, backup, assignment) is being replaced wholesale — user
# chose "fresh start" so the legacy tables and GuildConfig columns are
# dropped. Migrations here run once against the target DB.
#
# CRITICAL: two of these table names (`gvg_teams`, `gvg_positions`) are
# ALSO the names used by the new SPEC models. The bare "DROP … IF EXISTS"
# would happily wipe the live SPEC data on every restart. So we gate the
# whole block on a fingerprint check: only run when `gvg_events` (a name
# unique to the OLD schema) is still present. Once cleaned up, the check
# fails on subsequent restarts and the block no-ops permanently.
_GVG_LEGACY_TABLES_TO_DROP: tuple[str, ...] = (
    # Cascade so the assignments/backups children go with the parents.
    "gvg_backups",
    "gvg_assignments",
    "gvg_positions",
    "gvg_teams",
    "gvg_events",  # must be last — its absence is the fingerprint
)
_GVG_LEGACY_COLUMNS_TO_DROP: tuple[str, ...] = (
    # Deprecated per-guild config columns (moved to gvg_guild_settings).
    "gvg_manager_role_ids",
    "gvg_voice_channel_id",
    "gvg_tz_offset",
    "gvg_mention_on_show",
)


def _translate_for_sqlite(sql_type: str) -> str:
    return (
        sql_type
        .replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP")
        .replace("DEFAULT FALSE", "DEFAULT 0")
        .replace("DEFAULT TRUE", "DEFAULT 1")
    )


class Database:
    def __init__(self, url: str) -> None:
        # echo=False; pool defaults are fine for a bot
        self.engine: AsyncEngine = create_async_engine(url, future=True)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def init_models(self) -> None:
        # Log connection target so deploy mishaps (e.g. accidentally falling
        # back to SQLite in a container) are immediately visible in logs.
        log.info(
            "Connecting to DB: dialect=%s url=%s",
            self.engine.dialect.name,
            self.engine.url.render_as_string(hide_password=True),
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self._apply_incremental_migrations()
        log.info("DB ready (additive schema only — see policy in session.py)")

    async def _apply_incremental_migrations(self) -> None:
        dialect = self.engine.dialect.name
        async with self.engine.begin() as conn:
            for table, column, sql_type in _INCREMENTAL_COLUMNS:
                if dialect == "sqlite":
                    result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
                    cols = {row[1] for row in result.fetchall()}
                    if column not in cols:
                        await conn.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {column} "
                            f"{_translate_for_sqlite(sql_type)}"
                        )
                        log.info("Added column %s.%s (sqlite)", table, column)
                else:
                    # PostgreSQL et al. support ADD COLUMN IF NOT EXISTS.
                    await conn.execute(
                        text(
                            f"ALTER TABLE {table} "
                            f"ADD COLUMN IF NOT EXISTS {column} {sql_type}"
                        )
                    )

        # Backfill the legacy single-role column into the new CSV column.
        # Runs in its own transaction so a missing legacy column (fresh
        # install) doesn't poison the migrations above.
        await self._backfill_lfg_role_ids(dialect)

        # One-shot cleanup of the pre-SPEC /gvg tables + config columns.
        # SQLite doesn't support ALTER … DROP COLUMN so we skip there;
        # dev DBs get recreated anyway.
        #
        # Fingerprint gate: two of the tables in the drop list share
        # names with the new SPEC schema (`gvg_teams`, `gvg_positions`).
        # If we ran the drops unconditionally we would wipe live data
        # every restart. Instead we probe for `gvg_events` (unique to
        # the OLD schema); when it's gone, so is the reason to run any
        # of these drops.
        if dialect != "sqlite":
            async with self.engine.begin() as conn:
                legacy_present = (
                    await conn.execute(
                        text("SELECT to_regclass('public.gvg_events')")
                    )
                ).scalar()
            if legacy_present is not None:
                log.warning(
                    "gvg: legacy pre-SPEC schema detected — running "
                    "one-shot cleanup (drops gvg_events + siblings + "
                    "deprecated guild_configs columns).",
                )
                for table in _GVG_LEGACY_TABLES_TO_DROP:
                    try:
                        async with self.engine.begin() as conn:
                            await conn.execute(
                                text(f"DROP TABLE IF EXISTS {table} CASCADE")
                            )
                    except Exception as e:
                        log.debug(
                            "gvg legacy drop skipped (%s): %s", table, e,
                        )
                for column in _GVG_LEGACY_COLUMNS_TO_DROP:
                    try:
                        async with self.engine.begin() as conn:
                            await conn.execute(text(
                                f"ALTER TABLE guild_configs "
                                f"DROP COLUMN IF EXISTS {column}"
                            ))
                    except Exception as e:
                        log.debug(
                            "gvg legacy column drop skipped (%s): %s",
                            column, e,
                        )
                # Re-create the freshly-dropped SPEC tables — the drops
                # above take out gvg_teams / gvg_positions which are
                # also SPEC-model names, and the earlier create_all()
                # ran before this cleanup so those tables are now gone.
                async with self.engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

        # ---- Teams-guild-scoped migration ---------------------------------
        #
        # Original SPEC-rewrite schema (PR #26) kept `gvg_teams.match_id` — a
        # team belonged to exactly one match. The user then asked for teams
        # to be shared across every match of the guild (only roster
        # assignments differ per match). Fingerprint the old shape by
        # looking for `gvg_teams.match_id` — when present, drop the team +
        # assignment tables (destructive: team names + assignments are
        # unrecoverable) and let `create_all` recreate them with the new
        # guild-scoped shape. Idempotent once migrated (column gone → skip).
        if dialect != "sqlite":
            async with self.engine.begin() as conn:
                needs_team_migration = (
                    await conn.execute(text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'gvg_teams' "
                        "AND column_name = 'match_id' LIMIT 1"
                    ))
                ).scalar()
            if needs_team_migration:
                log.warning(
                    "gvg: teams are being migrated from per-match to "
                    "guild-scoped — dropping gvg_teams + "
                    "gvg_roster_assignments (destructive, per user request)."
                )
                try:
                    async with self.engine.begin() as conn:
                        await conn.execute(text(
                            "DROP TABLE IF EXISTS gvg_roster_assignments "
                            "CASCADE"
                        ))
                        await conn.execute(text(
                            "DROP TABLE IF EXISTS gvg_teams CASCADE"
                        ))
                    async with self.engine.begin() as conn:
                        await conn.run_sync(Base.metadata.create_all)
                    log.warning(
                        "gvg: teams migration complete — teams are now "
                        "guild-scoped."
                    )
                except Exception:
                    log.exception("gvg: teams-guild-scoped migration failed")

    async def _backfill_lfg_role_ids(self, dialect: str) -> None:
        try:
            if dialect == "sqlite":
                async with self.engine.begin() as conn:
                    result = await conn.exec_driver_sql(
                        "PRAGMA table_info(guild_configs)"
                    )
                    cols = {row[1] for row in result.fetchall()}
                    if "lfg_role_id" not in cols:
                        return
                    await conn.execute(
                        text(
                            "UPDATE guild_configs "
                            "SET lfg_role_ids = CAST(lfg_role_id AS TEXT) "
                            "WHERE lfg_role_id IS NOT NULL "
                            "AND (lfg_role_ids IS NULL OR lfg_role_ids = '')"
                        )
                    )
            else:
                async with self.engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE guild_configs "
                            "SET lfg_role_ids = CAST(lfg_role_id AS VARCHAR(512)) "
                            "WHERE lfg_role_id IS NOT NULL "
                            "AND (lfg_role_ids IS NULL OR lfg_role_ids = '')"
                        )
                    )
        except Exception as e:
            # Legacy column missing — fresh install, nothing to backfill.
            log.debug("Skipping lfg_role_id backfill: %s", e)

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as s:
            yield s
