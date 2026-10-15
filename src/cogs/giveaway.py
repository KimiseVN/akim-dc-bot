"""Quay Thưởng (Give Away) — leader-driven raffle with an animated reveal.

Flow
----
1. Owner runs `/quaythuong-admin setup channel:#giveaway`.
2. Owner runs `/quaythuong start leader:@user gift:"iPhone 15"`.
   → Bot posts a card in the configured channel with Join + Start Roll
     + Cancel buttons.
3. Anyone clicks Join → their name enters the list.
4. Only the Game Leader (leader_id) can click Start Roll.
5. Bot pre-picks a random winner, then plays a 10-second name-cycle
   animation (10 frames @ ~1s = safe under Discord's 5-edit / 5-sec
   channel budget). Final frame reveals the winner.
6. Leader now sees Reroll (up to 3 uses) + Finish + Cancel.
7. Finish locks the result and turns the card into a big Vinh Danh
   embed with the Gift name.

Persistence
-----------
All buttons are DynamicItems keyed on the game id — the bot survives
restarts and rolling-state games are recovered on `on_ready` (see
_recover_games). Buttons are disabled per status inside _build_view
so a stale message that predates a redeploy still renders correctly.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db.models import (
    GiveawayEntry,
    GiveawayGame,
    GuildConfig,
)
from ..i18n import (
    LANG_BUTTON_EMOJI,
    LANG_BUTTON_LABEL,
    LANG_EN,
    LANG_VI,
    next_lang,
    normalize_lang,
)

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.giveaway")


# Reroll budget after the first roll. First roll doesn't count → the
# leader can end up with up to 4 different winners before Finishing.
_MAX_REROLLS = 3

# Slow ramping tail — the last 4 frames of the roll always use these
# intervals so every roll ends with a suspenseful reveal. Total = 3.75s.
_ROLL_REVEAL_TAIL: tuple[float, ...] = (0.70, 0.85, 1.00, 1.20)
# Total roll budget we aim for. Fast phase gets everything except the
# reveal tail. Discord's ~5-edit / 5-sec bucket applies to the fast
# phase only.
_ROLL_TARGET_DURATION = 10.0  # seconds
# Baseline fast-phase length used when there are few participants. 17
# fast frames + 4 reveal frames = 21 total (~9.9s at the default 0.30s
# cadence, matching the user-requested ≥3 names/sec floor).
_ROLL_DEFAULT_FAST_FRAMES = 17
# Bounds on the fast-phase edit interval.
#   - MAX enforces the ≥3 names/sec floor asked for when the crowd is
#     small (would otherwise drift to ~2.7/s if we just divided the
#     6.25s budget across 17 default frames).
#   - MIN caps how aggressive we get with large crowds. Any faster and
#     discord.py's rate-limit backpressure would blow the budget
#     anyway — 0.15s = 6.6 edits/sec.
_ROLL_MAX_FAST_INTERVAL = 0.30
_ROLL_MIN_FAST_INTERVAL = 0.15


def _compute_roll_plan(n_users: int) -> tuple[int, float]:
    """Return (fast_frames, fast_interval) so that every participant
    gets at least one on-screen appearance during the fast phase.

    Behaviour by crowd size (default = 17 frames, 6.25s fast budget):
      • N ≤ 21   → 17 fast frames at 0.30s (3.3 names/sec floor,
                   fast phase ≈ 5.1s, total ≈ 8.85s).
      • 21 < N ≤ 41 → N fast frames; interval shrinks from 0.30s down
                     to the 0.15s floor to keep the fast phase ≈ 6.25s
                     so total stays ≈ 10s.
      • N > 41   → N fast frames at 0.15s. Fast phase extends past
                   6.25s so every participant still appears; total
                   stretches with the crowd (~15s at N=75, ~19s at
                   N=100) — better than dropping users from the reel.
    """
    fast_frames = max(_ROLL_DEFAULT_FAST_FRAMES, n_users)
    fast_budget = _ROLL_TARGET_DURATION - sum(_ROLL_REVEAL_TAIL)  # ~6.25s
    interval = min(
        _ROLL_MAX_FAST_INTERVAL,
        max(_ROLL_MIN_FAST_INTERVAL, fast_budget / fast_frames),
    )
    return fast_frames, interval


def _build_spotlight_sequence(
    entry_ids: list[int], fast_frames: int,
) -> list[int]:
    """Build the frame-by-frame spotlight IDs for the fast phase.

    Strategy: shuffle the entry list, then cycle through it. This
    guarantees every participant appears at least ``fast_frames // n``
    times (i.e. at least once when fast_frames >= n). We also skip any
    entry that would repeat the previous frame — a repeat looks like
    the embed glitched.
    """
    if not entry_ids:
        return []
    pool = entry_ids.copy()
    random.shuffle(pool)
    seq: list[int] = []
    idx = 0
    last: int | None = None
    for _ in range(fast_frames):
        candidate = pool[idx % len(pool)]
        if candidate == last and len(pool) > 1:
            idx += 1
            candidate = pool[idx % len(pool)]
        seq.append(candidate)
        last = candidate
        idx += 1
        # Reshuffle each time we finish a full pass so back-to-back
        # cycles don't produce the same order.
        if idx % len(pool) == 0:
            random.shuffle(pool)
            idx = 0
    return seq


# =============================================================================
# Translations
# =============================================================================

# All card strings, keyed by language. Every embed builder passes through
# _t() so we can flip the card between VI and EN without changing shape.
_I18N: dict[str, dict[str, str]] = {
    LANG_VI: {
        # button labels
        "btn_join":       "Tham gia",
        "btn_start_roll": "Start Roll",
        "btn_reroll":     "Reroll",
        "btn_finish":     "Finish",
        "btn_cancel":     "Hủy",
        # waiting card
        "waiting_title": "🎁 QUAY THƯỞNG — Đang mở đăng ký",
        "waiting_desc": (
            "🎁 **Phần thưởng:** {gift}\n"
            "🎤 **Người tổ chức:** <@{leader}>\n\n"
            "Bấm **Tham gia** để góp mặt.\n"
            "Chỉ **người tổ chức** có thể bấm **Start Roll** để quay."
        ),
        "entries_field": "👥 Đã tham gia ({n})",
        "entries_empty": "_Chưa có ai tham gia. Bấm **Tham gia** để join._",
        "entries_more":  "_(+{n} người khác)_",
        # rolling card
        "rolling_title": "🎰 QUAY THƯỞNG — Đang quay...",
        "rolling_desc": (
            "🎁 **Phần thưởng:** {gift}\n"
            "🎤 **Tổ chức:** <@{leader}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "### 👉 <@{spotlight}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_🎲 Vòng quay đang chọn ngẫu nhiên..._"
        ),
        "rolling_footer": "Giveaway #{id} • {n} người tham gia",
        # showed card
        "showed_title": "🏆 QUAY THƯỞNG — Kết quả (chưa chốt)",
        "showed_desc": (
            "🎁 **Phần thưởng:** {gift}\n"
            "🎤 **Tổ chức:** <@{leader}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "### 🎉 <@{winner}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👑 Người tổ chức có thể **Reroll** "
            "({remaining}/{max} lượt còn lại) "
            "hoặc **Finish** để chốt winner."
        ),
        "showed_footer": (
            "Giveaway #{id} • {n} người tham gia • "
            "Đã reroll: {reroll}/{max}"
        ),
        # finished card
        "finished_title": "🎊 🏆  CHIẾN THẮNG  🏆 🎊",
        "finished_desc": (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "# 🎉 <@{winner}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🎁 **PHẦN THƯỞNG:** \n"
            "### {gift}\n\n"
            "🎤 Tổ chức bởi <@{leader}>\n"
            "👥 Có **{n}** người tham gia\n"
            "🔄 Đã reroll: **{reroll}/{max}** lượt\n\n"
            "_Chúc mừng winner! Liên hệ người tổ chức để nhận thưởng._"
        ),
        "finished_footer": "Giveaway #{id} • Đã chốt kết quả",
        # cancelled card
        "cancelled_title": "🛑 QUAY THƯỞNG — Đã hủy",
        "cancelled_desc": (
            "🎁 **Phần thưởng:** {gift}\n"
            "🎤 **Tổ chức:** <@{leader}>\n"
            "👥 **Người tham gia:** {n}\n\n"
            "Game đã bị hủy trước khi quay."
        ),
        "cancelled_footer": "Giveaway #{id}",
    },
    LANG_EN: {
        "btn_join":       "Join",
        "btn_start_roll": "Start Roll",
        "btn_reroll":     "Reroll",
        "btn_finish":     "Finish",
        "btn_cancel":     "Cancel",
        "waiting_title": "🎁 GIVEAWAY — Entries Open",
        "waiting_desc": (
            "🎁 **Prize:** {gift}\n"
            "🎤 **Host:** <@{leader}>\n\n"
            "Click **Join** to enter.\n"
            "Only the **host** can click **Start Roll** to spin."
        ),
        "entries_field": "👥 Entries ({n})",
        "entries_empty": "_No one has joined yet. Click **Join** to enter._",
        "entries_more":  "_(+{n} more)_",
        "rolling_title": "🎰 GIVEAWAY — Rolling...",
        "rolling_desc": (
            "🎁 **Prize:** {gift}\n"
            "🎤 **Host:** <@{leader}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "### 👉 <@{spotlight}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_🎲 The wheel is spinning..._"
        ),
        "rolling_footer": "Giveaway #{id} • {n} entries",
        "showed_title": "🏆 GIVEAWAY — Result (not locked in)",
        "showed_desc": (
            "🎁 **Prize:** {gift}\n"
            "🎤 **Host:** <@{leader}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "### 🎉 <@{winner}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👑 The host can **Reroll** "
            "({remaining}/{max} left) or **Finish** to lock the winner."
        ),
        "showed_footer": (
            "Giveaway #{id} • {n} entries • "
            "Rerolled: {reroll}/{max}"
        ),
        "finished_title": "🎊 🏆  WINNER  🏆 🎊",
        "finished_desc": (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "# 🎉 <@{winner}>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🎁 **PRIZE:** \n"
            "### {gift}\n\n"
            "🎤 Hosted by <@{leader}>\n"
            "👥 **{n}** total entries\n"
            "🔄 Rerolled: **{reroll}/{max}**\n\n"
            "_Congrats! DM the host to claim your prize._"
        ),
        "finished_footer": "Giveaway #{id} • Result locked",
        "cancelled_title": "🛑 GIVEAWAY — Cancelled",
        "cancelled_desc": (
            "🎁 **Prize:** {gift}\n"
            "🎤 **Host:** <@{leader}>\n"
            "👥 **Entries:** {n}\n\n"
            "The giveaway was cancelled before rolling."
        ),
        "cancelled_footer": "Giveaway #{id}",
    },
}


def _t(lang: str, key: str, **fmt: object) -> str:
    """Look up a card string in the requested language, defaulting back
    to VI on unknown lang codes."""
    table = _I18N.get(normalize_lang(lang), _I18N[LANG_VI])
    return table[key].format(**fmt) if fmt else table[key]


# =============================================================================
# Embed builders
# =============================================================================


def _fmt_entry_list(
    lang: str, entries: list[GiveawayEntry], max_show: int = 40,
) -> str:
    if not entries:
        return _t(lang, "entries_empty")
    lines = [f"`{i:>2}.` <@{e.user_id}>" for i, e in enumerate(
        entries[:max_show], start=1
    )]
    if len(entries) > max_show:
        lines.append(_t(lang, "entries_more", n=len(entries) - max_show))
    return "\n".join(lines)


def _game_lang(game: GiveawayGame) -> str:
    return normalize_lang(getattr(game, "language", None))


def _build_waiting_embed(
    game: GiveawayGame, entries: list[GiveawayEntry],
) -> discord.Embed:
    lang = _game_lang(game)
    embed = discord.Embed(
        title=_t(lang, "waiting_title"),
        color=discord.Color.blurple(),
    )
    embed.description = _t(
        lang, "waiting_desc",
        gift=game.gift_name, leader=game.leader_id,
    )
    embed.add_field(
        name=_t(lang, "entries_field", n=len(entries)),
        value=_fmt_entry_list(lang, entries),
        inline=False,
    )
    embed.set_footer(text=f"Giveaway #{game.id}")
    return embed


def _build_rolling_embed(
    game: GiveawayGame, entries: list[GiveawayEntry], spotlight_id: int,
) -> discord.Embed:
    lang = _game_lang(game)
    embed = discord.Embed(
        title=_t(lang, "rolling_title"),
        color=discord.Color.gold(),
    )
    embed.description = _t(
        lang, "rolling_desc",
        gift=game.gift_name, leader=game.leader_id, spotlight=spotlight_id,
    )
    embed.set_footer(
        text=_t(lang, "rolling_footer", id=game.id, n=len(entries))
    )
    return embed


def _build_showed_embed(
    game: GiveawayGame, entries: list[GiveawayEntry],
) -> discord.Embed:
    lang = _game_lang(game)
    remaining = max(0, _MAX_REROLLS - game.reroll_count)
    embed = discord.Embed(
        title=_t(lang, "showed_title"),
        color=discord.Color.orange(),
    )
    embed.description = _t(
        lang, "showed_desc",
        gift=game.gift_name, leader=game.leader_id, winner=game.winner_id,
        remaining=remaining, max=_MAX_REROLLS,
    )
    embed.set_footer(
        text=_t(
            lang, "showed_footer",
            id=game.id, n=len(entries),
            reroll=game.reroll_count, max=_MAX_REROLLS,
        )
    )
    return embed


def _build_finished_embed(
    game: GiveawayGame, entries: list[GiveawayEntry],
) -> discord.Embed:
    lang = _game_lang(game)
    embed = discord.Embed(
        title=_t(lang, "finished_title"),
        color=discord.Color.green(),
    )
    embed.description = _t(
        lang, "finished_desc",
        winner=game.winner_id, gift=game.gift_name, leader=game.leader_id,
        n=len(entries), reroll=game.reroll_count, max=_MAX_REROLLS,
    )
    embed.set_footer(text=_t(lang, "finished_footer", id=game.id))
    return embed


def _build_cancelled_embed(
    game: GiveawayGame, entries: list[GiveawayEntry],
) -> discord.Embed:
    lang = _game_lang(game)
    embed = discord.Embed(
        title=_t(lang, "cancelled_title"),
        color=discord.Color.dark_gray(),
    )
    embed.description = _t(
        lang, "cancelled_desc",
        gift=game.gift_name, leader=game.leader_id, n=len(entries),
    )
    embed.set_footer(text=_t(lang, "cancelled_footer", id=game.id))
    return embed


# =============================================================================
# Persistent buttons — one per status transition
# =============================================================================


class JoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"giveaway:join:(?P<game_id>\d+)",
):
    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"giveaway:join:{game_id}",
                label=_t(lang, "btn_join"),
                emoji="🎫",
                style=discord.ButtonStyle.primary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "JoinButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Giveaway" = interaction.client.get_cog("Giveaway")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_join(interaction, self.game_id)


class StartRollButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"giveaway:startroll:(?P<game_id>\d+)",
):
    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"giveaway:startroll:{game_id}",
                label=_t(lang, "btn_start_roll"),
                emoji="🎰",
                style=discord.ButtonStyle.success,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "StartRollButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Giveaway" = interaction.client.get_cog("Giveaway")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_start_roll(interaction, self.game_id)


class RerollButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"giveaway:reroll:(?P<game_id>\d+)",
):
    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"giveaway:reroll:{game_id}",
                label=_t(lang, "btn_reroll"),
                emoji="🔄",
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "RerollButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Giveaway" = interaction.client.get_cog("Giveaway")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_reroll(interaction, self.game_id)


class FinishButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"giveaway:finish:(?P<game_id>\d+)",
):
    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"giveaway:finish:{game_id}",
                label=_t(lang, "btn_finish"),
                emoji="✅",
                style=discord.ButtonStyle.success,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "FinishButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Giveaway" = interaction.client.get_cog("Giveaway")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_finish(interaction, self.game_id)


class CancelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"giveaway:cancel:(?P<game_id>\d+)",
):
    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"giveaway:cancel:{game_id}",
                label=_t(lang, "btn_cancel"),
                emoji="🛑",
                style=discord.ButtonStyle.danger,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "CancelButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Giveaway" = interaction.client.get_cog("Giveaway")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_cancel(interaction, self.game_id)


class LangToggleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"giveaway:lang:(?P<game_id>\d+)",
):
    """🌐 Flip the card between Vietnamese and English.

    Label always shows the *other* language (so from VI it says
    'English'; from EN it says 'Tiếng Việt'). One click persists the
    new language on the GiveawayGame row and redraws the card.
    """

    def __init__(self, game_id: int, *, lang: str = LANG_VI) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"giveaway:lang:{game_id}",
                label=LANG_BUTTON_LABEL[normalize_lang(lang)],
                emoji=LANG_BUTTON_EMOJI,
                style=discord.ButtonStyle.secondary,
                row=1,  # keep it on its own row under the game buttons
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "LangToggleButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Giveaway" = interaction.client.get_cog("Giveaway")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_lang_toggle(interaction, self.game_id)


def _build_view(game: GiveawayGame) -> discord.ui.View:
    """View shape depends on status.

    waiting  → Join + Start Roll + Cancel  + 🌐 Language
    rolling  → all disabled (animation in flight) + 🌐 Language
    showed   → Reroll + Finish + Cancel (Join disabled) + 🌐 Language
    finished / cancelled → 🌐 Language only (people may still want to
                           switch to view the past card in English).

    Language toggle is on its own row so it doesn't compete with the
    primary action buttons.
    """
    v = discord.ui.View(timeout=None)
    lang = _game_lang(game)
    if game.status == "waiting":
        v.add_item(JoinButton(game.id, lang=lang))
        v.add_item(StartRollButton(game.id, lang=lang))
        v.add_item(CancelButton(game.id, lang=lang))
    elif game.status == "rolling":
        v.add_item(JoinButton(game.id, disabled=True, lang=lang))
        v.add_item(StartRollButton(game.id, disabled=True, lang=lang))
        v.add_item(CancelButton(game.id, disabled=True, lang=lang))
    elif game.status == "showed":
        can_reroll = game.reroll_count < _MAX_REROLLS
        v.add_item(RerollButton(game.id, disabled=not can_reroll, lang=lang))
        v.add_item(FinishButton(game.id, lang=lang))
        v.add_item(CancelButton(game.id, lang=lang))
    v.add_item(LangToggleButton(game.id, lang=lang))
    return v


def _render_for_status(
    game: GiveawayGame, entries: list[GiveawayEntry],
) -> discord.Embed:
    if game.status == "waiting":
        return _build_waiting_embed(game, entries)
    if game.status == "showed":
        return _build_showed_embed(game, entries)
    if game.status == "finished":
        return _build_finished_embed(game, entries)
    if game.status == "cancelled":
        return _build_cancelled_embed(game, entries)
    # rolling is only shown mid-animation via _perform_roll_animation
    # directly — this branch is a fallback in case someone recovers a
    # game stuck at rolling on redeploy.
    spotlight = entries[0].user_id if entries else game.leader_id
    return _build_rolling_embed(game, entries, spotlight)


# =============================================================================
# Cog
# =============================================================================


class Giveaway(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        # Guard so on_ready only recovers once even if the gateway
        # reconnects mid-session.
        self._recovered = False

    # ------------------------------------------------------------------
    # Command groups
    # ------------------------------------------------------------------

    quaythuong = app_commands.Group(
        name="quaythuong",
        description="Quay Thuong — giveaway game",
        guild_only=True,
    )
    quaythuong_admin = app_commands.Group(
        name="quaythuong-admin",
        description="(Owner) Manage Quay Thuong",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        )

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if self._is_owner(interaction):
            return True
        msg = "❌ Only the **server owner** can use this command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    async def _fetch_game(self, game_id: int) -> GiveawayGame | None:
        async with self.bot.db.session() as s:
            return await s.scalar(
                select(GiveawayGame)
                .where(GiveawayGame.id == game_id)
                .options(selectinload(GiveawayGame.entries))
            )

    async def _refresh_card(
        self,
        game_id: int,
        *,
        override_embed: discord.Embed | None = None,
    ) -> None:
        """Edit the persistent card message to match the current status.

        override_embed is passed in during roll animation so we don't
        rebuild from status — that path constructs a rolling embed with
        the current spotlight id per frame.
        """
        game = await self._fetch_game(game_id)
        if game is None or game.message_id is None:
            return
        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(game.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await channel.fetch_message(game.message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        entries = list(game.entries)
        embed = override_embed or _render_for_status(game, entries)
        view = _build_view(game)
        try:
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            log.warning("giveaway refresh failed: %s", e)

    # ------------------------------------------------------------------
    # Recovery — any game stuck at rolling on redeploy needs a nudge
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._recovered:
            return
        self._recovered = True
        await self._recover_games()

    async def _recover_games(self) -> None:
        """A restart while a game was in the rolling state would leave
        the card frozen mid-animation. Flip such games to 'showed' (if a
        winner had been picked) or back to 'waiting' — safer than
        leaving them un-clickable."""
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(GiveawayGame).where(GiveawayGame.status == "rolling")
            )
            stuck = list(rows.scalars().all())
            for g in stuck:
                if g.winner_id is not None:
                    g.status = "showed"
                    log.info("giveaway: game %s stuck rolling → showed", g.id)
                else:
                    g.status = "waiting"
                    log.info(
                        "giveaway: game %s stuck rolling (no winner yet) → waiting",
                        g.id,
                    )
            if stuck:
                await s.commit()
        for g in stuck:
            await self._refresh_card(g.id)

    # ------------------------------------------------------------------
    # /quaythuong-admin setup
    # ------------------------------------------------------------------

    @quaythuong_admin.command(
        name="setup",
        description="(Owner) Configure the channel for Quay Thuong cards",
    )
    @app_commands.describe(
        channel="Channel to post giveaway cards (bot must have Send Messages)",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            cfg.giveaway_channel_id = channel.id
            await s.commit()
        await interaction.followup.send(
            f"✅ Quay Thuong channel set: {channel.mention}\n"
            "The owner can now run `/quaythuong start` to begin.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /quaythuong start
    # ------------------------------------------------------------------

    @quaythuong.command(
        name="start",
        description="(Owner) Create a new Quay Thuong game",
    )
    @app_commands.describe(
        leader="Game leader — the person who presses Start Roll",
        gift="Prize name (shown on the card + winner announcement)",
    )
    async def start_cmd(
        self,
        interaction: discord.Interaction,
        leader: discord.Member,
        gift: app_commands.Range[str, 1, 256],
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        if leader.bot:
            await interaction.followup.send(
                "❌ Bots can't be chosen as the Game Leader.", ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
        if cfg is None or cfg.giveaway_channel_id is None:
            await interaction.followup.send(
                "❌ Channel not configured. Run "
                "`/quaythuong-admin setup channel:#...` first.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        assert guild is not None
        target_channel = guild.get_channel(cfg.giveaway_channel_id)
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ Configured channel is no longer valid. Re-run setup.",
                ephemeral=True,
            )
            return

        # Insert first so the buttons carry a real game id.
        async with self.bot.db.session() as s:
            game = GiveawayGame(
                guild_id=interaction.guild_id,
                channel_id=target_channel.id,
                creator_id=interaction.user.id,
                leader_id=leader.id,
                gift_name=gift.strip(),
                status="waiting",
            )
            s.add(game)
            await s.commit()
            await s.refresh(game)
            game_id = game.id

        # Reload with entries relationship populated (empty list).
        fresh = await self._fetch_game(game_id)
        assert fresh is not None
        embed = _build_waiting_embed(fresh, list(fresh.entries))
        view = _build_view(fresh)
        try:
            msg = await target_channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {target_channel.mention}.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            row = await s.get(GiveawayGame, game_id)
            if row is not None:
                row.message_id = msg.id
                await s.commit()

        await interaction.followup.send(
            f"✅ Giveaway #{game_id} created in {target_channel.mention}.\n"
            f"🎤 Leader: {leader.mention}\n"
            f"🎁 Gift: **{gift}**",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    async def handle_join(
        self, interaction: discord.Interaction, game_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            game = await s.get(GiveawayGame, game_id)
            if game is None:
                await interaction.response.send_message(
                    "Game no longer exists.", ephemeral=True,
                )
                return
            if game.status != "waiting":
                await interaction.response.send_message(
                    "⏱ Entries are closed for this game.", ephemeral=True,
                )
                return
            existing = await s.scalar(
                select(GiveawayEntry).where(
                    GiveawayEntry.game_id == game_id,
                    GiveawayEntry.user_id == interaction.user.id,
                )
            )
            if existing is not None:
                await interaction.response.send_message(
                    "✅ You've already joined. Waiting for the Leader to press Start Roll!",
                    ephemeral=True,
                )
                return
            s.add(GiveawayEntry(
                game_id=game_id, user_id=interaction.user.id,
            ))
            await s.commit()
        await interaction.response.send_message(
            "🎫 Entry recorded — good luck!", ephemeral=True,
        )
        await self._refresh_card(game_id)

    async def handle_start_roll(
        self, interaction: discord.Interaction, game_id: int,
    ) -> None:
        # Load game + entries, guard the leader-only + status checks in
        # a single transaction that also flips status to 'rolling'. That
        # blocks a second Start Roll click from racing in.
        async with self.bot.db.session() as s:
            game = await s.scalar(
                select(GiveawayGame)
                .where(GiveawayGame.id == game_id)
                .options(selectinload(GiveawayGame.entries))
            )
            if game is None:
                await interaction.response.send_message(
                    "Game no longer exists.", ephemeral=True,
                )
                return
            if interaction.user.id != game.leader_id:
                await interaction.response.send_message(
                    "❌ Only the **Game Leader** can press Start Roll.",
                    ephemeral=True,
                )
                return
            if game.status != "waiting":
                await interaction.response.send_message(
                    f"Current status: **{game.status}** — can't roll right now.",
                    ephemeral=True,
                )
                return
            entries = list(game.entries)
            if len(entries) < 1:
                await interaction.response.send_message(
                    "❌ No entries yet — nobody to roll.",
                    ephemeral=True,
                )
                return
            game.status = "rolling"
            await s.commit()

        # Ack quickly so we don't blow the 3s window. The animation
        # itself runs after — it edits the message, not the interaction.
        await interaction.response.send_message(
            "🎰 Rolling now!", ephemeral=True,
        )

        # Kick off the animation without awaiting so this handler
        # returns quickly; the task owns the wall-clock 10s + final
        # winner commit.
        asyncio.create_task(self._run_roll_animation(game_id))

    async def handle_reroll(
        self, interaction: discord.Interaction, game_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            game = await s.scalar(
                select(GiveawayGame)
                .where(GiveawayGame.id == game_id)
                .options(selectinload(GiveawayGame.entries))
            )
            if game is None:
                await interaction.response.send_message(
                    "Game no longer exists.", ephemeral=True,
                )
                return
            if interaction.user.id != game.leader_id:
                await interaction.response.send_message(
                    "❌ Only the **Game Leader** can Reroll.", ephemeral=True,
                )
                return
            if game.status != "showed":
                await interaction.response.send_message(
                    f"Current status: **{game.status}** — can't reroll.",
                    ephemeral=True,
                )
                return
            if game.reroll_count >= _MAX_REROLLS:
                await interaction.response.send_message(
                    f"❌ Reached max rerolls (**{_MAX_REROLLS}**). "
                    "Press Finish to lock in the winner.",
                    ephemeral=True,
                )
                return
            entries = list(game.entries)
            if len(entries) < 1:
                await interaction.response.send_message(
                    "❌ Nobody to roll.", ephemeral=True,
                )
                return
            game.reroll_count += 1
            game.status = "rolling"
            await s.commit()

        await interaction.response.send_message(
            f"🔄 Reroll ({game.reroll_count}/{_MAX_REROLLS})...",
            ephemeral=True,
        )
        asyncio.create_task(self._run_roll_animation(game_id))

    async def handle_finish(
        self, interaction: discord.Interaction, game_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            game = await s.get(GiveawayGame, game_id)
            if game is None:
                await interaction.response.send_message(
                    "Game no longer exists.", ephemeral=True,
                )
                return
            if interaction.user.id != game.leader_id:
                await interaction.response.send_message(
                    "❌ Only the **Game Leader** can Finish.", ephemeral=True,
                )
                return
            if game.status != "showed" or game.winner_id is None:
                await interaction.response.send_message(
                    "❌ You must roll at least once before you can Finish.",
                    ephemeral=True,
                )
                return
            game.status = "finished"
            game.finished_at = datetime.now(timezone.utc)
            winner_id = game.winner_id
            gift = game.gift_name
            channel_id = game.channel_id
            await s.commit()

        await interaction.response.send_message(
            f"✅ Winner locked in: <@{winner_id}> receives **{gift}**!",
            ephemeral=True,
        )
        await self._refresh_card(game_id)
        # Also post a public @mention shout-out in the same channel so
        # the winner gets a ping (embed mentions don't ping).
        guild = self.bot.get_guild(interaction.guild_id or 0)
        if guild is not None:
            ch = guild.get_channel(channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    await ch.send(
                        f"🎉 Congrats <@{winner_id}> — you won **{gift}**! "
                        "DM the organizer to claim your prize.",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.Forbidden:
                    log.warning(
                        "giveaway finish: forbidden to shout in %s", channel_id,
                    )

    async def handle_cancel(
        self, interaction: discord.Interaction, game_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            game = await s.get(GiveawayGame, game_id)
            if game is None:
                await interaction.response.send_message(
                    "Game no longer exists.", ephemeral=True,
                )
                return
            is_creator = interaction.user.id == game.creator_id
            is_leader = interaction.user.id == game.leader_id
            is_owner = (
                interaction.guild is not None
                and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
            )
            if not (is_creator or is_leader or is_owner):
                await interaction.response.send_message(
                    "❌ Only the Creator, Leader, or server owner can Cancel.",
                    ephemeral=True,
                )
                return
            if game.status in ("finished", "cancelled"):
                await interaction.response.send_message(
                    f"Game is **{game.status}** — can no longer be cancelled.",
                    ephemeral=True,
                )
                return
            game.status = "cancelled"
            game.finished_at = datetime.now(timezone.utc)
            await s.commit()

        await interaction.response.send_message(
            "🛑 Giveaway cancelled.", ephemeral=True,
        )
        await self._refresh_card(game_id)

    async def handle_lang_toggle(
        self, interaction: discord.Interaction, game_id: int,
    ) -> None:
        """Flip the card's language and redraw. Anyone can toggle — the
        card is public and language is a display concern, not a state
        change that affects the outcome."""
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            game = await s.get(GiveawayGame, game_id)
            if game is None:
                await interaction.followup.send(
                    "Game không còn tồn tại. / Game no longer exists.",
                    ephemeral=True,
                )
                return
            new_lang = next_lang(game.language)
            game.language = new_lang
            await s.commit()
        # Skip animating language flips mid-roll — the animation loop is
        # already editing the card; setting the row is enough for the
        # very next frame + all future refreshes to pick it up.
        if game.status != "rolling":
            await self._refresh_card(game_id)
        ack_map = {
            LANG_VI: "🌐 Chuyển sang **Tiếng Việt**.",
            LANG_EN: "🌐 Switched to **English**.",
        }
        await interaction.followup.send(
            ack_map[new_lang], ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Roll animation
    # ------------------------------------------------------------------

    async def _run_roll_animation(self, game_id: int) -> None:
        """Fire-and-forget animation task.

        Contract:
          - game must already be in status='rolling' before this runs.
          - We pre-pick the final winner up front (before frame 0) so
            the animation is just a visual sequence — the persisted
            winner_id doesn't drift with the last-random-frame.
          - On completion, set status='showed', winner_id=winner,
            then refresh the card so the leader sees Reroll/Finish.
          - Any exception mid-animation: fall back to marking the game
            'showed' with whatever winner we managed to pick, so the
            leader isn't left with a frozen card.
        """
        try:
            game = await self._fetch_game(game_id)
            if game is None:
                return
            entries = list(game.entries)
            if not entries:
                # Shouldn't happen — handle_start_roll checks this —
                # but be defensive: flip back to waiting.
                async with self.bot.db.session() as s:
                    row = await s.get(GiveawayGame, game_id)
                    if row is not None and row.status == "rolling":
                        row.status = "waiting"
                        await s.commit()
                await self._refresh_card(game_id)
                return

            winner = random.choice(entries)
            guild = self.bot.get_guild(game.guild_id)
            channel = guild.get_channel(game.channel_id) if guild else None
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                # No visible channel to animate on — just settle the DB.
                await self._settle_winner(game_id, winner.user_id)
                return
            try:
                msg = await channel.fetch_message(game.message_id or 0)
            except (discord.NotFound, discord.Forbidden):
                await self._settle_winner(game_id, winner.user_id)
                return

            view = _build_view(game)  # disabled buttons during animation
            # Plan the animation: fast-phase length + interval depend on
            # the crowd size so every participant appears at least once.
            entry_ids = [e.user_id for e in entries]
            fast_frames, fast_interval = _compute_roll_plan(len(entry_ids))
            spotlight_seq = _build_spotlight_sequence(entry_ids, fast_frames)
            # Reveal tail frames land AFTER the fast phase. Their content
            # is random (not-same-as-last) except the final frame, which
            # is the pre-picked winner.
            reveal_frames = len(_ROLL_REVEAL_TAIL)
            total_frames = fast_frames + reveal_frames

            last_spotlight: int | None = None
            for i in range(total_frames):
                if i == total_frames - 1:
                    spotlight_id = winner.user_id
                elif i < fast_frames:
                    spotlight_id = spotlight_seq[i]
                elif len(entry_ids) > 1:
                    # Reveal-tail suspense frames: pick a random entry
                    # that isn't the previous spotlight so the buildup
                    # doesn't visibly freeze.
                    pool = [uid for uid in entry_ids if uid != last_spotlight]
                    spotlight_id = random.choice(pool)
                else:
                    spotlight_id = entry_ids[0]
                last_spotlight = spotlight_id
                embed = _build_rolling_embed(game, entries, spotlight_id)
                try:
                    await msg.edit(embed=embed, view=view)
                except discord.HTTPException as e:
                    # Rate-limited or transient — log and continue
                    # (the sleep between frames usually clears it).
                    log.warning("giveaway anim: edit failed on frame %d: %s", i, e)
                # Fast phase uses the computed uniform interval; tail
                # frames use the fixed ramping schedule.
                if i < fast_frames:
                    delay = fast_interval
                else:
                    delay = _ROLL_REVEAL_TAIL[i - fast_frames]
                await asyncio.sleep(delay)

            await self._settle_winner(game_id, winner.user_id)
        except Exception:
            log.exception("giveaway: roll animation crashed for %s", game_id)
            # Best-effort: settle whatever we've got.
            try:
                await self._settle_winner_fallback(game_id)
            except Exception:
                log.exception("giveaway: fallback settle also failed")

    async def _settle_winner(self, game_id: int, winner_id: int) -> None:
        """Commit the winner and flip status to 'showed' → refresh card."""
        async with self.bot.db.session() as s:
            game = await s.get(GiveawayGame, game_id)
            if game is None or game.status != "rolling":
                # Somebody cancelled mid-animation; leave it alone.
                return
            game.winner_id = winner_id
            game.status = "showed"
            await s.commit()
        await self._refresh_card(game_id)

    async def _settle_winner_fallback(self, game_id: int) -> None:
        """When _run_roll_animation crashes, still land in a sane state.
        Pick a random winner from current entries (or leader as a last
        resort) so the card isn't frozen at 'rolling'."""
        game = await self._fetch_game(game_id)
        if game is None or game.status != "rolling":
            return
        entries = list(game.entries)
        winner_id = (
            random.choice(entries).user_id if entries else game.leader_id
        )
        await self._settle_winner(game_id, winner_id)


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(Giveaway(bot))
