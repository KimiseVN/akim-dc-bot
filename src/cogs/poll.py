"""/poll — event-participation polls with optional weekly recurrence.

Owner-only slash commands:
  /poll create      – post a poll card in a channel now
  /poll edit        – change title / options / close-time on an existing poll
  /poll close       – lock voting early (poll stays visible)
  /poll delete      – hard-remove the poll (and its message)
  /poll list        – list all polls in this guild

Users vote via a SelectMenu (single choice, changing your pick replaces
the previous vote). One vote per user per poll.

Recurrence
----------
When a poll is created with ``repeat: daily | weekly``, the row itself
serves as the recurrence "head" — the scheduler reads only heads and
spawns fresh Poll children whose ``parent_poll_id`` points back. Child
rows are plain ``repeat=once`` polls so they never recurse themselves.
Editing the head propagates to future children (they inherit
title/options/duration at spawn time).

Scheduler
---------
Single background task ticks every 60s over every ``enabled`` head
whose ``next_run_at <= now`` and spawns the next instance. Same design
as the /lfg schedule tick already in the codebase.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from ..db.models import Poll, PollLeaderboard, PollOption, PollVote

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.poll")


# =============================================================================
# Constants + parsing helpers
# =============================================================================

_HHMM_RE = re.compile(r"^([0-2]?\d):([0-5]\d)$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
# Options can be separated by pipe OR newline — pick what feels natural
# in your input method. Whitespace around each option is trimmed.
_OPT_SPLIT_RE = re.compile(r"[|\n]")

_MAX_OPTIONS = 25          # Discord Select cap
_MAX_TITLE_LEN = 200
_MAX_OPT_LABEL_LEN = 100
_DEFAULT_TZ_OFFSET = 7     # +7 = Vietnam
_SCHEDULER_TICK_SECONDS = 60

# How long we coalesce leaderboard refresh requests. Many votes can
# land in quick succession (mid-poll surge) — one edit per 5s window
# keeps us under Discord's per-channel edit budget while still feeling
# live to viewers.
_LEADERBOARD_DEBOUNCE_SECONDS = 5

# How many distinct option-labels the leaderboard shows. Discord embed
# caps at 25 fields; anything above ~8 makes the card scroll-heavy.
_LEADERBOARD_MAX_LABELS = 8
# Fallback per-label ranking depth when no display_count is stored.
_LEADERBOARD_DEFAULT_DEPTH = 10


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    m = _HHMM_RE.match(text.strip())
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    return h, mn


def _parse_date(text: str) -> tuple[int, int, int] | None:
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return y, mo, d


def _tz(offset: int) -> timezone:
    return timezone(timedelta(hours=offset))


def _fmt_tz(offset: int) -> str:
    if offset == 0:
        return "UTC"
    return f"UTC{offset:+d}"


def _parse_options(raw: str) -> list[str]:
    """Split + normalize the pipe/newline-separated options string."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk in _OPT_SPLIT_RE.split(raw):
        label = chunk.strip()
        if not label:
            continue
        if len(label) > _MAX_OPT_LABEL_LEN:
            label = label[:_MAX_OPT_LABEL_LEN]
        # Dedupe case-insensitively — voters shouldn't see "Yes" twice.
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= _MAX_OPTIONS:
            break
    return out


def _compute_next_run(
    now_utc: datetime,
    repeat_kind: str,
    weekday: int | None,
    hour: int,
    minute: int,
    tz_offset: int,
) -> datetime | None:
    """Return the next UTC datetime the recurrence should fire, or None
    when repeat_kind='once'.

    Behaves like the /lfg schedule engine: local wall-clock is fixed to
    (hour:minute) in ``tz_offset`` land; DAILY picks the next day it
    isn't already-past for; WEEKLY jumps to the next matching weekday
    (0=Mon..6=Sun).
    """
    if repeat_kind == "once":
        return None
    tz_local = _tz(tz_offset)
    local_now = now_utc.astimezone(tz_local)
    candidate = local_now.replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    if repeat_kind == "daily":
        if candidate <= local_now:
            candidate += timedelta(days=1)
    elif repeat_kind == "weekly":
        assert weekday is not None
        # Advance to the next matching weekday. If today matches and
        # the time is still in the future, use today.
        for delta_days in range(0, 8):
            probe = candidate + timedelta(days=delta_days)
            if probe.weekday() == weekday and probe > local_now:
                candidate = probe
                break
    else:
        return None
    return candidate.astimezone(timezone.utc)


# =============================================================================
# Card rendering
# =============================================================================


