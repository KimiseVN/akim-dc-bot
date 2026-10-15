"""/gvg — Guild War roster manager (SPEC rewrite, Phases A-E, complete).

This file implements the data-management, roster-mutation, and persistent
roster-board surface described in the GvG SPEC:

  Phase A — foundations
    /gvg setup config    /gvg setup status
    /gvg position add / edit / delete / list / reorder
    /gvg match create / edit / delete / view / list
    /gvg team add / edit / delete / list / reorder
    (Teams AND positions are guild-scoped — shared across every match
    of the guild. Only per-player assignments differ per match.)

  Phase B — roster
    /gvg roster assign   (UserSelect multi-user + Add More)
    /gvg roster remove   (multi-select)
    /gvg roster move     (multi-select + Team/Position/Type target)
    /gvg roster swap
    /gvg roster view
    /gvg team commander
    /gvg player view / style / reset-style / note

  Phase C — persistent roster board (SPEC §32-§34)
    /gvg roster publish   (create/replace the board in the roster channel)
    /gvg roster refresh   (edit stored messages in place)
    Buttons: [➕ Assign] [🔄 Move] [👥 Teams] [🔃 Refresh]
    Auto-refresh — every mutation that changes what the board shows
    (assign/remove/move/swap, team rename/reorder/delete, position
    edit/delete, player style/note, commander change, match metadata)
    schedules a debounced refresh so batch operations trigger one edit,
    not one per row.

  Phase D — finer perms + audit surface (SPEC §35-§42)
    Permission tiers:
      BOT_ADMIN — Administrator + admin_role
      GVG_MANAGER — Manage Server + manager_role
      TEAM_COMMANDER — commander of a team; can manage that team's
        roster (add/remove/move/swap within own team) IF the guild
        setting `allow_commander_roster_management` is on
      VIEWER — anyone reading the published board (read-only)
    /gvg setup permissions — toggle the commander self-manage flag
    /gvg audit list — show recent mutations (filter by match)
    Every mutation forwards a summary embed to the guild's log channel
    when configured (batch operations post ONE summary, SPEC §42).

  Phase E — match lifecycle + clone + setup shortcuts (SPEC §5, §29, §45)
    /gvg match publish   (DRAFT → PUBLISHED + post board)
    /gvg match lock      (PUBLISHED → LOCKED; mutations refused)
    /gvg match unlock    (LOCKED → PUBLISHED)
    /gvg match archive   (→ ARCHIVED; board messages removed)
    /gvg match restore   (ARCHIVED → DRAFT; republish as needed)
    /gvg match clone     (deep copy: teams/main/reserve/commanders opt-in;
                          new match starts in DRAFT — global positions
                          are guild-scoped so they aren't cloned)
    /gvg setup channel        (shortcut for roster_channel)
    /gvg setup log-channel    (shortcut for log_channel)

Invariants (SPEC §87):
  - MAIN count per match ≤ match.main_player_limit (default 30)
  - Reserve players never count toward MAIN limit
  - UNIQUE(match_id, user_id): a user can hold one slot per match
  - Commander must be MAIN of the same team
  - Every write re-checks guild ownership + permission
  - Batch operations are transactional (SELECT … FOR UPDATE on the match
    row makes the MAIN cap concurrency-safe)
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select

# Pillow is an OPTIONAL dep — the grid renderer (view_mode=4) uses it,
# every other view_mode is pure text. If the package is missing, the
# renderer degrades to view_mode=1 at render time and logs once, so a
# botched deploy that dropped Pillow does not take the whole board down.
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont  # type: ignore

    _HAS_PIL = True
except Exception:  # pragma: no cover — best-effort import
    Image = ImageDraw = ImageFilter = ImageFont = None  # type: ignore
    _HAS_PIL = False

from ..db.models import (
    GvgAuditLog,
    GvgAvatarCache,
    GvgGuildSettings,
    GvgMatch,
    GvgPlayerProfile,
    GvgPosition,
    GvgRosterAssignment,
    GvgRosterMessage,
    GvgTeam,
    GvgTeamLeader,
    Player,
)

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.gvg")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAIN_LIMIT = 30
MATCH_STATUSES = ("DRAFT", "PUBLISHED", "LOCKED", "ARCHIVED")
ROSTER_TYPES = ("MAIN", "RESERVE")

# Discord caps a message at 10 embeds. The board tries to squeeze the
# overview + every team into as few messages as possible; the last
# message carries the [Assign / Move / Teams / Refresh] view.
MAX_EMBEDS_PER_MESSAGE = 10
# Debounce for auto-refresh so batch operations coalesce into one edit
# rather than firing per-row (SPEC §34).
AUTO_REFRESH_DELAY_SEC = 0.6
# Ephemeral "success" confirmations auto-delete after this many seconds
# so the manager's DM isn't cluttered with `✅ Team added` / `✅ Position
# updated` messages after every action.
SUCCESS_DISMISS_DELAY_SEC = 1.0

# Prefix shown at the top of every team block on the roster board to
# announce that team's per-match tactical lead. Distinct from the
# guild-wide 👑 commander badge — Team Leader is scoped to a single
# match and can be reassigned per match without disturbing the
# guild-level commander.
TEAM_LEADER_ICON = "🎖️"
TEAM_LEADER_LABEL = "Team Lead"

# Separator used between players inside a position group on the
# roster board. A pipe reads cleaner than a comma when the names
# already contain punctuation from embedded weapon/position emoji.
PLAYER_LIST_SEPARATOR = " │ "

# ---------------------------------------------------------------------------
# view_mode=4 (grid image) layout constants
# ---------------------------------------------------------------------------
#
# One PNG per team. Each MAIN player is a 5-col cell showing:
#   - avatar (from GvgAvatarCache; placeholder when not cached),
#   - main-weapon emoji overlay top-right,
#   - ingame_name (or member display name) caption below.
# Reserves — when any — render as a compact strip beneath the MAIN grid
# with smaller cells so the roster remains scannable at a glance.
#
# Sizes were picked so a 30-player MAIN roster (6 rows × 5 cols) fits in
# a ~800×1400 PNG that lands well under Discord's 25 MiB attachment cap
# with plenty of headroom for team headers and reserve strips.
GRID_COLS = 5
GRID_CELL_W = 200
GRID_AVATAR_SIZE = 128
GRID_WEAPON_SIZE = 60
GRID_CELL_PADDING = 10
GRID_MARGIN = 18
GRID_HEADER_H = 68
GRID_RESERVE_LABEL_H = 40
GRID_RESERVE_CELL_W = 168
GRID_RESERVE_AVATAR_SIZE = 96

# Content-only cell height WITHOUT top/bottom padding — so the runtime
# cell height is `content + top_pad + bottom_pad`. Both pads come from
# GvgGuildSettings (grid_top_padding, grid_bottom_padding); guilds can
# tune the whitespace at either edge without shifting the visible
# content upward or truncating the position icon.
GRID_MAIN_CELL_CONTENT_H = 312  # avatar + gap + caption + icon-gap + icon
GRID_RESERVE_CELL_CONTENT_H = 222
GRID_DEFAULT_TOP_PAD = 18
GRID_DEFAULT_BOTTOM_PAD = 10
# Kept as legacy defaults so any lingering caller of _draw_cell that
# doesn't pass an explicit cell_h still gets a sane background rect.
GRID_CELL_H = (
    GRID_MAIN_CELL_CONTENT_H + GRID_DEFAULT_TOP_PAD + GRID_DEFAULT_BOTTOM_PAD
)
GRID_RESERVE_CELL_H = (
    GRID_RESERVE_CELL_CONTENT_H
    + GRID_DEFAULT_TOP_PAD + GRID_DEFAULT_BOTTOM_PAD
)

# Caption / header font sizes. Tuned so the grid reads at Discord's
# typical inline-image render width — closer to the chat font size
# than PIL's tiny default.
GRID_CAPTION_FONT_SIZE = 28
GRID_RESERVE_CAPTION_FONT_SIZE = 28
GRID_HEADER_TITLE_SIZE = 30
GRID_HEADER_STATS_SIZE = 22

_GRID_BG = (30, 33, 36)
_GRID_CARD_BG = (43, 45, 49)
_GRID_TEXT = (240, 240, 240)
_GRID_MUTED = (170, 170, 175)
_GRID_HEADER_TEXT = (255, 255, 255)

# Font search chain — first hit wins. Bundled fonts are NOT required;
# on typical Debian-based container images DejaVu Sans ships in the
# stdlib install (fonts-dejavu-core). PIL's default bitmap font is the
# final fallback so the renderer never fails hard on a missing font —
# it just loses Vietnamese glyphs.
_GRID_FONT_CHAIN = (
    # Noto first — best Vietnamese coverage. Installed by nixpacks.toml
    # (`fonts-noto-core`) on Railway.
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    # DejaVu is the second-best option (installed by `fonts-dejavu-core`);
    # covers Vietnamese too, just slightly less consistent per-glyph
    # weight. Kept as a fallback in case a base image ships DejaVu but
    # not Noto.
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)

# Custom-emoji id extractor: matches `<:name:id>` or `<a:name:id>` and
# captures the numeric id so the grid renderer can fetch the emoji PNG
# from Discord's CDN.
_CUSTOM_EMOJI_ID_RE = re.compile(r"^<a?:[A-Za-z0-9_]+:(\d+)>$")

# One-time warn latch so a Pillow-less deploy does not spam the log on
# every board refresh.
_PIL_MISSING_WARNED = False

# Audit action → (icon, human title) map used by the log-channel
# forwarder and the /gvg audit list surface (SPEC §41).
_AUDIT_LABELS: dict[str, tuple[str, str]] = {
    "MATCH_CREATED":         ("🆕", "Match created"),
    "MATCH_UPDATED":         ("✏️", "Match updated"),
    "MATCH_DELETED":         ("🗑️", "Match deleted"),
    "MATCH_PUBLISHED":       ("📢", "Match published"),
    "MATCH_LOCKED":          ("🔒", "Match locked"),
    "MATCH_UNLOCKED":        ("🔓", "Match unlocked"),
    "MATCH_ARCHIVED":        ("🗄️", "Match archived"),
    "BOARD_PUBLISHED":       ("📢", "Roster board published"),
    "TEAM_CREATED":          ("🛡️", "Team created"),
    "TEAM_UPDATED":          ("🛠️", "Team updated"),
    "TEAM_DELETED":          ("💥", "Team deleted"),
    "POSITION_CREATED":      ("🎯", "Position created"),
    "POSITION_UPDATED":      ("🎯", "Position updated"),
    "POSITION_DELETED":      ("🗑️", "Position deleted"),
    "POSITION_DISABLED":     ("🚫", "Position disabled"),
    "PLAYERS_ASSIGNED":      ("📥", "Players assigned"),
    "PLAYERS_UNASSIGNED":    ("📤", "Players removed"),
    "PLAYERS_MOVED":         ("🔄", "Player moved"),
    "PLAYERS_SWAPPED":       ("↔️", "Players swapped"),
    "COMMANDER_SET":         ("👑", "Commander set"),
    "COMMANDER_REMOVED":     ("👋", "Commander cleared"),
    "TEAM_LEADER_SET":       ("🎖️", "Team leader set"),
    "TEAM_LEADER_REMOVED":   ("🎗️", "Team leader cleared"),
    "PLAYER_STYLE_UPDATED":  ("🎨", "Player style updated"),
    "SETTINGS_UPDATED":      ("⚙️", "GvG settings updated"),
}
_AUDIT_COLORS: dict[str, discord.Color] = {
    "MATCH_CREATED":         discord.Color.green(),
    "MATCH_UPDATED":         discord.Color.blurple(),
    "MATCH_DELETED":         discord.Color.red(),
    "MATCH_PUBLISHED":       discord.Color.blue(),
    "MATCH_LOCKED":          discord.Color.dark_grey(),
    "MATCH_UNLOCKED":        discord.Color.blurple(),
    "MATCH_ARCHIVED":        discord.Color.dark_orange(),
    "BOARD_PUBLISHED":       discord.Color.blue(),
    "TEAM_CREATED":          discord.Color.green(),
    "TEAM_UPDATED":          discord.Color.blurple(),
    "TEAM_DELETED":          discord.Color.red(),
    "POSITION_CREATED":      discord.Color.green(),
    "POSITION_UPDATED":      discord.Color.blurple(),
    "POSITION_DELETED":      discord.Color.red(),
    "POSITION_DISABLED":     discord.Color.orange(),
    "PLAYERS_ASSIGNED":      discord.Color.green(),
    "PLAYERS_UNASSIGNED":    discord.Color.orange(),
    "PLAYERS_MOVED":         discord.Color.blurple(),
    "PLAYERS_SWAPPED":       discord.Color.blurple(),
    "COMMANDER_SET":         discord.Color.gold(),
    "COMMANDER_REMOVED":     discord.Color.dark_gold(),
    "TEAM_LEADER_SET":       discord.Color.gold(),
    "TEAM_LEADER_REMOVED":   discord.Color.dark_gold(),
    "PLAYER_STYLE_UPDATED":  discord.Color.purple(),
    "SETTINGS_UPDATED":      discord.Color.greyple(),
}

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

# Proper Discord custom-emoji format: <:name:id> or <a:name:id> (animated).
# Discord only renders those two shapes AND raw unicode emojis inside embed
# descriptions — a bare `:name:` shortcode (which users often paste from
# the emoji picker) shows up as literal text and makes the roster look
# duplicated (e.g. `:zDPS:` right before `**DPS**`). `_sanitize_display_icon`
# drops shortcodes and lets everything else through.
_DISCORD_CUSTOM_EMOJI = re.compile(r"^<a?:[A-Za-z0-9_]+:\d+>$")
_SHORTCODE_ONLY = re.compile(r"^:[A-Za-z0-9_+-]+:$")


def sanitize_display_icon(
    icon: str | None,
    guild: discord.Guild | None = None,
) -> str | None:
    """Return the icon in a form Discord actually renders inside embeds.

    - Proper `<:name:id>` / `<a:name:id>` → kept as-is (both title and
      description render these).
    - Unicode emoji (or any non-shortcode text) → kept as-is (this is
      how team icons "just work" when the user pastes 🛡️).
    - Bare `:name:` shortcode: Discord does NOT parse `:name:` in embed
      text. But if `guild` has a custom emoji named `name`, we upgrade
      the shortcode to the proper `<:name:id>` form on the fly so it
      renders. Otherwise the value is dropped (rather than leaked as
      literal `:name:`).
    """
    if not icon:
        return None
    s = icon.strip()
    if not s:
        return None
    if _DISCORD_CUSTOM_EMOJI.match(s):
        return s
    if _SHORTCODE_ONLY.match(s):
        if guild is not None:
            name = s[1:-1]
            emoji = discord.utils.get(guild.emojis, name=name)
            if emoji is not None:
                # str(discord.Emoji) yields <a?:name:id>, which Discord
                # renders in embed text the same as a manually-typed
                # custom emoji.
                return str(emoji)
        return None
    return s

# hex range → nearest color emoji (SPEC §13). Ordered so lookups scan
# smallest-radius neighbourhoods first.
_COLOR_EMOJIS: tuple[tuple[str, int, int, int], ...] = (
    # (emoji, r, g, b)
    ("🟥", 220,  20,  20),
    ("🟧", 240, 140,  20),
    ("🟨", 245, 220,  50),
    ("🟩",  50, 190,  90),
    ("🟦",  50, 130, 220),
    ("🟪", 155,  80, 205),
    ("🟫", 130,  80,  40),
    ("⬜", 240, 240, 240),
    ("⬛",  20,  20,  20),
)


# ---------------------------------------------------------------------------
# Typed errors (SPEC §50)
# ---------------------------------------------------------------------------


class GvgError(RuntimeError):
    """Base — service layer raises subclasses; the slash-command layer
    turns them into ephemeral messages."""


class NotFoundError(GvgError):
    pass


class PermissionError(GvgError):
    pass


class ValidationError(GvgError):
    pass


class RosterFullError(GvgError):
    pass


class ConflictError(GvgError):
    pass


class MatchLockedError(GvgError):
    pass


class InvalidStateError(GvgError):
    pass


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def normalize_hex(value: str | None) -> str | None:
    """Normalize `#RRGGBB` / `RRGGBB` → `#RRGGBB` uppercase. Return None
    for None/empty; raise ValidationError for bad input."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    m = _HEX_RE.match(v)
    if not m:
        raise ValidationError(
            f"Invalid HEX color `{value}` — expected `#RRGGBB` (e.g. `#FF8800`)."
        )
    return "#" + m.group(1).upper()


def hex_to_nearest_color_emoji(hex_value: str | None) -> str:
    """SPEC §13: pick the closest palette emoji for a HEX color.
    Fallback is ⬜ so callers can concat without checking."""
    if not hex_value:
        return "⬜"
    m = _HEX_RE.match(hex_value)
    if not m:
        return "⬜"
    raw = m.group(1)
    r, g, b = int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    best = "⬜"
    best_d = float("inf")
    for emoji, cr, cg, cb in _COLOR_EMOJIS:
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if d < best_d:
            best_d = d
            best = emoji
    return best


def hex_to_discord_color(hex_value: str | None) -> discord.Color:
    if not hex_value:
        return discord.Color.blurple()
    m = _HEX_RE.match(hex_value)
    if not m:
        return discord.Color.blurple()
    return discord.Color(int(m.group(1), 16))


def normalize_status(value: str | None) -> str:
    if not value:
        return "DRAFT"
    v = value.strip().upper()
    if v not in MATCH_STATUSES:
        raise ValidationError(
            f"Invalid status `{value}` — expected one of {', '.join(MATCH_STATUSES)}."
        )
    return v


def normalize_roster_type(value: str | None) -> str:
    if not value:
        return "MAIN"
    v = value.strip().upper()
    if v not in ROSTER_TYPES:
        raise ValidationError(
            f"Invalid roster type `{value}` — expected MAIN or RESERVE."
        )
    return v


async def error_reply(
    interaction: discord.Interaction, err: BaseException,
) -> None:
    """Convert a GvgError (or unexpected Exception) into an ephemeral
    reply. Never leak stack traces to users (SPEC §49)."""
    if isinstance(err, NotFoundError):
        msg = f"❌ {err}" if str(err) else "❌ Not found."
    elif isinstance(err, PermissionError):
        msg = f"❌ {err}" if str(err) else "❌ You don't have permission to do this."
    elif isinstance(err, RosterFullError):
        msg = f"❌ {err}" if str(err) else "❌ Main roster is full."
    elif isinstance(err, MatchLockedError):
        msg = f"❌ {err}" if str(err) else "❌ This Guild War is locked."
    elif isinstance(err, ValidationError):
        msg = f"❌ {err}"
    elif isinstance(err, ConflictError):
        msg = f"❌ {err}" if str(err) else "❌ Conflict."
    elif isinstance(err, InvalidStateError):
        msg = f"❌ {err}" if str(err) else "❌ Invalid state."
    elif isinstance(err, GvgError):
        msg = f"❌ {err}"
    else:
        log.exception("gvg: unexpected error", exc_info=err)
        msg = "❌ Unexpected error — check the log channel."
    send = (
        interaction.followup.send
        if interaction.response.is_done()
        else interaction.response.send_message
    )
    try:
        await send(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def _dismiss_after(
    msg: "discord.WebhookMessage | discord.Message | None",
    delay: float,
) -> None:
    """Fire-and-forget delete of an ephemeral followup message after
    `delay` seconds. Swallows the usual "message already gone" errors
    so the caller doesn't need to care."""
    if msg is None:
        return
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except (asyncio.CancelledError, discord.NotFound, discord.HTTPException):
        pass


async def send_ok(
    interaction: discord.Interaction,
    content: str,
    *,
    delay: float = SUCCESS_DISMISS_DELAY_SEC,
) -> None:
    """Send an ephemeral "✅ …"-style confirmation and auto-dismiss it
    after `delay` seconds. Errors during the send fall silently — the
    receipt was already implicit from the action succeeding."""
    try:
        msg = await interaction.followup.send(
            content, ephemeral=True, wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        return
    asyncio.create_task(_dismiss_after(msg, delay))


def resolve_display_name(
    guild: discord.Guild | None,
    user_id: int,
    profile: GvgPlayerProfile | None,
) -> str:
    """SPEC §10 priority chain."""
    if profile and profile.display_name_override:
        return profile.display_name_override
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return member.display_name
    return f"<@{user_id}>"


# ---------------------------------------------------------------------------
# view_mode=4 grid — Pillow helpers (module-level so the sync compositor
# stays testable without a live cog). All of these no-op when Pillow is
# not installed; the cog-side branch checks _HAS_PIL before calling them.
# ---------------------------------------------------------------------------


def _extract_emoji_id(icon: str | None) -> int | None:
    """Pull the numeric id out of a `<:name:id>` / `<a:name:id>` icon
    string, or return None for unicode / bare shortcode / empty input.
    The grid renderer needs the id so it can fetch the emoji PNG from
    Discord's CDN; unicode emoji get skipped (no PIL emoji font)."""
    if not icon:
        return None
    m = _CUSTOM_EMOJI_ID_RE.match(icon.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _strip_custom_emoji(text: str) -> str:
    """Remove `<:name:id>` / `<a:name:id>` markup from a string so the
    caption text renders cleanly with a stroke font (Pillow can't draw
    Discord custom emoji inline)."""
    if not text:
        return ""
    return re.sub(r"<a?:[A-Za-z0-9_]+:\d+>", "", text).strip()


_FONT_CACHE: dict[tuple[str, int], "ImageFont.ImageFont"] = {}


def _load_grid_font(size: int) -> "ImageFont.ImageFont":
    """Return a truetype font at `size` from the first available path,
    falling back to PIL's default bitmap font if none of the chain
    fonts exist on this host. Caches by (path, size)."""
    if not _HAS_PIL:  # pragma: no cover — guarded by callers
        raise RuntimeError("Pillow not installed")
    for path in _GRID_FONT_CHAIN:
        key = (path, size)
        if key in _FONT_CACHE:
            return _FONT_CACHE[key]
        try:
            if os.path.exists(path):
                font = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = font
                return font
        except OSError:
            continue
    # Default bitmap font — small, fixed-size, no Vietnamese; last resort.
    fallback = ImageFont.load_default()
    _FONT_CACHE[("__default__", size)] = fallback
    return fallback


def _hex_to_rgb(hex_value: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if not hex_value:
        return default
    m = _HEX_RE.match(hex_value)
    if not m:
        return default
    raw = m.group(1)
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _placeholder_avatar(name: str, size: int, seed: int) -> "Image.Image":
    """Render a first-letter tile for users whose avatar is not cached.
    Colored by hashing the user id so the same player always gets the
    same tile — recognisable across refreshes."""
    # Seed → hue: cycle through a warm-ish palette so tiles are readable
    # against the dark card background.
    palette = (
        (218, 76, 76), (218, 141, 60), (206, 178, 55),
        (86, 178, 92), (58, 149, 216), (152, 96, 214),
        (216, 92, 158), (99, 178, 178),
    )
    color = palette[seed % len(palette)]
    img = Image.new("RGB", (size, size), color)
    draw = ImageDraw.Draw(img)
    letter = (name.strip() or "?")[0].upper()
    try:
        font = _load_grid_font(int(size * 0.55))
        bbox = draw.textbbox((0, 0), letter, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
            letter, fill=(255, 255, 255), font=font,
        )
    except Exception:
        # Bitmap-font path: no textbbox surprises, just center best-effort.
        draw.text((size // 3, size // 3), letter, fill=(255, 255, 255))
    return img


def _open_avatar_bytes(data: bytes, size: int) -> "Image.Image":
    """Decode cached avatar bytes to an RGB square of `size`px. Animated
    GIFs use the first frame; webp / png go through as-is. On any decode
    error the caller falls back to `_placeholder_avatar`."""
    with Image.open(io.BytesIO(data)) as raw:
        raw.seek(0)
        img = raw.convert("RGB")
    # Center-crop to a square before resizing so short-edge crop keeps
    # the face rather than distorting the aspect ratio.
    w, h = img.size
    if w != h:
        edge = min(w, h)
        left = (w - edge) // 2
        top = (h - edge) // 2
        img = img.crop((left, top, left + edge, top + edge))
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    return img


def _round_avatar(img: "Image.Image") -> "Image.Image":
    """Apply a circular alpha mask so the tile reads as a chat avatar
    instead of a hard square. Returns an RGBA image ready to paste."""
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img.convert("RGB"), (0, 0), mask)
    return out


def _wrap_caption(
    text: str, font: "ImageFont.ImageFont", max_width: int, max_lines: int,
) -> list[str]:
    """Greedy word-wrap `text` so no line exceeds `max_width` pixels
    when rendered with `font`. Truncates to `max_lines` and appends `…`
    when text does not fit."""
    text = text or ""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
                if len(lines) == max_lines:
                    break
            # Force-fit an oversized word by character clipping.
            piece = word
            while piece:
                bbox = draw.textbbox((0, 0), piece, font=font)
                if bbox[2] - bbox[0] <= max_width or len(piece) <= 1:
                    current = piece
                    break
                piece = piece[:-1]
    if current and len(lines) < max_lines:
        lines.append(current)
    if not lines:
        lines = [text[:12]]
    # If we bailed early from word-wrapping, mark the truncation.
    joined_len = sum(len(x) for x in lines)
    if joined_len < len(text.replace(" ", "")):
        last = lines[-1]
        while last:
            bbox = draw.textbbox((0, 0), last + "…", font=font)
            if bbox[2] - bbox[0] <= max_width or len(last) <= 1:
                break
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _draw_cell(
    canvas: "Image.Image",
    x: int,
    y: int,
    *,
    avatar: "Image.Image",
    weapon_png: bytes | None,
    caption: str,
    seed: int,
    cell_w: int = GRID_CELL_W,
    cell_h: int = GRID_CELL_H,
    avatar_size: int = GRID_AVATAR_SIZE,
    weapon_size: int = GRID_WEAPON_SIZE,
    caption_font_size: int = GRID_CAPTION_FONT_SIZE,
    position_label: str = "",
    position_font_size: int = 18,
    position_icon_png: bytes | None = None,
    position_icon_size: int = 80,
    is_leader: bool = False,
    leader_badge_png: bytes | None = None,
    badge_size: int = 44,
    top_pad: int = 18,
    cell_bg_color: tuple[int, int, int] | None = None,
    cell_bg_alpha: int = 100,
    glass_enabled: bool = False,
    glass_blur: bool = True,
    glass_opacity: int = 80,
) -> None:
    """Draw one player cell onto `canvas` at (x, y). Cell renders a
    rounded card background, the circular avatar centered, an optional
    weapon-emoji overlay at top-right, and a wrapped caption below."""
    # Card / glass background.
    if glass_enabled:
        # 1. Frosted backdrop — blur the canvas region behind the cell.
        if glass_blur:
            region = canvas.crop((x, y, x + cell_w, y + cell_h))
            canvas.paste(
                region.filter(ImageFilter.GaussianBlur(radius=12)),
                (x, y),
            )
        # 2. Translucent glass overlay via RGBA composite.
        tint = cell_bg_color if cell_bg_color is not None else _GRID_CARD_BG
        if cell_bg_color is not None:
            card_a = max(0, min(255, cell_bg_alpha * 255 // 100))
        else:
            card_a = max(0, min(255, glass_opacity * 255 // 100))
        overlay = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
        draw_ov = ImageDraw.Draw(overlay)
        draw_ov.rounded_rectangle(
            (0, 0, cell_w, cell_h), radius=12,
            fill=(*tint, card_a),
        )
        # Top highlight strip — white sheen that sells the glass look.
        strip_h = max(8, cell_h // 8)
        draw_ov.rounded_rectangle(
            (2, 2, cell_w - 2, strip_h), radius=10,
            fill=(255, 255, 255, 38),
        )
        # Subtle border.
        draw_ov.rounded_rectangle(
            (0, 0, cell_w - 1, cell_h - 1), radius=12,
            outline=(255, 255, 255, 70), width=1,
        )
        region_rgba = canvas.crop((x, y, x + cell_w, y + cell_h)).convert("RGBA")
        region_rgba.alpha_composite(overlay)
        canvas.paste(region_rgba.convert("RGB"), (x, y))
    else:
        # Solid card background — blend role color with default bg when
        # cell_bg_alpha < 100 so the tint can be dialed down without glass.
        if cell_bg_color is not None and cell_bg_alpha < 100:
            a = max(0, min(100, cell_bg_alpha))
            bg: tuple[int, int, int] = tuple(  # type: ignore[assignment]
                _GRID_CARD_BG[i] + (cell_bg_color[i] - _GRID_CARD_BG[i]) * a // 100
                for i in range(3)
            )
        else:
            bg = cell_bg_color if cell_bg_color is not None else _GRID_CARD_BG
        ImageDraw.Draw(canvas).rounded_rectangle(
            (x, y, x + cell_w, y + cell_h),
            radius=12, fill=bg,
        )
    # Avatar centered horizontally, top-padded so the corner badges
    # (drawn above the avatar rect at ay - 4) still clear the cell's
    # rounded border by ~10px instead of hugging the top edge.
    ax = x + (cell_w - avatar_size) // 2
    ay = y + top_pad
    avatar_rgba = _round_avatar(avatar)
    canvas.paste(avatar_rgba, (ax, ay), avatar_rgba)
    # Team Leader corner badge — top-left of the avatar.
    # Missing PNG (config icon didn't resolve) silently drops the badge.
    def _paste_badge(bx: int, by: int, data: bytes) -> None:
        try:
            with Image.open(io.BytesIO(data)) as raw:
                raw.seek(0)
                bimg = raw.convert("RGBA")
            if bimg.size != (badge_size, badge_size):
                bimg = bimg.resize(
                    (badge_size, badge_size), Image.LANCZOS,
                )
            # Light dark disc under the badge so a bright emoji stays
            # readable against a pale avatar.
            ImageDraw.Draw(canvas).ellipse(
                (bx - 2, by - 2,
                 bx + badge_size + 2, by + badge_size + 2),
                fill=(0, 0, 0),
            )
            canvas.paste(bimg, (bx, by), bimg)
        except Exception:
            pass

    badges: list[bytes] = []
    if is_leader and leader_badge_png is not None:
        badges.append(leader_badge_png)
    for idx, badge in enumerate(badges):
        bx = ax - 4
        by = ay - 4 + idx * (badge_size + 6)
        _paste_badge(bx, by, badge)
    # Weapon icon overlay (top-right of the cell, slightly inside the
    # card so it doesn't clip). Skipped when the weapon has no PNG.
    if weapon_png is not None:
        try:
            with Image.open(io.BytesIO(weapon_png)) as wraw:
                wraw.seek(0)
                w_img = wraw.convert("RGBA")
            if w_img.size != (weapon_size, weapon_size):
                w_img = w_img.resize(
                    (weapon_size, weapon_size), Image.LANCZOS,
                )
            wx = x + cell_w - weapon_size - 8
            wy = y + 6
            # Slight dark disc under the icon for contrast on light emoji.
            ImageDraw.Draw(canvas).ellipse(
                (wx - 2, wy - 2,
                 wx + weapon_size + 2, wy + weapon_size + 2),
                fill=(0, 0, 0),
            )
            canvas.paste(w_img, (wx, wy), w_img)
        except Exception:
            # Any decode / paste error just drops the overlay — the cell
            # is still legible without a weapon badge.
            pass
    # Caption text — wraps to 2 lines under the avatar.
    cap_font = _load_grid_font(caption_font_size)
    caption_clean = _strip_custom_emoji(caption)
    lines = _wrap_caption(
        caption_clean, cap_font, cell_w - 12, max_lines=2,
    )
    draw = ImageDraw.Draw(canvas)
    ty = ay + avatar_size + 8
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=cap_font)
        tw = bbox[2] - bbox[0]
        tx = x + (cell_w - tw) // 2 - bbox[0]
        draw.text((tx, ty), line, fill=_GRID_TEXT, font=cap_font)
        ty += (bbox[3] - bbox[1]) + 2
    # Position under the name — icon takes priority over text so a
    # visual roster stays visual. Falls back to the plain text label
    # when no icon PNG resolved (e.g. position with no icon set, or
    # CDN fetch failed). Both branches skip the line entirely when
    # the assignment has no position, keeping empty rows tight.
    if position_icon_png is not None:
        try:
            with Image.open(io.BytesIO(position_icon_png)) as praw:
                praw.seek(0)
                pimg = praw.convert("RGBA")
            if pimg.size != (position_icon_size, position_icon_size):
                pimg = pimg.resize(
                    (position_icon_size, position_icon_size),
                    Image.LANCZOS,
                )
            # One blank line worth of gap between name and position icon
            # so the two read as separate rows instead of blurring
            # together — matches game-UI conventions.
            ty += max(24, caption_font_size)
            px = x + (cell_w - position_icon_size) // 2
            canvas.paste(pimg, (px, ty), pimg)
        except Exception:
            # Icon decode failed — fall back to the text label so the
            # cell still communicates the position.
            position_icon_png = None
    if position_icon_png is None and position_label:
        pos_font = _load_grid_font(position_font_size)
        pos_lines = _wrap_caption(
            position_label, pos_font, cell_w - 12, max_lines=1,
        )
        ty += 2
        for line in pos_lines:
            bbox = draw.textbbox((0, 0), line, font=pos_font)
            tw = bbox[2] - bbox[0]
            tx = x + (cell_w - tw) // 2 - bbox[0]
            draw.text((tx, ty), line, fill=_GRID_MUTED, font=pos_font)
            ty += (bbox[3] - bbox[1]) + 2


def _render_team_image_sync(
    *,
    team_name: str,
    team_color_hex: str | None,
    header_stats: str,
    main_cells: list[dict],
    reserve_cells: list[dict],
    top_pad: int = 18,
    bottom_pad: int = GRID_DEFAULT_BOTTOM_PAD,
    glass_enabled: bool = False,
    glass_blur: bool = True,
    glass_opacity: int = 80,
) -> bytes:
    """Compose one team's PNG in-process. Runs on a worker thread via
    `asyncio.to_thread` — Pillow releases the GIL for most ops but the
    caller must not tie up the event loop with it.

    `bottom_pad` controls the empty pixel band under the position icon
    inside every cell — plumbed through from
    `GvgGuildSettings.grid_bottom_padding` so guilds can tune whether
    the grid reads tighter or roomier. Clamped to [0, 200] so a
    nonsensical value from a corrupt config can't blow the canvas up.

    Each cell dict carries:
        avatar: PIL.Image (RGB square)
        weapon_png: bytes | None
        caption: str
        seed: int
        is_leader: bool
    """
    if not _HAS_PIL:  # pragma: no cover
        raise RuntimeError("Pillow not installed")

    top_pad = max(0, min(int(top_pad or 0), 200))
    bottom_pad = max(0, min(int(bottom_pad or 0), 200))
    # Reserve strip uses proportionally smaller top pad so the compact
    # cells don't inherit a huge headroom that dwarfs their avatars.
    reserve_top_pad = max(6, top_pad * 2 // 3)
    main_cell_h = GRID_MAIN_CELL_CONTENT_H + top_pad + bottom_pad
    reserve_cell_h = (
        GRID_RESERVE_CELL_CONTENT_H + reserve_top_pad + bottom_pad
    )

    main_rows = max(1, math.ceil(max(1, len(main_cells)) / GRID_COLS))
    reserve_rows = (
        math.ceil(len(reserve_cells) / GRID_COLS) if reserve_cells else 0
    )

    grid_w = (
        GRID_MARGIN * 2
        + GRID_COLS * GRID_CELL_W
        + (GRID_COLS - 1) * GRID_CELL_PADDING
    )
    grid_h = (
        GRID_HEADER_H
        + GRID_MARGIN
        + main_rows * main_cell_h
        + (main_rows - 1) * GRID_CELL_PADDING
        + GRID_MARGIN
    )
    if reserve_cells:
        grid_h += (
            GRID_RESERVE_LABEL_H
            + reserve_rows * reserve_cell_h
            + (reserve_rows - 1) * GRID_CELL_PADDING
            + GRID_MARGIN
        )

    canvas = Image.new("RGB", (grid_w, grid_h), _GRID_BG)
    draw = ImageDraw.Draw(canvas)

    # Header band — team-color background, name + stats.
    header_color = _hex_to_rgb(team_color_hex, (88, 101, 242))  # blurple-ish
    draw.rectangle((0, 0, grid_w, GRID_HEADER_H), fill=header_color)
    title_font = _load_grid_font(GRID_HEADER_TITLE_SIZE)
    stats_font = _load_grid_font(GRID_HEADER_STATS_SIZE)
    title_clean = _strip_custom_emoji(team_name)
    # Vertically center the title/stats inside the header band.
    title_bbox = draw.textbbox((0, 0), title_clean or "Team", font=title_font)
    title_y = (GRID_HEADER_H - (title_bbox[3] - title_bbox[1])) // 2 - title_bbox[1]
    draw.text(
        (GRID_MARGIN, title_y), title_clean or "Team",
        fill=_GRID_HEADER_TEXT, font=title_font,
    )
    if header_stats:
        bbox = draw.textbbox((0, 0), header_stats, font=stats_font)
        tw = bbox[2] - bbox[0]
        stats_y = (GRID_HEADER_H - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text(
            (grid_w - GRID_MARGIN - tw, stats_y),
            header_stats, fill=_GRID_HEADER_TEXT, font=stats_font,
        )

    # MAIN grid.
    origin_y = GRID_HEADER_H + GRID_MARGIN
    for idx, cell in enumerate(main_cells):
        row = idx // GRID_COLS
        col = idx % GRID_COLS
        cx = (
            GRID_MARGIN
            + col * (GRID_CELL_W + GRID_CELL_PADDING)
        )
        cy = origin_y + row * (main_cell_h + GRID_CELL_PADDING)
        _draw_cell(
            canvas, cx, cy,
            avatar=cell["avatar"],
            weapon_png=cell.get("weapon_png"),
            caption=cell.get("caption", ""),
            seed=cell.get("seed", 0),
            cell_h=main_cell_h,
            top_pad=top_pad,
            position_label=cell.get("position_label", ""),
            position_icon_png=cell.get("position_icon_png"),
            is_leader=bool(cell.get("is_leader")),
            leader_badge_png=cell.get("leader_badge_png"),
            cell_bg_color=cell.get("cell_bg_color"),
            cell_bg_alpha=int(cell.get("cell_bg_alpha", 100)),
            glass_enabled=glass_enabled,
            glass_blur=glass_blur,
            glass_opacity=glass_opacity,
        )

    # Reserve strip (compact).
    if reserve_cells:
        rlabel_y = (
            origin_y
            + main_rows * main_cell_h
            + (main_rows - 1) * GRID_CELL_PADDING
            + GRID_MARGIN
        )
        draw.text(
            (GRID_MARGIN, rlabel_y),
            f"Reserve ({len(reserve_cells)})",
            fill=_GRID_MUTED, font=stats_font,
        )
        rgrid_y = rlabel_y + GRID_RESERVE_LABEL_H
        for idx, cell in enumerate(reserve_cells):
            row = idx // GRID_COLS
            col = idx % GRID_COLS
            cx = (
                GRID_MARGIN
                + col * (GRID_RESERVE_CELL_W + GRID_CELL_PADDING)
            )
            cy = rgrid_y + row * (
                reserve_cell_h + GRID_CELL_PADDING
            )
            _draw_cell(
                canvas, cx, cy,
                avatar=cell["avatar"],
                weapon_png=cell.get("weapon_png"),
                caption=cell.get("caption", ""),
                seed=cell.get("seed", 0),
                cell_w=GRID_RESERVE_CELL_W,
                cell_h=reserve_cell_h,
                avatar_size=GRID_RESERVE_AVATAR_SIZE,
                weapon_size=40,
                caption_font_size=GRID_RESERVE_CAPTION_FONT_SIZE,
                position_label=cell.get("position_label", ""),
                position_font_size=14,
                position_icon_png=cell.get("position_icon_png"),
                position_icon_size=56,
                is_leader=bool(cell.get("is_leader")),
                leader_badge_png=cell.get("leader_badge_png"),
                badge_size=30,
                top_pad=reserve_top_pad,
                cell_bg_color=cell.get("cell_bg_color"),
                cell_bg_alpha=int(cell.get("cell_bg_alpha", 100)),
                glass_enabled=glass_enabled,
                glass_blur=glass_blur,
                glass_opacity=glass_opacity,
            )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Confirmation view for destructive actions (SPEC §43). Non-persistent —
# it lives only for the ephemeral message the destructive command posts.
# ---------------------------------------------------------------------------


class ConfirmView(discord.ui.View):
    """Two-button Confirm/Cancel with a per-view result future so the
    caller can `await view.wait_result()`. Only the actor may click."""

    def __init__(self, actor_id: int, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.actor_id = actor_id
        self.value: bool | None = None
        self._done = asyncio.Event()

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This confirmation isn't yours.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Confirm", emoji="🗑️", style=discord.ButtonStyle.danger,
    )
    async def _confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.value = True
        self._done.set()
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True
        await interaction.response.edit_message(
            content="Confirmed — running…", view=self,
        )
        self.stop()

    @discord.ui.button(
        label="Cancel", emoji="✋", style=discord.ButtonStyle.secondary,
    )
    async def _cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.value = False
        self._done.set()
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True
        await interaction.response.edit_message(
            content="Cancelled.", view=self,
        )
        self.stop()

    async def on_timeout(self) -> None:
        self.value = False
        self._done.set()

    async def wait_result(self) -> bool:
        await self._done.wait()
        return bool(self.value)


# ---------------------------------------------------------------------------
# Button-driven Move wizard — two ephemeral, session-scoped Views chained
# together. Non-persistent (nothing to survive a restart; the flow is
# short-lived and initiated from the persistent board's Move button).
# ---------------------------------------------------------------------------


class MoveWizardPickPlayerView(discord.ui.View):
    """Step 1 of the board's Move wizard: pick a player already on the
    match's roster. UserSelect (Discord's native member picker) rather
    than a StringSelect populated with the roster — Discord caps
    StringSelect at 25 options, so on a match with a full 30-player
    MAIN + reserves the tail was silently truncated. The handler
    validates the pick against the match's assignments and (for
    commanders) their own team scope before advancing to step 2."""

    def __init__(
        self,
        cog: "GvG",
        match_id: int,
        actor_id: int,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.match_id = match_id
        self.actor_id = actor_id
        select = discord.ui.UserSelect(
            placeholder="Pick the player to move…",
            min_values=1, max_values=1,
        )
        select.callback = self._on_pick_player  # type: ignore[method-assign]
        self.add_item(select)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This wizard isn't yours.", ephemeral=True,
            )
            return False
        return True

    async def _on_pick_player(self, interaction: discord.Interaction) -> None:
        raw_values = (interaction.data or {}).get("values") or []
        try:
            user_id = int(raw_values[0]) if raw_values else 0
        except (ValueError, TypeError):
            user_id = 0
        await self.cog._move_wizard_present_target(
            interaction, match_id=self.match_id, user_id=user_id,
        )


class MoveWizardTargetView(discord.ui.View):
    """Step 2: pick destination team + (optional) position + (optional)
    roster type, then Submit. Runs the move via the cog on Submit."""

    def __init__(
        self,
        cog: "GvG",
        match_id: int,
        user_id: int,
        actor_id: int,
        team_options: list[discord.SelectOption],
        position_options: list[discord.SelectOption],
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.match_id = match_id
        self.user_id = user_id
        self.actor_id = actor_id
        self.selected_team_id: int | None = None
        # None = keep current; "clear" = clear; else id-as-str.
        self.selected_position: str | None = None
        # None = keep current; else "MAIN" | "RESERVE".
        self.selected_type: str | None = None

        team_sel = discord.ui.Select(
            placeholder="Destination team (required)",
            min_values=1, max_values=1,
            options=team_options[:25],
        )
        team_sel.callback = self._on_team  # type: ignore[method-assign]
        self.add_item(team_sel)

        pos_options = [
            discord.SelectOption(
                label="— keep current —", value="__keep__", default=True,
            ),
            discord.SelectOption(label="— clear position —", value="__clear__"),
        ] + position_options[:23]
        pos_sel = discord.ui.Select(
            placeholder="Destination position",
            min_values=1, max_values=1,
            options=pos_options[:25],
        )
        pos_sel.callback = self._on_position  # type: ignore[method-assign]
        self.add_item(pos_sel)

        type_sel = discord.ui.Select(
            placeholder="Roster type",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label="— keep current —", value="__keep__", default=True,
                ),
                discord.SelectOption(label="MAIN", value="MAIN"),
                discord.SelectOption(label="RESERVE", value="RESERVE"),
            ],
        )
        type_sel.callback = self._on_type  # type: ignore[method-assign]
        self.add_item(type_sel)

        submit = discord.ui.Button(
            label="Submit move",
            style=discord.ButtonStyle.success,
            emoji="🔄",
        )
        submit.callback = self._on_submit  # type: ignore[method-assign]
        self.add_item(submit)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This wizard isn't yours.", ephemeral=True,
            )
            return False
        return True

    async def _on_team(self, interaction: discord.Interaction) -> None:
        raw = ((interaction.data or {}).get("values") or ["0"])[0]  # type: ignore[assignment]
        try:
            self.selected_team_id = int(raw)
        except (ValueError, TypeError):
            self.selected_team_id = None
        await interaction.response.defer()

    async def _on_position(self, interaction: discord.Interaction) -> None:
        raw = ((interaction.data or {}).get("values") or ["__keep__"])[0]  # type: ignore[assignment]
        self.selected_position = raw
        await interaction.response.defer()

    async def _on_type(self, interaction: discord.Interaction) -> None:
        raw = ((interaction.data or {}).get("values") or ["__keep__"])[0]  # type: ignore[assignment]
        self.selected_type = raw if raw in ("MAIN", "RESERVE") else None
        await interaction.response.defer()

    async def _on_submit(self, interaction: discord.Interaction) -> None:
        if self.selected_team_id is None:
            await interaction.response.send_message(
                "Pick a destination team first.", ephemeral=True,
            )
            return
        for c in self.children:
            if isinstance(c, (discord.ui.Button, discord.ui.Select)):
                c.disabled = True
        await interaction.response.edit_message(view=self)
        await self.cog._move_wizard_commit(
            interaction,
            match_id=self.match_id,
            user_id=self.user_id,
            team_id=self.selected_team_id,
            position_choice=self.selected_position,
            new_type=self.selected_type,
        )
        self.stop()


# ---------------------------------------------------------------------------
# Bulk-Move wizard views (ephemeral, in-memory — not persistent).
# ---------------------------------------------------------------------------

_ALL_SENTINEL = "__all__"


class BulkMoveStep1View(discord.ui.View):
    """Step 1: pick the source team.  Presents a StringSelect whose
    options are every team on this match that has at least one player
    assigned.  On pick, transitions to BulkMoveStep2View."""

    def __init__(
        self,
        cog: "GvG",
        match_id: int,
        actor_id: int,
        team_options: list[discord.SelectOption],
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.match_id = match_id
        self.actor_id = actor_id

        src_sel = discord.ui.Select(
            placeholder="Source team (must have players)…",
            min_values=1, max_values=1,
            options=team_options[:25],
        )
        src_sel.callback = self._on_source  # type: ignore[method-assign]
        self.add_item(src_sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This wizard isn't yours.", ephemeral=True,
            )
            return False
        return True

    async def _on_source(self, interaction: discord.Interaction) -> None:
        raw = ((interaction.data or {}).get("values") or ["0"])[0]  # type: ignore[assignment]
        try:
            src_team_id = int(raw)
        except (ValueError, TypeError):
            src_team_id = 0
        await self.cog._bulk_move_step2(
            interaction, match_id=self.match_id,
            actor_id=self.actor_id, src_team_id=src_team_id,
        )


class BulkMoveStep2View(discord.ui.View):
    """Step 2: pick players (multi + ALL option), destination team,
    optional position override, optional roster-type override, then
    Submit.  Five rows: players | destination | position | type | buttons."""

    def __init__(
        self,
        cog: "GvG",
        match_id: int,
        src_team_id: int,
        actor_id: int,
        player_options: list[discord.SelectOption],
        dest_options: list[discord.SelectOption],
        position_options: list[discord.SelectOption],
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.match_id = match_id
        self.src_team_id = src_team_id
        self.actor_id = actor_id
        # state
        self.selected_player_values: list[str] = []
        self.selected_dest_id: int | None = None
        self.selected_position: str | None = None
        self.selected_type: str | None = None

        player_sel = discord.ui.Select(
            placeholder="Players to move (or pick ALL)…",
            min_values=1, max_values=len(player_options),
            options=player_options[:25],
        )
        player_sel.callback = self._on_players  # type: ignore[method-assign]
        self.add_item(player_sel)

        dest_sel = discord.ui.Select(
            placeholder="Destination team (required)…",
            min_values=1, max_values=1,
            options=dest_options[:25],
        )
        dest_sel.callback = self._on_dest  # type: ignore[method-assign]
        self.add_item(dest_sel)

        pos_opts = [
            discord.SelectOption(label="— keep current position —", value="__keep__", default=True),
            discord.SelectOption(label="— clear position —", value="__clear__"),
        ] + position_options[:23]
        pos_sel = discord.ui.Select(
            placeholder="Position override…",
            min_values=1, max_values=1,
            options=pos_opts[:25],
        )
        pos_sel.callback = self._on_position  # type: ignore[method-assign]
        self.add_item(pos_sel)

        type_sel = discord.ui.Select(
            placeholder="Roster type override…",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="— keep current —", value="__keep__", default=True),
                discord.SelectOption(label="MAIN", value="MAIN"),
                discord.SelectOption(label="RESERVE", value="RESERVE"),
            ],
        )
        type_sel.callback = self._on_type  # type: ignore[method-assign]
        self.add_item(type_sel)

        submit_btn = discord.ui.Button(
            label="Move →", style=discord.ButtonStyle.success, emoji="⚡",
        )
        submit_btn.callback = self._on_submit  # type: ignore[method-assign]
        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary,
        )
        cancel_btn.callback = self._on_cancel  # type: ignore[method-assign]
        self.add_item(submit_btn)
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                "This wizard isn't yours.", ephemeral=True,
            )
            return False
        return True

    async def _on_players(self, interaction: discord.Interaction) -> None:
        self.selected_player_values = list(
            (interaction.data or {}).get("values") or []  # type: ignore[arg-type]
        )
        await interaction.response.defer()

    async def _on_dest(self, interaction: discord.Interaction) -> None:
        raw = ((interaction.data or {}).get("values") or ["0"])[0]  # type: ignore[assignment]
        try:
            self.selected_dest_id = int(raw)
        except (ValueError, TypeError):
            self.selected_dest_id = None
        await interaction.response.defer()

    async def _on_position(self, interaction: discord.Interaction) -> None:
        self.selected_position = ((interaction.data or {}).get("values") or ["__keep__"])[0]  # type: ignore[assignment]
        await interaction.response.defer()

    async def _on_type(self, interaction: discord.Interaction) -> None:
        raw = ((interaction.data or {}).get("values") or ["__keep__"])[0]  # type: ignore[assignment]
        self.selected_type = raw if raw in ("MAIN", "RESERVE") else None
        await interaction.response.defer()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        for c in self.children:
            if isinstance(c, (discord.ui.Button, discord.ui.Select)):
                c.disabled = True
        await interaction.response.edit_message(content="_Cancelled._", view=self)
        self.stop()

    async def _on_submit(self, interaction: discord.Interaction) -> None:
        if not self.selected_player_values:
            await interaction.response.send_message(
                "Pick at least one player (or ALL).", ephemeral=True,
            )
            return
        if self.selected_dest_id is None:
            await interaction.response.send_message(
                "Pick a destination team first.", ephemeral=True,
            )
            return
        for c in self.children:
            if isinstance(c, (discord.ui.Button, discord.ui.Select)):
                c.disabled = True
        await interaction.response.edit_message(view=self)
        await self.cog._bulk_move_commit(
            interaction,
            match_id=self.match_id,
            src_team_id=self.src_team_id,
            player_values=self.selected_player_values,
            dest_team_id=self.selected_dest_id,
            position_choice=self.selected_position,
            new_type=self.selected_type,
        )
        self.stop()


# ---------------------------------------------------------------------------
# Persistent DynamicItem stubs for Phase-C roster board.
# ---------------------------------------------------------------------------


class GvgAssignPickerButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:assign:(?P<match_id>\d+)",
):
    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:assign:{match_id}",
                label="Assign Players",
                emoji="➕",
                style=discord.ButtonStyle.success,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_assign(interaction, self.match_id)


class GvgMovePickerButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:move:(?P<match_id>\d+)",
):
    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:move:{match_id}",
                label="Move Players",
                emoji="🔄",
                style=discord.ButtonStyle.primary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_move(interaction, self.match_id)


class GvgBulkMoveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:bulkmove:(?P<match_id>\d+)",
):
    """Board button that opens the Bulk Move wizard — lets managers
    move multiple players (or an entire team) to another team in one
    operation.  Step 1 picks the source team; step 2 picks players,
    destination, optional position override and roster-type override."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:bulkmove:{match_id}",
                label="Bulk Move",
                emoji="⚡",
                style=discord.ButtonStyle.primary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_bulk_move(interaction, self.match_id)


class GvgTeamsButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:teams:(?P<match_id>\d+)",
):
    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:teams:{match_id}",
                label="Teams",
                emoji="👥",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_teams(interaction, self.match_id)


class GvgRefreshButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:refresh:(?P<match_id>\d+)",
):
    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:refresh:{match_id}",
                label="Refresh",
                emoji="🔃",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_refresh(interaction, self.match_id)


class GvgViewRosterButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:view:(?P<match_id>\d+)",
):
    """Ephemeral roster preview for owners / managers. Useful when the
    manager channel is split from the public board — a single click
    surfaces the full roster (in the current view_mode) as a private
    reply, no channel-hop needed. Read-only, doesn't schedule a
    refresh."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:view:{match_id}",
                label="View Roster",
                emoji="👁️",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_view_roster(interaction, self.match_id)


class GvgSelfRegisterButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:selfreg:(?P<match_id>\d+)",
):
    """Public "sign yourself into a team" button. Opens a 3-step
    wizard: pick a team → pick a position → walk through the
    /player-reg dialog. If the user already has a Player row the
    modal comes pre-filled so they can Enter through it (and
    optionally update their weapons); either way the final save
    also creates the roster assignment via a `post_save` hook."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:selfreg:{match_id}",
                label="Register",
                emoji="✍️",
                style=discord.ButtonStyle.primary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_self_register(interaction, self.match_id)


class GvgSelfReserveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:selfreserve:(?P<match_id>\d+)",
):
    """Public "move myself to RESERVE" button. Flips the caller's
    MAIN assignment to RESERVE in place. Refuses cleanly when the
    caller isn't on the roster yet or is already RESERVE. Doesn't
    change team or position — this is a roster-type toggle only,
    kept single-click so it's the fastest way for a member to say
    "I might not make it, treat me as backup"."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:selfreserve:{match_id}",
                label="Reserve",
                emoji="🛌",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_self_reserve(interaction, self.match_id)


class GvgUnRegisterButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:unreg:(?P<match_id>\d+)",
):
    """Public "remove myself from the match" button. Deletes the
    caller's own assignment and drops any Team Leader row they
    held on this match. Refuses cleanly when they aren't on the
    roster or the match is LOCKED."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:unreg:{match_id}",
                label="Un-Register",
                emoji="🚪",
                style=discord.ButtonStyle.danger,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_self_unregister(interaction, self.match_id)


class GvgPositionsButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:positions:(?P<match_id>\d+)",
):
    """Position management panel — same shape as the Teams panel:
    read-only list of positions plus [➕ Add] / [✏️ Edit] action
    buttons that open the corresponding admin modal."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:positions:{match_id}",
                label="Positions",
                emoji="🎯",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_positions(interaction, self.match_id)


class GvgSettingsButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:settings:(?P<match_id>\d+)",
):
    """Admin-only ⚙️ button that opens a modal for the guild's
    GvG settings. Discord modals accept TextInput only — role and
    channel changes still route through the existing `/gvg setup`
    slash commands (native pickers there). This modal covers the
    text-friendly knobs: default MAIN limit, view mode, and the
    commander self-manage toggle."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:settings:{match_id}",
                label="Settings",
                emoji="⚙️",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_settings(interaction, self.match_id)


class GvgSettingsModal(discord.ui.Modal):
    """Popup form for the text-editable GvG settings. Every field is
    optional — a blank input leaves the current value alone. Written
    fields are validated on submit; a nice per-field error is sent
    back to the caller if the input is malformed."""

    def __init__(self, cog: "GvG", cfg: GvgGuildSettings) -> None:
        super().__init__(title="⚙️ GvG Settings")
        self.cog = cog
        self.main_limit_input = discord.ui.TextInput(
            label="Default MAIN player limit (1–500)",
            placeholder="e.g. 30 — blank keeps current",
            required=False,
            max_length=4,
            default=str(cfg.default_main_limit),
        )
        self.view_mode_input = discord.ui.TextInput(
            label="Roster view mode (1 · 2 · 3 · 4)",
            placeholder="1 grouped · 2 flat · 3 horiz · 4 grid",
            required=False,
            max_length=1,
            default=str(cfg.view_mode),
        )
        # Combined icon + cell-background color for Team Leader.
        # Format: `ICON` · `#HEX` · or `ICON / #HEX` for both.
        # Blank field = keep current; "-" part = clear that value.
        # Icon accepts a Discord custom emoji (`<:name:id>`) or unicode.
        _ldr_default = " / ".join(filter(None, [
            cfg.grid_leader_icon or "",
            cfg.grid_leader_bg_hex or "",
        ]))
        self.grid_leader_icon_input = discord.ui.TextInput(
            label="Leader icon / cell color",
            placeholder="🎖️ / #00BFFF  •  icon, #hex, or both",
            required=False,
            max_length=140,
            default=_ldr_default,
        )
        # Combined cell padding: `top / bottom` (or a single number
        # applied to both). Lower = tighter cell; 0-80 each. Kept as
        # one modal row so we fit under Discord's 5-item cap alongside
        # the other grid settings.
        self.grid_padding_input = discord.ui.TextInput(
            label="Grid padding top / bottom (px)",
            placeholder="18 / 10  •  or one value for both",
            required=False,
            max_length=16,
            default=(
                f"{cfg.grid_top_padding} / {cfg.grid_bottom_padding}"
            ),
        )
        self.add_item(self.main_limit_input)
        self.add_item(self.view_mode_input)
        self.add_item(self.grid_leader_icon_input)
        self.add_item(self.grid_padding_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.commit_settings_modal(
            interaction,
            main_limit_raw=str(self.main_limit_input.value or "").strip(),
            view_mode_raw=str(self.view_mode_input.value or "").strip(),
            commander_raw="",
            grid_leader_icon_raw=str(
                self.grid_leader_icon_input.value or "",
            ),
            grid_padding_raw=str(
                self.grid_padding_input.value or "",
            ).strip(),
        )


class GvgGridVisualModal(discord.ui.Modal):
    """Popup form for the glass / frosted-glass visual settings on the
    view_mode=4 avatar grid. Four text inputs:

      1. Glass look on/off
      2. Blur backdrop on/off
      3. Glass card opacity (0-100 %)
      4. Leader color opacity (0-100 %)

    A blank field leaves the current value unchanged.
    """

    def __init__(self, cog: "GvG", cfg: GvgGuildSettings) -> None:
        super().__init__(title="🪟 Grid Visual Settings")
        self.cog = cog
        self.glass_enabled_input = discord.ui.TextInput(
            label="Glass look (on / off)",
            placeholder="on — enables frosted-glass card style",
            required=False,
            max_length=4,
            default="on" if cfg.grid_glass_enabled else "off",
        )
        self.glass_blur_input = discord.ui.TextInput(
            label="Blur backdrop (on / off)",
            placeholder="on — blurs background behind each card",
            required=False,
            max_length=4,
            default="on" if cfg.grid_glass_blur else "off",
        )
        self.glass_opacity_input = discord.ui.TextInput(
            label="Glass card opacity (0–100 %)",
            placeholder="80 — higher = more opaque card",
            required=False,
            max_length=3,
            default=str(cfg.grid_glass_opacity),
        )
        self.leader_alpha_input = discord.ui.TextInput(
            label="Leader color opacity (0–100 %)",
            placeholder="100 — full solid color",
            required=False,
            max_length=3,
            default=str(cfg.grid_leader_bg_alpha),
        )
        self.add_item(self.glass_enabled_input)
        self.add_item(self.glass_blur_input)
        self.add_item(self.glass_opacity_input)
        self.add_item(self.leader_alpha_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.commit_grid_visual_modal(
            interaction,
            glass_enabled_raw=str(self.glass_enabled_input.value or "").strip(),
            glass_blur_raw=str(self.glass_blur_input.value or "").strip(),
            glass_opacity_raw=str(self.glass_opacity_input.value or "").strip(),
            leader_alpha_raw=str(self.leader_alpha_input.value or "").strip(),
        )


# --- Team / Position admin modals ---
#
# Discord modals accept `TextInput` only, so name / description / icon /
# color live here; sort_order + soft-disable stay on the slash surface
# (rarer actions, and Discord caps a modal at 5 fields). Blank field on
# Edit means "clear this value"; leaving the default value means "keep".
# ---------------------------------------------------------------------------
# Named color palette for the post-create color picker (Team / Position).
# Discord modals can't host a StringSelect, so the color choice happens
# in a follow-up ephemeral view right after the create/edit modal closes.
# 24 presets + "🚫 No color" = 25, the StringSelect max.
# ---------------------------------------------------------------------------

_TEAM_COLOR_PRESETS: tuple[tuple[str, str | None, str], ...] = (
    ("Red",           "#E53935", "🟥"),
    ("Crimson",       "#C2185B", "🍒"),
    ("Pink",          "#EC407A", "🌸"),
    ("Magenta",       "#D81B60", "💗"),
    ("Purple",        "#8E24AA", "🟪"),
    ("Deep Purple",   "#5E35B1", "💜"),
    ("Indigo",        "#3949AB", "🔷"),
    ("Blurple",       "#5865F2", "💙"),
    ("Blue",          "#1E88E5", "🟦"),
    ("Sky",           "#039BE5", "🌊"),
    ("Cyan",          "#00ACC1", "💠"),
    ("Teal",          "#00897B", "🟢"),
    ("Green",         "#43A047", "🟩"),
    ("Discord Green", "#57F287", "✅"),
    ("Lime",          "#7CB342", "🍋"),
    ("Yellow",        "#FDD835", "🟨"),
    ("Gold",          "#FEE75C", "🏆"),
    ("Amber",         "#FFB300", "🟠"),
    ("Orange",        "#FB8C00", "🍊"),
    ("Deep Orange",   "#F4511E", "🔥"),
    ("Brown",         "#6D4C41", "🟫"),
    ("Gray",          "#757575", "⚪"),
    ("Blue Grey",     "#546E7A", "🌫️"),
    ("Slate",         "#37474F", "🪨"),
    ("No color",      None,      "🚫"),
)


def _color_select_options() -> list[discord.SelectOption]:
    """Build the StringSelect options list from the palette. `None` hex
    encodes as the sentinel "__NONE__" so the callback can distinguish
    'clear the color' from 'no selection'."""
    opts: list[discord.SelectOption] = []
    for label, hex_value, emoji in _TEAM_COLOR_PRESETS:
        opts.append(discord.SelectOption(
            label=label,
            value=("__NONE__" if hex_value is None else hex_value),
            emoji=emoji,
            description=(hex_value or "clear color"),
        ))
    return opts


class _TeamColorSelect(discord.ui.Select):
    """StringSelect that binds one preset color to a team_id."""

    def __init__(self, cog: "GvG", team_id: int) -> None:
        super().__init__(
            placeholder="Pick a color for this team…",
            min_values=1, max_values=1,
            options=_color_select_options(),
        )
        self.cog = cog
        self.team_id = team_id

    async def callback(self, interaction: discord.Interaction) -> None:
        val = self.values[0]
        color_hex = None if val == "__NONE__" else val
        await self.cog._apply_team_color(
            interaction, self.team_id, color_hex,
        )


class _TeamColorPickerView(discord.ui.View):
    """One-shot ephemeral view carrying the team color picker. Not
    persistent — 5-min timeout is fine because the ephemeral disappears
    if the picker isn't used (the team is already saved without a
    color at that point, matching prior 'no hex typed' behavior)."""

    def __init__(self, cog: "GvG", team_id: int) -> None:
        super().__init__(timeout=300)
        self.add_item(_TeamColorSelect(cog, team_id))


class _PositionColorSelect(discord.ui.Select):
    def __init__(self, cog: "GvG", position_id: int) -> None:
        super().__init__(
            placeholder="Pick a color for this position…",
            min_values=1, max_values=1,
            options=_color_select_options(),
        )
        self.cog = cog
        self.position_id = position_id

    async def callback(self, interaction: discord.Interaction) -> None:
        val = self.values[0]
        color_hex = None if val == "__NONE__" else val
        await self.cog._apply_position_color(
            interaction, self.position_id, color_hex,
        )


class _PositionColorPickerView(discord.ui.View):
    def __init__(self, cog: "GvG", position_id: int) -> None:
        super().__init__(timeout=300)
        self.add_item(_PositionColorSelect(cog, position_id))


class _TeamAddModal(discord.ui.Modal):
    """New team, guild-wide (matches `/gvg team add` semantics)."""

    def __init__(self, cog: "GvG") -> None:
        super().__init__(title="➕ Add Team")
        self.cog = cog
        self.name_in = discord.ui.TextInput(
            label="Team name (required)",
            placeholder="e.g. Alpha", required=True, max_length=64,
        )
        self.desc_in = discord.ui.TextInput(
            label="Description (optional)",
            required=False, max_length=256, style=discord.TextStyle.paragraph,
        )
        self.icon_in = discord.ui.TextInput(
            label="Icon — emoji or <:name:id> (optional)",
            required=False, max_length=64,
        )
        for it in (
            self.name_in, self.desc_in, self.icon_in,
        ):
            self.add_item(it)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Color moved to a follow-up picker view — see commit_team_add.
        await self.cog.commit_team_add(
            interaction,
            name=str(self.name_in.value or "").strip(),
            description=str(self.desc_in.value or "").strip(),
            icon=str(self.icon_in.value or "").strip(),
        )


class _TeamEditModal(discord.ui.Modal):
    """Rename / restyle an existing team. Fields pre-filled with the
    current values. Blank input = clear that field (except name, which
    is required)."""

    def __init__(self, cog: "GvG", trow: GvgTeam) -> None:
        super().__init__(title=f"✏️ Edit Team")
        self.cog = cog
        self.team_id = trow.id
        self.name_in = discord.ui.TextInput(
            label="Team name (required)",
            required=True, max_length=64,
            default=trow.name,
        )
        self.desc_in = discord.ui.TextInput(
            label="Description (blank = clear)",
            required=False, max_length=256, style=discord.TextStyle.paragraph,
            default=trow.description or "",
        )
        self.icon_in = discord.ui.TextInput(
            label="Icon (blank = clear)",
            required=False, max_length=64,
            default=trow.icon or "",
        )
        for it in (
            self.name_in, self.desc_in, self.icon_in,
        ):
            self.add_item(it)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Color is picked in a follow-up view — see commit_team_edit.
        await self.cog.commit_team_edit(
            interaction,
            team_id=self.team_id,
            name=str(self.name_in.value or "").strip(),
            description=str(self.desc_in.value or "").strip(),
            icon=str(self.icon_in.value or "").strip(),
        )


class _PositionAddModal(discord.ui.Modal):
    """New position, guild-wide (matches `/gvg position add`)."""

    def __init__(self, cog: "GvG") -> None:
        super().__init__(title="➕ Add Position")
        self.cog = cog
        self.name_in = discord.ui.TextInput(
            label="Position name (required)",
            placeholder="e.g. DPS / Tank / Healer",
            required=True, max_length=64,
        )
        self.desc_in = discord.ui.TextInput(
            label="Description (optional)",
            required=False, max_length=256, style=discord.TextStyle.paragraph,
        )
        self.icon_in = discord.ui.TextInput(
            label="Icon — emoji or <:name:id> (optional)",
            required=False, max_length=64,
        )
        for it in (
            self.name_in, self.desc_in, self.icon_in,
        ):
            self.add_item(it)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Color moved to a follow-up picker view — see commit_position_add.
        await self.cog.commit_position_add(
            interaction,
            name=str(self.name_in.value or "").strip(),
            description=str(self.desc_in.value or "").strip(),
            icon=str(self.icon_in.value or "").strip(),
        )


class _PositionEditModal(discord.ui.Modal):
    """Rename / restyle an existing position."""

    def __init__(self, cog: "GvG", prow: GvgPosition) -> None:
        super().__init__(title="✏️ Edit Position")
        self.cog = cog
        self.position_id = prow.id
        self.name_in = discord.ui.TextInput(
            label="Position name (required)",
            required=True, max_length=64, default=prow.name,
        )
        self.desc_in = discord.ui.TextInput(
            label="Description (blank = clear)",
            required=False, max_length=256, style=discord.TextStyle.paragraph,
            default=prow.description or "",
        )
        self.icon_in = discord.ui.TextInput(
            label="Icon (blank = clear)",
            required=False, max_length=64,
            default=prow.icon or "",
        )
        for it in (
            self.name_in, self.desc_in, self.icon_in,
        ):
            self.add_item(it)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Color is picked in a follow-up view — see commit_position_edit.
        await self.cog.commit_position_edit(
            interaction,
            position_id=self.position_id,
            name=str(self.name_in.value or "").strip(),
            description=str(self.desc_in.value or "").strip(),
            icon=str(self.icon_in.value or "").strip(),
        )


# UserSelect that powers `/gvg roster assign` and the "Add more players"
# extension flow. Custom_id encodes the target so the interaction router
# can rebuild it after a restart (SPEC §31).
class GvgRosterUserSelect(
    discord.ui.DynamicItem[discord.ui.UserSelect],
    template=(
        r"gvg:pick:(?P<match_id>\d+):(?P<team_id>\d+)"
        r":(?P<position_id>\d+):(?P<roster_type>MAIN|RESERVE)"
    ),
):
    def __init__(
        self,
        match_id: int,
        team_id: int,
        position_id: int,
        roster_type: str,
        max_values: int,
    ) -> None:
        self.match_id = match_id
        self.team_id = team_id
        self.position_id = position_id
        self.roster_type = roster_type
        super().__init__(
            discord.ui.UserSelect(
                custom_id=(
                    f"gvg:pick:{match_id}:{team_id}:{position_id}:{roster_type}"
                ),
                placeholder=f"Select up to {max_values} player(s)",
                min_values=1,
                max_values=max_values,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(
            int(match["match_id"]),
            int(match["team_id"]),
            int(match["position_id"]),
            match["roster_type"],
            max_values=25,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_roster_pick(
            interaction,
            match_id=self.match_id,
            team_id=self.team_id,
            position_id=self.position_id or None,
            roster_type=self.roster_type,
            users=list(self.item.values),  # type: ignore[union-attr]
        )


class GvgAddMoreButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"gvg:more:(?P<match_id>\d+):(?P<team_id>\d+)"
        r":(?P<position_id>\d+):(?P<roster_type>MAIN|RESERVE)"
    ),
):
    """[Add More Players] — reopens the picker with the same target."""

    def __init__(
        self,
        match_id: int,
        team_id: int,
        position_id: int,
        roster_type: str,
    ) -> None:
        self.match_id = match_id
        self.team_id = team_id
        self.position_id = position_id
        self.roster_type = roster_type
        super().__init__(
            discord.ui.Button(
                custom_id=(
                    f"gvg:more:{match_id}:{team_id}:{position_id}:{roster_type}"
                ),
                label="Add More Players",
                emoji="➕",
                style=discord.ButtonStyle.primary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(
            int(match["match_id"]),
            int(match["team_id"]),
            int(match["position_id"]),
            match["roster_type"],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.reopen_assign_picker(
            interaction,
            match_id=self.match_id,
            team_id=self.team_id,
            position_id=self.position_id or None,
            roster_type=self.roster_type,
        )


# Persistent StringSelect that turns the [Assign Players] board button into
# a two-step wizard: pick a team → then get a UserSelect for that team as
# MAIN with no position. Fully persistent (survives restart) because the
# board itself is a persistent message.
class GvgAssignTeamSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"gvg:board:assign_team:(?P<match_id>\d+)",
):
    def __init__(
        self, match_id: int, options: list[discord.SelectOption] | None = None,
    ) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Select(
                custom_id=f"gvg:board:assign_team:{match_id}",
                placeholder="Pick a team to assign players to…",
                min_values=1, max_values=1,
                options=(options or [
                    discord.SelectOption(label="(no teams)", value="0"),
                ])[:25],
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        raw = (self.item.values or ["0"])[0]  # type: ignore[union-attr]
        try:
            team_id = int(raw)
        except ValueError:
            team_id = 0
        await cog.handle_board_assign_pick_team(
            interaction, match_id=self.match_id, team_id=team_id,
        )


# Persistent StringSelect for the third step of the Assign wizard —
# after a team is picked, pick a position (or "no position") before
# opening the UserSelect. Custom_id encodes both match + team so the
# router can rebuild it after a restart.
class GvgAssignPositionSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"gvg:board:assign_pos:(?P<match_id>\d+):(?P<team_id>\d+)",
):
    def __init__(
        self,
        match_id: int,
        team_id: int,
        options: list[discord.SelectOption] | None = None,
    ) -> None:
        self.match_id = match_id
        self.team_id = team_id
        super().__init__(
            discord.ui.Select(
                custom_id=f"gvg:board:assign_pos:{match_id}:{team_id}",
                placeholder="Pick a position (or no position)…",
                min_values=1, max_values=1,
                options=(options or [
                    discord.SelectOption(label="(no positions)", value="0"),
                ])[:25],
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]), int(match["team_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        raw = (self.item.values or ["0"])[0]  # type: ignore[union-attr]
        try:
            position_id = int(raw)
        except ValueError:
            position_id = 0
        await cog.handle_board_assign_pick_position(
            interaction,
            match_id=self.match_id,
            team_id=self.team_id,
            position_id=position_id,
        )


class GvgSetLeaderButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:leader:(?P<match_id>\d+)",
):
    """Board button that opens the two-step Team Leader wizard —
    pick a team, then pick one of the match's players (any team) to
    nominate as leader (or clear the current one)."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:leader:{match_id}",
                label="Set Team Leader",
                emoji="🎖️",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_leader(interaction, self.match_id)


# --- Assign Reserve — the RESERVE-flavored twin of the assign wizard ---
#
# Same 3-step flow (team → position → user pick) as the MAIN assign
# wizard, but the target roster_type is RESERVE. The reason for
# separate dynamic-item classes (rather than encoding roster_type in
# the existing custom_ids) is discord.py's DynamicItem template
# regex — the MAIN templates are already live on published boards, so
# changing them would strand old messages. Parallel classes keep
# both flows fully persistent.
class GvgAssignReservePickerButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:assign_r:(?P<match_id>\d+)",
):
    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:assign_r:{match_id}",
                label="Assign Reserve",
                emoji="🛌",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_assign(
            interaction, self.match_id, roster_type="RESERVE",
        )


class GvgAssignReserveTeamSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"gvg:board:assign_r_team:(?P<match_id>\d+)",
):
    def __init__(
        self, match_id: int, options: list[discord.SelectOption] | None = None,
    ) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Select(
                custom_id=f"gvg:board:assign_r_team:{match_id}",
                placeholder="Pick a team to add RESERVE players to…",
                min_values=1, max_values=1,
                options=(options or [
                    discord.SelectOption(label="(no teams)", value="0"),
                ])[:25],
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        raw = (self.item.values or ["0"])[0]  # type: ignore[union-attr]
        try:
            team_id = int(raw)
        except ValueError:
            team_id = 0
        await cog.handle_board_assign_pick_team(
            interaction, match_id=self.match_id, team_id=team_id,
            roster_type="RESERVE",
        )


class GvgAssignReservePositionSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"gvg:board:assign_r_pos:(?P<match_id>\d+):(?P<team_id>\d+)",
):
    def __init__(
        self,
        match_id: int,
        team_id: int,
        options: list[discord.SelectOption] | None = None,
    ) -> None:
        self.match_id = match_id
        self.team_id = team_id
        super().__init__(
            discord.ui.Select(
                custom_id=f"gvg:board:assign_r_pos:{match_id}:{team_id}",
                placeholder="Pick a position (or no position)…",
                min_values=1, max_values=1,
                options=(options or [
                    discord.SelectOption(label="(no positions)", value="0"),
                ])[:25],
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]), int(match["team_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        raw = (self.item.values or ["0"])[0]  # type: ignore[union-attr]
        try:
            position_id = int(raw)
        except ValueError:
            position_id = 0
        await cog.handle_board_assign_pick_position(
            interaction,
            match_id=self.match_id,
            team_id=self.team_id,
            position_id=position_id,
            roster_type="RESERVE",
        )


class GvgLeaderTeamSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"gvg:board:leader_team:(?P<match_id>\d+)",
):
    """Step 1 of the Team Leader wizard — pick which team's leader
    slot to edit."""

    def __init__(
        self,
        match_id: int,
        options: list[discord.SelectOption] | None = None,
    ) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Select(
                custom_id=f"gvg:board:leader_team:{match_id}",
                placeholder="Pick a team to set / clear its leader…",
                min_values=1, max_values=1,
                options=(options or [
                    discord.SelectOption(label="(no teams)", value="0"),
                ])[:25],
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        raw = (self.item.values or ["0"])[0]  # type: ignore[union-attr]
        try:
            team_id = int(raw)
        except ValueError:
            team_id = 0
        await cog.handle_board_leader_pick_team(
            interaction, match_id=self.match_id, team_id=team_id,
        )


class GvgLeaderUserSelect(
    discord.ui.DynamicItem[discord.ui.UserSelect],
    template=(
        r"gvg:board:leader_user:(?P<match_id>\d+):(?P<team_id>\d+)"
    ),
):
    """Step 2 of the Team Leader wizard — Discord's native user picker
    (searches every guild member). The handler validates that the
    picked user is on the match roster before writing the leader row.

    UserSelect over a StringSelect: Discord caps a StringSelect at 25
    options, so a curated candidate list would silently truncate on
    matches with more than ~24 assigned players. UserSelect has no
    such cap because it delegates to the client's member search."""

    def __init__(self, match_id: int, team_id: int) -> None:
        self.match_id = match_id
        self.team_id = team_id
        super().__init__(
            discord.ui.UserSelect(
                custom_id=f"gvg:board:leader_user:{match_id}:{team_id}",
                placeholder="Pick the team leader…",
                min_values=1, max_values=1,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]), int(match["team_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        users = list(self.item.values or [])  # type: ignore[union-attr]
        if not users:
            return
        await cog.handle_board_leader_pick_user(
            interaction,
            match_id=self.match_id, team_id=self.team_id,
            choice=str(users[0].id),
        )


class GvgLeaderClearButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"gvg:board:leader_clear:(?P<match_id>\d+):(?P<team_id>\d+)"
    ),
):
    """Removes the currently-set team leader for (match, team).
    Rendered next to the UserSelect only when a leader already exists,
    so the manager can clear without needing to pick a new one first."""

    def __init__(self, match_id: int, team_id: int) -> None:
        self.match_id = match_id
        self.team_id = team_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:leader_clear:{match_id}:{team_id}",
                label="Clear current leader",
                emoji="🎗️",
                style=discord.ButtonStyle.danger,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]), int(match["team_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_leader_pick_user(
            interaction,
            match_id=self.match_id, team_id=self.team_id,
            choice="__clear__",
        )


class GvgRemovePickerButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gvg:board:remove:(?P<match_id>\d+)",
):
    """Board button that opens the Remove Players wizard — a
    UserSelect (multi) whose handler validates the picks are on the
    match roster, then deletes their assignments (with SPEC §43
    commander-clear confirmation)."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gvg:board:remove:{match_id}",
                label="Remove Players",
                emoji="🗑️",
                style=discord.ButtonStyle.danger,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_remove(interaction, self.match_id)


class GvgRemoveUserSelect(
    discord.ui.DynamicItem[discord.ui.UserSelect],
    template=r"gvg:board:remove_user:(?P<match_id>\d+)",
):
    """Multi-pick UserSelect for the Remove Players wizard. The
    handler validates every pick is on the match roster before it
    runs the removal — otherwise it refuses cleanly rather than
    partial-write.

    UserSelect over StringSelect for the same reason as leader and
    move: no 25-option cap, and it feels native for a "pick people"
    action."""

    def __init__(self, match_id: int) -> None:
        self.match_id = match_id
        super().__init__(
            discord.ui.UserSelect(
                custom_id=f"gvg:board:remove_user:{match_id}",
                placeholder="Pick player(s) to remove…",
                min_values=1, max_values=25,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # type: ignore[override]
        return cls(int(match["match_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GvG | None" = interaction.client.get_cog("GvG")  # type: ignore[assignment]
        if cog is None:
            return
        users = list(self.item.values or [])  # type: ignore[union-attr]
        if not users:
            return
        await cog.handle_board_remove_commit(
            interaction,
            match_id=self.match_id,
            user_ids=[u.id for u in users],
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class GvG(commands.Cog, name="GvG"):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        # Per-match debounce for auto-refresh. Each mutation schedules
        # one entry; if another mutation lands before the timer fires,
        # the pending task is cancelled and re-armed so batches collapse
        # to a single edit (SPEC §34).
        self._refresh_tasks: dict[int, asyncio.Task] = {}
        # Weapon-emoji PNG cache for the view_mode=4 grid renderer.
        # Maps emoji_id → bytes (or None = negative cache after a failed
        # fetch, so we don't hammer the CDN on every refresh). Lifetime
        # is the process — a bot restart re-fetches on demand.
        self._grid_emoji_cache: dict[int, bytes | None] = {}
        # Second cache, keyed on the unicode emoji string, for Twemoji
        # PNG fetches used by the corner-badge renderer. Kept separate
        # because the id-keyed cache can't distinguish '👑' from '🎖️'.
        self._grid_unicode_emoji_cache: dict[str, bytes | None] = {}
        # Shared aiohttp session for CDN emoji fetches. Lazily built the
        # first time the grid renderer runs; disposed in cog_unload.
        self._grid_http: object | None = None

    # /gvg root group
    gvg = app_commands.Group(
        name="gvg",
        description="Guild War roster management (30-player + reserve)",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    setup_g = app_commands.Group(
        name="setup",
        description="Configure GvG for this Discord guild",
        parent=gvg,
    )
    position_g = app_commands.Group(
        name="position",
        description="Global positions (Tank / DPS / Healer …)",
        parent=gvg,
    )
    match_g = app_commands.Group(
        name="match",
        description="Manage Guild War matches",
        parent=gvg,
    )
    team_g = app_commands.Group(
        name="team",
        description="Manage teams inside a match",
        parent=gvg,
    )
    roster_g = app_commands.Group(
        name="roster",
        description="Assign / move / swap / view players",
        parent=gvg,
    )
    player_g = app_commands.Group(
        name="player",
        description="Player profile: display name / icon / color / note",
        parent=gvg,
    )
    audit_g = app_commands.Group(
        name="audit",
        description="Inspect the GvG audit log (SPEC §41)",
        parent=gvg,
    )
    avatar_g = app_commands.Group(
        name="avatar",
        description="Cache Discord avatars for the roster grid renderer",
        parent=gvg,
    )

    # ---------------- Authorization (SPEC §35-§36) ----------------

    async def _settings(self, guild_id: int) -> GvgGuildSettings:
        async with self.bot.db.session() as s:
            row = await s.get(GvgGuildSettings, guild_id)
            if row is None:
                row = GvgGuildSettings(guild_id=guild_id)
                s.add(row)
                await s.commit()
                await s.refresh(row)
            return row

    async def is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member,
        ):
            return False
        member = interaction.user
        if self.bot.is_owner(member.id, interaction.guild.owner_id):
            return True
        if member.guild_permissions.administrator:
            return True
        settings = await self._settings(interaction.guild.id)
        if settings.admin_role_id is not None:
            if any(r.id == settings.admin_role_id for r in member.roles):
                return True
        return False

    async def is_manager(self, interaction: discord.Interaction) -> bool:
        if await self.is_admin(interaction):
            return True
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member,
        ):
            return False
        if interaction.user.guild_permissions.manage_guild:
            return True
        settings = await self._settings(interaction.guild.id)
        if settings.manager_role_id is not None:
            if any(r.id == settings.manager_role_id for r in interaction.user.roles):
                return True
        return False

    async def require_manager(self, interaction: discord.Interaction) -> None:
        if not await self.is_manager(interaction):
            raise PermissionError(
                "You need the GvG Manager role (or Manage Server / Administrator)."
            )

    async def require_admin(self, interaction: discord.Interaction) -> None:
        if not await self.is_admin(interaction):
            raise PermissionError(
                "You need the GvG Admin role (or Administrator)."
            )

    async def _actor_can_command_team(
        self,
        interaction: discord.Interaction,
        team: GvgTeam,
    ) -> bool:
        """SPEC §35 TEAM_COMMANDER tier: the commander of a team may
        manage that team's roster when `allow_commander_roster_management`
        is on. Returns False if the actor isn't the commander of this
        team or the guild setting is off; callers must fall back to
        `require_manager` for cross-team actions."""
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member,
        ):
            return False
        if team.commander_user_id != interaction.user.id:
            return False
        cfg = await self._settings(interaction.guild.id)
        return bool(cfg.allow_commander_roster_management)

    async def require_manager_or_own_team_commander(
        self,
        interaction: discord.Interaction,
        team: GvgTeam,
    ) -> None:
        """Roster mutations that stay within one team accept either a
        manager or the team's own commander (when the guild allows it)."""
        if await self.is_manager(interaction):
            return
        if await self._actor_can_command_team(interaction, team):
            return
        raise PermissionError(
            "Only the team commander (with commander self-manage on) or a "
            "GvG Manager can change this team's roster."
        )

    # ---------------- Audit + log-channel forwarding ----------------

    async def _audit(
        self,
        *,
        guild_id: int,
        match_id: int | None,
        actor_user_id: int,
        action: str,
        entity_type: str,
        entity_id: int | None = None,
        before: dict | None = None,
        after: dict | None = None,
    ) -> None:
        try:
            async with self.bot.db.session() as s:
                s.add(GvgAuditLog(
                    guild_id=guild_id,
                    match_id=match_id,
                    actor_user_id=actor_user_id,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    before_json=json.dumps(before) if before is not None else None,
                    after_json=json.dumps(after) if after is not None else None,
                ))
                await s.commit()
        except Exception:
            log.exception("gvg audit write failed (%s/%s)", action, entity_type)
        # Fan out to the guild's log channel — best-effort, one summary
        # per audit row (batch operations already write one row, per
        # SPEC §42, so we don't spam per player).
        try:
            await self._forward_audit_to_log_channel(
                guild_id=guild_id, match_id=match_id,
                actor_user_id=actor_user_id, action=action,
                entity_type=entity_type, entity_id=entity_id,
                before=before, after=after,
            )
        except Exception:
            log.exception(
                "gvg audit forward failed (%s/%s)", action, entity_type,
            )

    async def _forward_audit_to_log_channel(
        self,
        *,
        guild_id: int,
        match_id: int | None,
        actor_user_id: int,
        action: str,
        entity_type: str,
        entity_id: int | None,
        before: dict | None,
        after: dict | None,
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        cfg = await self._settings(guild_id)
        if cfg.log_channel_id is None:
            return
        channel = guild.get_channel(cfg.log_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(cfg.log_channel_id)
            except (discord.NotFound, discord.Forbidden):
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        embed = await self._build_audit_embed(
            guild=guild, match_id=match_id, actor_user_id=actor_user_id,
            action=action, entity_type=entity_type, entity_id=entity_id,
            before=before, after=after,
        )
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            log.debug("gvg log-channel send failed: %s", e)

    async def _build_audit_embed(
        self,
        *,
        guild: discord.Guild,
        match_id: int | None,
        actor_user_id: int,
        action: str,
        entity_type: str,
        entity_id: int | None,
        before: dict | None,
        after: dict | None,
    ) -> discord.Embed:
        # Match name for context (best-effort).
        match_name = None
        if match_id is not None:
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is not None and mrow.guild_id == guild.id:
                    match_name = mrow.name
        icon, title = _AUDIT_LABELS.get(
            action, ("📝", action.replace("_", " ").title()),
        )
        color = _AUDIT_COLORS.get(action, discord.Color.blurple())
        embed = discord.Embed(
            title=f"{icon} {title}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="By", value=f"<@{actor_user_id}>", inline=True,
        )
        if match_name is not None:
            embed.add_field(name="Match", value=match_name, inline=True)
        # Batch summary for PLAYERS_ASSIGNED / UNASSIGNED / MOVED / SWAPPED.
        if after and "user_ids" in after:
            uids = list(after.get("user_ids") or [])
            if uids:
                lines = [f"• <@{u}>" for u in uids[:20]]
                if len(uids) > 20:
                    lines.append(f"… (+{len(uids) - 20} more)")
                embed.add_field(
                    name=f"Players ({len(uids)})",
                    value="\n".join(lines)[:1024],
                    inline=False,
                )
        # Team / roster / position context when the payload has it.
        ctx_lines: list[str] = []
        if after:
            if "team" in after:
                ctx_lines.append(f"**Team:** {after['team']}")
            if "roster" in after:
                ctx_lines.append(f"**Roster:** {after['roster']}")
            if "user_id" in after:
                ctx_lines.append(f"**Target:** <@{after['user_id']}>")
        if before and "user_ids" in before and not (
            after and "user_ids" in after
        ):
            uids = list(before.get("user_ids") or [])
            if uids:
                lines = [f"• <@{u}>" for u in uids[:20]]
                if len(uids) > 20:
                    lines.append(f"… (+{len(uids) - 20} more)")
                embed.add_field(
                    name=f"Players ({len(uids)})",
                    value="\n".join(lines)[:1024],
                    inline=False,
                )
        if ctx_lines:
            embed.add_field(
                name="Context", value="\n".join(ctx_lines)[:1024],
                inline=False,
            )
        # Main count on assign/unassign so managers see the running total.
        if match_id is not None and action in (
            "PLAYERS_ASSIGNED", "PLAYERS_UNASSIGNED",
            "PLAYERS_MOVED", "PLAYERS_SWAPPED",
        ):
            try:
                async with self.bot.db.session() as s:
                    row = await s.scalar(
                        select(GvgMatch).where(GvgMatch.id == match_id)
                    )
                    if row is not None:
                        main_count = (await s.execute(
                            select(func.count(GvgRosterAssignment.id))
                            .where(
                                GvgRosterAssignment.match_id == match_id,
                                GvgRosterAssignment.roster_type == "MAIN",
                            )
                        )).scalar_one()
                        embed.set_footer(
                            text=f"Main roster: {main_count}/{row.main_player_limit}",
                        )
            except Exception:
                pass
        return embed

    # ---------------- /gvg setup ----------------

    @setup_g.command(
        name="config",
        description="Configure GvG roles, roster channel, log channel, main limit",
    )
    @app_commands.describe(
        manager_role="Role that acts as GvG Manager (in addition to Manage Server)",
        admin_role="Role that acts as GvG Admin (in addition to Administrator)",
        roster_channel="Channel where the published roster board lives",
        log_channel="Channel to receive audit-log messages",
        manager_channel=(
            "Optional channel for the admin buttons (Assign/Move/…) — "
            "the ✍️ Register button always stays on the public board. "
            "Gate the manager channel via Discord's own permissions."
        ),
        main_limit="Default MAIN player limit per match (default 30, max 500)",
        allow_commander_management="Allow Team Commanders to manage their own team",
    )
    async def setup_config(
        self,
        interaction: discord.Interaction,
        manager_role: discord.Role | None = None,
        admin_role: discord.Role | None = None,
        roster_channel: discord.TextChannel | None = None,
        log_channel: discord.TextChannel | None = None,
        manager_channel: discord.TextChannel | None = None,
        main_limit: app_commands.Range[int, 1, 500] | None = None,
        allow_commander_management: bool | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_admin(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.get(GvgGuildSettings, interaction.guild.id)
                if row is None:
                    row = GvgGuildSettings(guild_id=interaction.guild.id)
                    s.add(row)
                if manager_role is not None:
                    row.manager_role_id = manager_role.id
                if admin_role is not None:
                    row.admin_role_id = admin_role.id
                if roster_channel is not None:
                    row.roster_channel_id = roster_channel.id
                if log_channel is not None:
                    row.log_channel_id = log_channel.id
                if manager_channel is not None:
                    row.manager_channel_id = manager_channel.id
                if main_limit is not None:
                    row.default_main_limit = int(main_limit)
                if allow_commander_management is not None:
                    row.allow_commander_roster_management = bool(
                        allow_commander_management,
                    )
                await s.commit()
                cfg = await s.get(GvgGuildSettings, interaction.guild.id)
            assert cfg is not None
            await self._audit(
                guild_id=cfg.guild_id, match_id=None,
                actor_user_id=interaction.user.id,
                action="SETTINGS_UPDATED", entity_type="GvgGuildSettings",
            )
            await interaction.followup.send(
                embed=self._settings_embed(interaction.guild, cfg),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    @setup_g.command(
        name="permissions",
        description="Toggle team-commander self-manage on/off (SPEC §35)",
    )
    @app_commands.describe(
        commander_self_manage=(
            "If ON, team commanders can add/remove/move/swap players inside "
            "their own team without a Manager role."
        ),
    )
    async def setup_permissions(
        self,
        interaction: discord.Interaction,
        commander_self_manage: bool,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_admin(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.get(GvgGuildSettings, interaction.guild.id)
                if row is None:
                    row = GvgGuildSettings(guild_id=interaction.guild.id)
                    s.add(row)
                row.allow_commander_roster_management = bool(commander_self_manage)
                await s.commit()
                cfg = await s.get(GvgGuildSettings, interaction.guild.id)
            assert cfg is not None
            await self._audit(
                guild_id=cfg.guild_id, match_id=None,
                actor_user_id=interaction.user.id,
                action="SETTINGS_UPDATED", entity_type="GvgGuildSettings",
                after={
                    "allow_commander_roster_management": (
                        cfg.allow_commander_roster_management
                    ),
                },
            )
            await interaction.followup.send(
                embed=self._settings_embed(interaction.guild, cfg),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    @setup_g.command(
        name="channel",
        description="Shortcut for `/gvg setup config roster_channel:#…`",
    )
    @app_commands.describe(channel="Channel where the published roster board lives")
    async def setup_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_admin(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.get(GvgGuildSettings, interaction.guild.id)
                if row is None:
                    row = GvgGuildSettings(guild_id=interaction.guild.id)
                    s.add(row)
                row.roster_channel_id = channel.id
                await s.commit()
                cfg = await s.get(GvgGuildSettings, interaction.guild.id)
            assert cfg is not None
            await self._audit(
                guild_id=cfg.guild_id, match_id=None,
                actor_user_id=interaction.user.id,
                action="SETTINGS_UPDATED", entity_type="GvgGuildSettings",
                after={"roster_channel_id": channel.id},
            )
            await send_ok(interaction, f"✅ Roster board channel set to {channel.mention}.")
        except Exception as e:
            await error_reply(interaction, e)

    @setup_g.command(
        name="log-channel",
        description="Shortcut for `/gvg setup config log_channel:#…`",
    )
    @app_commands.describe(channel="Channel to receive GvG audit-log messages")
    async def setup_log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_admin(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.get(GvgGuildSettings, interaction.guild.id)
                if row is None:
                    row = GvgGuildSettings(guild_id=interaction.guild.id)
                    s.add(row)
                row.log_channel_id = channel.id
                await s.commit()
                cfg = await s.get(GvgGuildSettings, interaction.guild.id)
            assert cfg is not None
            await self._audit(
                guild_id=cfg.guild_id, match_id=None,
                actor_user_id=interaction.user.id,
                action="SETTINGS_UPDATED", entity_type="GvgGuildSettings",
                after={"log_channel_id": channel.id},
            )
            await send_ok(interaction, f"✅ Audit log channel set to {channel.mention}.")
        except Exception as e:
            await error_reply(interaction, e)

    @setup_g.command(
        name="manager-channel",
        description=(
            "Move admin buttons off the board into a manager-only "
            "channel (Register stays on the board)"
        ),
    )
    @app_commands.describe(
        channel=(
            "Manager-only channel for the button card (leave empty to "
            "un-set and put the buttons back on the public board)"
        ),
    )
    async def setup_manager_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Set the channel that hosts the compact "Manager controls"
        card with the four action buttons. Gate visibility with
        Discord's own channel permissions (allow Owner + Manager role
        View Channel; deny @everyone). Set to blank / omit to put the
        buttons back on the public roster board."""
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_admin(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.get(GvgGuildSettings, interaction.guild.id)
                if row is None:
                    row = GvgGuildSettings(guild_id=interaction.guild.id)
                    s.add(row)
                row.manager_channel_id = channel.id if channel else None
                await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="SETTINGS_UPDATED", entity_type="GvgGuildSettings",
                after={
                    "manager_channel_id": (channel.id if channel else None),
                },
            )
            # Refresh every published match so the new layout takes
            # effect (buttons move to / return from the manager channel).
            async with self.bot.db.session() as s:
                published_mids = list((await s.execute(
                    select(GvgRosterMessage.match_id)
                    .join(
                        GvgMatch,
                        GvgMatch.id == GvgRosterMessage.match_id,
                    )
                    .where(GvgMatch.guild_id == interaction.guild.id)
                    .distinct()
                )).scalars().all())
            for mid in published_mids:
                self._schedule_refresh(interaction.guild.id, mid)
            if channel is not None:
                await send_ok(interaction, (
                    f"✅ Manager controls channel set to {channel.mention}.\n"
                    "Reminder: make sure that channel is visible only "
                    "to the roles you want to manage GvG (Discord's "
                    "own **View Channel** permission). The ✍️ Register "
                    "button stays on the public board so members can "
                    "still self-sign-up."
                ))
            else:
                await send_ok(interaction, (
                    "✅ Manager controls channel cleared. All admin "
                    "buttons return to the public roster board on the "
                    "next refresh."
                ))
        except Exception as e:
            await error_reply(interaction, e)

    @setup_g.command(
        name="view-mode",
        description="Switch roster board layout: 1 grouped / 2 flat / 3 horizontal / 4 avatar grid",
    )
    @app_commands.describe(
        mode=(
            "1 grouped, 2 flat, 3 horizontal, 4 image grid (avatars, "
            "run /gvg avatar sync first)"
        ),
    )
    async def setup_view_mode(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Range[int, 1, 4],
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_admin(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.get(GvgGuildSettings, interaction.guild.id)
                if row is None:
                    row = GvgGuildSettings(guild_id=interaction.guild.id)
                    s.add(row)
                before = row.view_mode
                row.view_mode = int(mode)
                await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="SETTINGS_UPDATED", entity_type="GvgGuildSettings",
                before={"view_mode": before},
                after={"view_mode": int(mode)},
            )
            # Refresh every published match so the change is visible
            # immediately without waiting for the next mutation.
            async with self.bot.db.session() as s:
                published_ids = list((await s.execute(
                    select(GvgMatch.id).where(
                        GvgMatch.guild_id == interaction.guild.id,
                        GvgMatch.status.in_(("PUBLISHED", "LOCKED")),
                    )
                )).scalars().all())
            for mid in published_ids:
                self._schedule_refresh(interaction.guild.id, mid)
            mode_label = {
                1: "grouped per position",
                2: "flat one-line-per-player",
                3: "compact horizontal (inline fields)",
                4: "avatar grid (image)",
            }.get(int(mode), str(int(mode)))
            await send_ok(
                interaction,
                (
                    f"✅ Roster view mode set to **{int(mode)}** "
                    f"({mode_label}). "
                    f"Refreshing {len(published_ids)} board(s)…"
                ),
            )
        except Exception as e:
            await error_reply(interaction, e)

    @setup_g.command(
        name="grid",
        description=(
            "Configure glass / frosted-glass visual style for the "
            "view_mode=4 avatar grid"
        ),
    )
    async def setup_grid(self, interaction: discord.Interaction) -> None:
        """Open the glass / frosted-glass visual settings modal for the
        avatar grid (glass look, blur backdrop, opacity values)."""
        try:
            await self.require_manager(interaction)
            assert interaction.guild is not None
            cfg = await self._settings(interaction.guild.id)
            await interaction.response.send_modal(
                GvgGridVisualModal(self, cfg),
            )
        except Exception as e:
            await error_reply(interaction, e)

    @setup_g.command(
        name="status",
        description="Show the current GvG configuration + counts",
    )
    async def setup_status(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            cfg = await self._settings(interaction.guild.id)
            async with self.bot.db.session() as s:
                positions = (await s.execute(
                    select(func.count(GvgPosition.id))
                    .where(GvgPosition.guild_id == interaction.guild.id)
                )).scalar_one()
                matches = (await s.execute(
                    select(func.count(GvgMatch.id))
                    .where(GvgMatch.guild_id == interaction.guild.id)
                )).scalar_one()
            embed = self._settings_embed(interaction.guild, cfg)
            embed.add_field(name="Positions", value=str(positions), inline=True)
            embed.add_field(name="Matches", value=str(matches), inline=True)
            await interaction.followup.send(
                embed=embed, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    def _settings_embed(
        self, guild: discord.Guild, cfg: GvgGuildSettings,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"⚔️ GvG configuration — {guild.name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Manager role",
            value=f"<@&{cfg.manager_role_id}>" if cfg.manager_role_id else "—",
            inline=True,
        )
        embed.add_field(
            name="Admin role",
            value=f"<@&{cfg.admin_role_id}>" if cfg.admin_role_id else "—",
            inline=True,
        )
        embed.add_field(
            name="Roster channel",
            value=f"<#{cfg.roster_channel_id}>" if cfg.roster_channel_id else "—",
            inline=True,
        )
        embed.add_field(
            name="Log channel",
            value=f"<#{cfg.log_channel_id}>" if cfg.log_channel_id else "—",
            inline=True,
        )
        embed.add_field(
            name="Manager channel",
            value=(
                f"<#{cfg.manager_channel_id}> _(Register still on board)_"
                if cfg.manager_channel_id else
                "— _(all buttons on the board)_"
            ),
            inline=True,
        )
        embed.add_field(
            name="MAIN limit",
            value=str(cfg.default_main_limit),
            inline=True,
        )
        embed.add_field(
            name="Commander self-manage",
            value="✅ ON" if cfg.allow_commander_roster_management else "❌ OFF",
            inline=True,
        )
        vm_label = {
            1: "1 · Grouped (`- Pos: p1 │ p2 │ …`)",
            2: "2 · Flat (`- Pos - weapon Name`)",
            3: "3 · Horizontal (inline fields, 3/row)",
            4: "4 · Grid image (avatars, run `/gvg avatar sync`)",
        }.get(cfg.view_mode, str(cfg.view_mode))
        embed.add_field(
            name="Roster view",
            value=vm_label,
            inline=True,
        )
        ldr_icon_val = cfg.grid_leader_icon or "🎖️ _(default)_"
        ldr_color_val = cfg.grid_leader_bg_hex or "_(none)_"
        embed.add_field(
            name="Leader icon / cell color",
            value=f"{ldr_icon_val} / {ldr_color_val}",
            inline=True,
        )
        embed.add_field(
            name="Grid padding (top / bottom)",
            value=(
                f"{cfg.grid_top_padding} / {cfg.grid_bottom_padding} px"
            ),
            inline=True,
        )
        glass_val = (
            f"{'✅ ON' if cfg.grid_glass_enabled else '❌ OFF'}"
            f" · blur {'✅' if cfg.grid_glass_blur else '❌'}"
            f" · opacity {cfg.grid_glass_opacity}%"
        )
        embed.add_field(
            name="Glass look",
            value=glass_val,
            inline=True,
        )
        embed.add_field(
            name="Leader color opacity",
            value=f"{cfg.grid_leader_bg_alpha}%",
            inline=True,
        )
        return embed

    # ---------------- /gvg position ----------------

    @position_g.command(name="add", description="Add a global position (Tank / DPS / …)")
    @app_commands.describe(
        name="Position name",
        description="Short description (optional)",
        icon="Icon (emoji or <:name:id>)",
        color="HEX color like #FF8800",
        sort_order="Display order (lower shows first)",
    )
    async def position_add(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 64],
        description: app_commands.Range[str, 1, 256] | None = None,
        icon: app_commands.Range[str, 1, 64] | None = None,
        color: str | None = None,
        sort_order: app_commands.Range[int, 0, 999] | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            color_hex = normalize_hex(color)
            async with self.bot.db.session() as s:
                existing = await s.scalar(
                    select(GvgPosition).where(
                        GvgPosition.guild_id == interaction.guild.id,
                        GvgPosition.name == name.strip(),
                    )
                )
                if existing is not None:
                    if not existing.is_active:
                        # Re-enable a previously disabled position.
                        existing.is_active = True
                        if description is not None:
                            existing.description = description.strip()
                        if icon is not None:
                            existing.icon = icon.strip()
                        if color_hex is not None:
                            existing.color_hex = color_hex
                        if sort_order is not None:
                            existing.sort_order = int(sort_order)
                        await s.commit()
                        pos_id = existing.id
                        await self._audit(
                            guild_id=interaction.guild.id, match_id=None,
                            actor_user_id=interaction.user.id,
                            action="POSITION_ENABLED",
                            entity_type="GvgPosition", entity_id=pos_id,
                        )
                        await send_ok(interaction, f"✅ Position **{name}** re-enabled.")
                        return
                    raise ConflictError(
                        f"Position **{name}** already exists in this guild."
                    )
                if sort_order is None:
                    count = (await s.execute(
                        select(func.count(GvgPosition.id))
                        .where(GvgPosition.guild_id == interaction.guild.id)
                    )).scalar_one()
                    sort_order = int(count)
                row = GvgPosition(
                    guild_id=interaction.guild.id,
                    name=name.strip(),
                    description=(description.strip() if description else None),
                    icon=(icon.strip() if icon else None),
                    color_hex=color_hex,
                    sort_order=int(sort_order),
                )
                s.add(row)
                await s.commit()
                await s.refresh(row)
                pos_id = row.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="POSITION_CREATED", entity_type="GvgPosition",
                entity_id=pos_id, after={"name": name.strip()},
            )
            await send_ok(interaction, f"✅ Position **{name}** added.")
        except Exception as e:
            await error_reply(interaction, e)

    @position_g.command(name="edit", description="Rename / restyle a position")
    @app_commands.describe(
        position="Which position",
        new_name="New name (optional)",
        description="New description (optional)",
        icon="New icon (optional; '-' to clear)",
        color="New HEX color (optional; '-' to clear)",
        sort_order="New display order (optional)",
    )
    async def position_edit(
        self,
        interaction: discord.Interaction,
        position: str,
        new_name: app_commands.Range[str, 1, 64] | None = None,
        description: app_commands.Range[str, 1, 256] | None = None,
        icon: app_commands.Range[str, 1, 64] | None = None,
        color: str | None = None,
        sort_order: app_commands.Range[int, 0, 999] | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.scalar(
                    select(GvgPosition).where(
                        GvgPosition.guild_id == interaction.guild.id,
                        GvgPosition.name == position.strip(),
                    )
                )
                if row is None:
                    raise NotFoundError(f"Position **{position}** not found.")
                if new_name is not None:
                    clash = await s.scalar(
                        select(GvgPosition).where(
                            GvgPosition.guild_id == interaction.guild.id,
                            GvgPosition.name == new_name.strip(),
                            GvgPosition.id != row.id,
                        )
                    )
                    if clash is not None:
                        raise ConflictError(
                            f"Position **{new_name}** already exists."
                        )
                    row.name = new_name.strip()
                if description is not None:
                    row.description = description.strip() or None
                if icon is not None:
                    row.icon = None if icon.strip() in ("", "-") else icon.strip()
                if color is not None:
                    if color.strip() in ("", "-"):
                        row.color_hex = None
                    else:
                        row.color_hex = normalize_hex(color)
                if sort_order is not None:
                    row.sort_order = int(sort_order)
                await s.commit()
                pid = row.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="POSITION_UPDATED", entity_type="GvgPosition",
                entity_id=pid,
            )
            # A position is global — every match that references it
            # renders differently now.
            await self._refresh_matches_referencing_position(
                interaction.guild.id, pid,
            )
            await send_ok(interaction, "✅ Position updated.")
        except Exception as e:
            await error_reply(interaction, e)

    @position_g.command(
        name="delete",
        description="Delete a position (soft-disable when referenced)",
    )
    @app_commands.describe(position="Which position")
    async def position_delete(
        self,
        interaction: discord.Interaction,
        position: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.scalar(
                    select(GvgPosition).where(
                        GvgPosition.guild_id == interaction.guild.id,
                        GvgPosition.name == position.strip(),
                    )
                )
                if row is None:
                    raise NotFoundError(f"Position **{position}** not found.")
                refs = (await s.execute(
                    select(func.count(GvgRosterAssignment.id))
                    .where(GvgRosterAssignment.position_id == row.id)
                )).scalar_one()
                pid = row.id
                if refs > 0:
                    # SPEC §9: soft-disable rather than hard delete.
                    row.is_active = False
                    await s.commit()
                    action = "POSITION_DISABLED"
                else:
                    await s.delete(row)
                    await s.commit()
                    action = "POSITION_DELETED"
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action=action, entity_type="GvgPosition",
                entity_id=pid,
            )
            await self._refresh_matches_referencing_position(
                interaction.guild.id, pid,
            )
            note = (
                "disabled (kept for historical assignments)"
                if action == "POSITION_DISABLED" else "deleted"
            )
            await send_ok(interaction, f"✅ Position **{position}** {note}.")
        except Exception as e:
            await error_reply(interaction, e)

    @position_g.command(name="list", description="List all positions in this guild")
    @app_commands.describe(include_disabled="Also show disabled positions")
    async def position_list(
        self,
        interaction: discord.Interaction,
        include_disabled: bool = False,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                stmt = select(GvgPosition).where(
                    GvgPosition.guild_id == interaction.guild.id
                )
                if not include_disabled:
                    stmt = stmt.where(GvgPosition.is_active.is_(True))
                stmt = stmt.order_by(GvgPosition.sort_order, GvgPosition.id)
                rows = list((await s.execute(stmt)).scalars().all())
            if not rows:
                await interaction.followup.send(
                    "_No positions yet — use `/gvg position add`._",
                    ephemeral=True,
                )
                return
            lines = []
            for r in rows:
                r_icon = sanitize_display_icon(r.icon, interaction.guild)
                icon = f"{r_icon} " if r_icon else ""
                color = f" {hex_to_nearest_color_emoji(r.color_hex)}" if r.color_hex else ""
                dis = "  _(disabled)_" if not r.is_active else ""
                lines.append(f"`{r.sort_order:>2}.` {icon}{r.name}{color}{dis}")
            embed = discord.Embed(
                title="🎯 Positions",
                description="\n".join(lines)[:4000],
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await error_reply(interaction, e)

    @position_g.command(name="reorder", description="Change a position's display order")
    @app_commands.describe(position="Which position", sort_order="New sort order")
    async def position_reorder(
        self,
        interaction: discord.Interaction,
        position: str,
        sort_order: app_commands.Range[int, 0, 999],
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.scalar(
                    select(GvgPosition).where(
                        GvgPosition.guild_id == interaction.guild.id,
                        GvgPosition.name == position.strip(),
                    )
                )
                if row is None:
                    raise NotFoundError(f"Position **{position}** not found.")
                row.sort_order = int(sort_order)
                await s.commit()
            await send_ok(interaction, f"✅ Position **{position}** moved to sort order {sort_order}.")
        except Exception as e:
            await error_reply(interaction, e)

    # ---------------- /gvg match ----------------

    @match_g.command(name="create", description="Create a new Guild War match")
    @app_commands.describe(
        name="Match name (e.g. 'GvG 05/09')",
        opponent="Opponent guild name (optional)",
        description="Description (optional)",
        main_limit="MAIN player limit (default 30, max 500)",
    )
    async def match_create(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 128],
        opponent: app_commands.Range[str, 1, 128] | None = None,
        description: app_commands.Range[str, 1, 512] | None = None,
        main_limit: app_commands.Range[int, 1, 500] | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            settings = await self._settings(interaction.guild.id)
            limit = int(main_limit) if main_limit is not None else settings.default_main_limit
            async with self.bot.db.session() as s:
                row = GvgMatch(
                    guild_id=interaction.guild.id,
                    name=name.strip(),
                    opponent_name=(opponent.strip() if opponent else None),
                    description=(description.strip() if description else None),
                    main_player_limit=limit,
                    status="DRAFT",
                    created_by=interaction.user.id,
                )
                s.add(row)
                await s.commit()
                await s.refresh(row)
                mid = row.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=interaction.user.id,
                action="MATCH_CREATED", entity_type="GvgMatch",
                entity_id=mid,
                after={"name": name.strip(), "main_limit": limit},
            )
            await send_ok(interaction, (
                f"✅ Match **{name}** created (#{mid}). Add teams with "
                "`/gvg team add`."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    @match_g.command(name="edit", description="Rename / update a match")
    @app_commands.describe(
        match="Match name",
        new_name="New match name (optional)",
        opponent="New opponent (optional; '-' to clear)",
        description="New description (optional; '-' to clear)",
        main_limit="New MAIN limit (max 500; rejects if current MAIN count > new limit)",
    )
    async def match_edit(
        self,
        interaction: discord.Interaction,
        match: str,
        new_name: app_commands.Range[str, 1, 128] | None = None,
        opponent: app_commands.Range[str, 1, 128] | None = None,
        description: app_commands.Range[str, 1, 512] | None = None,
        main_limit: app_commands.Range[int, 1, 500] | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await self._get_match_by_name(s, interaction.guild.id, match)
                self._reject_if_locked(row)
                if new_name is not None:
                    row.name = new_name.strip()
                if opponent is not None:
                    row.opponent_name = None if opponent.strip() in ("", "-") else opponent.strip()
                if description is not None:
                    row.description = None if description.strip() in ("", "-") else description.strip()
                if main_limit is not None:
                    new_limit = int(main_limit)
                    main_count = (await s.execute(
                        select(func.count(GvgRosterAssignment.id))
                        .where(
                            GvgRosterAssignment.match_id == row.id,
                            GvgRosterAssignment.roster_type == "MAIN",
                        )
                    )).scalar_one()
                    if main_count > new_limit:
                        raise ValidationError(
                            f"Can't lower main_limit below current MAIN count "
                            f"({main_count} placed → {new_limit} would be under)."
                        )
                    row.main_player_limit = new_limit
                await s.commit()
                mid = row.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=interaction.user.id,
                action="MATCH_UPDATED", entity_type="GvgMatch",
                entity_id=mid,
            )
            self._schedule_refresh(interaction.guild.id, mid)
            await send_ok(interaction, "✅ Match updated.")
        except Exception as e:
            await error_reply(interaction, e)

    @match_g.command(name="delete", description="Delete a match (and every team/assignment)")
    @app_commands.describe(match="Match name")
    async def match_delete(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_admin(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await self._get_match_by_name(s, interaction.guild.id, match)
                mid = row.id
                mname = row.name
                # "Teams used" = distinct team_ids referenced by this
                # match's assignments (teams themselves are guild-wide
                # and survive the match deletion).
                team_count = (await s.execute(
                    select(func.count(func.distinct(GvgRosterAssignment.team_id)))
                    .where(GvgRosterAssignment.match_id == mid)
                )).scalar_one()
                assign_count = (await s.execute(
                    select(func.count(GvgRosterAssignment.id))
                    .where(GvgRosterAssignment.match_id == mid)
                )).scalar_one()
            # SPEC §43 — confirm before deleting a match with content.
            confirm = ConfirmView(actor_id=interaction.user.id)
            await interaction.followup.send(
                content=(
                    f"⚠️ Delete match **{mname}**?\n"
                    f"This removes **{assign_count}** roster assignment(s) "
                    f"across **{team_count}** team(s), plus the published "
                    "board messages. Teams themselves are guild-wide and "
                    "survive. Not reversible."
                ),
                view=confirm, ephemeral=True,
            )
            ok = await confirm.wait_result()
            if not ok:
                return
            # Delete the published Discord messages before we cascade the
            # GvgRosterMessage rows away — otherwise the messages linger.
            deleted_msgs = await self._delete_board_messages(
                interaction.guild, mid,
            )
            # Cancel any pending debounced refresh for this match.
            pending = self._refresh_tasks.pop(mid, None)
            if pending is not None and not pending.done():
                pending.cancel()
            async with self.bot.db.session() as s:
                row = await s.get(GvgMatch, mid)
                if row is not None:
                    await s.delete(row)
                    await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="MATCH_DELETED", entity_type="GvgMatch",
                entity_id=mid,
                before={"name": mname},
                after={"deleted_board_messages": deleted_msgs},
            )
            suffix = (
                f" ({deleted_msgs} board message(s) removed)"
                if deleted_msgs else ""
            )
            await send_ok(interaction, f"✅ Match **{mname}** deleted.{suffix}")
        except Exception as e:
            await error_reply(interaction, e)

    @match_g.command(name="view", description="Show a match summary + team counts")
    @app_commands.describe(match="Match name")
    async def match_view(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                row = await s.scalar(
                    select(GvgMatch).where(
                        GvgMatch.guild_id == interaction.guild.id,
                        GvgMatch.name == match.strip(),
                    )
                )
                if row is None:
                    raise NotFoundError(f"Match **{match}** not found.")
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                )).scalars().all())
                assigns = list((await s.execute(
                    select(GvgRosterAssignment)
                    .where(GvgRosterAssignment.match_id == row.id)
                )).scalars().all())
            await interaction.followup.send(
                embed=self._match_embed(
                    row, teams, assigns, interaction.guild,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    @match_g.command(name="list", description="List every match in this guild")
    @app_commands.describe(include_archived="Include ARCHIVED matches")
    async def match_list(
        self,
        interaction: discord.Interaction,
        include_archived: bool = False,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                stmt = select(GvgMatch).where(
                    GvgMatch.guild_id == interaction.guild.id
                )
                if not include_archived:
                    stmt = stmt.where(GvgMatch.status != "ARCHIVED")
                stmt = stmt.order_by(GvgMatch.created_at.desc())
                matches = list((await s.execute(stmt)).scalars().all())
            if not matches:
                await interaction.followup.send(
                    "_No matches yet._", ephemeral=True,
                )
                return
            lines = [
                f"`#{m.id}` **{m.name}** — {m.status}"
                + (f" vs {m.opponent_name}" if m.opponent_name else "")
                for m in matches
            ]
            embed = discord.Embed(
                title="⚔️ Guild War matches",
                description="\n".join(lines)[:4000],
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await error_reply(interaction, e)

    def _match_embed(
        self,
        m: GvgMatch,
        teams: list[GvgTeam],
        assignments: list[GvgRosterAssignment],
        guild: discord.Guild | None = None,
    ) -> discord.Embed:
        main_count = sum(1 for a in assignments if a.roster_type == "MAIN")
        reserve_count = sum(1 for a in assignments if a.roster_type == "RESERVE")
        embed = discord.Embed(
            title=f"⚔️ {m.name}",
            description=(
                (f"vs **{m.opponent_name}**\n" if m.opponent_name else "")
                + (m.description or "")
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Status", value=m.status, inline=True)
        embed.add_field(
            name="MAIN",
            value=f"{main_count}/{m.main_player_limit}",
            inline=True,
        )
        embed.add_field(name="RESERVE", value=str(reserve_count), inline=True)
        embed.add_field(name="Teams", value=str(len(teams)), inline=True)
        by_team: dict[int, list[GvgRosterAssignment]] = {}
        for a in assignments:
            by_team.setdefault(a.team_id, []).append(a)
        for team in sorted(teams, key=lambda t: t.sort_order):
            team_assigns = by_team.get(team.id, [])
            tm = sum(1 for a in team_assigns if a.roster_type == "MAIN")
            tr = sum(1 for a in team_assigns if a.roster_type == "RESERVE")
            commander = (
                f"<@{team.commander_user_id}>" if team.commander_user_id else "_(none)_"
            )
            t_icon = sanitize_display_icon(team.icon, guild) or ""
            embed.add_field(
                name=f"{t_icon} {team.name}".strip(),
                value=f"MAIN {tm} • RESERVE {tr}\nCommander: {commander}",
                inline=False,
            )
        embed.set_footer(text=f"Match #{m.id} • Created by user {m.created_by}")
        return embed

    # ---------------- /gvg match lifecycle (Phase E) ----------------
    #
    # SPEC §5 state machine:
    #   DRAFT ─publish─▶ PUBLISHED ─lock─▶ LOCKED ─unlock─▶ PUBLISHED
    #                       │                    │
    #                       └──archive──▶ ARCHIVED ◀──archive──┘
    #                                          │
    #                                          └──restore──▶ DRAFT

    @match_g.command(
        name="publish",
        description="Publish the match: DRAFT → PUBLISHED and post the board",
    )
    @app_commands.describe(match="Match name")
    async def match_publish(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(
                    s, interaction.guild.id, match,
                )
                if mrow.status == "ARCHIVED":
                    raise InvalidStateError(
                        f"Match **{mrow.name}** is ARCHIVED — restore via "
                        "`/gvg match restore` before publishing."
                    )
                mid = mrow.id
                mname = mrow.name
            posted, edited, deleted = await self._publish_or_refresh(
                interaction.guild, mid,
                actor_id=interaction.user.id, refresh_only=False,
            )
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=interaction.user.id,
                action="MATCH_PUBLISHED", entity_type="GvgMatch",
                entity_id=mid,
                after={"posted": posted, "edited": edited, "deleted": deleted},
            )
            await send_ok(interaction, (
                f"📢 Published **{mname}** "
                f"({posted} posted, {edited} edited, {deleted} removed)."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    @match_g.command(
        name="lock",
        description="Freeze the match: any further roster mutation is refused",
    )
    @app_commands.describe(match="Match name")
    async def match_lock(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(
                    s, interaction.guild.id, match,
                )
                if mrow.status == "LOCKED":
                    await interaction.followup.send(
                        f"_Match **{mrow.name}** is already LOCKED._",
                        ephemeral=True,
                    )
                    return
                if mrow.status == "ARCHIVED":
                    raise InvalidStateError(
                        f"Match **{mrow.name}** is ARCHIVED — restore first."
                    )
                before_status = mrow.status
                mrow.status = "LOCKED"
                await s.commit()
                mid = mrow.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=interaction.user.id,
                action="MATCH_LOCKED", entity_type="GvgMatch",
                entity_id=mid,
                before={"status": before_status},
                after={"status": "LOCKED"},
            )
            self._schedule_refresh(interaction.guild.id, mid)
            await send_ok(interaction, (
                f"🔒 Match **{match}** is now LOCKED — further roster "
                "mutations will be refused until `/gvg match unlock`."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    @match_g.command(
        name="unlock",
        description="Unfreeze a LOCKED match (returns to PUBLISHED)",
    )
    @app_commands.describe(match="Match name")
    async def match_unlock(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(
                    s, interaction.guild.id, match,
                )
                if mrow.status != "LOCKED":
                    await interaction.followup.send(
                        f"_Match **{mrow.name}** isn't LOCKED (current "
                        f"status: {mrow.status})._",
                        ephemeral=True,
                    )
                    return
                mrow.status = "PUBLISHED"
                await s.commit()
                mid = mrow.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=interaction.user.id,
                action="MATCH_UNLOCKED", entity_type="GvgMatch",
                entity_id=mid,
                before={"status": "LOCKED"},
                after={"status": "PUBLISHED"},
            )
            self._schedule_refresh(interaction.guild.id, mid)
            await send_ok(interaction, f"🔓 Match **{match}** is now PUBLISHED again.")
        except Exception as e:
            await error_reply(interaction, e)

    @match_g.command(
        name="archive",
        description="Archive the match (hides it from active lists; board removed)",
    )
    @app_commands.describe(match="Match name")
    async def match_archive(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(
                    s, interaction.guild.id, match,
                )
                if mrow.status == "ARCHIVED":
                    await interaction.followup.send(
                        f"_Match **{mrow.name}** is already ARCHIVED._",
                        ephemeral=True,
                    )
                    return
                before_status = mrow.status
                mid = mrow.id
                mname = mrow.name
            # SPEC §43 — confirm before archiving.
            confirm = ConfirmView(actor_id=interaction.user.id)
            await interaction.followup.send(
                content=(
                    f"⚠️ Archive match **{mname}**?\n"
                    "The board messages are removed and further mutations "
                    "are refused. You can restore via "
                    "`/gvg match restore`."
                ),
                view=confirm, ephemeral=True,
            )
            ok = await confirm.wait_result()
            if not ok:
                return
            # Remove board messages first so the DB row transition doesn't
            # leave stale messages linked.
            deleted_msgs = await self._delete_board_messages(
                interaction.guild, mid,
            )
            # Cancel any pending debounced refresh.
            pending = self._refresh_tasks.pop(mid, None)
            if pending is not None and not pending.done():
                pending.cancel()
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, mid)
                if mrow is not None:
                    mrow.status = "ARCHIVED"
                    await s.execute(
                        delete(GvgRosterMessage).where(
                            GvgRosterMessage.match_id == mid
                        )
                    )
                    await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=interaction.user.id,
                action="MATCH_ARCHIVED", entity_type="GvgMatch",
                entity_id=mid,
                before={"status": before_status},
                after={"status": "ARCHIVED", "removed_messages": deleted_msgs},
            )
            await send_ok(interaction, (
                f"🗄️ Match **{mname}** archived "
                f"({deleted_msgs} board message(s) removed)."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    @match_g.command(
        name="restore",
        description="Restore an ARCHIVED match (returns to DRAFT — republish to re-post)",
    )
    @app_commands.describe(match="Match name")
    async def match_restore(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.scalar(
                    select(GvgMatch).where(
                        GvgMatch.guild_id == interaction.guild.id,
                        GvgMatch.name == match.strip(),
                    )
                )
                if mrow is None:
                    raise NotFoundError(f"Match **{match}** not found.")
                if mrow.status != "ARCHIVED":
                    await interaction.followup.send(
                        f"_Match **{mrow.name}** isn't ARCHIVED (current "
                        f"status: {mrow.status})._",
                        ephemeral=True,
                    )
                    return
                mrow.status = "DRAFT"
                await s.commit()
                mid = mrow.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=interaction.user.id,
                action="MATCH_UPDATED", entity_type="GvgMatch",
                entity_id=mid,
                before={"status": "ARCHIVED"},
                after={"status": "DRAFT"},
            )
            await send_ok(interaction, (
                f"♻️ Match **{match}** restored to DRAFT — use "
                "`/gvg match publish` to re-post the board."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    @match_g.command(
        name="clone",
        description="Deep-clone a match's roster into a new DRAFT match",
    )
    @app_commands.describe(
        match="Source match",
        new_name="Name for the new match",
        clone_main="Copy MAIN roster assignments (default True)",
        clone_reserve="Copy RESERVE roster assignments (default True)",
    )
    async def match_clone(
        self,
        interaction: discord.Interaction,
        match: str,
        new_name: app_commands.Range[str, 1, 128],
        clone_main: bool = True,
        clone_reserve: bool = True,
    ) -> None:
        """Teams and positions are guild-wide so they don't need to be
        cloned — the new match sees the same team list automatically.
        This only copies over the source match's per-player assignments
        (MAIN and/or RESERVE) — commanders are a team-level attribute
        so they carry across too."""
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            target_name = new_name.strip()
            async with self.bot.db.session() as s:
                src = await s.scalar(
                    select(GvgMatch).where(
                        GvgMatch.guild_id == interaction.guild.id,
                        GvgMatch.name == match.strip(),
                    )
                )
                if src is None:
                    raise NotFoundError(f"Match **{match}** not found.")
                clash = await s.scalar(
                    select(GvgMatch).where(
                        GvgMatch.guild_id == interaction.guild.id,
                        GvgMatch.name == target_name,
                    )
                )
                if clash is not None:
                    raise ConflictError(
                        f"A match named **{target_name}** already exists."
                    )
                src_assigns = list((await s.execute(
                    select(GvgRosterAssignment)
                    .where(GvgRosterAssignment.match_id == src.id)
                    .order_by(
                        GvgRosterAssignment.team_id,
                        GvgRosterAssignment.roster_type,
                        GvgRosterAssignment.sort_order,
                    )
                )).scalars().all())
                # SPEC §45: transactional clone. Build the whole graph
                # and commit once.
                dst = GvgMatch(
                    guild_id=interaction.guild.id,
                    name=target_name,
                    opponent_name=src.opponent_name,
                    description=src.description,
                    scheduled_at=None,
                    main_player_limit=src.main_player_limit,
                    status="DRAFT",
                    created_by=interaction.user.id,
                )
                s.add(dst)
                await s.flush()  # get dst.id
                mains_copied = 0
                reserves_copied = 0
                # Track which user_ids made it into dst — Team Leader
                # rows we clone below survive as long as the leader
                # has ANY assignment in dst (they may lead a team
                # they're not a member of).
                cloned_user_ids: set[int] = set()
                for sa in src_assigns:
                    include = (
                        (sa.roster_type == "MAIN" and clone_main)
                        or (sa.roster_type == "RESERVE" and clone_reserve)
                    )
                    if not include:
                        continue
                    s.add(GvgRosterAssignment(
                        match_id=dst.id,
                        team_id=sa.team_id,  # guild-wide team survives
                        user_id=sa.user_id,
                        position_id=sa.position_id,
                        roster_type=sa.roster_type,
                        custom_icon=sa.custom_icon,
                        custom_color_hex=sa.custom_color_hex,
                        note=sa.note,
                        sort_order=sa.sort_order,
                        created_by=interaction.user.id,
                    ))
                    cloned_user_ids.add(sa.user_id)
                    if sa.roster_type == "MAIN":
                        mains_copied += 1
                    else:
                        reserves_copied += 1
                # Copy Team Leader rows for any leader whose user id
                # is on the cloned dst roster (in any team, MAIN or
                # RESERVE) — skips leaders lost to a filter that
                # excluded the leader entirely.
                leaders_copied = 0
                src_leaders = (await s.execute(
                    select(GvgTeamLeader).where(
                        GvgTeamLeader.match_id == src.id,
                    )
                )).scalars().all()
                for lrow in src_leaders:
                    if lrow.user_id in cloned_user_ids:
                        s.add(GvgTeamLeader(
                            match_id=dst.id,
                            team_id=lrow.team_id,
                            user_id=lrow.user_id,
                            created_by=interaction.user.id,
                        ))
                        leaders_copied += 1
                await s.commit()
                new_mid = dst.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=new_mid,
                actor_user_id=interaction.user.id,
                action="MATCH_CREATED", entity_type="GvgMatch",
                entity_id=new_mid,
                after={
                    "name": target_name,
                    "cloned_from": match.strip(),
                    "main": mains_copied,
                    "reserve": reserves_copied,
                    "team_leaders": leaders_copied,
                },
            )
            leader_note = (
                f"• {leaders_copied} Team Leader assignment(s)\n"
                if leaders_copied else ""
            )
            await send_ok(interaction, (
                f"✅ Cloned **{match}** → **{target_name}** (DRAFT):\n"
                f"• {mains_copied} MAIN assignment(s)\n"
                f"• {reserves_copied} RESERVE assignment(s)\n"
                f"{leader_note}\n"
                "Teams + commanders + positions are shared guild-wide, "
                "so the new match already knows about them. Run "
                "`/gvg match publish` when you're ready to post the "
                "board."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    # ---------------- /gvg team ----------------

    @team_g.command(
        name="add",
        description="Create a team (shared across every match of this guild)",
    )
    @app_commands.describe(
        name="Team name (Alpha / Bravo / …) — must be unique for this guild",
        description="Description (optional)",
        icon="Team icon (optional)",
        color="Team HEX color (optional)",
        sort_order="Display order (optional; default appends)",
    )
    async def team_add(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 64],
        description: app_commands.Range[str, 1, 256] | None = None,
        icon: app_commands.Range[str, 1, 64] | None = None,
        color: str | None = None,
        sort_order: app_commands.Range[int, 0, 999] | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            color_hex = normalize_hex(color)
            async with self.bot.db.session() as s:
                clash = await s.scalar(
                    select(GvgTeam).where(
                        GvgTeam.guild_id == interaction.guild.id,
                        GvgTeam.name == name.strip(),
                    )
                )
                if clash is not None:
                    raise ConflictError(
                        f"Team **{name}** already exists in this guild."
                    )
                if sort_order is None:
                    count = (await s.execute(
                        select(func.count(GvgTeam.id))
                        .where(GvgTeam.guild_id == interaction.guild.id)
                    )).scalar_one()
                    sort_order = int(count)
                row = GvgTeam(
                    guild_id=interaction.guild.id,
                    name=name.strip(),
                    description=(description.strip() if description else None),
                    icon=(icon.strip() if icon else None),
                    color_hex=color_hex,
                    sort_order=int(sort_order),
                )
                s.add(row)
                await s.commit()
                await s.refresh(row)
                tid = row.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="TEAM_CREATED", entity_type="GvgTeam", entity_id=tid,
                after={"name": name.strip()},
            )
            # New team is guild-wide; refresh every match that already
            # has assignments (none for a brand-new team, so this is a
            # no-op — but the wire is consistent with edit/rename).
            await self._refresh_matches_referencing_team(
                interaction.guild.id, tid,
            )
            await send_ok(interaction, f"✅ Team **{name}** created (guild-wide).")
        except Exception as e:
            await error_reply(interaction, e)

    @team_g.command(name="edit", description="Rename / restyle a team")
    @app_commands.describe(
        team="Team name (guild-wide)",
        new_name="New team name (optional)",
        description="New description (optional; '-' to clear)",
        icon="New icon (optional; '-' to clear)",
        color="New HEX color (optional; '-' to clear)",
        sort_order="New display order (optional)",
    )
    async def team_edit(
        self,
        interaction: discord.Interaction,
        team: str,
        new_name: app_commands.Range[str, 1, 64] | None = None,
        description: app_commands.Range[str, 1, 256] | None = None,
        icon: app_commands.Range[str, 1, 64] | None = None,
        color: str | None = None,
        sort_order: app_commands.Range[int, 0, 999] | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                trow = await self._get_team_by_name(
                    s, interaction.guild.id, team,
                )
                if new_name is not None:
                    clash = await s.scalar(
                        select(GvgTeam).where(
                            GvgTeam.guild_id == interaction.guild.id,
                            GvgTeam.name == new_name.strip(),
                            GvgTeam.id != trow.id,
                        )
                    )
                    if clash is not None:
                        raise ConflictError(
                            f"Team **{new_name}** already exists."
                        )
                    trow.name = new_name.strip()
                if description is not None:
                    trow.description = None if description.strip() in ("", "-") else description.strip()
                if icon is not None:
                    trow.icon = None if icon.strip() in ("", "-") else icon.strip()
                if color is not None:
                    trow.color_hex = None if color.strip() in ("", "-") else normalize_hex(color)
                if sort_order is not None:
                    trow.sort_order = int(sort_order)
                await s.commit()
                tid = trow.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="TEAM_UPDATED", entity_type="GvgTeam", entity_id=tid,
            )
            # Guild-wide team change fans out to every match that has
            # assignments on this team.
            await self._refresh_matches_referencing_team(
                interaction.guild.id, tid,
            )
            await send_ok(interaction, "✅ Team updated.")
        except Exception as e:
            await error_reply(interaction, e)

    @team_g.command(
        name="delete",
        description="Delete a team (unassigns every player across every match)",
    )
    @app_commands.describe(team="Team name (guild-wide)")
    async def team_delete(
        self,
        interaction: discord.Interaction,
        team: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                trow = await self._get_team_by_name(
                    s, interaction.guild.id, team,
                )
                tid = trow.id
                tname = trow.name
                had_players = (await s.execute(
                    select(func.count(GvgRosterAssignment.id))
                    .where(GvgRosterAssignment.team_id == tid)
                )).scalar_one()
            # SPEC §43 — confirm before deleting a team that still has
            # players (guild-wide: this may span multiple matches).
            if had_players > 0:
                confirm = ConfirmView(actor_id=interaction.user.id)
                await interaction.followup.send(
                    content=(
                        f"⚠️ Delete team **{tname}**?\n"
                        f"This will unassign **{had_players}** player(s) "
                        "across every match that uses this team. Not "
                        "reversible."
                    ),
                    view=confirm, ephemeral=True,
                )
                ok = await confirm.wait_result()
                if not ok:
                    return
            # Collect the affected matches BEFORE cascading the team.
            affected_match_ids: list[int] = []
            async with self.bot.db.session() as s:
                affected_match_ids = list((await s.execute(
                    select(GvgRosterAssignment.match_id)
                    .where(GvgRosterAssignment.team_id == tid)
                    .distinct()
                )).scalars().all())
                trow = await s.get(GvgTeam, tid)
                if trow is not None:
                    await s.delete(trow)
                    await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="TEAM_DELETED", entity_type="GvgTeam", entity_id=tid,
                before={"name": tname, "players": had_players},
            )
            for mid in affected_match_ids:
                self._schedule_refresh(interaction.guild.id, mid)
            await send_ok(interaction, (
                f"✅ Team **{tname}** deleted (with **{had_players}** "
                f"assignment(s) across {len(affected_match_ids)} match(es))."
                if had_players
                else f"✅ Team **{tname}** deleted."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    @team_g.command(
        name="list",
        description="List every team in this guild",
    )
    async def team_list(
        self,
        interaction: discord.Interaction,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                )).scalars().all())
            if not teams:
                await interaction.followup.send(
                    "_No teams yet — add one with `/gvg team add`._",
                    ephemeral=True,
                )
                return
            lines = []
            for t in teams:
                cmd = (
                    f"<@{t.commander_user_id}>" if t.commander_user_id else "—"
                )
                t_icon = sanitize_display_icon(t.icon, interaction.guild)
                icon = f"{t_icon} " if t_icon else ""
                lines.append(
                    f"`{t.sort_order:>2}.` {icon}**{t.name}** • "
                    f"Cmdr: {cmd}"
                )
            embed = discord.Embed(
                title=f"🛡️ Teams — {interaction.guild.name}",
                description="\n".join(lines)[:4000],
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(
                embed=embed, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    @team_g.command(
        name="reorder",
        description="Change a team's display order (guild-wide)",
    )
    @app_commands.describe(team="Team name", sort_order="New sort order")
    async def team_reorder(
        self,
        interaction: discord.Interaction,
        team: str,
        sort_order: app_commands.Range[int, 0, 999],
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                trow = await self._get_team_by_name(
                    s, interaction.guild.id, team,
                )
                trow.sort_order = int(sort_order)
                await s.commit()
                tid = trow.id
            await self._refresh_matches_referencing_team(
                interaction.guild.id, tid,
            )
            await send_ok(interaction, f"✅ Team **{team}** moved to sort order {sort_order}.")
        except Exception as e:
            await error_reply(interaction, e)

    @team_g.command(
        name="commander",
        description="Set / clear a team's commander (guild-wide)",
    )
    @app_commands.describe(
        team="Team name",
        user="Discord user (leave empty to clear)",
    )
    async def team_commander(
        self,
        interaction: discord.Interaction,
        team: str,
        user: discord.Member | None = None,
    ) -> None:
        """Commander is a team attribute (guild-wide). The crown appears
        on the roster board of every match the commander is assigned
        MAIN of that team on; matches where they aren't assigned just
        show no crown for that team."""
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                trow = await self._get_team_by_name(
                    s, interaction.guild.id, team,
                )
                tid = trow.id
                had_commander = trow.commander_user_id
            if user is None:
                # SPEC §43 — confirm before clearing an existing commander.
                if had_commander is not None:
                    confirm = ConfirmView(actor_id=interaction.user.id)
                    await interaction.followup.send(
                        content=(
                            f"⚠️ Clear commander <@{had_commander}> from "
                            f"team **{team}**?"
                        ),
                        view=confirm, ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    ok = await confirm.wait_result()
                    if not ok:
                        return
                async with self.bot.db.session() as s:
                    trow = await s.get(GvgTeam, tid)
                    if trow is not None:
                        trow.commander_user_id = None
                        await s.commit()
                await self._audit(
                    guild_id=interaction.guild.id, match_id=None,
                    actor_user_id=interaction.user.id,
                    action="COMMANDER_REMOVED", entity_type="GvgTeam",
                    entity_id=tid,
                )
                await self._refresh_matches_referencing_team(
                    interaction.guild.id, tid,
                )
                await send_ok(interaction, f"✅ Commander cleared for team **{team}**.")
                return
            # Set commander — no per-match validation any more. The crown
            # shows up wherever the user happens to be MAIN of this team.
            async with self.bot.db.session() as s:
                trow = await s.get(GvgTeam, tid)
                if trow is None:
                    raise NotFoundError("Team disappeared.")
                trow.commander_user_id = user.id
                await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="COMMANDER_SET", entity_type="GvgTeam",
                entity_id=tid, after={"user_id": user.id},
            )
            await self._refresh_matches_referencing_team(
                interaction.guild.id, tid,
            )
            await send_ok(interaction, f"✅ Commander of **{team}**: {user.mention} (guild-wide).")
        except Exception as e:
            await error_reply(interaction, e)

    @team_g.command(
        name="leader",
        description="Set / clear a team's per-match leader (renders at top of team block)",
    )
    @app_commands.describe(
        match="Match name",
        team="Team name (guild-wide)",
        user="Discord user (must be on the match roster; can be on a different team; leave empty to clear)",
    )
    async def team_leader(
        self,
        interaction: discord.Interaction,
        match: str,
        team: str,
        user: discord.Member | None = None,
    ) -> None:
        """Team Leader is per-(match, team) tactical role rendered as a
        `🎖️ Team Lead:` line at the top of that team's block. Distinct
        from the guild-wide 👑 commander badge — set it fresh per match
        without disturbing the commander."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(
                    s, interaction.guild.id, match,
                )
                self._reject_if_locked(mrow)
                trow = await self._get_team_by_name(
                    s, interaction.guild.id, team,
                )
                # Manager, or the team's own commander (when
                # self-manage is enabled), may set the leader.
                if not await self.is_manager(interaction):
                    if not await self._actor_can_command_team(
                        interaction, trow,
                    ):
                        raise PermissionError(
                            "Only a GvG Manager or the team's commander "
                            "(with commander self-manage on) can change "
                            "the team leader."
                        )
                mid = mrow.id
                tid = trow.id
                existing = await s.scalar(
                    select(GvgTeamLeader).where(
                        GvgTeamLeader.match_id == mid,
                        GvgTeamLeader.team_id == tid,
                    )
                )
                if user is None:
                    if existing is None:
                        raise NotFoundError(
                            f"Team **{team}** has no leader on **{match}**."
                        )
                    before_uid = existing.user_id
                    await s.delete(existing)
                    await s.commit()
                    await self._audit(
                        guild_id=interaction.guild.id, match_id=mid,
                        actor_user_id=interaction.user.id,
                        action="TEAM_LEADER_REMOVED",
                        entity_type="GvgTeamLeader",
                        entity_id=tid,
                        before={"user_id": before_uid},
                    )
                    self._schedule_refresh(interaction.guild.id, mid)
                    await send_ok(
                        interaction,
                        f"🎗️ Team leader cleared for **{team}** on **{match}**.",
                    )
                    return
                # Set — enforce that the target is at least assigned
                # somewhere on this match. A leader may be MAIN or
                # RESERVE of any team (they don't have to be on the
                # team they lead), but they can't be a random Discord
                # user with no involvement in the match.
                arow = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mid,
                        GvgRosterAssignment.user_id == user.id,
                    )
                )
                if arow is None:
                    raise ValidationError(
                        f"{user.mention} isn't on the **{match}** roster "
                        "yet — assign them to any team first, then set "
                        "them as leader."
                    )
                if existing is not None:
                    existing.user_id = user.id
                    existing.created_by = interaction.user.id
                else:
                    s.add(GvgTeamLeader(
                        match_id=mid, team_id=tid, user_id=user.id,
                        created_by=interaction.user.id,
                    ))
                await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=interaction.user.id,
                action="TEAM_LEADER_SET",
                entity_type="GvgTeamLeader",
                entity_id=tid,
                after={"user_id": user.id},
            )
            self._schedule_refresh(interaction.guild.id, mid)
            await send_ok(
                interaction,
                f"🎖️ Team leader of **{team}** on **{match}**: {user.mention}.",
            )
        except Exception as e:
            await error_reply(interaction, e)

    # ---------------- /gvg roster ----------------

    @roster_g.command(
        name="assign",
        description="Open a user-picker to assign one or more players",
    )
    @app_commands.describe(
        match="Match name",
        team="Team name",
        position="Position name (or leave empty for no position)",
        roster_type="MAIN or RESERVE (default MAIN)",
    )
    async def roster_assign(
        self,
        interaction: discord.Interaction,
        match: str,
        team: str,
        position: str | None = None,
        roster_type: str | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            rtype = normalize_roster_type(roster_type)
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(s, interaction.guild.id, match)
                self._reject_if_locked(mrow)
                trow = await self._get_team_by_name(s, interaction.guild.id, team)
                await self.require_manager_or_own_team_commander(interaction, trow)
                pos_id = 0
                if position is not None and position.strip():
                    prow = await s.scalar(
                        select(GvgPosition).where(
                            GvgPosition.guild_id == interaction.guild.id,
                            GvgPosition.name == position.strip(),
                            GvgPosition.is_active.is_(True),
                        )
                    )
                    if prow is None:
                        raise NotFoundError(
                            f"Position **{position}** not found or disabled."
                        )
                    pos_id = prow.id
                if rtype == "MAIN":
                    main_count = (await s.execute(
                        select(func.count(GvgRosterAssignment.id))
                        .where(
                            GvgRosterAssignment.match_id == mrow.id,
                            GvgRosterAssignment.roster_type == "MAIN",
                        )
                    )).scalar_one()
                    remaining = mrow.main_player_limit - main_count
                    if remaining <= 0:
                        raise RosterFullError(
                            f"Main roster full ({main_count}/{mrow.main_player_limit})."
                        )
                    max_pick = min(25, remaining)
                else:
                    max_pick = 25
            view = discord.ui.View(timeout=600)
            view.add_item(
                GvgRosterUserSelect(
                    match_id=mrow.id, team_id=trow.id,
                    position_id=pos_id, roster_type=rtype,
                    max_values=max_pick,
                )
            )
            view.add_item(
                GvgAddMoreButton(
                    match_id=mrow.id, team_id=trow.id,
                    position_id=pos_id, roster_type=rtype,
                )
            )
            await interaction.followup.send(
                content=(
                    f"Pick up to **{max_pick}** player(s) to assign to "
                    f"**{trow.name}** / **{position or 'no position'}** as "
                    f"**{rtype}**."
                ),
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def reopen_assign_picker(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        team_id: int,
        position_id: int | None,
        roster_type: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                trow = await s.get(GvgTeam, team_id)
                if trow is None or trow.guild_id != interaction.guild.id:
                    raise NotFoundError("Team not found in this guild.")
                await self.require_manager_or_own_team_commander(
                    interaction, trow,
                )
                if roster_type == "MAIN":
                    main_count = (await s.execute(
                        select(func.count(GvgRosterAssignment.id))
                        .where(
                            GvgRosterAssignment.match_id == mrow.id,
                            GvgRosterAssignment.roster_type == "MAIN",
                        )
                    )).scalar_one()
                    remaining = mrow.main_player_limit - main_count
                    if remaining <= 0:
                        raise RosterFullError(
                            f"Main roster full ({main_count}/{mrow.main_player_limit})."
                        )
                    max_pick = min(25, remaining)
                else:
                    max_pick = 25
            view = discord.ui.View(timeout=600)
            view.add_item(
                GvgRosterUserSelect(
                    match_id=mrow.id, team_id=trow.id,
                    position_id=position_id or 0, roster_type=roster_type,
                    max_values=max_pick,
                )
            )
            view.add_item(
                GvgAddMoreButton(
                    match_id=mrow.id, team_id=trow.id,
                    position_id=position_id or 0, roster_type=roster_type,
                )
            )
            await interaction.followup.send(
                content=(
                    f"Pick up to **{max_pick}** more player(s) for "
                    f"**{trow.name}** as **{roster_type}**."
                ),
                view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_roster_pick(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        team_id: int,
        position_id: int | None,
        roster_type: str,
        users: list[discord.User | discord.Member],
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            picks = [u for u in users if not getattr(u, "bot", False)]
            seen: set[int] = set()
            deduped: list[discord.User | discord.Member] = []
            for u in picks:
                if u.id in seen:
                    continue
                seen.add(u.id)
                deduped.append(u)
            placed: list[discord.User | discord.Member] = []
            skipped: list[str] = []
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                trow = await s.get(GvgTeam, team_id)
                if trow is None or trow.guild_id != interaction.guild.id:
                    raise NotFoundError("Team not found in this guild.")
                await self.require_manager_or_own_team_commander(
                    interaction, trow,
                )
                # Concurrency-safe MAIN cap check: row-lock the match.
                await self._lock_match(s, mrow.id)
                main_count = (await s.execute(
                    select(func.count(GvgRosterAssignment.id))
                    .where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.roster_type == "MAIN",
                    )
                )).scalar_one()
                for u in deduped:
                    existing = await s.scalar(
                        select(GvgRosterAssignment).where(
                            GvgRosterAssignment.match_id == mrow.id,
                            GvgRosterAssignment.user_id == u.id,
                        )
                    )
                    if existing is not None:
                        skipped.append(f"{u.mention} — already assigned")
                        continue
                    if roster_type == "MAIN" and main_count >= mrow.main_player_limit:
                        skipped.append(
                            f"{u.mention} — main roster full "
                            f"({main_count}/{mrow.main_player_limit})"
                        )
                        continue
                    s.add(GvgRosterAssignment(
                        match_id=mrow.id,
                        team_id=trow.id,
                        user_id=u.id,
                        position_id=position_id or None,
                        roster_type=roster_type,
                        sort_order=0,
                        created_by=interaction.user.id,
                    ))
                    if roster_type == "MAIN":
                        main_count += 1
                    placed.append(u)
                if placed:
                    await s.commit()
                    await self._audit(
                        guild_id=interaction.guild.id, match_id=mrow.id,
                        actor_user_id=interaction.user.id,
                        action="PLAYERS_ASSIGNED",
                        entity_type="GvgRosterAssignment",
                        entity_id=trow.id,
                        after={
                            "team": trow.name,
                            "roster": roster_type,
                            "count": len(placed),
                            "user_ids": [u.id for u in placed],
                        },
                    )
                    self._schedule_refresh(interaction.guild.id, mrow.id)
                    # Opportunistic avatar cache for each fresh
                    # assignee — image-grid view_mode needs one, and
                    # doing it here keeps the cache warm without a
                    # /gvg avatar sync round-trip.
                    for u in placed:
                        self._schedule_avatar_cache(
                            interaction.guild, u.id,
                        )
            lines: list[str] = []
            if placed:
                lines.append(
                    f"✅ {len(placed)} player(s) assigned to "
                    f"**{trow.name}** as **{roster_type}**:"
                )
                lines.extend(f"• {u.mention}" for u in placed)
            if skipped:
                lines.append("")
                lines.append(f"⚠️ {len(skipped)} skipped:")
                lines.extend(f"• {s}" for s in skipped)
            if not lines:
                lines.append("_(nothing placed)_")
            # Include an [Add More] button so the manager keeps going.
            view = discord.ui.View(timeout=600)
            view.add_item(GvgAddMoreButton(
                match_id=mrow.id, team_id=trow.id,
                position_id=position_id or 0, roster_type=roster_type,
            ))
            try:
                msg = await interaction.followup.send(
                    "\n".join(lines),
                    view=view,
                    ephemeral=True, wait=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                asyncio.create_task(
                    _dismiss_after(msg, SUCCESS_DISMISS_DELAY_SEC)
                )
            except discord.HTTPException:
                pass
        except Exception as e:
            await error_reply(interaction, e)

    @roster_g.command(
        name="remove",
        description="Remove one or more players from a match",
    )
    @app_commands.describe(
        match="Match name",
        user="User to remove",
        user2="(optional)",
        user3="(optional)",
        user4="(optional)",
        user5="(optional)",
    )
    async def roster_remove(
        self,
        interaction: discord.Interaction,
        match: str,
        user: discord.Member,
        user2: discord.Member | None = None,
        user3: discord.Member | None = None,
        user4: discord.Member | None = None,
        user5: discord.Member | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            users = [u for u in (user, user2, user3, user4, user5) if u is not None]
            uids = list({u.id for u in users})
            commander_uids: list[int] = []
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(s, interaction.guild.id, match)
                self._reject_if_locked(mrow)
                rows = list((await s.execute(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id.in_(uids),
                    )
                )).scalars().all())
                if not rows:
                    raise NotFoundError(
                        "None of those players are on this match's roster."
                    )
                # TEAM_COMMANDER may only remove players from their own
                # team. A manager may remove anyone.
                if not await self.is_manager(interaction):
                    team_ids = {r.team_id for r in rows}
                    if len(team_ids) != 1:
                        raise PermissionError(
                            "Team commanders can only remove players from "
                            "their own team."
                        )
                    only_team = await s.get(GvgTeam, next(iter(team_ids)))
                    if only_team is None or not await self._actor_can_command_team(
                        interaction, only_team,
                    ):
                        raise PermissionError(
                            "Only the team commander (with commander "
                            "self-manage on) or a GvG Manager can remove "
                            "these players."
                        )
                # SPEC §43 — flag removals that would strip a commander
                # from a team so we can ask before proceeding.
                for r in rows:
                    team = await s.get(GvgTeam, r.team_id)
                    if team is not None and team.commander_user_id == r.user_id:
                        commander_uids.append(r.user_id)
                mid = mrow.id
                row_uids = [r.user_id for r in rows]
            if commander_uids:
                confirm = ConfirmView(actor_id=interaction.user.id)
                mentions = ", ".join(f"<@{u}>" for u in commander_uids)
                await interaction.followup.send(
                    content=(
                        f"⚠️ You're about to remove **{len(commander_uids)}** "
                        f"commander(s) from their team(s): {mentions}\n"
                        "Their team(s) will lose the commander badge."
                    ),
                    view=confirm, ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                ok = await confirm.wait_result()
                if not ok:
                    return
            async with self.bot.db.session() as s:
                rows = list((await s.execute(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mid,
                        GvgRosterAssignment.user_id.in_(row_uids),
                    )
                )).scalars().all())
                cleared_cmd: list[int] = []
                for r in rows:
                    team = await s.get(GvgTeam, r.team_id)
                    if team is not None and team.commander_user_id == r.user_id:
                        team.commander_user_id = None
                        cleared_cmd.append(team.id)
                    await s.delete(r)
                # If any of the removed users was a Team Leader for
                # this match, clear that too — a removed player can't
                # lead. GvgTeamLeader has no FK to the assignment
                # row, so the CASCADE won't handle this for us.
                await s.execute(delete(GvgTeamLeader).where(
                    GvgTeamLeader.match_id == mid,
                    GvgTeamLeader.user_id.in_(row_uids),
                ))
                await s.commit()
                mrow = await s.get(GvgMatch, mid)
            await self._audit(
                guild_id=interaction.guild.id, match_id=mrow.id,
                actor_user_id=interaction.user.id,
                action="PLAYERS_UNASSIGNED",
                entity_type="GvgRosterAssignment",
                before={"user_ids": [r.user_id for r in rows]},
            )
            self._schedule_refresh(interaction.guild.id, mrow.id)
            parts = [
                f"✅ Removed {len(rows)} player(s) from **{match}**."
            ]
            if cleared_cmd:
                parts.append(
                    f"👑 Cleared commander on {len(cleared_cmd)} team(s)."
                )
            await send_ok(interaction, "\n".join(parts))
        except Exception as e:
            await error_reply(interaction, e)

    @roster_g.command(
        name="move",
        description="Move a player to a different team / position / roster type",
    )
    @app_commands.describe(
        match="Match name",
        user="Player to move",
        team="Destination team",
        position="Destination position (optional; '-' to clear)",
        roster_type="MAIN or RESERVE (optional; keeps current if empty)",
    )
    async def roster_move(
        self,
        interaction: discord.Interaction,
        match: str,
        user: discord.Member,
        team: str | None = None,
        position: str | None = None,
        roster_type: str | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            new_type = normalize_roster_type(roster_type) if roster_type else None
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(s, interaction.guild.id, match)
                self._reject_if_locked(mrow)
                arow = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == user.id,
                    )
                )
                if arow is None:
                    raise NotFoundError(
                        f"{user.mention} is not on this match's roster."
                    )
                before = {
                    "team_id": arow.team_id,
                    "position_id": arow.position_id,
                    "roster_type": arow.roster_type,
                }
                new_team_row: GvgTeam | None = None
                if team is not None:
                    new_team_row = await self._get_team_by_name(
                        s, interaction.guild.id, team,
                    )
                    # TEAM_COMMANDER may only move within own team — the
                    # source AND destination team must both be theirs
                    # (SPEC §35). A manager can move anywhere.
                    if not await self.is_manager(interaction):
                        src_team = await s.get(GvgTeam, before["team_id"])
                        can_src = src_team is not None and await self._actor_can_command_team(
                            interaction, src_team,
                        )
                        can_dst = await self._actor_can_command_team(
                            interaction, new_team_row,
                        )
                        if not (can_src and can_dst):
                            raise PermissionError(
                                "Team commanders can only move players "
                                "inside their own team."
                            )
                    arow.team_id = new_team_row.id
                elif not await self.is_manager(interaction):
                    # No team change requested — commander can adjust the
                    # player's own team, still requires them to be that
                    # team's commander.
                    src_team = await s.get(GvgTeam, before["team_id"])
                    if src_team is None or not await self._actor_can_command_team(
                        interaction, src_team,
                    ):
                        raise PermissionError(
                            "Only the team commander (with commander "
                            "self-manage on) or a GvG Manager can move "
                            "this player."
                        )
                if position is not None:
                    if position.strip() in ("", "-"):
                        arow.position_id = None
                    else:
                        prow = await s.scalar(
                            select(GvgPosition).where(
                                GvgPosition.guild_id == interaction.guild.id,
                                GvgPosition.name == position.strip(),
                                GvgPosition.is_active.is_(True),
                            )
                        )
                        if prow is None:
                            raise NotFoundError(
                                f"Position **{position}** not found or disabled."
                            )
                        arow.position_id = prow.id
                if new_type is not None and new_type != arow.roster_type:
                    if new_type == "MAIN":
                        await self._lock_match(s, mrow.id)
                        main_count = (await s.execute(
                            select(func.count(GvgRosterAssignment.id))
                            .where(
                                GvgRosterAssignment.match_id == mrow.id,
                                GvgRosterAssignment.roster_type == "MAIN",
                            )
                        )).scalar_one()
                        if main_count >= mrow.main_player_limit:
                            raise RosterFullError(
                                f"Main roster full ({main_count}/{mrow.main_player_limit})."
                            )
                    arow.roster_type = new_type
                # If commander of old team followed the move, keep sane state.
                old_team = await s.get(GvgTeam, before["team_id"])
                if (
                    old_team is not None
                    and old_team.commander_user_id == user.id
                    and (new_team_row is not None or new_type == "RESERVE")
                ):
                    old_team.commander_user_id = None
                # Team Leader rows survive a move: a leader may lead a
                # team they aren't a member of. As long as the user is
                # still on the match roster, their leader nominations
                # stay valid.
                await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=mrow.id,
                actor_user_id=interaction.user.id,
                action="PLAYERS_MOVED", entity_type="GvgRosterAssignment",
                entity_id=arow.id, before=before,
                after={
                    "team_id": arow.team_id,
                    "position_id": arow.position_id,
                    "roster_type": arow.roster_type,
                },
            )
            self._schedule_refresh(interaction.guild.id, mrow.id)
            await send_ok(interaction, f"✅ Moved {user.mention}.")
        except Exception as e:
            await error_reply(interaction, e)

    @roster_g.command(
        name="swap",
        description="Swap two players (typical use: MAIN ↔ RESERVE within 30 cap)",
    )
    @app_commands.describe(
        match="Match name",
        user_a="Player A",
        user_b="Player B",
    )
    async def roster_swap(
        self,
        interaction: discord.Interaction,
        match: str,
        user_a: discord.Member,
        user_b: discord.Member,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            if user_a.id == user_b.id:
                raise ValidationError("Pick two different players to swap.")
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(s, interaction.guild.id, match)
                self._reject_if_locked(mrow)
                await self._lock_match(s, mrow.id)
                a = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == user_a.id,
                    )
                )
                b = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == user_b.id,
                    )
                )
                if a is None or b is None:
                    raise NotFoundError("Both players must already be on the roster.")
                # TEAM_COMMANDER may swap only within their own team
                # (both slots must belong to it). Managers can swap any
                # two players in the match.
                if not await self.is_manager(interaction):
                    if a.team_id != b.team_id:
                        raise PermissionError(
                            "Team commanders can only swap two players "
                            "inside their own team."
                        )
                    common_team = await s.get(GvgTeam, a.team_id)
                    if common_team is None or not await self._actor_can_command_team(
                        interaction, common_team,
                    ):
                        raise PermissionError(
                            "Only the team commander (with commander "
                            "self-manage on) or a GvG Manager can swap "
                            "these players."
                        )
                # Swap team + position + roster_type in one shot; the pair
                # can't violate MAIN cap because we only swap slots.
                a.team_id, b.team_id = b.team_id, a.team_id
                a.position_id, b.position_id = b.position_id, a.position_id
                a.roster_type, b.roster_type = b.roster_type, a.roster_type
                # Team Leader rows survive: a leader may lead a team
                # they aren't a member of, so a swap between two teams
                # doesn't invalidate any leader nomination for either
                # of the swapped users.
                await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=mrow.id,
                actor_user_id=interaction.user.id,
                action="PLAYERS_SWAPPED", entity_type="GvgRosterAssignment",
                after={"user_ids": [user_a.id, user_b.id]},
            )
            self._schedule_refresh(interaction.guild.id, mrow.id)
            await send_ok(interaction, f"✅ Swapped {user_a.mention} ↔ {user_b.mention}.")
        except Exception as e:
            await error_reply(interaction, e)

    @roster_g.command(name="view", description="Show a match's full roster")
    @app_commands.describe(match="Match name")
    async def roster_view(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            bundle = await self._load_match_bundle(
                interaction.guild.id,
                # _load_match_bundle looks up by id — fetch id by name first.
                await self._match_id_by_name(interaction.guild.id, match),
            )
            if bundle is None:
                raise NotFoundError(f"Match **{match}** not found.")
            (
                mrow, teams, assignments, positions,
                profiles, players, team_leaders,
            ) = bundle
            cfg = await self._settings(interaction.guild.id)
            embed = self._render_roster_view(
                mrow, teams, assignments, positions, profiles, players,
                interaction.guild, team_leaders,
                view_mode=cfg.view_mode,
            )
            await interaction.followup.send(
                embed=embed, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def _match_id_by_name(self, guild_id: int, name: str) -> int:
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(GvgMatch).where(
                    GvgMatch.guild_id == guild_id,
                    GvgMatch.name == name.strip(),
                )
            )
        if row is None:
            raise NotFoundError(f"Match **{name}** not found.")
        return row.id

    def _render_roster_view(
        self,
        m: GvgMatch,
        teams: list[GvgTeam],
        assignments: list[GvgRosterAssignment],
        positions: dict[int, GvgPosition],
        profiles: dict[int, GvgPlayerProfile],
        players: dict[int, Player],
        guild: discord.Guild | None,
        team_leaders: dict[int, int] | None = None,
        view_mode: int = 1,
    ) -> discord.Embed:
        main_count = sum(1 for a in assignments if a.roster_type == "MAIN")
        reserve_count = sum(1 for a in assignments if a.roster_type == "RESERVE")
        embed = discord.Embed(
            title=f"⚔️ Guild War — {m.name}",
            description=(
                (f"vs **{m.opponent_name}**\n" if m.opponent_name else "")
                + f"MAIN: **{main_count}/{m.main_player_limit}** • "
                f"RESERVE: **{reserve_count}**"
            ),
            color=discord.Color.blurple(),
        )
        by_team: dict[int, list[GvgRosterAssignment]] = {}
        for a in assignments:
            by_team.setdefault(a.team_id, []).append(a)
        for t in sorted(teams, key=lambda x: x.sort_order):
            t_icon = sanitize_display_icon(t.icon, guild)
            icon = f"{t_icon} " if t_icon else ""
            color_e = (
                f" {hex_to_nearest_color_emoji(t.color_hex)}" if t.color_hex else ""
            )
            team_assigns = by_team.get(t.id, [])
            mains = [a for a in team_assigns if a.roster_type == "MAIN"]
            reserves = [a for a in team_assigns if a.roster_type == "RESERVE"]
            lines: list[str] = []
            _tby_id = {tm.id: tm for tm in teams}
            leader_line = self._render_team_leader_line(
                (team_leaders or {}).get(t.id),
                assignments, positions, profiles, players, guild,
                teams_by_id=_tby_id, rendering_team_id=t.id,
            )
            if leader_line:
                lines.append(leader_line)
            if not mains and not reserves:
                lines.append("_(empty)_")
            else:
                render_group = (
                    self._render_flat_by_position
                    if view_mode == 2 else
                    self._render_grouped_by_position
                )
                lines.extend(render_group(
                    mains, positions, profiles, players, guild,
                ))
                if reserves:
                    lines.append("_Reserve:_")
                    lines.extend(render_group(
                        reserves, positions, profiles, players, guild,
                    ))
            # view_mode=3 → inline field (compact horizontal); Discord
            # packs up to 3 side-by-side on desktop.
            embed.add_field(
                name=(
                    f"{icon}{t.name}{color_e} — "
                    f"{len(mains)} Main / {len(reserves)} Reserve"
                ),
                value="\n".join(lines)[:1024],
                inline=(view_mode == 3),
            )
        embed.set_footer(text=f"Match #{m.id} • Status {m.status}")
        return embed

    # ---------------- Phase C: persistent roster board ----------------
    #
    # Rendering, publish/refresh, and the debounced auto-refresh that
    # fires after every mutation. Board messages live in the guild's
    # roster_channel_id (SPEC §32-§34).
    #
    # Design:
    #   - _render_board_embeds() → an overview embed + one embed per
    #     team. Split into ≤10-embed chunks so each chunk fits one
    #     Discord message (SPEC §33).
    #   - _publish_or_refresh() edits stored messages in place when
    #     possible; posts new messages when we need more chunks than
    #     stored, deletes trailing ones when we need fewer. Re-posts a
    #     chunk if the stored message was removed manually (404 on edit).
    #   - _schedule_refresh() cancels any pending debounce timer for
    #     the same match_id and re-arms, so a batch of mutations lands
    #     as one edit ~600ms later.

    async def _load_match_bundle(
        self, guild_id: int, match_id: int,
    ) -> tuple[
        GvgMatch,
        list[GvgTeam],
        list[GvgRosterAssignment],
        dict[int, GvgPosition],
        dict[int, GvgPlayerProfile],
        dict[int, Player],
        dict[int, int],
    ] | None:
        """Load everything the renderer needs for one match in fixed
        queries (no ORM lazy-load).

        Teams are guild-scoped and come back as an ordered list of
        every team in the guild — the renderer walks that list and
        filters assignments per-team. Assignments are loaded once for
        this match. Positions/profiles/players are guild-scoped too.
        ``team_leaders`` maps team_id → leader user_id for this match
        (per-match tactical lead; empty when nothing is set)."""
        async with self.bot.db.session() as s:
            mrow = await s.scalar(
                select(GvgMatch).where(
                    GvgMatch.guild_id == guild_id,
                    GvgMatch.id == match_id,
                )
            )
            if mrow is None:
                return None
            teams = list((await s.execute(
                select(GvgTeam)
                .where(GvgTeam.guild_id == guild_id)
                .order_by(GvgTeam.sort_order, GvgTeam.id)
            )).scalars().all())
            assignments = list((await s.execute(
                select(GvgRosterAssignment)
                .where(GvgRosterAssignment.match_id == mrow.id)
                .order_by(
                    GvgRosterAssignment.team_id,
                    GvgRosterAssignment.roster_type,
                    GvgRosterAssignment.sort_order,
                )
            )).scalars().all())
            positions = {
                p.id: p for p in (await s.execute(
                    select(GvgPosition).where(GvgPosition.guild_id == guild_id)
                )).scalars().all()
            }
            profiles = {
                p.user_id: p for p in (await s.execute(
                    select(GvgPlayerProfile).where(
                        GvgPlayerProfile.guild_id == guild_id
                    )
                )).scalars().all()
            }
            # Player rows come from the /baodanh registration store —
            # they carry the in-game name + main-weapon icon we want to
            # surface on the roster. main_weapon is loaded eagerly on
            # the model (lazy="joined") so no N+1 hit here.
            user_ids = {a.user_id for a in assignments}
            players: dict[int, Player] = {}
            if user_ids:
                rows = (await s.execute(
                    select(Player).where(
                        Player.guild_id == guild_id,
                        Player.discord_id.in_(list(user_ids)),
                    )
                )).scalars().all()
                players = {p.discord_id: p for p in rows}
            leader_rows = (await s.execute(
                select(GvgTeamLeader).where(
                    GvgTeamLeader.match_id == mrow.id,
                )
            )).scalars().all()
            team_leaders: dict[int, int] = {
                r.team_id: r.user_id for r in leader_rows
            }
        return (
            mrow, teams, assignments, positions,
            profiles, players, team_leaders,
        )

    def _render_board_embeds(
        self,
        m: GvgMatch,
        teams: list[GvgTeam],
        assignments: list[GvgRosterAssignment],
        positions: dict[int, GvgPosition],
        profiles: dict[int, GvgPlayerProfile],
        players: dict[int, Player],
        guild: discord.Guild | None,
        team_leaders: dict[int, int] | None = None,
        view_mode: int = 1,
    ) -> list[discord.Embed]:
        """Overview embed + one embed per team, ordered by sort_order.

        Teams are guild-wide (walked in `teams`); each match filters
        this match's `assignments` per team_id at render time.

        Team body groups players by position:
            - {pos_icon} {pos_name}: player1, player2, …
        A player whose /baodanh registration exists renders as
        `{main_weapon.icon} {ingame_name}`; otherwise we fall back to
        `resolve_display_name`. Commander gets a 👑 prefix — but only
        when the commander is on this match's MAIN roster of that team
        (otherwise no crown for that match)."""
        embeds: list[discord.Embed] = []
        # Overview.
        main_count = sum(1 for a in assignments if a.roster_type == "MAIN")
        reserve_count = sum(1 for a in assignments if a.roster_type == "RESERVE")
        available = max(0, m.main_player_limit - main_count)
        header = f"⚔️ **Guild War — {m.name}**"
        if m.opponent_name:
            header += f"\nvs **{m.opponent_name}**"
        if m.scheduled_at is not None:
            ts = int(m.scheduled_at.timestamp())
            header += f"\n📅 <t:{ts}:F> (<t:{ts}:R>)"
        header += (
            f"\n🔥 **MAIN:** {main_count}/{m.main_player_limit}"
            f"  •  🌪️ **AVAILABLE:** {available}"
            f"  •  🛌 **RESERVE:** {reserve_count}"
        )
        if m.description:
            header += f"\n\n{m.description}"
        overview = discord.Embed(
            title=None,
            description=header[:4000],
            color=discord.Color.blurple(),
        )
        overview.set_footer(
            text=f"Match #{m.id} • Status {m.status}",
        )
        embeds.append(overview)

        # Pre-group assignments by team_id.
        by_team: dict[int, list[GvgRosterAssignment]] = {}
        for a in assignments:
            by_team.setdefault(a.team_id, []).append(a)

        # view_mode=3 → compact horizontal: each team is an *inline
        # field* inside a shared embed so Discord packs 3 per row on
        # desktop instead of stacking one embed per team.
        # Trade-offs: fields cap at 1024 chars each (grouped format
        # keeps rosters well under it); per-team color bars are lost
        # (bar is per-embed); mobile clients degrade to 2 or 1 column.
        # Overflow (>25 fields OR ~5500 chars) rolls to a new embed.
        if view_mode == 3:
            grid = discord.Embed(color=discord.Color.blurple())
            grid_chars = 0
            FIELD_BUDGET = 5500  # leaves headroom under Discord's 6000
            for t in sorted(teams, key=lambda x: x.sort_order):
                t_icon = sanitize_display_icon(t.icon, guild)
                icon = f"{t_icon} " if t_icon else ""
                color_e = (
                    f" {hex_to_nearest_color_emoji(t.color_hex)}"
                    if t.color_hex else ""
                )
                team_assigns = by_team.get(t.id, [])
                mains = [
                    a for a in team_assigns if a.roster_type == "MAIN"
                ]
                reserves = [
                    a for a in team_assigns if a.roster_type == "RESERVE"
                ]
                lines: list[str] = []
                teams_by_id = {tm.id: tm for tm in teams}
                leader_line = self._render_team_leader_line(
                    (team_leaders or {}).get(t.id),
                    assignments, positions, profiles, players, guild,
                    teams_by_id=teams_by_id, rendering_team_id=t.id,
                )
                if leader_line:
                    lines.append(leader_line)
                if not mains and not reserves:
                    lines.append("_(empty)_")
                else:
                    lines.extend(self._render_grouped_by_position(
                        mains, positions, profiles, players, guild,
                    ))
                    if reserves:
                        lines.append(f"_Reserve ({len(reserves)}):_")
                        lines.extend(self._render_grouped_by_position(
                            reserves, positions, profiles, players, guild,
                        ))
                body = "\n".join(lines)
                if len(body) > 1024:
                    body = body[:1014].rstrip() + "\n… _(cut)_"
                field_name = (
                    f"{icon}{t.name}{color_e} — "
                    f"{len(mains)}M / {len(reserves)}R"
                )[:256]
                # Roll to a new grid embed when we'd bust either cap.
                would_bust_chars = (
                    grid_chars + len(field_name) + len(body) > FIELD_BUDGET
                )
                if (
                    len(grid.fields) >= 25 or
                    (grid.fields and would_bust_chars)
                ):
                    embeds.append(grid)
                    grid = discord.Embed(color=discord.Color.blurple())
                    grid_chars = 0
                grid.add_field(
                    name=field_name, value=body or "_(empty)_",
                    inline=True,
                )
                grid_chars += len(field_name) + len(body)
            if grid.fields:
                embeds.append(grid)
            return embeds

        for t in sorted(teams, key=lambda x: x.sort_order):
            t_icon = sanitize_display_icon(t.icon, guild)
            icon = f"{t_icon} " if t_icon else ""
            color_e = (
                f" {hex_to_nearest_color_emoji(t.color_hex)}" if t.color_hex else ""
            )
            team_assigns = by_team.get(t.id, [])
            mains = [a for a in team_assigns if a.roster_type == "MAIN"]
            reserves = [a for a in team_assigns if a.roster_type == "RESERVE"]
            lines: list[str] = []
            _tby_id = {tm.id: tm for tm in teams}
            leader_line = self._render_team_leader_line(
                (team_leaders or {}).get(t.id),
                assignments, positions, profiles, players, guild,
                teams_by_id=_tby_id, rendering_team_id=t.id,
            )
            if leader_line:
                lines.append(leader_line)
                lines.append("")
            if not mains and not reserves:
                lines.append("_(empty)_")
            else:
                render_group = (
                    self._render_flat_by_position
                    if view_mode == 2 else
                    self._render_grouped_by_position
                )
                lines.extend(render_group(
                    mains, positions, profiles, players, guild,
                ))
                if reserves:
                    lines.append("")
                    lines.append("_Reserve:_")
                    lines.extend(render_group(
                        reserves, positions, profiles, players, guild,
                    ))
            body = "\n".join(lines)
            if len(body) > 4000:
                body = body[:3990].rstrip() + "\n… _(truncated)_"
            team_embed = discord.Embed(
                title=(
                    f"{icon}{t.name}{color_e} — "
                    f"{len(mains)} Main / {len(reserves)} Reserve"
                ),
                description=body,
                color=hex_to_discord_color(t.color_hex),
            )
            if t.description:
                team_embed.set_footer(text=t.description[:2000])
            embeds.append(team_embed)
        return embeds

    # ---------------- view_mode=4: grid image renderer ----------------
    #
    # One PNG per team (5-col avatar grid) posted as an attachment on
    # each team's embed via `embed.set_image(url="attachment://…")`.
    # Sits on top of the PR 1 avatar cache: bytes come from
    # GvgAvatarCache when available, otherwise a first-letter tile is
    # drawn on the fly. Emoji-weapon PNGs are pulled from Discord's
    # CDN and cached in-process so a refresh does not re-fetch them.
    #
    # The whole pipeline degrades gracefully:
    #   - Pillow missing → caller falls back to view_mode=1 with a
    #     one-time warning; every other view mode is unaffected.
    #   - Individual emoji fetch fails → cell renders without the
    #     weapon badge; log-level DEBUG.
    #   - Avatar cache miss → placeholder tile, so the grid is always
    #     complete regardless of sync state.

    async def _get_http_session(self):
        """Lazy aiohttp session used only for CDN emoji fetches. Reused
        across refreshes; disposed in `cog_unload`."""
        import aiohttp

        if self._grid_http is None:
            self._grid_http = aiohttp.ClientSession()
        return self._grid_http

    async def cog_unload(self) -> None:
        # Best-effort session close so a `cog_unload` during a hot
        # reload doesn't leak the aiohttp connector.
        sess = self._grid_http
        if sess is not None:
            try:
                await sess.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._grid_http = None

    async def _fetch_emoji_png(self, emoji_id: int) -> bytes | None:
        """Return the PNG bytes for one Discord custom emoji, cached in
        the cog. Negative results are cached as None so a failed fetch
        does not repeat on every refresh."""
        if emoji_id in self._grid_emoji_cache:
            return self._grid_emoji_cache[emoji_id]
        # size=128 gives crisp downscale to the 60px MAIN weapon overlay
        # (and the 40px reserve one); larger doesn't help much and costs
        # bandwidth on the initial fetch.
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.png?size=128"
        try:
            session = await self._get_http_session()
            async with session.get(url, timeout=10) as resp:  # type: ignore[union-attr]
                if resp.status != 200:
                    self._grid_emoji_cache[emoji_id] = None
                    return None
                data = await resp.read()
            self._grid_emoji_cache[emoji_id] = data
            return data
        except Exception:
            log.debug("emoji fetch failed id=%s", emoji_id, exc_info=True)
            self._grid_emoji_cache[emoji_id] = None
            return None

    async def _fetch_unicode_emoji_png(
        self, emoji: str,
    ) -> bytes | None:
        """Fetch the Twemoji PNG for a unicode emoji so the grid can
        render commander / leader badges even when the setting isn't a
        Discord custom emoji. Cached per-process; negative results are
        cached too so a broken emoji doesn't repeatedly hit the CDN."""
        if not emoji:
            return None
        if emoji in self._grid_unicode_emoji_cache:
            return self._grid_unicode_emoji_cache[emoji]
        # Twemoji filenames strip the U+FE0F variation selector but
        # keep every other codepoint (ZWJ included) hex-joined with
        # dashes.
        codepoints = [
            f"{ord(c):x}" for c in emoji if c != "️"
        ]
        if not codepoints:
            self._grid_unicode_emoji_cache[emoji] = None
            return None
        slug = "-".join(codepoints)
        url = (
            "https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/"
            f"assets/72x72/{slug}.png"
        )
        try:
            session = await self._get_http_session()
            async with session.get(url, timeout=10) as resp:  # type: ignore[union-attr]
                if resp.status != 200:
                    self._grid_unicode_emoji_cache[emoji] = None
                    return None
                data = await resp.read()
            self._grid_unicode_emoji_cache[emoji] = data
            return data
        except Exception:
            log.debug(
                "twemoji fetch failed emoji=%r slug=%s",
                emoji, slug, exc_info=True,
            )
            self._grid_unicode_emoji_cache[emoji] = None
            return None

    async def _resolve_grid_icon_png(
        self,
        icon: str | None,
        *,
        default: str | None = None,
        guild: discord.Guild | None = None,
    ) -> bytes | None:
        """Turn a config icon string into PNG bytes for the grid.

        Priority:
          - Bare `:name:` shortcodes are upgraded to `<:name:id>` via
            the guild's custom-emoji table (matches the same lookup
            weapons/team icons use).
          - `<:name:id>` / `<a:name:id>` → Discord CDN (sharp, cached
            with the weapon-icon pipeline).
          - Unicode emoji → Twemoji CDN (best-effort; may return None
            if the codepoint isn't in the Twemoji asset set).
          - If the caller-supplied `icon` fails to resolve for any
            reason (fetch 404, unknown shortcode, unreachable Twemoji
            codepoint), retry once with `default` so the visible badge
            never silently disappears just because the user typed a
            broken value.
        Never raises; callers treat None as 'draw no badge'."""

        async def _try(pick: str) -> bytes | None:
            resolved = sanitize_display_icon(pick, guild) or pick
            eid = _extract_emoji_id(resolved)
            if eid is not None:
                return await self._fetch_emoji_png(eid)
            return await self._fetch_unicode_emoji_png(pick)

        user_pick = (icon or "").strip()
        if user_pick:
            data = await _try(user_pick)
            if data is not None:
                return data
        if default:
            return await _try(default)
        return None

    async def _load_avatar_map(
        self, guild_id: int, user_ids: list[int],
    ) -> dict[int, bytes]:
        """Bulk-load cached avatars for the given user_ids. Returns
        `{user_id: bytes}` — users not in the cache are simply absent
        from the mapping (the renderer draws a placeholder for them)."""
        if not user_ids:
            return {}
        async with self.bot.db.session() as s:
            rows = (await s.execute(
                select(
                    GvgAvatarCache.user_id, GvgAvatarCache.avatar_bytes,
                ).where(
                    GvgAvatarCache.guild_id == guild_id,
                    GvgAvatarCache.user_id.in_(user_ids),
                )
            )).all()
        return {int(r[0]): bytes(r[1]) for r in rows}

    async def _build_team_image(
        self,
        team: GvgTeam,
        team_assignments: list[GvgRosterAssignment],
        positions: dict[int, GvgPosition],
        profiles: dict[int, GvgPlayerProfile],
        players: dict[int, Player],
        guild: discord.Guild | None,
        avatar_map: dict[int, bytes],
        *,
        team_leader_user_id: int | None,
        leader_badge_png: bytes | None,
        top_pad: int = GRID_DEFAULT_TOP_PAD,
        bottom_pad: int = GRID_DEFAULT_BOTTOM_PAD,
        all_leader_ids: frozenset[int] = frozenset(),
        leader_bg_color: tuple[int, int, int] | None = None,
        glass_enabled: bool = False,
        glass_blur: bool = True,
        glass_opacity: int = 80,
        leader_bg_alpha: int = 100,
        ghost_leader_uid: int | None = None,
        all_assignments: list[GvgRosterAssignment] | None = None,
    ) -> bytes | None:
        """Compose one team's PNG. Returns None on error so the caller
        can fall back to an emojiless embed placeholder."""
        if not _HAS_PIL:
            return None
        mains = [a for a in team_assignments if a.roster_type == "MAIN"]
        reserves = [a for a in team_assignments if a.roster_type == "RESERVE"]
        # Prefetch every weapon icon we'll need for this team so the
        # Pillow thread doesn't await inside the tight paste loop.
        weapon_ids: set[int] = set()
        for a in team_assignments:
            p = players.get(a.user_id)
            if p is not None and p.main_weapon is not None:
                eid = _extract_emoji_id(p.main_weapon.icon)
                if eid is not None:
                    weapon_ids.add(eid)
        # Include ghost leader's weapon in the prefetch.
        if ghost_leader_uid is not None:
            _gp = players.get(ghost_leader_uid)
            if _gp is not None and _gp.main_weapon is not None:
                _geid = _extract_emoji_id(_gp.main_weapon.icon)
                if _geid is not None:
                    weapon_ids.add(_geid)
        weapon_png_map: dict[int, bytes | None] = {}
        for eid in weapon_ids:
            weapon_png_map[eid] = await self._fetch_emoji_png(eid)

        # Prefetch every position icon we'll need for this team so the
        # Pillow thread doesn't await inside the tight paste loop. Keys
        # by position_id since two positions may share the same icon
        # string but resolve differently (guild-emoji shortcode lookup).
        position_icon_pngs: dict[int, bytes | None] = {}
        seen_positions: set[int] = set()
        for a in team_assignments:
            if a.position_id is None or a.position_id in seen_positions:
                continue
            seen_positions.add(a.position_id)
            pos = positions.get(a.position_id)
            if pos is None or not pos.icon:
                continue
            # Upgrade `:name:` shortcodes via guild emoji when possible,
            # then resolve to PNG bytes via the standard icon pipeline
            # (custom emoji → Discord CDN; unicode → Twemoji).
            resolved = sanitize_display_icon(pos.icon, guild) or pos.icon
            position_icon_pngs[a.position_id] = (
                await self._resolve_grid_icon_png(resolved)
            )
        # Include ghost leader's position in the prefetch when they are
        # assigned to a different team (their position_id may not be in
        # this team's assignments yet).
        if ghost_leader_uid is not None and all_assignments is not None:
            _ghost_a = next(
                (a for a in all_assignments if a.user_id == ghost_leader_uid),
                None,
            )
            if (
                _ghost_a is not None and
                _ghost_a.position_id is not None and
                _ghost_a.position_id not in seen_positions
            ):
                seen_positions.add(_ghost_a.position_id)
                _gpos = positions.get(_ghost_a.position_id)
                if _gpos is not None and _gpos.icon:
                    _resolved = (
                        sanitize_display_icon(_gpos.icon, guild) or _gpos.icon
                    )
                    position_icon_pngs[_ghost_a.position_id] = (
                        await self._resolve_grid_icon_png(_resolved)
                    )

        def _position_label_for(a: GvgRosterAssignment) -> str:
            """Position name shown as a fallback caption when the
            position has no renderable icon (or the icon fetch failed).
            Emoji-in-name is stripped so it renders cleanly through
            PIL text; empty return drops the line entirely."""
            if a.position_id is None:
                return ""
            pos = positions.get(a.position_id)
            if pos is None:
                return ""
            # Icon takes priority — text is only rendered when no icon
            # is available for this position.
            if position_icon_pngs.get(a.position_id) is not None:
                return ""
            return _strip_custom_emoji(pos.name)

        def _position_icon_for(a: GvgRosterAssignment) -> bytes | None:
            if a.position_id is None:
                return None
            return position_icon_pngs.get(a.position_id)

        def _cell_for(a: GvgRosterAssignment) -> dict:
            uid = a.user_id
            profile = profiles.get(uid)
            player = players.get(uid)
            # Caption priority: ingame_name → display name override →
            # guild member display name → fallback @-mention.
            if player is not None and player.ingame_name:
                caption = player.ingame_name
            else:
                caption = resolve_display_name(guild, uid, profile)
                if caption.startswith("<@"):
                    caption = f"User {uid}"
            weapon_png = None
            if player is not None and player.main_weapon is not None:
                eid = _extract_emoji_id(player.main_weapon.icon)
                if eid is not None:
                    weapon_png = weapon_png_map.get(eid)
            data = avatar_map.get(uid)
            avatar_img = None
            if data:
                try:
                    avatar_img = _open_avatar_bytes(data, GRID_AVATAR_SIZE)
                except Exception:
                    avatar_img = None
            if avatar_img is None:
                avatar_img = _placeholder_avatar(
                    caption, GRID_AVATAR_SIZE, uid & 0xFF,
                )
            # Cell background: leader highlight or default (None).
            if leader_bg_color is not None and uid in all_leader_ids:
                cell_bg = leader_bg_color
                cell_alpha = leader_bg_alpha
            else:
                cell_bg = None
                cell_alpha = 100
            return {
                "avatar": avatar_img,
                "weapon_png": weapon_png,
                "caption": caption,
                "position_label": _position_label_for(a),
                "position_icon_png": _position_icon_for(a),
                "seed": uid & 0xFF,
                "is_leader": (uid == team_leader_user_id),
                "leader_badge_png": leader_badge_png,
                "cell_bg_color": cell_bg,
                "cell_bg_alpha": cell_alpha,
            }

        # Reserve cells use the smaller avatar size — build them at the
        # right size so the compositor doesn't have to resize twice.
        def _reserve_cell_for(a: GvgRosterAssignment) -> dict:
            base = _cell_for(a)
            uid = a.user_id
            data = avatar_map.get(uid)
            small = None
            if data:
                try:
                    small = _open_avatar_bytes(
                        data, GRID_RESERVE_AVATAR_SIZE,
                    )
                except Exception:
                    small = None
            if small is None:
                small = _placeholder_avatar(
                    base["caption"], GRID_RESERVE_AVATAR_SIZE, uid & 0xFF,
                )
            base["avatar"] = small
            # Reserves stay reserves — never inherit leader badge.
            base["is_leader"] = False
            return base

        def _pos_sort_key(a: GvgRosterAssignment) -> tuple[int, int, int]:
            pos = positions.get(a.position_id) if a.position_id else None
            return (pos.sort_order if pos else 9999, a.sort_order, a.id)

        main_cells = [_cell_for(a) for a in sorted(mains, key=_pos_sort_key)]
        reserve_cells = [
            _reserve_cell_for(a) for a in sorted(reserves, key=_pos_sort_key)
        ]
        # Header stats reflect only the team's actual assignments.
        header_stats = (
            f"{len(mains)} MAIN  •  {len(reserves)} RESERVE"
        )
        # Ghost leader cell: this team's leader is physically assigned to
        # a different team — prepend them as a display-only cell so they
        # appear in this team's grid without adding to the count above.
        if ghost_leader_uid is not None:
            _ga = next(
                (a for a in (all_assignments or []) if a.user_id == ghost_leader_uid),
                None,
            )
            _gp = players.get(ghost_leader_uid)
            _gprof = profiles.get(ghost_leader_uid)
            if _gp is not None and _gp.ingame_name:
                _gcaption = _gp.ingame_name
            else:
                _gcaption = resolve_display_name(guild, ghost_leader_uid, _gprof)
                if _gcaption.startswith("<@"):
                    _gcaption = f"User {ghost_leader_uid}"
            _gweapon = None
            if _gp is not None and _gp.main_weapon is not None:
                _geid = _extract_emoji_id(_gp.main_weapon.icon)
                if _geid is not None:
                    _gweapon = weapon_png_map.get(_geid)
            _gdata = avatar_map.get(ghost_leader_uid)
            _gavatar = None
            if _gdata:
                try:
                    _gavatar = _open_avatar_bytes(_gdata, GRID_AVATAR_SIZE)
                except Exception:
                    _gavatar = None
            if _gavatar is None:
                _gavatar = _placeholder_avatar(
                    _gcaption, GRID_AVATAR_SIZE, ghost_leader_uid & 0xFF,
                )
            _gpos_label = ""
            _gpos_icon = None
            if _ga is not None:
                _gpos_label = _position_label_for(_ga)
                _gpos_icon = _position_icon_for(_ga)
            _gcell_bg = leader_bg_color if leader_bg_color is not None else None
            ghost_cell: dict = {
                "avatar": _gavatar,
                "weapon_png": _gweapon,
                "caption": _gcaption,
                "position_label": _gpos_label,
                "position_icon_png": _gpos_icon,
                "seed": ghost_leader_uid & 0xFF,
                "is_leader": True,
                "leader_badge_png": leader_badge_png,
                "cell_bg_color": _gcell_bg,
                "cell_bg_alpha": leader_bg_alpha,
            }
            main_cells = [ghost_cell] + main_cells
        team_name = team.name
        if team.icon:
            t_icon = sanitize_display_icon(team.icon, guild) or ""
            if t_icon:
                team_name = f"{t_icon} {team.name}"
        try:
            return await asyncio.to_thread(
                _render_team_image_sync,
                team_name=team_name,
                team_color_hex=team.color_hex,
                header_stats=header_stats,
                main_cells=main_cells,
                reserve_cells=reserve_cells,
                top_pad=top_pad,
                bottom_pad=bottom_pad,
                glass_enabled=glass_enabled,
                glass_blur=glass_blur,
                glass_opacity=glass_opacity,
            )
        except Exception:
            log.exception(
                "grid render failed team=%s (guild=%s)",
                team.id, team.guild_id,
            )
            return None

    async def _render_grid_board(
        self,
        m: GvgMatch,
        teams: list[GvgTeam],
        assignments: list[GvgRosterAssignment],
        positions: dict[int, GvgPosition],
        profiles: dict[int, GvgPlayerProfile],
        players: dict[int, Player],
        guild: discord.Guild | None,
        team_leaders: dict[int, int] | None,
        avatar_map: dict[int, bytes],
    ) -> tuple[
        list[discord.Embed],
        list[tuple[str, bytes] | None],
    ]:
        """view_mode=4 output: overview embed + one embed per team, each
        team embed carrying a PNG attachment for its 5-col avatar grid.

        Returns `(embeds, files)` aligned index-for-index — a `None`
        file entry means "no attachment for this embed" (the overview).
        Files are returned as `(filename, bytes)` tuples rather than
        `discord.File` objects because a File wraps a BytesIO that
        drains on the first send/edit — the caller re-wraps to a fresh
        `discord.File` per Discord call. Callers must chunk both lists
        together when they hit the 10-per-message Discord cap."""
        embeds: list[discord.Embed] = []
        files: list[tuple[str, bytes] | None] = []

        # Overview — same numbers/format as the text renderer, just
        # without any per-team drilldown.
        main_count = sum(1 for a in assignments if a.roster_type == "MAIN")
        reserve_count = sum(1 for a in assignments if a.roster_type == "RESERVE")
        available = max(0, m.main_player_limit - main_count)
        header = f"⚔️ **Guild War — {m.name}**"
        if m.opponent_name:
            header += f"\nvs **{m.opponent_name}**"
        if m.scheduled_at is not None:
            ts = int(m.scheduled_at.timestamp())
            header += f"\n📅 <t:{ts}:F> (<t:{ts}:R>)"
        header += (
            f"\n🔥 **MAIN:** {main_count}/{m.main_player_limit}"
            f"  •  🌪️ **AVAILABLE:** {available}"
            f"  •  🛌 **RESERVE:** {reserve_count}"
        )
        if m.description:
            header += f"\n\n{m.description}"
        overview = discord.Embed(
            title=None,
            description=header[:4000],
            color=discord.Color.blurple(),
        )
        overview.set_footer(text=f"Match #{m.id} • Status {m.status}")
        embeds.append(overview)
        files.append(None)

        by_team: dict[int, list[GvgRosterAssignment]] = {}
        for a in assignments:
            by_team.setdefault(a.team_id, []).append(a)

        # Pre-fetch the leader badge icon once per board.
        cfg = await self._settings(guild.id if guild else 0)
        leader_badge_png = await self._resolve_grid_icon_png(
            cfg.grid_leader_icon, default="🎖️", guild=guild,
        )

        # Cross-team leader set: a player who leads ANY team in this
        # match gets the highlight on their grid cell wherever they sit.
        all_leader_ids: frozenset[int] = frozenset(
            (team_leaders or {}).values()
        )
        leader_bg_color: tuple[int, int, int] | None = (
            _hex_to_rgb(cfg.grid_leader_bg_hex, _GRID_CARD_BG)
            if cfg.grid_leader_bg_hex else None
        )
        glass_enabled = bool(cfg.grid_glass_enabled)
        glass_blur = bool(cfg.grid_glass_blur)
        glass_opacity = max(0, min(100, int(cfg.grid_glass_opacity or 80)))
        leader_bg_alpha = max(0, min(100, int(cfg.grid_leader_bg_alpha or 100)))

        # Build set of assigned user_ids per team for ghost-cell detection.
        assigned_per_team: dict[int, set[int]] = {
            t.id: {a.user_id for a in by_team.get(t.id, [])}
            for t in teams
        }
        teams_by_id = {t.id: t for t in teams}

        for t in sorted(teams, key=lambda x: x.sort_order):
            team_assigns = by_team.get(t.id, [])
            mains = [a for a in team_assigns if a.roster_type == "MAIN"]
            reserves = [
                a for a in team_assigns if a.roster_type == "RESERVE"
            ]
            leader_uid = (team_leaders or {}).get(t.id)
            # Ghost leader: assigned to a different team but leads this one.
            ghost_leader_uid: int | None = None
            if (
                leader_uid is not None and
                leader_uid not in assigned_per_team.get(t.id, set())
            ):
                ghost_leader_uid = leader_uid
            leader_line = self._render_team_leader_line(
                leader_uid,
                assignments, positions, profiles, players, guild,
                teams_by_id=teams_by_id, rendering_team_id=t.id,
            )
            t_icon = sanitize_display_icon(t.icon, guild)
            title_icon = f"{t_icon} " if t_icon else ""
            color_e = (
                f" {hex_to_nearest_color_emoji(t.color_hex)}"
                if t.color_hex else ""
            )
            desc_parts: list[str] = []
            if leader_line:
                desc_parts.append(leader_line)
            desc_parts.append(
                f"🔥 {len(mains)} Main · 🛌 {len(reserves)} Reserve"
            )
            team_embed = discord.Embed(
                title=(
                    f"{title_icon}{t.name}{color_e}"
                ),
                description="\n".join(desc_parts)[:4000] or "​",
                color=hex_to_discord_color(t.color_hex),
            )
            png = None
            if team_assigns or ghost_leader_uid is not None:
                png = await self._build_team_image(
                    t, team_assigns, positions, profiles, players,
                    guild, avatar_map,
                    team_leader_user_id=leader_uid,
                    leader_badge_png=leader_badge_png,
                    top_pad=cfg.grid_top_padding,
                    bottom_pad=cfg.grid_bottom_padding,
                    all_leader_ids=all_leader_ids,
                    leader_bg_color=leader_bg_color,
                    glass_enabled=glass_enabled,
                    glass_blur=glass_blur,
                    glass_opacity=glass_opacity,
                    leader_bg_alpha=leader_bg_alpha,
                    ghost_leader_uid=ghost_leader_uid,
                    all_assignments=assignments,
                )
            if png is not None:
                filename = f"team-{t.id}.png"
                team_embed.set_image(url=f"attachment://{filename}")
                files.append((filename, png))
            else:
                # Empty team, or render failed — still surface the
                # embed so the board stays complete.
                files.append(None)
            if t.description:
                team_embed.set_footer(text=t.description[:2000])
            embeds.append(team_embed)

        return embeds, files

    def _render_grouped_by_position(
        self,
        assignments: list[GvgRosterAssignment],
        positions: dict[int, GvgPosition],
        profiles: dict[int, GvgPlayerProfile],
        players: dict[int, Player],
        guild: discord.Guild | None,
    ) -> list[str]:
        """Group `assignments` by position_id and emit one line per group:
            - {pos_icon} {pos_name}: player1, player2, …
        Positions render in their `sort_order`; assignments with no
        position collect under a "No position" bucket rendered last."""
        buckets: dict[int, list[GvgRosterAssignment]] = {}
        no_pos: list[GvgRosterAssignment] = []
        for a in sorted(assignments, key=lambda x: (x.sort_order, x.id)):
            if a.position_id is None or a.position_id not in positions:
                no_pos.append(a)
            else:
                buckets.setdefault(a.position_id, []).append(a)
        # Order buckets by the position's sort_order then id for stability.
        ordered_pos_ids = sorted(
            buckets.keys(),
            key=lambda pid: (positions[pid].sort_order, positions[pid].id),
        )
        out: list[str] = []
        for pid in ordered_pos_ids:
            pos = positions[pid]
            p_icon = sanitize_display_icon(pos.icon, guild)
            pos_icon = f"{p_icon} " if p_icon else ""
            names = PLAYER_LIST_SEPARATOR.join(
                self._render_player_display(
                    a, players.get(a.user_id), profiles.get(a.user_id), guild,
                )
                for a in buckets[pid]
            )
            out.append(f"- {pos_icon}**{pos.name}**: {names}")
        if no_pos:
            names = PLAYER_LIST_SEPARATOR.join(
                self._render_player_display(
                    a, players.get(a.user_id), profiles.get(a.user_id), guild,
                )
                for a in no_pos
            )
            out.append(f"- **(No position)**: {names}")
        return out

    def _render_flat_by_position(
        self,
        assignments: list[GvgRosterAssignment],
        positions: dict[int, GvgPosition],
        profiles: dict[int, GvgPlayerProfile],
        players: dict[int, Player],
        guild: discord.Guild | None,
    ) -> list[str]:
        """view_mode=2 layout: one line per player, format
            - {pos_icon}**{pos_name}** — {weapon_icon}**{name}** {crown}

        Assignments are ordered by (position.sort_order, assignment
        sort_order) so all Tanks come first, then DPS, etc. — the
        line-per-player layout keeps that grouping visually without
        header rows. Players with no position land under a
        `(No position)` label rendered last."""
        def _pos_key(a: GvgRosterAssignment) -> tuple[int, int, int]:
            pos = positions.get(a.position_id or 0)
            pos_sort = pos.sort_order if pos is not None else 10_000
            pos_id = pos.id if pos is not None else 0
            # no-position bucket sorts after everything else
            no_pos_bucket = 0 if pos is not None else 1
            return (no_pos_bucket, pos_sort, pos_id)

        ordered = sorted(
            assignments,
            key=lambda a: (
                *_pos_key(a),
                a.sort_order,
                a.id,
            ),
        )
        out: list[str] = []
        for a in ordered:
            pos = positions.get(a.position_id or 0)
            if pos is not None:
                p_icon = sanitize_display_icon(pos.icon, guild)
                pos_prefix = f"{p_icon} " if p_icon else ""
                pos_label = f"{pos_prefix}**{pos.name}**"
            else:
                pos_label = "**(No position)**"
            player = players.get(a.user_id)
            profile = profiles.get(a.user_id)
            weapon_icon = ""
            if player is not None and player.main_weapon is not None:
                w = sanitize_display_icon(player.main_weapon.icon, guild)
                if w:
                    weapon_icon = f"{w} "
            if player is not None and player.ingame_name:
                name = f"**{player.ingame_name}**"
            else:
                raw_icon = a.custom_icon or (
                    profile.icon if profile else None
                )
                fallback = sanitize_display_icon(raw_icon, guild)
                if fallback and not weapon_icon:
                    weapon_icon = f"{fallback} "
                name = (
                    f"**{resolve_display_name(guild, a.user_id, profile)}**"
                )
            out.append(f"- {pos_label} - {weapon_icon}{name}")
        return out

    def _render_team_leader_line(
        self,
        leader_user_id: int | None,
        match_assignments: list[GvgRosterAssignment],
        positions: dict[int, GvgPosition],
        profiles: dict[int, GvgPlayerProfile],
        players: dict[int, Player],
        guild: discord.Guild | None,
        *,
        teams_by_id: dict[int, "GvgTeam"] | None = None,
        rendering_team_id: int | None = None,
    ) -> str:
        """Compose the "Team Lead" line rendered at the top of a team
        block:  🎖️ **Team Lead:** {weapon} **{name}** {position}.

        The leader may be a member of a different team on the same
        match — we look up their own assignment (any team, MAIN or
        RESERVE) to render their position icon. Appends
        "(from Team X)" when the leader is cross-team so viewers know
        at a glance. Returns "" only when the leader has been removed
        from the match entirely (row is stale)."""
        if leader_user_id is None:
            return ""
        leader_a: GvgRosterAssignment | None = None
        for a in match_assignments:
            if a.user_id == leader_user_id:
                leader_a = a
                break
        if leader_a is None:
            return ""
        player = players.get(leader_user_id)
        profile = profiles.get(leader_user_id)
        weapon_icon = ""
        if player is not None and player.main_weapon is not None:
            w = sanitize_display_icon(player.main_weapon.icon, guild)
            if w:
                weapon_icon = f"{w} "
        if player is not None and player.ingame_name:
            name = f"**{player.ingame_name}**"
        else:
            # Fallback: use the assignment's custom icon (if any) as
            # the "weapon" slot when there's no registered player.
            raw_icon = leader_a.custom_icon or (
                profile.icon if profile else None
            )
            fallback_icon = sanitize_display_icon(raw_icon, guild)
            if fallback_icon and not weapon_icon:
                weapon_icon = f"{fallback_icon} "
            name = (
                f"**{resolve_display_name(guild, leader_user_id, profile)}**"
            )
        pos_suffix = ""
        if leader_a.position_id and leader_a.position_id in positions:
            p_icon = sanitize_display_icon(
                positions[leader_a.position_id].icon, guild,
            )
            if p_icon:
                pos_suffix = f" {p_icon}"
        from_note = ""
        if (
            rendering_team_id is not None and
            teams_by_id is not None and
            leader_a.team_id != rendering_team_id
        ):
            other_team = teams_by_id.get(leader_a.team_id)
            if other_team:
                from_note = f" _(from {other_team.name})_"
        return (
            f"{TEAM_LEADER_ICON} **{TEAM_LEADER_LABEL}:** "
            f"{weapon_icon}{name}{pos_suffix}{from_note}"
        )

    def _render_player_display(
        self,
        a: GvgRosterAssignment,
        player: Player | None,
        profile: GvgPlayerProfile | None,
        guild: discord.Guild | None,
    ) -> str:
        """Priority (SPEC §10 + user request):
            - Registered player  → {main_weapon.icon} {ingame_name}
            - Otherwise          → resolve_display_name(...)
        Per-assignment style overrides (custom_icon / custom_color_hex)
        still apply when there's no registered player."""
        if player is not None and player.ingame_name:
            weapon_icon = ""
            if player.main_weapon is not None:
                w = sanitize_display_icon(player.main_weapon.icon, guild)
                if w:
                    weapon_icon = f"{w} "
            return f"{weapon_icon}**{player.ingame_name}**"
        # Fallback path — profile decorations for unregistered users.
        raw_icon = a.custom_icon or (profile.icon if profile else None)
        icon = sanitize_display_icon(raw_icon, guild)
        color_hex = a.custom_color_hex or (
            profile.color_hex if profile else None
        )
        icon_str = f"{icon} " if icon else ""
        color_e = (
            f"{hex_to_nearest_color_emoji(color_hex)} " if color_hex else ""
        )
        name = resolve_display_name(guild, a.user_id, profile)
        return f"{color_e}{icon_str}**{name}**"

    def _register_button_view(self, match_id: int) -> discord.ui.View:
        """Self-serve buttons only — attached to the public roster
        board when a manager channel is configured so members can
        still sign themselves up, flip to RESERVE, or un-register
        from the public board even though the admin controls have
        moved elsewhere. Named `_register_*` for backwards
        compatibility; carries all three public actions."""
        view = discord.ui.View(timeout=None)
        view.add_item(GvgSelfRegisterButton(match_id))
        view.add_item(GvgSelfReserveButton(match_id))
        view.add_item(GvgUnRegisterButton(match_id))
        return view

    def _admin_button_view(self, match_id: int) -> discord.ui.View:
        """Manager-only buttons. Lives on the manager card when
        `manager_channel_id` is set — the public board carries just
        the Register button in that layout."""
        view = discord.ui.View(timeout=None)
        view.add_item(GvgAssignPickerButton(match_id))
        view.add_item(GvgAssignReservePickerButton(match_id))
        view.add_item(GvgSetLeaderButton(match_id))
        view.add_item(GvgMovePickerButton(match_id))
        view.add_item(GvgBulkMoveButton(match_id))
        view.add_item(GvgRemovePickerButton(match_id))
        view.add_item(GvgViewRosterButton(match_id))
        view.add_item(GvgTeamsButton(match_id))
        view.add_item(GvgPositionsButton(match_id))
        view.add_item(GvgRefreshButton(match_id))
        view.add_item(GvgSettingsButton(match_id))
        return view

    def _board_button_view(self, match_id: int) -> discord.ui.View:
        """Full view: Register + every admin button, used when no
        manager channel is configured (both audiences share one
        surface). Row 0 = self-serve (any member can click ✍️
        Register); rows 1-2 = manager-gated actions. Discord caps a
        row at 5 components so buttons beyond the 5th auto-flow to
        the next row."""
        view = discord.ui.View(timeout=None)
        view.add_item(GvgSelfRegisterButton(match_id))
        view.add_item(GvgSelfReserveButton(match_id))
        view.add_item(GvgUnRegisterButton(match_id))
        view.add_item(GvgAssignPickerButton(match_id))
        view.add_item(GvgAssignReservePickerButton(match_id))
        view.add_item(GvgSetLeaderButton(match_id))
        view.add_item(GvgMovePickerButton(match_id))
        view.add_item(GvgBulkMoveButton(match_id))
        view.add_item(GvgRemovePickerButton(match_id))
        view.add_item(GvgViewRosterButton(match_id))
        view.add_item(GvgTeamsButton(match_id))
        view.add_item(GvgPositionsButton(match_id))
        view.add_item(GvgRefreshButton(match_id))
        view.add_item(GvgSettingsButton(match_id))
        return view

    async def _publish_or_refresh(
        self,
        guild: discord.Guild,
        match_id: int,
        *,
        actor_id: int,
        refresh_only: bool,
    ) -> tuple[int, int, int]:
        """Post/edit the persistent board. Returns (posted, edited, deleted).

        When `manager_channel_id` is set the roster board (in
        roster_channel_id) is read-only — the four action buttons live
        on a separate compact "Manager Controls" card in the manager
        channel, so Discord's own channel permissions hide them from
        non-managers. When it's unset (default) the buttons attach to
        the last roster-board page, matching the previous behavior.

        `refresh_only=True` is the debounced auto-refresh path: if no
        BOARD rows exist yet, no-op instead of publishing implicitly."""
        cfg = await self._settings(guild.id)
        if cfg.roster_channel_id is None:
            raise ValidationError(
                "Configure a roster channel via `/gvg setup config roster_channel:#…` first."
            )
        bundle = await self._load_match_bundle(guild.id, match_id)
        if bundle is None:
            raise NotFoundError("Match not found.")
        (
            mrow, teams, assignments, positions,
            profiles, players, team_leaders,
        ) = bundle

        async with self.bot.db.session() as s:
            existing_all = list((await s.execute(
                select(GvgRosterMessage)
                .where(GvgRosterMessage.match_id == mrow.id)
                .order_by(GvgRosterMessage.kind, GvgRosterMessage.page_index)
            )).scalars().all())
        existing_board = [r for r in existing_all if r.kind == "BOARD"]
        existing_manager = next(
            (r for r in existing_all if r.kind == "MANAGER"), None,
        )
        if refresh_only and not existing_all:
            return (0, 0, 0)

        channel = await self._resolve_text_channel(
            guild, cfg.roster_channel_id,
        )
        if channel is None:
            raise ValidationError(
                "Roster channel not accessible — reconfigure via "
                "`/gvg setup config roster_channel:#…`."
            )

        manager_channel = None
        if cfg.manager_channel_id is not None:
            manager_channel = await self._resolve_text_channel(
                guild, cfg.manager_channel_id,
            )
            if manager_channel is None:
                # Config points at a channel we can't reach — leave the
                # buttons on the board so the flow still works.
                log.warning(
                    "gvg: manager_channel_id=%s not accessible; "
                    "falling back to buttons-on-board.",
                    cfg.manager_channel_id,
                )

        # When a manager channel is configured, the roster board (in
        # the public channel) carries just the Register button so
        # members can still self-sign-up; every admin button moves to
        # the manager card. When no manager channel is set, board
        # carries the full combined view (Register + admin).
        if manager_channel is not None:
            board_view_source = self._register_button_view(mrow.id)
        else:
            board_view_source = self._board_button_view(mrow.id)
        attach_buttons_to_board = True

        # Grid view (view_mode=4) needs Pillow + per-team image attachments.
        # We fall back to the grouped text renderer when Pillow is absent
        # so a stripped-down deploy stays functional; log once per process
        # so the operator can see why the board isn't using the grid.
        use_grid = cfg.view_mode == 4
        if use_grid and not _HAS_PIL:
            global _PIL_MISSING_WARNED
            if not _PIL_MISSING_WARNED:
                log.warning(
                    "gvg view_mode=4 requested but Pillow is not "
                    "installed — falling back to view_mode=1. Install "
                    "`Pillow>=10.0` to render the grid.",
                )
                _PIL_MISSING_WARNED = True
            use_grid = False

        payload_files: list[tuple[str, bytes] | None]
        if use_grid:
            user_ids = sorted({a.user_id for a in assignments})
            avatar_map = await self._load_avatar_map(guild.id, user_ids)
            embeds, payload_files = await self._render_grid_board(
                mrow, teams, assignments, positions, profiles, players,
                guild, team_leaders, avatar_map,
            )
        else:
            embeds = self._render_board_embeds(
                mrow, teams, assignments, positions, profiles, players,
                guild, team_leaders, view_mode=cfg.view_mode,
            )
            payload_files = [None] * len(embeds)

        # Slice embeds AND files together so each chunk lands on one
        # message with its own attachments (Discord caps at 10 embeds
        # AND 10 attachments per message).
        chunks: list[list[discord.Embed]] = [
            embeds[i:i + MAX_EMBEDS_PER_MESSAGE]
            for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE)
        ] or [[]]
        file_chunks: list[list[tuple[str, bytes]]] = []
        for i in range(0, max(len(embeds), 1), MAX_EMBEDS_PER_MESSAGE):
            slice_files = payload_files[i:i + MAX_EMBEDS_PER_MESSAGE]
            file_chunks.append([f for f in slice_files if f is not None])
        # Guarantee 1:1 with chunks even when embeds is empty.
        while len(file_chunks) < len(chunks):
            file_chunks.append([])

        def _fresh_files(
            entries: list[tuple[str, bytes]],
        ) -> list[discord.File]:
            """Rebuild `discord.File` objects on demand — BytesIO drains
            on the first send/edit, so a retry (or the send fallback
            after an edit 404) needs new File instances even though the
            underlying bytes never change."""
            return [
                discord.File(io.BytesIO(data), filename=name)
                for name, data in entries
            ]

        posted = edited = deleted = 0
        board_rows_after: list[tuple[int, int]] = []
        manager_row_after: tuple[int, int] | None = None  # (msg_id, chan_id)

        for i, chunk in enumerate(chunks):
            view = (
                board_view_source
                if (attach_buttons_to_board and i == len(chunks) - 1)
                else None
            )
            chunk_files_bytes = file_chunks[i] if i < len(file_chunks) else []
            if i < len(existing_board):
                row = existing_board[i]
                try:
                    # Always pass `attachments=` on edit — for grid mode
                    # this attaches the freshly-rendered PNGs; for text
                    # modes it clears leftover attachments from a
                    # previous grid publish so an embed image link
                    # doesn't 404 for viewers.
                    await channel.get_partial_message(row.message_id).edit(
                        embeds=chunk, view=view,
                        attachments=_fresh_files(chunk_files_bytes),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    board_rows_after.append((row.message_id, i))
                    edited += 1
                    continue
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    log.warning(
                        "gvg board edit failed (match=%s page=%s): %s",
                        mrow.id, i, e,
                    )
                    continue
            try:
                msg = await channel.send(
                    embeds=chunk, view=view,
                    files=_fresh_files(chunk_files_bytes),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                board_rows_after.append((msg.id, i))
                posted += 1
            except discord.HTTPException as e:
                log.warning(
                    "gvg board post failed (match=%s page=%s): %s",
                    mrow.id, i, e,
                )

        # Delete trailing BOARD rows we no longer need.
        for row in existing_board[len(chunks):]:
            try:
                await channel.get_partial_message(row.message_id).delete()
                deleted += 1
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException as e:
                log.debug("gvg board stale delete: %s", e)

        # Manager card: post/edit when a manager_channel is configured.
        if manager_channel is not None:
            manager_embed = self._render_manager_card_embed(mrow, assignments)
            manager_view = self._admin_button_view(mrow.id)
            # If the existing manager card sits in a different channel
            # (user just re-pointed manager_channel_id), delete the old
            # one before posting the new one.
            if (
                existing_manager is not None
                and existing_manager.channel_id != manager_channel.id
            ):
                stale = await self._resolve_text_channel(
                    guild, existing_manager.channel_id,
                )
                if stale is not None:
                    try:
                        await stale.get_partial_message(
                            existing_manager.message_id
                        ).delete()
                        deleted += 1
                    except (discord.NotFound, discord.Forbidden):
                        pass
                    except discord.HTTPException as e:
                        log.debug("gvg manager stale delete: %s", e)
                existing_manager = None
            if existing_manager is not None:
                try:
                    await manager_channel.get_partial_message(
                        existing_manager.message_id
                    ).edit(
                        embed=manager_embed, view=manager_view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    manager_row_after = (
                        existing_manager.message_id, manager_channel.id,
                    )
                    edited += 1
                except discord.NotFound:
                    existing_manager = None
                except discord.HTTPException as e:
                    log.warning(
                        "gvg manager card edit failed (match=%s): %s",
                        mrow.id, e,
                    )
            if manager_row_after is None:
                try:
                    msg = await manager_channel.send(
                        embed=manager_embed, view=manager_view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    manager_row_after = (msg.id, manager_channel.id)
                    posted += 1
                except discord.HTTPException as e:
                    log.warning(
                        "gvg manager card post failed (match=%s): %s",
                        mrow.id, e,
                    )
        elif existing_manager is not None:
            # manager_channel_id was cleared — remove the leftover card.
            stale = await self._resolve_text_channel(
                guild, existing_manager.channel_id,
            )
            if stale is not None:
                try:
                    await stale.get_partial_message(
                        existing_manager.message_id
                    ).delete()
                    deleted += 1
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException as e:
                    log.debug("gvg manager stale delete: %s", e)

        # Persist the new row set atomically.
        async with self.bot.db.session() as s:
            await s.execute(
                delete(GvgRosterMessage).where(
                    GvgRosterMessage.match_id == mrow.id
                )
            )
            for mid, pidx in board_rows_after:
                s.add(GvgRosterMessage(
                    match_id=mrow.id,
                    channel_id=channel.id,
                    message_id=mid,
                    page_index=pidx,
                    kind="BOARD",
                ))
            if manager_row_after is not None:
                mgr_msg_id, mgr_chan_id = manager_row_after
                s.add(GvgRosterMessage(
                    match_id=mrow.id,
                    channel_id=mgr_chan_id,
                    message_id=mgr_msg_id,
                    page_index=0,
                    kind="MANAGER",
                ))
            # First publish flips DRAFT → PUBLISHED (SPEC §5).
            if not refresh_only:
                live = await s.get(GvgMatch, mrow.id)
                if live is not None and live.status == "DRAFT":
                    live.status = "PUBLISHED"
            await s.commit()

        return (posted, edited, deleted)

    async def _resolve_text_channel(
        self, guild: discord.Guild, channel_id: int,
    ) -> discord.TextChannel | discord.Thread | None:
        ch = guild.get_channel(channel_id)
        if ch is None:
            try:
                ch = await guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return None
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return None
        return ch

    def _render_manager_card_embed(
        self,
        m: GvgMatch,
        assignments: list[GvgRosterAssignment],
    ) -> discord.Embed:
        """Compact card that lives in the manager channel next to the
        buttons — just enough context so managers know which match's
        buttons they're clicking, without redundantly re-rendering the
        full roster (that stays on the public board)."""
        main_count = sum(1 for a in assignments if a.roster_type == "MAIN")
        reserve_count = sum(1 for a in assignments if a.roster_type == "RESERVE")
        opp = f"vs **{m.opponent_name}**\n" if m.opponent_name else ""
        embed = discord.Embed(
            title=f"🛠️ Manager controls — {m.name}",
            description=(
                f"{opp}"
                f"🔥 MAIN **{main_count}/{m.main_player_limit}**  •  "
                f"🛌 RESERVE **{reserve_count}**\n"
                "Buttons only work for GvG Manager / Admin (and, when "
                "the setting is on, the team commander)."
            ),
            color=discord.Color.dark_gold(),
        )
        embed.set_footer(text=f"Match #{m.id} • Status {m.status}")
        return embed

    def _schedule_refresh(self, guild_id: int, match_id: int) -> None:
        """Debounce: cancel any pending refresh for this match and
        re-arm. Batch operations therefore collapse to one board edit."""
        prev = self._refresh_tasks.pop(match_id, None)
        if prev is not None and not prev.done():
            prev.cancel()
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._delayed_refresh(guild_id, match_id))
        self._refresh_tasks[match_id] = task

    async def _delayed_refresh(self, guild_id: int, match_id: int) -> None:
        try:
            await asyncio.sleep(AUTO_REFRESH_DELAY_SEC)
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            bot_user = self.bot.user
            actor = bot_user.id if bot_user is not None else 0
            await self._publish_or_refresh(
                guild, match_id, actor_id=actor, refresh_only=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "gvg auto-refresh failed (guild=%s match=%s)",
                guild_id, match_id,
            )
        finally:
            # Only clear if we still own the slot (a newer refresh may
            # have replaced us).
            cur = self._refresh_tasks.get(match_id)
            if cur is not None and cur.done():
                self._refresh_tasks.pop(match_id, None)

    # ---------------- /gvg roster publish + refresh ----------------

    @roster_g.command(
        name="publish",
        description="Post (or replace) the persistent roster board in the roster channel",
    )
    @app_commands.describe(match="Match name")
    async def roster_publish(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            mrow = None
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(
                    s, interaction.guild.id, match,
                )
            posted, edited, deleted = await self._publish_or_refresh(
                interaction.guild, mrow.id,
                actor_id=interaction.user.id, refresh_only=False,
            )
            await self._audit(
                guild_id=interaction.guild.id, match_id=mrow.id,
                actor_user_id=interaction.user.id,
                action="BOARD_PUBLISHED", entity_type="GvgRosterMessage",
                entity_id=mrow.id,
                after={"posted": posted, "edited": edited, "deleted": deleted},
            )
            await send_ok(interaction, (
                f"✅ Board for **{mrow.name}** updated "
                f"({posted} posted, {edited} edited, {deleted} removed)."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    @roster_g.command(
        name="refresh",
        description="Re-render the persistent roster board in place",
    )
    @app_commands.describe(match="Match name")
    async def roster_refresh(
        self,
        interaction: discord.Interaction,
        match: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await self._get_match_by_name(
                    s, interaction.guild.id, match,
                )
            posted, edited, deleted = await self._publish_or_refresh(
                interaction.guild, mrow.id,
                actor_id=interaction.user.id, refresh_only=False,
            )
            if posted == 0 and edited == 0 and deleted == 0:
                await interaction.followup.send(
                    "_No board messages to refresh — run "
                    "`/gvg roster publish` first._",
                    ephemeral=True,
                )
                return
            await send_ok(interaction, (
                f"🔃 Board refreshed ({edited} edited, "
                f"{posted} posted, {deleted} removed)."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    # ---------------- Board-button handlers ----------------

    async def handle_board_refresh(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            posted, edited, deleted = await self._publish_or_refresh(
                interaction.guild, match_id,
                actor_id=interaction.user.id, refresh_only=False,
            )
            await send_ok(interaction, (
                f"🔃 Refreshed ({edited} edited, {posted} posted, "
                f"{deleted} removed)."
            ))
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_view_roster(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        """Ephemeral roster preview — same embeds the public board
        renders (respecting the guild's view_mode), sent as a private
        reply to the clicker. Manager-gated so a curious member can't
        get the full roster this way if the guild has hidden the
        board. Read-only; no refresh scheduled."""
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            bundle = await self._load_match_bundle(
                interaction.guild.id, match_id,
            )
            if bundle is None:
                raise NotFoundError("Match not found.")
            (
                mrow, teams, assignments, positions,
                profiles, players, team_leaders,
            ) = bundle
            cfg = await self._settings(interaction.guild.id)

            # Mirror the publish path: view_mode=4 uses the grid image
            # renderer; all other modes use the text embed renderer.
            use_grid = cfg.view_mode == 4 and _HAS_PIL
            payload_files: list[tuple[str, bytes] | None]
            if use_grid:
                user_ids = sorted({a.user_id for a in assignments})
                avatar_map = await self._load_avatar_map(
                    interaction.guild.id, user_ids,
                )
                embeds, payload_files = await self._render_grid_board(
                    mrow, teams, assignments, positions, profiles, players,
                    interaction.guild, team_leaders, avatar_map,
                )
            else:
                embeds = self._render_board_embeds(
                    mrow, teams, assignments, positions, profiles, players,
                    interaction.guild, team_leaders,
                    view_mode=cfg.view_mode,
                )
                payload_files = [None] * len(embeds)

            # Discord caps a message at 10 embeds + 10 files; chunk and
            # send. First chunk uses followup (deferred); rest are
            # additional followups.
            chunks = [
                embeds[i:i + MAX_EMBEDS_PER_MESSAGE]
                for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE)
            ] or [[]]
            file_chunks: list[list[tuple[str, bytes]]] = []
            for i in range(0, max(len(embeds), 1), MAX_EMBEDS_PER_MESSAGE):
                sl = payload_files[i:i + MAX_EMBEDS_PER_MESSAGE]
                file_chunks.append([f for f in sl if f is not None])
            while len(file_chunks) < len(chunks):
                file_chunks.append([])

            for i, (chunk, fchunk) in enumerate(zip(chunks, file_chunks)):
                header = (
                    "👁️ Roster preview (only you see this)"
                    if i == 0 else
                    f"👁️ Roster preview — part {i + 1}"
                )
                files = [
                    discord.File(fp=__import__("io").BytesIO(data), filename=name)
                    for name, data in fchunk
                ]
                await interaction.followup.send(
                    content=header,
                    embeds=chunk,
                    files=files,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_settings(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        """Admin-only: pop the settings modal pre-filled with the
        guild's current text-editable settings. Role and channel
        settings still route through the existing `/gvg setup`
        slash commands (Discord modals only accept TextInput; the
        native role / channel pickers can't live inside one)."""
        try:
            # send_modal MUST be the first response — no defer here.
            if not isinstance(interaction.user, discord.Member):
                raise PermissionError(
                    "Run this inside a server to edit GvG settings."
                )
            # require_admin uses discord permissions + admin_role;
            # inline the check because a defer would kill send_modal.
            if not await self.is_admin(interaction):
                raise PermissionError(
                    "Only a GvG Admin (Administrator + admin_role) "
                    "can edit GvG settings."
                )
            assert interaction.guild is not None
            cfg = await self._settings(interaction.guild.id)
            await interaction.response.send_modal(
                GvgSettingsModal(self, cfg),
            )
        except Exception as e:
            # No response was consumed yet — send an ephemeral error.
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"❌ {e}", ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"❌ {e}", ephemeral=True,
                    )
            except Exception:
                log.exception("settings modal open failed")

    async def commit_settings_modal(
        self,
        interaction: discord.Interaction,
        *,
        main_limit_raw: str,
        view_mode_raw: str,
        commander_raw: str,
        grid_leader_icon_raw: str = "",
        grid_padding_raw: str = "",
    ) -> None:
        """Called by GvgSettingsModal.on_submit — parses the three
        text inputs, writes the deltas to GvgGuildSettings, audits,
        and schedules a refresh of every PUBLISHED / LOCKED match
        so the board picks up the new view_mode immediately."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            errors: list[str] = []
            new_main: int | None = None
            new_mode: int | None = None
            new_cmd: bool | None = None
            if main_limit_raw:
                try:
                    v = int(main_limit_raw)
                    if not (1 <= v <= 500):
                        raise ValueError
                    new_main = v
                except ValueError:
                    errors.append(
                        "MAIN limit must be an integer between 1 and 500."
                    )
            if view_mode_raw:
                try:
                    v = int(view_mode_raw)
                    if v not in (1, 2, 3, 4):
                        raise ValueError
                    new_mode = v
                except ValueError:
                    errors.append(
                        "View mode must be one of `1`, `2`, `3`, `4`."
                    )
            if commander_raw:
                lo = commander_raw.strip().lower()
                if lo in ("on", "true", "yes", "y", "1"):
                    new_cmd = True
                elif lo in ("off", "false", "no", "n", "0"):
                    new_cmd = False
                else:
                    errors.append(
                        "Commander self-manage must be `on` or `off`."
                    )
            # Combined icon / color fields. Format: `ICON`, `#HEX`, or
            # `ICON / #HEX`. Blank = keep both; "-" = clear that part.
            # Color must be #RRGGBB; icon is any emoji/custom-emoji str.
            _KEEP = object()
            new_leader_icon: object = _KEEP
            new_leader_color: object = _KEEP

            def _parse_icon_color_field(
                raw: str,
            ) -> tuple[object, object, str | None]:
                """Return (icon_val, color_val, error_msg).

                Each value is either _KEEP (not changed), None (cleared),
                or a validated str. error_msg is None on success."""
                raw = raw.strip()
                if not raw:
                    return _KEEP, _KEEP, None
                # Split on "/" — first "/" separates icon from color.
                if "/" in raw:
                    icon_part, color_part = (
                        p.strip() for p in raw.split("/", 1)
                    )
                else:
                    # Decide by content: starts with # → color only.
                    if raw.startswith("#"):
                        icon_part, color_part = "", raw
                    else:
                        icon_part, color_part = raw, ""

                # Icon part
                if not icon_part:
                    icon_val: object = _KEEP
                elif icon_part == "-":
                    icon_val = None
                else:
                    icon_val = icon_part

                # Color part
                if not color_part:
                    color_val: object = _KEEP
                elif color_part == "-":
                    color_val = None
                else:
                    if not re.match(
                        r"^#[0-9A-Fa-f]{6}$", color_part
                    ):
                        return (
                            _KEEP, _KEEP,
                            f"`{color_part}` is not a valid hex color "
                            f"(use #RRGGBB, e.g. #FFD700).",
                        )
                    color_val = color_part.upper()

                return icon_val, color_val, None

            if grid_leader_icon_raw:
                _li, _lc, _err = _parse_icon_color_field(
                    grid_leader_icon_raw,
                )
                if _err:
                    errors.append(f"Leader color: {_err}")
                else:
                    new_leader_icon = _li
                    new_leader_color = _lc
            new_top_pad: int | None = None
            new_bottom_pad: int | None = None
            if grid_padding_raw:
                # Accept "N" (both use N) or "TOP / BOTTOM" — with
                # either "/" or "," as the separator so pasted values
                # from either convention work.
                parts = [
                    p.strip()
                    for p in grid_padding_raw.replace(",", "/").split("/")
                    if p.strip()
                ]
                try:
                    if len(parts) == 1:
                        v = int(parts[0])
                        if not (0 <= v <= 80):
                            raise ValueError
                        new_top_pad = v
                        new_bottom_pad = v
                    elif len(parts) == 2:
                        t = int(parts[0])
                        b = int(parts[1])
                        if not (0 <= t <= 80) or not (0 <= b <= 80):
                            raise ValueError
                        new_top_pad = t
                        new_bottom_pad = b
                    else:
                        raise ValueError
                except ValueError:
                    errors.append(
                        "Grid padding must be a single integer (both) "
                        "or `top / bottom` — each 0-80."
                    )
            if errors:
                raise ValidationError("\n• ".join(["Input errors:"] + errors))
            async with self.bot.db.session() as s:
                row = await s.get(GvgGuildSettings, interaction.guild.id)
                if row is None:
                    row = GvgGuildSettings(guild_id=interaction.guild.id)
                    s.add(row)
                before = {
                    "default_main_limit": row.default_main_limit,
                    "view_mode": row.view_mode,
                    "allow_commander_roster_management":
                        row.allow_commander_roster_management,
                    "grid_leader_icon": row.grid_leader_icon,
                    "grid_top_padding": row.grid_top_padding,
                    "grid_bottom_padding": row.grid_bottom_padding,
                    "grid_leader_bg_hex": row.grid_leader_bg_hex,
                }
                if new_main is not None:
                    row.default_main_limit = new_main
                if new_mode is not None:
                    row.view_mode = new_mode
                if new_cmd is not None:
                    row.allow_commander_roster_management = new_cmd
                if new_leader_icon is not _KEEP:
                    row.grid_leader_icon = new_leader_icon  # type: ignore[assignment]
                if new_leader_color is not _KEEP:
                    row.grid_leader_bg_hex = new_leader_color  # type: ignore[assignment]
                if new_top_pad is not None:
                    row.grid_top_padding = new_top_pad
                if new_bottom_pad is not None:
                    row.grid_bottom_padding = new_bottom_pad
                await s.commit()
                cfg = await s.get(GvgGuildSettings, interaction.guild.id)
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="SETTINGS_UPDATED",
                entity_type="GvgGuildSettings",
                before=before,
                after={
                    "default_main_limit":
                        (cfg.default_main_limit if cfg else None),
                    "view_mode": (cfg.view_mode if cfg else None),
                    "allow_commander_roster_management":
                        (cfg.allow_commander_roster_management if cfg else None),
                    "grid_leader_icon":
                        (cfg.grid_leader_icon if cfg else None),
                    "grid_top_padding":
                        (cfg.grid_top_padding if cfg else None),
                    "grid_bottom_padding":
                        (cfg.grid_bottom_padding if cfg else None),
                    "grid_leader_bg_hex":
                        (cfg.grid_leader_bg_hex if cfg else None),
                },
            )
            # Any change that affects the visible board (layout switch,
            # new leader badge/color, spacing tweak) → refresh every
            # published match so the new look lands immediately.
            grid_icons_changed = (
                new_leader_icon is not _KEEP
                or new_leader_color is not _KEEP
            )
            grid_layout_changed = (
                (
                    new_top_pad is not None
                    and new_top_pad != before["grid_top_padding"]
                )
                or (
                    new_bottom_pad is not None
                    and new_bottom_pad != before["grid_bottom_padding"]
                )
            )
            should_refresh = (
                (new_mode is not None and new_mode != before["view_mode"])
                or grid_icons_changed
                or grid_layout_changed
            )
            if should_refresh:
                async with self.bot.db.session() as s:
                    ids = list((await s.execute(
                        select(GvgMatch.id).where(
                            GvgMatch.guild_id == interaction.guild.id,
                            GvgMatch.status.in_(("PUBLISHED", "LOCKED")),
                        )
                    )).scalars().all())
                for mid in ids:
                    self._schedule_refresh(interaction.guild.id, mid)
            assert cfg is not None
            await interaction.followup.send(
                content="✅ GvG settings updated.",
                embed=self._settings_embed(interaction.guild, cfg),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def commit_grid_visual_modal(
        self,
        interaction: discord.Interaction,
        *,
        glass_enabled_raw: str,
        glass_blur_raw: str,
        glass_opacity_raw: str,
        leader_alpha_raw: str,
    ) -> None:
        """Called by GvgGridVisualModal.on_submit — parses the four
        text inputs and writes the glass/visual settings delta to
        GvgGuildSettings, then schedules a board refresh."""
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            errors: list[str] = []

            def _parse_bool(raw: str, label: str) -> bool | None:
                if not raw:
                    return None
                lo = raw.strip().lower()
                if lo in ("on", "true", "yes", "y", "1"):
                    return True
                if lo in ("off", "false", "no", "n", "0"):
                    return False
                errors.append(f"{label} must be `on` or `off`.")
                return None

            def _parse_pct(raw: str, label: str) -> int | None:
                if not raw:
                    return None
                try:
                    v = int(raw.strip())
                    if not (0 <= v <= 100):
                        raise ValueError
                    return v
                except ValueError:
                    errors.append(f"{label} must be an integer between 0 and 100.")
                    return None

            new_glass_enabled = _parse_bool(glass_enabled_raw, "Glass look")
            new_glass_blur = _parse_bool(glass_blur_raw, "Blur backdrop")
            new_glass_opacity = _parse_pct(glass_opacity_raw, "Glass opacity")
            new_ldr_alpha = _parse_pct(leader_alpha_raw, "Leader color opacity")

            if errors:
                raise ValidationError("\n• ".join(["Input errors:"] + errors))

            async with self.bot.db.session() as s:
                row = await s.get(GvgGuildSettings, interaction.guild.id)
                if row is None:
                    row = GvgGuildSettings(guild_id=interaction.guild.id)
                    s.add(row)
                before = {
                    "grid_glass_enabled": row.grid_glass_enabled,
                    "grid_glass_blur": row.grid_glass_blur,
                    "grid_glass_opacity": row.grid_glass_opacity,
                    "grid_leader_bg_alpha": row.grid_leader_bg_alpha,
                }
                if new_glass_enabled is not None:
                    row.grid_glass_enabled = new_glass_enabled
                if new_glass_blur is not None:
                    row.grid_glass_blur = new_glass_blur
                if new_glass_opacity is not None:
                    row.grid_glass_opacity = new_glass_opacity
                if new_ldr_alpha is not None:
                    row.grid_leader_bg_alpha = new_ldr_alpha
                await s.commit()
                cfg = await s.get(GvgGuildSettings, interaction.guild.id)
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="SETTINGS_UPDATED",
                entity_type="GvgGuildSettings",
                before=before,
                after={
                    "grid_glass_enabled": (cfg.grid_glass_enabled if cfg else None),
                    "grid_glass_blur": (cfg.grid_glass_blur if cfg else None),
                    "grid_glass_opacity": (cfg.grid_glass_opacity if cfg else None),
                    "grid_leader_bg_alpha": (
                        cfg.grid_leader_bg_alpha if cfg else None
                    ),
                },
            )
            # Visual change → refresh every published match.
            async with self.bot.db.session() as s:
                ids = list((await s.execute(
                    select(GvgMatch.id).where(
                        GvgMatch.guild_id == interaction.guild.id,
                        GvgMatch.status.in_(("PUBLISHED", "LOCKED")),
                    )
                )).scalars().all())
            for mid in ids:
                self._schedule_refresh(interaction.guild.id, mid)
            assert cfg is not None
            await interaction.followup.send(
                content="✅ Grid visual settings updated.",
                embed=self._settings_embed(interaction.guild, cfg),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    # ----- Team / Position modal commit handlers -----
    #
    # The 4 modals below all funnel through these commit helpers so the
    # slash and button flows share the same permission check, audit
    # entry, and match-refresh fan-out.

    async def commit_team_add(
        self,
        interaction: discord.Interaction,
        *,
        name: str,
        description: str,
        icon: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            if not name:
                raise ValidationError("Team name is required.")
            async with self.bot.db.session() as s:
                clash = await s.scalar(
                    select(GvgTeam).where(
                        GvgTeam.guild_id == interaction.guild.id,
                        GvgTeam.name == name,
                    )
                )
                if clash is not None:
                    raise ConflictError(
                        f"Team **{name}** already exists in this guild."
                    )
                count = (await s.execute(
                    select(func.count(GvgTeam.id))
                    .where(GvgTeam.guild_id == interaction.guild.id)
                )).scalar_one()
                row = GvgTeam(
                    guild_id=interaction.guild.id,
                    name=name,
                    description=description or None,
                    icon=icon or None,
                    color_hex=None,  # picked in the follow-up view below
                    sort_order=int(count),
                )
                s.add(row)
                await s.commit()
                await s.refresh(row)
                tid = row.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="TEAM_CREATED", entity_type="GvgTeam",
                entity_id=tid, after={"name": name},
            )
            await self._refresh_matches_referencing_team(
                interaction.guild.id, tid,
            )
            # Follow-up ephemeral: preset color picker. If the user
            # dismisses it or lets it time out, the team stays without
            # a color — matching prior 'left hex blank' behavior.
            await interaction.followup.send(
                content=(
                    f"✅ Team **{name}** created — pick a header color "
                    f"below (skip to keep default)."
                ),
                view=_TeamColorPickerView(self, tid),
                ephemeral=True,
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def commit_team_edit(
        self,
        interaction: discord.Interaction,
        *,
        team_id: int,
        name: str,
        description: str,
        icon: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            if not name:
                raise ValidationError("Team name is required.")
            async with self.bot.db.session() as s:
                trow = await s.get(GvgTeam, team_id)
                if (
                    trow is None
                    or trow.guild_id != interaction.guild.id
                ):
                    raise NotFoundError("Team not found in this guild.")
                if name != trow.name:
                    clash = await s.scalar(
                        select(GvgTeam).where(
                            GvgTeam.guild_id == interaction.guild.id,
                            GvgTeam.name == name,
                            GvgTeam.id != trow.id,
                        )
                    )
                    if clash is not None:
                        raise ConflictError(
                            f"Team **{name}** already exists."
                        )
                trow.name = name
                trow.description = description or None
                trow.icon = icon or None
                # Color left as-is; the follow-up picker updates it
                # separately when the user selects one.
                await s.commit()
                tid = trow.id
                current_color = trow.color_hex
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="TEAM_UPDATED", entity_type="GvgTeam",
                entity_id=tid,
            )
            await self._refresh_matches_referencing_team(
                interaction.guild.id, tid,
            )
            hint = (
                f"Current color: `{current_color}` — pick a new one below "
                f"(skip to keep it)."
                if current_color else
                "Pick a header color below (skip to keep default)."
            )
            await interaction.followup.send(
                content=f"✅ Team **{name}** updated. {hint}",
                view=_TeamColorPickerView(self, tid),
                ephemeral=True,
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def commit_position_add(
        self,
        interaction: discord.Interaction,
        *,
        name: str,
        description: str,
        icon: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            if not name:
                raise ValidationError("Position name is required.")
            async with self.bot.db.session() as s:
                existing = await s.scalar(
                    select(GvgPosition).where(
                        GvgPosition.guild_id == interaction.guild.id,
                        GvgPosition.name == name,
                    )
                )
                if existing is not None:
                    if not existing.is_active:
                        existing.is_active = True
                        existing.description = description or None
                        existing.icon = icon or None
                        # Color left as-is on re-enable; picker follows.
                        await s.commit()
                        pos_id = existing.id
                        await self._audit(
                            guild_id=interaction.guild.id, match_id=None,
                            actor_user_id=interaction.user.id,
                            action="POSITION_ENABLED",
                            entity_type="GvgPosition", entity_id=pos_id,
                        )
                        await interaction.followup.send(
                            content=(
                                f"✅ Position **{name}** re-enabled — "
                                f"pick a color below (skip to keep it)."
                            ),
                            view=_PositionColorPickerView(self, pos_id),
                            ephemeral=True,
                        )
                        return
                    raise ConflictError(
                        f"Position **{name}** already exists in this guild."
                    )
                count = (await s.execute(
                    select(func.count(GvgPosition.id))
                    .where(GvgPosition.guild_id == interaction.guild.id)
                )).scalar_one()
                row = GvgPosition(
                    guild_id=interaction.guild.id,
                    name=name,
                    description=description or None,
                    icon=icon or None,
                    color_hex=None,  # picked in the follow-up view below
                    sort_order=int(count),
                )
                s.add(row)
                await s.commit()
                await s.refresh(row)
                pos_id = row.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="POSITION_CREATED", entity_type="GvgPosition",
                entity_id=pos_id, after={"name": name},
            )
            await interaction.followup.send(
                content=(
                    f"✅ Position **{name}** added — pick a color below "
                    f"(skip to keep default)."
                ),
                view=_PositionColorPickerView(self, pos_id),
                ephemeral=True,
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def commit_position_edit(
        self,
        interaction: discord.Interaction,
        *,
        position_id: int,
        name: str,
        description: str,
        icon: str,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            if not name:
                raise ValidationError("Position name is required.")
            async with self.bot.db.session() as s:
                prow = await s.get(GvgPosition, position_id)
                if (
                    prow is None
                    or prow.guild_id != interaction.guild.id
                ):
                    raise NotFoundError(
                        "Position not found in this guild."
                    )
                if name != prow.name:
                    clash = await s.scalar(
                        select(GvgPosition).where(
                            GvgPosition.guild_id == interaction.guild.id,
                            GvgPosition.name == name,
                            GvgPosition.id != prow.id,
                        )
                    )
                    if clash is not None:
                        raise ConflictError(
                            f"Position **{name}** already exists."
                        )
                prow.name = name
                prow.description = description or None
                prow.icon = icon or None
                # Color untouched; follow-up picker updates it.
                await s.commit()
                pid = prow.id
                current_color = prow.color_hex
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="POSITION_UPDATED", entity_type="GvgPosition",
                entity_id=pid,
            )
            await self._refresh_matches_referencing_position(
                interaction.guild.id, pid,
            )
            hint = (
                f"Current color: `{current_color}` — pick a new one below "
                f"(skip to keep it)."
                if current_color else
                "Pick a color below (skip to keep default)."
            )
            await interaction.followup.send(
                content=f"✅ Position **{name}** updated. {hint}",
                view=_PositionColorPickerView(self, pid),
                ephemeral=True,
            )
        except Exception as e:
            await error_reply(interaction, e)

    # ---------------- Color picker apply-callbacks ----------------

    async def _apply_team_color(
        self,
        interaction: discord.Interaction,
        team_id: int,
        color_hex: str | None,
    ) -> None:
        """Follow-up: update the team's color from a picker selection,
        audit the change, refresh every match that uses the team, and
        replace the ephemeral with a confirmation."""
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                trow = await s.get(GvgTeam, team_id)
                if (
                    trow is None
                    or trow.guild_id != interaction.guild.id
                ):
                    raise NotFoundError(
                        "Team not found in this guild."
                    )
                before = trow.color_hex
                trow.color_hex = color_hex
                await s.commit()
                tname = trow.name
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="TEAM_UPDATED", entity_type="GvgTeam",
                entity_id=team_id,
                before={"color_hex": before},
                after={"color_hex": color_hex},
            )
            await self._refresh_matches_referencing_team(
                interaction.guild.id, team_id,
            )
            label = color_hex or "cleared"
            try:
                await interaction.edit_original_response(
                    content=f"🎨 Team **{tname}** color → `{label}`.",
                    view=None,
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    content=f"🎨 Team **{tname}** color → `{label}`.",
                    ephemeral=True,
                )
        except Exception as e:
            await error_reply(interaction, e)

    async def _apply_position_color(
        self,
        interaction: discord.Interaction,
        position_id: int,
        color_hex: str | None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                prow = await s.get(GvgPosition, position_id)
                if (
                    prow is None
                    or prow.guild_id != interaction.guild.id
                ):
                    raise NotFoundError(
                        "Position not found in this guild."
                    )
                before = prow.color_hex
                prow.color_hex = color_hex
                await s.commit()
                pname = prow.name
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="POSITION_UPDATED", entity_type="GvgPosition",
                entity_id=position_id,
                before={"color_hex": before},
                after={"color_hex": color_hex},
            )
            await self._refresh_matches_referencing_position(
                interaction.guild.id, position_id,
            )
            label = color_hex or "cleared"
            try:
                await interaction.edit_original_response(
                    content=f"🎨 Position **{pname}** color → `{label}`.",
                    view=None,
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    content=f"🎨 Position **{pname}** color → `{label}`.",
                    ephemeral=True,
                )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_teams(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.scalar(
                    select(GvgMatch).where(
                        GvgMatch.guild_id == interaction.guild.id,
                        GvgMatch.id == match_id,
                    )
                )
            if mrow is None:
                raise NotFoundError("Match not found.")
            async with self.bot.db.session() as s:
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                )).scalars().all())
                assigns = list((await s.execute(
                    select(GvgRosterAssignment)
                    .where(GvgRosterAssignment.match_id == mrow.id)
                )).scalars().all())
            by_team: dict[int, list[GvgRosterAssignment]] = {}
            for a in assigns:
                by_team.setdefault(a.team_id, []).append(a)
            if teams:
                lines: list[str] = []
                for t in teams:
                    team_a = by_team.get(t.id, [])
                    tm = sum(1 for a in team_a if a.roster_type == "MAIN")
                    tr = sum(
                        1 for a in team_a if a.roster_type == "RESERVE"
                    )
                    cmd = (
                        f"<@{t.commander_user_id}>"
                        if t.commander_user_id else "—"
                    )
                    t_icon = sanitize_display_icon(
                        t.icon, interaction.guild,
                    )
                    icon = f"{t_icon} " if t_icon else ""
                    lines.append(
                        f"`{t.sort_order:>2}.` {icon}**{t.name}** — "
                        f"MAIN {tm} • RESERVE {tr} • Cmdr: {cmd}"
                    )
                body = "\n".join(lines)[:4000]
            else:
                body = "_No teams yet — click ➕ Add Team below._"
            embed = discord.Embed(
                title=f"🛡️ Teams in {mrow.name}",
                description=body,
                color=discord.Color.blurple(),
            )
            view = self._team_admin_view(teams)
            await interaction.followup.send(
                embed=embed, view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_positions(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        """Positions management panel — list + ➕ Add / ✏️ Edit
        action buttons. Mirrors `handle_board_teams`."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.scalar(
                    select(GvgMatch).where(
                        GvgMatch.guild_id == interaction.guild.id,
                        GvgMatch.id == match_id,
                    )
                )
                if mrow is None:
                    raise NotFoundError("Match not found.")
                positions = list((await s.execute(
                    select(GvgPosition)
                    .where(GvgPosition.guild_id == interaction.guild.id)
                    .order_by(GvgPosition.sort_order, GvgPosition.id)
                )).scalars().all())
            if positions:
                lines: list[str] = []
                for p in positions:
                    p_icon = sanitize_display_icon(
                        p.icon, interaction.guild,
                    )
                    icon = f"{p_icon} " if p_icon else ""
                    active = "" if p.is_active else "  _(disabled)_"
                    lines.append(
                        f"`{p.sort_order:>2}.` {icon}**{p.name}**{active}"
                    )
                body = "\n".join(lines)[:4000]
            else:
                body = "_No positions yet — click ➕ Add Position below._"
            embed = discord.Embed(
                title=f"🎯 Positions (guild-wide)",
                description=body,
                color=discord.Color.blurple(),
            )
            view = self._position_admin_view(positions)
            await interaction.followup.send(
                embed=embed, view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    # ----- Self-register wizard (public button) -----
    #
    # 3 steps: team pick → position pick → /player-reg dialog with a
    # post-save hook that creates the roster assignment. Not admin-
    # gated — any guild member can sign themselves up. Blocked when
    # the match is LOCKED or when the user is already on it.

    async def handle_board_self_register(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            if interaction.guild is None or interaction.user.bot:
                raise ValidationError(
                    "Only guild members (not bots) can register."
                )
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                if mrow.status == "LOCKED":
                    raise MatchLockedError(
                        "This match is LOCKED — self-register is "
                        "closed. Ask a manager if you need in."
                    )
                if mrow.status == "ARCHIVED":
                    raise InvalidStateError(
                        "This match is ARCHIVED — no more sign-ups."
                    )
                mine = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == interaction.user.id,
                    )
                )
                if mine is not None:
                    raise ConflictError(
                        "You're already on this match's roster. Ask a "
                        "manager to move you if you need a different "
                        "team or position."
                    )
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                    .limit(25)
                )).scalars().all())
            if not teams:
                await interaction.followup.send(
                    "_No teams exist yet — ask a manager to add one._",
                    ephemeral=True,
                )
                return
            options = [
                discord.SelectOption(
                    label=t.name[:100],
                    value=str(t.id),
                    emoji=(
                        t.icon if t.icon and len(t.icon) <= 2 else None
                    ),
                )
                for t in teams
            ]
            view = self._self_register_team_view(match_id, options)
            await interaction.followup.send(
                content=(
                    f"✍️ **Sign yourself into {mrow.name}** — pick "
                    "the team you want to join."
                ),
                view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    def _self_register_team_view(
        self, match_id: int, options: list[discord.SelectOption],
    ) -> discord.ui.View:
        cog = self

        class _V(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=600)
                self.add_item(_TeamPick())

        class _TeamPick(discord.ui.Select):
            def __init__(self) -> None:
                super().__init__(
                    placeholder="Pick your team…",
                    min_values=1, max_values=1,
                    options=options,
                )

            async def callback(self, interaction: discord.Interaction) -> None:
                raw = (self.values or ["0"])[0]
                try:
                    team_id = int(raw)
                except ValueError:
                    team_id = 0
                await cog._self_register_present_position(
                    interaction, match_id=match_id, team_id=team_id,
                )

        return _V()

    async def _self_register_present_position(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        team_id: int,
    ) -> None:
        """Step 2 — after team picked, offer positions (+ "no
        position"). Uses `edit_message` so the wizard stays in one
        ephemeral bubble."""
        assert interaction.guild is not None
        async with self.bot.db.session() as s:
            trow = await s.get(GvgTeam, team_id)
            if (
                trow is None
                or trow.guild_id != interaction.guild.id
            ):
                await interaction.response.send_message(
                    "❌ Team not found.", ephemeral=True,
                )
                return
            positions = list((await s.execute(
                select(GvgPosition)
                .where(
                    GvgPosition.guild_id == interaction.guild.id,
                    GvgPosition.is_active.is_(True),
                )
                .order_by(GvgPosition.sort_order, GvgPosition.id)
                .limit(24)
            )).scalars().all())
        options: list[discord.SelectOption] = [
            discord.SelectOption(
                label="— No position —", value="0",
                description="Sign up without a specific role slot",
            )
        ]
        for p in positions:
            p_icon = sanitize_display_icon(p.icon, interaction.guild)
            options.append(discord.SelectOption(
                label=p.name[:100], value=str(p.id),
                emoji=(p_icon if p_icon and len(p_icon) <= 2 else None),
            ))
        view = self._self_register_position_view(
            match_id, team_id, options,
        )
        try:
            await interaction.response.edit_message(
                content=(
                    f"✍️ Team **{trow.name}** — pick your position "
                    "(or *— No position —*)."
                ),
                view=view,
            )
        except discord.HTTPException:
            await interaction.followup.send(
                content=(
                    f"✍️ Team **{trow.name}** — pick your position "
                    "(or *— No position —*)."
                ),
                view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    def _self_register_position_view(
        self, match_id: int, team_id: int,
        options: list[discord.SelectOption],
    ) -> discord.ui.View:
        cog = self

        class _V(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=600)
                self.add_item(_PosPick())

        class _PosPick(discord.ui.Select):
            def __init__(self) -> None:
                super().__init__(
                    placeholder="Pick your position…",
                    min_values=1, max_values=1,
                    options=options[:25],
                )

            async def callback(self, interaction: discord.Interaction) -> None:
                raw = (self.values or ["0"])[0]
                try:
                    position_id = int(raw)
                except ValueError:
                    position_id = 0
                await cog._self_register_open_modal(
                    interaction, match_id=match_id, team_id=team_id,
                    position_id=position_id,
                )

        return _V()

    async def _self_register_open_modal(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        team_id: int,
        position_id: int,
    ) -> None:
        """Step 3 — open UidModal (pre-filled if the user has an
        existing Player row) with a `post_save` hook that creates
        the roster assignment once the registration commits."""
        # Local import to avoid a circular import at module load.
        from ..views.registration import UidModal
        assert interaction.guild is not None
        try:
            async with self.bot.db.session() as s:
                existing = await s.scalar(
                    select(Player).where(
                        Player.guild_id == interaction.guild.id,
                        Player.discord_id == interaction.user.id,
                    )
                )
        except Exception as e:
            await error_reply(interaction, e)
            return

        cog = self

        async def _create_assignment(
            inter: discord.Interaction, player_id: int,
        ) -> None:
            await cog._create_self_assignment(
                inter, match_id=match_id, team_id=team_id,
                position_id=(position_id or None),
            )

        try:
            await interaction.response.send_modal(
                UidModal(
                    self.bot, prefill=existing,
                    post_save=_create_assignment,
                ),
            )
        except discord.HTTPException as e:
            try:
                await interaction.followup.send(
                    f"❌ Couldn't open the registration form: {e}",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def _create_self_assignment(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        team_id: int,
        position_id: int | None,
    ) -> None:
        """The RegistrationView `post_save` hook — attaches the user
        to (match, team, position). Called AFTER the Player row has
        been committed, so a caller who wasn't registered before
        now is. Sends its own ephemeral confirmation and schedules
        a board refresh. Errors here are reported to the caller but
        never rolled back onto the registration itself."""
        try:
            assert interaction.guild is not None
            user_id = interaction.user.id
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                if mrow.status in ("LOCKED", "ARCHIVED"):
                    raise MatchLockedError(
                        "That match is no longer accepting sign-ups."
                    )
                trow = await s.get(GvgTeam, team_id)
                if (
                    trow is None
                    or trow.guild_id != interaction.guild.id
                ):
                    raise NotFoundError("Team not found.")
                if position_id is not None:
                    prow = await s.get(GvgPosition, position_id)
                    if (
                        prow is None
                        or prow.guild_id != interaction.guild.id
                        or not prow.is_active
                    ):
                        raise NotFoundError(
                            "Position not found or disabled."
                        )
                dup = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == user_id,
                    )
                )
                if dup is not None:
                    raise ConflictError(
                        "You're already on this match's roster."
                    )
                # Try MAIN first; fall back to RESERVE when the MAIN
                # cap is full so the self-serve flow never fails just
                # because the roster's tight.
                await self._lock_match(s, mrow.id)
                main_count = (await s.execute(
                    select(func.count(GvgRosterAssignment.id))
                    .where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.roster_type == "MAIN",
                    )
                )).scalar_one()
                roster_type = (
                    "MAIN"
                    if main_count < mrow.main_player_limit
                    else "RESERVE"
                )
                arow = GvgRosterAssignment(
                    match_id=mrow.id,
                    team_id=trow.id,
                    user_id=user_id,
                    position_id=position_id,
                    roster_type=roster_type,
                    created_by=user_id,
                )
                s.add(arow)
                await s.commit()
                await s.refresh(arow)
                aid = arow.id
                mid = mrow.id
                tname = trow.name
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=user_id,
                action="PLAYERS_ASSIGNED",
                entity_type="GvgRosterAssignment",
                entity_id=aid,
                after={
                    "self_register": True,
                    "user_ids": [user_id],
                    "team_id": team_id,
                    "position_id": position_id,
                    "roster_type": roster_type,
                },
            )
            self._schedule_refresh(interaction.guild.id, mid)
            # Opportunistic avatar cache — image-grid view_mode
            # needs one; fire-and-forget so the reply isn't blocked
            # on a CDN fetch.
            self._schedule_avatar_cache(interaction.guild, user_id)
            note = (
                "MAIN"
                if roster_type == "MAIN" else
                "RESERVE (MAIN was full — a manager can promote you "
                "if a slot opens)"
            )
            await interaction.followup.send(
                content=(
                    f"✅ Signed you into **{tname}** as **{note}**."
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            try:
                await interaction.followup.send(
                    f"❌ Couldn't create the assignment: {e}",
                    ephemeral=True,
                )
            except discord.HTTPException:
                log.exception("self-register assignment failed")

    async def handle_board_self_reserve(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        """Flip the caller's own MAIN assignment to RESERVE in place
        (no team / position change). Refuses cleanly when the caller
        isn't on the roster, or is already RESERVE, or the match is
        LOCKED / ARCHIVED. Team + Team-Leader stay put — the leader
        invariant is "assigned somewhere on the match", which
        RESERVE still satisfies."""
        try:
            await interaction.response.defer(ephemeral=True)
            if interaction.guild is None or interaction.user.bot:
                raise ValidationError(
                    "Only guild members (not bots) can use this."
                )
            assert interaction.guild is not None
            user_id = interaction.user.id
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                if mrow.status == "LOCKED":
                    raise MatchLockedError(
                        "This match is LOCKED — roster edits are "
                        "closed. Ask a manager if you need out."
                    )
                if mrow.status == "ARCHIVED":
                    raise InvalidStateError(
                        "This match is ARCHIVED — no more edits."
                    )
                arow = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == user_id,
                    )
                )
                if arow is None:
                    raise NotFoundError(
                        "You're not on this match — click ✍️ Register "
                        "first to sign up."
                    )
                if arow.roster_type == "RESERVE":
                    raise ConflictError(
                        "You're already on the RESERVE list."
                    )
                before = {
                    "team_id": arow.team_id,
                    "position_id": arow.position_id,
                    "roster_type": arow.roster_type,
                }
                arow.roster_type = "RESERVE"
                await s.commit()
                mid = mrow.id
                aid = arow.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=user_id,
                action="PLAYERS_MOVED",
                entity_type="GvgRosterAssignment",
                entity_id=aid,
                before=before,
                after={
                    "self_reserve": True,
                    "roster_type": "RESERVE",
                    "team_id": arow.team_id,
                    "position_id": arow.position_id,
                },
            )
            self._schedule_refresh(interaction.guild.id, mid)
            await send_ok(
                interaction,
                "🛌 Moved you to the RESERVE list.",
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_self_unregister(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        """Delete the caller's own assignment + any Team Leader row
        they held on this match. Refuses cleanly when they aren't
        on the roster or the match is LOCKED / ARCHIVED. Since the
        caller can only ever affect their OWN row, no confirm step
        is needed."""
        try:
            await interaction.response.defer(ephemeral=True)
            if interaction.guild is None or interaction.user.bot:
                raise ValidationError(
                    "Only guild members (not bots) can use this."
                )
            assert interaction.guild is not None
            user_id = interaction.user.id
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                if mrow.status == "LOCKED":
                    raise MatchLockedError(
                        "This match is LOCKED — roster edits are "
                        "closed. Ask a manager if you need out."
                    )
                if mrow.status == "ARCHIVED":
                    raise InvalidStateError(
                        "This match is ARCHIVED — no more edits."
                    )
                arow = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == user_id,
                    )
                )
                if arow is None:
                    raise NotFoundError(
                        "You're not on this match — nothing to remove."
                    )
                # Clear commander badge if the caller was the commander
                # of the team they're leaving.
                team = await s.get(GvgTeam, arow.team_id)
                cleared_commander = False
                if team is not None and team.commander_user_id == user_id:
                    team.commander_user_id = None
                    cleared_commander = True
                await s.delete(arow)
                # Also drop any Team Leader rows this user held on
                # the match — the invariant "leader has any assignment
                # on this match" no longer holds after Un-Register.
                await s.execute(delete(GvgTeamLeader).where(
                    GvgTeamLeader.match_id == mrow.id,
                    GvgTeamLeader.user_id == user_id,
                ))
                await s.commit()
                mid = mrow.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=mid,
                actor_user_id=user_id,
                action="PLAYERS_UNASSIGNED",
                entity_type="GvgRosterAssignment",
                before={
                    "self_unregister": True,
                    "user_ids": [user_id],
                    "cleared_commander": cleared_commander,
                },
            )
            self._schedule_refresh(interaction.guild.id, mid)
            note = (
                "\n👑 Also cleared your commander badge on that team."
                if cleared_commander else ""
            )
            await send_ok(
                interaction,
                "✅ Removed you from this match." + note,
            )
        except Exception as e:
            await error_reply(interaction, e)

    def _team_admin_view(self, teams: list[GvgTeam]) -> discord.ui.View:
        """Build the ephemeral action View shown under the team list:
        one ➕ Add Team button, plus (when there is at least one team)
        an ✏️ Edit Team button that swaps the view for a team picker."""
        cog = self

        class _Panel(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=600)
                self.add_item(_AddBtn())
                if teams:
                    self.add_item(_EditBtn())

        class _AddBtn(discord.ui.Button):
            def __init__(self) -> None:
                super().__init__(
                    label="Add Team", emoji="➕",
                    style=discord.ButtonStyle.success,
                )

            async def callback(self, interaction: discord.Interaction) -> None:
                if not await cog.is_manager(interaction):
                    await interaction.response.send_message(
                        "❌ Only a GvG Manager can add teams.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.send_modal(_TeamAddModal(cog))

        class _EditBtn(discord.ui.Button):
            def __init__(self) -> None:
                super().__init__(
                    label="Edit Team", emoji="✏️",
                    style=discord.ButtonStyle.secondary,
                )

            async def callback(self, interaction: discord.Interaction) -> None:
                if not await cog.is_manager(interaction):
                    await interaction.response.send_message(
                        "❌ Only a GvG Manager can edit teams.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.edit_message(
                    content="Pick the team to edit:",
                    embed=None,
                    view=cog._team_edit_picker_view(teams),
                )

        return _Panel()

    def _team_edit_picker_view(
        self, teams: list[GvgTeam],
    ) -> discord.ui.View:
        """StringSelect of teams — on pick, opens the Team Edit modal
        pre-filled with the selected team's current values."""
        cog = self

        options = [
            discord.SelectOption(
                label=t.name[:100], value=str(t.id),
                emoji=(t.icon if t.icon and len(t.icon) <= 2 else None),
            )
            for t in teams[:25]
        ] or [discord.SelectOption(label="(no teams)", value="0")]

        class _PickerView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=600)
                self.add_item(_TeamPickSelect())

        class _TeamPickSelect(discord.ui.Select):
            def __init__(self) -> None:
                super().__init__(
                    placeholder="Pick a team to edit…",
                    min_values=1, max_values=1,
                    options=options,
                )

            async def callback(self, interaction: discord.Interaction) -> None:
                if not await cog.is_manager(interaction):
                    await interaction.response.send_message(
                        "❌ Only a GvG Manager can edit teams.",
                        ephemeral=True,
                    )
                    return
                raw = (self.values or ["0"])[0]
                try:
                    tid = int(raw)
                except ValueError:
                    tid = 0
                async with cog.bot.db.session() as s:
                    trow = await s.get(GvgTeam, tid)
                if (
                    trow is None
                    or interaction.guild is None
                    or trow.guild_id != interaction.guild.id
                ):
                    await interaction.response.send_message(
                        "❌ Team not found — refresh the panel.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.send_modal(
                    _TeamEditModal(cog, trow),
                )

        return _PickerView()

    def _position_admin_view(
        self, positions: list[GvgPosition],
    ) -> discord.ui.View:
        """Twin of `_team_admin_view` for positions."""
        cog = self

        class _Panel(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=600)
                self.add_item(_AddBtn())
                if positions:
                    self.add_item(_EditBtn())

        class _AddBtn(discord.ui.Button):
            def __init__(self) -> None:
                super().__init__(
                    label="Add Position", emoji="➕",
                    style=discord.ButtonStyle.success,
                )

            async def callback(self, interaction: discord.Interaction) -> None:
                if not await cog.is_manager(interaction):
                    await interaction.response.send_message(
                        "❌ Only a GvG Manager can add positions.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.send_modal(
                    _PositionAddModal(cog),
                )

        class _EditBtn(discord.ui.Button):
            def __init__(self) -> None:
                super().__init__(
                    label="Edit Position", emoji="✏️",
                    style=discord.ButtonStyle.secondary,
                )

            async def callback(self, interaction: discord.Interaction) -> None:
                if not await cog.is_manager(interaction):
                    await interaction.response.send_message(
                        "❌ Only a GvG Manager can edit positions.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.edit_message(
                    content="Pick the position to edit:",
                    embed=None,
                    view=cog._position_edit_picker_view(positions),
                )

        return _Panel()

    def _position_edit_picker_view(
        self, positions: list[GvgPosition],
    ) -> discord.ui.View:
        cog = self
        options = [
            discord.SelectOption(
                label=p.name[:100], value=str(p.id),
                emoji=(p.icon if p.icon and len(p.icon) <= 2 else None),
            )
            for p in positions[:25]
        ] or [discord.SelectOption(label="(no positions)", value="0")]

        class _PickerView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=600)
                self.add_item(_PosPickSelect())

        class _PosPickSelect(discord.ui.Select):
            def __init__(self) -> None:
                super().__init__(
                    placeholder="Pick a position to edit…",
                    min_values=1, max_values=1,
                    options=options,
                )

            async def callback(self, interaction: discord.Interaction) -> None:
                if not await cog.is_manager(interaction):
                    await interaction.response.send_message(
                        "❌ Only a GvG Manager can edit positions.",
                        ephemeral=True,
                    )
                    return
                raw = (self.values or ["0"])[0]
                try:
                    pid = int(raw)
                except ValueError:
                    pid = 0
                async with cog.bot.db.session() as s:
                    prow = await s.get(GvgPosition, pid)
                if (
                    prow is None
                    or interaction.guild is None
                    or prow.guild_id != interaction.guild.id
                ):
                    await interaction.response.send_message(
                        "❌ Position not found — refresh the panel.",
                        ephemeral=True,
                    )
                    return
                await interaction.response.send_modal(
                    _PositionEditModal(cog, prow),
                )

        return _PickerView()

    async def handle_board_assign(
        self,
        interaction: discord.Interaction,
        match_id: int,
        *,
        roster_type: str = "MAIN",
    ) -> None:
        """Team-picker step of the button-driven Assign wizard.

        A GvG manager sees every team; a team commander only sees the
        team(s) they command (guarded by allow_commander_roster_management).
        `roster_type` picks between the MAIN and RESERVE variants of
        the wizard (each has its own persistent DynamicItem class so
        both flows survive a restart)."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            is_reserve = roster_type == "RESERVE"
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                    .limit(25)
                )).scalars().all())
            if not teams:
                await interaction.followup.send(
                    "_No teams in this guild yet — add one with "
                    "`/gvg team add`._",
                    ephemeral=True,
                )
                return
            manager = await self.is_manager(interaction)
            if not manager:
                # Narrow to only the teams this actor commands (if the
                # guild allows commander self-manage).
                cmd_teams: list[GvgTeam] = []
                for t in teams:
                    if await self._actor_can_command_team(interaction, t):
                        cmd_teams.append(t)
                if not cmd_teams:
                    raise PermissionError(
                        "You need to be the commander of a team on this "
                        "match (with commander self-manage on) — or a "
                        "GvG Manager — to assign players."
                    )
                teams = cmd_teams
            options = [
                discord.SelectOption(
                    label=(t.name[:100]),
                    value=str(t.id),
                    emoji=(t.icon if t.icon and len(t.icon) <= 2 else None),
                )
                for t in teams
            ]
            view = discord.ui.View(timeout=600)
            if is_reserve:
                view.add_item(GvgAssignReserveTeamSelect(
                    match_id, options=options,
                ))
                content = (
                    "Pick the team to add **RESERVE** players to.\n"
                    "_(No MAIN-cap check; a player can only hold one "
                    "roster slot per match — reserve pick fails if "
                    "the user is already MAIN somewhere.)_"
                )
            else:
                view.add_item(GvgAssignTeamSelect(match_id, options=options))
                content = (
                    "Pick the team to assign players to.  \n"
                    "_(Advanced options — position, RESERVE — use "
                    "`/gvg roster assign` instead.)_"
                )
            await interaction.followup.send(
                content=content, view=view, ephemeral=True,
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_assign_pick_team(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        team_id: int,
        roster_type: str = "MAIN",
    ) -> None:
        """Second step of the board-Assign wizard — pick a position for
        the chosen team (or explicitly "no position"). Step 3 opens the
        UserSelect once a position is chosen."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            is_reserve = roster_type == "RESERVE"
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                trow = await s.get(GvgTeam, team_id)
                if trow is None or trow.guild_id != interaction.guild.id:
                    raise NotFoundError("Team not found in this guild.")
                await self.require_manager_or_own_team_commander(
                    interaction, trow,
                )
                positions = list((await s.execute(
                    select(GvgPosition)
                    .where(
                        GvgPosition.guild_id == interaction.guild.id,
                        GvgPosition.is_active.is_(True),
                    )
                    .order_by(GvgPosition.sort_order, GvgPosition.id)
                    .limit(24)
                )).scalars().all())
            options = [
                discord.SelectOption(
                    label="— No position —",
                    value="0",
                    description="Assign without a position slot",
                )
            ]
            for p in positions:
                p_icon = sanitize_display_icon(p.icon, interaction.guild)
                options.append(discord.SelectOption(
                    label=p.name[:100],
                    value=str(p.id),
                    emoji=(p_icon if p_icon and len(p_icon) <= 2 else None),
                ))
            view = discord.ui.View(timeout=600)
            if is_reserve:
                view.add_item(GvgAssignReservePositionSelect(
                    match_id=mrow.id, team_id=trow.id, options=options,
                ))
            else:
                view.add_item(GvgAssignPositionSelect(
                    match_id=mrow.id, team_id=trow.id, options=options,
                ))
            roster_label = "RESERVE" if is_reserve else "MAIN"
            await interaction.followup.send(
                content=(
                    f"Pick a position for **{trow.name}** — or "
                    "*— No position —* to skip. Next step picks the "
                    f"players (roster type: **{roster_label}**)."
                ),
                view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_assign_pick_position(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        team_id: int,
        position_id: int,
        roster_type: str = "MAIN",
    ) -> None:
        """Third step: show the UserSelect for the picked team + position.
        Add-More re-opens the picker with the same target. MAIN checks
        the per-match cap; RESERVE skips that check (uncapped by design)."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            is_reserve = roster_type == "RESERVE"
            pos_label = "no position"
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                trow = await s.get(GvgTeam, team_id)
                if trow is None or trow.guild_id != interaction.guild.id:
                    raise NotFoundError("Team not found in this guild.")
                await self.require_manager_or_own_team_commander(
                    interaction, trow,
                )
                if position_id != 0:
                    prow = await s.get(GvgPosition, position_id)
                    if (
                        prow is None
                        or prow.guild_id != interaction.guild.id
                    ):
                        raise NotFoundError("Position not found.")
                    if not prow.is_active:
                        raise ValidationError(
                            f"Position **{prow.name}** is disabled — "
                            "can't assign new players to it."
                        )
                    pos_label = prow.name
                if is_reserve:
                    # RESERVE isn't capped by main_player_limit —
                    # Discord's UserSelect max_values is the only
                    # per-click limit (25).
                    max_pick = 25
                else:
                    main_count = (await s.execute(
                        select(func.count(GvgRosterAssignment.id))
                        .where(
                            GvgRosterAssignment.match_id == mrow.id,
                            GvgRosterAssignment.roster_type == "MAIN",
                        )
                    )).scalar_one()
                    remaining = mrow.main_player_limit - main_count
                    if remaining <= 0:
                        raise RosterFullError(
                            f"Main roster full ({main_count}/{mrow.main_player_limit})."
                        )
                    max_pick = min(25, remaining)
            view = discord.ui.View(timeout=600)
            view.add_item(GvgRosterUserSelect(
                match_id=mrow.id, team_id=trow.id,
                position_id=position_id, roster_type=roster_type,
                max_values=max_pick,
            ))
            view.add_item(GvgAddMoreButton(
                match_id=mrow.id, team_id=trow.id,
                position_id=position_id, roster_type=roster_type,
            ))
            await interaction.followup.send(
                content=(
                    f"Pick up to **{max_pick}** player(s) to add to "
                    f"**{trow.name}** / **{pos_label}** as **{roster_type}**."
                ),
                view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_leader(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        """Step 1 of the Set Team Leader wizard — pick which team's
        leader slot to edit. A GvG manager sees every team; a team
        commander only sees the team(s) they command (guarded by
        allow_commander_roster_management)."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                    .limit(25)
                )).scalars().all())
            if not teams:
                await interaction.followup.send(
                    "_No teams in this guild yet — add one with "
                    "`/gvg team add`._",
                    ephemeral=True,
                )
                return
            manager = await self.is_manager(interaction)
            if not manager:
                cmd_teams: list[GvgTeam] = []
                for t in teams:
                    if await self._actor_can_command_team(interaction, t):
                        cmd_teams.append(t)
                if not cmd_teams:
                    raise PermissionError(
                        "You need to be the commander of a team on this "
                        "match (with commander self-manage on) — or a "
                        "GvG Manager — to set a team leader."
                    )
                teams = cmd_teams
            options = [
                discord.SelectOption(
                    label=(t.name[:100]),
                    value=str(t.id),
                    emoji=(t.icon if t.icon and len(t.icon) <= 2 else None),
                )
                for t in teams
            ]
            view = discord.ui.View(timeout=600)
            view.add_item(GvgLeaderTeamSelect(match_id, options=options))
            await interaction.followup.send(
                content=(
                    "Pick the team to set (or clear) its **Team Leader** "
                    "for this match.  \n"
                    "_The leader can be any player on the match — they "
                    "don't have to be a member of the team they lead._"
                ),
                view=view, ephemeral=True,
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_leader_pick_team(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        team_id: int,
    ) -> None:
        """Step 2 of the Set Team Leader wizard — show a UserSelect
        that searches the whole guild. The commit handler validates
        the pick is on the match roster. A `Clear current leader`
        button appears next to it when a leader is already set."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                trow = await s.get(GvgTeam, team_id)
                if trow is None or trow.guild_id != interaction.guild.id:
                    raise NotFoundError("Team not found in this guild.")
                if not await self.is_manager(interaction):
                    if not await self._actor_can_command_team(
                        interaction, trow,
                    ):
                        raise PermissionError(
                            "You can only change the leader of a team "
                            "you command."
                        )
                any_assigned = await s.scalar(
                    select(func.count(GvgRosterAssignment.id))
                    .where(GvgRosterAssignment.match_id == mrow.id)
                )
                existing = await s.scalar(
                    select(GvgTeamLeader).where(
                        GvgTeamLeader.match_id == mrow.id,
                        GvgTeamLeader.team_id == trow.id,
                    )
                )
            if not any_assigned:
                await interaction.followup.send(
                    f"_**{mrow.name}** has no assigned players yet — "
                    "add some with the ➕ Assign Players button first._",
                    ephemeral=True,
                )
                return
            view = discord.ui.View(timeout=600)
            view.add_item(GvgLeaderUserSelect(
                match_id=mrow.id, team_id=trow.id,
            ))
            if existing is not None:
                view.add_item(GvgLeaderClearButton(
                    match_id=mrow.id, team_id=trow.id,
                ))
            current_line = (
                f"\n_Current leader: <@{existing.user_id}>._"
                if existing is not None else ""
            )
            await interaction.followup.send(
                content=(
                    f"Pick the **Team Leader** for **{trow.name}** on "
                    f"**{mrow.name}**.\n"
                    "_The nominee must already be on the match "
                    "roster (any team, MAIN or RESERVE)._"
                    f"{current_line}"
                ),
                view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_leader_pick_user(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        team_id: int,
        choice: str,
    ) -> None:
        """Step 3 of the Set Team Leader wizard — commit the leader
        row (or clear it) via the same code path as `/gvg team leader`."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                trow = await s.get(GvgTeam, team_id)
                if trow is None or trow.guild_id != interaction.guild.id:
                    raise NotFoundError("Team not found in this guild.")
                if not await self.is_manager(interaction):
                    if not await self._actor_can_command_team(
                        interaction, trow,
                    ):
                        raise PermissionError(
                            "You can only change the leader of a team "
                            "you command."
                        )
                existing = await s.scalar(
                    select(GvgTeamLeader).where(
                        GvgTeamLeader.match_id == mrow.id,
                        GvgTeamLeader.team_id == trow.id,
                    )
                )
                if choice == "__clear__":
                    if existing is None:
                        raise NotFoundError(
                            f"Team **{trow.name}** has no leader on "
                            f"**{mrow.name}**."
                        )
                    before_uid = existing.user_id
                    await s.delete(existing)
                    await s.commit()
                    await self._audit(
                        guild_id=interaction.guild.id, match_id=mrow.id,
                        actor_user_id=interaction.user.id,
                        action="TEAM_LEADER_REMOVED",
                        entity_type="GvgTeamLeader",
                        entity_id=trow.id,
                        before={"user_id": before_uid},
                    )
                    self._schedule_refresh(interaction.guild.id, mrow.id)
                    await send_ok(
                        interaction,
                        f"🎗️ Team leader cleared for **{trow.name}** "
                        f"on **{mrow.name}**.",
                    )
                    return
                try:
                    user_id = int(choice)
                except ValueError:
                    raise ValidationError("Invalid selection.")
                arow = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == user_id,
                    )
                )
                if arow is None:
                    raise ValidationError(
                        "That user isn't on this match's roster any more "
                        "— refresh the picker and try again."
                    )
                if existing is not None:
                    existing.user_id = user_id
                    existing.created_by = interaction.user.id
                else:
                    s.add(GvgTeamLeader(
                        match_id=mrow.id, team_id=trow.id,
                        user_id=user_id, created_by=interaction.user.id,
                    ))
                await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=match_id,
                actor_user_id=interaction.user.id,
                action="TEAM_LEADER_SET",
                entity_type="GvgTeamLeader",
                entity_id=team_id,
                after={"user_id": user_id},
            )
            self._schedule_refresh(interaction.guild.id, match_id)
            await send_ok(
                interaction,
                f"🎖️ Team leader of **{trow.name}** on **{mrow.name}**: "
                f"<@{user_id}>.",
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_remove(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        """Open the Remove Players wizard — a multi UserSelect over
        the whole guild. handle_board_remove_commit validates every
        pick against the match roster before touching anything, so
        selecting a non-roster member fails cleanly rather than
        partial-writing. Team commanders (with self-manage on) may
        also open the wizard but their picks are further restricted
        to their own team at commit time."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                any_assigned = await s.scalar(
                    select(func.count(GvgRosterAssignment.id))
                    .where(GvgRosterAssignment.match_id == mrow.id)
                )
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                )).scalars().all())
            if not any_assigned:
                await interaction.followup.send(
                    "_This match has no players assigned yet._",
                    ephemeral=True,
                )
                return
            if not await self.is_manager(interaction):
                any_cmd = False
                for t in teams:
                    if await self._actor_can_command_team(interaction, t):
                        any_cmd = True
                        break
                if not any_cmd:
                    raise PermissionError(
                        "You need to be the commander of a team on this "
                        "match (with commander self-manage on) — or a "
                        "GvG Manager — to remove players from the board."
                    )
            view = discord.ui.View(timeout=600)
            view.add_item(GvgRemoveUserSelect(match_id))
            await interaction.followup.send(
                content=(
                    "Pick the player(s) to remove from this match.\n"
                    "_The users must already be on the roster. "
                    "Removing a commander asks you to confirm first._"
                ),
                view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_remove_commit(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        user_ids: list[int],
    ) -> None:
        """Delete the picked users' roster assignments (+ any Team
        Leader rows they held) via the same rules as
        `/gvg roster remove`: TEAM_COMMANDER scope check, commander
        clearance confirm (SPEC §43), audit, refresh."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            uids = list({int(u) for u in user_ids})
            commander_uids: list[int] = []
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                rows = list((await s.execute(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id.in_(uids),
                    )
                )).scalars().all())
                if not rows:
                    raise NotFoundError(
                        "None of those users are on this match's roster."
                    )
                if not await self.is_manager(interaction):
                    team_ids = {r.team_id for r in rows}
                    if len(team_ids) != 1:
                        raise PermissionError(
                            "Team commanders can only remove players "
                            "from their own team."
                        )
                    only_team = await s.get(GvgTeam, next(iter(team_ids)))
                    if (
                        only_team is None
                        or not await self._actor_can_command_team(
                            interaction, only_team,
                        )
                    ):
                        raise PermissionError(
                            "Only the team commander (with commander "
                            "self-manage on) or a GvG Manager can "
                            "remove these players."
                        )
                for r in rows:
                    team = await s.get(GvgTeam, r.team_id)
                    if (
                        team is not None
                        and team.commander_user_id == r.user_id
                    ):
                        commander_uids.append(r.user_id)
                mid = mrow.id
                row_uids = [r.user_id for r in rows]
            if commander_uids:
                confirm = ConfirmView(actor_id=interaction.user.id)
                mentions = ", ".join(f"<@{u}>" for u in commander_uids)
                await interaction.followup.send(
                    content=(
                        f"⚠️ You're about to remove "
                        f"**{len(commander_uids)}** commander(s) from "
                        f"their team(s): {mentions}\n"
                        "Their team(s) will lose the commander badge."
                    ),
                    view=confirm, ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                ok = await confirm.wait_result()
                if not ok:
                    return
            async with self.bot.db.session() as s:
                rows = list((await s.execute(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mid,
                        GvgRosterAssignment.user_id.in_(row_uids),
                    )
                )).scalars().all())
                cleared_cmd: list[int] = []
                for r in rows:
                    team = await s.get(GvgTeam, r.team_id)
                    if (
                        team is not None
                        and team.commander_user_id == r.user_id
                    ):
                        team.commander_user_id = None
                        cleared_cmd.append(team.id)
                    await s.delete(r)
                await s.execute(delete(GvgTeamLeader).where(
                    GvgTeamLeader.match_id == mid,
                    GvgTeamLeader.user_id.in_(row_uids),
                ))
                await s.commit()
                mrow = await s.get(GvgMatch, mid)
            await self._audit(
                guild_id=interaction.guild.id, match_id=mrow.id,
                actor_user_id=interaction.user.id,
                action="PLAYERS_UNASSIGNED",
                entity_type="GvgRosterAssignment",
                before={"user_ids": [r.user_id for r in rows]},
            )
            self._schedule_refresh(interaction.guild.id, mrow.id)
            parts = [
                f"✅ Removed {len(rows)} player(s) from **{mrow.name}**."
            ]
            if cleared_cmd:
                parts.append(
                    f"👑 Cleared commander on {len(cleared_cmd)} team(s)."
                )
            await send_ok(interaction, "\n".join(parts))
        except Exception as e:
            await error_reply(interaction, e)

    async def handle_board_move(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        """Step 1 of the button-driven Move wizard: pick a player.

        Manager path opens a UserSelect over the whole guild — the
        handler validates the pick against the match roster. A team
        commander sees the same picker but their eventual pick is
        further restricted to players on the team(s) they command
        (checked in the target step; friendly error if they miss).
        This replaces an earlier StringSelect capped at 25 options
        which silently truncated a full 30-player roster."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                any_assigned = await s.scalar(
                    select(func.count(GvgRosterAssignment.id))
                    .where(GvgRosterAssignment.match_id == mrow.id)
                )
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                )).scalars().all())
            if not any_assigned:
                await interaction.followup.send(
                    "_This match has no players assigned yet._",
                    ephemeral=True,
                )
                return
            # Front-load the commander-scope check so a non-commander
            # doesn't get to open the picker at all.
            if not await self.is_manager(interaction):
                any_cmd = False
                for t in teams:
                    if await self._actor_can_command_team(interaction, t):
                        any_cmd = True
                        break
                if not any_cmd:
                    raise PermissionError(
                        "You need to be the commander of a team on this "
                        "match (with commander self-manage on) — or a "
                        "GvG Manager — to move players from the board."
                    )
            view = MoveWizardPickPlayerView(
                self, match_id=match_id,
                actor_id=interaction.user.id,
            )
            await interaction.followup.send(
                content=(
                    "Pick the player to move.\n"
                    "_The player must already be on the match roster._"
                ),
                view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def _move_wizard_present_target(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        user_id: int,
    ) -> None:
        """Step 2 — after a player is picked, render destination team +
        position + roster-type selectors."""
        try:
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                arow = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == user_id,
                    )
                )
                if arow is None:
                    raise NotFoundError(
                        "That user isn't on this match's roster — pick "
                        "one of the assigned players."
                    )
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                )).scalars().all())
                positions = list((await s.execute(
                    select(GvgPosition)
                    .where(
                        GvgPosition.guild_id == interaction.guild.id,
                        GvgPosition.is_active.is_(True),
                    )
                    .order_by(GvgPosition.sort_order)
                )).scalars().all())
                # Commander source-team check: only players on a team
                # the actor commands are movable. `_move_wizard_commit`
                # also verifies this, but catching it here gives a
                # clean error before the destination selectors render.
                if not await self.is_manager(interaction):
                    src_team = await s.get(GvgTeam, arow.team_id)
                    if (
                        src_team is None
                        or not await self._actor_can_command_team(
                            interaction, src_team,
                        )
                    ):
                        raise PermissionError(
                            "You can only move players who are on a "
                            "team you command."
                        )
            # Narrow destination teams for a commander.
            is_mgr = await self.is_manager(interaction)
            if not is_mgr:
                allowed: list[GvgTeam] = []
                for t in teams:
                    if await self._actor_can_command_team(interaction, t):
                        allowed.append(t)
                teams = allowed
                if not teams:
                    raise PermissionError(
                        "You don't command any team on this match."
                    )
            team_options = [
                discord.SelectOption(
                    label=t.name[:100],
                    value=str(t.id),
                    description=(
                        "current team" if t.id == arow.team_id else None
                    ),
                    emoji=(t.icon if t.icon and len(t.icon) <= 2 else None),
                    default=(t.id == arow.team_id),
                )
                for t in teams[:25]
            ]
            position_options = [
                discord.SelectOption(
                    label=p.name[:100],
                    value=str(p.id),
                    emoji=(p.icon if p.icon and len(p.icon) <= 2 else None),
                )
                for p in positions[:23]
            ]
            member = interaction.guild.get_member(user_id)
            who = member.mention if member else f"<@{user_id}>"
            view = MoveWizardTargetView(
                self, match_id=match_id, user_id=user_id,
                actor_id=interaction.user.id,
                team_options=team_options,
                position_options=position_options,
            )
            await interaction.response.edit_message(
                content=(
                    f"Move {who} — pick destination team, then optionally "
                    "change position or roster type. Submit when ready."
                ),
                view=view,
            )
        except Exception as e:
            # Best-effort ephemeral error; if response was already used,
            # fall through to followup.
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"❌ {e}", ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"❌ {e}", ephemeral=True,
                    )
            except Exception:
                log.exception("move-wizard target present failed")

    async def _move_wizard_commit(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        user_id: int,
        team_id: int,
        position_choice: str | None,
        new_type: str | None,
    ) -> None:
        """Step 3 — apply the move via the same code path /gvg roster move
        uses (identical permission checks + audit + auto-refresh)."""
        try:
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                arow = await s.scalar(
                    select(GvgRosterAssignment).where(
                        GvgRosterAssignment.match_id == mrow.id,
                        GvgRosterAssignment.user_id == user_id,
                    )
                )
                if arow is None:
                    raise NotFoundError(
                        "That player is no longer on this match's roster."
                    )
                before = {
                    "team_id": arow.team_id,
                    "position_id": arow.position_id,
                    "roster_type": arow.roster_type,
                }
                new_team = await s.get(GvgTeam, team_id)
                if (
                    new_team is None
                    or new_team.guild_id != interaction.guild.id
                ):
                    raise NotFoundError("Destination team not found.")
                # Same commander check as /gvg roster move.
                if not await self.is_manager(interaction):
                    src_team = await s.get(GvgTeam, before["team_id"])
                    can_src = src_team is not None and await self._actor_can_command_team(
                        interaction, src_team,
                    )
                    can_dst = await self._actor_can_command_team(
                        interaction, new_team,
                    )
                    if not (can_src and can_dst):
                        raise PermissionError(
                            "Team commanders can only move players inside "
                            "their own team."
                        )
                arow.team_id = new_team.id
                if position_choice not in (None, "__keep__"):
                    if position_choice == "__clear__":
                        arow.position_id = None
                    else:
                        try:
                            pid = int(position_choice)
                        except (ValueError, TypeError):
                            pid = 0
                        prow = await s.get(GvgPosition, pid) if pid else None
                        if prow is None or prow.guild_id != interaction.guild.id:
                            raise NotFoundError(
                                "Destination position not found."
                            )
                        if not prow.is_active:
                            raise ValidationError(
                                f"Position **{prow.name}** is disabled — "
                                "can't assign new players to it."
                            )
                        arow.position_id = prow.id
                if new_type is not None and new_type != arow.roster_type:
                    if new_type == "MAIN":
                        await self._lock_match(s, mrow.id)
                        main_count = (await s.execute(
                            select(func.count(GvgRosterAssignment.id))
                            .where(
                                GvgRosterAssignment.match_id == mrow.id,
                                GvgRosterAssignment.roster_type == "MAIN",
                            )
                        )).scalar_one()
                        if main_count >= mrow.main_player_limit:
                            raise RosterFullError(
                                f"Main roster full "
                                f"({main_count}/{mrow.main_player_limit})."
                            )
                    arow.roster_type = new_type
                old_team = await s.get(GvgTeam, before["team_id"])
                if (
                    old_team is not None
                    and old_team.commander_user_id == user_id
                    and (old_team.id != arow.team_id or arow.roster_type == "RESERVE")
                ):
                    old_team.commander_user_id = None
                # Team Leader rows survive a move — a leader may lead
                # a team they aren't a member of.
                await s.commit()
            await self._audit(
                guild_id=interaction.guild.id, match_id=mrow.id,
                actor_user_id=interaction.user.id,
                action="PLAYERS_MOVED", entity_type="GvgRosterAssignment",
                entity_id=arow.id, before=before,
                after={
                    "team_id": arow.team_id,
                    "position_id": arow.position_id,
                    "roster_type": arow.roster_type,
                    "user_id": user_id,
                },
            )
            self._schedule_refresh(interaction.guild.id, mrow.id)
            await send_ok(interaction, (
                f"✅ Moved <@{user_id}> to **{new_team.name}**"
                + (f" as **{arow.roster_type}**" if new_type else "")
                + "."
            ))
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
            except Exception:
                log.exception("move-wizard commit failed")

    # ------------------------------------------------------------------
    # Bulk Move wizard handlers
    # ------------------------------------------------------------------

    async def handle_board_bulk_move(
        self, interaction: discord.Interaction, match_id: int,
    ) -> None:
        """Step 1 of the Bulk Move wizard: show a source-team picker.

        Only teams that actually have players on this match are offered —
        an empty team can't contribute anyone to move."""
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                )).scalars().all())
                # Teams that have ≥1 player on this match.
                populated_ids: set[int] = set((await s.execute(
                    select(GvgRosterAssignment.team_id).distinct()
                    .where(GvgRosterAssignment.match_id == match_id)
                )).scalars().all())
            if not populated_ids:
                await interaction.followup.send(
                    "_No players assigned to this match yet._", ephemeral=True,
                )
                return
            if not await self.is_manager(interaction):
                any_cmd = any(
                    await self._actor_can_command_team(interaction, t)
                    for t in teams
                )
                if not any_cmd:
                    raise PermissionError(
                        "You need to be a GvG Manager — or the commander of "
                        "a team on this match — to use Bulk Move."
                    )
            src_teams = [t for t in teams if t.id in populated_ids]
            team_opts = [
                discord.SelectOption(
                    label=t.name[:100],
                    value=str(t.id),
                    emoji=(t.icon if t.icon and len(t.icon) <= 2 else None),
                )
                for t in src_teams[:25]
            ]
            view = BulkMoveStep1View(
                self, match_id=match_id,
                actor_id=interaction.user.id,
                team_options=team_opts,
            )
            await interaction.followup.send(
                content="**Bulk Move** — step 1 of 2: pick the **source team**.",
                view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def _bulk_move_step2(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        actor_id: int,
        src_team_id: int,
    ) -> None:
        """Step 2: build player picker + destination selects for the
        chosen source team."""
        try:
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                src_team = await s.get(GvgTeam, src_team_id)
                if src_team is None or src_team.guild_id != interaction.guild.id:
                    raise NotFoundError("Source team not found.")
                # Commander scope check on the source team.
                if not await self.is_manager(interaction):
                    if not await self._actor_can_command_team(
                        interaction, src_team,
                    ):
                        raise PermissionError(
                            "You can only move players from a team you command."
                        )
                assigns = list((await s.execute(
                    select(GvgRosterAssignment)
                    .where(
                        GvgRosterAssignment.match_id == match_id,
                        GvgRosterAssignment.team_id == src_team_id,
                    )
                )).scalars().all())
                if not assigns:
                    raise NotFoundError(
                        f"**{src_team.name}** has no players on this match."
                    )
                all_teams = list((await s.execute(
                    select(GvgTeam)
                    .where(GvgTeam.guild_id == interaction.guild.id)
                    .order_by(GvgTeam.sort_order, GvgTeam.id)
                )).scalars().all())
                positions = list((await s.execute(
                    select(GvgPosition)
                    .where(
                        GvgPosition.guild_id == interaction.guild.id,
                        GvgPosition.is_active.is_(True),
                    )
                    .order_by(GvgPosition.sort_order)
                )).scalars().all())
                # Resolve display names for the player picker.
                player_rows: dict[int, Player | None] = {}
                profiles: dict[int, GvgPlayerProfile | None] = {}
                for a in assigns:
                    player_rows[a.user_id] = await s.scalar(
                        select(Player).where(Player.user_id == a.user_id)
                    )
                    profiles[a.user_id] = await s.scalar(
                        select(GvgPlayerProfile).where(
                            GvgPlayerProfile.user_id == a.user_id,
                            GvgPlayerProfile.guild_id == interaction.guild.id,
                        )
                    )
            # Build player display names.
            def _player_label(a: GvgRosterAssignment) -> str:
                prof = profiles.get(a.user_id)
                pl = player_rows.get(a.user_id)
                if prof and prof.display_name:
                    return prof.display_name[:100]
                if pl and pl.ingame_name:
                    return pl.ingame_name[:100]
                m = interaction.guild.get_member(a.user_id) if interaction.guild else None  # type: ignore[union-attr]
                return (m.display_name if m else f"User {a.user_id}")[:100]

            # Player options: ALL sentinel first, then individuals (up to 24).
            player_opts: list[discord.SelectOption] = [
                discord.SelectOption(
                    label=f"ALL ({len(assigns)} players)",
                    value=_ALL_SENTINEL,
                    description="Move every player from this team",
                    emoji="⚡",
                ),
            ]
            for a in assigns[:24]:
                player_opts.append(discord.SelectOption(
                    label=_player_label(a),
                    value=str(a.user_id),
                    description=f"{'MAIN' if a.roster_type == 'MAIN' else 'RESERVE'}",
                ))
            if len(assigns) > 24:
                player_opts[0] = discord.SelectOption(
                    label=f"ALL ({len(assigns)} players — select this to move all)",
                    value=_ALL_SENTINEL,
                    description="Showing first 24 individually; pick ALL for everyone",
                    emoji="⚡",
                )

            # Destination team options (all teams, mark source as current).
            is_mgr = await self.is_manager(interaction)
            dest_teams = all_teams
            if not is_mgr:
                dest_teams = [
                    t for t in all_teams
                    if await self._actor_can_command_team(interaction, t)
                ]
            dest_opts = [
                discord.SelectOption(
                    label=t.name[:100],
                    value=str(t.id),
                    emoji=(t.icon if t.icon and len(t.icon) <= 2 else None),
                    description="current source team" if t.id == src_team_id else None,
                )
                for t in dest_teams[:25]
            ]

            pos_opts = [
                discord.SelectOption(
                    label=p.name[:100],
                    value=str(p.id),
                    emoji=(p.icon if p.icon and len(p.icon) <= 2 else None),
                )
                for p in positions[:23]
            ]

            view = BulkMoveStep2View(
                self,
                match_id=match_id,
                src_team_id=src_team_id,
                actor_id=actor_id,
                player_options=player_opts,
                dest_options=dest_opts,
                position_options=pos_opts,
            )
            await interaction.response.edit_message(
                content=(
                    f"**Bulk Move** — step 2 of 2: moving from **{src_team.name}**.\n"
                    "Pick players (or ALL), destination team, and optional overrides."
                ),
                view=view,
            )
        except Exception as e:
            await error_reply(interaction, e)

    async def _bulk_move_commit(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        src_team_id: int,
        player_values: list[str],
        dest_team_id: int,
        position_choice: str | None,
        new_type: str | None,
    ) -> None:
        """Commit the bulk move: resolve ALL sentinel, validate, then
        apply each move in a single transaction."""
        try:
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                mrow = await s.get(GvgMatch, match_id)
                if mrow is None or mrow.guild_id != interaction.guild.id:
                    raise NotFoundError("Match not found.")
                self._reject_if_locked(mrow)
                dest_team = await s.get(GvgTeam, dest_team_id)
                if dest_team is None or dest_team.guild_id != interaction.guild.id:
                    raise NotFoundError("Destination team not found.")
                # Permission: commander must command both src and dst.
                if not await self.is_manager(interaction):
                    src_team = await s.get(GvgTeam, src_team_id)
                    can_src = src_team is not None and await self._actor_can_command_team(
                        interaction, src_team,
                    )
                    can_dst = await self._actor_can_command_team(
                        interaction, dest_team,
                    )
                    if not (can_src and can_dst):
                        raise PermissionError(
                            "You can only move players between teams you command."
                        )
                # Resolve ALL sentinel.
                if _ALL_SENTINEL in player_values:
                    rows = list((await s.execute(
                        select(GvgRosterAssignment).where(
                            GvgRosterAssignment.match_id == match_id,
                            GvgRosterAssignment.team_id == src_team_id,
                        )
                    )).scalars().all())
                else:
                    uids = set()
                    for v in player_values:
                        try:
                            uids.add(int(v))
                        except (ValueError, TypeError):
                            pass
                    rows = list((await s.execute(
                        select(GvgRosterAssignment).where(
                            GvgRosterAssignment.match_id == match_id,
                            GvgRosterAssignment.team_id == src_team_id,
                            GvgRosterAssignment.user_id.in_(uids),
                        )
                    )).scalars().all())
                if not rows:
                    raise NotFoundError(
                        "None of the selected players are on the source team."
                    )
                # Main-limit check upfront (only relevant when forcing MAIN).
                if new_type == "MAIN":
                    await self._lock_match(s, mrow.id)
                    current_main = (await s.scalar(
                        select(func.count(GvgRosterAssignment.id)).where(
                            GvgRosterAssignment.match_id == match_id,
                            GvgRosterAssignment.roster_type == "MAIN",
                        )
                    )) or 0
                    moving_to_main = sum(
                        1 for r in rows if r.roster_type != "MAIN"
                    )
                    if current_main + moving_to_main > mrow.main_player_limit:
                        raise RosterFullError(
                            f"Moving all selected players as MAIN would exceed "
                            f"the limit ({current_main + moving_to_main}/"
                            f"{mrow.main_player_limit})."
                        )
                # Resolve new position once.
                new_pos_id: int | None | bool = False  # False = keep
                if position_choice not in (None, "__keep__"):
                    if position_choice == "__clear__":
                        new_pos_id = None
                    else:
                        try:
                            pid = int(position_choice)
                        except (ValueError, TypeError):
                            pid = 0
                        prow = await s.get(GvgPosition, pid) if pid else None
                        if prow is None or prow.guild_id != interaction.guild.id:
                            raise NotFoundError("Destination position not found.")
                        if not prow.is_active:
                            raise ValidationError(
                                f"Position **{prow.name}** is disabled."
                            )
                        new_pos_id = prow.id

                moved_uids: list[int] = []
                for arow in rows:
                    before = {
                        "team_id": arow.team_id,
                        "position_id": arow.position_id,
                        "roster_type": arow.roster_type,
                    }
                    arow.team_id = dest_team.id
                    if new_pos_id is not False:
                        arow.position_id = new_pos_id  # type: ignore[assignment]
                    if new_type is not None:
                        arow.roster_type = new_type
                    # Clear commander if the player was commander of src team
                    # and is now moving away (mirrors single-move logic).
                    src_team_obj = await s.get(GvgTeam, src_team_id)
                    if (
                        src_team_obj is not None
                        and src_team_obj.commander_user_id == arow.user_id
                        and (
                            arow.team_id != src_team_id
                            or arow.roster_type == "RESERVE"
                        )
                    ):
                        src_team_obj.commander_user_id = None
                    moved_uids.append(arow.user_id)
                await s.commit()

            # Single audit entry listing all moved players.
            await self._audit(
                guild_id=interaction.guild.id, match_id=mrow.id,
                actor_user_id=interaction.user.id,
                action="PLAYERS_MOVED", entity_type="GvgRosterAssignment",
                entity_id=match_id,
                before={"src_team_id": src_team_id, "count": len(moved_uids)},
                after={
                    "dest_team_id": dest_team_id,
                    "user_ids": moved_uids,
                    "position_choice": position_choice,
                    "new_type": new_type,
                },
            )
            self._schedule_refresh(interaction.guild.id, mrow.id)
            mentions = " ".join(f"<@{uid}>" for uid in moved_uids[:20])
            suffix = f" _(and {len(moved_uids) - 20} more)_" if len(moved_uids) > 20 else ""
            await send_ok(interaction, (
                f"✅ Moved **{len(moved_uids)}** player(s) → **{dest_team.name}**"
                + (f" as **{new_type}**" if new_type else "")
                + f".\n{mentions}{suffix}"
            ))
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
            except Exception:
                log.exception("bulk-move commit failed")

    async def _delete_board_messages(
        self, guild: discord.Guild, match_id: int,
    ) -> int:
        """Best-effort cleanup — used by `/gvg match delete` before the
        cascade removes GvgRosterMessage rows. Returns how many were
        actually deleted from Discord."""
        async with self.bot.db.session() as s:
            rows = list((await s.execute(
                select(GvgRosterMessage).where(
                    GvgRosterMessage.match_id == match_id
                )
            )).scalars().all())
        removed = 0
        for row in rows:
            channel = guild.get_channel(row.channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(row.channel_id)
                except (discord.NotFound, discord.Forbidden):
                    continue
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            try:
                await channel.get_partial_message(row.message_id).delete()
                removed += 1
            except (discord.NotFound, discord.Forbidden):
                continue
            except discord.HTTPException as e:
                log.debug("gvg board delete on match-delete: %s", e)
        return removed

    # ---------------- /gvg player ----------------

    @player_g.command(
        name="view",
        description="Show a player's stored profile (icon / color / note)",
    )
    @app_commands.describe(user="User to inspect (defaults to you)")
    async def player_view(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            assert interaction.guild is not None
            target = user or interaction.user
            if not isinstance(target, (discord.Member, discord.User)):
                raise ValidationError("Bad user.")
            async with self.bot.db.session() as s:
                prof = await s.scalar(
                    select(GvgPlayerProfile).where(
                        GvgPlayerProfile.guild_id == interaction.guild.id,
                        GvgPlayerProfile.user_id == target.id,
                    )
                )
            if prof is None:
                await interaction.followup.send(
                    f"{target.mention} has no GvG profile yet — one will be "
                    "created on their first assignment.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            color_e = (
                f" {hex_to_nearest_color_emoji(prof.color_hex)}" if prof.color_hex else ""
            )
            embed = discord.Embed(
                title=(
                    f"👤 {prof.display_name_override or resolve_display_name(interaction.guild, target.id, prof)}"
                ),
                color=hex_to_discord_color(prof.color_hex),
            )
            embed.add_field(name="Discord", value=target.mention, inline=True)
            embed.add_field(
                name="Icon", value=prof.icon or "—", inline=True,
            )
            embed.add_field(
                name="Color",
                value=(f"{prof.color_hex}{color_e}" if prof.color_hex else "—"),
                inline=True,
            )
            embed.add_field(name="Note", value=prof.note or "—", inline=False)
            await interaction.followup.send(
                embed=embed, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    @player_g.command(
        name="style",
        description="Set a player's display name / icon / color / note",
    )
    @app_commands.describe(
        user="Target user",
        display_name="Override the shown name (optional; '-' to clear)",
        icon="Icon emoji (optional; '-' to clear)",
        color="HEX color like #FF8800 (optional; '-' to clear)",
        note="Free-text note (optional; '-' to clear)",
    )
    async def player_style(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        display_name: app_commands.Range[str, 1, 64] | None = None,
        icon: app_commands.Range[str, 1, 64] | None = None,
        color: str | None = None,
        note: app_commands.Range[str, 1, 256] | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                prof = await s.scalar(
                    select(GvgPlayerProfile).where(
                        GvgPlayerProfile.guild_id == interaction.guild.id,
                        GvgPlayerProfile.user_id == user.id,
                    )
                )
                if prof is None:
                    prof = GvgPlayerProfile(
                        guild_id=interaction.guild.id, user_id=user.id,
                    )
                    s.add(prof)
                if display_name is not None:
                    prof.display_name_override = (
                        None if display_name.strip() in ("", "-") else display_name.strip()
                    )
                if icon is not None:
                    prof.icon = None if icon.strip() in ("", "-") else icon.strip()
                if color is not None:
                    prof.color_hex = None if color.strip() in ("", "-") else normalize_hex(color)
                if note is not None:
                    prof.note = None if note.strip() in ("", "-") else note.strip()
                await s.commit()
                pid = prof.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="PLAYER_STYLE_UPDATED", entity_type="GvgPlayerProfile",
                entity_id=pid, after={"user_id": user.id},
            )
            await self._refresh_matches_for_user(interaction.guild.id, user.id)
            await send_ok(interaction, f"✅ Updated profile for {user.mention}.")
        except Exception as e:
            await error_reply(interaction, e)

    @player_g.command(
        name="reset-style",
        description="Clear a player's stored style (keeps display name)",
    )
    @app_commands.describe(user="Target user")
    async def player_reset_style(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                prof = await s.scalar(
                    select(GvgPlayerProfile).where(
                        GvgPlayerProfile.guild_id == interaction.guild.id,
                        GvgPlayerProfile.user_id == user.id,
                    )
                )
                if prof is None:
                    raise NotFoundError(f"{user.mention} has no profile to reset.")
                prof.icon = None
                prof.color_hex = None
                await s.commit()
                pid = prof.id
            await self._audit(
                guild_id=interaction.guild.id, match_id=None,
                actor_user_id=interaction.user.id,
                action="PLAYER_STYLE_UPDATED", entity_type="GvgPlayerProfile",
                entity_id=pid,
            )
            await self._refresh_matches_for_user(interaction.guild.id, user.id)
            await send_ok(interaction, f"✅ Style cleared for {user.mention}.")
        except Exception as e:
            await error_reply(interaction, e)

    @player_g.command(
        name="note",
        description="Set a free-text note on a player",
    )
    @app_commands.describe(
        user="Target user",
        note="Note text (empty string to clear)",
    )
    async def player_note(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        note: app_commands.Range[str, 0, 256] | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                prof = await s.scalar(
                    select(GvgPlayerProfile).where(
                        GvgPlayerProfile.guild_id == interaction.guild.id,
                        GvgPlayerProfile.user_id == user.id,
                    )
                )
                if prof is None:
                    prof = GvgPlayerProfile(
                        guild_id=interaction.guild.id, user_id=user.id,
                    )
                    s.add(prof)
                if note is None or note.strip() in ("", "-"):
                    prof.note = None
                else:
                    prof.note = note.strip()
                await s.commit()
            await self._refresh_matches_for_user(interaction.guild.id, user.id)
            await send_ok(interaction, f"✅ Note updated for {user.mention}.")
        except Exception as e:
            await error_reply(interaction, e)

    # ---------------- /gvg audit ----------------

    @audit_g.command(
        name="list",
        description="Show the most recent GvG mutations (SPEC §41)",
    )
    @app_commands.describe(
        match="Filter to a specific match (optional)",
        limit="How many rows to show (1-25, default 15)",
    )
    async def audit_list(
        self,
        interaction: discord.Interaction,
        match: str | None = None,
        limit: app_commands.Range[int, 1, 25] | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            limit_val = int(limit) if limit is not None else 15
            match_id: int | None = None
            match_label: str | None = None
            async with self.bot.db.session() as s:
                if match is not None and match.strip():
                    mrow = await s.scalar(
                        select(GvgMatch).where(
                            GvgMatch.guild_id == interaction.guild.id,
                            GvgMatch.name == match.strip(),
                        )
                    )
                    if mrow is None:
                        raise NotFoundError(f"Match **{match}** not found.")
                    match_id = mrow.id
                    match_label = mrow.name
                stmt = (
                    select(GvgAuditLog)
                    .where(GvgAuditLog.guild_id == interaction.guild.id)
                    .order_by(GvgAuditLog.created_at.desc())
                    .limit(limit_val)
                )
                if match_id is not None:
                    stmt = stmt.where(GvgAuditLog.match_id == match_id)
                rows = list((await s.execute(stmt)).scalars().all())
            if not rows:
                await interaction.followup.send(
                    "_No audit rows yet._", ephemeral=True,
                )
                return
            lines: list[str] = []
            for r in rows:
                icon, title = _AUDIT_LABELS.get(
                    r.action, ("📝", r.action.replace("_", " ").title()),
                )
                ts = int(r.created_at.timestamp())
                extra = ""
                # Best-effort inline detail for common actions.
                if r.after_json:
                    try:
                        after = json.loads(r.after_json)
                        if "user_ids" in after and after["user_ids"]:
                            n = len(after["user_ids"])
                            extra = f" ({n} player{'s' if n != 1 else ''})"
                        elif "user_id" in after:
                            extra = f" — target <@{after['user_id']}>"
                        elif "name" in after:
                            extra = f" — {after['name']}"
                    except Exception:
                        pass
                lines.append(
                    f"<t:{ts}:R> {icon} **{title}** by <@{r.actor_user_id}>"
                    f"{extra}"
                )
            title_bits = ["📋 GvG audit"]
            if match_label:
                title_bits.append(f"— {match_label}")
            embed = discord.Embed(
                title=" ".join(title_bits),
                description="\n".join(lines)[:4000],
                color=discord.Color.blurple(),
            )
            embed.set_footer(
                text=f"Showing {len(rows)} of the most recent {limit_val} rows.",
            )
            await interaction.followup.send(
                embed=embed, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    # ---------------- /gvg avatar (cached faces for view_mode=4) ----------------
    #
    # Two entry points populate the cache:
    #   1) `/gvg avatar sync`  — admin batch, incremental via avatar_key.
    #   2) auto-cache on Register / manager Assign — 1-user fire-and-
    #      forget, keeps steady state up to date without admin action.
    # Read side is the image-grid renderer (PR 2). The cache never
    # blocks a mutation path — every write is best-effort, logs on
    # failure, does not raise.

    async def _cache_avatar(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        force: bool = False,
    ) -> str:
        """Fetch+store one user's guild avatar. Returns a status:
        `cached` (bytes written), `unchanged` (key match, skipped),
        `missing` (member not in guild), `error` (network / DB).
        Never raises — the caller (auto-cache path) fires this
        without awaiting the result."""
        try:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.HTTPException):
                    return "missing"
            asset = member.display_avatar
            key = asset.key
            async with self.bot.db.session() as s:
                existing = await s.scalar(
                    select(GvgAvatarCache).where(
                        GvgAvatarCache.guild_id == guild.id,
                        GvgAvatarCache.user_id == user_id,
                    )
                )
                if (
                    existing is not None
                    and existing.avatar_key == key
                    and not force
                ):
                    return "unchanged"
            try:
                data = await asset.read()
            except (discord.HTTPException, discord.NotFound):
                return "error"
            content_type = (
                "image/gif" if asset.is_animated() else "image/png"
            )
            async with self.bot.db.session() as s:
                existing = await s.scalar(
                    select(GvgAvatarCache).where(
                        GvgAvatarCache.guild_id == guild.id,
                        GvgAvatarCache.user_id == user_id,
                    )
                )
                if existing is None:
                    s.add(GvgAvatarCache(
                        guild_id=guild.id,
                        user_id=user_id,
                        avatar_key=key,
                        avatar_bytes=data,
                        content_type=content_type,
                    ))
                else:
                    existing.avatar_key = key
                    existing.avatar_bytes = data
                    existing.content_type = content_type
                await s.commit()
            return "cached"
        except Exception:
            log.exception(
                "avatar cache failed user=%s guild=%s",
                user_id, getattr(guild, "id", None),
            )
            return "error"

    def _schedule_avatar_cache(
        self, guild: discord.Guild, user_id: int,
    ) -> None:
        """Fire-and-forget: caches one user's avatar in the background.
        Called from assign / self-register paths so the roster gains
        an avatar without any manual sync needed. Never awaits and
        never raises."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._cache_avatar(guild, user_id))
        except RuntimeError:
            # No running loop (shouldn't happen inside a coroutine) —
            # skip silently.
            pass

    @avatar_g.command(
        name="sync",
        description="Batch-cache Discord avatars for every player on the guild's rosters",
    )
    @app_commands.describe(
        force="Re-fetch every avatar even if the Discord key is unchanged",
    )
    async def avatar_sync(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            guild = interaction.guild
            async with self.bot.db.session() as s:
                user_ids_rows = (await s.execute(
                    select(GvgRosterAssignment.user_id)
                    .join(
                        GvgMatch,
                        GvgMatch.id == GvgRosterAssignment.match_id,
                    )
                    .where(GvgMatch.guild_id == guild.id)
                    .distinct()
                )).all()
            user_ids = sorted({int(r[0]) for r in user_ids_rows})
            if not user_ids:
                await send_ok(
                    interaction,
                    "_No assigned players yet — nothing to cache._",
                )
                return
            total = len(user_ids)
            cached = unchanged = missing = errored = 0
            # Bounded concurrency: fetch a handful in parallel without
            # hammering the CDN or the DB pool.
            sem = asyncio.Semaphore(10)

            async def _one(uid: int) -> str:
                async with sem:
                    return await self._cache_avatar(
                        guild, uid, force=force,
                    )

            # Progress: edit the ephemeral every ~2s so admin sees life.
            progress_msg = await interaction.followup.send(
                content=(
                    f"⏳ Syncing avatars — 0/{total}…"
                ),
                ephemeral=True, wait=True,
            )
            done = 0
            last_edit = 0.0
            tasks = [asyncio.create_task(_one(u)) for u in user_ids]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                done += 1
                if result == "cached":
                    cached += 1
                elif result == "unchanged":
                    unchanged += 1
                elif result == "missing":
                    missing += 1
                else:
                    errored += 1
                now = asyncio.get_running_loop().time()
                if now - last_edit >= 2.0 or done == total:
                    last_edit = now
                    try:
                        await progress_msg.edit(
                            content=(
                                f"⏳ Syncing avatars — "
                                f"{done}/{total} "
                                f"(cached {cached}, unchanged "
                                f"{unchanged}, missing {missing}, "
                                f"errored {errored})…"
                            ),
                        )
                    except discord.HTTPException:
                        pass
            summary = (
                f"✅ Avatar sync done — {total} player(s):\n"
                f"• 🖼️ **{cached}** refreshed\n"
                f"• ✅ **{unchanged}** already up to date\n"
                f"• 👋 **{missing}** left the guild (no cache)\n"
                f"• ⚠️ **{errored}** failed (see logs)"
            )
            try:
                await progress_msg.edit(content=summary)
            except discord.HTTPException:
                await interaction.followup.send(
                    content=summary, ephemeral=True,
                )
        except Exception as e:
            await error_reply(interaction, e)

    @avatar_g.command(
        name="status",
        description="Show how many players' avatars are currently cached",
    )
    async def avatar_status(
        self, interaction: discord.Interaction,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            guild_id = interaction.guild.id
            async with self.bot.db.session() as s:
                cached_count = (await s.execute(
                    select(func.count(GvgAvatarCache.id))
                    .where(GvgAvatarCache.guild_id == guild_id)
                )).scalar_one()
                total_bytes = (await s.execute(
                    select(func.coalesce(
                        func.sum(func.length(GvgAvatarCache.avatar_bytes)),
                        0,
                    ))
                    .where(GvgAvatarCache.guild_id == guild_id)
                )).scalar_one()
                assigned_users = (await s.execute(
                    select(func.count(func.distinct(
                        GvgRosterAssignment.user_id,
                    )))
                    .join(
                        GvgMatch,
                        GvgMatch.id == GvgRosterAssignment.match_id,
                    )
                    .where(GvgMatch.guild_id == guild_id)
                )).scalar_one()
                cached_userids_rows = (await s.execute(
                    select(GvgAvatarCache.user_id)
                    .where(GvgAvatarCache.guild_id == guild_id)
                )).all()
                assigned_userids_rows = (await s.execute(
                    select(GvgRosterAssignment.user_id.distinct())
                    .join(
                        GvgMatch,
                        GvgMatch.id == GvgRosterAssignment.match_id,
                    )
                    .where(GvgMatch.guild_id == guild_id)
                )).all()
                cached_set = {int(r[0]) for r in cached_userids_rows}
                assigned_set = {int(r[0]) for r in assigned_userids_rows}
                missing_from_cache = len(assigned_set - cached_set)
            embed = discord.Embed(
                title="🖼️ GvG avatar cache — status",
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="Cached avatars", value=str(cached_count), inline=True,
            )
            embed.add_field(
                name="Assigned players",
                value=str(assigned_users), inline=True,
            )
            embed.add_field(
                name="Missing (need sync)",
                value=str(missing_from_cache), inline=True,
            )
            kib = int(total_bytes) // 1024
            embed.add_field(
                name="Storage",
                value=f"{kib} KiB", inline=True,
            )
            embed.set_footer(
                text="Run /gvg avatar sync to refresh missing / stale.",
            )
            await interaction.followup.send(
                embed=embed, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            await error_reply(interaction, e)

    @avatar_g.command(
        name="clear",
        description="Drop the cached avatar for one player (they'll re-cache on next sync)",
    )
    @app_commands.describe(user="Whose avatar to drop")
    async def avatar_clear(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.require_manager(interaction)
            assert interaction.guild is not None
            async with self.bot.db.session() as s:
                result = await s.execute(
                    delete(GvgAvatarCache).where(
                        GvgAvatarCache.guild_id == interaction.guild.id,
                        GvgAvatarCache.user_id == user.id,
                    )
                )
                await s.commit()
                dropped = int(result.rowcount or 0)
            if dropped == 0:
                await send_ok(
                    interaction,
                    f"_No cached avatar found for {user.mention}._",
                )
            else:
                await send_ok(
                    interaction,
                    f"🗑️ Cleared cached avatar for {user.mention}.",
                )
        except Exception as e:
            await error_reply(interaction, e)

    # ---------------- Autocomplete ----------------

    @match_edit.autocomplete("match")
    @match_delete.autocomplete("match")
    @match_view.autocomplete("match")
    @roster_assign.autocomplete("match")
    @roster_remove.autocomplete("match")
    @roster_move.autocomplete("match")
    @roster_swap.autocomplete("match")
    @roster_view.autocomplete("match")
    @roster_publish.autocomplete("match")
    @roster_refresh.autocomplete("match")
    @audit_list.autocomplete("match")
    @match_publish.autocomplete("match")
    @match_lock.autocomplete("match")
    @match_unlock.autocomplete("match")
    @match_archive.autocomplete("match")
    @match_restore.autocomplete("match")
    @match_clone.autocomplete("match")
    @team_leader.autocomplete("match")
    async def _ac_match(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        async with self.bot.db.session() as s:
            rows = list((await s.execute(
                select(GvgMatch)
                .where(GvgMatch.guild_id == interaction.guild_id)
                .order_by(GvgMatch.created_at.desc())
                .limit(25)
            )).scalars().all())
        needle = (current or "").lower().strip()
        matches = [r for r in rows if not needle or needle in r.name.lower()]
        return [
            app_commands.Choice(name=f"{r.name} [{r.status}]", value=r.name)
            for r in matches[:25]
        ]

    @team_edit.autocomplete("team")
    @team_delete.autocomplete("team")
    @team_reorder.autocomplete("team")
    @team_commander.autocomplete("team")
    @team_leader.autocomplete("team")
    @roster_assign.autocomplete("team")
    @roster_move.autocomplete("team")
    async def _ac_team(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        """Teams are guild-scoped now — no `match` filter needed."""
        if interaction.guild_id is None:
            return []
        async with self.bot.db.session() as s:
            rows = list((await s.execute(
                select(GvgTeam)
                .where(GvgTeam.guild_id == interaction.guild_id)
                .order_by(GvgTeam.sort_order, GvgTeam.id)
                .limit(25)
            )).scalars().all())
        needle = (current or "").lower().strip()
        teams = [t for t in rows if not needle or needle in t.name.lower()]
        return [
            app_commands.Choice(name=t.name, value=t.name)
            for t in teams[:25]
        ]

    @position_edit.autocomplete("position")
    @position_delete.autocomplete("position")
    @position_reorder.autocomplete("position")
    @roster_assign.autocomplete("position")
    @roster_move.autocomplete("position")
    async def _ac_position(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        async with self.bot.db.session() as s:
            rows = list((await s.execute(
                select(GvgPosition)
                .where(
                    GvgPosition.guild_id == interaction.guild_id,
                    GvgPosition.is_active.is_(True),
                )
                .order_by(GvgPosition.sort_order)
            )).scalars().all())
        needle = (current or "").lower().strip()
        positions = [p for p in rows if not needle or needle in p.name.lower()]
        return [
            app_commands.Choice(name=p.name, value=p.name)
            for p in positions[:25]
        ]

    @roster_assign.autocomplete("roster_type")
    @roster_move.autocomplete("roster_type")
    async def _ac_roster_type(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        needle = (current or "").upper().strip()
        return [
            app_commands.Choice(name=t, value=t)
            for t in ROSTER_TYPES
            if not needle or t.startswith(needle)
        ]

    # ---------------- Helpers ----------------

    async def _get_match_by_name(
        self, s, guild_id: int, name: str,
    ) -> GvgMatch:
        row = await s.scalar(
            select(GvgMatch).where(
                GvgMatch.guild_id == guild_id,
                GvgMatch.name == name.strip(),
            )
        )
        if row is None:
            raise NotFoundError(f"Match **{name}** not found.")
        return row

    async def _get_team_by_name(
        self, s, guild_id: int, name: str,
    ) -> GvgTeam:
        """Guild-scoped team lookup — teams are shared across every match
        of the guild, so this ignores match_id."""
        row = await s.scalar(
            select(GvgTeam).where(
                GvgTeam.guild_id == guild_id,
                GvgTeam.name == name.strip(),
            )
        )
        if row is None:
            raise NotFoundError(f"Team **{name}** not found in this guild.")
        return row

    async def _refresh_matches_referencing_team(
        self, guild_id: int, team_id: int,
    ) -> None:
        """Auto-refresh every match whose roster still holds this team
        (a team rename/reorder/color/commander change should fan out
        to every published board)."""
        async with self.bot.db.session() as s:
            ids = list((await s.execute(
                select(GvgRosterAssignment.match_id)
                .where(GvgRosterAssignment.team_id == team_id)
                .distinct()
            )).scalars().all())
        for mid in ids:
            self._schedule_refresh(guild_id, mid)

    async def _refresh_matches_referencing_position(
        self, guild_id: int, position_id: int,
    ) -> None:
        """Auto-refresh every published match whose roster still holds
        this position (SPEC §34 says position rename / icon / color
        edits should refresh the board)."""
        async with self.bot.db.session() as s:
            ids = list((await s.execute(
                select(GvgRosterAssignment.match_id)
                .where(
                    GvgRosterAssignment.position_id == position_id,
                )
                .distinct()
            )).scalars().all())
        for mid in ids:
            self._schedule_refresh(guild_id, mid)

    async def _refresh_matches_for_user(
        self, guild_id: int, user_id: int,
    ) -> None:
        """Auto-refresh every match whose roster holds this user
        (player style / note / display-name changes)."""
        async with self.bot.db.session() as s:
            ids = list((await s.execute(
                select(GvgRosterAssignment.match_id)
                .join(GvgMatch, GvgMatch.id == GvgRosterAssignment.match_id)
                .where(
                    GvgMatch.guild_id == guild_id,
                    GvgRosterAssignment.user_id == user_id,
                )
                .distinct()
            )).scalars().all())
        for mid in ids:
            self._schedule_refresh(guild_id, mid)

    async def _lock_match(self, s, match_id: int) -> None:
        """Row-lock the match to make the MAIN cap concurrency-safe. On
        Postgres this is SELECT … FOR UPDATE; on SQLite it's a no-op
        (dev-only) — SQLite's default is already fully-serial anyway."""
        try:
            await s.execute(
                select(GvgMatch.id)
                .where(GvgMatch.id == match_id)
                .with_for_update()
            )
        except Exception:
            # SQLite / dialects that reject FOR UPDATE — skip silently.
            pass

    def _reject_if_locked(self, m: GvgMatch) -> None:
        if m.status == "LOCKED":
            raise MatchLockedError(
                f"Match **{m.name}** is LOCKED — unlock via "
                "`/gvg match unlock` before editing."
            )
        if m.status == "ARCHIVED":
            raise InvalidStateError(
                f"Match **{m.name}** is ARCHIVED — restore via "
                "`/gvg match restore` before editing."
            )

    # ==========================================================================
    # LFG ↔ GVG integration (called by the LFG cog/views, not slash commands)
    # ==========================================================================

    _LFG_POOL_TEAM_NAME = "⚔️ GW Roster"

    async def auto_create_for_gw(
        self,
        *,
        guild: discord.Guild,
        lfg_party_id: int,
        name: str,
        main_limit: int,
        creator_id: int,
        note: str | None,
    ) -> int | None:
        """Create a GvgMatch linked to an LFG Guild War party and publish it.

        Ensures a guild-level "pool" team exists so LFG members have a home
        team until managers move them. Returns the new match_id, or None if
        the GVG roster channel isn't configured (match is created but not
        published — managers publish it later)."""
        async with self.bot.db.session() as s:
            # Get or create the pool team for this guild
            pool_team = (await s.execute(
                select(GvgTeam).where(
                    GvgTeam.guild_id == guild.id,
                    GvgTeam.name == self._LFG_POOL_TEAM_NAME,
                )
            )).scalar_one_or_none()
            if pool_team is None:
                pool_team = GvgTeam(
                    guild_id=guild.id,
                    name=self._LFG_POOL_TEAM_NAME,
                    description="Auto-created pool for LFG Guild War members",
                    icon="⚔️",
                    sort_order=0,
                )
                s.add(pool_team)
                await s.flush()

            desc = note or None
            match_row = GvgMatch(
                guild_id=guild.id,
                name=name,
                description=desc,
                main_player_limit=main_limit,
                status="PUBLISHED",
                created_by=creator_id,
                lfg_party_id=lfg_party_id,
                lfg_pool_team_id=pool_team.id,
            )
            s.add(match_row)
            await s.commit()
            await s.refresh(match_row)
            match_id = match_row.id
            pool_team_id = pool_team.id

        await self._audit(
            guild_id=guild.id, match_id=match_id,
            actor_user_id=creator_id,
            action="MATCH_CREATED", entity_type="GvgMatch",
            entity_id=match_id,
            after={"name": name, "main_limit": main_limit, "source": "lfg_gw"},
        )

        cfg = await self._settings(guild.id)
        if cfg.roster_channel_id is not None:
            try:
                await self._publish_or_refresh(guild, match_id, actor_id=creator_id, refresh_only=False)
            except Exception:
                log.exception("gvg: failed to publish auto-created GW match %s", match_id)

        return match_id

    async def sync_gw_member(
        self,
        guild_id: int,
        match_id: int,
        user_id: int,
        roster_type: str,
    ) -> None:
        """Upsert a GvgRosterAssignment for an LFG GW member.

        Finds the match's pool team and inserts or updates the assignment.
        Silently skips if the match no longer exists."""
        async with self.bot.db.session() as s:
            match_row = await s.get(GvgMatch, match_id)
            if match_row is None or match_row.lfg_pool_team_id is None:
                return

            existing = (await s.execute(
                select(GvgRosterAssignment).where(
                    GvgRosterAssignment.match_id == match_id,
                    GvgRosterAssignment.user_id == user_id,
                )
            )).scalar_one_or_none()

            if existing is not None:
                existing.roster_type = roster_type
            else:
                s.add(GvgRosterAssignment(
                    match_id=match_id,
                    team_id=match_row.lfg_pool_team_id,
                    user_id=user_id,
                    roster_type=roster_type,
                    created_by=user_id,
                ))
            await s.commit()

        self._schedule_refresh(guild_id, match_id)

    async def remove_gw_member(
        self,
        guild_id: int,
        match_id: int,
        user_id: int,
    ) -> None:
        """Remove a GvgRosterAssignment when a user leaves the LFG GW party."""
        async with self.bot.db.session() as s:
            existing = (await s.execute(
                select(GvgRosterAssignment).where(
                    GvgRosterAssignment.match_id == match_id,
                    GvgRosterAssignment.user_id == user_id,
                )
            )).scalar_one_or_none()
            if existing is not None:
                await s.delete(existing)
                await s.commit()

        self._schedule_refresh(guild_id, match_id)


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(GvG(bot))
