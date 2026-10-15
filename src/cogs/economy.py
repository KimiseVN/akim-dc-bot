"""Coin Economy cog — slash commands, listeners, and minigame integration.

Slash surface (all under /economy except /wallet, /leaderboard, /history)
- /economy setup ...                     (admin)   guild config knobs
- /economy ignore add|remove|list        (admin)   per-channel ignore list
- /economy reward setup ...              (admin)   minigame reward amounts
- /economy sync-members                  (admin)   wallets for existing members
- /economy add user amount [reason]      (admin)   credit
- /economy remove user amount [reason]   (admin)   debit
- /economy set user amount [reason]      (admin)   set exact balance
- /wallet [user]                         (anyone)  view balance + earned/spent
- /leaderboard [count]                   (anyone)  top balances in this guild
- /history [user] [count]                (anyone)  recent transactions

Listeners
- on_message             text reward (skip bot/short/ignored)
- on_voice_state_update  open/close VoiceSession, grant on close
- on_member_update       boost reward (premium_since None→set)
- on_player_approved     /baodanh approval reward
- on_wallet_changed      debounce + refresh Rich List leaderboard
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, desc, func, select

from ..db.models import (
    CheckinCard,
    CheckinReminderPref,
    CoinTransaction,
    DailyCheckin,
    GameWinBoard,
    GuildEconomyConfig,
    IgnoredRewardChannel,
    MiniGameRewardConfig,
    PhuHoLeaderboard,
    UserWallet,
    VoiceSession,
)
from ..economy.service import (
    InsufficientBalanceError,
    REASON_ADMIN_ADD,
    REASON_ADMIN_REMOVE,
    REASON_APPROVAL,
    REASON_CHECKIN,
    REASON_SYNC_INIT,
    RewardEngine,
    WalletService,
    is_economy_owner,
)

# Debounce window for auto-refreshing the Rich List leaderboard. Many wallet
# changes can land in quick succession (e.g. a minigame paying out 30
# participants); we coalesce them into a single edit per guild per window.
_PHUHO_DEBOUNCE_SECONDS = 5
# Debounce for the checkin card + board pair (2 messages, 2 edits each).
# A wave of check-ins right after 00:00 local time otherwise fires N * 2
# edits back-to-back and burns Discord's per-IP quota.
_CHECKIN_REFRESH_DEBOUNCE_SECONDS = 1.0

# Daily check-in (/diemdanh) constants. Spec:
#   - Random 10–100 coin per day
#   - Every 7 consecutive days: +200 coin bonus (streak resets to 1 on a miss)
_CHECKIN_MIN = 10
_CHECKIN_MAX = 100
_CHECKIN_STREAK_BONUS = 200
_CHECKIN_STREAK_LENGTH = 7
# Throttle between member DMs to avoid Discord rate-limits on large servers.
_REMINDER_DM_DELAY_SECONDS = 0.5
# How long the per-click "Checked in" ephemeral lingers before auto-delete.
_CHECKIN_ACK_DISMISS_SECONDS = 10
# Max users to list on the persistent checkin card before truncating.
_CARD_MAX_LIST = 40
# Max checkin rows shown in the history dialog before truncating.
_HISTORY_MAX_ROWS = 30


_ECON_I18N: dict[str, dict[str, str]] = {
    "ru": {
        # checkin card
        "checkin_title": "🌅 Ежедневный чекин",
        "checkin_today": "📅 Сегодня: **{today}** ({tz})",
        "checkin_reward": "🎁 Награда: случайные **{min}-{max}** {coin}/день",
        "checkin_streak_bonus": "🔥 Каждые **{n}** дней подряд: бонус **+{bonus}** {coin}",
        "checkin_cta": "Нажмите **📍 Отметиться** ниже, чтобы получить награду.\nЕсли ваше имя уже есть в списке — нажимать не нужно.",
        "checkin_reminder_hint": "🔔 Нажмите **DM Reminder**, чтобы включить/отключить напоминания.",
        "checkin_field_title": "✅ Отметились сегодня ({count})",
        "checkin_nobody": "_Никто ещё не отмечался сегодня. Первый?_",
        "checkin_footer": "Список сбрасывается в 00:00 {tz} каждый день • Серия прерывается при пропуске",
        # checkin response (ephemeral)
        "checkin_already": (
            "📌 Вы уже отметились сегодня! "
            "Сегодня вы заработали **{prev_amount:,}** {coin} "
            "(серия **{prev_streak}** дней).\n"
            "Возвращайтесь после 00:00 {tz}."
        ),
        "checkin_success": (
            "✅ **Отметились!**\n"
            "💰 Получено: **{total:,}** {coin_icon}{coin}\n"
            "🎲 Сегодняшний бросок: **{base:,}**{bonus_line}\n"
            "🔥 Текущая серия: **{streak}** дней"
        ),
        "checkin_streak_bonus_line": "\n🔥 **+{bonus:,}** {coin} бонус за серию **{n}** дней!",
        # reminder toggle
        "reminder_on": "🔔 DM-напоминания о чекине **включены**.\nВы будете получать DM каждый день (если ещё не отметились).",
        "reminder_off": "🔕 DM-напоминания о чекине **выключены**.\nВы больше не будете получать напоминания. Нажмите снова, чтобы включить.",
        "reminder_guild_disabled": "\n\n⚠️ Сервер отключил DM-напоминания глобально. Ваш выбор сохранён и вступит в силу после включения владельцем.",
        # reminder DM
        "reminder_dm": (
            "🌅 **Ежедневный чекин — {guild}**\n\n"
            "Новый день начался! Не забудьте отметиться и получить монеты.\n"
            "Зайдите на сервер **{guild}** и нажмите кнопку на карточке чекина.\n\n"
            "🎁 Случайная награда: **{min}-{max}** монет/день\n"
            "🔥 Каждые **{n}** дней подряд: бонус **+{bonus}** монет\n\n"
            "_Не хотите получать напоминания? Нажмите 🔔 DM Reminder на карточке чекина._"
        ),
        # wallet
        "wallet_title": "💼 Кошелёк: {name}",
        "wallet_balance": "Текущий баланс",
        "wallet_earned": "Всего заработано",
        "wallet_spent": "Всего потрачено",
        "wallet_no_txs": "Транзакций пока нет для {mention}.",
        # leaderboard
        "lb_title": "🏆 Таблица лидеров: {coin}",
        "lb_empty": "В этом сервере ещё нет кошельков.",
        # history
        "hist_title": "📜 История монет: {name}",
        "hist_footer": "Валюта: {coin}",
        # phuho board
        "phuho_title": "🏆  БОГАТЕЙШИЕ  🏆",
        "phuho_empty": "_На сервере ещё нет монет._\nИграйте в мини-игры или общайтесь каждый день!",
        "phuho_footer": "{guild} • Топ {count} • Обновляется автоматически",
        # checkin streak board
        "board_title": "🔥  ТОП СЕРИЙ ЧЕКИНА  🔥",
        "board_empty": "_Никто не отмечался сегодня или вчера._\n**Начните серию прямо сейчас!**",
        "board_footer": "{guild} • Топ {count} • Пропустил день ⇒ выбывает из топа",
        "board_streak": "🔥 `{streak:,}` дней _(лучшее: {best:,})_",
        # history embed (button dialog)
        "hist_embed_title": "🗓 История чекинов",
        "hist_filter_label": "🔍 Активный фильтр",
        "hist_stats_label": "📊 Статистика",
        "hist_stats_value": "**{count}** дней чекина • **{total:,}** {coin_icon} {coin}",
        "hist_details_label": "Детали",
        "hist_details_empty": "_Нет чекинов в выбранном диапазоне._",
        "hist_details_n": "Детали ({n} последних дней)",
        "hist_footer_hint": "Измените фильтр ↓ или нажмите Close для закрытия",
        "hist_more": "_(+{n} ещё дней)_",
        # language button labels
        "lang_button": "🌐 EN",
    },
    "en": {
        # checkin card
        "checkin_title": "🌅 Daily Check-in",
        "checkin_today": "📅 Today: **{today}** ({tz})",
        "checkin_reward": "🎁 Reward: random **{min}-{max}** {coin}/day",
        "checkin_streak_bonus": "🔥 Every **{n}**-day streak: bonus **+{bonus}** {coin}",
        "checkin_cta": "Press **📍 Check in now** below to claim your reward.\nIf your name is already on the list you don't need to click again.",
        "checkin_reminder_hint": "🔔 Press **DM Reminder** to toggle your personal check-in DM.",
        "checkin_field_title": "✅ Checked in today ({count})",
        "checkin_nobody": "_Nobody has checked in yet today. First?_",
        "checkin_footer": "List clears at 00:00 {tz} each day • Streak resets if you miss a day",
        # checkin response (ephemeral)
        "checkin_already": (
            "📌 You've already checked in today! "
            "Today you earned **{prev_amount:,}** {coin} "
            "(streak **{prev_streak}** days).\n"
            "Come back after 00:00 {tz}."
        ),
        "checkin_success": (
            "✅ **Checked in!**\n"
            "💰 Received: **{total:,}** {coin_icon}{coin}\n"
            "🎲 Today's roll: **{base:,}**{bonus_line}\n"
            "🔥 Current streak: **{streak}** days"
        ),
        "checkin_streak_bonus_line": "\n🔥 **+{bonus:,}** {coin} bonus for a **{n}**-day streak!",
        # reminder toggle
        "reminder_on": "🔔 Daily check-in DM reminders are **ON**.\nYou'll get a DM each day (unless you've already checked in).",
        "reminder_off": "🔕 Daily check-in DM reminders are **OFF**.\nYou won't get DM reminders anymore. Press the button again to re-enable.",
        "reminder_guild_disabled": "\n\n⚠️ The server has DM reminders disabled globally. Your preference is saved and will take effect once the owner enables DM reminders for the server.",
        # reminder DM
        "reminder_dm": (
            "🌅 **Daily check-in — {guild}**\n\n"
            "A new day just started! Don't forget to check in for coins.\n"
            "Head to **{guild}** and press the button on the check-in card.\n\n"
            "🎁 Random reward: **{min}-{max}** coin/day\n"
            "🔥 Every **{n}**-day streak: bonus **+{bonus}** coin\n\n"
            "_Don't want these DMs? Press 🔔 DM Reminder on the check-in card._"
        ),
        # wallet
        "wallet_title": "💼 {name}'s wallet",
        "wallet_balance": "Current balance",
        "wallet_earned": "Total earned",
        "wallet_spent": "Total spent",
        "wallet_no_txs": "No transactions yet for {mention}.",
        # leaderboard
        "lb_title": "🏆 {coin} Leaderboard",
        "lb_empty": "No wallets in this server yet.",
        # history
        "hist_title": "📜 Coin history: {name}",
        "hist_footer": "Currency: {coin}",
        # phuho board
        "phuho_title": "🏆  RICH LIST  🏆",
        "phuho_empty": "_Nobody has any coin in the server yet._\nPlay minigames or chat daily to start earning!",
        "phuho_footer": "{guild} • Top {count} • Auto-updates on wallet changes",
        # checkin streak board
        "board_title": "🔥  TOP CHECK-IN STREAKS  🔥",
        "board_empty": "_Nobody has checked in today or yesterday._\n**Check in and start your streak!**",
        "board_footer": "{guild} • Top {count} • Missed a day ⇒ dropped from board",
        "board_streak": "🔥 `{streak:,}` days _(best: {best:,})_",
        # history embed (button dialog)
        "hist_embed_title": "🗓 Check-in history",
        "hist_filter_label": "🔍 Active filter",
        "hist_stats_label": "📊 Stats",
        "hist_stats_value": "**{count}** check-in day(s) • **{total:,}** {coin_icon} {coin}",
        "hist_details_label": "Details",
        "hist_details_empty": "_No check-in days in the selected range._",
        "hist_details_n": "Details ({n} most recent days)",
        "hist_footer_hint": "Change the filter ↓ or press Close to dismiss",
        "hist_more": "_(+{n} more days)_",
        # language button labels
        "lang_button": "🌐 RU",
    },
}


def _et(lang: str, key: str) -> str:
    """Economy translation helper. Falls back to RU if key missing."""
    d = _ECON_I18N.get(lang, _ECON_I18N["ru"])
    return d.get(key, _ECON_I18N["ru"].get(key, key))


def _local_today_string(offset_hours: int) -> str:
    """YYYY-MM-DD in the guild's configured UTC offset."""
    return datetime.now(timezone(timedelta(hours=offset_hours))).strftime("%Y-%m-%d")