_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _build_poll_embed(
    poll: Poll,
    votes_by_option: dict[int, list[int]],
    creator_mention: str,
) -> discord.Embed:
    """Render the live poll card. votes_by_option: option_id → user_ids."""
    total_votes = sum(len(v) for v in votes_by_option.values())
    if poll.status == "open":
        color = discord.Color.blurple()
        status_line = f"⏰ Closes: <t:{int(poll.close_at.timestamp())}:R>"
    elif poll.status == "closed":
        color = discord.Color.dark_gray()
        status_line = "🔒 Voting closed."
    else:  # cancelled
        color = discord.Color.red()
        status_line = "🛑 Cancelled."

    embed = discord.Embed(
        title=f"📊 {poll.title}",
        color=color,
    )
    parts: list[str] = []
    if poll.description:
        parts.append(poll.description)
    parts.append(f"🗓 **Event:** <t:{int(poll.close_at.timestamp())}:F>")
    parts.append(status_line)
    parts.append(f"👤 Created by {creator_mention}")
    if poll.repeat_kind != "once":
        if poll.repeat_kind == "weekly" and poll.repeat_weekday is not None:
            wd = _WEEKDAY_NAMES[poll.repeat_weekday]
            parts.append(
                f"🔁 Repeat: every **{wd}** at "
                f"`{poll.repeat_hour:02d}:{poll.repeat_minute:02d}` "
                f"({_fmt_tz(poll.tz_offset)})"
            )
        elif poll.repeat_kind == "daily":
            parts.append(
                f"🔁 Repeat: every **day** at "
                f"`{poll.repeat_hour:02d}:{poll.repeat_minute:02d}` "
                f"({_fmt_tz(poll.tz_offset)})"
            )
    embed.description = "\n".join(parts)

    # Options with vote counts. Highlight the leader when the poll has
    # closed so the winning option is obvious at a glance.
    winning_count = -1
    if poll.status == "closed" and votes_by_option:
        winning_count = max(len(v) for v in votes_by_option.values())

    lines: list[str] = []
    for opt in poll.options:
        voters = votes_by_option.get(opt.id, [])
        n = len(voters)
        pct = (n / total_votes * 100) if total_votes > 0 else 0
        bar_len = 20
        filled = int(bar_len * (pct / 100))
        bar = "█" * filled + "░" * (bar_len - filled)
        marker = ""
        if poll.status == "closed" and n == winning_count and n > 0:
            marker = " 🏆"
        lines.append(
            f"**{opt.position + 1}. {opt.label}**{marker}\n"
            f"`{bar}` {n} · {pct:.0f}%"
        )
        if voters:
            preview = ", ".join(f"<@{uid}>" for uid in voters[:5])
            more = f" _(+{len(voters) - 5})_" if len(voters) > 5 else ""
            lines.append(f"　└ {preview}{more}")
    embed.add_field(
        name=f"🗳 Options ({total_votes} vote(s))",
        value="\n".join(lines)[:1024] if lines else "_(no options)_",
        inline=False,
    )
    embed.set_footer(text=f"Poll #{poll.id}")
    return embed


# =============================================================================
# Persistent view: Select + Cancel + Close
# =============================================================================


class PollVoteSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"poll:vote:(?P<poll_id>\d+)",
):
    """The single-choice vote dropdown. Value is the option position
    (0-based) — cheap + stable across edits."""

    def __init__(
        self, poll_id: int, options: list[PollOption], *,
        disabled: bool = False,
    ) -> None:
        self.poll_id = poll_id
        select = discord.ui.Select(
            custom_id=f"poll:vote:{poll_id}",
            placeholder="Pick your option…",
            min_values=1, max_values=1,
            disabled=disabled,
            options=[
                discord.SelectOption(
                    label=opt.label[:100],
                    value=str(opt.position),
                    description=None,
                )
                for opt in options[:_MAX_OPTIONS]
            ] or [
                discord.SelectOption(label="(no options)", value="0")
            ],
        )
        super().__init__(select)

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        # Rehydrate from DB — we need the current option list to
        # accept the vote payload.
        cog: "PollCog" = interaction.client.get_cog("Poll")  # type: ignore[assignment]
        if cog is None:
            return cls(int(match["poll_id"]), [])
        poll_id = int(match["poll_id"])
        opts = await cog._fetch_options(poll_id)
        return cls(poll_id, opts, disabled=item.disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "PollCog" = interaction.client.get_cog("Poll")  # type: ignore[assignment]
        if cog is None:
            return
        raw = self.item.values[0] if self.item.values else None
        try:
            position = int(raw) if raw is not None else None
        except ValueError:
            position = None
        if position is None:
            await interaction.response.send_message(
                "❌ Invalid option.", ephemeral=True,
            )
            return
        await cog.handle_vote(interaction, self.poll_id, position)


class PollCloseButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"poll:close:(?P<poll_id>\d+)",
):
    """Owner-only 🔒 Close — freezes voting, keeps the card visible."""

    def __init__(self, poll_id: int, *, disabled: bool = False) -> None:
        self.poll_id = poll_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"poll:close:{poll_id}",
                label="Close poll",
                emoji="🔒",
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["poll_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "PollCog" = interaction.client.get_cog("Poll")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_close_button(interaction, self.poll_id)


class PollCancelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"poll:cancel:(?P<poll_id>\d+)",
):
    """Owner-only 🛑 Cancel — abandons the poll (marks cancelled)."""

    def __init__(self, poll_id: int, *, disabled: bool = False) -> None:
        self.poll_id = poll_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"poll:cancel:{poll_id}",
                label="Cancel",
                emoji="🛑",
                style=discord.ButtonStyle.danger,
                disabled=disabled,
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["poll_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "PollCog" = interaction.client.get_cog("Poll")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_cancel_button(interaction, self.poll_id)


def _build_poll_view(poll: Poll) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    is_active = poll.status == "open"
    v.add_item(PollVoteSelect(poll.id, list(poll.options), disabled=not is_active))
    v.add_item(PollCloseButton(poll.id, disabled=not is_active))
    v.add_item(PollCancelButton(poll.id, disabled=not is_active))
    return v


# =============================================================================
# Cog
# =============================================================================


_REPEAT_CHOICES = Literal["once", "daily", "weekly"]
_WEEKDAY_CHOICES = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]
_WEEKDAY_INDEX: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


