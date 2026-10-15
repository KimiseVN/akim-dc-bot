"""Quiz minigame cog.

Schema in src/db/models.py:
  QuizQuestion           — per-guild bank of multiple-choice questions
  QuizSession            — one playthrough (waiting → running → finished)
  QuizSessionQuestion    — which questions in that session, in order
  QuizAnswer             — one user's pick for one question of one session

Slash surface
  /quiz add        (admin) bulk-paste questions via modal
  /quiz list       (admin) paginated bank listing
  /quiz remove id  (admin) delete a question from the bank
  /quiz start ...  (admin) create a session in the configured channel

Game flow
  start  → bot posts a controller message with a 'Start Quiz' button
  Start  → creator clicks; per-question loop begins
  per Q  → bot posts question + A/B/C/D buttons; timer counts down;
           each user can answer once; on expiry the message is edited
           to highlight the correct answer and list who picked what.
  end    → final scoreboard posted; session marked finished.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from ..db.models import (
    GameWinBoard,
    GuildConfig,
    GuildEconomyConfig,
    QuizAnswer,
    QuizQuestion,
    QuizSession,
    QuizSessionQuestion,
    QuizWin,
)
from ..economy.service import MiniGameResult, RewardEngine
from ..i18n import LANG_BUTTON_EMOJI, LANG_EN, LANG_RU

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.quiz")


LETTERS = ("A", "B", "C", "D")
_REVEAL_PAUSE_SECONDS = 6


# =============================================================================
# Bulk-add parser
# =============================================================================


_QUESTION_LINE = re.compile(r"^\s*(\d+)\s*[\.\)]\s*(.+)$")
_OPTION_LINE = re.compile(r"^\s*([A-D])\s*[\.\)]\s*(.+)$", re.IGNORECASE)
_ANSWER_LINE = re.compile(
    r"^\s*(?:đáp\s*án|dap\s*an|đa|d\.?a|answer|correct)\s*[:=]\s*([A-D])\b",
    re.IGNORECASE,
)


def parse_questions_text(text: str) -> tuple[list[dict], list[str]]:
    """Parse a bulk-paste of questions in the screenshot's format.

    Returns (valid_questions, error_messages). Each valid_question is a
    dict with keys: question, option_a, option_b, option_c, option_d, correct.
    """
    current: dict | None = None
    valid: list[dict] = []
    errors: list[str] = []
    started_n = 0

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        missing = [opt for opt in LETTERS if opt not in current["options"]]
        if missing:
            errors.append(
                f"Question #{current['n']}: missing option {', '.join(missing)} → skipped"
            )
        elif current["correct"] is None:
            errors.append(
                f"Question #{current['n']}: missing `Answer: X` line → skipped"
            )
        else:
            valid.append(
                {
                    "question": current["text"].strip(),
                    "option_a": current["options"]["A"].strip(),
                    "option_b": current["options"]["B"].strip(),
                    "option_c": current["options"]["C"].strip(),
                    "option_d": current["options"]["D"].strip(),
                    "correct": current["correct"],
                }
            )
        current = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m_q = _QUESTION_LINE.match(line)
        m_o = _OPTION_LINE.match(line)
        m_a = _ANSWER_LINE.match(line)
        if m_q and not m_o:  # don't confuse 'A. text' with question start
            flush()
            started_n += 1
            current = {
                "n": started_n,
                "text": m_q.group(2),
                "options": {},
                "correct": None,
            }
        elif m_o and current is not None:
            letter = m_o.group(1).upper()
            current["options"][letter] = m_o.group(2)
        elif m_a and current is not None:
            current["correct"] = m_a.group(1).upper()
        else:
            # Continuation of the current question text or option — append.
            if current is None:
                continue
            if current["options"]:
                last_letter = list(current["options"].keys())[-1]
                current["options"][last_letter] += " " + line.strip()
            else:
                current["text"] += " " + line.strip()
    flush()
    return valid, errors


# =============================================================================
# Bulk-add modal
# =============================================================================


_ADD_TEMPLATE = (
    "1. What is the primary genre of Where Winds Meet?\n"
    "A. Survival FPS\n"
    "B. Wuxia open-world action-adventure RPG\n"
    "C. MOBA 5v5\n"
    "D. Card battle RPG\n"
    "Answer: B\n\n"
    "2. (Next question...)\n"
    "A. ...\nB. ...\nC. ...\nD. ...\nAnswer: A"
)


class _BulkAddModal(discord.ui.Modal):
    def __init__(self, bot: "AkimBot") -> None:
        super().__init__(title="Add quiz questions (bulk)")
        self.bot = bot
        self.payload = discord.ui.TextInput(
            label="Questions (many, per the standard format)",
            placeholder="N. question / A./B./C./D. options / Answer: X",
            style=discord.TextStyle.paragraph,
            min_length=20,
            max_length=4000,
            required=True,
        )
        self.add_item(self.payload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        text = str(self.payload.value)
        parsed, errors = parse_questions_text(text)

        if not parsed:
            example = f"```\n{_ADD_TEMPLATE}\n```"
            await interaction.response.send_message(
                "❌ No questions could be parsed. Format per question:\n" + example
                + ("\n\n**Details:**\n" + "\n".join(errors) if errors else ""),
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            for q in parsed:
                s.add(
                    QuizQuestion(
                        guild_id=interaction.guild_id,
                        question_text=q["question"][:2000],
                        option_a=q["option_a"][:500],
                        option_b=q["option_b"][:500],
                        option_c=q["option_c"][:500],
                        option_d=q["option_d"][:500],
                        correct=q["correct"],
                        added_by=interaction.user.id,
                    )
                )
            await s.commit()

        lines = [f"✅ Added **{len(parsed)} question(s)** to the bank."]
        if errors:
            lines.append(f"⚠️ {len(errors)} question(s) had errors (skipped):")
            lines.extend(f"• {e}" for e in errors[:10])
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )


# =============================================================================
# Quiz views (controller + per-question)
# =============================================================================


def _option_label(letter: str, text: str) -> str:
    raw = f"{letter}. {text}"
    return raw if len(raw) <= 80 else raw[:77] + "…"


def _build_question_embed(
    session: QuizSession,
    sq: QuizSessionQuestion,
    *,
    reveal: bool = False,
    answers_by_choice: dict[str, list[int]] | None = None,
    deadline_at: datetime | None = None,
) -> discord.Embed:
    """deadline_at is the UTC moment at which the current phase ends.

    For a live question, that's the answer deadline; for a reveal, that's
    when the next question / scoreboard lands. Rendered as Discord's
    <t:ts:R> token so every viewer's client refreshes the countdown
    locally — the bot does not have to edit the message every second.
    """
    q = sq.question
    total = session.num_questions
    idx = sq.order_index + 1
    color = discord.Color.green() if reveal else discord.Color.blurple()
    title = f"Question {idx}/{total}"
    if reveal:
        title += "  •  ✅ Answer"
    embed = discord.Embed(title=title, color=color)
    embed.description = q.question_text
    if deadline_at is not None:
        ts = int(deadline_at.timestamp())
        if reveal:
            embed.description += (
                f"\n\n⏭ **Next question:** <t:{ts}:R>"
            )
        else:
            embed.description += (
                f"\n\n⏳ **Deadline:** <t:{ts}:R>  •  1 user = 1 answer"
            )

    answers_by_choice = answers_by_choice or {}
    for letter in LETTERS:
        is_correct = letter == q.correct
        users = answers_by_choice.get(letter, [])
        prefix = ""
        if reveal:
            if is_correct:
                prefix = "✅ "
            elif users:
                prefix = "❌ "
        line_value = q.option(letter)
        field_name = f"{prefix}{letter}"
        if reveal:
            mentions = (
                ", ".join(f"<@{u}>" for u in users[:20])
                + (f" +{len(users) - 20}" if len(users) > 20 else "")
            )
            count_str = f"`{len(users)}` picked"
            field_value = (
                f"{line_value}\n— {count_str}"
                + (f": {mentions}" if mentions else "")
            )
        else:
            field_value = line_value
        embed.add_field(name=field_name, value=field_value[:1024], inline=False)

    # Countdown moved into description (footer can't render <t:ts:R>).
    # Keep a static footer as fallback when no deadline was passed.
    if deadline_at is None:
        if reveal:
            embed.set_footer(
                text=f"Next question in {_REVEAL_PAUSE_SECONDS}s…"
            )
        else:
            embed.set_footer(
                text=f"⏳ {session.seconds_per_question}s / question  •  1 user = 1 answer"
            )
    return embed


class _AnswerButton(discord.ui.Button):
    def __init__(
        self,
        letter: str,
        session_id: int,
        sq_id: int,
        *,
        disabled: bool = False,
    ) -> None:
        self.letter = letter
        self.session_id = session_id
        self.sq_id = sq_id
        super().__init__(
            label=letter,
            style=discord.ButtonStyle.secondary,
            custom_id=f"quiz:ans:{session_id}:{sq_id}:{letter}",
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Quiz" = interaction.client.get_cog("Quiz")  # type: ignore[assignment]
        if cog is None:
            await interaction.response.send_message(
                "Quiz cog isn't loaded.", ephemeral=True
            )
            return
        await cog.handle_answer_click(
            interaction, self.session_id, self.sq_id, self.letter
        )


def build_question_view(
    session_id: int, sq_id: int, *, disabled: bool = False
) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for letter in LETTERS:
        v.add_item(_AnswerButton(letter, session_id, sq_id, disabled=disabled))
    return v


class _StartButton(discord.ui.Button):
    def __init__(self, session_id: int, *, disabled: bool = False) -> None:
        self.session_id = session_id
        super().__init__(
            label="Start Quiz",
            emoji="▶️",
            style=discord.ButtonStyle.success,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Quiz" = interaction.client.get_cog("Quiz")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_start_click(interaction, self.session_id)


def build_controller_view(session_id: int, *, disabled: bool = False) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(_StartButton(session_id, disabled=disabled))
    return v


# =============================================================================
# Board lang button
# =============================================================================


class CunhanBoardLangButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"quiz:board_lang:(?P<guild_id>\d+)",
):
    """🌐 Toggle the Scholar leaderboard between RU and EN."""

    def __init__(self, guild_id: int, *, lang: str = LANG_RU) -> None:
        self.guild_id = guild_id
        label = "English" if lang == LANG_RU else "Русский"
        super().__init__(discord.ui.Button(
            custom_id=f"quiz:board_lang:{guild_id}",
            label=label, emoji=LANG_BUTTON_EMOJI,
            style=discord.ButtonStyle.secondary, row=1,
        ))

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match, /
    ) -> "CunhanBoardLangButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Quiz" = interaction.client.get_cog("Quiz")  # type: ignore[assignment]
        if cog:
            await cog.handle_board_lang_toggle(interaction, self.guild_id)


# =============================================================================
# Cog
# =============================================================================


class Quiz(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        # session_id -> running per-question task
        self._tasks: dict[int, asyncio.Task] = {}

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
        # (session_id, session_question_id) -> list of answer interactions whose
        # ephemeral 'answer recorded' ack we want to auto-dismiss when the question
        # ends. Interaction webhook tokens are valid for 15 minutes, which is
        # well above any reasonable quiz duration.
        self._answer_acks: dict[
            tuple[int, int], list[discord.Interaction]
        ] = {}

    # ---- /quiz group (user-facing) ----
    # No default_permissions: every member sees this group so that
    # /quiz start is reachable. Runtime gating still applies — start
    # rejects callers without admin OR an explicit creator role once
    # quiz adds that config (currently it's admin-only at runtime).
    quiz = app_commands.Group(
        name="quiz",
        description="Quiz minigame",
        guild_only=True,
    )

    # ---- /quiz-admin group (owner-only) ----
    # Discord caps permission gating at the top-level group, so we
    # split admin commands into a separate group rather than try (and
    # fail) to override default_permissions per subcommand. The
    # group-level Administrator perm is the strictest UI gate slash
    # commands support; runtime _require_owner still trims it down to
    # the actual guild owner.
    quiz_admin = app_commands.Group(
        name="quiz-admin",
        description="(Owner) Quiz admin: setup, question bank, hall of fame",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ---------- /quiz add ----------

    @quiz_admin.command(name="add", description="(Owner) Open a dialog to paste many questions at once")
    async def quiz_add(self, interaction: discord.Interaction) -> None:
        # Owner gate must run BEFORE send_modal (which IS the response).
        if not self._is_owner(interaction):
            await interaction.response.send_message(
                "❌ Only the **server owner** can use this command.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(_BulkAddModal(self.bot))

    # ---------- /quiz list ----------

    @quiz_admin.command(name="list", description="(Owner) List questions in the bank")
    async def quiz_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(QuizQuestion)
                .where(QuizQuestion.guild_id == interaction.guild_id)
                .order_by(QuizQuestion.id)
            )
            qs = list(rows.scalars().all())
        if not qs:
            await interaction.response.send_message(
                "Bank is empty. Use `/quiz-admin add` to add questions.", ephemeral=True
            )
            return
        chunks: list[str] = []
        current = ""
        for q in qs:
            piece = (
                f"`#{q.id}` **{q.question_text[:120]}** "
                f"_(answer: {q.correct})_\n"
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
                title=f"Quiz bank ({len(qs)} questions) — page {i + 1}/{len(chunks)}",
                description=c,
                color=discord.Color.blurple(),
            )
            if i == 0:
                await interaction.response.send_message(embed=emb, ephemeral=True)
            else:
                await interaction.followup.send(embed=emb, ephemeral=True)

    # ---------- /quiz remove ----------

    @quiz_admin.command(
        name="remove",
        description="(Owner) Remove one question by ID, or 'all' to wipe the whole bank",
    )
    @app_commands.describe(
        target="Question ID (e.g. 12) — or type `all` to wipe the whole bank",
    )
    async def quiz_remove(
        self, interaction: discord.Interaction, target: str
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        normalized = target.strip().lower()

        # ---- bulk wipe ----
        if normalized == "all":
            async with self.bot.db.session() as s:
                total = await s.scalar(
                    select(func.count(QuizQuestion.id)).where(
                        QuizQuestion.guild_id == interaction.guild_id
                    )
                )
                if not total:
                    await interaction.response.send_message(
                        "Bank is empty — nothing to remove.", ephemeral=True
                    )
                    return
                result = await s.execute(
                    delete(QuizQuestion).where(
                        QuizQuestion.guild_id == interaction.guild_id
                    )
                )
                await s.commit()
            await interaction.response.send_message(
                f"🧹 Removed **all {result.rowcount or total} questions** from the bank.\n"
                "ℹ️ Past/current quiz sessions were also removed (cascade).",
                ephemeral=True,
            )
            return

        # ---- single delete by id ----
        try:
            qid = int(normalized)
        except ValueError:
            await interaction.response.send_message(
                "❌ `target` must be a numeric ID (e.g. `12`) or `all` to wipe all.",
                ephemeral=True,
            )
            return
        async with self.bot.db.session() as s:
            result = await s.execute(
                delete(QuizQuestion).where(
                    QuizQuestion.id == qid,
                    QuizQuestion.guild_id == interaction.guild_id,
                )
            )
            await s.commit()
        if result.rowcount:
            await interaction.response.send_message(
                f"🗑 Question `#{qid}` removed.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Question `#{qid}` not found in the bank.",
                ephemeral=True,
            )

    # ---------- /quiz-admin setup ----------

    @quiz_admin.command(
        name="setup",
        description="(Owner) Configure /quiz defaults: channel, hall of fame, winner role",
    )
    @app_commands.describe(
        channel="Default channel where /quiz start will post",
        announce_channel="Channel where the hall-of-fame post is announced",
        winner_role="Role auto-granted to users hitting the required win count",
        wins_for_role="Wins required to earn winner_role (1-100)",
    )
    async def quiz_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        announce_channel: discord.TextChannel | None = None,
        winner_role: discord.Role | None = None,
        wins_for_role: app_commands.Range[int, 1, 100] | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            if channel is not None:
                cfg.quiz_channel_id = channel.id
            if announce_channel is not None:
                cfg.quiz_announce_channel_id = announce_channel.id
            if winner_role is not None:
                cfg.quiz_winner_role_id = winner_role.id
            if wins_for_role is not None:
                cfg.quiz_wins_for_role = int(wins_for_role)
            await s.commit()
            cfg = await s.get(GuildConfig, interaction.guild_id)

        emb = discord.Embed(
            title="🎯 /quiz configuration",
            color=discord.Color.blurple(),
        )
        ch = f"<#{cfg.quiz_channel_id}>" if cfg and cfg.quiz_channel_id else "—"
        ach = (
            f"<#{cfg.quiz_announce_channel_id}>"
            if cfg and cfg.quiz_announce_channel_id else "—"
        )
        wr = f"<@&{cfg.quiz_winner_role_id}>" if cfg and cfg.quiz_winner_role_id else "—"
        wins = str(cfg.quiz_wins_for_role) if cfg and cfg.quiz_wins_for_role else "—"
        emb.add_field(name="Default channel", value=ch, inline=True)
        emb.add_field(name="Hall-of-fame channel", value=ach, inline=True)
        emb.add_field(name="Winner role", value=wr, inline=True)
        emb.add_field(name="Wins for role", value=wins, inline=True)
        await interaction.response.send_message(
            embed=emb,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /quiz start ----------

    @quiz.command(
        name="start",
        description="(Admin) Create a quiz session — random questions + controller card",
    )
    @app_commands.describe(
        num_questions="Number of questions this round (1–30)",
        seconds_per_question="Time per question (5–120 seconds)",
        channel="Override the channel to post in (default: from /quiz-admin setup)",
    )
    async def quiz_start(
        self,
        interaction: discord.Interaction,
        num_questions: app_commands.Range[int, 1, 30],
        seconds_per_question: app_commands.Range[int, 5, 120] = 20,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild_id is not None

        # Resolve target channel: explicit param > guild default
        if channel is None:
            async with self.bot.db.session() as s:
                cfg = await s.get(GuildConfig, interaction.guild_id)
            default_id = cfg.quiz_channel_id if cfg else None
            if default_id is None:
                await interaction.response.send_message(
                    "❌ No default channel set. Admin: run "
                    "`/quiz-admin setup channel:#<channel>` first, "
                    "or pass `channel:` to this command.",
                    ephemeral=True,
                )
                return
            resolved = interaction.guild.get_channel(default_id) if interaction.guild else None
            if not isinstance(resolved, discord.TextChannel):
                await interaction.response.send_message(
                    "❌ Default channel is no longer valid (deleted or bot lost access). "
                    "Re-run `/quiz-admin setup` to pick a new channel.",
                    ephemeral=True,
                )
                return
            channel = resolved

        async with self.bot.db.session() as s:
            count = await s.scalar(
                select(func.count(QuizQuestion.id)).where(
                    QuizQuestion.guild_id == interaction.guild_id
                )
            )
            if (count or 0) < num_questions:
                await interaction.response.send_message(
                    f"❌ Bank only has {count or 0} questions, not enough for {num_questions}. "
                    "Add more with `/quiz-admin add` first.",
                    ephemeral=True,
                )
                return
            rows = await s.execute(
                select(QuizQuestion)
                .where(QuizQuestion.guild_id == interaction.guild_id)
                .order_by(func.random())
                .limit(num_questions)
            )
            picked = list(rows.scalars().all())

            session = QuizSession(
                guild_id=interaction.guild_id,
                channel_id=channel.id,
                creator_id=interaction.user.id,
                num_questions=num_questions,
                seconds_per_question=seconds_per_question,
                status="waiting",
            )
            s.add(session)
            await s.commit()
            await s.refresh(session)

            for i, q in enumerate(picked):
                s.add(
                    QuizSessionQuestion(
                        session_id=session.id,
                        question_id=q.id,
                        order_index=i,
                    )
                )
            await s.commit()
            session_id = session.id

        # Post controller message
        embed = discord.Embed(
            title="🎯 Quiz getting ready",
            description=(
                f"**{num_questions}** questions — **{seconds_per_question}s/question**\n"
                f"Created by: {interaction.user.mention}\n\n"
                "Press **Start Quiz** to begin. The timer starts on click."
            ),
            color=discord.Color.gold(),
        )
        view = build_controller_view(session_id)
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot lacks permission to post in that channel.", ephemeral=True
            )
            return

        async with self.bot.db.session() as s:
            sess = await s.get(QuizSession, session_id)
            if sess is not None:
                sess.controller_message_id = msg.id
                await s.commit()

        if channel.id == interaction.channel_id:
            await interaction.response.send_message(
                f"✅ Quiz #{session_id} created ↑", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ Quiz #{session_id} created in {channel.mention}", ephemeral=True
            )

    # ---------- handlers ----------

    async def handle_start_click(
        self, interaction: discord.Interaction, session_id: int
    ) -> None:
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(QuizSession)
                .where(QuizSession.id == session_id)
                .options(selectinload(QuizSession.questions))
            )
            if session is None:
                await interaction.response.send_message(
                    "Quiz session doesn't exist.", ephemeral=True
                )
                return
            if interaction.user.id != session.creator_id and not (
                isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.manage_guild
            ):
                await interaction.response.send_message(
                    "❌ Only the creator (or an admin) can press Start.", ephemeral=True
                )
                return
            if session.status != "waiting":
                await interaction.response.send_message(
                    f"Quiz is already **{session.status}**, can't restart.",
                    ephemeral=True,
                )
                return
            if not session.questions:
                await interaction.response.send_message(
                    "❌ This session has no questions.", ephemeral=True
                )
                return
            session.status = "running"
            session.started_at = datetime.now(timezone.utc)
            session.current_index = 0
            await s.commit()
            # Load the first session question with its referenced QuizQuestion.
            first_sq = await s.scalar(
                select(QuizSessionQuestion)
                .where(
                    QuizSessionQuestion.session_id == session_id,
                    QuizSessionQuestion.order_index == 0,
                )
                .options(selectinload(QuizSessionQuestion.question))
            )

        if first_sq is None:
            await interaction.response.send_message(
                "❌ Couldn't load the first question.", ephemeral=True
            )
            return

        # Replace the controller embed with question 1 in-place. The same
        # message will be edited for every subsequent question and finally
        # for the scoreboard.
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=session.seconds_per_question
        )
        embed = _build_question_embed(session, first_sq, deadline_at=deadline)
        view = build_question_view(session_id, first_sq.id)
        await interaction.response.edit_message(embed=embed, view=view)

        # Kick off the loop. It skips drawing question 0 (already on screen)
        # but still runs its timer + reveal + advance.
        task = asyncio.create_task(self._run_session(session_id))
        self._tasks[session_id] = task

    async def handle_answer_click(
        self,
        interaction: discord.Interaction,
        session_id: int,
        sq_id: int,
        letter: str,
    ) -> None:
        async with self.bot.db.session() as s:
            session = await s.get(QuizSession, session_id)
            sq = await s.get(QuizSessionQuestion, sq_id)
            if session is None or sq is None:
                await interaction.response.send_message(
                    "This question is no longer valid.", ephemeral=True
                )
                return
            if session.status != "running" or session.current_index != sq.order_index:
                await interaction.response.send_message(
                    "⏱ This question is closed. Wait for the next one.", ephemeral=True
                )
                return
            existing = await s.scalar(
                select(QuizAnswer).where(
                    QuizAnswer.session_id == session_id,
                    QuizAnswer.question_id == sq.question_id,
                    QuizAnswer.user_id == interaction.user.id,
                )
            )
            if existing is not None:
                await interaction.response.send_message(
                    f"❌ You already picked **{existing.choice}** — can't change it.",
                    ephemeral=True,
                )
                return
            s.add(
                QuizAnswer(
                    session_id=session_id,
                    question_id=sq.question_id,
                    user_id=interaction.user.id,
                    choice=letter,
                )
            )
            await s.commit()
        await interaction.response.send_message(
            f"✅ Answer recorded: **{letter}**", ephemeral=True
        )
        # Remember this interaction so the ack ephemeral can be auto-deleted
        # right before the reveal, keeping the user's chat clean.
        self._answer_acks.setdefault((session_id, sq_id), []).append(interaction)

    # ---------- core loop ----------

    async def _run_session(self, session_id: int) -> None:
        try:
            await self._run_session_inner(session_id)
        except asyncio.CancelledError:
            log.info("Quiz session %s cancelled", session_id)
        except Exception:
            log.exception("Quiz session %s crashed", session_id)
        finally:
            self._tasks.pop(session_id, None)

    async def _run_session_inner(self, session_id: int) -> None:
        """Drive the session by editing one single message across all questions.

        On entry, the controller message has already been transformed into
        question #0 by handle_start_click. We just timer + reveal + advance.
        """
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(QuizSession)
                .where(QuizSession.id == session_id)
                .options(selectinload(QuizSession.questions))
            )
            if session is None or session.status != "running":
                return
            channel_id = session.channel_id
            msg_id = session.controller_message_id
            sqs = list(session.questions)

        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning("Quiz %s: channel %s not usable", session_id, channel_id)
            return
        if msg_id is None:
            log.warning("Quiz %s: no controller_message_id stored", session_id)
            return
        try:
            qmsg = await channel.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden) as e:
            log.warning("Quiz %s: cannot fetch quiz message: %s", session_id, e)
            return

        for idx, sq in enumerate(sqs):
            # Reload session to honor mid-game state changes.
            async with self.bot.db.session() as s:
                session = await s.get(QuizSession, session_id)
                if session is None or session.status != "running":
                    return
                session.current_index = idx
                await s.commit()
                sq_fresh = await s.scalar(
                    select(QuizSessionQuestion)
                    .where(QuizSessionQuestion.id == sq.id)
                    .options(selectinload(QuizSessionQuestion.question))
                )
            if sq_fresh is None:
                continue

            # Question #0 was drawn by handle_start_click — skip the edit.
            # Question #1+ replaces the previous reveal in the same message.
            if idx > 0:
                deadline = datetime.now(timezone.utc) + timedelta(
                    seconds=session.seconds_per_question
                )
                embed = _build_question_embed(
                    session, sq_fresh, deadline_at=deadline
                )
                view = build_question_view(session_id, sq_fresh.id)
                try:
                    await qmsg.edit(embed=embed, view=view)
                except discord.HTTPException as e:
                    log.warning(
                        "Quiz %s: edit q%s failed: %s", session_id, idx, e
                    )
                    return

            # Reuse the existing column for the message id so /quiz history
            # can reference where each question ran (same id for all here).
            async with self.bot.db.session() as s:
                row = await s.get(QuizSessionQuestion, sq.id)
                if row is not None and row.message_id != qmsg.id:
                    row.message_id = qmsg.id
                    await s.commit()

            # Per-question countdown
            await asyncio.sleep(session.seconds_per_question)

            # Auto-dismiss every user's 'Answer recorded' ack before showing the
            # reveal so their chat is clean for the next question.
            acks = self._answer_acks.pop((session_id, sq_fresh.id), [])
            for ack in acks:
                try:
                    await ack.delete_original_response()
                except (discord.NotFound, discord.HTTPException):
                    pass  # user dismissed it themselves, or token expired

            # Reveal — edit same message
            async with self.bot.db.session() as s:
                ans_rows = await s.execute(
                    select(QuizAnswer).where(
                        QuizAnswer.session_id == session_id,
                        QuizAnswer.question_id == sq_fresh.question_id,
                    )
                )
                answers = list(ans_rows.scalars().all())
            by_choice: dict[str, list[int]] = defaultdict(list)
            for a in answers:
                by_choice[a.choice].append(a.user_id)
            # Only show the 'next question in …' countdown if there IS
            # one. The very last reveal jumps straight to the scoreboard
            # so the live timer would mislead.
            is_last = idx == len(sqs) - 1
            reveal_deadline = (
                None
                if is_last
                else datetime.now(timezone.utc) + timedelta(seconds=_REVEAL_PAUSE_SECONDS)
            )
            reveal_embed = _build_question_embed(
                session, sq_fresh,
                reveal=True,
                answers_by_choice=by_choice,
                deadline_at=reveal_deadline,
            )
            try:
                await qmsg.edit(
                    embed=reveal_embed,
                    view=build_question_view(session_id, sq_fresh.id, disabled=True),
                )
            except discord.HTTPException as e:
                log.warning("Quiz %s: reveal edit failed: %s", session_id, e)

            # Pause between questions
            if idx < len(sqs) - 1:
                await asyncio.sleep(_REVEAL_PAUSE_SECONDS)

        # Grant coin rewards FIRST so we can show each user's prize in
        # the scoreboard. Returns a {user_id: coin_amount} map and the
        # coin icon for rendering.
        rewards_map, coin_icon = await self._grant_rewards_for_session(session_id)
        # Final scoreboard — edit the same message to the leaderboard.
        await self._edit_to_scoreboard(qmsg, session_id, rewards_map, coin_icon)
        # Record winners + announce + possibly grant winner role.
        await self._process_winners(session_id)
        async with self.bot.db.session() as s:
            session = await s.get(QuizSession, session_id)
            if session is not None:
                session.status = "finished"
                session.ended_at = datetime.now(timezone.utc)
                await s.commit()

    async def _grant_rewards_for_session(
        self, session_id: int
    ) -> tuple[dict[int, int], str]:
        """Compute top-1/2/3 + participants, call the economy reward engine
        directly, and return ({user_id: coin_amount}, coin_icon).

        Doing this synchronously (instead of fire-and-forget event) so the
        scoreboard can show each user the prize they just earned. The
        reward engine is idempotent on (game_type, session_id, user, outcome)
        so a duplicate call from anywhere else is a no-op.
        """
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(QuizSession)
                .where(QuizSession.id == session_id)
                .options(selectinload(QuizSession.questions))
            )
            if session is None:
                return {}, ""
            correct_by_q = {
                sq.question_id: sq.question.correct for sq in session.questions
            }
            ans_rows = await s.execute(
                select(QuizAnswer).where(QuizAnswer.session_id == session_id)
            )
            answers = list(ans_rows.scalars().all())
            econ_cfg = await s.get(GuildEconomyConfig, session.guild_id)
        coin_icon = (econ_cfg.coin_icon if econ_cfg and econ_cfg.coin_icon else "🪙")

        scores: dict[int, int] = {}
        for a in answers:
            if correct_by_q.get(a.question_id) == a.choice:
                scores[a.user_id] = scores.get(a.user_id, 0) + 1

        by_score: dict[int, list[int]] = defaultdict(list)
        for uid, sc in scores.items():
            by_score[sc].append(uid)
        ranked = sorted(by_score.keys(), reverse=True)
        top1 = by_score.get(ranked[0], []) if len(ranked) >= 1 else []
        top2 = by_score.get(ranked[1], []) if len(ranked) >= 2 else []
        top3 = by_score.get(ranked[2], []) if len(ranked) >= 3 else []
        placed = set(top1) | set(top2) | set(top3)
        participants = sorted({a.user_id for a in answers} - placed)

        engine = RewardEngine(self.bot)
        try:
            granted = await engine.grant_minigame_rewards(
                MiniGameResult(
                    guild_id=session.guild_id,
                    game_type="quiz",
                    session_id=session_id,
                    top1=top1,
                    top2=top2,
                    top3=top3,
                    participants=participants,
                )
            )
        except Exception:
            log.exception("quiz: grant_minigame_rewards failed")
            return {}, coin_icon
        return {g.user_id: g.amount for g in granted}, coin_icon

    async def _edit_to_scoreboard(
        self,
        message: discord.Message,
        session_id: int,
        rewards_map: dict[int, int] | None = None,
        coin_icon: str = "🪙",
    ) -> None:
        rewards_map = rewards_map or {}
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(QuizSession)
                .where(QuizSession.id == session_id)
                .options(selectinload(QuizSession.questions))
            )
            if session is None:
                return
            correct_by_q = {
                sq.question_id: sq.question.correct for sq in session.questions
            }
            ans_rows = await s.execute(
                select(QuizAnswer).where(QuizAnswer.session_id == session_id)
            )
            answers = list(ans_rows.scalars().all())

        scores: Counter[int] = Counter()
        attempts: Counter[int] = Counter()
        for a in answers:
            attempts[a.user_id] += 1
            if correct_by_q.get(a.question_id) == a.choice:
                scores[a.user_id] += 1

        if not scores and not attempts:
            empty_embed = discord.Embed(
                title="🏁 Quiz ended",
                description="_Nobody participated._",
                color=discord.Color.dark_gray(),
            )
            try:
                await message.edit(embed=empty_embed, view=None)
            except discord.HTTPException as e:
                log.warning(
                    "Quiz %s: empty scoreboard edit failed: %s", session_id, e
                )
            return

        leaderboard = sorted(
            attempts.items(),
            key=lambda kv: (-scores.get(kv[0], 0), -kv[1], kv[0]),
        )
        lines: list[str] = []
        for rank, (uid, _att) in enumerate(leaderboard[:20], start=1):
            medal = (
                "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3
                else f"`#{rank}`"
            )
            prize = rewards_map.get(uid, 0)
            prize_part = (
                f"  •  🎁 `{prize:,}` {coin_icon}" if prize > 0 else ""
            )
            lines.append(
                f"{medal} <@{uid}> — **{scores.get(uid, 0)}**/{session.num_questions} correct"
                f"  ({attempts[uid]} answered){prize_part}"
            )
        embed = discord.Embed(
            title="🏁 Quiz ended — Leaderboard",
            description="\n".join(lines)[:4000] or "_Nobody participated._",
            color=discord.Color.gold(),
        )
        if rewards_map:
            total_paid = sum(rewards_map.values())
            embed.set_footer(
                text=f"Total prizes: {total_paid:,} (paid to each winner's wallet)"
            )
        try:
            await message.edit(embed=embed, view=None)
        except discord.HTTPException as e:
            log.warning("Quiz %s: scoreboard edit failed: %s", session_id, e)

    async def _process_winners(self, session_id: int) -> None:
        """After a session finishes: pick top scorers as winners, log to
        quiz_wins, optionally announce in vinh-danh channel, and grant the
        configured winner role to anyone who has hit the wins threshold."""
        async with self.bot.db.session() as s:
            session = await s.scalar(
                select(QuizSession)
                .where(QuizSession.id == session_id)
                .options(selectinload(QuizSession.questions))
            )
            if session is None:
                return
            correct_by_q = {
                sq.question_id: sq.question.correct for sq in session.questions
            }
            ans_rows = await s.execute(
                select(QuizAnswer).where(QuizAnswer.session_id == session_id)
            )
            answers = list(ans_rows.scalars().all())
            cfg = await s.get(GuildConfig, session.guild_id)

        # Compute per-user correct counts; winner = max score (must be > 0).
        scores: dict[int, int] = {}
        for a in answers:
            if correct_by_q.get(a.question_id) == a.choice:
                scores[a.user_id] = scores.get(a.user_id, 0) + 1
        if not scores:
            return
        max_score = max(scores.values())
        if max_score <= 0:
            return
        winners = [uid for uid, sc in scores.items() if sc == max_score]

        # Log each winner. Ties → multiple rows for this session.
        async with self.bot.db.session() as s:
            for uid in winners:
                s.add(
                    QuizWin(
                        guild_id=session.guild_id,
                        user_id=uid,
                        session_id=session_id,
                    )
                )
            await s.commit()

        # Win count changed → push fresh Scholar leaderboard if any
        # configured. Best-effort: failures here don't block announcement.
        try:
            await self._refresh_cunhan_board(session.guild_id)
        except Exception:
            log.exception("quiz: cunhan board refresh failed")

        # Coin rewards already granted in _grant_rewards_for_session before
        # the scoreboard was rendered; no event dispatch needed.

        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return

        # Announcement
        if cfg and cfg.quiz_announce_channel_id:
            channel = guild.get_channel(cfg.quiz_announce_channel_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                lines: list[str] = []
                async with self.bot.db.session() as s:
                    for uid in winners:
                        total = await s.scalar(
                            select(func.count(QuizWin.id)).where(
                                QuizWin.guild_id == session.guild_id,
                                QuizWin.user_id == uid,
                            )
                        )
                        lines.append(
                            f"🏆 <@{uid}> — **{max_score}/{session.num_questions}** correct "
                            f"• total **{total or 0}** wins"
                        )
                embed = discord.Embed(
                    title="🏆 Vinh Danh — Quiz",
                    description="\n".join(lines),
                    color=discord.Color.gold(),
                )
                try:
                    await channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except discord.Forbidden:
                    log.warning("quiz: forbidden announce in %s", channel.id)

        # Role grant at threshold
        if not cfg or not cfg.quiz_winner_role_id or not cfg.quiz_wins_for_role:
            return
        role = guild.get_role(cfg.quiz_winner_role_id)
        if role is None:
            return
        me = guild.me
        if me is None or role >= me.top_role:
            log.warning(
                "quiz: cannot grant role %s (above bot's top role)", role.id
            )
            return
        threshold = cfg.quiz_wins_for_role
        async with self.bot.db.session() as s:
            for uid in winners:
                total = await s.scalar(
                    select(func.count(QuizWin.id)).where(
                        QuizWin.guild_id == session.guild_id,
                        QuizWin.user_id == uid,
                    )
                )
                if (total or 0) < threshold:
                    continue
                member = guild.get_member(uid)
                if member is None or role in member.roles:
                    continue
                try:
                    await member.add_roles(
                        role, reason=f"Quiz {threshold}+ wins"
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("quiz: failed to grant role to %s: %s", uid, e)


    # =========================================================================
    # /quiz-admin cunhan — Scholar leaderboard (auto-updates on wins)
    # =========================================================================

    cunhan = app_commands.Group(
        name="cunhan",
        description="Scholar leaderboard (top quiz wins)",
        parent=quiz_admin,
    )

    _CUNHAN_I18N: dict[str, dict[str, str]] = {
        LANG_RU: {
            "title":      "🎓  ТАБЛИЦА УЧЁНЫХ  🎓",
            "no_winners": "_Ещё никто не выиграл викторину._\n**Участвуй и займи место!**",
            "win_unit":   "побед",
            "footer":     "{guild} • Топ {n} • Обновляется после каждой викторины",
        },
        LANG_EN: {
            "title":      "🎓  SCHOLAR LEADERBOARD  🎓",
            "no_winners": "_Nobody has won a quiz yet._\n**Jump in and claim a spot!**",
            "win_unit":   "wins",
            "footer":     "{guild} • Top {n} • Auto-updates after every quiz",
        },
    }

    async def _build_cunhan_embed(
        self, guild_id: int, display_count: int, lang: str = LANG_RU,
    ) -> discord.Embed:
        strings = self._CUNHAN_I18N.get(lang, self._CUNHAN_I18N[LANG_RU])
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(
                    QuizWin.user_id, func.count(QuizWin.id).label("wins")
                )
                .where(QuizWin.guild_id == guild_id)
                .group_by(QuizWin.user_id)
                .order_by(func.count(QuizWin.id).desc(), QuizWin.user_id)
                .limit(display_count)
            )
            results = list(rows.all())

        guild = self.bot.get_guild(guild_id)
        guild_name = guild.name if guild else "Server"

        embed = discord.Embed(
            title=strings["title"],
            color=discord.Color.from_rgb(155, 89, 182),  # purple — academic
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
                    f"{medal}  <@{uid}>  •  🏆 `{wins:,}` {strings['win_unit']}"
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
        """Link button to the configured /quiz channel so a leaderboard
        viewer can jump to where rounds are posted. Empty view when no
        channel is set or it's no longer accessible."""
        view = discord.ui.View(timeout=None)
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
        ch_id = cfg.quiz_channel_id if cfg else None
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

    async def _build_cunhan_view(
        self, guild_id: int, lang: str = LANG_RU,
    ) -> discord.ui.View:
        view = await self._build_play_view(guild_id)
        view.add_item(CunhanBoardLangButton(guild_id, lang=lang))
        return view

    async def handle_board_lang_toggle(
        self, interaction: discord.Interaction, guild_id: int,
    ) -> None:
        await interaction.response.defer()
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "quiz",
                )
            )
            if board is None:
                await interaction.followup.send("❌ Board not found.", ephemeral=True)
                return
            new_lang = LANG_EN if getattr(board, "language", LANG_RU) == LANG_RU else LANG_RU
            board.language = new_lang
            display_count = board.display_count
            await s.commit()
        embed = await self._build_cunhan_embed(guild_id, display_count, lang=new_lang)
        view = await self._build_cunhan_view(guild_id, lang=new_lang)
        await interaction.edit_original_response(
            embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _refresh_cunhan_board(self, guild_id: int) -> None:
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "quiz",
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
        embed = await self._build_cunhan_embed(guild_id, board.display_count, lang=lang)
        view = await self._build_cunhan_view(guild_id, lang=lang)
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
                log.warning("cunhan: edit failed for %s: %s", guild_id, e)
            return
        # Message gone — repost in same channel
        try:
            new_msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning("cunhan: forbidden to repost in %s", guild_id)
            return
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "quiz",
                )
            )
            if row is not None:
                row.message_id = new_msg.id
                await s.commit()

    @cunhan.command(
        name="post",
        description="(Admin) Create/replace the Scholar leaderboard in a channel",
    )
    @app_commands.describe(
        channel="Channel to post the board in (any channel — usually hall-of-fame)",
        display_count="Users shown (1-25, default 10)",
    )
    async def cunhan_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        display_count: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        # Delete old board message if any
        async with self.bot.db.session() as s:
            prev = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "quiz",
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

        embed = await self._build_cunhan_embed(
            interaction.guild_id, int(display_count), lang=LANG_RU,
        )
        view = await self._build_cunhan_view(interaction.guild_id, lang=LANG_RU)
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
                    GameWinBoard.game_type == "quiz",
                )
            )
            if board is None:
                board = GameWinBoard(
                    guild_id=interaction.guild_id,
                    game_type="quiz",
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
            f"✅ Scholar leaderboard posted in {channel.mention} (top {display_count}). "
            "It auto-updates after every quiz.",
            ephemeral=True,
        )

    @cunhan.command(
        name="remove",
        description="(Admin) Remove the Scholar leaderboard + stop auto-update",
    )
    async def cunhan_remove(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "quiz",
                )
            )
            if board is None:
                await interaction.followup.send(
                    "No Scholar leaderboard in this server.", ephemeral=True
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
            "✅ Scholar leaderboard removed + auto-update stopped.", ephemeral=True
        )

    @cunhan.command(
        name="refresh",
        description="(Admin) Refresh the Scholar leaderboard immediately",
    )
    async def cunhan_refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "quiz",
                )
            )
        if board is None:
            await interaction.followup.send(
                "No Scholar leaderboard configured. Run `/quiz-admin cunhan post` first.",
                ephemeral=True,
            )
            return
        await self._refresh_cunhan_board(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Scholar leaderboard refreshed in <#{board.channel_id}>.",
            ephemeral=True,
        )


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(Quiz(bot))