def _local_yesterday_string(offset_hours: int) -> str:
    return (
        datetime.now(timezone(timedelta(hours=offset_hours))) - timedelta(days=1)
    ).strftime("%Y-%m-%d")


def _format_tz_offset(offset_hours: int) -> str:
    if offset_hours == 0:
        return "UTC"
    return f"UTC{offset_hours:+d}"


@dataclass
class _CheckinResult:
    """Outcome of one /diemdanh or button click. Shared between the
    slash command and the persistent button so both surfaces produce
    the same user-visible message + side effects."""

    already_checked: bool
    base: int = 0
    bonus: int = 0
    total: int = 0
    streak: int = 0
    # Set when already_checked=True so we can echo the previous amount.
    prev_amount: int = 0
    prev_streak: int = 0
    # Local "today" string in the guild's configured tz — callers can
    # pass this to side-effect helpers instead of recomputing.
    today: str = ""


class CheckinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"diemdanh:click:(?P<guild_id>\d+)",
):
    """Persistent 'Check in now' button on the per-guild check-in card.

    The custom_id carries the guild_id so the same dynamic item class
    handles every server's cards. Survives bot restarts via
    bot.add_dynamic_items registration.
    """

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"diemdanh:click:{guild_id}",
                label="Check in now",
                emoji="📍",
                style=discord.ButtonStyle.success,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "CheckinButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Economy" = interaction.client.get_cog("Economy")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_checkin_button(interaction)


class HistoryButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"diemdanh:history:(?P<guild_id>\d+)",
):
    """Persistent 'History' button on the per-guild check-in card.

    On click, opens an ephemeral history dialog with a month/week
    filter dropdown and a Close button — only the clicker sees it.
    """

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"diemdanh:history:{guild_id}",
                label="History",
                emoji="📋",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "HistoryButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Economy" = interaction.client.get_cog("Economy")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_history_button(interaction)


class CheckinReminderToggleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"diemdanh:remindtoggle:(?P<guild_id>\d+)",
):
    """Persistent per-guild toggle for personal check-in DM reminders.

    Clicking flips the user's CheckinReminderPref.dm_enabled row and
    replies with an ephemeral confirmation. The button label is static
    (the current state lives inside the user-specific pref, not on the
    shared card), so viewers only see 'DM Reminder' — the actual on/off
    state is echoed in the ack after the click.
    """

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"diemdanh:remindtoggle:{guild_id}",
                label="DM Reminder",
                emoji="🔔",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "CheckinReminderToggleButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Economy" = interaction.client.get_cog("Economy")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_reminder_toggle_button(interaction, self.guild_id)


class EconLangButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"econ:lang:(?P<guild_id>\d+):(?P<ctx>[a-z]+)",
):
    """Persistent language-toggle button for economy embeds.

    ctx encodes which embed to refresh after toggling:
      card    — check-in button card
      phuho   — Rich List board
      board   — check-in streak board
      wallet  — wallet ephemeral (edit in place)
      lb      — leaderboard ephemeral (edit in place)
    """

    def __init__(self, guild_id: int, ctx: str, lang: str) -> None:
        self.guild_id = guild_id
        self.ctx = ctx
        label = _et(lang, "lang_button")
        super().__init__(
            discord.ui.Button(
                custom_id=f"econ:lang:{guild_id}:{ctx}",
                label=label,
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "EconLangButton":
        return cls(int(match["guild_id"]), match["ctx"], "ru")

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Economy" = interaction.client.get_cog("Economy")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_econ_lang_toggle(interaction, self.guild_id, self.ctx)


# Filter options for the history dialog. Stable values used to avoid
# rebuilding the Select dropdown when the user changes filter.
_HISTORY_FILTERS: list[tuple[str, str]] = [
    ("7d", "Last 7 days"),
    ("30d", "Last 30 days"),
    ("90d", "Last 90 days"),
    ("this_month", "This month"),
    ("last_month", "Last month"),
    ("all", "All time"),
]


def _compute_history_range(filter_value: str) -> tuple[datetime, datetime | None, str]:
    """Return (since, until, label) for one of the filter values.

    until is non-None only for fixed-window filters like 'last_month'.
    """
    now = datetime.now(timezone.utc)
    if filter_value == "7d":
        return now - timedelta(days=7), None, "Last 7 days"
    if filter_value == "30d":
        return now - timedelta(days=30), None, "Last 30 days"
    if filter_value == "90d":
        return now - timedelta(days=90), None, "Last 90 days"
    if filter_value == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, None, "This month"
    if filter_value == "last_month":
        first_of_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_prev = first_of_this - timedelta(seconds=1)
        start = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, first_of_this, "Last month"
    # all
    return datetime(1970, 1, 1, tzinfo=timezone.utc), None, "All time"


class _HistoryFilterSelect(discord.ui.Select):
    """Dropdown that re-renders the history embed when the user picks
    a new range. Lives inside _HistoryView so it can call back into the
    cog via the view's stored reference."""

    def __init__(self, current: str) -> None:
        options = [
            discord.SelectOption(
                label=label, value=value, default=(value == current),
            )
            for value, label in _HISTORY_FILTERS
        ]
        super().__init__(
            placeholder="Filter by…",
            options=options,
            min_values=1, max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_HistoryView" = self.view  # type: ignore[assignment]
        await view.on_filter_change(interaction, self.values[0])


class _HistoryView(discord.ui.View):
    """Ephemeral dialog view: filter Select + Close button. timeout=300
    so the dialog disappears after 5 minutes of inactivity."""

    def __init__(
        self, cog: "Economy", user_id: int, guild_id: int,
        current_filter: str = "30d",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id
        self.current_filter = current_filter
        self._populate()

    def _populate(self) -> None:
        self.clear_items()
        self.add_item(_HistoryFilterSelect(self.current_filter))
        close_btn = discord.ui.Button(
            label="Close", emoji="✖️",
            style=discord.ButtonStyle.danger, row=1,
        )
        close_btn.callback = self._on_close  # type: ignore[method-assign]
        self.add_item(close_btn)

    async def on_filter_change(
        self, interaction: discord.Interaction, new_filter: str,
    ) -> None:
        self.current_filter = new_filter
        # Rebuild the Select with the new default highlighted.
        self._populate()
        embed = await self.cog._build_history_embed(
            self.guild_id, self.user_id, new_filter,
        )
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def _on_close(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(
                content="_(Check-in history closed.)_",
                embed=None, view=None,
            )
        except (discord.NotFound, discord.HTTPException):
            pass


def _resolve_member_display(
    bot: "AkimBot", guild: discord.Guild | None, user_id: int,
) -> str:
    """Render a user id as either a clickable @mention (when the user
    is still in the guild) or a plain-text fallback (when they left).

    Discord's `<@id>` syntax renders nicely for members but becomes a
    broken-looking raw link for non-members — clicking it shows "You
    don't have access to this link". This helper avoids that by:
      1. trying guild.get_member(uid) → use real mention
      2. else bot.get_user(uid) (cross-guild cache) → bold name + tag
      3. else show "_User #<id> (left server)_"
    """
    if guild is not None:
        m = guild.get_member(user_id)
        if m is not None:
            return m.mention
    u = bot.get_user(user_id)
    if u is not None:
        name = u.global_name or u.name
        return f"**{name}** _(left server)_"
    return f"_User #{user_id} (left server)_"


def _build_checkin_card_embed(
    cfg: GuildEconomyConfig,
    today: str,
    rows: list[DailyCheckin],
    member_lookup: dict[int, str],
) -> discord.Embed:
    """The body of the persistent card. Renders today's check-ins as a
    bulleted list (mention + amount + streak)."""
    lang = getattr(cfg, "language", "ru") or "ru"
    coin_icon = cfg.coin_icon or "🪙"
    coin_name = cfg.coin_name
    tz_label = _format_tz_offset(cfg.checkin_tz_offset)
    embed = discord.Embed(
        title=_et(lang, "checkin_title"),
        description=(
            _et(lang, "checkin_today").format(today=today, tz=tz_label) + "\n\n"
            + _et(lang, "checkin_reward").format(
                min=_CHECKIN_MIN, max=_CHECKIN_MAX, coin=coin_name
            ) + "\n"
            + _et(lang, "checkin_streak_bonus").format(
                n=_CHECKIN_STREAK_LENGTH, bonus=_CHECKIN_STREAK_BONUS, coin=coin_name
            ) + "\n\n"
            + _et(lang, "checkin_cta") + "\n"
            + _et(lang, "checkin_reminder_hint")
        ),
        color=discord.Color.from_rgb(241, 196, 15),
        timestamp=datetime.now(timezone.utc),
    )

    if not rows:
        embed.add_field(
            name=_et(lang, "checkin_field_title").format(count=0),
            value=_et(lang, "checkin_nobody"),
            inline=False,
        )
    else:
        lines: list[str] = []
        for row in rows[:_CARD_MAX_LIST]:
            name = member_lookup.get(row.user_id, f"_User #{row.user_id}_")
            bonus_tag = (
                f" 🔥+{_CHECKIN_STREAK_BONUS}"
                if row.current_streak > 0
                and row.current_streak % _CHECKIN_STREAK_LENGTH == 0
                else ""
            )
            lines.append(
                f"• {name} — **{row.last_amount:,}** {coin_icon} "
                f"(streak {row.current_streak}){bonus_tag}"
            )
        if len(rows) > _CARD_MAX_LIST:
            lines.append(f"_(+{len(rows) - _CARD_MAX_LIST} more)_")
        embed.add_field(
            name=_et(lang, "checkin_field_title").format(count=len(rows)),
            value="\n".join(lines)[:1024],
            inline=False,
        )

    embed.set_footer(
        text=_et(lang, "checkin_footer").format(tz=tz_label)
    )
    return embed


def _build_card_view(guild_id: int, lang: str = "ru") -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(CheckinButton(guild_id))
    v.add_item(HistoryButton(guild_id))
    v.add_item(CheckinReminderToggleButton(guild_id))
    v.add_item(EconLangButton(guild_id, "card", lang))
    return v

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.economy")


class Economy(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        self.wallet = WalletService(bot)
        self.rewards = RewardEngine(bot)
        # guild_id -> pending refresh task (debounced)
        self._phuho_tasks: dict[int, asyncio.Task] = {}
        # guild_id -> pending checkin board + card refresh (debounced).
        # A burst of check-ins (30 users in a minute) would otherwise
        # trigger 4 Discord API calls per user; coalescing to one edit
        # per guild per ~1s keeps us under Cloudflare's per-IP cap.
        self._checkin_refresh_tasks: dict[int, asyncio.Task] = {}
        # Single hourly task — sweeps every guild, refreshes its
        # check-in card and DMs reminders when the local date (per the
        # guild's checkin_tz_offset) rolls over OR when the configured
        # UTC reminder hour hits. Replaces the old fixed-09:00-UTC
        # reminder loop. Re-entry guarded in on_ready.
        self._midnight_task: asyncio.Task | None = None
        # guild_id -> last YYYY-MM-DD (local tz) the card was rendered for.
        # Used by the hourly loop to detect a local-midnight crossing.
        self._last_card_date: dict[int, str] = {}
        # guild_id -> last YYYY-MM-DD (LOCAL date per checkin_tz_offset)
        # the reminder DM batch was sent. Used to dedup the hour-based
        # reminder so a single configured hour fires exactly once per
        # local calendar day, even when that local hour falls on a
        # different UTC date.
        self._last_dm_date: dict[int, str] = {}

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _cfg(self, guild_id: int) -> GuildEconomyConfig:
        return await self.rewards.get_or_create_config(guild_id)

    async def _require_owner(
        self, interaction: discord.Interaction
    ) -> bool:
        """Owner-only gate for every /economy admin command. Caller MUST
        have already deferred the interaction so we have a followup
        webhook for the rejection message. Manage Server + custom
        manager roles do NOT pass — economy admin is owner-exclusive
        so coin balances, reward configs, and leaderboard placement
        can't be influenced by non-owners the server lead appointed
        for unrelated tasks."""
        if interaction.guild_id is None or not isinstance(
            interaction.user, discord.Member
        ):
            return False
        if self.bot.is_owner(interaction.user.id, interaction.user.guild.owner_id):
            return True
        await interaction.followup.send(
            "❌ Only the server owner can use economy admin commands.",
            ephemeral=True,
        )
        return False

    # =========================================================================
    # /wallet, /leaderboard, /history  (user-facing)
    # =========================================================================

    @app_commands.command(
        name="wallet",
        description="View your or someone else's coin balance",
    )
    @app_commands.guild_only()
    @app_commands.describe(user="Who to view (default: you)")
    async def wallet_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        target = user or interaction.user
        cfg = await self._cfg(interaction.guild_id)
        lang = getattr(cfg, "language", "ru") or "ru"
        wallet = await self.wallet.get_or_create_wallet(
            interaction.guild_id, target.id
        )
        embed = discord.Embed(
            title=_et(lang, "wallet_title").format(name=target.display_name),
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(
            name=_et(lang, "wallet_balance"),
            value=cfg.coin_label(wallet.balance),
            inline=False,
        )
        embed.add_field(
            name=_et(lang, "wallet_earned"),
            value=cfg.coin_label(wallet.total_earned),
            inline=True,
        )
        embed.add_field(
            name=_et(lang, "wallet_spent"),
            value=cfg.coin_label(wallet.total_spent),
            inline=True,
        )
        view = discord.ui.View(timeout=60)
        view.add_item(EconLangButton(interaction.guild_id, "wallet", lang))
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="leaderboard",
        description="Top coin balances in the server",
    )
    @app_commands.guild_only()
    @app_commands.describe(count="Number of users to show (1-25, default 10)")
    async def leaderboard_cmd(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        assert interaction.guild_id is not None
        cfg = await self._cfg(interaction.guild_id)
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(UserWallet)
                .where(UserWallet.guild_id == interaction.guild_id)
                .order_by(desc(UserWallet.balance))
                .limit(count)
            )
            wallets = list(rows.scalars().all())
        lang = getattr(cfg, "language", "ru") or "ru"
        if not wallets:
            await interaction.response.send_message(
                _et(lang, "lb_empty"), ephemeral=True
            )
            return
        lines: list[str] = []
        for rank, w in enumerate(wallets, start=1):
            medal = (
                "🥇" if rank == 1
                else "🥈" if rank == 2
                else "🥉" if rank == 3
                else f"`#{rank}`"
            )
            lines.append(f"{medal} <@{w.user_id}> — **{cfg.coin_label(w.balance)}**")
        embed = discord.Embed(
            title=_et(lang, "lb_title").format(coin=cfg.coin_name),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        view = discord.ui.View(timeout=60)
        view.add_item(EconLangButton(interaction.guild_id, "lb", lang))
        await interaction.response.send_message(
            embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="history",
        description="Recent coin transaction history",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="Who to view (default: you)",
        count="Number of transactions to show (1-25, default 10)",
    )
    async def history_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        count: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        assert interaction.guild_id is not None
        target = user or interaction.user
        cfg = await self._cfg(interaction.guild_id)
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(CoinTransaction)
                .where(
                    CoinTransaction.guild_id == interaction.guild_id,
                    CoinTransaction.user_id == target.id,
                )
                .order_by(desc(CoinTransaction.created_at))
                .limit(count)
            )
            txs = list(rows.scalars().all())
        lang = getattr(cfg, "language", "ru") or "ru"
        if not txs:
            await interaction.response.send_message(
                _et(lang, "wallet_no_txs").format(mention=target.mention),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        lines: list[str] = []
        for t in txs:
            sign = "+" if t.delta >= 0 else "−"
            amount = abs(t.delta)
            ts = t.created_at.astimezone(timezone.utc).strftime("%d/%m %H:%M")
            desc_part = f" · _{t.description}_" if t.description else ""
            lines.append(
                f"`{ts}` {sign}**{amount:,}** _{t.reason}_ "
                f"(→ {t.balance_after:,}){desc_part}"
            )
        embed = discord.Embed(
            title=_et(lang, "hist_title").format(name=target.display_name),
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=_et(lang, "hist_footer").format(coin=cfg.coin_name))
        await interaction.response.send_message(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # =========================================================================
    # /economy group (admin)
    # =========================================================================

    economy = app_commands.Group(
        name="economy",
        description="Manage the coin economy system",
        # Hidden from regular users — Discord only shows /economy
        # to members with Administrator. Owner has Admin implicitly
        # so this is the strictest "owner-only" gate the slash-command
        # UI natively supports; the runtime _require_owner check still
        # rejects any non-owner admins who DO see and try to use it.
        # Owner can tighten further via Server Settings → Integrations.
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ---------- /economy setup ----------

    @economy.command(name="setup", description="(Admin) Configure the economy for this server")
    @app_commands.describe(
        coin_name="Coin unit name (e.g. Gold, Coin, Diamond)",
        coin_icon="Emoji (🪙 or <:name:id>)",
        join_reward="(LEGACY — no longer auto-fires on join. Use approval_reward instead.)",
        daily_text_reward="Coin/day/channel earned by chatting",
        daily_voice_reward="Coin/day/channel earned in voice (>= voice_min_duration)",
        boost_reward="Coin granted when a member Boosts the server (once per user)",
        approval_reward="Coin granted when a registration is approved (once per user)",
        min_message_length="Minimum message length that counts for a reward",
        voice_min_duration_seconds="Minimum time in voice to earn a reward (seconds)",
        checkin_reminder_enabled="Enable DM reminders for /diemdanh at each new day (per checkin_tz_offset)",
        checkin_tz_offset="Timezone for check-in day (e.g. 7 = VN, 0 = UTC, -5 = EST). Range -12..14",
        checkin_reminder_hour="Hour of the day for the reminder DM (0-23, in checkin_tz_offset). Blank = fire at day rollover",
    )
    async def economy_setup(
        self,
        interaction: discord.Interaction,
        coin_name: app_commands.Range[str, 1, 32] | None = None,
        coin_icon: app_commands.Range[str, 1, 64] | None = None,
        join_reward: app_commands.Range[int, 0, 1_000_000] | None = None,
        daily_text_reward: app_commands.Range[int, 0, 1_000_000] | None = None,
        daily_voice_reward: app_commands.Range[int, 0, 1_000_000] | None = None,
        boost_reward: app_commands.Range[int, 0, 10_000_000] | None = None,
        approval_reward: app_commands.Range[int, 0, 10_000_000] | None = None,
        min_message_length: app_commands.Range[int, 0, 200] | None = None,
        voice_min_duration_seconds: app_commands.Range[int, 0, 86400] | None = None,
        checkin_reminder_enabled: bool | None = None,
        checkin_tz_offset: app_commands.Range[int, -12, 14] | None = None,
        checkin_reminder_hour: app_commands.Range[int, 0, 23] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildEconomyConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildEconomyConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            if coin_name is not None:
                cfg.coin_name = coin_name
            if coin_icon is not None:
                cfg.coin_icon = coin_icon
            if join_reward is not None:
                cfg.join_reward_amount = int(join_reward)
                cfg.join_reward_enabled = int(join_reward) > 0
            if daily_text_reward is not None:
                cfg.daily_text_reward = int(daily_text_reward)
                cfg.daily_text_enabled = int(daily_text_reward) > 0
            if daily_voice_reward is not None:
                cfg.daily_voice_reward = int(daily_voice_reward)
                cfg.daily_voice_enabled = int(daily_voice_reward) > 0
            if boost_reward is not None:
                cfg.boost_reward_amount = int(boost_reward)
                cfg.boost_reward_enabled = int(boost_reward) > 0
            if approval_reward is not None:
                cfg.approval_reward_amount = int(approval_reward)
                cfg.approval_reward_enabled = int(approval_reward) > 0
            if min_message_length is not None:
                cfg.min_message_length = int(min_message_length)
            if voice_min_duration_seconds is not None:
                cfg.voice_min_duration_seconds = int(voice_min_duration_seconds)
            if checkin_reminder_enabled is not None:
                cfg.checkin_reminder_enabled = bool(checkin_reminder_enabled)
            if checkin_tz_offset is not None:
                cfg.checkin_tz_offset = int(checkin_tz_offset)
            if checkin_reminder_hour is not None:
                # Stored in column `checkin_reminder_hour_utc` per
                # additive-schema policy; semantic is now local hour.
                cfg.checkin_reminder_hour_utc = int(checkin_reminder_hour)
            # NOTE: manager_role_ids on the schema is intentionally
            # untouched. Economy admin is owner-only now (see
            # _require_owner) — that column is inert but kept per
            # additive-only schema policy.
            await s.commit()
            cfg = await s.get(GuildEconomyConfig, interaction.guild_id)

        assert cfg is not None
        embed = discord.Embed(
            title="💰 Economy configuration", color=discord.Color.gold()
        )
        embed.add_field(
            name="Coin",
            value=f"{cfg.coin_icon or ''} **{cfg.coin_name}**",
            inline=False,
        )
        embed.add_field(name="Join reward", value=str(cfg.join_reward_amount), inline=True)
        embed.add_field(
            name="Daily text",
            value=f"{cfg.daily_text_reward}/ch/day",
            inline=True,
        )
        embed.add_field(
            name="Daily voice",
            value=f"{cfg.daily_voice_reward}/ch/day",
            inline=True,
        )
        embed.add_field(
            name="Boost reward",
            value=(
                f"{cfg.boost_reward_amount:,} (once per user)"
                if cfg.boost_reward_amount > 0 else "—"
            ),
            inline=True,
        )
        embed.add_field(
            name="Approval reward",
            value=(
                f"{cfg.approval_reward_amount:,} (once per user)"
                if cfg.approval_reward_amount > 0 else "—"
            ),
            inline=True,
        )
        embed.add_field(
            name="Min msg length", value=str(cfg.min_message_length), inline=True
        )
        embed.add_field(
            name="Voice min duration",
            value=f"{cfg.voice_min_duration_seconds}s",
            inline=True,
        )
        embed.add_field(
            name="Admin access",
            value="🔒 **Server owner only**",
            inline=False,
        )
        chk_rem = "✅ on" if cfg.checkin_reminder_enabled else "❌ off"
        chk_tz = _format_tz_offset(cfg.checkin_tz_offset)
        if cfg.checkin_reminder_hour_utc is not None:
            local_hour = cfg.checkin_reminder_hour_utc
            target_utc = (local_hour - (cfg.checkin_tz_offset or 0)) % 24
            chk_hour = (
                f"`{local_hour:02d}:00` ({chk_tz}) "
                f"≈ `{target_utc:02d}:00` UTC"
            )
        else:
            chk_hour = f"_local midnight ({chk_tz})_"
        embed.add_field(name="Check-in DM reminder", value=chk_rem, inline=True)
        embed.add_field(name="Check-in timezone", value=chk_tz, inline=True)
        embed.add_field(name="Reminder hour", value=chk_hour, inline=True)
        await interaction.followup.send(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /economy ignore (group) ----------

    ignore = app_commands.Group(
        name="ignore",
        description="Channel exclude khỏi daily reward",
        parent=economy,
    )

    @ignore.command(name="add", description="(Admin) Add a channel to the ignore list")
    @app_commands.describe(channel="Channel to ignore (text or voice)")
    async def ignore_add(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            s.add(IgnoredRewardChannel(
                guild_id=interaction.guild_id,
                channel_id=channel.id,
                added_by=interaction.user.id,
            ))
            try:
                await s.commit()
                msg = f"🚫 {channel.mention} is now ignored for daily rewards."
            except Exception:
                await s.rollback()
                msg = f"{channel.mention} is already in the ignore list."
        await interaction.followup.send(msg, ephemeral=True)

    @ignore.command(name="remove", description="(Admin) Remove a channel from the ignore list")
    @app_commands.describe(channel="Channel to remove from the ignore list")
    async def ignore_remove(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            result = await s.execute(
                delete(IgnoredRewardChannel).where(
                    IgnoredRewardChannel.guild_id == interaction.guild_id,
                    IgnoredRewardChannel.channel_id == channel.id,
                )
            )
            await s.commit()
        if result.rowcount:
            await interaction.followup.send(
                f"✅ Removed {channel.mention} from the ignore list.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"{channel.mention} is not in the ignore list.", ephemeral=True
            )

    @ignore.command(name="list", description="(Admin) List channels being ignored")
    async def ignore_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(IgnoredRewardChannel).where(
                    IgnoredRewardChannel.guild_id == interaction.guild_id
                )
            )
            chans = list(rows.scalars().all())
        if not chans:
            await interaction.followup.send(
                "No channels are ignored.", ephemeral=True
            )
            return
        lines = [f"• <#{c.channel_id}>" for c in chans]
        embed = discord.Embed(
            title=f"🚫 Ignored channels ({len(chans)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- /economy reward (minigame configs) ----------

    reward = app_commands.Group(
        name="reward",
        description="Configure per-minigame rewards",
        parent=economy,
    )

    @reward.command(
        name="setup",
        description="(Admin) Set reward amounts for one game type",
    )
    @app_commands.describe(
        game_type="Game type",
        top1_reward="Coin for top 1",
        top2_reward="Coin for top 2",
        top3_reward="Coin for top 3",
        participate_reward="Coin for participants outside the top 3",
        entry_fee="Coin cost to join (0 = free)",
        enabled="Enable/disable reward for this game",
    )
    async def reward_setup(
        self,
        interaction: discord.Interaction,
        game_type: Literal["quiz", "sohoc"],
        top1_reward: app_commands.Range[int, 0, 1_000_000] | None = None,
        top2_reward: app_commands.Range[int, 0, 1_000_000] | None = None,
        top3_reward: app_commands.Range[int, 0, 1_000_000] | None = None,
        participate_reward: app_commands.Range[int, 0, 1_000_000] | None = None,
        entry_fee: app_commands.Range[int, 0, 1_000_000] | None = None,
        enabled: bool | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(MiniGameRewardConfig).where(
                    MiniGameRewardConfig.guild_id == interaction.guild_id,
                    MiniGameRewardConfig.game_type == game_type,
                )
            )
            if row is None:
                row = MiniGameRewardConfig(
                    guild_id=interaction.guild_id,
                    game_type=game_type,
                )
                s.add(row)
            if top1_reward is not None:
                row.top1_reward = int(top1_reward)
            if top2_reward is not None:
                row.top2_reward = int(top2_reward)
            if top3_reward is not None:
                row.top3_reward = int(top3_reward)
            if participate_reward is not None:
                row.participate_reward = int(participate_reward)
            if entry_fee is not None:
                row.entry_fee = int(entry_fee)
            if enabled is not None:
                row.enabled = bool(enabled)
            await s.commit()
            await s.refresh(row)
        cfg = await self._cfg(interaction.guild_id)
        embed = discord.Embed(
            title=f"🎮 Reward cho {game_type}",
            color=discord.Color.gold(),
        )
        embed.add_field(name="🥇 Top 1", value=cfg.coin_label(row.top1_reward), inline=True)
        embed.add_field(name="🥈 Top 2", value=cfg.coin_label(row.top2_reward), inline=True)
        embed.add_field(name="🥉 Top 3", value=cfg.coin_label(row.top3_reward), inline=True)
        embed.add_field(
            name="Participate", value=cfg.coin_label(row.participate_reward), inline=True
        )
        embed.add_field(
            name="Entry fee", value=cfg.coin_label(row.entry_fee), inline=True
        )
        embed.add_field(
            name="Enabled", value="✅" if row.enabled else "❌", inline=True
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- /economy sync-members ----------

    @economy.command(
        name="sync-members",
        description="(Admin) Create wallets for all existing members (no join reward)",
    )
    async def economy_sync_members(
        self, interaction: discord.Interaction
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None and interaction.guild is not None
        created = 0
        skipped = 0
        async with self.bot.db.session() as s:
            existing_rows = await s.execute(
                select(UserWallet.user_id).where(
                    UserWallet.guild_id == interaction.guild_id
                )
            )
            existing = {r[0] for r in existing_rows.all()}
        for member in interaction.guild.members:
            if member.bot:
                continue
            if member.id in existing:
                skipped += 1
                continue
            await self.wallet.get_or_create_wallet(
                interaction.guild_id, member.id
            )
            # Mark with a zero-amount sync_init row so /history shows the
            # wallet was provisioned (no balance change needed since adjust
            # skips delta == 0 — write the tx manually here).
            async with self.bot.db.session() as s:
                s.add(CoinTransaction(
                    guild_id=interaction.guild_id,
                    user_id=member.id,
                    delta=0,
                    reason=REASON_SYNC_INIT,
                    description="Wallet provisioned by /economy sync-members",
                    actor_id=interaction.user.id,
                    balance_after=0,
                ))
                await s.commit()
            created += 1
        await interaction.followup.send(
            f"✅ Synced: created {created} new wallets, skipped {skipped} (already existed) "
            f"— bots were ignored.",
            ephemeral=True,
        )

    # ---------- /economy add | remove | set ----------

    @economy.command(name="add", description="(Admin) Add coins to a user")
    @app_commands.describe(
        user="User receiving coins",
        amount="Coin amount (> 0)",
        reason="Reason (shown in /history)",
    )
    async def economy_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
        reason: app_commands.Range[str, 1, 64] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        try:
            wallet = await self.wallet.adjust(
                interaction.guild_id,
                user.id,
                int(amount),
                REASON_ADMIN_ADD,
                description=reason or f"Admin grant by {interaction.user}",
                actor_id=interaction.user.id,
            )
        except InsufficientBalanceError as e:
            await interaction.followup.send(
                f"❌ {e}", ephemeral=True
            )
            return
        cfg = await self._cfg(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Added **{cfg.coin_label(int(amount))}** to {user.mention}. "
            f"New balance: **{cfg.coin_label(wallet.balance)}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @economy.command(name="remove", description="(Admin) Deduct coins from a user")
    @app_commands.describe(
        user="User losing coins",
        amount="Coin amount (> 0)",
        reason="Reason (shown in /history)",
    )
    async def economy_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
        reason: app_commands.Range[str, 1, 64] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        try:
            wallet = await self.wallet.adjust(
                interaction.guild_id,
                user.id,
                -int(amount),
                REASON_ADMIN_REMOVE,
                description=reason or f"Admin debit by {interaction.user}",
                actor_id=interaction.user.id,
            )
        except InsufficientBalanceError as e:
            await interaction.followup.send(
                f"❌ {e}", ephemeral=True
            )
            return
        cfg = await self._cfg(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Deducted **{cfg.coin_label(int(amount))}** from {user.mention}. "
            f"New balance: **{cfg.coin_label(wallet.balance)}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @economy.command(name="set", description="(Admin) Set a user's balance to a specific value")
    @app_commands.describe(
        user="User whose balance to set",
        amount="New balance (>= 0)",
        reason="Reason (shown in /history)",
    )
    async def economy_set(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 0, 1_000_000_000],
        reason: app_commands.Range[str, 1, 64] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        try:
            wallet = await self.wallet.set_balance(
                interaction.guild_id,
                user.id,
                int(amount),
                actor_id=interaction.user.id,
                description=reason,
            )
        except InsufficientBalanceError as e:
            await interaction.followup.send(
                f"❌ {e}", ephemeral=True
            )
            return
        cfg = await self._cfg(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Set {user.mention}'s balance to **{cfg.coin_label(wallet.balance)}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # =========================================================================
    # Listeners
    # =========================================================================

    # on_member_join was removed deliberately. The previous build
    # granted join_reward here AND approval_reward in on_player_approved,
    # which double-rewarded every new member that went through the
    # registration flow. This bot's canonical welcome bonus is the approval reward;
    # the join_reward column + grant_join_reward method are kept for
    # additive-schema continuity but no longer fire automatically.

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member,
    ) -> None:
        """Fires for every member change. We only react to the
        boost-start transition: premium_since None → datetime. A user
        who cancels and re-boosts later still won't be paid a second
        time — grant_boost_reward is dedup'd on CoinTransaction.reason."""
        if after.bot:
            return
        if before.premium_since is not None or after.premium_since is None:
            return
        try:
            amount = await self.rewards.grant_boost_reward(
                after.guild.id, after.id,
            )
        except Exception:
            log.exception(
                "economy: boost reward failed for %s in guild %s",
                after.id, after.guild.id,
            )
            return
        if amount > 0:
            cfg = await self._cfg(after.guild.id)
            log.info(
                "economy: granted %s boost reward to %s in guild %s",
                cfg.coin_label(amount), after.id, after.guild.id,
            )

    @commands.Cog.listener()
    async def on_player_approved(
        self, guild_id: int, user_id: int,
    ) -> None:
        """Dispatched by views/approval.py after a /baodanh card is
        approved. RewardEngine.grant_approval_reward dedups on
        CoinTransaction.reason='approval' so it's safe to fire on
        re-approval cycles too — no double-pay."""
        try:
            amount = await self.rewards.grant_approval_reward(
                guild_id, user_id,
            )
        except Exception:
            log.exception(
                "economy: approval reward failed for %s in guild %s",
                user_id, guild_id,
            )
            return
        if amount > 0:
            cfg = await self._cfg(guild_id)
            log.info(
                "economy: granted %s approval reward to %s in guild %s",
                cfg.coin_label(amount), user_id, guild_id,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.author, discord.Member):
            return
        cfg = await self._cfg(message.guild.id)
        # Length check only runs when we actually have content access. The
        # message_content privileged intent is OFF by default, so
        # message.content is "" for non-mention messages — in that case
        # we skip the check and grant the reward (admins who want strict
        # length enforcement need to enable the intent in the developer
        # portal AND in bot.py, then bump min_message_length via /economy
        # setup).
        if cfg.min_message_length > 0:
            content = (message.content or "").strip()
            if content and len(content) < cfg.min_message_length:
                return
        try:
            await self.rewards.try_claim_daily_text(
                message.guild.id, message.author.id, message.channel.id
            )
        except Exception:
            log.exception("economy: daily text reward failed")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        guild_id = member.guild.id

        # Close any open voice session if user left / switched channels
        if before.channel is not None and (
            after.channel is None or after.channel.id != before.channel.id
        ):
            async with self.bot.db.session() as s:
                row = await s.scalar(
                    select(VoiceSession)
                    .where(
                        VoiceSession.guild_id == guild_id,
                        VoiceSession.user_id == member.id,
                        VoiceSession.left_at.is_(None),
                    )
                    .order_by(desc(VoiceSession.joined_at))
                    .limit(1)
                )
                if row is not None:
                    now = datetime.now(timezone.utc)
                    duration = int((now - row.joined_at).total_seconds())
                    row.left_at = now
                    row.duration_seconds = max(0, duration)
                    await s.commit()
                    try:
                        await self.rewards.try_claim_daily_voice(
                            guild_id,
                            member.id,
                            row.channel_id,
                            row.duration_seconds,
                        )
                    except Exception:
                        log.exception("economy: daily voice reward failed")

        # Open a new voice session if user joined / switched into a channel
        if after.channel is not None and (
            before.channel is None or after.channel.id != before.channel.id
        ):
            async with self.bot.db.session() as s:
                s.add(VoiceSession(
                    guild_id=guild_id,
                    user_id=member.id,
                    channel_id=after.channel.id,
                ))
                await s.commit()

    # ---------- Rich List auto-update listener ----------

    @commands.Cog.listener()
    async def on_wallet_changed(self, guild_id: int) -> None:
        """Dispatched by WalletService.adjust() whenever a wallet moves.

        We coalesce burst updates (a minigame paying 30 users) into a
        single edit per guild per debounce window — otherwise we'd
        rate-limit ourselves on the Discord edit endpoint.
        """
        if guild_id in self._phuho_tasks:
            return  # a refresh is already scheduled for this guild
        self._phuho_tasks[guild_id] = asyncio.create_task(
            self._debounced_phuho_refresh(guild_id)
        )

    async def _debounced_phuho_refresh(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(_PHUHO_DEBOUNCE_SECONDS)
            await self._refresh_phuho_board(guild_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("phuho: debounced refresh failed for %s", guild_id)
        finally:
            self._phuho_tasks.pop(guild_id, None)

    async def _build_phuho_embed(
        self,
        guild_id: int,
        display_count: int,
    ) -> discord.Embed:
        cfg = await self._cfg(guild_id)
        lang = getattr(cfg, "language", "ru") or "ru"
        coin_icon = cfg.coin_icon or "🪙"
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(UserWallet)
                .where(
                    UserWallet.guild_id == guild_id,
                    UserWallet.balance > 0,
                )
                .order_by(desc(UserWallet.balance), UserWallet.user_id)
                .limit(display_count)
            )
            wallets = list(rows.scalars().all())

        guild = self.bot.get_guild(guild_id)
        guild_name = guild.name if guild else "Server"

        embed = discord.Embed(
            title=_et(lang, "phuho_title"),
            color=discord.Color.gold(),
        )
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        if not wallets:
            embed.description = _et(lang, "phuho_empty")
        else:
            lines: list[str] = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
            for rank, w in enumerate(wallets, start=1):
                if rank == 1:
                    medal = "🥇"
                elif rank == 2:
                    medal = "🥈"
                elif rank == 3:
                    medal = "🥉"
                else:
                    medal = f"`#{rank:>2}`"
                name = _resolve_member_display(
                    self.bot, guild, w.user_id,
                )
                lines.append(
                    f"{medal}  {name}  •  `{w.balance:,}` {coin_icon}"
                )
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            embed.description = "\n".join(lines)

        embed.set_footer(
            text=_et(lang, "phuho_footer").format(guild=guild_name, count=display_count)
        )
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    def _build_phuho_view(self, guild_id: int, lang: str) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        v.add_item(EconLangButton(guild_id, "phuho", lang))
        return v

    async def _refresh_phuho_board(self, guild_id: int) -> None:
        """Edit the existing Rich List board message in place. If it's gone,
        clear the row so the admin knows to re-run /economy phuho post."""
        async with self.bot.db.session() as s:
            board = await s.get(PhuHoLeaderboard, guild_id)
        if board is None:
            return  # no board configured for this guild
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(board.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning(
                "phuho: channel %s not usable for guild %s",
                board.channel_id, guild_id,
            )
            return
        cfg = await self._cfg(guild_id)
        lang = getattr(cfg, "language", "ru") or "ru"
        embed = await self._build_phuho_embed(guild_id, board.display_count)
        view = self._build_phuho_view(guild_id, lang)
        # PartialMessage.edit() skips the pre-flight GET — it hits Discord
        # once instead of twice per refresh.
        if board.message_id is not None:
            try:
                await channel.get_partial_message(board.message_id).edit(
                    embed=embed, view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except discord.NotFound:
                pass  # message deleted — fall through to repost
            except discord.Forbidden:
                log.warning("phuho: forbidden to edit in guild %s", guild_id)
                return
            except discord.HTTPException as e:
                log.warning("phuho: edit failed for guild %s: %s", guild_id, e)
                return
        # Message gone — repost and update pointer.
        try:
            new_msg = await channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning("phuho: forbidden to repost in guild %s", guild_id)
            return
        async with self.bot.db.session() as s:
            row = await s.get(PhuHoLeaderboard, guild_id)
            if row is not None:
                row.message_id = new_msg.id
                await s.commit()

    # ---------- /economy phuho subgroup ----------

    phuho = app_commands.Group(
        name="phuho",
        description="Rich List leaderboard (auto-updates)",
        parent=economy,
    )

    @phuho.command(
        name="post",
        description="(Admin) Create/replace the Rich List leaderboard in a channel",
    )
    @app_commands.describe(
        channel="Channel to post the leaderboard in (any channel)",
        display_count="Users shown (1-25, default 10)",
    )
    async def phuho_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        display_count: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        # Try to delete the previous board message (if any) so we don't
        # leave stale boards floating around when admin moves the channel.
        async with self.bot.db.session() as s:
            prev = await s.get(PhuHoLeaderboard, interaction.guild_id)
        if prev is not None and prev.message_id is not None:
            prev_channel = (
                interaction.guild.get_channel(prev.channel_id)
                if interaction.guild else None
            )
            if isinstance(prev_channel, (discord.TextChannel, discord.Thread)):
                try:
                    old_msg = await prev_channel.fetch_message(prev.message_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        embed = await self._build_phuho_embed(
            interaction.guild_id, int(display_count)
        )
        cfg = await self._cfg(interaction.guild_id)
        lang = getattr(cfg, "language", "ru") or "ru"
        view = self._build_phuho_view(interaction.guild_id, lang)
        try:
            msg = await channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {channel.mention}.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            board = await s.get(PhuHoLeaderboard, interaction.guild_id)
            if board is None:
                board = PhuHoLeaderboard(
                    guild_id=interaction.guild_id,
                    channel_id=channel.id,
                    message_id=msg.id,
                    display_count=int(display_count),
                )
                s.add(board)
            else:
                board.channel_id = channel.id
                board.message_id = msg.id
                board.display_count = int(display_count)
            await s.commit()

        await interaction.followup.send(
            f"✅ Rich List posted in {channel.mention} "
            f"(top {display_count}). Auto-updates on wallet changes.",
            ephemeral=True,
        )

    @phuho.command(
        name="remove",
        description="(Admin) Remove the Rich List + stop auto-update",
    )
    async def phuho_remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            board = await s.get(PhuHoLeaderboard, interaction.guild_id)
            if board is None:
                await interaction.followup.send(
                    "No Rich List configured in this server.", ephemeral=True
                )
                return
            channel_id = board.channel_id
            message_id = board.message_id
            await s.delete(board)
            await s.commit()
        # Best-effort delete of the actual message
        if message_id is not None and interaction.guild is not None:
            ch = interaction.guild.get_channel(channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await ch.fetch_message(message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        await interaction.followup.send(
            "✅ Rich List removed + auto-update stopped.", ephemeral=True
        )

    @phuho.command(
        name="refresh",
        description="(Admin) Refresh the Rich List right away (skip debounce)",
    )
    async def phuho_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            board = await s.get(PhuHoLeaderboard, interaction.guild_id)
        if board is None:
            await interaction.followup.send(
                "No Rich List configured. Run `/economy phuho post` first.",
                ephemeral=True,
            )
            return
        await self._refresh_phuho_board(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Rich List refreshed in <#{board.channel_id}>.",
            ephemeral=True,
        )

    # =========================================================================
    # /diemdanh — daily check-in user command
    # =========================================================================

    @app_commands.command(
        name="diemdanh-nhac",
        description="Toggle your personal daily check-in DM reminder",
    )
    @app_commands.describe(
        bat=(
            "True = enable DM reminder, False = disable. "
            "Leave blank to view current state."
        ),
    )
    @app_commands.guild_only()
    async def diemdanh_nhac_cmd(
        self,
        interaction: discord.Interaction,
        bat: bool | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if bat is None:
            enabled = await self._get_user_reminder_pref(
                interaction.guild_id, interaction.user.id,
            )
        else:
            await self._set_user_reminder_pref(
                interaction.guild_id, interaction.user.id, bool(bat),
            )
            enabled = bool(bat)
        cfg = await self._cfg(interaction.guild_id)
        await interaction.followup.send(
            self._format_reminder_state(enabled, cfg),
            ephemeral=True,
        )

    @app_commands.command(
        name="diemdanh",
        description="Daily check-in for coins (10-100 random, +200 bonus every 7-day streak)",
    )
    @app_commands.guild_only()
    async def diemdanh_cmd(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        result = await self._perform_checkin(
            interaction.guild_id, interaction.user.id
        )
        cfg = await self._cfg(interaction.guild_id)
        text = self._format_checkin_response(result, cfg)
        # Capture the followup so we can target it specifically for
        # auto-dismiss — using delete_original_response would only
        # remove the deferred indicator, leaving this followup behind.
        ack = await interaction.followup.send(text, ephemeral=True, wait=True)
        asyncio.create_task(
            self._dismiss_message_after(ack, _CHECKIN_ACK_DISMISS_SECONDS)
        )

        if not result.already_checked:
            await self._post_checkin_side_effects(
                interaction.user, result, result.today,
                interaction.guild_id, cfg,
            )

    async def _perform_checkin(
        self, guild_id: int, user_id: int
    ) -> _CheckinResult:
        """Compute streak + commit DailyCheckin atomically + credit the
        wallet. Shared between /diemdanh and the persistent button.

        "Today" is derived from the guild's configured UTC offset
        (checkin_tz_offset), so streak/rollover follows local time.

        Returns a _CheckinResult — caller decides how to render the
        response (slash uses followup, button uses ephemeral)."""
        cfg = await self._cfg(guild_id)
        offset = cfg.checkin_tz_offset or 0
        today = _local_today_string(offset)
        yesterday = _local_yesterday_string(offset)

        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(DailyCheckin).where(
                    DailyCheckin.guild_id == guild_id,
                    DailyCheckin.user_id == user_id,
                )
            )
            if row is not None and row.last_checkin_date == today:
                return _CheckinResult(
                    already_checked=True,
                    prev_amount=row.last_amount,
                    prev_streak=row.current_streak,
                    today=today,
                )

            if row is None:
                new_streak = 1
                row = DailyCheckin(
                    guild_id=guild_id, user_id=user_id,
                    last_checkin_date=today,
                    current_streak=new_streak,
                    longest_streak=new_streak,
                    total_checkins=0,
                    last_amount=0,
                )
                s.add(row)
            elif row.last_checkin_date == yesterday:
                new_streak = row.current_streak + 1
            else:
                new_streak = 1

            row.last_checkin_date = today
            row.current_streak = new_streak
            row.longest_streak = max(row.longest_streak, new_streak)
            row.total_checkins += 1
            base = random.randint(_CHECKIN_MIN, _CHECKIN_MAX)
            bonus = (
                _CHECKIN_STREAK_BONUS
                if new_streak > 0 and new_streak % _CHECKIN_STREAK_LENGTH == 0
                else 0
            )
            total = base + bonus
            row.last_amount = total
            await s.commit()

        try:
            await self.wallet.adjust(
                guild_id, user_id, total, REASON_CHECKIN,
                description=(
                    f"Daily check-in (streak {new_streak})"
                    + (f" + 7-day bonus" if bonus > 0 else "")
                ),
                metadata={
                    "date": today, "streak": new_streak,
                    "base": base, "bonus": bonus,
                },
            )
        except Exception:
            log.exception(
                "checkin: wallet adjust failed for %s/%s", guild_id, user_id
            )

        return _CheckinResult(
            already_checked=False,
            base=base, bonus=bonus, total=total, streak=new_streak,
            today=today,
        )

    def _format_checkin_response(
        self, result: _CheckinResult, cfg: GuildEconomyConfig
    ) -> str:
        lang = getattr(cfg, "language", "ru") or "ru"
        coin_name = cfg.coin_name
        coin_icon = cfg.coin_icon or ""
        tz_label = _format_tz_offset(cfg.checkin_tz_offset)
        if result.already_checked:
            return _et(lang, "checkin_already").format(
                prev_amount=result.prev_amount,
                coin=coin_name,
                prev_streak=result.prev_streak,
                tz=tz_label,
            )
        bonus_line = (
            _et(lang, "checkin_streak_bonus_line").format(
                bonus=result.bonus, coin=coin_name, n=_CHECKIN_STREAK_LENGTH
            )
            if result.bonus > 0 else ""
        )
        return _et(lang, "checkin_success").format(
            total=result.total,
            coin_icon=coin_icon,
            coin=coin_name,
            base=result.base,
            bonus_line=bonus_line,
            streak=result.streak,
        )

    async def _post_checkin_side_effects(
        self,
        user: discord.User | discord.Member,
        result: _CheckinResult,
        today: str,
        guild_id: int,
        cfg: GuildEconomyConfig,
    ) -> None:
        """After a successful checkin: schedule a debounced refresh of
        the streak leaderboard AND the persistent card. A wave of
        clicks in the same second collapses to one edit per surface
        instead of 4 API calls per click.

        (We intentionally do NOT post a public per-event embed — the
        card itself shows who has checked in today, and the clicker
        already got an ephemeral receipt.)"""
        self._schedule_checkin_refresh(guild_id)

    def _schedule_checkin_refresh(self, guild_id: int) -> None:
        """Coalesce checkin-card + checkin-board refreshes for `guild_id`.
        Idempotent: a second call while a task is pending is a no-op
        (the pending task will pick up the latest DB state when it
        fires)."""
        existing = self._checkin_refresh_tasks.get(guild_id)
        if existing is not None and not existing.done():
            return
        self._checkin_refresh_tasks[guild_id] = asyncio.create_task(
            self._debounced_checkin_refresh(guild_id)
        )

    async def _debounced_checkin_refresh(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(_CHECKIN_REFRESH_DEBOUNCE_SECONDS)
            try:
                await self._refresh_checkin_board(guild_id)
            except Exception:
                log.exception(
                    "checkin: debounced leaderboard refresh failed for %s",
                    guild_id,
                )
            try:
                await self._refresh_checkin_card(guild_id)
            except Exception:
                log.exception(
                    "checkin: debounced card refresh failed for %s",
                    guild_id,
                )
        except asyncio.CancelledError:
            pass
        finally:
            self._checkin_refresh_tasks.pop(guild_id, None)

    async def _dismiss_message_after(
        self, msg: discord.WebhookMessage | discord.Message, delay: int,
    ) -> None:
        """Delete a specific message (typically a followup ephemeral)
        after a short delay. Safe to fire-and-forget — if the user has
        already dismissed it or the token expired, the API call just
        no-ops. Targets the message directly (not the interaction's
        original_response) so the surrounding card cannot be affected.
        """
        try:
            await asyncio.sleep(delay)
            await msg.delete()
        except (
            asyncio.CancelledError, discord.NotFound, discord.HTTPException,
        ):
            pass

    async def handle_checkin_button(
        self, interaction: discord.Interaction
    ) -> None:
        """Handler for the persistent 'Check in now' button on the card.

        The button is on a public channel message (the card) — we MUST NOT
        touch interaction.message or call delete_original_response on the
        interaction, otherwise we'd be at risk of accidentally affecting
        the card itself. We only ever delete the ephemeral followup we
        explicitly sent.
        """
        if interaction.guild_id is None:
            return
        # If Discord's per-IP quota (Cloudflare 1015) is blocking us,
        # `defer()` fails with 429 — swallow it as a one-line warning
        # instead of a full DynamicItem traceback. The user still sees
        # "didn't respond in time", but the log stays quiet enough to
        # spot real bugs.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as e:
            log.warning(
                "checkin: defer rejected (status=%s) — likely Discord "
                "per-IP rate-limit; skipping this click.",
                getattr(e, "status", "?"),
            )
            return
        result = await self._perform_checkin(
            interaction.guild_id, interaction.user.id
        )
        cfg = await self._cfg(interaction.guild_id)
        text = self._format_checkin_response(result, cfg)
        ack = await interaction.followup.send(text, ephemeral=True, wait=True)
        asyncio.create_task(
            self._dismiss_message_after(ack, _CHECKIN_ACK_DISMISS_SECONDS)
        )
        if not result.already_checked:
            await self._post_checkin_side_effects(
                interaction.user, result, result.today,
                interaction.guild_id, cfg,
            )

    async def handle_history_button(
        self, interaction: discord.Interaction
    ) -> None:
        """Handler for the persistent 'History' button — opens an ephemeral
        dialog with a filter dropdown and a Close button."""
        if interaction.guild_id is None:
            return
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as e:
            log.warning(
                "history: defer rejected (status=%s) — per-IP rate-limit.",
                getattr(e, "status", "?"),
            )
            return
        embed = await self._build_history_embed(
            interaction.guild_id, interaction.user.id, "30d",
        )
        view = _HistoryView(
            self, interaction.user.id, interaction.guild_id, "30d",
        )
        await interaction.followup.send(
            embed=embed, view=view, ephemeral=True,
        )

    async def handle_reminder_toggle_button(
        self, interaction: discord.Interaction, guild_id: int,
    ) -> None:
        """Handler for the persistent '🔔 DM Reminder' button on the checkin
        card. Toggles the user's opt-out row and ephemerally echoes the
        new state (and whether the guild-level reminder is enabled at
        all)."""
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as e:
            log.warning(
                "reminder toggle: defer rejected (status=%s) — per-IP rate-limit.",
                getattr(e, "status", "?"),
            )
            return
        new_enabled = await self._toggle_user_reminder_pref(
            guild_id, interaction.user.id,
        )
        cfg = await self._cfg(guild_id)
        await interaction.followup.send(
            self._format_reminder_state(new_enabled, cfg),
            ephemeral=True,
        )

    async def handle_econ_lang_toggle(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        ctx: str,
    ) -> None:
        """Handler for EconLangButton. Toggles guild economy language between
        'ru' and 'en', then refreshes the relevant persistent board or
        updates the ephemeral message in place."""
        is_persistent = ctx in ("card", "phuho", "board")
        try:
            await interaction.response.defer(ephemeral=not is_persistent)
        except discord.HTTPException as e:
            log.warning(
                "econ lang toggle: defer rejected (status=%s)", getattr(e, "status", "?")
            )
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildEconomyConfig, guild_id)
            if cfg is None:
                cfg = GuildEconomyConfig(guild_id=guild_id, language="en")
                s.add(cfg)
            else:
                current = getattr(cfg, "language", "ru") or "ru"
                cfg.language = "en" if current == "ru" else "ru"
            await s.commit()
            new_lang = cfg.language

        if ctx == "card":
            cfg2 = await self._cfg(guild_id)
            offset = cfg2.checkin_tz_offset or 0
            today = _local_today_string(offset)
            async with self.bot.db.session() as s:
                rows_q = await s.execute(
                    select(DailyCheckin)
                    .where(
                        DailyCheckin.guild_id == guild_id,
                        DailyCheckin.last_checkin_date == today,
                    )
                    .order_by(DailyCheckin.updated_at)
                )
                rows = list(rows_q.scalars().all())
            guild_obj = self.bot.get_guild(guild_id)
            member_lookup = {
                row.user_id: _resolve_member_display(self.bot, guild_obj, row.user_id)
                for row in rows
            }
            embed = _build_checkin_card_embed(cfg2, today, rows, member_lookup)
            view = _build_card_view(guild_id, new_lang)
            await interaction.edit_original_response(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif ctx == "phuho":
            async with self.bot.db.session() as s:
                phuho_board = await s.get(PhuHoLeaderboard, guild_id)
            display_count = phuho_board.display_count if phuho_board else 10
            embed = await self._build_phuho_embed(guild_id, display_count)
            view = self._build_phuho_view(guild_id, new_lang)
            await interaction.edit_original_response(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif ctx == "board":
            async with self.bot.db.session() as s:
                checkin_board = await s.scalar(
                    select(GameWinBoard).where(
                        GameWinBoard.guild_id == guild_id,
                        GameWinBoard.game_type == "checkin",
                    )
                )
            display_count = checkin_board.display_count if checkin_board else 10
            embed = await self._build_checkin_embed(guild_id, display_count)
            view = await self._build_diemdanh_play_view(guild_id)
            await interaction.edit_original_response(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif ctx == "wallet" and interaction.guild_id is not None:
            target = interaction.user
            cfg2 = await self._cfg(guild_id)
            wallet = await self.wallet.get_or_create_wallet(guild_id, target.id)
            embed = discord.Embed(
                title=_et(new_lang, "wallet_title").format(
                    name=target.display_name if hasattr(target, "display_name") else str(target)
                ),
                color=discord.Color.gold(),
            )
            if hasattr(target, "display_avatar"):
                embed.set_thumbnail(url=target.display_avatar.url)
            embed.add_field(
                name=_et(new_lang, "wallet_balance"),
                value=cfg2.coin_label(wallet.balance),
                inline=False,
            )
            embed.add_field(
                name=_et(new_lang, "wallet_earned"),
                value=cfg2.coin_label(wallet.total_earned),
                inline=True,
            )
            embed.add_field(
                name=_et(new_lang, "wallet_spent"),
                value=cfg2.coin_label(wallet.total_spent),
                inline=True,
            )
            view = discord.ui.View(timeout=60)
            view.add_item(EconLangButton(guild_id, "wallet", new_lang))
            await interaction.edit_original_response(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif ctx == "lb" and interaction.guild_id is not None:
            cfg2 = await self._cfg(guild_id)
            async with self.bot.db.session() as s:
                rows = await s.execute(
                    select(UserWallet)
                    .where(UserWallet.guild_id == guild_id)
                    .order_by(desc(UserWallet.balance))
                    .limit(10)
                )
                wallets = list(rows.scalars().all())
            lines: list[str] = []
            for rank, w in enumerate(wallets, start=1):
                medal = (
                    "🥇" if rank == 1
                    else "🥈" if rank == 2
                    else "🥉" if rank == 3
                    else f"`#{rank}`"
                )
                lines.append(f"{medal} <@{w.user_id}> — **{cfg2.coin_label(w.balance)}**")
            embed = discord.Embed(
                title=_et(new_lang, "lb_title").format(coin=cfg2.coin_name),
                description="\n".join(lines) if lines else _et(new_lang, "lb_empty"),
                color=discord.Color.gold(),
            )
            view = discord.ui.View(timeout=60)
            view.add_item(EconLangButton(guild_id, "lb", new_lang))
            await interaction.edit_original_response(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _get_user_reminder_pref(
        self, guild_id: int, user_id: int,
    ) -> bool:
        """Return the user's dm_enabled flag. Default TRUE if no row
        exists — matches the pref model where opt-out is explicit."""
        async with self.bot.db.session() as s:
            pref = await s.scalar(
                select(CheckinReminderPref).where(
                    CheckinReminderPref.guild_id == guild_id,
                    CheckinReminderPref.user_id == user_id,
                )
            )
        return True if pref is None else bool(pref.dm_enabled)

    async def _set_user_reminder_pref(
        self, guild_id: int, user_id: int, enabled: bool,
    ) -> None:
        """Upsert the per-user opt-out row."""
        async with self.bot.db.session() as s:
            pref = await s.scalar(
                select(CheckinReminderPref).where(
                    CheckinReminderPref.guild_id == guild_id,
                    CheckinReminderPref.user_id == user_id,
                )
            )
            if pref is None:
                pref = CheckinReminderPref(
                    guild_id=guild_id, user_id=user_id, dm_enabled=enabled,
                )
                s.add(pref)
            else:
                pref.dm_enabled = enabled
            await s.commit()

    async def _toggle_user_reminder_pref(
        self, guild_id: int, user_id: int,
    ) -> bool:
        """Flip the flag, upsert, return the new value."""
        current = await self._get_user_reminder_pref(guild_id, user_id)
        new_value = not current
        await self._set_user_reminder_pref(guild_id, user_id, new_value)
        return new_value

    def _format_reminder_state(
        self, enabled: bool, cfg: GuildEconomyConfig,
    ) -> str:
        """Ephemeral echo shown after toggling. Includes a warning when
        the guild has reminders disabled globally — otherwise the user
        might think they'll get DMs when they won't."""
        lang = getattr(cfg, "language", "ru") or "ru"
        body = _et(lang, "reminder_on" if enabled else "reminder_off")
        if not cfg.checkin_reminder_enabled:
            body += _et(lang, "reminder_guild_disabled")
        return body

    async def _build_history_embed(
        self, guild_id: int, user_id: int, filter_value: str,
    ) -> discord.Embed:
        since, until, label = _compute_history_range(filter_value)
        async with self.bot.db.session() as s:
            stmt = select(CoinTransaction).where(
                CoinTransaction.guild_id == guild_id,
                CoinTransaction.user_id == user_id,
                CoinTransaction.reason == REASON_CHECKIN,
                CoinTransaction.created_at >= since,
            )
            if until is not None:
                stmt = stmt.where(CoinTransaction.created_at < until)
            stmt = stmt.order_by(
                desc(CoinTransaction.created_at)
            ).limit(_HISTORY_MAX_ROWS + 1)
            rows = await s.execute(stmt)
            txs = list(rows.scalars().all())
            total_q = await s.execute(
                select(
                    func.count(CoinTransaction.id),
                    func.coalesce(func.sum(CoinTransaction.delta), 0),
                ).where(
                    CoinTransaction.guild_id == guild_id,
                    CoinTransaction.user_id == user_id,
                    CoinTransaction.reason == REASON_CHECKIN,
                    CoinTransaction.created_at >= since,
                    *( (CoinTransaction.created_at < until,) if until else ()),
                )
            )
            total_count, total_coin = total_q.one()

        cfg = await self._cfg(guild_id)
        lang = getattr(cfg, "language", "ru") or "ru"
        coin_icon = cfg.coin_icon or "🪙"
        coin_name = cfg.coin_name

        embed = discord.Embed(
            title=_et(lang, "hist_embed_title"),
            color=discord.Color.from_rgb(52, 152, 219),
        )
        embed.add_field(
            name=_et(lang, "hist_filter_label"),
            value=f"**{label}**",
            inline=True,
        )
        embed.add_field(
            name=_et(lang, "hist_stats_label"),
            value=_et(lang, "hist_stats_value").format(
                count=int(total_count or 0),
                total=int(total_coin or 0),
                coin_icon=coin_icon,
                coin=coin_name,
            ),
            inline=True,
        )

        if not txs:
            embed.add_field(
                name=_et(lang, "hist_details_label"),
                value=_et(lang, "hist_details_empty"),
                inline=False,
            )
        else:
            lines: list[str] = []
            for t in txs[:_HISTORY_MAX_ROWS]:
                meta: dict = {}
                if t.metadata_json:
                    try:
                        meta = json.loads(t.metadata_json)
                    except Exception:
                        meta = {}
                date_str = meta.get(
                    "date", t.created_at.strftime("%Y-%m-%d")
                )
                streak = meta.get("streak")
                bonus = meta.get("bonus", 0) or 0
                streak_part = (
                    f" • streak {streak}" if streak is not None else ""
                )
                bonus_part = f"  🔥+{bonus:,}" if bonus > 0 else ""
                lines.append(
                    f"`{date_str}` **{t.delta:,}** {coin_icon}{streak_part}{bonus_part}"
                )
            if len(txs) > _HISTORY_MAX_ROWS:
                lines.append(
                    _et(lang, "hist_more").format(
                        n=int(total_count or 0) - _HISTORY_MAX_ROWS
                    )
                )
            embed.add_field(
                name=_et(lang, "hist_details_n").format(
                    n=min(len(txs), _HISTORY_MAX_ROWS)
                ),
                value="\n".join(lines)[:1024],
                inline=False,
            )

        embed.set_footer(text=_et(lang, "hist_footer_hint"))
        return embed

    # =========================================================================
    # /economy diemdanh — streak leaderboard (admin)
    # =========================================================================

    diemdanh = app_commands.Group(
        name="diemdanh",
        description="Check-in streak leaderboard",
        parent=economy,
    )

    async def _build_checkin_embed(
        self, guild_id: int, display_count: int
    ) -> discord.Embed:
        cfg = await self._cfg(guild_id)
        lang = getattr(cfg, "language", "ru") or "ru"
        offset = cfg.checkin_tz_offset or 0
        today = _local_today_string(offset)
        yesterday = _local_yesterday_string(offset)
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(DailyCheckin)
                .where(
                    DailyCheckin.guild_id == guild_id,
                    # Only count as "active" streaks if last checkin was
                    # today or yesterday — otherwise the streak is broken
                    # and shouldn't display as live.
                    DailyCheckin.last_checkin_date.in_([today, yesterday]),
                )
                .order_by(
                    DailyCheckin.current_streak.desc(),
                    DailyCheckin.longest_streak.desc(),
                    DailyCheckin.user_id,
                )
                .limit(display_count)
            )
            results = list(rows.scalars().all())

        guild = self.bot.get_guild(guild_id)
        guild_name = guild.name if guild else "Server"
        embed = discord.Embed(
            title=_et(lang, "board_title"),
            color=discord.Color.from_rgb(230, 126, 34),  # warm orange
        )
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if not results:
            embed.description = _et(lang, "board_empty")
        else:
            lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
            for rank, row in enumerate(results, start=1):
                if rank == 1:
                    medal = "🥇"
                elif rank == 2:
                    medal = "🥈"
                elif rank == 3:
                    medal = "🥉"
                else:
                    medal = f"`#{rank:>2}`"
                name = _resolve_member_display(
                    self.bot, guild, row.user_id,
                )
                streak_str = _et(lang, "board_streak").format(
                    streak=row.current_streak, best=row.longest_streak
                )
                lines.append(f"{medal}  {name}  •  {streak_str}")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            embed.description = "\n".join(lines)
        embed.set_footer(
            text=_et(lang, "board_footer").format(
                guild=guild_name, count=display_count
            )
        )
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    async def _build_diemdanh_play_view(
        self, guild_id: int,
    ) -> discord.ui.View:
        """Link button to the configured check-in channel — the place
        the persistent /diemdanh card lives. Lets a leaderboard viewer
        jump straight to where they'd tap the button. Empty view when
        the admin hasn't set checkin_channel_id."""
        view = discord.ui.View(timeout=None)
        cfg = await self._cfg(guild_id)
        lang = getattr(cfg, "language", "ru") or "ru"
        ch_id = cfg.checkin_channel_id if cfg else None
        if ch_id is not None:
            guild = self.bot.get_guild(guild_id)
            ch = guild.get_channel(ch_id) if guild else None
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                view.add_item(discord.ui.Button(
                    label="Go check in",
                    emoji="📝",
                    style=discord.ButtonStyle.link,
                    url=ch.jump_url,
                ))
        view.add_item(EconLangButton(guild_id, "board", lang))
        return view

    async def _refresh_checkin_board(self, guild_id: int) -> None:
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "checkin",
                )
            )
        if board is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(board.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        embed = await self._build_checkin_embed(guild_id, board.display_count)
        view = await self._build_diemdanh_play_view(guild_id)
        if board.message_id is not None:
            try:
                await channel.get_partial_message(board.message_id).edit(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except discord.NotFound:
                pass  # deleted — fall through to repost
            except discord.Forbidden:
                log.warning(
                    "checkin board: forbidden to edit in %s", guild_id,
                )
                return
            except discord.HTTPException as e:
                log.warning("checkin board: edit failed for %s: %s", guild_id, e)
                return
        try:
            new_msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning("checkin board: forbidden to repost in %s", guild_id)
            return
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "checkin",
                )
            )
            if row is not None:
                row.message_id = new_msg.id
                await s.commit()

    @diemdanh.command(
        name="post",
        description="(Admin) Create/replace the Top-Streak board in a channel",
    )
    @app_commands.describe(
        channel="Channel to post the board in (any channel)",
        display_count="Users shown (1-25, default 10)",
    )
    async def diemdanh_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        display_count: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            prev = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "checkin",
                )
            )
        if prev is not None and prev.message_id is not None and interaction.guild:
            prev_ch = interaction.guild.get_channel(prev.channel_id)
            if isinstance(prev_ch, (discord.TextChannel, discord.Thread)):
                try:
                    old_msg = await prev_ch.fetch_message(prev.message_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        embed = await self._build_checkin_embed(
            interaction.guild_id, int(display_count)
        )
        view = await self._build_diemdanh_play_view(interaction.guild_id)
        try:
            msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {channel.mention}.", ephemeral=True
            )
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "checkin",
                )
            )
            if board is None:
                board = GameWinBoard(
                    guild_id=interaction.guild_id,
                    game_type="checkin",
                    channel_id=channel.id,
                    message_id=msg.id,
                    display_count=int(display_count),
                )
                s.add(board)
            else:
                board.channel_id = channel.id
                board.message_id = msg.id
                board.display_count = int(display_count)
            await s.commit()
        await interaction.followup.send(
            f"✅ Top-Streak board posted in {channel.mention} "
            f"(top {display_count}). Auto-updates after every /diemdanh.",
            ephemeral=True,
        )

    @diemdanh.command(
        name="remove",
        description="(Admin) Remove the Top-Streak board + stop auto-update",
    )
    async def diemdanh_remove(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "checkin",
                )
            )
            if board is None:
                await interaction.followup.send(
                    "No Top-Streak board in this server.", ephemeral=True
                )
                return
            channel_id = board.channel_id
            message_id = board.message_id
            await s.delete(board)
            await s.commit()
        if message_id is not None and interaction.guild is not None:
            ch = interaction.guild.get_channel(channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await ch.fetch_message(message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        await interaction.followup.send(
            "✅ Top-Streak board removed.", ephemeral=True
        )

    @diemdanh.command(
        name="refresh",
        description="(Admin) Refresh the Top-Streak board immediately",
    )
    async def diemdanh_refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "checkin",
                )
            )
        if board is None:
            await interaction.followup.send(
                "No board configured. Run `/economy diemdanh post` first.",
                ephemeral=True,
            )
            return
        await self._refresh_checkin_board(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Top-Streak board refreshed in <#{board.channel_id}>.",
            ephemeral=True,
        )

    # =========================================================================
    # /economy diemdanh card-* — persistent button card management
    # =========================================================================

    async def _refresh_checkin_card(self, guild_id: int) -> None:
        """Edit the persistent card in-place to reflect today's checkin
        list. If the tracked message has been deleted, the next refresh
        reposts in the same channel and updates the pointer."""
        async with self.bot.db.session() as s:
            card = await s.get(CheckinCard, guild_id)
        if card is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(card.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning(
                "checkin card: channel %s not usable for guild %s",
                card.channel_id, guild_id,
            )
            return

        cfg = await self._cfg(guild_id)
        offset = cfg.checkin_tz_offset or 0
        today = _local_today_string(offset)
        async with self.bot.db.session() as s:
            rows_q = await s.execute(
                select(DailyCheckin)
                .where(
                    DailyCheckin.guild_id == guild_id,
                    DailyCheckin.last_checkin_date == today,
                )
                .order_by(DailyCheckin.updated_at)
            )
            rows = list(rows_q.scalars().all())

        # Resolve user display per row — falls back to a plain-text
        # name for users who left the guild so they don't show as a
        # broken raw <@id> link.
        member_lookup = {
            row.user_id: _resolve_member_display(self.bot, guild, row.user_id)
            for row in rows
        }

        lang = getattr(cfg, "language", "ru") or "ru"
        embed = _build_checkin_card_embed(cfg, today, rows, member_lookup)
        view = _build_card_view(guild_id, lang)
        if card.message_id is not None:
            try:
                await channel.get_partial_message(card.message_id).edit(
                    embed=embed, view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except discord.NotFound:
                pass  # deleted — fall through to repost
            except discord.Forbidden:
                log.warning(
                    "checkin card: forbidden to edit in %s", guild_id,
                )
                return
            except discord.HTTPException as e:
                log.warning("checkin card: edit failed for %s: %s", guild_id, e)
                return
        # Lost the message — repost and update the pointer.
        try:
            new_msg = await channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning("checkin card: forbidden to repost in %s", guild_id)
            return
        async with self.bot.db.session() as s:
            row = await s.get(CheckinCard, guild_id)
            if row is not None:
                row.message_id = new_msg.id
                await s.commit()

    @diemdanh.command(
        name="card-post",
        description="(Admin) Create/replace the check-in button card in a channel",
    )
    @app_commands.describe(
        channel="Channel to post the card in (members press the button here)",
    )
    async def diemdanh_card_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        # Delete any previous card message + DB row so we never leave a
        # zombie card floating in another channel.
        async with self.bot.db.session() as s:
            prev = await s.get(CheckinCard, interaction.guild_id)
        if prev is not None and prev.message_id is not None and interaction.guild:
            prev_ch = interaction.guild.get_channel(prev.channel_id)
            if isinstance(prev_ch, (discord.TextChannel, discord.Thread)):
                try:
                    old_msg = await prev_ch.fetch_message(prev.message_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        cfg = await self._cfg(interaction.guild_id)
        offset = cfg.checkin_tz_offset or 0
        today = _local_today_string(offset)
        async with self.bot.db.session() as s:
            rows_q = await s.execute(
                select(DailyCheckin)
                .where(
                    DailyCheckin.guild_id == interaction.guild_id,
                    DailyCheckin.last_checkin_date == today,
                )
                .order_by(DailyCheckin.updated_at)
            )
            rows = list(rows_q.scalars().all())
        member_lookup = {
            row.user_id: _resolve_member_display(
                self.bot, interaction.guild, row.user_id,
            )
            for row in rows
        }
        lang = getattr(cfg, "language", "ru") or "ru"
        embed = _build_checkin_card_embed(cfg, today, rows, member_lookup)
        view = _build_card_view(interaction.guild_id, lang)
        try:
            msg = await channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {channel.mention}.", ephemeral=True
            )
            return

        async with self.bot.db.session() as s:
            card = await s.get(CheckinCard, interaction.guild_id)
            if card is None:
                card = CheckinCard(
                    guild_id=interaction.guild_id,
                    channel_id=channel.id,
                    message_id=msg.id,
                )
                s.add(card)
            else:
                card.channel_id = channel.id
                card.message_id = msg.id
            await s.commit()

        await interaction.followup.send(
            f"✅ Check-in card posted in {channel.mention}. "
            "Members press the **📍 Check in now** button on the card to earn coins. "
            "The card auto-refreshes with each check-in and auto-clears at "
            "00:00 UTC daily.",
            ephemeral=True,
        )

    @diemdanh.command(
        name="card-remove",
        description="(Admin) Remove the check-in card",
    )
    async def diemdanh_card_remove(
        self, interaction: discord.Interaction
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            card = await s.get(CheckinCard, interaction.guild_id)
            if card is None:
                await interaction.followup.send(
                    "No check-in card in this server.", ephemeral=True
                )
                return
            channel_id = card.channel_id
            message_id = card.message_id
            await s.delete(card)
            await s.commit()
        if message_id is not None and interaction.guild is not None:
            ch = interaction.guild.get_channel(channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await ch.fetch_message(message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        await interaction.followup.send(
            "✅ Check-in card removed.", ephemeral=True
        )

    @diemdanh.command(
        name="card-refresh",
        description="(Admin) Refresh the check-in card immediately",
    )
    async def diemdanh_card_refresh(
        self, interaction: discord.Interaction
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            card = await s.get(CheckinCard, interaction.guild_id)
        if card is None:
            await interaction.followup.send(
                "No card configured. Run `/economy diemdanh card-post` first.",
                ephemeral=True,
            )
            return
        await self._refresh_checkin_card(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Card refreshed in <#{card.channel_id}>.", ephemeral=True
        )

    # =========================================================================
    # Local-midnight sweep — card refresh + DM reminders, per guild tz
    # =========================================================================

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Re-entry guarded so gateway reconnects don't spawn duplicates.
        if self._midnight_task is None or self._midnight_task.done():
            self._midnight_task = asyncio.create_task(
                self._local_midnight_loop()
            )

    async def _local_midnight_loop(self) -> None:
        """Hourly sweep. For each guild the bot is in, check if the
        LOCAL calendar date (per checkin_tz_offset) has rolled over
        since the last sweep. If yes:
          - refresh the persistent check-in card (no-op if no card)
          - DM every non-bot, non-already-checked member when
            checkin_reminder_enabled is True

        First sighting per guild PER PROCESS is recorded silently — we
        don't DM on bot startup / first deploy to avoid a noisy blast.
        Subsequent date changes (the actual daily rollover) fire DMs.
        """
        log.info("checkin: hourly local-midnight loop started")
        while True:
            try:
                now = datetime.now(timezone.utc)
                # Sleep to the next HH:00:30 UTC.
                target = (now + timedelta(hours=1)).replace(
                    minute=0, second=30, microsecond=0
                )
                if target <= now:
                    target += timedelta(hours=1)
                sleep_seconds = (target - now).total_seconds()
                log.info(
                    "checkin: next hourly sweep at %s UTC (in %ds)",
                    target.isoformat(), int(sleep_seconds),
                )
                await asyncio.sleep(sleep_seconds)
                await self._local_midnight_sweep()
            except asyncio.CancelledError:
                log.info("checkin hourly loop cancelled")
                break
            except Exception:
                log.exception(
                    "checkin hourly loop crashed; retry in 60s"
                )
                await asyncio.sleep(60)

    async def _local_midnight_sweep(self) -> None:
        """One pass over every guild the bot is in. Two independent
        concerns:

          (a) Card refresh: fires on LOCAL date change (per
              checkin_tz_offset) so the daily list clears at the right
              time.
          (b) DM reminder: if checkin_reminder_hour_utc is set, fires
              when the current UTC hour matches that value (dedup once
              per UTC day). If not set, falls back to the same local-
              midnight detection used for the card (first-sighting on
              boot is silenced to avoid blast on redeploy)."""
        now_utc_hour = datetime.now(timezone.utc).hour
        for guild in list(self.bot.guilds):
            try:
                cfg = await self._cfg(guild.id)
            except Exception:
                log.exception("checkin: cfg load failed for %s", guild.id)
                continue
            offset = cfg.checkin_tz_offset or 0
            local_today = _local_today_string(offset)

            # === (a) Card refresh on local-date change ===
            last_card = self._last_card_date.get(guild.id)
            local_date_changed = last_card != local_today
            is_first_card_sighting = last_card is None
            if local_date_changed:
                self._last_card_date[guild.id] = local_today
                log.info(
                    "checkin: guild %s local date %s → %s [%s]",
                    guild.id, last_card, local_today,
                    _format_tz_offset(offset),
                )
                try:
                    await self._refresh_checkin_card(guild.id)
                except Exception:
                    log.exception(
                        "checkin: card refresh failed for %s", guild.id
                    )

            # === (b) DM reminder ===
            if not cfg.checkin_reminder_enabled:
                continue

            hour_cfg = cfg.checkin_reminder_hour_utc  # local hour now
            if hour_cfg is not None:
                # Owner pinned a LOCAL hour (per checkin_tz_offset).
                # Translate to UTC for the actual sweep comparison.
                target_utc_hour = (hour_cfg - offset) % 24
                if now_utc_hour != target_utc_hour:
                    continue
                # Dedup by LOCAL date — if the hour crosses UTC midnight
                # from the guild's perspective, this still fires exactly
                # once per local calendar day.
                last_dm = self._last_dm_date.get(guild.id)
                if last_dm == local_today:
                    continue
                self._last_dm_date[guild.id] = local_today
                log.info(
                    "checkin: guild %s — local hour %02d hit "
                    "(= %02d UTC), sending DMs",
                    guild.id, hour_cfg, target_utc_hour,
                )
                try:
                    await self._send_reminders_for_guild(
                        guild, cfg, local_today,
                    )
                except Exception:
                    log.exception(
                        "checkin: DM batch failed for %s", guild.id
                    )
            else:
                # Fallback: fire on local-midnight detection. Skip on
                # first-sighting-per-process so a restart doesn't blast.
                if not local_date_changed:
                    continue
                if is_first_card_sighting:
                    log.info(
                        "checkin: first sighting for %s, skipping DM",
                        guild.id,
                    )
                    continue
                try:
                    await self._send_reminders_for_guild(
                        guild, cfg, local_today,
                    )
                except Exception:
                    log.exception(
                        "checkin: DM batch failed for %s", guild.id
                    )

    async def _send_reminders_for_guild(
        self,
        guild: discord.Guild,
        cfg: GuildEconomyConfig,
        today: str,
    ) -> None:
        """DM every non-bot member who hasn't checked in today (per
        guild local tz) AND hasn't opted out of DM reminders. Throttled
        at _REMINDER_DM_DELAY_SECONDS between sends to keep us under
        Discord's bot-DM rate limits."""
        async with self.bot.db.session() as s:
            already = await s.execute(
                select(DailyCheckin.user_id).where(
                    DailyCheckin.guild_id == guild.id,
                    DailyCheckin.last_checkin_date == today,
                )
            )
            already_set = {r[0] for r in already.all()}
            # Users who explicitly opted out. Missing rows = default ON,
            # so we only need the disabled=False set.
            opted_out_rows = await s.execute(
                select(CheckinReminderPref.user_id).where(
                    CheckinReminderPref.guild_id == guild.id,
                    CheckinReminderPref.dm_enabled == False,  # noqa: E712
                )
            )
            opted_out_set = {r[0] for r in opted_out_rows.all()}
        lang = getattr(cfg, "language", "ru") or "ru"
        text = _et(lang, "reminder_dm").format(
            guild=guild.name,
            min=_CHECKIN_MIN,
            max=_CHECKIN_MAX,
            n=_CHECKIN_STREAK_LENGTH,
            bonus=_CHECKIN_STREAK_BONUS,
        )
        sent = 0
        skipped_bot = 0
        skipped_already = 0
        skipped_opted_out = 0
        skipped_dm_blocked = 0
        for member in guild.members:
            if member.bot:
                skipped_bot += 1
                continue
            if member.id in already_set:
                skipped_already += 1
                continue
            if member.id in opted_out_set:
                skipped_opted_out += 1
                continue
            try:
                await member.send(text)
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                # User blocks DMs from non-friends or rate limit.
                skipped_dm_blocked += 1
            await asyncio.sleep(_REMINDER_DM_DELAY_SECONDS)
        log.info(
            "checkin reminder: guild %s (%s) — sent=%d, "
            "skipped(bot=%d, claimed=%d, opted_out=%d, dm_off=%d)",
            guild.id, guild.name, sent,
            skipped_bot, skipped_already, skipped_opted_out,
            skipped_dm_blocked,
        )


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(Economy(bot))