class PollCog(commands.Cog, name="Poll"):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        # Lock per poll_id — serializes vote writes so two clicks on the
        # same select can't race on the "existing vote row" upsert.
        self._locks: dict[int, asyncio.Lock] = {}
        # guild_id → pending leaderboard-refresh task (debounced).
        self._board_tasks: dict[int, asyncio.Task] = {}
        self.scheduler_task.start()

    def cog_unload(self) -> None:
        self.scheduler_task.cancel()
        for task in self._board_tasks.values():
            if not task.done():
                task.cancel()

    poll_group = app_commands.Group(
        name="poll",
        description="Event poll (owner-only)",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ---------------- helpers ----------------

    def _lock(self, poll_id: int) -> asyncio.Lock:
        lock = self._locks.get(poll_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[poll_id] = lock
        return lock

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild is not None
            and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        ):
            return True
        msg = "❌ Only the **server owner** can use this command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    async def _fetch_poll(self, poll_id: int) -> Poll | None:
        async with self.bot.db.session() as s:
            return await s.scalar(
                select(Poll)
                .where(Poll.id == poll_id)
                .options(
                    selectinload(Poll.options),
                    selectinload(Poll.votes),
                )
            )

    async def _fetch_options(self, poll_id: int) -> list[PollOption]:
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(PollOption)
                .where(PollOption.poll_id == poll_id)
                .order_by(PollOption.position)
            )
            return list(rows.scalars().all())

    async def _refresh_card(self, poll_id: int) -> None:
        poll = await self._fetch_poll(poll_id)
        if poll is None or poll.message_id is None:
            return
        guild = self.bot.get_guild(poll.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(poll.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await channel.fetch_message(poll.message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        votes_by_option: dict[int, list[int]] = {}
        for v in poll.votes:
            votes_by_option.setdefault(v.option_id, []).append(v.user_id)
        creator_member = guild.get_member(poll.creator_id)
        creator_mention = (
            creator_member.mention if creator_member else f"<@{poll.creator_id}>"
        )
        embed = _build_poll_embed(poll, votes_by_option, creator_mention)
        view = _build_poll_view(poll)
        try:
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            log.warning("poll refresh failed for #%s: %s", poll_id, e)

    async def _post_poll_card(
        self, poll: Poll, channel: discord.TextChannel | discord.Thread,
    ) -> discord.Message | None:
        """Send the card in the target channel + persist message_id."""
        # Reload with relationships so the embed can render.
        fresh = await self._fetch_poll(poll.id)
        assert fresh is not None
        votes_by_option: dict[int, list[int]] = {}
        creator = channel.guild.get_member(fresh.creator_id)
        creator_mention = (
            creator.mention if creator else f"<@{fresh.creator_id}>"
        )
        embed = _build_poll_embed(fresh, votes_by_option, creator_mention)
        view = _build_poll_view(fresh)
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            log.warning(
                "poll: forbidden to post in channel %s", channel.id,
            )
            return None
        async with self.bot.db.session() as s:
            row = await s.get(Poll, poll.id)
            if row is not None:
                row.message_id = msg.id
                await s.commit()
        return msg

    # ---------------- /poll create ----------------

    @poll_group.command(
        name="create",
        description="Create a new poll in this channel (owner-only)",
    )
    @app_commands.describe(
        title="Poll title (e.g. Dinner tonight?)",
        options=(
            "Options, separated by | or newlines. "
            "Max 25 options, each ≤100 characters."
        ),
        duration_minutes="Voting duration in minutes (default 60).",
        event_date="Event date YYYY-MM-DD (default = today in tz).",
        event_time="Event time HH:MM (default 21:00).",
        tz_offset="Timezone offset (default 7 = VN).",
        repeat="Repeat: once (default), daily, weekly.",
        weekday="Weekday (required when repeat=weekly).",
        description="Extra description (optional).",
        channel="Channel to post the poll in (default: current channel).",
    )
    async def poll_create(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, _MAX_TITLE_LEN],
        options: app_commands.Range[str, 1, 2000],
        duration_minutes: app_commands.Range[int, 1, 60 * 24 * 30] = 60,
        event_date: app_commands.Range[str, 8, 10] | None = None,
        event_time: app_commands.Range[str, 4, 5] = "21:00",
        tz_offset: app_commands.Range[int, -12, 14] = _DEFAULT_TZ_OFFSET,
        repeat: _REPEAT_CHOICES = "once",
        weekday: _WEEKDAY_CHOICES | None = None,
        description: app_commands.Range[str, 1, 1000] | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return

        # Validate options
        opts = _parse_options(options)
        if len(opts) < 2:
            await interaction.followup.send(
                "❌ Need at least 2 options (separated by `|` or newlines).",
                ephemeral=True,
            )
            return

        # Validate time inputs
        hm = _parse_hhmm(event_time)
        if hm is None:
            await interaction.followup.send(
                "❌ `event_time` must be 24h `HH:MM`.", ephemeral=True,
            )
            return
        event_hour, event_min = hm

        # Compute close_at (== event time in the requested tz).
        tz_local = _tz(tz_offset)
        if event_date is not None:
            parsed = _parse_date(event_date)
            if parsed is None:
                await interaction.followup.send(
                    "❌ `event_date` must be `YYYY-MM-DD`.", ephemeral=True,
                )
                return
            y, mo, d = parsed
            try:
                local_dt = datetime(
                    y, mo, d, event_hour, event_min, tzinfo=tz_local,
                )
            except ValueError as e:
                await interaction.followup.send(
                    f"❌ Invalid date: `{e}`.", ephemeral=True,
                )
                return
        else:
            # Default: today at HH:MM in tz; if past, roll forward one day.
            now_local = datetime.now(tz_local)
            local_dt = now_local.replace(
                hour=event_hour, minute=event_min, second=0, microsecond=0,
            )
            if local_dt <= now_local:
                local_dt += timedelta(days=1)
        close_at = local_dt.astimezone(timezone.utc)

        # Recurrence checks.
        weekday_idx: int | None = None
        if repeat == "weekly":
            if weekday is None:
                await interaction.followup.send(
                    "❌ `repeat=weekly` requires a `weekday`.", ephemeral=True,
                )
                return
            weekday_idx = _WEEKDAY_INDEX[weekday]
        next_run_at = _compute_next_run(
            datetime.now(timezone.utc), repeat,
            weekday_idx, event_hour, event_min, tz_offset,
        )

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                "❌ Invalid channel.", ephemeral=True,
            )
            return

        # Insert poll + options in one transaction.
        async with self.bot.db.session() as s:
            poll = Poll(
                guild_id=interaction.guild_id,
                channel_id=target_channel.id,
                creator_id=interaction.user.id,
                title=title.strip(),
                description=description.strip() if description else None,
                close_at=close_at,
                status="open",
                repeat_kind=repeat,
                repeat_weekday=weekday_idx,
                repeat_hour=event_hour if repeat != "once" else None,
                repeat_minute=event_min if repeat != "once" else None,
                tz_offset=tz_offset,
                duration_minutes=int(duration_minutes),
                next_run_at=next_run_at,
                enabled=True,
            )
            s.add(poll)
            await s.flush()
            for i, label in enumerate(opts):
                s.add(PollOption(
                    poll_id=poll.id, position=i, label=label,
                ))
            await s.commit()
            poll_id = poll.id

        # Post the card.
        fresh = await self._fetch_poll(poll_id)
        assert fresh is not None
        msg = await self._post_poll_card(fresh, target_channel)
        if msg is None:
            await interaction.followup.send(
                f"❌ Couldn't post in {target_channel.mention}. "
                f"Delete poll #{poll_id} with `/poll delete`.",
                ephemeral=True,
            )
            return

        ack = [f"✅ Poll `#{poll_id}` created in {target_channel.mention}."]
        if repeat != "once":
            if next_run_at is not None:
                ack.append(
                    f"🔁 Repeat `{repeat}` — next: "
                    f"<t:{int(next_run_at.timestamp())}:F>"
                )
        await interaction.followup.send("\n".join(ack), ephemeral=True)

    # ---------------- /poll edit ----------------

    @poll_group.command(
        name="edit",
        description="Edit an existing poll (only for polls that aren't closed)",
    )
    @app_commands.describe(
        poll_id="Poll ID (see /poll list)",
        title="Change the title (optional).",
        options=(
            "Replace all options — will DELETE existing votes. "
            "Leave blank to keep."
        ),
        description="Change description. Pass `-` to clear.",
        close_at_extra_minutes=(
            "Shift the deadline in minutes (negative to shorten). e.g. 30 = +30 minutes."
        ),
    )
    async def poll_edit(
        self,
        interaction: discord.Interaction,
        poll_id: int,
        title: app_commands.Range[str, 1, _MAX_TITLE_LEN] | None = None,
        options: app_commands.Range[str, 1, 2000] | None = None,
        description: app_commands.Range[str, 1, 1000] | None = None,
        close_at_extra_minutes: app_commands.Range[int, -60 * 24 * 30, 60 * 24 * 30] | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            poll = await s.get(Poll, poll_id)
            if poll is None or poll.guild_id != interaction.guild_id:
                await interaction.followup.send(
                    f"❌ Poll `#{poll_id}` not found in this server.",
                    ephemeral=True,
                )
                return
            if poll.status == "cancelled":
                await interaction.followup.send(
                    "❌ Poll was cancelled — can't be edited.", ephemeral=True,
                )
                return
            changes: list[str] = []
            if title is not None:
                poll.title = title.strip()
                changes.append("title")
            if description is not None:
                poll.description = None if description == "-" else description.strip()
                changes.append("description")
            if close_at_extra_minutes is not None and close_at_extra_minutes != 0:
                poll.close_at = poll.close_at + timedelta(
                    minutes=int(close_at_extra_minutes),
                )
                changes.append("close_at")
                # Reopen if we've pushed the deadline back into the future.
                if (
                    poll.status == "closed"
                    and poll.close_at > datetime.now(timezone.utc)
                ):
                    poll.status = "open"
                    changes.append("status→open")
            if options is not None:
                new_opts = _parse_options(options)
                if len(new_opts) < 2:
                    await interaction.followup.send(
                        "❌ Need at least 2 options.", ephemeral=True,
                    )
                    return
                # Wipe old options + their FK votes.
                await s.execute(
                    delete(PollOption).where(PollOption.poll_id == poll_id)
                )
                await s.flush()
                for i, label in enumerate(new_opts):
                    s.add(PollOption(
                        poll_id=poll_id, position=i, label=label,
                    ))
                changes.append(f"options({len(new_opts)}) — votes reset")
            if not changes:
                await interaction.followup.send(
                    "⚠️ No changes provided.", ephemeral=True,
                )
                return
            await s.commit()

        await self._refresh_card(poll_id)
        # If options were replaced, votes cascade-deleted → leaderboard
        # totals shifted. Nudge it.
        if any(c.startswith("options(") for c in changes):
            self._schedule_leaderboard_refresh(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Poll `#{poll_id}` updated: {', '.join(changes)}.",
            ephemeral=True,
        )

    # ---------------- /poll delete ----------------

    @poll_group.command(
        name="delete",
        description="Delete a poll from DB + remove the message from the channel",
    )
    @app_commands.describe(poll_id="Poll ID to delete")
    async def poll_delete(
        self,
        interaction: discord.Interaction,
        poll_id: int,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            poll = await s.get(Poll, poll_id)
            if poll is None or poll.guild_id != interaction.guild_id:
                await interaction.followup.send(
                    f"❌ Poll `#{poll_id}` not found.",
                    ephemeral=True,
                )
                return
            channel_id = poll.channel_id
            message_id = poll.message_id
            await s.delete(poll)
            await s.commit()

        # Best-effort message cleanup.
        if message_id is not None and interaction.guild is not None:
            ch = interaction.guild.get_channel(channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await ch.fetch_message(message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        # Poll gone → its votes cascade-deleted → totals in the
        # aggregate drop. Nudge the leaderboard.
        self._schedule_leaderboard_refresh(interaction.guild_id)
        await interaction.followup.send(
            f"🗑 Poll `#{poll_id}` deleted.", ephemeral=True,
        )

    # ---------------- /poll close ----------------

    @poll_group.command(
        name="close",
        description="Close a poll early (keeps the card, locks voting)",
    )
    @app_commands.describe(poll_id="Poll ID to close")
    async def poll_close(
        self,
        interaction: discord.Interaction,
        poll_id: int,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        await self._close_poll(poll_id, guild_id=interaction.guild_id)
        await interaction.followup.send(
            f"🔒 Poll `#{poll_id}` closed.", ephemeral=True,
        )

    async def _close_poll(self, poll_id: int, *, guild_id: int) -> None:
        async with self.bot.db.session() as s:
            poll = await s.get(Poll, poll_id)
            if poll is None or poll.guild_id != guild_id:
                return
            if poll.status != "open":
                return
            poll.status = "closed"
            await s.commit()
        await self._refresh_card(poll_id)

    # ---------------- /poll list ----------------

    @poll_group.command(
        name="list",
        description="List all polls in this server",
    )
    async def poll_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(Poll)
                .where(Poll.guild_id == interaction.guild_id)
                .order_by(Poll.id.desc())
                .limit(50)
            )
            polls = list(rows.scalars().all())
            # Tally votes per poll in one query.
            counts = await s.execute(
                select(PollVote.poll_id, func.count(PollVote.id))
                .where(PollVote.poll_id.in_([p.id for p in polls]) if polls else False)
                .group_by(PollVote.poll_id)
            )
            count_map = {row[0]: row[1] for row in counts.all()}

        if not polls:
            await interaction.followup.send(
                "No polls yet. Use `/poll create` to make one.",
                ephemeral=True,
            )
            return
        lines = [f"**{len(polls)} most recent polls:**"]
        for p in polls:
            status_emoji = {
                "open": "🟢", "closed": "⚪", "cancelled": "🛑",
            }.get(p.status, "❓")
            repeat_tag = ""
            if p.repeat_kind != "once":
                repeat_tag = f" 🔁 {p.repeat_kind}"
            votes = count_map.get(p.id, 0)
            lines.append(
                f"{status_emoji} `#{p.id}` **{p.title[:50]}** — "
                f"{votes} vote · <#{p.channel_id}>{repeat_tag}"
            )
        await interaction.followup.send(
            "\n".join(lines)[:2000], ephemeral=True,
        )

    # ---------------- vote handler ----------------

    async def handle_vote(
        self,
        interaction: discord.Interaction,
        poll_id: int,
        position: int,
    ) -> None:
        async with self._lock(poll_id):
            async with self.bot.db.session() as s:
                poll = await s.get(Poll, poll_id)
                if poll is None:
                    await interaction.response.send_message(
                        "❌ Poll no longer exists.", ephemeral=True,
                    )
                    return
                if poll.status != "open":
                    await interaction.response.send_message(
                        f"⏱ Poll is **{poll.status}** — no more votes accepted.",
                        ephemeral=True,
                    )
                    return
                if datetime.now(timezone.utc) >= poll.close_at:
                    # Deadline already passed — flip to closed & bail.
                    poll.status = "closed"
                    await s.commit()
                    await interaction.response.send_message(
                        "⏱ Voting deadline has passed.", ephemeral=True,
                    )
                    await self._refresh_card(poll_id)
                    return
                opt = await s.scalar(
                    select(PollOption).where(
                        PollOption.poll_id == poll_id,
                        PollOption.position == position,
                    )
                )
                if opt is None:
                    await interaction.response.send_message(
                        "❌ Option no longer exists — may have just been edited.",
                        ephemeral=True,
                    )
                    return
                existing = await s.scalar(
                    select(PollVote).where(
                        PollVote.poll_id == poll_id,
                        PollVote.user_id == interaction.user.id,
                    )
                )
                if existing is None:
                    s.add(PollVote(
                        poll_id=poll_id,
                        user_id=interaction.user.id,
                        option_id=opt.id,
                    ))
                    ack = f"✅ Vote recorded: **{opt.label}**"
                elif existing.option_id == opt.id:
                    ack = f"⏭ You already picked **{opt.label}**."
                else:
                    existing.option_id = opt.id
                    existing.voted_at = datetime.now(timezone.utc)
                    ack = f"🔁 Vote changed to **{opt.label}**"
                await s.commit()
                guild_id = poll.guild_id

        await interaction.response.send_message(ack, ephemeral=True)
        await self._refresh_card(poll_id)
        # Vote count changed → nudge the guild's leaderboard card.
        # Debounced so a burst of votes only produces one edit.
        self._schedule_leaderboard_refresh(guild_id)

    # ---------------- button handlers ----------------

    async def handle_close_button(
        self, interaction: discord.Interaction, poll_id: int,
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self._close_poll(poll_id, guild_id=interaction.guild_id)
        await interaction.followup.send(
            f"🔒 Poll `#{poll_id}` closed.", ephemeral=True,
        )

    async def handle_cancel_button(
        self, interaction: discord.Interaction, poll_id: int,
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            poll = await s.get(Poll, poll_id)
            if poll is None or poll.guild_id != interaction.guild_id:
                await interaction.followup.send(
                    "❌ Poll no longer exists.", ephemeral=True,
                )
                return
            if poll.status != "open":
                await interaction.followup.send(
                    f"Poll is **{poll.status}** — can no longer be cancelled.",
                    ephemeral=True,
                )
                return
            poll.status = "cancelled"
            await s.commit()
        await self._refresh_card(poll_id)
        await interaction.followup.send(
            f"🛑 Poll `#{poll_id}` cancelled.", ephemeral=True,
        )

    # ---------------- scheduler ----------------

    @tasks.loop(seconds=_SCHEDULER_TICK_SECONDS)
    async def scheduler_task(self) -> None:
        """Tick every minute. Two jobs:
          1. Auto-close polls whose close_at has passed.
          2. Fire the next recurring instance for polls whose
             next_run_at has passed.
        """
        now = datetime.now(timezone.utc)
        try:
            await self._auto_close_expired(now)
            await self._fire_recurring(now)
        except Exception:
            log.exception("poll scheduler tick failed")

    @scheduler_task.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def _auto_close_expired(self, now: datetime) -> None:
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(Poll).where(
                    Poll.status == "open",
                    Poll.close_at <= now,
                )
            )
            expired = list(rows.scalars().all())
            for p in expired:
                p.status = "closed"
            if expired:
                await s.commit()
        for p in expired:
            try:
                await self._refresh_card(p.id)
            except Exception:
                log.exception("poll auto-close refresh failed for %s", p.id)

    async def _fire_recurring(self, now: datetime) -> None:
        """Spawn new child polls for any head whose next_run_at is due.

        A head = repeat_kind != 'once' AND parent_poll_id IS NULL AND
        enabled = True. Children never get scheduled themselves.
        """
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(Poll)
                .where(
                    Poll.repeat_kind != "once",
                    Poll.parent_poll_id.is_(None),
                    Poll.enabled == True,  # noqa: E712
                    Poll.next_run_at.is_not(None),
                    Poll.next_run_at <= now,
                )
                .options(selectinload(Poll.options))
            )
            heads = list(rows.scalars().all())

        for head in heads:
            try:
                await self._spawn_recurring_child(head)
            except Exception:
                log.exception("poll: recurring spawn failed for head %s", head.id)
            # Advance the head's next_run_at regardless — even a failed
            # spawn shouldn't loop the same tick over and over.
            new_next = _compute_next_run(
                datetime.now(timezone.utc),
                head.repeat_kind,
                head.repeat_weekday,
                head.repeat_hour or 0,
                head.repeat_minute or 0,
                head.tz_offset,
            )
            async with self.bot.db.session() as s:
                row = await s.get(Poll, head.id)
                if row is not None:
                    row.next_run_at = new_next
                    await s.commit()

    async def _spawn_recurring_child(self, head: Poll) -> None:
        """Create a fresh child Poll cloned from head + post its card."""
        guild = self.bot.get_guild(head.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(head.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning(
                "poll: head %s channel %s missing — skip",
                head.id, head.channel_id,
            )
            return

        # Child close_at = now + duration_minutes.
        close_at = datetime.now(timezone.utc) + timedelta(
            minutes=head.duration_minutes,
        )
        async with self.bot.db.session() as s:
            child = Poll(
                guild_id=head.guild_id,
                channel_id=head.channel_id,
                creator_id=head.creator_id,
                title=head.title,
                description=head.description,
                close_at=close_at,
                status="open",
                repeat_kind="once",  # children never recurse
                tz_offset=head.tz_offset,
                duration_minutes=head.duration_minutes,
                parent_poll_id=head.id,
                enabled=True,
            )
            s.add(child)
            await s.flush()
            # Clone options in order.
            for opt in head.options:
                s.add(PollOption(
                    poll_id=child.id,
                    position=opt.position,
                    label=opt.label,
                ))
            await s.commit()
            child_id = child.id

        fresh = await self._fetch_poll(child_id)
        assert fresh is not None
        await self._post_poll_card(fresh, channel)
        log.info(
            "poll: spawned child #%s from head #%s (close_at=%s)",
            child_id, head.id, close_at.isoformat(),
        )

    # ================================================================
    # /poll rank — aggregated vote leaderboard (auto-updating card)
    # ================================================================

    rank = app_commands.Group(
        name="rank",
        description="Poll vote leaderboard",
        parent=poll_group,
    )

    @rank.command(
        name="post",
        description="(Owner) Post the poll vote leaderboard in a channel",
    )
    @app_commands.describe(
        channel="Channel to post the leaderboard in",
        display_count="Users shown per option (1-25, default 10)",
    )
    async def rank_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        display_count: app_commands.Range[int, 1, 25] = _LEADERBOARD_DEFAULT_DEPTH,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return

        # If there's an existing board (any channel), delete the old
        # message so we don't leave a stale ranking floating around.
        async with self.bot.db.session() as s:
            prev = await s.get(PollLeaderboard, interaction.guild_id)
        if (
            prev is not None
            and prev.message_id is not None
            and interaction.guild is not None
        ):
            prev_ch = interaction.guild.get_channel(prev.channel_id)
            if isinstance(prev_ch, (discord.TextChannel, discord.Thread)):
                try:
                    old_msg = await prev_ch.fetch_message(prev.message_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        embed = await self._build_leaderboard_embed(
            interaction.guild_id, int(display_count),
        )
        try:
            msg = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {channel.mention}.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            board = await s.get(PollLeaderboard, interaction.guild_id)
            if board is None:
                board = PollLeaderboard(
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
            f"✅ Poll vote leaderboard posted in {channel.mention} "
            f"(top {display_count} per option). "
            "The board auto-updates whenever a new vote lands.",
            ephemeral=True,
        )

    @rank.command(
        name="remove",
        description="(Owner) Remove the leaderboard + stop auto-update",
    )
    async def rank_remove(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.get(PollLeaderboard, interaction.guild_id)
            if board is None:
                await interaction.followup.send(
                    "No leaderboard configured in this server.", ephemeral=True,
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
        # Cancel any pending debounced refresh so it can't recreate
        # a card after we've told the user it's gone.
        pending = self._board_tasks.pop(interaction.guild_id, None)
        if pending is not None and not pending.done():
            pending.cancel()
        await interaction.followup.send(
            "✅ Leaderboard removed and auto-update stopped.", ephemeral=True,
        )

    @rank.command(
        name="refresh",
        description="(Owner) Refresh the leaderboard immediately",
    )
    async def rank_refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.get(PollLeaderboard, interaction.guild_id)
        if board is None:
            await interaction.followup.send(
                "No leaderboard yet. Run `/poll rank post` first.",
                ephemeral=True,
            )
            return
        await self._refresh_leaderboard_now(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Leaderboard refreshed in <#{board.channel_id}>.",
            ephemeral=True,
        )

    # ---------------- aggregation + embed ----------------

    async def _fetch_vote_aggregate(
        self, guild_id: int,
    ) -> dict[str, dict[str, dict]]:
        """Return {label_key: {"display": str, "users": {uid: votes}}}.

        Grouping is case-insensitive on option label so "Yes" and "yes"
        (same word, different casing across polls) share a bucket. We
        keep MIN(label) as the display representative — arbitrary but
        stable across refreshes.
        """
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(
                    func.lower(PollOption.label).label("label_key"),
                    func.min(PollOption.label).label("display_label"),
                    PollVote.user_id,
                    func.count(PollVote.id).label("votes"),
                )
                .join(PollOption, PollOption.id == PollVote.option_id)
                .join(Poll, Poll.id == PollVote.poll_id)
                .where(Poll.guild_id == guild_id)
                .group_by(
                    func.lower(PollOption.label),
                    PollVote.user_id,
                )
            )
            data = rows.all()
        buckets: dict[str, dict] = {}
        for label_key, display, user_id, votes in data:
            entry = buckets.setdefault(
                label_key, {"display": display, "users": {}},
            )
            entry["users"][user_id] = int(votes)
        return buckets

    async def _build_leaderboard_embed(
        self, guild_id: int, display_count: int,
    ) -> discord.Embed:
        buckets = await self._fetch_vote_aggregate(guild_id)
        guild = self.bot.get_guild(guild_id)
        guild_name = guild.name if guild else "Server"

        embed = discord.Embed(
            title="🏆 POLL VOTE LEADERBOARD",
            color=discord.Color.from_rgb(52, 152, 219),  # calm blue
        )
        if guild is not None and guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)

        if not buckets:
            embed.description = (
                "_No votes yet in this server._\n"
                "Owner: create a poll with `/poll create` to get people voting!"
            )
        else:
            # Sort labels by total-votes desc — most popular option
            # comes first. Cap the number of labels shown so the embed
            # doesn't exceed Discord's 25-field limit.
            labels_sorted = sorted(
                buckets.items(),
                key=lambda kv: sum(kv[1]["users"].values()),
                reverse=True,
            )[:_LEADERBOARD_MAX_LABELS]

            for _, bucket in labels_sorted:
                display = bucket["display"]
                users_map = bucket["users"]
                total = sum(users_map.values())
                top_users = sorted(
                    users_map.items(), key=lambda x: x[1], reverse=True,
                )[:display_count]
                lines: list[str] = []
                for rank_idx, (uid, votes) in enumerate(top_users, start=1):
                    if rank_idx == 1:
                        medal = "🥇"
                    elif rank_idx == 2:
                        medal = "🥈"
                    elif rank_idx == 3:
                        medal = "🥉"
                    else:
                        medal = f"`#{rank_idx:>2}`"
                    lines.append(f"{medal} <@{uid}> — **{votes}** vote")
                if not lines:
                    continue
                embed.add_field(
                    name=f"🗳 {display} — {total} vote(s)",
                    value="\n".join(lines)[:1024],
                    inline=False,
                )
            total_all = sum(
                sum(b["users"].values()) for b in buckets.values()
            )
            unique_voters = len({
                uid for b in buckets.values() for uid in b["users"]
            })
            embed.description = (
                f"📊 Total **{total_all:,}** votes from "
                f"**{unique_voters:,}** users, "
                f"across **{len(buckets)}** distinct options."
            )

        embed.set_footer(
            text=(
                f"{guild_name} • Top {display_count} per option • "
                "Auto-updates on new votes / polls"
            )
        )
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    async def _refresh_leaderboard_now(self, guild_id: int) -> None:
        """Edit-in-place refresh of the leaderboard card. No-op when
        the guild hasn't posted a board yet."""
        async with self.bot.db.session() as s:
            board = await s.get(PollLeaderboard, guild_id)
        if board is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(board.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        embed = await self._build_leaderboard_embed(
            guild_id, board.display_count,
        )
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
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as e:
                log.warning(
                    "poll rank refresh edit failed for %s: %s",
                    guild_id, e,
                )
            return
        # Message vanished (deleted, purged, or bot lost permission
        # briefly). Repost + persist new message_id so future refreshes
        # target the new one.
        try:
            new_msg = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning(
                "poll rank refresh: forbidden to repost in %s", guild_id,
            )
            return
        async with self.bot.db.session() as s:
            row = await s.get(PollLeaderboard, guild_id)
            if row is not None:
                row.message_id = new_msg.id
                await s.commit()

    def _schedule_leaderboard_refresh(self, guild_id: int) -> None:
        """Debounced trigger — coalesces N refresh requests within a
        few seconds into a single Discord message edit. Called from
        vote / delete / recurring-spawn paths so the card stays live
        without hammering the API."""
        existing = self._board_tasks.get(guild_id)
        if existing is not None and not existing.done():
            existing.cancel()
        self._board_tasks[guild_id] = asyncio.create_task(
            self._debounced_leaderboard_task(guild_id)
        )

    async def _debounced_leaderboard_task(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(_LEADERBOARD_DEBOUNCE_SECONDS)
            await self._refresh_leaderboard_now(guild_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception(
                "poll: leaderboard debounced refresh crashed for %s",
                guild_id,
            )
        finally:
            self._board_tasks.pop(guild_id, None)


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(PollCog(bot))
