"""Sohoc (number-guessing) — number-guess minigame.

Game loop
---------
1. Creator runs /sohoc start. Bot posts a single game embed in the
   configured channel with a 'Tham gia' button and a countdown.
2. Each user clicks Tham gia → modal asks for a 2-digit number (00-99).
   Valid submissions are recorded and the embed is updated with the
   entry list (name • number • timestamp).
3. When the end time hits, the Tham gia button is disabled and a 'Roll
   number' button appears for the creator.
4. Creator clicks Roll → bot picks a random 0..99 → finds the earliest
   entry that matches → winner shown in the embed with a 🏆 icon.
5. Winner is also announced in the announce channel (if configured)
   and their lifetime win count is incremented. At 5+ wins the
   configured winner role is granted automatically.

Auto-dismiss
   Every 'Picked N' ephemeral ack the bot sends after a user
   submits is tracked and deleted when the end timer fires, so
   players' DMs stay clean by the time results land.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

# Default UTC offset (hours) used when a guild has not configured
# sohoc_tz_offset yet. UTC+7 (Vietnam) keeps existing setups working
# the same way after the upgrade.
_DEFAULT_TZ_OFFSET = 7

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..db.models import (
    GameWinBoard,
    GuildConfig,
    MiniGameRewardConfig,
    SohocEntry,
    SohocSession,
    SohocWin,
)
from ..i18n import (
    LANG_BUTTON_EMOJI,
    LANG_EN,
    LANG_RU,
    LANG_VI,
    normalize_lang,
)

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.sohoc")


_WINS_FOR_ROLE = 5  # auto-grant winner role at this many lifetime wins
_NUM_RE = re.compile(r"^\d{1,2}$")
_MAX_ENTRIES_IN_EMBED = 50  # don't blow past the 4096 description limit
_HHMM_RE = re.compile(r"^([0-2]?\d):([0-5]\d)$")
# Digits only (no thousands separator, no decimals). Max 10 digits keeps
# bet amounts inside int32 so downstream arithmetic can't overflow.
_BET_RE = re.compile(r"^\d{1,10}$")
_BET_UNIT = 1000  # bonus = (bet // _BET_UNIT) * top1_reward


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """Return (hour, minute) for a 24h 'HH:MM' string, or None if invalid."""
    m = _HHMM_RE.match(text.strip())
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    return h, mn


def tz_from_offset(hours: int) -> timezone:
    return timezone(timedelta(hours=hours))


def format_tz_offset(hours: int) -> str:
    """Render the configured offset as 'UTC+7' / 'UTC-3' / 'UTC'."""
    if hours == 0:
        return "UTC"
    return f"UTC{hours:+d}"


def _find_two_winners(
    entries: list["SohocEntry"], rolled: int, rolled_2: int,
) -> tuple["SohocEntry | None", "SohocEntry | None"]:
    """Return (top1_winner, top2_winner) — the earliest entry that
    matches each roll. Same user could theoretically hit both if they'd
    picked two numbers, but each user only picks one → the two winners
    are always different (or None if nobody picked that number).
    """
    winner_1: "SohocEntry | None" = None
    winner_2: "SohocEntry | None" = None
    for e in sorted(entries, key=lambda x: x.submitted_at):
        if winner_1 is None and e.number == rolled:
            winner_1 = e
        elif winner_2 is None and e.number == rolled_2:
            winner_2 = e
        if winner_1 is not None and winner_2 is not None:
            break
    return winner_1, winner_2


def next_end_at(h: int, mn: int, tz_offset: int) -> datetime:
    """Return the next UTC datetime matching HH:MM in the given UTC offset tz.

    If today's HH:MM already passed (in that local tz), roll to tomorrow.
    """
    tz = tz_from_offset(tz_offset)
    now = datetime.now(tz)
    target = now.replace(hour=h, minute=mn, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


# =============================================================================
# Translations
# =============================================================================

_SOHOC_LANG_BUTTON_LABEL: dict[str, str] = {
    LANG_RU: "English",
    LANG_EN: "Русский",
    LANG_VI: "Русский",
}

_SOHOC_NEXT_LANG: dict[str, str] = {
    LANG_RU: LANG_EN,
    LANG_EN: LANG_RU,
    LANG_VI: LANG_RU,
}


def _sohoc_next_lang(current: str) -> str:
    return _SOHOC_NEXT_LANG.get(current, LANG_RU)


_I18N: dict[str, dict[str, str]] = {
    LANG_RU: {
        "btn_join":    "Участвовать",
        "btn_bet":     "Ставка",
        "btn_roll":    "Жеребьёвка",
        "btn_end":     "Завершить",
        "btn_explain": "Объяснение",
        "title_waiting":   "🎲 Угадай Число — Приём заявок",
        "title_rollable":  "🎲 Угадай Число — Время вышло, жеребьёвка",
        "title_cancelled": "🎲 Угадай Число — Отменено",
        "title_finished":  "🎲 Угадай Число — Результат",
        "creator":     "**Создатель:** {mention}",
        "deadline":    "**Приём заявок до:** <t:{ts}:R>",
        "hint_join":   "Нажми **Участвовать** → введи число 00-99 → Отправить.",
        "hint_bet": (
            "💰 Нажми **Ставка** чтобы умножить приз при угадывании "
            "(❓ Объяснение для формулы)."
        ),
        "hint_rollable": "Создатель, нажмите **Жеребьёвка** для розыгрыша.",
        "cancelled_note": (
            "🛑 Игра отменена администратором. Все взносы (если были) "
            "возвращены."
        ),
        "rolled_number":   "🎯 **Число главного приза:** `{n:02d}`",
        "rolled_number_2": "🎁 **Утешительное число:** `{n:02d}`",
        "winner_line":     "🏆 **Главный приз:** <@{uid}>",
        "winner_line_2":   "🎉 **Утешительный приз:** <@{uid}>",
        "no_winner":       "😢 Никто не угадал число главного приза.",
        "no_winner_2":     "😢 Никто не угадал утешительное число.",
        "entries_field":   "👥 Участники ({n})",
        "entries_empty":   "_Участников пока нет._",
        "entries_more":    "_(+{n} игрок(ов) ещё)_",
        "total_prize_footer": (
            "Итого призовых: {total} (зачислено на кошельки)"
        ),
    },
    LANG_VI: {
        "btn_join":    "Tham gia",
        "btn_bet":     "Đặt cược",
        "btn_roll":    "Roll số",
        "btn_end":     "Kết thúc",
        "btn_explain": "Giải thích",
        "title_waiting":   "🎲 Đoán Số — Tham gia ngay",
        "title_rollable":  "🎲 Đoán Số — Hết giờ, chờ Roll",
        "title_cancelled": "🎲 Đoán Số — Đã hủy",
        "title_finished":  "🎲 Đoán Số — Kết quả",
        "creator":     "**Creator:** {mention}",
        "deadline":    "**Hết giờ chọn số:** <t:{ts}:R>",
        "hint_join":   "Bấm **Tham gia** → nhập số 00-99 → Submit.",
        "hint_bet": (
            "💰 Bấm **Đặt cược** để nhân thưởng khi đoán trúng "
            "(❓ Giải thích để xem công thức)."
        ),
        "hint_rollable": "Creator hãy bấm **Roll số** để bốc thăm.",
        "cancelled_note": (
            "🛑 Game đã bị admin hủy. Tất cả phí tham gia (nếu có) "
            "đã được hoàn lại."
        ),
        "rolled_number":   "🎯 **Số giải nhất:** `{n:02d}`",
        "rolled_number_2": "🎁 **Số giải khuyến khích:** `{n:02d}`",
        "winner_line":     "🏆 **Giải nhất:** <@{uid}>",
        "winner_line_2":   "🎉 **Giải khuyến khích:** <@{uid}>",
        "no_winner":       "😢 Không ai chọn trúng số giải nhất.",
        "no_winner_2":     "😢 Không ai chọn trúng số giải khuyến khích.",
        "entries_field":   "👥 Đã chọn ({n})",
        "entries_empty":   "_Chưa có ai tham gia._",
        "entries_more":    "_(+{n} người chơi khác)_",
        "total_prize_footer": (
            "Tổng giải thưởng: {total} (đã ghi vào ví của từng người)"
        ),
    },
    LANG_EN: {
        "btn_join":    "Join",
        "btn_bet":     "Bet",
        "btn_roll":    "Roll",
        "btn_end":     "End",
        "btn_explain": "Explain",
        "title_waiting":   "🎲 Guess Number — Entries Open",
        "title_rollable":  "🎲 Guess Number — Time's Up, Awaiting Roll",
        "title_cancelled": "🎲 Guess Number — Cancelled",
        "title_finished":  "🎲 Guess Number — Result",
        "creator":     "**Creator:** {mention}",
        "deadline":    "**Entries close:** <t:{ts}:R>",
        "hint_join":   "Click **Join** → type a number 00-99 → Submit.",
        "hint_bet": (
            "💰 Click **Bet** to multiply the prize if you guess right "
            "(❓ Explain for the formula)."
        ),
        "hint_rollable": "Creator, click **Roll** to draw the number.",
        "cancelled_note": (
            "🛑 The game was cancelled by an admin. Any entry fees "
            "have been refunded."
        ),
        "rolled_number":   "🎯 **Grand prize number:** `{n:02d}`",
        "rolled_number_2": "🎁 **Consolation number:** `{n:02d}`",
        "winner_line":     "🏆 **Grand prize:** <@{uid}>",
        "winner_line_2":   "🎉 **Consolation:** <@{uid}>",
        "no_winner":       "😢 No one guessed the grand prize number.",
        "no_winner_2":     "😢 No one guessed the consolation number.",
        "entries_field":   "👥 Entries ({n})",
        "entries_empty":   "_No entries yet._",
        "entries_more":    "_(+{n} more players)_",
        "total_prize_footer": (
            "Total prize: {total} (credited to each wallet)"
        ),
    },
}


def _t(lang: str, key: str, **fmt: object) -> str:
    table = _I18N.get(lang, _I18N[LANG_RU])
    return table[key].format(**fmt) if fmt else table[key]


def _session_lang(session: SohocSession) -> str:
    lang = getattr(session, "language", None)
    if lang in (LANG_EN, LANG_RU, LANG_VI):
        return lang
    return LANG_RU


# =============================================================================
# Embed builder
# =============================================================================


def _fmt_local(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M:%S UTC")


def _build_game_embed(
    session: SohocSession,
    creator_mention: str,
    *,
    rewards_map: dict[int, int] | None = None,
    coin_icon: str = "🪙",
) -> discord.Embed:
    lang = _session_lang(session)
    if session.status == "waiting":
        color = discord.Color.blurple()
        title = _t(lang, "title_waiting")
    elif session.status == "rollable":
        color = discord.Color.gold()
        title = _t(lang, "title_rollable")
    elif session.status == "cancelled":
        color = discord.Color.red()
        title = _t(lang, "title_cancelled")
    else:  # finished
        color = (
            discord.Color.green()
            if session.winner_id is not None
            else discord.Color.dark_gray()
        )
        title = _t(lang, "title_finished")

    embed = discord.Embed(title=title, color=color)
    desc_lines = [_t(lang, "creator", mention=creator_mention)]
    if session.status == "waiting":
        end_ts = int(session.end_at.timestamp())
        desc_lines.append(_t(lang, "deadline", ts=end_ts))
        desc_lines.append(_t(lang, "hint_join"))
        desc_lines.append(_t(lang, "hint_bet"))
    elif session.status == "rollable":
        desc_lines.append(_t(lang, "hint_rollable"))
    elif session.status == "cancelled":
        desc_lines.append(_t(lang, "cancelled_note"))
    else:
        # Grand prize (top-1) line.
        if session.rolled_number is not None:
            desc_lines.append(
                _t(lang, "rolled_number", n=session.rolled_number)
            )
        if session.winner_id is not None:
            desc_lines.append(_t(lang, "winner_line", uid=session.winner_id))
        else:
            desc_lines.append(_t(lang, "no_winner"))
        # Consolation (top-2) line — only shown when the second roll
        # was actually drawn (legacy games have rolled_number_2 = NULL).
        rolled_2 = getattr(session, "rolled_number_2", None)
        winner_2 = getattr(session, "winner_2_id", None)
        if rolled_2 is not None:
            desc_lines.append(_t(lang, "rolled_number_2", n=rolled_2))
            if winner_2 is not None:
                desc_lines.append(_t(lang, "winner_line_2", uid=winner_2))
            else:
                desc_lines.append(_t(lang, "no_winner_2"))
    embed.description = "\n".join(desc_lines)

    entries = list(session.entries)
    rewards_map = rewards_map or {}
    if entries:
        lines: list[str] = []
        rolled_2 = getattr(session, "rolled_number_2", None)
        winner_2 = getattr(session, "winner_2_id", None)
        for i, e in enumerate(entries[:_MAX_ENTRIES_IN_EMBED], start=1):
            tag = ""
            if session.status == "finished":
                if (
                    session.winner_id == e.user_id
                    and session.rolled_number == e.number
                ):
                    tag = " 🏆"
                elif (
                    winner_2 == e.user_id
                    and rolled_2 is not None
                    and rolled_2 == e.number
                ):
                    tag = " 🎉"
            prize = rewards_map.get(e.user_id, 0)
            prize_part = (
                f" • 🎁 `{prize:,}` {coin_icon}" if prize > 0 else ""
            )
            bet_val = int(getattr(e, "bet_amount", 0) or 0)
            bet_part = (
                f" • 💰 `{bet_val:,}`" if bet_val > 0 else ""
            )
            # Timestamp intentionally omitted — the card was cluttered
            # with UTC times that most players couldn't use.
            lines.append(
                f"`{i:>2}.` <@{e.user_id}> — `{e.number:02d}`"
                f"{tag}{bet_part}{prize_part}"
            )
        if len(entries) > _MAX_ENTRIES_IN_EMBED:
            lines.append(
                _t(lang, "entries_more",
                   n=len(entries) - _MAX_ENTRIES_IN_EMBED)
            )
        embed.add_field(
            name=_t(lang, "entries_field", n=len(entries)),
            value="\n".join(lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name=_t(lang, "entries_field", n=0),
            value=_t(lang, "entries_empty"),
            inline=False,
        )
    if rewards_map and session.status == "finished":
        total_paid = sum(rewards_map.values())
        embed.set_footer(
            text=_t(lang, "total_prize_footer", total=f"{total_paid:,}")
        )
    return embed


# =============================================================================
# Modal
# =============================================================================


class _NumberModal(discord.ui.Modal):
    def __init__(self, cog: "Sohoc", session_id: int) -> None:
        super().__init__(title="Pick a number 00-99")
        self.cog = cog
        self.session_id = session_id
        self.number_input = discord.ui.TextInput(
            label="Your number (00-99)",
            placeholder="e.g. 07, 42, 99",
            min_length=1,
            max_length=2,
            required=True,
        )
        self.add_item(self.number_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.number_input.value).strip()
        if not _NUM_RE.match(raw):
            await interaction.response.send_message(
                "❌ Number must be 1-2 digits (00-99).", ephemeral=True
            )
            return
        n = int(raw)
        if not 0 <= n <= 99:
            await interaction.response.send_message(
                "❌ Out of range. Only 00-99 is accepted.",
                ephemeral=True,
            )
            return

        async with self.cog.bot.db.session() as s:
            session = await s.get(SohocSession, self.session_id)
            if session is None:
                await interaction.response.send_message(
                    "Game no longer exists.", ephemeral=True
                )
                return
            if session.status != "waiting":
                await interaction.response.send_message(
                    "⏱ Game entries are closed, no more numbers accepted.",
                    ephemeral=True,
                )
                return
            existing = await s.scalar(
                select(SohocEntry).where(
                    SohocEntry.session_id == self.session_id,
                    SohocEntry.user_id == interaction.user.id,
                )
            )
            if existing is not None:
                await interaction.response.send_message(
                    f"❌ You already picked `{existing.number:02d}` — "
                    "can't change it.",
                    ephemeral=True,
                )
                return
            s.add(
                SohocEntry(
                    session_id=self.session_id,
                    user_id=interaction.user.id,
                    number=n,
                )
            )
            await s.commit()

        # Entry-fee deduction. Reads the per-guild MiniGameRewardConfig for
        # 'sohoc'; if entry_fee > 0 we debit the wallet right after the
        # entry insert. On failure (e.g. wallet balance dropped between
        # config-read and debit), reverse the entry insert so the user
        # isn't recorded without paying.
        from ..db.models import MiniGameRewardConfig, GuildEconomyConfig
        from ..economy.service import WalletService, InsufficientBalanceError
        from sqlalchemy import delete as sa_delete
        guild_id = interaction.guild_id
        fee = 0
        coin_name = "Coin"
        coin_icon = ""
        if guild_id is not None:
            async with self.cog.bot.db.session() as s:
                mg_cfg = await s.scalar(
                    select(MiniGameRewardConfig).where(
                        MiniGameRewardConfig.guild_id == guild_id,
                        MiniGameRewardConfig.game_type == "sohoc",
                    )
                )
                if mg_cfg is not None and mg_cfg.enabled:
                    fee = int(mg_cfg.entry_fee or 0)
                econ_cfg = await s.get(GuildEconomyConfig, guild_id)
                if econ_cfg is not None:
                    coin_name = econ_cfg.coin_name
                    coin_icon = econ_cfg.coin_icon or ""

        fee_note = ""
        if fee > 0 and guild_id is not None:
            wallet_svc = WalletService(self.cog.bot)
            try:
                await wallet_svc.adjust(
                    guild_id,
                    interaction.user.id,
                    -fee,
                    f"minigame_sohoc_entry_fee",
                    description=f"Sohoc session #{self.session_id} entry fee",
                    metadata={"session_id": self.session_id, "number": n},
                )
            except InsufficientBalanceError:
                # Roll back the entry so the user isn't on the roster
                # without paying.
                async with self.cog.bot.db.session() as s:
                    await s.execute(
                        sa_delete(SohocEntry).where(
                            SohocEntry.session_id == self.session_id,
                            SohocEntry.user_id == interaction.user.id,
                        )
                    )
                    await s.commit()
                await interaction.response.send_message(
                    f"❌ Not enough coins for the **{fee:,} {coin_icon}{coin_name}** "
                    f"entry fee. Entry cancelled.",
                    ephemeral=True,
                )
                return
            fee_note = (
                f" (charged **{fee:,} {coin_icon}{coin_name}** entry fee)"
            )

        await interaction.response.send_message(
            f"✅ Recorded number `{n:02d}`{fee_note}. "
            "Wait for the Creator to Roll.",
            ephemeral=True,
        )
        # Two-layer cleanup: short auto-dismiss (so the user's chat clears
        # quickly even on long games) plus the existing end-timer sweep
        # as a safety net for any acks that the short timer missed.
        self.cog._acks.setdefault(self.session_id, []).append(interaction)
        asyncio.create_task(self.cog._schedule_ack_dismiss(interaction))

        # Refresh the public embed so the list updates live
        await self.cog._refresh_embed(self.session_id)


class _BetModal(discord.ui.Modal):
    """Coin escrow for the 'Bet' button.

    The user must have already picked a number (an entry row exists)
    before betting. Coins are debited immediately with reason
    ``minigame_sohoc_bet``; the entry's bet_amount is incremented (a
    second submit stacks on top of the first, no cap other than the
    wallet balance and the modal's max_length).

    Payout at roll time:
      • Winner:  wallet += bet_amount + (bet_amount // 1000) * top1_reward
                 (bet returned + bonus, on top of the standard top-1 prize)
      • Loser:   bet stays lost (already debited).

    The bonus multiplier is `top1_reward` read at ROLL time — matching
    the standard win prize configuration on the same session.
    """

    def __init__(self, cog: "Sohoc", session_id: int) -> None:
        super().__init__(title="Place a bet")
        self.cog = cog
        self.session_id = session_id
        self.amount_input = discord.ui.TextInput(
            label="Coin amount to bet",
            placeholder="e.g. 500, 5000, 10000 (positive integer)",
            min_length=1,
            max_length=10,
            required=True,
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.amount_input.value).strip().replace(",", "").replace(".", "")
        if not _BET_RE.match(raw):
            await interaction.response.send_message(
                "❌ Bet must be a positive integer (1-10 digits).",
                ephemeral=True,
            )
            return
        amount = int(raw)
        if amount <= 0:
            await interaction.response.send_message(
                "❌ Bet must be > 0.", ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "❌ This can only be used in a server.", ephemeral=True,
            )
            return

        # Fast checks against DB before touching the wallet: session
        # exists, is still 'waiting', user has an entry to bet on.
        async with self.cog.bot.db.session() as s:
            session = await s.get(SohocSession, self.session_id)
            if session is None:
                await interaction.response.send_message(
                    "Game no longer exists.", ephemeral=True,
                )
                return
            if session.status != "waiting":
                await interaction.response.send_message(
                    "⏱ Game entries are closed, no more bets accepted.",
                    ephemeral=True,
                )
                return
            entry = await s.scalar(
                select(SohocEntry).where(
                    SohocEntry.session_id == self.session_id,
                    SohocEntry.user_id == interaction.user.id,
                )
            )
            if entry is None:
                await interaction.response.send_message(
                    "❌ You must press **Join** and pick a number before betting.",
                    ephemeral=True,
                )
                return

        # Debit the wallet. On insufficient balance the entry stays as-is
        # (they just don't bet) — no rollback needed since we haven't
        # written to the entry yet.
        from ..db.models import GuildEconomyConfig
        from ..economy.service import WalletService, InsufficientBalanceError

        coin_name = "Coin"
        coin_icon = ""
        async with self.cog.bot.db.session() as s:
            econ_cfg = await s.get(GuildEconomyConfig, guild_id)
            if econ_cfg is not None:
                coin_name = econ_cfg.coin_name
                coin_icon = econ_cfg.coin_icon or ""

        wallet_svc = WalletService(self.cog.bot)
        try:
            await wallet_svc.adjust(
                guild_id,
                interaction.user.id,
                -amount,
                "minigame_sohoc_bet",
                description=f"Sohoc session #{self.session_id} bet",
                metadata={
                    "session_id": self.session_id,
                    "amount": amount,
                },
            )
        except InsufficientBalanceError:
            await interaction.response.send_message(
                f"❌ Not enough coins to bet **{amount:,} {coin_icon}{coin_name}**. "
                "Lower the bet or earn more coins.",
                ephemeral=True,
            )
            return

        # Persist the bet increment. Stacking is allowed — a user who
        # bets 1000 then bets 500 more ends up with bet_amount=1500.
        # Re-check session status here to close the narrow window
        # between the wallet debit and this write. If the roll has
        # already fired (status flipped), refund and bail so the bet
        # isn't credited after the payout already ran.
        async with self.cog.bot.db.session() as s:
            session_recheck = await s.get(SohocSession, self.session_id)
            fresh_entry = await s.scalar(
                select(SohocEntry).where(
                    SohocEntry.session_id == self.session_id,
                    SohocEntry.user_id == interaction.user.id,
                )
            )
            status_ok = (
                session_recheck is not None
                and session_recheck.status == "waiting"
            )
            if fresh_entry is None or not status_ok:
                await wallet_svc.adjust(
                    guild_id,
                    interaction.user.id,
                    amount,
                    "minigame_sohoc_bet_refund",
                    description=(
                        f"Sohoc session #{self.session_id} bet auto-refund "
                        "(entry gone or game closed)"
                    ),
                    metadata={"session_id": self.session_id},
                )
                await interaction.response.send_message(
                    "❌ Missed the bet window — game entries are closed. "
                    "Your bet has been refunded.",
                    ephemeral=True,
                )
                return
            fresh_entry.bet_amount = int(fresh_entry.bet_amount or 0) + amount
            new_total = fresh_entry.bet_amount
            await s.commit()

        await interaction.response.send_message(
            f"✅ Bet **{amount:,} {coin_icon}{coin_name}** placed. "
            f"Current total bet: **{new_total:,}**.",
            ephemeral=True,
        )
        # Piggyback on the same auto-dismiss timer used by /join acks so
        # ephemeral clutter clears the same way.
        self.cog._acks.setdefault(self.session_id, []).append(interaction)
        asyncio.create_task(self.cog._schedule_ack_dismiss(interaction))
        await self.cog._refresh_embed(self.session_id)


# =============================================================================
# Persistent buttons
# =============================================================================


class JoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sohoc:join:(?P<session_id>\d+)",
):
    def __init__(
        self, session_id: int, *,
        disabled: bool = False, lang: str = LANG_RU,
    ) -> None:
        self.session_id = session_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"sohoc:join:{session_id}",
                label=_t(lang, "btn_join"),
                emoji="✏️",
                style=discord.ButtonStyle.primary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "JoinButton":
        return cls(int(match["session_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Sohoc" = interaction.client.get_cog("Sohoc")  # type: ignore[assignment]
        if cog is None:
            return
        await interaction.response.send_modal(_NumberModal(cog, self.session_id))


class RollButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sohoc:roll:(?P<session_id>\d+)",
):
    def __init__(
        self, session_id: int, *,
        disabled: bool = False, lang: str = LANG_RU,
    ) -> None:
        self.session_id = session_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"sohoc:roll:{session_id}",
                label=_t(lang, "btn_roll"),
                emoji="🎲",
                style=discord.ButtonStyle.success,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "RollButton":
        return cls(int(match["session_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Sohoc" = interaction.client.get_cog("Sohoc")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_roll(interaction, self.session_id)


class EndButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sohoc:end:(?P<session_id>\d+)",
):
    """Admin/creator-only kill switch. Visible while waiting/rollable so a
    game stuck on a wrong-channel post or a typo can be retired without
    leaving players in limbo. Entry fees (if any) are refunded.
    """

    def __init__(
        self, session_id: int, *,
        disabled: bool = False, lang: str = LANG_RU,
    ) -> None:
        self.session_id = session_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"sohoc:end:{session_id}",
                label=_t(lang, "btn_end"),
                emoji="🛑",
                style=discord.ButtonStyle.danger,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "EndButton":
        return cls(int(match["session_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Sohoc" = interaction.client.get_cog("Sohoc")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_end(interaction, self.session_id)


class BetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sohoc:bet:(?P<session_id>\d+)",
):
    """'Bet' — opens the bet modal. User must already have joined
    (an entry with their number pick) before betting. Coins are escrowed
    on submit; the winner gets them back plus a bonus, losers forfeit.
    """

    def __init__(
        self, session_id: int, *,
        disabled: bool = False, lang: str = LANG_RU,
    ) -> None:
        self.session_id = session_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"sohoc:bet:{session_id}",
                label=_t(lang, "btn_bet"),
                emoji="💰",
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "BetButton":
        return cls(int(match["session_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Sohoc" = interaction.client.get_cog("Sohoc")  # type: ignore[assignment]
        if cog is None:
            return
        await interaction.response.send_modal(_BetModal(cog, self.session_id))


class ExplainButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sohoc:explain:(?P<session_id>\d+)",
):
    """'Explain' — shows an ephemeral embed describing how the bet
    reward is calculated. Always available (waiting/rollable/finished/
    cancelled) so people can review the rules after the fact."""

    def __init__(self, session_id: int, *, lang: str = LANG_RU) -> None:
        self.session_id = session_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"sohoc:explain:{session_id}",
                label=_t(lang, "btn_explain"),
                emoji="❓",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "ExplainButton":
        return cls(int(match["session_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Sohoc" = interaction.client.get_cog("Sohoc")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.send_explain(interaction, self.session_id)


class ThandeBoardLangButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sohoc:board_lang:(?P<guild_id>\d+)",
):
    """🌐 Toggle the Prophet leaderboard between RU and EN."""

    def __init__(self, guild_id: int, *, lang: str = LANG_RU) -> None:
        self.guild_id = guild_id
        label = "English" if lang == LANG_RU else "Русский"
        super().__init__(
            discord.ui.Button(
                custom_id=f"sohoc:board_lang:{guild_id}",
                label=label,
                emoji=LANG_BUTTON_EMOJI,
                style=discord.ButtonStyle.secondary,
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "ThandeBoardLangButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Sohoc" = interaction.client.get_cog("Sohoc")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_lang_toggle(interaction, self.guild_id)


class LangToggleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sohoc:lang:(?P<session_id>\d+)",
):
    """🌐 Flip the card between RU and EN."""

    def __init__(self, session_id: int, *, lang: str = LANG_RU) -> None:
        self.session_id = session_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"sohoc:lang:{session_id}",
                label=_SOHOC_LANG_BUTTON_LABEL.get(lang, "English"),
                emoji=LANG_BUTTON_EMOJI,
                style=discord.ButtonStyle.secondary,
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "LangToggleButton":
        return cls(int(match["session_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Sohoc" = interaction.client.get_cog("Sohoc")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_lang_toggle(interaction, self.session_id)


def _build_view(
    session: SohocSession,
) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    lang = _session_lang(session)
    if session.status == "waiting":
        v.add_item(JoinButton(session.id, disabled=False, lang=lang))
        v.add_item(BetButton(session.id, disabled=False, lang=lang))
        v.add_item(EndButton(session.id, disabled=False, lang=lang))
        v.add_item(ExplainButton(session.id, lang=lang))
    elif session.status == "rollable":
        v.add_item(JoinButton(session.id, disabled=True, lang=lang))
        v.add_item(BetButton(session.id, disabled=True, lang=lang))
        v.add_item(RollButton(session.id, disabled=False, lang=lang))
        v.add_item(EndButton(session.id, disabled=False, lang=lang))
        v.add_item(ExplainButton(session.id, lang=lang))
    else:  # finished or cancelled
        v.add_item(JoinButton(session.id, disabled=True, lang=lang))
        v.add_item(BetButton(session.id, disabled=True, lang=lang))
        v.add_item(RollButton(session.id, disabled=True, lang=lang))
        v.add_item(EndButton(session.id, disabled=True, lang=lang))
        v.add_item(ExplainButton(session.id, lang=lang))
    # 🌐 sits on row 1 so it stays visually below the primary buttons.
    v.add_item(LangToggleButton(session.id, lang=lang))
    return v


# =============================================================================
# Cog
# =============================================================================


_ACK_DISMISS_AFTER = 15  # seconds — auto-dismiss success acks this soon
# How long after end_at the bot waits for the owner to manually Roll
# before auto-rolling on their behalf.
_AUTO_ROLL_AFTER_SECONDS = 30 * 60


class Sohoc(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        # session_id -> end-timer task
        self._tasks: dict[int, asyncio.Task] = {}
        # session_id -> 30-min "owner didn't roll" auto-roll watchdog task
        self._auto_roll_tasks: dict[int, asyncio.Task] = {}
        # session_id -> list of join-ack interactions to auto-dismiss
        self._acks: dict[int, list[discord.Interaction]] = {}
        # Guard so on_ready only triggers recovery once even if the gateway
        # reconnects mid-session.
        self._recovered = False

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

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._recovered:
            return
        self._recovered = True
        await self._recover_sessions()

    async def _recover_sessions(self) -> None:
        """Rescue sohoc sessions whose end-timer task was lost on restart.

        End-timer + auto-roll state is in-memory only, so a redeploy
        would leave 'waiting' games stuck and 'rollable' games never
        auto-rolling. On startup we walk every session that isn't
        finished/cancelled and either:

          - waiting + past end_at → flip to 'rollable', refresh, then
            schedule auto-roll for whatever's left of the 30-min grace
          - waiting + future end_at → re-schedule the end-timer
          - rollable → schedule auto-roll based on time since end_at
            (immediate auto-roll if grace already expired)

        All paths are idempotent — the status check inside each timer
        means a duplicate firing no-ops.
        """
        log.info("sohoc: scanning for sessions needing recovery...")
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(SohocSession).where(
                    SohocSession.status.in_(["waiting", "rollable"])
                )
            )
            sessions = list(rows.scalars().all())

        now = datetime.now(timezone.utc)
        overdue = 0
        rescheduled = 0
        auto_scheduled = 0

        for sess in sessions:
            if sess.status == "rollable":
                # Time the owner had remaining for manual roll
                grace_used = (now - sess.end_at).total_seconds()
                grace_left = _AUTO_ROLL_AFTER_SECONDS - grace_used
                if sess.id not in self._auto_roll_tasks:
                    if grace_left <= 0:
                        # Past grace — fire immediately
                        self._schedule_auto_roll(sess.id, 1)
                    else:
                        self._schedule_auto_roll(sess.id, int(grace_left))
                    auto_scheduled += 1
                    log.info(
                        "sohoc: rollable session %s — auto-roll in %ds",
                        sess.id, max(1, int(grace_left)),
                    )
                continue

            # waiting branch
            remaining = (sess.end_at - now).total_seconds()
            if remaining <= 0:
                async with self.bot.db.session() as s2:
                    fresh = await s2.get(SohocSession, sess.id)
                    if fresh is None or fresh.status != "waiting":
                        continue
                    fresh.status = "rollable"
                    await s2.commit()
                await self._refresh_embed(sess.id)
                overdue += 1
                # Also schedule auto-roll on the same recovery window
                grace_used = -remaining  # how long past end_at
                grace_left = _AUTO_ROLL_AFTER_SECONDS - grace_used
                self._schedule_auto_roll(
                    sess.id, max(1, int(grace_left)),
                )
                log.info(
                    "sohoc: session %s was overdue → rollable; auto-roll in %ds",
                    sess.id, max(1, int(grace_left)),
                )
            else:
                if sess.id in self._tasks:
                    continue
                task = asyncio.create_task(
                    self._end_timer(sess.id, int(remaining))
                )
                self._tasks[sess.id] = task
                rescheduled += 1
                log.info(
                    "sohoc: rescheduled session %s with %ss remaining",
                    sess.id, int(remaining),
                )
        if overdue or rescheduled or auto_scheduled:
            log.info(
                "sohoc recovery done — overdue:%d rescheduled:%d auto-roll:%d",
                overdue, rescheduled, auto_scheduled,
            )

    async def _schedule_ack_dismiss(
        self,
        interaction: discord.Interaction,
        delay: int = _ACK_DISMISS_AFTER,
    ) -> None:
        """Delete an ephemeral ack after a short delay.

        Safe to fire-and-forget via asyncio.create_task — if the user has
        already dismissed it manually or the 15-min interaction token has
        expired, the delete call just no-ops.
        """
        try:
            await asyncio.sleep(delay)
            await interaction.delete_original_response()
        except (asyncio.CancelledError, discord.NotFound, discord.HTTPException):
            pass

    # User-facing /sohoc group: visible to everyone so creator-role
    # members can run /sohoc start. Discord can't gate subcommands
    # separately from their group, so admin commands live under a
    # sibling /sohoc-admin group (defined below) that's locked behind
    # Administrator perm and a runtime owner check.
    sohoc = app_commands.Group(
        name="sohoc",
        description="Number-guess minigame (00-99)",
        guild_only=True,
    )

    sohoc_admin = app_commands.Group(
        name="sohoc-admin",
        description="(Owner) Manage Sohoc: setup, hall of fame",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ---------- /sohoc-admin setup ----------

    @sohoc_admin.command(
        name="setup",
        description="(Owner) Configure /sohoc defaults",
    )
    @app_commands.choices(default_language=[
        app_commands.Choice(name="Russian (ru)", value=LANG_RU),
        app_commands.Choice(name="English (en)", value=LANG_EN),
    ])
    @app_commands.describe(
        channel="Default channel to post games in",
        announce_channel="Channel where winner announcements land",
        creator_role1="First role allowed to run /sohoc start",
        creator_role2="(optional)",
        creator_role3="(optional)",
        winner_role="Role auto-granted at 5+ wins",
        end_time="Daily cut-off time for picking numbers, 24h 'HH:MM' format",
        tz_offset="Timezone for end_time (e.g. 7 = VN, 2 = CEST, 0 = UTC, -5 = EST). Range -12..14",
        autostart="Auto-spawn a new game right after the current one rolls",
        winner_reward="Coin reward for the top-1 winner. Set 0 to disable.",
        consolation_reward="Coin reward for the consolation winner (second roll). Set 0 to disable.",
        default_language="Default display language for new games (ru = Russian, en = English)",
    )
    async def sohoc_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        announce_channel: discord.TextChannel | None = None,
        creator_role1: discord.Role | None = None,
        creator_role2: discord.Role | None = None,
        creator_role3: discord.Role | None = None,
        winner_role: discord.Role | None = None,
        end_time: app_commands.Range[str, 4, 5] | None = None,
        tz_offset: app_commands.Range[int, -12, 14] | None = None,
        autostart: bool | None = None,
        winner_reward: app_commands.Range[int, 0, 10_000_000] | None = None,
        consolation_reward: app_commands.Range[int, 0, 10_000_000] | None = None,
        default_language: app_commands.Choice[str] | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return

        # Validate end_time before touching the DB so we don't write garbage
        parsed_end: tuple[int, int] | None = None
        if end_time is not None:
            parsed_end = parse_hhmm(end_time)
            if parsed_end is None:
                await interaction.response.send_message(
                    "❌ `end_time` must use 24h `HH:MM` format (e.g. `21:00`, `08:30`).",
                    ephemeral=True,
                )
                return

        seen: set[int] = set()
        creators: list[discord.Role] = []
        for r in (creator_role1, creator_role2, creator_role3):
            if r and r.id not in seen:
                seen.add(r.id)
                creators.append(r)

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            if channel is not None:
                cfg.sohoc_channel_id = channel.id
            if announce_channel is not None:
                cfg.sohoc_announce_channel_id = announce_channel.id
            if creators:
                cfg.sohoc_creator_role_ids = ",".join(str(r.id) for r in creators)
            elif (
                creator_role1 is None
                and creator_role2 is None
                and creator_role3 is None
            ):
                # User passed no roles at all — leave existing value alone.
                pass
            if winner_role is not None:
                cfg.sohoc_winner_role_id = winner_role.id
            if parsed_end is not None:
                h, mn = parsed_end
                cfg.sohoc_end_time = f"{h:02d}:{mn:02d}"
            if tz_offset is not None:
                cfg.sohoc_tz_offset = int(tz_offset)
            if autostart is not None:
                cfg.sohoc_autostart = bool(autostart)
            if default_language is not None:
                cfg.sohoc_default_language = default_language.value
            await s.commit()
            # Reload to echo back the full effective state
            cfg = await s.get(GuildConfig, interaction.guild_id)

        # winner_reward writes to the shared MiniGameRewardConfig table
        # (game_type='sohoc', top1_reward) so the existing reward engine
        # picks it up on the next roll — no sohoc-specific reward code
        # path. If admin wants finer control (top2/top3, entry fee),
        # /economy setup-minigame still works.
        # winner_reward → top1_reward; consolation_reward → top2_reward.
        # A single upsert covers both if either was passed.
        if winner_reward is not None or consolation_reward is not None:
            async with self.bot.db.session() as s:
                mg_cfg = await s.scalar(
                    select(MiniGameRewardConfig).where(
                        MiniGameRewardConfig.guild_id == interaction.guild_id,
                        MiniGameRewardConfig.game_type == "sohoc",
                    )
                )
                if mg_cfg is None:
                    mg_cfg = MiniGameRewardConfig(
                        guild_id=interaction.guild_id,
                        game_type="sohoc",
                        top1_reward=int(winner_reward) if winner_reward is not None else 0,
                        top2_reward=(
                            int(consolation_reward)
                            if consolation_reward is not None else 0
                        ),
                        enabled=True,
                    )
                    s.add(mg_cfg)
                else:
                    if winner_reward is not None:
                        mg_cfg.top1_reward = int(winner_reward)
                    if consolation_reward is not None:
                        mg_cfg.top2_reward = int(consolation_reward)
                    mg_cfg.enabled = True
                await s.commit()

        # Read back the effective rewards for the echo embed (whether or
        # not this call set them) so admins can confirm at a glance.
        async with self.bot.db.session() as s:
            mg_cfg = await s.scalar(
                select(MiniGameRewardConfig).where(
                    MiniGameRewardConfig.guild_id == interaction.guild_id,
                    MiniGameRewardConfig.game_type == "sohoc",
                )
            )
        current_winner_reward = int(mg_cfg.top1_reward) if mg_cfg else 0
        current_consolation_reward = int(mg_cfg.top2_reward) if mg_cfg else 0

        emb = discord.Embed(
            title="🎲 /sohoc configuration",
            color=discord.Color.blurple(),
        )
        ch = f"<#{cfg.sohoc_channel_id}>" if cfg and cfg.sohoc_channel_id else "—"
        ach = f"<#{cfg.sohoc_announce_channel_id}>" if cfg and cfg.sohoc_announce_channel_id else "—"
        cr_ids = cfg.sohoc_creator_ids() if cfg else []
        cr_text = ", ".join(f"<@&{x}>" for x in cr_ids) if cr_ids else "_Manage Server only_"
        wr = f"<@&{cfg.sohoc_winner_role_id}>" if cfg and cfg.sohoc_winner_role_id else "—"
        offset = (
            cfg.sohoc_tz_offset if cfg and cfg.sohoc_tz_offset is not None
            else _DEFAULT_TZ_OFFSET
        )
        et = (
            f"`{cfg.sohoc_end_time}` ({format_tz_offset(offset)})"
            if cfg and cfg.sohoc_end_time else "—"
        )
        emb.add_field(name="Default channel", value=ch, inline=True)
        emb.add_field(name="Hall of Fame channel", value=ach, inline=True)
        emb.add_field(name="Creator roles", value=cr_text, inline=False)
        emb.add_field(name="Winner role (5+ wins)", value=wr, inline=True)
        emb.add_field(name="End time", value=et, inline=True)
        emb.add_field(name="Timezone", value=format_tz_offset(offset), inline=True)
        emb.add_field(
            name="Default language",
            value=(
                "🇷🇺 Russian" if (cfg and cfg.sohoc_default_language == LANG_RU)
                else "🇬🇧 English" if (cfg and cfg.sohoc_default_language == LANG_EN)
                else "🇷🇺 Russian (default)"
            ),
            inline=True,
        )
        emb.add_field(
            name="Autostart",
            value="✅ ON" if cfg and cfg.sohoc_autostart else "❌ OFF",
            inline=True,
        )
        emb.add_field(
            name="Grand prize reward",
            value=(
                f"**{current_winner_reward:,}** coins"
                if current_winner_reward > 0 else "_no reward_"
            ),
            inline=True,
        )
        emb.add_field(
            name="Consolation reward",
            value=(
                f"**{current_consolation_reward:,}** coins"
                if current_consolation_reward > 0 else "_no reward_"
            ),
            inline=True,
        )
        await interaction.response.send_message(
            embed=emb,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /sohoc start ----------

    @sohoc.command(
        name="start",
        description="Start a new number-guess game (uses the channel + end_time from /sohoc-admin setup)",
    )
    async def sohoc_start(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        if not isinstance(interaction.user, discord.Member):
            return

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)

        # Permission: must hold a creator role OR have Manage Server
        creator_ids = set(cfg.sohoc_creator_ids()) if cfg else set()
        is_admin = interaction.user.guild_permissions.manage_guild
        if creator_ids:
            member_role_ids = {r.id for r in interaction.user.roles}
            if not (member_role_ids & creator_ids) and not is_admin:
                await interaction.response.send_message(
                    "❌ You don't have a role allowed to start /sohoc.",
                    ephemeral=True,
                )
                return
        elif not is_admin:
            await interaction.response.send_message(
                "❌ Admin has not configured a creator role via `/sohoc-admin setup`.",
                ephemeral=True,
            )
            return

        # Channel + end_time both come from the config — /sohoc start no longer accepts params.
        if cfg is None or cfg.sohoc_channel_id is None:
            await interaction.response.send_message(
                "❌ No channel configured. Admin must run `/sohoc-admin setup channel:#...` first.",
                ephemeral=True,
            )
            return
        target_channel = (
            interaction.guild.get_channel(cfg.sohoc_channel_id)
            if interaction.guild
            else None
        )
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Default channel is no longer valid. Run `/sohoc-admin setup` again.",
                ephemeral=True,
            )
            return

        if not cfg.sohoc_end_time:
            await interaction.response.send_message(
                "❌ No `end_time` set. Admin must run `/sohoc-admin setup end_time:HH:MM` first.",
                ephemeral=True,
            )
            return
        parsed = parse_hhmm(cfg.sohoc_end_time)
        if parsed is None:
            await interaction.response.send_message(
                f"❌ `end_time` is stored in an invalid format: `{cfg.sohoc_end_time}`. "
                "Admin must re-run `/sohoc-admin setup end_time:HH:MM`.",
                ephemeral=True,
            )
            return
        h, mn = parsed
        tz_offset = (
            cfg.sohoc_tz_offset if cfg.sohoc_tz_offset is not None
            else _DEFAULT_TZ_OFFSET
        )
        # next_end_at already rolls over to tomorrow when today's HH:MM is past.
        end_at = next_end_at(h, mn, tz_offset)
        actual_seconds = max(1, int((end_at - datetime.now(timezone.utc)).total_seconds()))
        default_lang = cfg.sohoc_default_language or LANG_RU if cfg else LANG_RU
        async with self.bot.db.session() as s:
            session = SohocSession(
                guild_id=interaction.guild_id,
                channel_id=target_channel.id,
                creator_id=interaction.user.id,
                end_at=end_at,
                status="waiting",
                language=default_lang,
            )
            s.add(session)
            await s.commit()
            await s.refresh(session)
            session_id = session.id

        # Load fresh with empty entries list for the embed
        async with self.bot.db.session() as s:
            fresh = await s.scalar(
                select(SohocSession)
                .where(SohocSession.id == session_id)
                .options(selectinload(SohocSession.entries))
            )
        assert fresh is not None
        view = _build_view(fresh)
        embed = _build_game_embed(fresh, interaction.user.mention)

        try:
            msg = await target_channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot doesn't have permission to post in that channel.", ephemeral=True
            )
            return

        async with self.bot.db.session() as s:
            sess = await s.get(SohocSession, session_id)
            if sess is not None:
                sess.message_id = msg.id
                await s.commit()

        # End-timer task
        task = asyncio.create_task(self._end_timer(session_id, actual_seconds))
        self._tasks[session_id] = task

        if target_channel.id == interaction.channel_id:
            await interaction.response.send_message(
                f"✅ Created number-guess game #{session_id} ↑", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ Created number-guess game #{session_id} in {target_channel.mention}",
                ephemeral=True,
            )
        # Self-dismiss this confirmation after a short delay so it doesn't
        # linger in the creator's chat for the full game duration.
        asyncio.create_task(self._schedule_ack_dismiss(interaction))

    # ---------- helpers ----------

    async def _spawn_next_game(
        self,
        guild_id: int,
        creator_id: int,
    ) -> int | None:
        """Create a fresh SohocSession + embed + end-timer. Shared by
        `/sohoc start` and the autostart chain. Returns the new session
        id on success, None if config is missing / channel gone / bot
        forbidden to post. Failures log a warning — the caller decides
        how to surface it (the interaction-driven path can DM the
        creator, the autostart path just logs and stops the chain).

        creator_id becomes the new session's creator — for autostart
        that's the same person who started the ORIGINAL game, so the
        Roll button keeps working for them.
        """
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
        if cfg is None or cfg.sohoc_channel_id is None:
            log.warning("sohoc autostart: guild %s has no sohoc_channel_id", guild_id)
            return None
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        target_channel = guild.get_channel(cfg.sohoc_channel_id)
        if not isinstance(target_channel, discord.TextChannel):
            log.warning(
                "sohoc autostart: sohoc_channel_id %s invalid in guild %s",
                cfg.sohoc_channel_id, guild_id,
            )
            return None
        if not cfg.sohoc_end_time:
            log.warning("sohoc autostart: guild %s has no sohoc_end_time", guild_id)
            return None
        parsed = parse_hhmm(cfg.sohoc_end_time)
        if parsed is None:
            log.warning(
                "sohoc autostart: bad sohoc_end_time %r in guild %s",
                cfg.sohoc_end_time, guild_id,
            )
            return None
        h, mn = parsed
        tz_offset = (
            cfg.sohoc_tz_offset if cfg.sohoc_tz_offset is not None
            else _DEFAULT_TZ_OFFSET
        )
        end_at = next_end_at(h, mn, tz_offset)
        actual_seconds = max(
            1, int((end_at - datetime.now(timezone.utc)).total_seconds())
        )
        default_lang = (cfg.sohoc_default_language or LANG_RU) if cfg else LANG_RU
        async with self.bot.db.session() as s:
            session = SohocSession(
                guild_id=guild_id,
                channel_id=target_channel.id,
                creator_id=creator_id,
                end_at=end_at,
                status="waiting",
                language=default_lang,
            )
            s.add(session)
            await s.commit()
            await s.refresh(session)
            session_id = session.id

        async with self.bot.db.session() as s:
            fresh = await s.scalar(
                select(SohocSession)
                .where(SohocSession.id == session_id)
                .options(selectinload(SohocSession.entries))
            )
        assert fresh is not None
        creator_member = guild.get_member(creator_id)
        creator_mention = (
            creator_member.mention if creator_member else f"<@{creator_id}>"
        )
        view = _build_view(fresh)
        embed = _build_game_embed(fresh, creator_mention)
        try:
            msg = await target_channel.send(embed=embed, view=view)
        except discord.Forbidden:
            log.warning(
                "sohoc autostart: forbidden to post in channel %s",
                target_channel.id,
            )
            # Clean up the orphan session row.
            async with self.bot.db.session() as s:
                orphan = await s.get(SohocSession, session_id)
                if orphan is not None:
                    orphan.status = "cancelled"
                    await s.commit()
            return None
        async with self.bot.db.session() as s:
            sess = await s.get(SohocSession, session_id)
            if sess is not None:
                sess.message_id = msg.id
                await s.commit()
        task = asyncio.create_task(self._end_timer(session_id, actual_seconds))
        self._tasks[session_id] = task
        return session_id

    async def _process_bet_payouts(
        self,
        session_id: int,
        guild_id: int,
        winner_top1_id: int | None,
        winner_top2_id: int | None,
    ) -> tuple[dict[int, int], int, int]:
        """Settle SohocEntry.bet_amount for both winner tiers.

        Formulas:
          top-1 winner: wallet += bet + (bet // 1000) * top1_reward
          top-2 winner: wallet += bet + (bet // 1000) * top2_reward
        Losers: bet was already debited on placement — nothing to do.

        Returns (bonus_map, top1_reward, top2_reward). bonus_map only
        contains entries for winners that actually placed a bet; the
        caller merges these into the rewards_map for the embed.

        Bet amounts are refetched from the DB (not passed in) so a bet
        committed in the last millisecond of the 'waiting' window still
        counts if its write landed before we read here.

        Idempotency: only invoked from the roll-completion path, itself
        gated by session.status transition, so double-execution is not
        possible.
        """
        # Read both prize tiers — needed for the bonus formulas and for
        # the 'Explain' embed's live display.
        async with self.bot.db.session() as s:
            mg_cfg = await s.scalar(
                select(MiniGameRewardConfig).where(
                    MiniGameRewardConfig.guild_id == guild_id,
                    MiniGameRewardConfig.game_type == "sohoc",
                )
            )
        top1_reward = int(mg_cfg.top1_reward) if mg_cfg else 0
        top2_reward = int(mg_cfg.top2_reward) if mg_cfg else 0

        bonus_map: dict[int, int] = {}
        from ..economy.service import WalletService
        wallet_svc = WalletService(self.bot)

        async def _payout(
            winner_id: int, reward_pool: int, tier_tag: str,
        ) -> None:
            async with self.bot.db.session() as s:
                w_entry = await s.scalar(
                    select(SohocEntry).where(
                        SohocEntry.session_id == session_id,
                        SohocEntry.user_id == winner_id,
                    )
                )
            w_bet = int(w_entry.bet_amount or 0) if w_entry else 0
            if w_bet <= 0:
                return
            bonus = (w_bet // _BET_UNIT) * reward_pool
            payout = w_bet + bonus
            if payout <= 0:
                return
            try:
                await wallet_svc.adjust(
                    guild_id, winner_id, payout,
                    "minigame_sohoc_bet_payout",
                    description=(
                        f"Sohoc session #{session_id} bet payout "
                        f"({tier_tag}: refund {w_bet:,} + bonus {bonus:,})"
                    ),
                    metadata={
                        "session_id": session_id,
                        "tier": tier_tag,
                        "bet_amount": w_bet,
                        "bonus": bonus,
                        "reward_pool": reward_pool,
                    },
                )
            except Exception:
                log.exception(
                    "sohoc: bet payout failed for user %s tier %s session %s",
                    winner_id, tier_tag, session_id,
                )
                return
            # Same user can hit both tiers only if they picked one
            # number that happens to match one of the two rolls — since
            # each user only picks a single number, that's impossible.
            # We still += (rather than =) as a defensive net.
            bonus_map[winner_id] = bonus_map.get(winner_id, 0) + payout

        if winner_top1_id is not None:
            await _payout(winner_top1_id, top1_reward, "top1")
        if winner_top2_id is not None:
            await _payout(winner_top2_id, top2_reward, "top2")
        return bonus_map, top1_reward, top2_reward

    async def _maybe_autostart_next(
        self, guild_id: int, previous_creator_id: int,
    ) -> None:
        """Called from both roll paths after a game finishes. Spawns
        the next game when `sohoc_autostart` is enabled on this
        guild's config. Silent no-op otherwise."""
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
            if cfg is None or not cfg.sohoc_autostart:
                return
            existing = await s.scalar(
                select(SohocSession).where(
                    SohocSession.guild_id == guild_id,
                    SohocSession.status.in_(["waiting", "rollable"]),
                )
            )
        if existing is not None:
            log.info(
                "sohoc autostart: guild %s already has active session %s — skipping",
                guild_id, existing.id,
            )
            return
        new_id = await self._spawn_next_game(guild_id, previous_creator_id)
        if new_id is not None:
            log.info(
                "sohoc autostart: guild %s spawned session %s (creator %s)",
                guild_id, new_id, previous_creator_id,
            )

    async def _fetch_session(self, session_id: int) -> SohocSession | None:
        async with self.bot.db.session() as s:
            return await s.scalar(
                select(SohocSession)
                .where(SohocSession.id == session_id)
                .options(selectinload(SohocSession.entries))
            )

    async def _refresh_embed(
        self,
        session_id: int,
        *,
        rewards_map: dict[int, int] | None = None,
        coin_icon: str = "🪙",
    ) -> None:
        session = await self._fetch_session(session_id)
        if session is None or session.message_id is None:
            return
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(session.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await channel.fetch_message(session.message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        creator = guild.get_member(session.creator_id)
        creator_mention = creator.mention if creator else f"<@{session.creator_id}>"
        embed = _build_game_embed(
            session, creator_mention,
            rewards_map=rewards_map, coin_icon=coin_icon,
        )
        view = _build_view(session)
        try:
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            log.warning("sohoc refresh failed: %s", e)

    async def _dismiss_acks(self, session_id: int) -> None:
        for ack in self._acks.pop(session_id, []):
            try:
                await ack.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass

    async def _end_timer(self, session_id: int, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
            async with self.bot.db.session() as s:
                session = await s.get(SohocSession, session_id)
                if session is None or session.status != "waiting":
                    return
                session.status = "rollable"
                await s.commit()
            await self._dismiss_acks(session_id)
            await self._refresh_embed(session_id)
            # Owner has 30 min from end_at to Roll manually. Past that we
            # auto-roll so the game doesn't sit stuck forever.
            self._schedule_auto_roll(session_id, _AUTO_ROLL_AFTER_SECONDS)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("sohoc end timer crashed for %s", session_id)
        finally:
            self._tasks.pop(session_id, None)

    def _schedule_auto_roll(self, session_id: int, seconds: int) -> None:
        """Spawn the watchdog task (or replace any prior one). Idempotent
        on the in-memory dict — restart-safe via _recover_sessions."""
        old = self._auto_roll_tasks.get(session_id)
        if old is not None and not old.done():
            old.cancel()
        self._auto_roll_tasks[session_id] = asyncio.create_task(
            self._auto_roll_timer(session_id, max(1, int(seconds)))
        )

    async def _auto_roll_timer(self, session_id: int, seconds: int) -> None:
        """After the configured grace window, perform the roll on the
        owner's behalf if the session is still in 'rollable' status."""
        try:
            await asyncio.sleep(seconds)
            async with self.bot.db.session() as s:
                session = await s.get(SohocSession, session_id)
                if session is None or session.status != "rollable":
                    return  # owner already rolled, or game cancelled
            log.info(
                "sohoc: auto-rolling session %s (owner didn't roll in %ds)",
                session_id, seconds,
            )
            await self._perform_auto_roll(session_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("sohoc auto-roll task crashed for %s", session_id)
        finally:
            self._auto_roll_tasks.pop(session_id, None)

    async def _perform_auto_roll(self, session_id: int) -> None:
        """Server-side roll without an interaction. Mirrors handle_roll
        except there's no ephemeral ack to send and no per-call admin
        check — getting here means the 30-min grace expired."""
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(SohocSession)
                .where(SohocSession.id == session_id)
                .options(selectinload(SohocSession.entries))
            )
            if session is None or session.status != "rollable":
                return
            # Two DIFFERENT numbers so consolation isn't a duplicate of
            # the grand prize.
            rolled, rolled_2 = random.sample(range(100), 2)
            entries = list(session.entries)
            winner_entry, winner_entry_2 = _find_two_winners(
                entries, rolled, rolled_2,
            )
            session.status = "finished"
            session.rolled_number = rolled
            session.rolled_number_2 = rolled_2
            session.winner_id = winner_entry.user_id if winner_entry else None
            session.winner_2_id = (
                winner_entry_2.user_id if winner_entry_2 else None
            )
            session.finished_at = datetime.now(timezone.utc)
            await s.commit()
            # Log lifetime wins for both tiers so the 5-wins-for-role
            # rule counts consolation winners too.
            for w in (winner_entry, winner_entry_2):
                if w is not None:
                    s.add(SohocWin(
                        guild_id=session.guild_id,
                        user_id=w.user_id,
                        session_id=session_id,
                    ))
            if winner_entry is not None or winner_entry_2 is not None:
                await s.commit()

        # Thande board refresh
        if winner_entry is not None or winner_entry_2 is not None:
            try:
                await self._refresh_thande_board(session.guild_id)
            except Exception:
                log.exception("sohoc auto-roll: thande board refresh failed")

        # Coin reward distribution: top1 = grand prize, top2 = consolation.
        rewards_map: dict[int, int] = {}
        coin_icon = "🪙"
        try:
            from ..economy.service import MiniGameResult, RewardEngine
            from ..db.models import GuildEconomyConfig as _ECfg
            top1 = [winner_entry.user_id] if winner_entry is not None else []
            top2 = (
                [winner_entry_2.user_id] if winner_entry_2 is not None else []
            )
            participants = sorted(
                {e.user_id for e in entries} - set(top1) - set(top2)
            )
            async with self.bot.db.session() as s:
                ecfg = await s.get(_ECfg, session.guild_id)
            if ecfg and ecfg.coin_icon:
                coin_icon = ecfg.coin_icon
            engine = RewardEngine(self.bot)
            granted = await engine.grant_minigame_rewards(
                MiniGameResult(
                    guild_id=session.guild_id,
                    game_type="sohoc",
                    session_id=session_id,
                    top1=top1,
                    top2=top2,
                    participants=participants,
                )
            )
            rewards_map = {g.user_id: g.amount for g in granted}
        except Exception:
            log.exception("sohoc auto-roll: grant_minigame_rewards failed")

        # Bet payouts for BOTH winners — bonus multiplier is top1_reward
        # for grand-prize winner, top2_reward for consolation winner.
        try:
            bonus_map, _, _ = await self._process_bet_payouts(
                session_id, session.guild_id,
                winner_entry.user_id if winner_entry else None,
                winner_entry_2.user_id if winner_entry_2 else None,
            )
            for uid, amount in bonus_map.items():
                rewards_map[uid] = rewards_map.get(uid, 0) + amount
        except Exception:
            log.exception("sohoc auto-roll: bet payout failed")

        await self._dismiss_acks(session_id)
        await self._refresh_embed(
            session_id, rewards_map=rewards_map, coin_icon=coin_icon,
        )
        await self._post_announcement(
            session_id, rewards_map=rewards_map, coin_icon=coin_icon,
        )
        for w in (winner_entry, winner_entry_2):
            if w is not None:
                await self._maybe_grant_winner_role_for_guild(
                    session.guild_id, w.user_id,
                )
        # Autostart hook — spawns the next session using this session's
        # creator as the new creator, so the Roll button in the fresh
        # game belongs to the same person.
        await self._maybe_autostart_next(session.guild_id, session.creator_id)

    async def _maybe_grant_winner_role_for_guild(
        self, guild_id: int, winner_id: int,
    ) -> None:
        """Non-interaction variant of _maybe_grant_winner_role for the
        auto-roll path. Same logic, no Interaction needed."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
            if cfg is None or cfg.sohoc_winner_role_id is None:
                return
            total = await s.scalar(
                select(func.count(SohocWin.id)).where(
                    SohocWin.guild_id == guild_id,
                    SohocWin.user_id == winner_id,
                )
            )
        if (total or 0) < _WINS_FOR_ROLE:
            return
        role = guild.get_role(cfg.sohoc_winner_role_id)
        if role is None:
            return
        member = guild.get_member(winner_id)
        if member is None or role in member.roles:
            return
        me = guild.me
        if me is None or role >= me.top_role:
            log.warning("sohoc: cannot grant %s (above bot role)", role.id)
            return
        try:
            await member.add_roles(
                role, reason="Sohoc winner 5+ wins (auto-roll)",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("sohoc: failed to grant winner role: %s", e)

    async def handle_end(
        self, interaction: discord.Interaction, session_id: int
    ) -> None:
        """Cancel a sohoc game that hasn't been rolled yet.

        Allowed for: session creator OR anyone with Manage Server.
        Refunds entry fees by reading CoinTransaction rows whose metadata
        carries this session_id — so the refund amount matches what was
        actually charged (in case the configured entry_fee changed since).
        """
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(SohocSession)
                .where(SohocSession.id == session_id)
                .options(selectinload(SohocSession.entries))
            )
            if session is None:
                await interaction.response.send_message(
                    "This game no longer exists.", ephemeral=True
                )
                return
            is_creator = interaction.user.id == session.creator_id
            is_admin = (
                isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.manage_guild
            )
            if not (is_creator or is_admin):
                await interaction.response.send_message(
                    "❌ Only the creator (or an admin) can end the game.",
                    ephemeral=True,
                )
                return
            if session.status not in ("waiting", "rollable"):
                await interaction.response.send_message(
                    f"Game is in status **{session.status}** — cannot be cancelled.",
                    ephemeral=True,
                )
                return
            session.status = "cancelled"
            session.finished_at = datetime.now(timezone.utc)
            guild_id = session.guild_id
            entry_user_ids = [e.user_id for e in session.entries]
            await s.commit()

        # Cancel any pending end-timer task so it doesn't flip status back.
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        # Cancel pending auto-roll watchdog too.
        ar_task = self._auto_roll_tasks.pop(session_id, None)
        if ar_task is not None and not ar_task.done():
            ar_task.cancel()

        # Refund: entry_fee AND placed bets both go back. Both are
        # matched by metadata.session_id on their original debit tx so
        # the refund equals what was actually charged (config drift is
        # irrelevant). Bets and fees are aggregated per user into one
        # refund tx so the wallet history stays readable.
        refunded_users = 0
        refunded_total = 0
        if entry_user_ids:
            from ..db.models import CoinTransaction
            from ..economy.service import WalletService
            import json as _json
            async with self.bot.db.session() as s:
                rows = await s.execute(
                    select(CoinTransaction).where(
                        CoinTransaction.guild_id == guild_id,
                        CoinTransaction.reason.in_((
                            "minigame_sohoc_entry_fee",
                            "minigame_sohoc_bet",
                        )),
                    )
                )
                debit_txs = list(rows.scalars().all())
            to_refund: dict[int, int] = {}
            for t in debit_txs:
                if not t.metadata_json:
                    continue
                try:
                    meta = _json.loads(t.metadata_json)
                except Exception:
                    continue
                if meta.get("session_id") != session_id:
                    continue
                to_refund[t.user_id] = to_refund.get(t.user_id, 0) + abs(t.delta)

            wallet_svc = WalletService(self.bot)
            for uid, amount in to_refund.items():
                if amount <= 0:
                    continue
                try:
                    await wallet_svc.adjust(
                        guild_id, uid, amount,
                        "minigame_sohoc_refund",
                        description=f"Refund fees + bets for cancelled sohoc game #{session_id}",
                        actor_id=interaction.user.id,
                        metadata={"session_id": session_id},
                    )
                    refunded_users += 1
                    refunded_total += amount
                except Exception:
                    log.exception(
                        "sohoc cancel: refund failed for user %s session %s",
                        uid, session_id,
                    )

        await self._dismiss_acks(session_id)
        await self._refresh_embed(session_id)

        actor_tag = (
            "creator" if interaction.user.id == session.creator_id else "admin"
        )
        msg_parts = [f"🛑 Cancelled number-guess game #{session_id} ({actor_tag})."]
        if entry_user_ids:
            if refunded_users:
                msg_parts.append(
                    f"💰 Refunded **{refunded_total:,}** coins to "
                    f"{refunded_users} player(s)."
                )
            else:
                msg_parts.append(
                    f"_(No entry fees to refund for "
                    f"{len(entry_user_ids)} player(s).)_"
                )
        await interaction.response.send_message(
            "\n".join(msg_parts), ephemeral=True
        )

    async def handle_roll(
        self, interaction: discord.Interaction, session_id: int
    ) -> None:
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(SohocSession)
                .where(SohocSession.id == session_id)
                .options(selectinload(SohocSession.entries))
            )
            if session is None:
                await interaction.response.send_message(
                    "This game no longer exists.", ephemeral=True
                )
                return
            if interaction.user.id != session.creator_id and not (
                isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.manage_guild
            ):
                await interaction.response.send_message(
                    "❌ Only the creator (or an admin) can Roll.", ephemeral=True
                )
                return
            if session.status != "rollable":
                await interaction.response.send_message(
                    f"Game is in status **{session.status}** — cannot Roll yet.",
                    ephemeral=True,
                )
                return

            # Two DIFFERENT numbers so consolation isn't a duplicate of
            # the grand prize.
            rolled, rolled_2 = random.sample(range(100), 2)
            entries = list(session.entries)
            winner_entry, winner_entry_2 = _find_two_winners(
                entries, rolled, rolled_2,
            )

            session.status = "finished"
            session.rolled_number = rolled
            session.rolled_number_2 = rolled_2
            session.winner_id = winner_entry.user_id if winner_entry else None
            session.winner_2_id = (
                winner_entry_2.user_id if winner_entry_2 else None
            )
            session.finished_at = datetime.now(timezone.utc)
            await s.commit()

            # Cancel the auto-roll watchdog — manual roll won the race.
            ar_task = self._auto_roll_tasks.pop(session_id, None)
            if ar_task is not None and not ar_task.done():
                ar_task.cancel()

            # Log lifetime wins for both tiers so the 5-wins-for-role
            # rule counts consolation winners too.
            for w in (winner_entry, winner_entry_2):
                if w is not None:
                    s.add(SohocWin(
                        guild_id=session.guild_id,
                        user_id=w.user_id,
                        session_id=session_id,
                    ))
            if winner_entry is not None or winner_entry_2 is not None:
                await s.commit()

        # Refresh Than De (Prophet) leaderboard if configured (win count changed).
        if winner_entry is not None or winner_entry_2 is not None:
            try:
                await self._refresh_thande_board(session.guild_id)
            except Exception:
                log.exception("sohoc: thande board refresh failed")

        # Acknowledge the click immediately so we don't blow the 3s budget
        # while we go off and update the public message + announce channel.
        await interaction.response.send_message(
            f"🎲 Rolled: grand prize `{rolled:02d}`, "
            f"consolation `{rolled_2:02d}`",
            ephemeral=True,
        )

        # Coin reward distribution: top1 = grand prize, top2 = consolation.
        rewards_map: dict[int, int] = {}
        coin_icon = "🪙"
        try:
            from ..economy.service import MiniGameResult, RewardEngine
            from ..db.models import GuildEconomyConfig as _ECfg
            top1 = [winner_entry.user_id] if winner_entry is not None else []
            top2 = (
                [winner_entry_2.user_id] if winner_entry_2 is not None else []
            )
            participants = sorted(
                {e.user_id for e in entries} - set(top1) - set(top2)
            )
            async with self.bot.db.session() as s:
                ecfg = await s.get(_ECfg, session.guild_id)
            if ecfg and ecfg.coin_icon:
                coin_icon = ecfg.coin_icon
            engine = RewardEngine(self.bot)
            granted = await engine.grant_minigame_rewards(
                MiniGameResult(
                    guild_id=session.guild_id,
                    game_type="sohoc",
                    session_id=session_id,
                    top1=top1,
                    top2=top2,
                    participants=participants,
                )
            )
            rewards_map = {g.user_id: g.amount for g in granted}
        except Exception:
            log.exception("sohoc: grant_minigame_rewards failed")

        # Bet payouts — bonus multiplier is top1_reward for grand-prize
        # winner, top2_reward for consolation winner.
        try:
            bonus_map, _, _ = await self._process_bet_payouts(
                session_id, session.guild_id,
                winner_entry.user_id if winner_entry else None,
                winner_entry_2.user_id if winner_entry_2 else None,
            )
            for uid, amount in bonus_map.items():
                rewards_map[uid] = rewards_map.get(uid, 0) + amount
        except Exception:
            log.exception("sohoc: bet payout failed")

        await self._dismiss_acks(session_id)
        await self._refresh_embed(
            session_id, rewards_map=rewards_map, coin_icon=coin_icon
        )
        await self._post_announcement(
            session_id, rewards_map=rewards_map, coin_icon=coin_icon
        )

        for w in (winner_entry, winner_entry_2):
            if w is not None:
                await self._maybe_grant_winner_role(interaction, w.user_id)
        # Autostart hook — same as _perform_auto_roll. The manual-Roll
        # path fires this after all announcements land so the fresh
        # game's channel post doesn't race the vinh-danh embed.
        await self._maybe_autostart_next(session.guild_id, session.creator_id)

    async def _post_announcement(
        self,
        session_id: int,
        *,
        rewards_map: dict[int, int] | None = None,
        coin_icon: str = "🪙",
    ) -> None:
        rewards_map = rewards_map or {}
        session = await self._fetch_session(session_id)
        if session is None:
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, session.guild_id)
        if cfg is None or cfg.sohoc_announce_channel_id is None:
            return
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(cfg.sohoc_announce_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        rolled_2 = getattr(session, "rolled_number_2", None)
        winner_2_id = getattr(session, "winner_2_id", None)

        async def _lifetime_wins(user_id: int) -> int:
            async with self.bot.db.session() as s:
                total = await s.scalar(
                    select(func.count(SohocWin.id)).where(
                        SohocWin.guild_id == session.guild_id,
                        SohocWin.user_id == user_id,
                    )
                )
            return int(total or 0)

        def _prize_line(user_id: int) -> str:
            prize = rewards_map.get(user_id, 0)
            return (
                f"\n🎁 Prize: `{prize:,}` {coin_icon}"
                if prize > 0 else ""
            )

        if session.winner_id is None and winner_2_id is None:
            desc_lines = [
                f"Grand prize number: `{session.rolled_number:02d}`",
            ]
            if rolled_2 is not None:
                desc_lines.append(
                    f"Consolation number: `{rolled_2:02d}`"
                )
            desc_lines.append("No one guessed correctly. See you next round!")
            embed = discord.Embed(
                title="🎲 Guess Number — No winner",
                description="\n".join(desc_lines),
                color=discord.Color.dark_gray(),
            )
        else:
            lines: list[str] = []
            if session.winner_id is not None:
                total_1 = await _lifetime_wins(session.winner_id)
                lines.append(
                    f"🏆 **Grand prize:** <@{session.winner_id}> — "
                    f"got `{session.rolled_number:02d}` "
                    f"(lifetime wins: **{total_1}**)"
                    f"{_prize_line(session.winner_id)}"
                )
            else:
                lines.append(
                    f"🏆 Grand prize: number `{session.rolled_number:02d}` — "
                    "no one guessed correctly."
                )
            if rolled_2 is not None:
                if winner_2_id is not None:
                    total_2 = await _lifetime_wins(winner_2_id)
                    lines.append(
                        f"🎉 **Consolation:** <@{winner_2_id}> — "
                        f"got `{rolled_2:02d}` "
                        f"(lifetime wins: **{total_2}**)"
                        f"{_prize_line(winner_2_id)}"
                    )
                else:
                    lines.append(
                        f"🎁 Consolation: number `{rolled_2:02d}` — "
                        "no one guessed correctly."
                    )
            embed = discord.Embed(
                title="🏆 Hall of Fame — Guess Number",
                description="\n".join(lines),
                color=discord.Color.gold(),
            )
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            log.warning("sohoc: forbidden to announce in %s", channel.id)

    async def send_explain(
        self, interaction: discord.Interaction, session_id: int,
    ) -> None:
        """Ephemeral 'Explain' embed. Shows the live top1_reward and
        a worked example so players know exactly what their bet buys.
        Safe from any status — no DB writes."""
        guild_id = interaction.guild_id
        top1_reward = 0
        top2_reward = 0
        coin_name = "Coin"
        coin_icon = ""
        if guild_id is not None:
            async with self.bot.db.session() as s:
                mg_cfg = await s.scalar(
                    select(MiniGameRewardConfig).where(
                        MiniGameRewardConfig.guild_id == guild_id,
                        MiniGameRewardConfig.game_type == "sohoc",
                    )
                )
                from ..db.models import GuildEconomyConfig
                econ_cfg = await s.get(GuildEconomyConfig, guild_id)
            top1_reward = int(mg_cfg.top1_reward) if mg_cfg else 0
            top2_reward = int(mg_cfg.top2_reward) if mg_cfg else 0
            if econ_cfg is not None:
                coin_name = econ_cfg.coin_name
                coin_icon = econ_cfg.coin_icon or ""

        coin_display = f"{coin_icon}{coin_name}".strip()

        embed = discord.Embed(
            title="❓ How Bet rewards are calculated",
            color=discord.Color.blurple(),
        )
        embed.description = (
            f"🏆 **Grand prize:** `{top1_reward:,}` {coin_display}\n"
            f"🎉 **Consolation:** `{top2_reward:,}` {coin_display}\n"
            f"**Bet unit:** every `{_BET_UNIT:,}` coins bet → "
            f"**+1×** the reward of the tier you win.\n\n"
            "**General formula when you guess right:**\n"
            "```\n"
            f"bonus = (bet_coins // {_BET_UNIT}) × tier_reward\n"
            "total received = tier_reward + bet_coins (refund) + bonus\n"
            "```"
        )
        embed.add_field(
            name="🏆 Grand prize hit",
            value=(
                f"• Get your **bet coins refunded**.\n"
                f"• Plus **grand prize reward** (`{top1_reward:,}` {coin_display}).\n"
                f"• Plus **bonus** = `(bet // {_BET_UNIT}) × {top1_reward:,}`."
            ),
            inline=False,
        )
        embed.add_field(
            name="🎉 Consolation hit",
            value=(
                f"• Get your **bet coins refunded**.\n"
                f"• Plus **consolation reward** "
                f"(`{top2_reward:,}` {coin_display}).\n"
                f"• Plus **bonus** = `(bet // {_BET_UNIT}) × {top2_reward:,}`."
            ),
            inline=False,
        )
        embed.add_field(
            name="🔴 If both numbers miss",
            value=(
                "All **bet coins are lost** (they were deducted from your "
                "wallet the moment you pressed Bet)."
            ),
            inline=False,
        )
        # A concrete example so users don't have to do the mental math.
        if top1_reward > 0 or top2_reward > 0:
            example_bet = max(_BET_UNIT, max(top1_reward, top2_reward))
            example_bonus_top1 = (example_bet // _BET_UNIT) * top1_reward
            example_total_top1 = top1_reward + example_bet + example_bonus_top1
            example_bonus_top2 = (example_bet // _BET_UNIT) * top2_reward
            example_total_top2 = top2_reward + example_bet + example_bonus_top2
            embed.add_field(
                name="🧮 Example",
                value=(
                    f"Bet `{example_bet:,}` {coin_display}:\n"
                    f"• Grand prize → **`{example_total_top1:,}` "
                    f"{coin_display}** (reward {top1_reward:,} + "
                    f"refund {example_bet:,} + "
                    f"bonus {example_bonus_top1:,})\n"
                    f"• Consolation → **`{example_total_top2:,}` "
                    f"{coin_display}** (reward {top2_reward:,} + "
                    f"refund {example_bet:,} + "
                    f"bonus {example_bonus_top2:,})"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="⚠️ Note",
                value=(
                    "This server has not configured `winner_reward` / "
                    "`consolation_reward` (both = 0), so bonus = 0. "
                    "Bets right now only refund the exact coins you bet "
                    "if you win, and are lost if you lose. Ask the owner "
                    "to run `/sohoc-admin setup winner_reward:... "
                    "consolation_reward:...` to unlock the bonus."
                ),
                inline=False,
            )
        embed.set_footer(
            text=f"Session #{session_id} • You can bet multiple times, amounts stack."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def handle_lang_toggle(
        self, interaction: discord.Interaction, session_id: int,
    ) -> None:
        """Persist the flipped language on the SohocSession row and
        redraw the card. Anyone can toggle — language is a display
        concern that doesn't affect the game outcome."""
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            session = await s.get(SohocSession, session_id)
            if session is None:
                await interaction.followup.send(
                    "Game không còn tồn tại. / Game no longer exists.",
                    ephemeral=True,
                )
                return
            new_lang = _sohoc_next_lang(session.language)
            session.language = new_lang
            await s.commit()
        await self._refresh_embed(session_id)
        ack_map = {
            LANG_RU: "🌐 Переключено на **Русский**.",
            LANG_EN: "🌐 Switched to **English**.",
            LANG_VI: "🌐 Chuyển sang **Tiếng Việt**.",
        }
        await interaction.followup.send(
            ack_map.get(new_lang, "🌐"), ephemeral=True
        )

    async def handle_board_lang_toggle(
        self, interaction: discord.Interaction, guild_id: int,
    ) -> None:
        await interaction.response.defer()
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "sohoc",
                )
            )
            if board is None:
                await interaction.followup.send("❌ Board not found.", ephemeral=True)
                return
            new_lang = LANG_EN if getattr(board, "language", LANG_RU) == LANG_RU else LANG_RU
            board.language = new_lang
            display_count = board.display_count
            await s.commit()
        embed = await self._build_thande_embed(guild_id, display_count, lang=new_lang)
        view = await self._build_thande_view(guild_id, lang=new_lang)
        await interaction.edit_original_response(
            embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _maybe_grant_winner_role(
        self, interaction: discord.Interaction, winner_id: int
    ) -> None:
        if interaction.guild is None:
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild.id)
            if cfg is None or cfg.sohoc_winner_role_id is None:
                return
            total = await s.scalar(
                select(func.count(SohocWin.id)).where(
                    SohocWin.guild_id == interaction.guild.id,
                    SohocWin.user_id == winner_id,
                )
            )
        if (total or 0) < _WINS_FOR_ROLE:
            return
        role = interaction.guild.get_role(cfg.sohoc_winner_role_id)
        if role is None:
            return
        member = interaction.guild.get_member(winner_id)
        if member is None or role in member.roles:
            return
        me = interaction.guild.me
        if me is None or role >= me.top_role:
            log.warning(
                "sohoc: cannot grant %s (above bot role) to %s", role.id, winner_id
            )
            return
        try:
            await member.add_roles(role, reason="Sohoc winner 5+ wins")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("sohoc: failed to grant winner role: %s", e)


    # =========================================================================
    # /sohoc thande — Than De (Prophet) leaderboard (top wins for number-guess)
    # =========================================================================

    thande = app_commands.Group(
        name="thande",
        description="Prophet leaderboard (top wins in the number-guess game)",
        parent=sohoc_admin,
    )

    _THANDE_I18N: dict[str, dict[str, str]] = {
        LANG_RU: {
            "title":      "🔮  ТАБЛИЦА ПРОРОКОВ  🔮",
            "no_winners": "_Ещё никто не угадал число._\n**Участвуй и стань первым!**",
            "win_unit":   "побед",
            "footer":     "{guild} • Топ {n} • Обновляется после каждого раунда",
        },
        LANG_EN: {
            "title":      "🔮  PROPHET LEADERBOARD  🔮",
            "no_winners": "_No one has won a number-guess game yet._\n**Join a round and be the first!**",
            "win_unit":   "wins",
            "footer":     "{guild} • Top {n} • Auto-updates after each round",
        },
    }

    async def _build_thande_embed(
        self, guild_id: int, display_count: int, lang: str = LANG_RU,
    ) -> discord.Embed:
        strings = self._THANDE_I18N.get(lang, self._THANDE_I18N[LANG_RU])
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(
                    SohocWin.user_id, func.count(SohocWin.id).label("wins")
                )
                .where(SohocWin.guild_id == guild_id)
                .group_by(SohocWin.user_id)
                .order_by(func.count(SohocWin.id).desc(), SohocWin.user_id)
                .limit(display_count)
            )
            results = list(rows.all())

        guild = self.bot.get_guild(guild_id)
        guild_name = guild.name if guild else "Server"

        embed = discord.Embed(
            title=strings["title"],
            color=discord.Color.from_rgb(231, 76, 60),
        )
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        if not results:
            embed.description = strings["no_winners"]
        else:
            lines: list[str] = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
            for rank, (uid, wins) in enumerate(results, start=1):
                if rank == 1:
                    medal = "🥇"
                elif rank == 2:
                    medal = "🥈"
                elif rank == 3:
                    medal = "🥉"
                else:
                    medal = f"`#{rank:>2}`"
                lines.append(
                    f"{medal}  <@{uid}>  •  🎯 `{wins:,}` {strings['win_unit']}"
                )
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            embed.description = "\n".join(lines)

        embed.set_footer(
            text=strings["footer"].format(guild=guild_name, n=display_count)
        )
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    async def _build_play_view(
        self, guild_id: int,
    ) -> discord.ui.View:
        """One-button View linking to the configured /sohoc game channel
        so a user reading the leaderboard can jump straight there to
        join the next round. Empty view (no button) when the channel
        isn't configured or no longer exists — leaves the embed alone
        rather than rendering a broken-looking solo button."""
        view = discord.ui.View(timeout=None)
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
        ch_id = cfg.sohoc_channel_id if cfg else None
        if ch_id is None:
            return view
        guild = self.bot.get_guild(guild_id)
        ch = guild.get_channel(ch_id) if guild else None
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return view
        view.add_item(discord.ui.Button(
            label="Play now",
            emoji="🎮",
            style=discord.ButtonStyle.link,
            url=ch.jump_url,
        ))
        return view

    async def _build_thande_view(
        self, guild_id: int, lang: str = LANG_RU,
    ) -> discord.ui.View:
        view = await self._build_play_view(guild_id)
        view.add_item(ThandeBoardLangButton(guild_id, lang=lang))
        return view

    async def _refresh_thande_board(self, guild_id: int) -> None:
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "sohoc",
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
        lang = getattr(board, "language", LANG_RU)
        embed = await self._build_thande_embed(guild_id, board.display_count, lang=lang)
        view = await self._build_thande_view(guild_id, lang=lang)
        msg = None
        if board.message_id is not None:
            try:
                msg = await channel.fetch_message(board.message_id)
            except (discord.NotFound, discord.Forbidden):
                msg = None
        if msg is not None:
            try:
                await msg.edit(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as e:
                log.warning("thande: edit failed for %s: %s", guild_id, e)
            return
        try:
            new_msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning("thande: forbidden to repost in %s", guild_id)
            return
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "sohoc",
                )
            )
            if row is not None:
                row.message_id = new_msg.id
                await s.commit()

    @thande.command(
        name="post",
        description="(Admin) Create / move the Prophet leaderboard in a channel",
    )
    @app_commands.describe(
        channel="Channel to post the leaderboard in (any text channel)",
        display_count="How many users to show (1-25, default 10)",
    )
    async def thande_post(
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
                    GameWinBoard.game_type == "sohoc",
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

        embed = await self._build_thande_embed(
            interaction.guild_id, int(display_count), lang=LANG_RU,
        )
        view = await self._build_thande_view(interaction.guild_id, lang=LANG_RU)
        try:
            msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot doesn't have permission to post in {channel.mention}.", ephemeral=True
            )
            return

        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "sohoc",
                )
            )
            if board is None:
                board = GameWinBoard(
                    guild_id=interaction.guild_id,
                    game_type="sohoc",
                    channel_id=channel.id,
                    message_id=msg.id,
                    display_count=int(display_count),
                    language=LANG_RU,
                )
                s.add(board)
            else:
                board.channel_id = channel.id
                board.message_id = msg.id
                board.display_count = int(display_count)
                board.language = LANG_RU
            await s.commit()

        await interaction.followup.send(
            f"✅ Prophet leaderboard posted in {channel.mention} (top {display_count}). "
            "It will auto-update after every winning roll.",
            ephemeral=True,
        )

    @thande.command(
        name="remove",
        description="(Admin) Delete the Prophet leaderboard + stop auto-update",
    )
    async def thande_remove(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "sohoc",
                )
            )
            if board is None:
                await interaction.followup.send(
                    "This server has no Prophet leaderboard.", ephemeral=True
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
            "✅ Prophet leaderboard deleted + auto-update stopped.", ephemeral=True
        )

    @thande.command(
        name="refresh",
        description="(Admin) Refresh the Prophet leaderboard immediately",
    )
    async def thande_refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "sohoc",
                )
            )
        if board is None:
            await interaction.followup.send(
                "This server has no Prophet leaderboard. Run `/sohoc-admin thande post` first.",
                ephemeral=True,
            )
            return
        await self._refresh_thande_board(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Prophet leaderboard refreshed in <#{board.channel_id}>.",
            ephemeral=True,
        )


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(Sohoc(bot))
