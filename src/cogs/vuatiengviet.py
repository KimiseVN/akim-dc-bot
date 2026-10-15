"""Vua Tiếng Việt — Vietnamese word-scramble minigame.

Setup tier (guild owner only)
-----------------------------
- /vuatiengviet setup       — channels + creator roles + winner role
- /vuatiengviet word add    — push a word + meaning into the dictionary
- /vuatiengviet word remove — drop by id, or 'all' to wipe the bank
- /vuatiengviet word list   — paginated dictionary listing
- /vuatiengviet bxh post|remove|refresh — top winners leaderboard

Player tier
-----------
- /vuatiengviet start ...   — anyone with a configured creator role
- "Answer" modal            — any player during the answering window

Game flow
---------
Creator runs /vuatiengviet start with N questions + per-question
seconds + optional pause-between + top1/2/3 prizes. The bot picks N
random words from the dictionary, posts a single embed in the configured
channel with [Start Game] / [End] for the creator. On Start:

  per question loop {
    1. write started_at, render the question embed with the letters
       randomly shuffled (joined by /) and a live <t:deadline:R>
       countdown, attach an "Answer" button
    2. wait for the deadline (or an early end-event)
    3. reveal: pick the earliest-correct answer as winner, render the
       reveal embed (answer + meaning + per-user response list)
    4. pause: either auto-sleep `pause_seconds` (with creator's
       "Next question" able to advance early) or wait indefinitely for
       the creator's Next click (when pause_seconds is empty)
  }

After all questions: rank by (correct_count desc, total_elapsed asc),
pay top1/2/3 via WalletService, write a VuaTiengVietWin row for top1,
refresh the bxh leaderboard, post a hall-of-fame card to the configured
announce channel, and edit the game embed into a final scoreboard.

Answering UX
------------
Each player clicks "Answer" — that opens a modal with one text
field. click_at is captured when the button is clicked, submitted_at
when the modal is submitted; elapsed_ms = submitted_at − click_at is
the tiebreaker for "many correct, fastest wins". Submissions made
after the question's deadline are stored with accepted=False (shown
in the audit list with ⏰ but not eligible).

Comparison is NFC + casefold so capitalization and Unicode form don't
matter — "Trăng Tròn", "trăng tròn", and "trang tron" cf "Trăng tròn"
all behave correctly (the last DOES NOT match since accents differ).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..db.models import (
    GameWinBoard,
    GuildConfig,
    GuildEconomyConfig,
    VietWord,
    VuaTiengVietAnswer,
    VuaTiengVietSession,
    VuaTiengVietSessionQuestion,
    VuaTiengVietWin,
)
from ..economy.service import WalletService
from ..i18n import LANG_BUTTON_EMOJI, LANG_EN, LANG_RU

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.vuatiengviet")


# Auto-dismiss for the per-click acks ("answer recorded") so the
# player's chat doesn't accumulate clutter through a long game.
_ACK_DISMISS_SECONDS = 8


# =============================================================================
# Helpers
# =============================================================================


def normalize_answer(s: str) -> str:
    """NFC + casefold so 'Trăng Tròn' == 'trăng tròn' for matching."""
    return unicodedata.normalize("NFC", (s or "").strip().casefold())


def scramble_word(word: str) -> str:
    """Render the word's letters in random order, joined by '/', dropping
    spaces. Retries a few times to avoid producing the original."""
    chars = [c for c in word if not c.isspace()]
    if len(chars) <= 1:
        return word
    original_compact = "".join(chars)
    for _ in range(8):
        random.shuffle(chars)
        if "".join(chars) != original_compact:
            break
    return " / ".join(chars)


def _format_elapsed(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.2f}s"


# =============================================================================
# Bulk-import parsers
# =============================================================================


_MAX_WORD_LEN = 128
_MAX_MEANING_LEN = 1000


def _validate_entry(
    word: str, meaning: str, location: str,
) -> tuple[dict | None, str | None]:
    """Trim + length-check one (word, meaning) pair. Returns (entry, error)
    — exactly one is non-None."""
    w = (word or "").strip()
    m = (meaning or "").strip()
    if not w or not m:
        return None, f"{location}: word or meaning empty → skipped"
    if len(w) > _MAX_WORD_LEN:
        return None, f"{location}: word > {_MAX_WORD_LEN} chars → skipped"
    if len(m) > _MAX_MEANING_LEN:
        return None, f"{location}: meaning > {_MAX_MEANING_LEN} chars → skipped"
    return {"word": w, "meaning": m}, None


def parse_bulk_text(text: str) -> tuple[list[dict], list[str]]:
    """Bulk-paste format: one entry per line, 'word | meaning'.
    '|' is the canonical separator; lines without it are reported.
    """
    valid: list[dict] = []
    errors: list[str] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue  # allow comments
        if "|" not in line:
            errors.append(
                f"Line {i}: missing `|` (e.g. `Trăng tròn | Mặt trăng…`) → skipped"
            )
            continue
        word, _, meaning = line.partition("|")
        entry, err = _validate_entry(word, meaning, f"Line {i}")
        if entry is not None:
            valid.append(entry)
        else:
            errors.append(err)
    return valid, errors


async def parse_json_attachment(
    attachment: discord.Attachment,
) -> tuple[list[dict], list[str]]:
    """JSON layout: a top-level array of objects with 'word' and 'meaning'.
    Tolerant of extra keys."""
    try:
        raw = await attachment.read()
    except Exception as e:
        return [], [f"Could not read file: {e}"]
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return [], [f"Invalid JSON: {e}"]
    if not isinstance(parsed, list):
        return [], ["JSON must be an array of `{word, meaning}` objects"]
    valid: list[dict] = []
    errors: list[str] = []
    for i, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            errors.append(f"Item #{i}: not an object → skipped")
            continue
        entry, err = _validate_entry(
            str(item.get("word", "")), str(item.get("meaning", "")),
            f"Item #{i}",
        )
        if entry is not None:
            valid.append(entry)
        else:
            errors.append(err)
    return valid, errors


async def parse_excel_attachment(
    attachment: discord.Attachment,
) -> tuple[list[dict], list[str]]:
    """Excel layout: column A = word, column B = meaning.
    Header row (if the first cell is 'word' / 'từ') is auto-skipped.
    Blank rows are silently ignored."""
    try:
        import openpyxl  # local import — file path may not always import this
    except ImportError:
        return [], [
            "The server has not installed `openpyxl`. Add `openpyxl==3.1.5` to "
            "requirements.txt and redeploy."
        ]
    try:
        raw = await attachment.read()
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        return [], [f"Could not read Excel: {e}"]
    valid: list[dict] = []
    errors: list[str] = []
    skipped_header = False
    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if not row or row[0] is None and (len(row) < 2 or row[1] is None):
            continue
        word_cell = row[0]
        meaning_cell = row[1] if len(row) > 1 else None
        # Auto-skip header if first row looks like one
        if i == 1 and not skipped_header:
            first = str(word_cell or "").strip().lower()
            if first in ("word", "từ", "tu", "ingame_word"):
                skipped_header = True
                continue
        entry, err = _validate_entry(
            str(word_cell or ""), str(meaning_cell or ""),
            f"Row {i}",
        )
        if entry is not None:
            valid.append(entry)
        else:
            # Excel often has trailing blank rows — only complain if at
            # least one cell had content.
            if (word_cell or "") or (meaning_cell or ""):
                errors.append(err)
    return valid, errors


_BULK_TEMPLATE = (
    "Trăng tròn | Mặt trăng tròn vành vạnh vào đêm rằm\n"
    "Mây trời | Những đám mây trôi trên bầu trời\n"
    "Cánh đồng | Vùng đất rộng trồng lúa hoặc hoa màu\n"
    "# lines starting with # are comments and are skipped"
)


class BulkAddModal(discord.ui.Modal):
    """Modal for the owner to paste multiple (word, meaning) lines at
    once. Max 4000 chars (Discord limit) — for larger batches use
    /vuatiengviet-admin word import with a file."""

    def __init__(self, cog: "VuaTiengViet") -> None:
        super().__init__(title="Bulk-add words — Vua Tieng Viet")
        self.cog = cog
        self.payload = discord.ui.TextInput(
            label="One per line: word | meaning",
            placeholder=(
                "Trăng tròn | Mặt trăng tròn vành vạnh\n"
                "Mây trời | Những đám mây trôi trên trời\n..."
            ),
            style=discord.TextStyle.paragraph,
            min_length=5,
            max_length=4000,
            required=True,
        )
        self.add_item(self.payload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_bulk_add(interaction, str(self.payload.value))


# =============================================================================
# Answer modal
# =============================================================================


class AnswerModal(discord.ui.Modal):
    """One-field modal carrying the original click_at so the elapsed
    timer measures button click → submit, not bot-receive timestamps."""

    def __init__(
        self, cog: "VuaTiengViet",
        session_id: int, sq_id: int,
        click_at: datetime,
    ) -> None:
        super().__init__(title="Vua Tieng Viet — Answer")
        self.cog = cog
        self.session_id = session_id
        self.sq_id = sq_id
        self.click_at = click_at
        self.answer = discord.ui.TextInput(
            label="Answer (case-insensitive)",
            placeholder="Type your answer and submit...",
            min_length=1,
            max_length=128,
            required=True,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_answer_submit(
            interaction, self.session_id, self.sq_id,
            self.click_at, str(self.answer.value),
        )


# =============================================================================
# Persistent buttons
# =============================================================================


class StartButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vtv:start:(?P<session_id>\d+)",
):
    def __init__(self, session_id: int, *, disabled: bool = False) -> None:
        self.session_id = session_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"vtv:start:{session_id}",
                label="Start Game",
                emoji="▶️",
                style=discord.ButtonStyle.success,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["session_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "VuaTiengViet" = interaction.client.get_cog("VuaTiengViet")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_start(interaction, self.session_id)


class EndButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vtv:end:(?P<session_id>\d+)",
):
    def __init__(self, session_id: int, *, disabled: bool = False) -> None:
        self.session_id = session_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"vtv:end:{session_id}",
                label="End",
                emoji="🛑",
                style=discord.ButtonStyle.danger,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["session_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "VuaTiengViet" = interaction.client.get_cog("VuaTiengViet")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_end(interaction, self.session_id)


class NextButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vtv:next:(?P<session_id>\d+)",
):
    def __init__(self, session_id: int, *, disabled: bool = False) -> None:
        self.session_id = session_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"vtv:next:{session_id}",
                label="Next question",
                emoji="⏭",
                style=discord.ButtonStyle.primary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["session_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "VuaTiengViet" = interaction.client.get_cog("VuaTiengViet")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_next(interaction, self.session_id)


class AnswerButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vtv:answer:(?P<session_id>\d+):(?P<sq_id>\d+)",
):
    """The 'Answer' button. Per-question custom_id so a stale
    button click on the previous question can't pollute the new one."""

    def __init__(
        self, session_id: int, sq_id: int, *, disabled: bool = False,
    ) -> None:
        self.session_id = session_id
        self.sq_id = sq_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"vtv:answer:{session_id}:{sq_id}",
                label="Answer",
                emoji="✍️",
                style=discord.ButtonStyle.primary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["session_id"]), int(match["sq_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # Capture click_at IMMEDIATELY so per-user elapsed measures
        # from this point — not from the modal send roundtrip.
        click_at = datetime.now(timezone.utc)
        cog: "VuaTiengViet" = interaction.client.get_cog("VuaTiengViet")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_answer_click(
            interaction, self.session_id, self.sq_id, click_at,
        )


# =============================================================================
# View builders
# =============================================================================


def _waiting_view(session_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(StartButton(session_id))
    v.add_item(EndButton(session_id))
    return v


def _question_view(session_id: int, sq_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(AnswerButton(session_id, sq_id))
    v.add_item(EndButton(session_id))
    return v


def _reveal_view(
    session_id: int, sq_id: int, *, has_next: bool, manual: bool,
) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    # Answer button kept but disabled so the reveal embed visually keeps
    # the button column instead of jumping around.
    v.add_item(AnswerButton(session_id, sq_id, disabled=True))
    if has_next and manual:
        v.add_item(NextButton(session_id))
    if has_next:
        v.add_item(EndButton(session_id))
    return v


# =============================================================================
# Embed builders
# =============================================================================


def _waiting_embed(
    session: VuaTiengVietSession, coin_name: str, coin_icon: str,
) -> discord.Embed:
    pause_txt = (
        f"**{session.pause_seconds}s** (auto)"
        if session.pause_seconds is not None
        else "_creator advances manually_"
    )
    embed = discord.Embed(
        title="🇻🇳 Vua Tieng Viet — Get Ready",
        description=(
            f"**{session.num_questions}** questions • "
            f"**{session.seconds_per_question}s** each • "
            f"Advance: {pause_txt}\n"
            f"Creator: <@{session.creator_id}>\n\n"
            "Click **Start Game** to begin. Players will see scrambled "
            "letters and click **Answer** to type their guess."
        ),
        color=discord.Color.from_rgb(231, 76, 60),
    )
    prize_lines = []
    if session.top1_reward > 0:
        prize_lines.append(f"🥇 Top 1: **{session.top1_reward:,}** {coin_icon}{coin_name}")
    if session.top2_reward > 0:
        prize_lines.append(f"🥈 Top 2: **{session.top2_reward:,}** {coin_icon}{coin_name}")
    if session.top3_reward > 0:
        prize_lines.append(f"🥉 Top 3: **{session.top3_reward:,}** {coin_icon}{coin_name}")
    if prize_lines:
        embed.add_field(name="🏆 Prizes", value="\n".join(prize_lines), inline=False)
    else:
        embed.add_field(name="🏆 Prizes", value="_No prize pool_", inline=False)
    return embed


def _question_embed(
    session: VuaTiengVietSession,
    sq: VuaTiengVietSessionQuestion,
    deadline: datetime,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🇻🇳 Vua Tieng Viet — Question {sq.order_index + 1}/{session.num_questions}",
        color=discord.Color.from_rgb(52, 152, 219),
    )
    ts = int(deadline.timestamp())
    embed.description = (
        f"🎲 **Scrambled letters:**\n# `{sq.scrambled_text}`\n\n"
        f"⏳ **Deadline:** <t:{ts}:R>\n"
        "Click **✍️ Answer** below to type your answer in the dialog. "
        "The earliest correct answer wins this question."
    )
    embed.set_footer(
        text=f"{session.seconds_per_question}s/question • Timer starts when you click Answer"
    )
    return embed


def _reveal_embed(
    session: VuaTiengVietSession,
    sq: VuaTiengVietSessionQuestion,
    answers: list[VuaTiengVietAnswer],
    *,
    has_next: bool,
    next_deadline: datetime | None,
    manual: bool,
) -> discord.Embed:
    color = (
        discord.Color.green() if sq.winner_id is not None
        else discord.Color.dark_gray()
    )
    embed = discord.Embed(
        title=(
            f"🇻🇳 Vua Tieng Viet — Question {sq.order_index + 1}"
            f"/{session.num_questions} • ✅ Answer"
        ),
        color=color,
    )
    description = [
        f"🎯 **Correct answer:** `{sq.word.word}`",
        f"📖 **Meaning:** {sq.word.meaning}",
    ]
    if sq.winner_id is not None:
        winner_ans = next(
            (a for a in answers if a.user_id == sq.winner_id), None
        )
        elapsed = (
            _format_elapsed(winner_ans.elapsed_ms) if winner_ans else "—"
        )
        description.append(
            f"\n🏆 **Earliest correct:** <@{sq.winner_id}> ({elapsed})"
        )
    else:
        description.append("\n😢 _No one answered correctly._")
    embed.description = "\n".join(description)

    if answers:
        lines = []
        # Sort: correct first, then by elapsed asc
        sorted_ans = sorted(
            answers,
            key=lambda a: (
                0 if (a.is_correct and a.accepted) else 1, a.elapsed_ms,
            ),
        )
        for a in sorted_ans[:20]:
            if not a.accepted:
                tag = "⏰"
                detail = "late"
            elif a.is_correct:
                tag = "✅"
                detail = _format_elapsed(a.elapsed_ms)
            else:
                tag = "❌"
                detail = _format_elapsed(a.elapsed_ms)
            ans_display = a.answer_text[:40] + (
                "…" if len(a.answer_text) > 40 else ""
            )
            lines.append(f"{tag} <@{a.user_id}> — “{ans_display}” — {detail}")
        if len(answers) > 20:
            lines.append(f"_(+{len(answers) - 20} more players)_")
        embed.add_field(
            name=f"📝 Answers ({len(answers)})",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="📝 Answers (0)",
            value="_No one answered this question._",
            inline=False,
        )

    if has_next and next_deadline is not None:
        ts = int(next_deadline.timestamp())
        embed.set_footer(text=f"Next question: auto-advance <t:{ts}:R>")
    elif has_next and manual:
        embed.set_footer(text="Creator, click 'Next question' to advance")
    elif not has_next:
        embed.set_footer(text="Last question — scoreboard coming up")
    return embed


def _scoreboard_embed(
    session: VuaTiengVietSession,
    scores: list[tuple[int, int, int]],  # (user_id, correct_count, total_elapsed_ms)
    coin_name: str, coin_icon: str,
    payouts: dict[int, int],  # user_id -> coin paid
) -> discord.Embed:
    embed = discord.Embed(
        title="🏁 Vua Tieng Viet — Result",
        color=discord.Color.gold(),
    )
    if not scores:
        embed.description = "_No one participated._"
        return embed

    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for rank, (uid, correct, total_ms) in enumerate(scores[:20], start=1):
        if rank == 1:
            medal = "🥇"
        elif rank == 2:
            medal = "🥈"
        elif rank == 3:
            medal = "🥉"
        else:
            medal = f"`#{rank:>2}`"
        avg = (total_ms / correct) if correct > 0 else 0
        avg_txt = f"avg {_format_elapsed(int(avg))}" if correct > 0 else "—"
        prize = payouts.get(uid, 0)
        prize_txt = (
            f"  •  🎁 **+{prize:,}** {coin_icon}"
            if prize > 0 else ""
        )
        lines.append(
            f"{medal}  <@{uid}> — **{correct}**/{session.num_questions} correct "
            f"({avg_txt}){prize_txt}"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    embed.description = "\n".join(lines)

    total_paid = sum(payouts.values())
    if total_paid > 0:
        embed.set_footer(
            text=f"Total prize pool: {total_paid:,} {coin_name} (credited to wallets)"
        )
    return embed


# =============================================================================
# Cog
# =============================================================================


class BxhBoardLangButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vtv:board_lang:(?P<guild_id>\d+)",
):
    """🌐 Toggle the VTV leaderboard between RU and EN."""

    def __init__(self, guild_id: int, *, lang: str = LANG_RU) -> None:
        self.guild_id = guild_id
        label = "English" if lang == LANG_RU else "Русский"
        super().__init__(
            discord.ui.Button(
                custom_id=f"vtv:board_lang:{guild_id}",
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
    ) -> "BxhBoardLangButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "VuaTiengViet" = interaction.client.get_cog("VuaTiengViet")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_lang_toggle(interaction, self.guild_id)


class VuaTiengViet(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        # session_id -> running game-loop task
        self._tasks: dict[int, asyncio.Task] = {}
        # session_id -> Event signaling "advance to next question now"
        self._next_events: dict[int, asyncio.Event] = {}
        # session_id -> Event signaling "stop the whole session"
        self._end_events: dict[int, asyncio.Event] = {}
        # (session_id, sq_id) -> list of pending answer-ack interactions
        # to auto-dismiss when the question reveals (keeps chat clean).
        self._answer_acks: dict[
            tuple[int, int], list[discord.Interaction]
        ] = {}

    # ---- /vuatiengviet group (user-facing) ----
    vtv = app_commands.Group(
        name="vuatiengviet",
        description="Vua Tieng Viet — Vietnamese word-scramble minigame",
        guild_only=True,
    )

    # ---- /vuatiengviet-admin group (owner-only) ----
    # Discord can't gate subcommands separately from their group, so
    # admin commands live in a sibling group under Administrator perm
    # + runtime _require_owner. Per-command default_permissions on
    # subcommands was a no-op and gets dropped.
    vtv_admin = app_commands.Group(
        name="vuatiengviet-admin",
        description="(Owner) Manage Vua Tieng Viet: setup, word bank, hall of fame",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ---------- Owner gate ----------

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        )

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        """Reject non-owner with ephemeral. Returns True if ok."""
        if self._is_owner(interaction):
            return True
        # Caller may or may not have deferred; followup is safe either way.
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(
            "❌ Only the **server owner** can use this command.",
            ephemeral=True,
        )
        return False

    # =========================================================================
    # /vuatiengviet setup
    # =========================================================================

    @vtv_admin.command(
        name="setup",
        description="(Owner) Configure default channel, creator roles, winner role",
    )
    @app_commands.describe(
        channel="Default channel to post games",
        announce_channel="Hall-of-fame channel — posts each game's winner",
        creator_role1="Role allowed to run /vuatiengviet start (leave empty = owner only)",
        creator_role2="(optional)",
        creator_role3="(optional)",
        winner_role="Role auto-granted to any top-1 who hits the win threshold",
        wins_for_role="Number of top-1 wins to earn winner_role (1-100)",
    )
    async def vtv_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        announce_channel: discord.TextChannel | None = None,
        creator_role1: discord.Role | None = None,
        creator_role2: discord.Role | None = None,
        creator_role3: discord.Role | None = None,
        winner_role: discord.Role | None = None,
        wins_for_role: app_commands.Range[int, 1, 100] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            if channel is not None:
                cfg.vtv_channel_id = channel.id
            if announce_channel is not None:
                cfg.vtv_announce_channel_id = announce_channel.id
            if winner_role is not None:
                cfg.vtv_winner_role_id = winner_role.id
            if wins_for_role is not None:
                cfg.vtv_wins_for_role = int(wins_for_role)
            roles = [r for r in (creator_role1, creator_role2, creator_role3) if r]
            if roles:
                seen: set[int] = set()
                kept: list[str] = []
                for r in roles:
                    if r.id in seen:
                        continue
                    seen.add(r.id)
                    kept.append(str(r.id))
                cfg.vtv_creator_role_ids = ",".join(kept)
            await s.commit()
            cfg = await s.get(GuildConfig, interaction.guild_id)

        assert cfg is not None
        ch = f"<#{cfg.vtv_channel_id}>" if cfg.vtv_channel_id else "—"
        ach = f"<#{cfg.vtv_announce_channel_id}>" if cfg.vtv_announce_channel_id else "—"
        crs = cfg.vtv_creator_ids()
        cr_text = ", ".join(f"<@&{x}>" for x in crs) if crs else "_owner only_"
        wr = f"<@&{cfg.vtv_winner_role_id}>" if cfg.vtv_winner_role_id else "—"
        wfr = str(cfg.vtv_wins_for_role) if cfg.vtv_wins_for_role else "—"
        embed = discord.Embed(
            title="🇻🇳 /vuatiengviet configuration",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Default channel", value=ch, inline=True)
        embed.add_field(name="Hall-of-fame channel", value=ach, inline=True)
        embed.add_field(name="Creator roles", value=cr_text, inline=False)
        embed.add_field(name="Winner role", value=wr, inline=True)
        embed.add_field(name="Top-1 wins to earn role", value=wfr, inline=True)
        await interaction.followup.send(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # =========================================================================
    # /vuatiengviet word add | remove | list
    # =========================================================================

    word = app_commands.Group(
        name="word",
        description="Manage the word/phrase database",
        parent=vtv_admin,
    )

    @word.command(
        name="add",
        description="(Owner) Add one word/phrase + its meaning to the database",
    )
    @app_commands.describe(
        word="Word or phrase (e.g. Trăng tròn)",
        meaning="Meaning / definition of the word",
    )
    async def word_add(
        self,
        interaction: discord.Interaction,
        word: app_commands.Range[str, 1, 128],
        meaning: app_commands.Range[str, 1, 1000],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            row = VietWord(
                guild_id=interaction.guild_id,
                word=word.strip(),
                meaning=meaning.strip(),
                added_by=interaction.user.id,
            )
            s.add(row)
            try:
                await s.commit()
                await s.refresh(row)
                msg = f"✅ Added word `#{row.id}` — **{row.word}**"
            except Exception:
                await s.rollback()
                msg = f"❌ Word **{word}** already exists in the database."
        await interaction.followup.send(msg, ephemeral=True)

    @word.command(
        name="remove",
        description="(Owner) Remove one word by ID, or type 'all' to wipe the bank",
    )
    @app_commands.describe(target="Word ID (e.g. 12) — or type `all` to wipe everything")
    async def word_remove(
        self, interaction: discord.Interaction, target: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        normalized = target.strip().lower()
        if normalized == "all":
            async with self.bot.db.session() as s:
                total = await s.scalar(
                    select(func.count(VietWord.id)).where(
                        VietWord.guild_id == interaction.guild_id
                    )
                )
                if not total:
                    await interaction.followup.send(
                        "Database is empty — nothing to delete.",
                        ephemeral=True,
                    )
                    return
                result = await s.execute(
                    delete(VietWord).where(
                        VietWord.guild_id == interaction.guild_id
                    )
                )
                await s.commit()
            await interaction.followup.send(
                f"🧹 Wiped **all {result.rowcount or total} words** from the database.",
                ephemeral=True,
            )
            return
        try:
            wid = int(normalized)
        except ValueError:
            await interaction.followup.send(
                "❌ `target` must be a word ID or the literal `all`.", ephemeral=True
            )
            return
        async with self.bot.db.session() as s:
            result = await s.execute(
                delete(VietWord).where(
                    VietWord.id == wid,
                    VietWord.guild_id == interaction.guild_id,
                )
            )
            await s.commit()
        if result.rowcount:
            await interaction.followup.send(
                f"🗑 Deleted word `#{wid}`.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Word `#{wid}` not found in the database.",
                ephemeral=True,
            )

    @word.command(
        name="list",
        description="(Owner) List the words in the database",
    )
    async def word_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(VietWord).where(
                    VietWord.guild_id == interaction.guild_id
                ).order_by(VietWord.id)
            )
            words = list(rows.scalars().all())
        if not words:
            await interaction.followup.send(
                "Database is empty. Use `/vuatiengviet-admin word add` to add a word.",
                ephemeral=True,
            )
            return
        chunks: list[str] = []
        current = ""
        for w in words:
            piece = (
                f"`#{w.id}` **{w.word}** — _{w.meaning[:80]}_\n"
            )
            if len(current) + len(piece) > 3800:
                chunks.append(current)
                current = piece
            else:
                current += piece
        if current:
            chunks.append(current)
        for i, c in enumerate(chunks):
            emb = discord.Embed(
                title=f"📚 Vua Tieng Viet database ({len(words)} words) — page {i + 1}/{len(chunks)}",
                description=c,
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(embed=emb, ephemeral=True)

    # =========================================================================
    # /vuatiengviet word bulk + import — batch insertion
    # =========================================================================

    async def _bulk_insert_words(
        self, guild_id: int, user_id: int, entries: list[dict],
    ) -> tuple[int, int, list[str]]:
        """Insert (word, meaning) pairs one at a time so a single
        duplicate doesn't poison the whole batch. Returns
        (added, duplicates, errors)."""
        added = 0
        duplicates = 0
        errors: list[str] = []
        for entry in entries:
            try:
                async with self.bot.db.session() as s:
                    s.add(
                        VietWord(
                            guild_id=guild_id,
                            word=entry["word"][:_MAX_WORD_LEN],
                            meaning=entry["meaning"][:_MAX_MEANING_LEN],
                            added_by=user_id,
                        )
                    )
                    try:
                        await s.commit()
                        added += 1
                    except IntegrityError:
                        await s.rollback()
                        duplicates += 1
            except Exception as e:
                log.exception(
                    "vtv bulk insert failed for '%s'", entry["word"]
                )
                errors.append(
                    f"`{entry['word'][:30]}`: DB error ({type(e).__name__})"
                )
        return added, duplicates, errors

    def _format_bulk_summary(
        self,
        added: int, duplicates: int,
        parse_errors: list[str], db_errors: list[str],
    ) -> str:
        lines = [
            f"✅ Added: **{added}** words",
        ]
        if duplicates:
            lines.append(f"♻️ Duplicates (already existed): **{duplicates}**")
        all_errors = parse_errors + db_errors
        if all_errors:
            lines.append(f"⚠️ Errors: **{len(all_errors)}**")
            # Show first ~10
            shown = all_errors[:10]
            lines.append("```\n" + "\n".join(shown) + "\n```")
            if len(all_errors) > 10:
                lines.append(f"_(+{len(all_errors) - 10} more errors)_")
        return "\n".join(lines)

    @word.command(
        name="bulk",
        description="(Owner) Open a dialog to paste many words at once",
    )
    async def word_bulk(self, interaction: discord.Interaction) -> None:
        # Owner gate has to run BEFORE send_modal (which is the response).
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "❌ Only the **server owner** can use this command.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(BulkAddModal(self))

    async def handle_bulk_add(
        self, interaction: discord.Interaction, text: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            return
        entries, parse_errors = parse_bulk_text(text)
        if not entries:
            await interaction.followup.send(
                "❌ Could not parse any valid lines.\n"
                "Line format: `word | meaning`. Example:\n"
                f"```\n{_BULK_TEMPLATE}\n```"
                + (
                    f"\n**Detail:** {len(parse_errors)} parse error(s)"
                    if parse_errors else ""
                ),
                ephemeral=True,
            )
            return
        added, duplicates, db_errors = await self._bulk_insert_words(
            interaction.guild_id, interaction.user.id, entries,
        )
        await interaction.followup.send(
            self._format_bulk_summary(added, duplicates, parse_errors, db_errors),
            ephemeral=True,
        )

    @word.command(
        name="import",
        description="(Owner) Import from a JSON or Excel (.xlsx) file",
    )
    @app_commands.describe(
        file=(
            "JSON file ([{word, meaning}, …]) or Excel .xlsx "
            "(column A = word, column B = meaning)"
        ),
    )
    async def word_import(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None

        filename = (file.filename or "").lower()
        if filename.endswith(".json"):
            entries, parse_errors = await parse_json_attachment(file)
        elif filename.endswith(".xlsx") or filename.endswith(".xlsm"):
            entries, parse_errors = await parse_excel_attachment(file)
        else:
            await interaction.followup.send(
                "❌ Unsupported format. Only `.json` or `.xlsx` is accepted.\n\n"
                "**JSON** layout:\n"
                "```json\n"
                '[\n  {"word": "Trăng tròn", "meaning": "Mặt trăng tròn..."},\n'
                '  {"word": "Mây trời", "meaning": "Những đám mây..."}\n]\n'
                "```\n"
                "**Excel** layout: column A = word, column B = meaning "
                "(the first row is treated as a header and skipped).",
                ephemeral=True,
            )
            return

        if not entries:
            await interaction.followup.send(
                "❌ Could not parse any valid lines from the file.\n"
                + (
                    f"**{len(parse_errors)} parse error(s):**\n```\n"
                    + "\n".join(parse_errors[:10]) + "\n```"
                    if parse_errors else ""
                ),
                ephemeral=True,
            )
            return

        added, duplicates, db_errors = await self._bulk_insert_words(
            interaction.guild_id, interaction.user.id, entries,
        )
        await interaction.followup.send(
            f"📥 Imported from `{file.filename}`:\n"
            + self._format_bulk_summary(added, duplicates, parse_errors, db_errors),
            ephemeral=True,
        )

    # =========================================================================
    # /vuatiengviet start
    # =========================================================================

    @vtv.command(
        name="start",
        description="Start a new round — random questions drawn from the database",
    )
    @app_commands.describe(
        num_questions="Number of questions this round (1-20)",
        seconds_per_question="Seconds to answer each question (10-300)",
        pause_seconds="Seconds to wait before the next question (empty = creator advances manually)",
        top1_reward="Coins awarded to top 1",
        top2_reward="Coins awarded to top 2",
        top3_reward="Coins awarded to top 3",
    )
    async def vtv_start(
        self,
        interaction: discord.Interaction,
        num_questions: app_commands.Range[int, 1, 20],
        seconds_per_question: app_commands.Range[int, 10, 300] = 30,
        pause_seconds: app_commands.Range[int, 1, 60] | None = None,
        top1_reward: app_commands.Range[int, 0, 1_000_000] = 0,
        top2_reward: app_commands.Range[int, 0, 1_000_000] = 0,
        top3_reward: app_commands.Range[int, 0, 1_000_000] = 0,
    ) -> None:
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)

        # Permission: configured creator role OR guild owner.
        is_owner_user = self._is_owner(interaction)
        creator_ids = set(cfg.vtv_creator_ids()) if cfg else set()
        if not is_owner_user:
            if not creator_ids:
                await interaction.response.send_message(
                    "❌ The server owner has not configured any creator role and you are not the owner.",
                    ephemeral=True,
                )
                return
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "❌ This command can only be used inside a server.", ephemeral=True
                )
                return
            user_role_ids = {r.id for r in interaction.user.roles}
            if not (user_role_ids & creator_ids):
                await interaction.response.send_message(
                    "❌ You don't have a role allowed to start /vuatiengviet.",
                    ephemeral=True,
                )
                return

        # Channel: configured default or current channel.
        target_channel: discord.TextChannel | None = None
        if cfg and cfg.vtv_channel_id and interaction.guild:
            ch = interaction.guild.get_channel(cfg.vtv_channel_id)
            if isinstance(ch, discord.TextChannel):
                target_channel = ch
        if target_channel is None and isinstance(interaction.channel, discord.TextChannel):
            target_channel = interaction.channel
        if target_channel is None:
            await interaction.response.send_message(
                "❌ No channel available. The owner must run `/vuatiengviet-admin setup channel:#...`.",
                ephemeral=True,
            )
            return

        # Bank sufficient?
        async with self.bot.db.session() as s:
            count = await s.scalar(
                select(func.count(VietWord.id)).where(
                    VietWord.guild_id == interaction.guild_id
                )
            )
            if (count or 0) < num_questions:
                await interaction.response.send_message(
                    f"❌ The database has only {count or 0} words — not enough for {num_questions} questions. "
                    "The owner must add more via `/vuatiengviet-admin word add` first.",
                    ephemeral=True,
                )
                return
            rows = await s.execute(
                select(VietWord).where(
                    VietWord.guild_id == interaction.guild_id
                ).order_by(func.random()).limit(num_questions)
            )
            picked = list(rows.scalars().all())

            session = VuaTiengVietSession(
                guild_id=interaction.guild_id,
                channel_id=target_channel.id,
                creator_id=interaction.user.id,
                num_questions=int(num_questions),
                seconds_per_question=int(seconds_per_question),
                pause_seconds=int(pause_seconds) if pause_seconds is not None else None,
                top1_reward=int(top1_reward),
                top2_reward=int(top2_reward),
                top3_reward=int(top3_reward),
                status="waiting",
            )
            s.add(session)
            await s.commit()
            await s.refresh(session)
            session_id = session.id

            for i, w in enumerate(picked):
                s.add(
                    VuaTiengVietSessionQuestion(
                        session_id=session_id,
                        word_id=w.id,
                        order_index=i,
                        scrambled_text=scramble_word(w.word),
                    )
                )
            await s.commit()

        coin_name, coin_icon = await self._coin_label_parts(interaction.guild_id)
        embed = _waiting_embed(session, coin_name, coin_icon)
        view = _waiting_view(session_id)
        try:
            msg = await target_channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Bot doesn't have permission to post in {target_channel.mention}.",
                ephemeral=True,
            )
            return
        async with self.bot.db.session() as s:
            row = await s.get(VuaTiengVietSession, session_id)
            if row is not None:
                row.message_id = msg.id
                await s.commit()

        ack = (
            f"✅ Started Vua Tieng Viet round #{session_id} ↑"
            if target_channel.id == interaction.channel_id
            else f"✅ Started round #{session_id} in {target_channel.mention}"
        )
        await interaction.response.send_message(ack, ephemeral=True)

    # =========================================================================
    # Helpers used by button handlers
    # =========================================================================

    async def _coin_label_parts(self, guild_id: int) -> tuple[str, str]:
        async with self.bot.db.session() as s:
            ecfg = await s.get(GuildEconomyConfig, guild_id)
        return (
            ecfg.coin_name if ecfg else "coin",
            (ecfg.coin_icon or "🪙") if ecfg else "🪙",
        )

    async def _fetch_message(self, session: VuaTiengVietSession) -> discord.Message | None:
        if session.message_id is None:
            return None
        channel = self.bot.get_channel(session.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        try:
            return await channel.fetch_message(session.message_id)
        except (discord.NotFound, discord.Forbidden):
            return None

    async def _dismiss_answer_acks(self, session_id: int, sq_id: int) -> None:
        for inter in self._answer_acks.pop((session_id, sq_id), []):
            try:
                await inter.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass

    async def _wait_phase(
        self, session_id: int, seconds: int | None,
    ) -> str:
        """Wait `seconds` (or forever if None). Returns one of:
        'timeout' / 'next' / 'end' so callers can branch."""
        next_evt = self._next_events.setdefault(session_id, asyncio.Event())
        end_evt = self._end_events.setdefault(session_id, asyncio.Event())
        next_task = asyncio.create_task(next_evt.wait())
        end_task = asyncio.create_task(end_evt.wait())
        tasks: list[asyncio.Task] = [next_task, end_task]
        timeout_task: asyncio.Task | None = None
        if seconds is not None and seconds > 0:
            timeout_task = asyncio.create_task(asyncio.sleep(seconds))
            tasks.append(timeout_task)
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if end_task in done:
            return "end"
        if next_task in done:
            return "next"
        return "timeout"

    # =========================================================================
    # Button handlers
    # =========================================================================

    async def handle_start(
        self, interaction: discord.Interaction, session_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(VuaTiengVietSession)
                .where(VuaTiengVietSession.id == session_id)
                .options(selectinload(VuaTiengVietSession.questions))
            )
            if session is None:
                await interaction.response.send_message(
                    "This round no longer exists.", ephemeral=True
                )
                return
            is_admin = (
                isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.manage_guild
            )
            if interaction.user.id != session.creator_id and not is_admin:
                await interaction.response.send_message(
                    "❌ Only the creator (or an admin) can press Start.", ephemeral=True
                )
                return
            if session.status != "waiting":
                await interaction.response.send_message(
                    f"Round is already in status **{session.status}**.", ephemeral=True
                )
                return
            session.status = "running"
            session.started_at = datetime.now(timezone.utc)
            session.current_index = 0
            await s.commit()

        await interaction.response.defer()
        # Spawn the game-loop task
        self._next_events[session_id] = asyncio.Event()
        self._end_events[session_id] = asyncio.Event()
        task = asyncio.create_task(self._run_session(session_id))
        self._tasks[session_id] = task

    async def handle_end(
        self, interaction: discord.Interaction, session_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            session = await s.get(VuaTiengVietSession, session_id)
            if session is None:
                await interaction.response.send_message(
                    "Round no longer exists.", ephemeral=True
                )
                return
            is_admin = (
                isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.manage_guild
            )
            if interaction.user.id != session.creator_id and not is_admin:
                await interaction.response.send_message(
                    "❌ Only the creator (or an admin) can end this round.", ephemeral=True
                )
                return
            if session.status not in ("waiting", "running"):
                await interaction.response.send_message(
                    f"Round is already in status **{session.status}**.", ephemeral=True
                )
                return
            session.status = "cancelled"
            session.ended_at = datetime.now(timezone.utc)
            await s.commit()
        # Signal any waiting loop to exit
        end_evt = self._end_events.get(session_id)
        if end_evt is not None:
            end_evt.set()
        await interaction.response.send_message(
            f"🛑 Round #{session_id} cancelled.", ephemeral=True
        )
        # Best-effort embed swap to cancelled state
        async with self.bot.db.session() as s:
            session = await s.get(VuaTiengVietSession, session_id)
        if session is not None:
            msg = await self._fetch_message(session)
            if msg is not None:
                cancelled_embed = discord.Embed(
                    title="🇻🇳 Vua Tieng Viet — Cancelled",
                    description=(
                        f"🛑 <@{interaction.user.id}> cancelled round #{session_id}."
                    ),
                    color=discord.Color.red(),
                )
                try:
                    await msg.edit(embed=cancelled_embed, view=None)
                except discord.HTTPException:
                    pass

    async def handle_next(
        self, interaction: discord.Interaction, session_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            session = await s.get(VuaTiengVietSession, session_id)
            if session is None or session.status != "running":
                await interaction.response.send_message(
                    "Round is not currently running.", ephemeral=True
                )
                return
            is_admin = (
                isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.manage_guild
            )
            if interaction.user.id != session.creator_id and not is_admin:
                await interaction.response.send_message(
                    "❌ Only the creator (or an admin) can advance the question.", ephemeral=True
                )
                return
        next_evt = self._next_events.get(session_id)
        if next_evt is not None:
            next_evt.set()
        await interaction.response.defer()

    async def handle_answer_click(
        self, interaction: discord.Interaction,
        session_id: int, sq_id: int, click_at: datetime,
    ) -> None:
        # Pre-check: is the question still accepting answers?
        async with self.bot.db.session() as s:
            session = await s.get(VuaTiengVietSession, session_id)
            sq = await s.get(VuaTiengVietSessionQuestion, sq_id)
            if session is None or sq is None:
                await interaction.response.send_message(
                    "Question no longer exists.", ephemeral=True
                )
                return
            if session.status != "running":
                await interaction.response.send_message(
                    "⏱ Round is not currently running.", ephemeral=True
                )
                return
            if sq.revealed_at is not None:
                await interaction.response.send_message(
                    "⏱ Time is up for this question.", ephemeral=True
                )
                return
            existing = await s.scalar(
                select(VuaTiengVietAnswer).where(
                    VuaTiengVietAnswer.session_question_id == sq_id,
                    VuaTiengVietAnswer.user_id == interaction.user.id,
                )
            )
            if existing is not None:
                await interaction.response.send_message(
                    f"❌ You already answered this question: “{existing.answer_text}”",
                    ephemeral=True,
                )
                return
        # Open the answer modal
        modal = AnswerModal(self, session_id, sq_id, click_at)
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            log.exception("vtv: send_modal failed")

    async def handle_answer_submit(
        self,
        interaction: discord.Interaction,
        session_id: int, sq_id: int,
        click_at: datetime, raw_answer: str,
    ) -> None:
        submitted_at = datetime.now(timezone.utc)
        elapsed_ms = max(0, int((submitted_at - click_at).total_seconds() * 1000))
        async with self.bot.db.session() as s:
            session = await s.get(VuaTiengVietSession, session_id)
            sq = await s.scalar(
                select(VuaTiengVietSessionQuestion)
                .where(VuaTiengVietSessionQuestion.id == sq_id)
                .options(selectinload(VuaTiengVietSessionQuestion.word))
            )
            if session is None or sq is None:
                await interaction.response.send_message(
                    "Question no longer exists.", ephemeral=True
                )
                return
            existing = await s.scalar(
                select(VuaTiengVietAnswer).where(
                    VuaTiengVietAnswer.session_question_id == sq_id,
                    VuaTiengVietAnswer.user_id == interaction.user.id,
                )
            )
            if existing is not None:
                await interaction.response.send_message(
                    f"❌ You already answered this question: “{existing.answer_text}”",
                    ephemeral=True,
                )
                return
            # Decide accepted: submitted_at must be ≤ question deadline.
            deadline = (
                sq.started_at + timedelta(seconds=session.seconds_per_question)
                if sq.started_at is not None else None
            )
            accepted = (
                sq.revealed_at is None
                and deadline is not None
                and submitted_at <= deadline
            )
            is_correct = (
                accepted
                and normalize_answer(raw_answer) == normalize_answer(sq.word.word)
            )
            ans = VuaTiengVietAnswer(
                session_question_id=sq_id,
                user_id=interaction.user.id,
                answer_text=raw_answer[:200],
                click_at=click_at,
                submitted_at=submitted_at,
                elapsed_ms=elapsed_ms,
                is_correct=is_correct,
                accepted=accepted,
            )
            s.add(ans)
            await s.commit()

        if not accepted:
            text = (
                "⏰ Time was up before your answer arrived — this one doesn't count.\n"
                f"Typing time: {_format_elapsed(elapsed_ms)}"
            )
        elif is_correct:
            text = (
                f"✅ **Correct answer recorded!**\n"
                f"⏱ Time: {_format_elapsed(elapsed_ms)} "
                "(you win if you're the earliest)"
            )
        else:
            text = (
                f"📨 Answer recorded.\n"
                f"⏱ Time: {_format_elapsed(elapsed_ms)}"
            )
        await interaction.response.send_message(text, ephemeral=True)
        # Track for auto-dismiss when the round reveals
        self._answer_acks.setdefault((session_id, sq_id), []).append(interaction)

    # =========================================================================
    # Game loop
    # =========================================================================

    async def _run_session(self, session_id: int) -> None:
        try:
            await self._run_session_inner(session_id)
        except asyncio.CancelledError:
            log.info("vtv session %s cancelled", session_id)
        except Exception:
            log.exception("vtv session %s crashed", session_id)
        finally:
            self._tasks.pop(session_id, None)
            self._next_events.pop(session_id, None)
            self._end_events.pop(session_id, None)

    async def _run_session_inner(self, session_id: int) -> None:
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(VuaTiengVietSession)
                .where(VuaTiengVietSession.id == session_id)
                .options(
                    selectinload(VuaTiengVietSession.questions)
                    .selectinload(VuaTiengVietSessionQuestion.word)
                )
            )
            if session is None or session.status != "running":
                return
            sqs = sorted(session.questions, key=lambda q: q.order_index)
        msg = await self._fetch_message(session)
        if msg is None:
            log.warning("vtv %s: missing game message", session_id)
            return

        for idx, sq in enumerate(sqs):
            # Reload session to honor cancellations
            async with self.bot.db.session() as s:
                session = await s.get(VuaTiengVietSession, session_id)
                if session is None or session.status != "running":
                    return
                session.current_index = idx
                sq_db = await s.get(VuaTiengVietSessionQuestion, sq.id)
                if sq_db is None:
                    return
                sq_db.started_at = datetime.now(timezone.utc)
                await s.commit()
                await s.refresh(sq_db)

            # Render question
            deadline = (
                sq_db.started_at
                + timedelta(seconds=session.seconds_per_question)
            )
            try:
                await msg.edit(
                    embed=_question_embed(session, sq, deadline),
                    view=_question_view(session_id, sq.id),
                )
            except discord.HTTPException as e:
                log.warning("vtv %s: question edit failed: %s", session_id, e)

            # Wait the answer window (or early end)
            phase = await self._wait_phase(
                session_id, session.seconds_per_question,
            )
            if phase == "end":
                return

            # Mark revealed; compute winner from accepted+correct answers
            async with self.bot.db.session() as s:
                sq_db = await s.scalar(
                    select(VuaTiengVietSessionQuestion)
                    .where(VuaTiengVietSessionQuestion.id == sq.id)
                    .options(selectinload(VuaTiengVietSessionQuestion.word))
                )
                if sq_db is None:
                    return
                sq_db.revealed_at = datetime.now(timezone.utc)
                ans_rows = await s.execute(
                    select(VuaTiengVietAnswer).where(
                        VuaTiengVietAnswer.session_question_id == sq.id
                    )
                )
                answers = list(ans_rows.scalars().all())
                eligible = [
                    a for a in answers if a.accepted and a.is_correct
                ]
                if eligible:
                    eligible.sort(key=lambda a: a.elapsed_ms)
                    sq_db.winner_id = eligible[0].user_id
                else:
                    sq_db.winner_id = None
                await s.commit()
                await s.refresh(sq_db)

            await self._dismiss_answer_acks(session_id, sq.id)

            # Render reveal
            has_next = idx + 1 < len(sqs)
            manual = session.pause_seconds is None
            next_deadline = (
                datetime.now(timezone.utc)
                + timedelta(seconds=session.pause_seconds)
                if has_next and not manual else None
            )
            try:
                await msg.edit(
                    embed=_reveal_embed(
                        session, sq_db, answers,
                        has_next=has_next,
                        next_deadline=next_deadline,
                        manual=manual,
                    ),
                    view=_reveal_view(
                        session_id, sq.id,
                        has_next=has_next, manual=manual,
                    ),
                )
            except discord.HTTPException as e:
                log.warning("vtv %s: reveal edit failed: %s", session_id, e)

            if not has_next:
                break

            # Pause between questions — auto or manual
            phase = await self._wait_phase(session_id, session.pause_seconds)
            if phase == "end":
                return
            # Clear the next event before the next round so it doesn't pre-fire
            evt = self._next_events.get(session_id)
            if evt is not None:
                evt.clear()

        # Finalize
        await self._finalize(session_id, msg)

    async def _finalize(
        self, session_id: int, msg: discord.Message,
    ) -> None:
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(VuaTiengVietSession)
                .where(VuaTiengVietSession.id == session_id)
                .options(selectinload(VuaTiengVietSession.questions))
            )
            if session is None:
                return
            sq_ids = [q.id for q in session.questions]
            ans_rows = await s.execute(
                select(VuaTiengVietAnswer).where(
                    VuaTiengVietAnswer.session_question_id.in_(sq_ids)
                )
            )
            all_answers = list(ans_rows.scalars().all())

        # Score: per question, only the question-winner counts as correct
        # in the "winner_id" sense; but here we tally per-user correct
        # answers (eligible+correct) as the leaderboard signal.
        from collections import defaultdict
        correct_count: dict[int, int] = defaultdict(int)
        total_elapsed: dict[int, int] = defaultdict(int)
        for a in all_answers:
            if a.accepted and a.is_correct:
                correct_count[a.user_id] += 1
                total_elapsed[a.user_id] += a.elapsed_ms

        # Anyone who only submitted wrong answers should still appear,
        # so include them with 0 correct (sorted last).
        all_users = {a.user_id for a in all_answers}
        scores = sorted(
            [
                (uid, correct_count.get(uid, 0), total_elapsed.get(uid, 0))
                for uid in all_users
            ],
            key=lambda x: (-x[1], x[2], x[0]),
        )

        payouts: dict[int, int] = {}
        wallet = WalletService(self.bot)

        async def _pay(uid: int, amount: int, outcome: str) -> None:
            """Grant a placement reward + pet buff. Stores the total
            (base + buff) in payouts so the scoreboard embed shows the
            full prize the user actually received."""
            try:
                await wallet.adjust(
                    session.guild_id, uid, amount,
                    f"minigame_vuatiengviet_{outcome}",
                    description=f"{outcome.title()} Vua Tieng Viet #{session_id}",
                    metadata={"session_id": session_id},
                )
            except Exception:
                log.exception("vtv: %s payout failed", outcome)
                return
            buff = 0
            try:
                buff = await wallet.apply_pet_minigame_buff(
                    session.guild_id, uid, amount, "vuatiengviet",
                    session_id=session_id, outcome=outcome,
                )
            except Exception:
                log.exception("vtv: pet buff failed for %s", outcome)
            payouts[uid] = amount + buff

        # Top1
        if scores and scores[0][1] > 0 and session.top1_reward > 0:
            await _pay(scores[0][0], session.top1_reward, "top1")
        # Top2
        if len(scores) >= 2 and scores[1][1] > 0 and session.top2_reward > 0:
            await _pay(scores[1][0], session.top2_reward, "top2")
        # Top3
        if len(scores) >= 3 and scores[2][1] > 0 and session.top3_reward > 0:
            await _pay(scores[2][0], session.top3_reward, "top3")

        # Record win + finalize session row
        top1_uid = scores[0][0] if scores and scores[0][1] > 0 else None
        async with self.bot.db.session() as s:
            sess = await s.get(VuaTiengVietSession, session_id)
            if sess is None:
                return
            sess.status = "finished"
            sess.ended_at = datetime.now(timezone.utc)
            sess.winner_id = top1_uid
            if top1_uid is not None:
                s.add(VuaTiengVietWin(
                    guild_id=sess.guild_id,
                    user_id=top1_uid,
                    session_id=session_id,
                ))
            await s.commit()

        coin_name, coin_icon = await self._coin_label_parts(session.guild_id)
        try:
            await msg.edit(
                embed=_scoreboard_embed(
                    session, scores, coin_name, coin_icon, payouts,
                ),
                view=None,
            )
        except discord.HTTPException:
            pass

        # Post vinh danh + grant winner role + refresh BXH
        if top1_uid is not None:
            try:
                await self._post_vinh_danh(
                    session_id, top1_uid, scores, payouts,
                )
            except Exception:
                log.exception("vtv: vinh danh post failed")
            try:
                await self._maybe_grant_winner_role(session.guild_id, top1_uid)
            except Exception:
                log.exception("vtv: role grant failed")
        try:
            await self._refresh_bxh_board(session.guild_id)
        except Exception:
            log.exception("vtv: bxh refresh failed")

    async def _post_vinh_danh(
        self, session_id: int, winner_id: int,
        scores: list[tuple[int, int, int]],
        payouts: dict[int, int],
    ) -> None:
        async with self.bot.db.session() as s:
            session = await s.get(VuaTiengVietSession, session_id)
            cfg = await s.get(GuildConfig, session.guild_id) if session else None
        if cfg is None or cfg.vtv_announce_channel_id is None or session is None:
            return
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(cfg.vtv_announce_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        # Total wins for the top1 user
        async with self.bot.db.session() as s:
            total = await s.scalar(
                select(func.count(VuaTiengVietWin.id)).where(
                    VuaTiengVietWin.guild_id == session.guild_id,
                    VuaTiengVietWin.user_id == winner_id,
                )
            )
        coin_name, coin_icon = await self._coin_label_parts(session.guild_id)
        prize = payouts.get(winner_id, 0)
        prize_line = (
            f"\n💰 Prize: **+{prize:,}** {coin_icon} {coin_name}"
            if prize > 0 else ""
        )
        embed = discord.Embed(
            title="👑 Hall of Fame — Vua Tieng Viet",
            description=(
                f"<@{winner_id}> won round #{session_id}!"
                f"{prize_line}\n"
                f"🎖 Total championships: **{total or 0}**"
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            pass

    async def _maybe_grant_winner_role(
        self, guild_id: int, winner_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
            if (
                cfg is None or cfg.vtv_winner_role_id is None
                or not cfg.vtv_wins_for_role
            ):
                return
            total = await s.scalar(
                select(func.count(VuaTiengVietWin.id)).where(
                    VuaTiengVietWin.guild_id == guild_id,
                    VuaTiengVietWin.user_id == winner_id,
                )
            )
        if (total or 0) < cfg.vtv_wins_for_role:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        role = guild.get_role(cfg.vtv_winner_role_id)
        if role is None:
            return
        member = guild.get_member(winner_id)
        if member is None or role in member.roles:
            return
        me = guild.me
        if me is None or role >= me.top_role:
            log.warning("vtv: cannot grant role %s (above bot top role)", role.id)
            return
        try:
            await member.add_roles(
                role, reason=f"Vua Tieng Viet {cfg.vtv_wins_for_role}+ wins",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("vtv: grant role failed: %s", e)

    # =========================================================================
    # /vuatiengviet bxh — leaderboard (owner only)
    # =========================================================================

    bxh = app_commands.Group(
        name="bxh",
        description="Vua Tieng Viet leaderboard",
        parent=vtv_admin,
    )

    _BXH_I18N: dict[str, dict[str, str]] = {
        LANG_RU: {
            "title":      "👑  ВУА ТИЕНГ ВЕТ — ТАБЛИЦА ЛИДЕРОВ  👑",
            "no_winners": "_Ещё никто не выиграл раунд._\n**Участвуй и стань первым!**",
            "win_unit":   "чемпионств",
            "footer":     "{guild} • Топ {n} • Обновляется после каждого раунда",
        },
        LANG_EN: {
            "title":      "👑  VUA TIENG VIET LEADERBOARD  👑",
            "no_winners": "_No one has won a round yet._\n**Join a round and be the first!**",
            "win_unit":   "championships",
            "footer":     "{guild} • Top {n} • Auto-updates after each round",
        },
    }

    async def _build_bxh_embed(
        self, guild_id: int, display_count: int, lang: str = LANG_RU,
    ) -> discord.Embed:
        strings = self._BXH_I18N.get(lang, self._BXH_I18N[LANG_RU])
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(
                    VuaTiengVietWin.user_id,
                    func.count(VuaTiengVietWin.id).label("wins"),
                )
                .where(VuaTiengVietWin.guild_id == guild_id)
                .group_by(VuaTiengVietWin.user_id)
                .order_by(
                    func.count(VuaTiengVietWin.id).desc(),
                    VuaTiengVietWin.user_id,
                )
                .limit(display_count)
            )
            results = list(rows.all())
        guild = self.bot.get_guild(guild_id)
        guild_name = guild.name if guild else "Server"
        embed = discord.Embed(
            title=strings["title"],
            color=discord.Color.from_rgb(241, 196, 15),
        )
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if not results:
            embed.description = strings["no_winners"]
        else:
            lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
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
                    f"{medal}  <@{uid}>  •  👑 `{wins:,}` {strings['win_unit']}"
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
        """Link button to the configured /vuatiengviet game channel
        where creators post new rounds. Empty view when no channel set."""
        view = discord.ui.View(timeout=None)
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
        ch_id = cfg.vtv_channel_id if cfg else None
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

    async def _build_bxh_view(
        self, guild_id: int, lang: str = LANG_RU,
    ) -> discord.ui.View:
        view = await self._build_play_view(guild_id)
        view.add_item(BxhBoardLangButton(guild_id, lang=lang))
        return view

    async def handle_board_lang_toggle(
        self, interaction: discord.Interaction, guild_id: int,
    ) -> None:
        await interaction.response.defer()
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "vuatiengviet",
                )
            )
            if board is None:
                await interaction.followup.send("❌ Board not found.", ephemeral=True)
                return
            new_lang = LANG_EN if getattr(board, "language", LANG_RU) == LANG_RU else LANG_RU
            board.language = new_lang
            display_count = board.display_count
            await s.commit()
        embed = await self._build_bxh_embed(guild_id, display_count, lang=new_lang)
        view = await self._build_bxh_view(guild_id, lang=new_lang)
        await interaction.edit_original_response(
            embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _refresh_bxh_board(self, guild_id: int) -> None:
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "vuatiengviet",
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
        embed = await self._build_bxh_embed(guild_id, board.display_count, lang=lang)
        view = await self._build_bxh_view(guild_id, lang=lang)
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
                log.warning("vtv bxh: edit failed: %s", e)
            return
        try:
            new_msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            return
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "vuatiengviet",
                )
            )
            if row is not None:
                row.message_id = new_msg.id
                await s.commit()

    @bxh.command(
        name="post",
        description="(Owner) Create / move the Vua Tieng Viet leaderboard",
    )
    @app_commands.describe(
        channel="Channel to post the leaderboard in (any text channel)",
        display_count="How many users to show (1-25, default 10)",
    )
    async def bxh_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        display_count: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            prev = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "vuatiengviet",
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
        embed = await self._build_bxh_embed(
            interaction.guild_id, int(display_count), lang=LANG_RU,
        )
        view = await self._build_bxh_view(interaction.guild_id, lang=LANG_RU)
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
                    GameWinBoard.game_type == "vuatiengviet",
                )
            )
            if board is None:
                board = GameWinBoard(
                    guild_id=interaction.guild_id,
                    game_type="vuatiengviet",
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
            f"✅ Leaderboard posted in {channel.mention} (top {display_count}). "
            "It will auto-update after every round ends.",
            ephemeral=True,
        )

    @bxh.command(
        name="remove",
        description="(Owner) Delete the leaderboard + stop auto-update",
    )
    async def bxh_remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "vuatiengviet",
                )
            )
            if board is None:
                await interaction.followup.send(
                    "This server has no leaderboard.", ephemeral=True
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
            "✅ Leaderboard deleted.", ephemeral=True
        )

    @bxh.command(
        name="refresh",
        description="(Owner) Refresh the leaderboard immediately",
    )
    async def bxh_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "vuatiengviet",
                )
            )
        if board is None:
            await interaction.followup.send(
                "No leaderboard yet. Run `/vuatiengviet-admin bxh post` first.",
                ephemeral=True,
            )
            return
        await self._refresh_bxh_board(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Leaderboard refreshed in <#{board.channel_id}>.", ephemeral=True
        )


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(VuaTiengViet(bot))
