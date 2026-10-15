"""Tournament — single-elimination bracket on top of /oantuti.

Lifecycle
---------
1. /tournament setup ... — admin configures the 3 channels (bracket /
   match arena / vinh danh).
2. /tournament create participants:N rounds:M top1:X top2:Y top3:Z
   odd_out:O — opens registration.
3. Users click 'Tham gia' on the registration card until N rows exist.
4. Admin clicks 'Tạo Bracket' — random pair, the odd one out (if N is
   odd) is paid odd_out_reward and removed; bracket embed posted.
5. Admin clicks 'Start Tournament' — first R1 match starts as an
   oantuti game in the match arena channel. Subsequent matches start
   sequentially as each finishes (driven by the on_oantuti_finished
   listener). At every round transition, if the survivor count is odd,
   one survivor is randomly bye-eliminated + paid odd_out_reward.
6. When the final completes, top1/top2/top3 are credited, the bracket
   embed shows final placements, and a celebratory post lands in the
   vinh danh channel.

Bracket display
---------------
Rendered as plain-text inside the embed description, one round per
field for compactness. Updated in-place after every match decision.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..db.models import (
    GameWinBoard,
    GuildConfig,
    GuildEconomyConfig,
    OantutiGame,
    Tournament,
    TournamentMatch,
    TournamentParticipant,
)
from ..economy.service import WalletService

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.tournament")


# =============================================================================
# Persistent buttons
# =============================================================================


class JoinTourneyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"tourney:join:(?P<t_id>\d+)",
):
    def __init__(self, t_id: int, *, disabled: bool = False) -> None:
        self.t_id = t_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"tourney:join:{t_id}",
                label="Tham gia",
                emoji="🏆",
                style=discord.ButtonStyle.primary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "JoinTourneyButton":
        return cls(int(match["t_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "TournamentCog" = interaction.client.get_cog("TournamentCog")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_join(interaction, self.t_id)


class LeaveTourneyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"tourney:leave:(?P<t_id>\d+)",
):
    def __init__(self, t_id: int, *, disabled: bool = False) -> None:
        self.t_id = t_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"tourney:leave:{t_id}",
                label="Leave",
                emoji="🚪",
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "LeaveTourneyButton":
        return cls(int(match["t_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "TournamentCog" = interaction.client.get_cog("TournamentCog")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_leave(interaction, self.t_id)


class BuildBracketButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"tourney:bracket:(?P<t_id>\d+)",
):
    def __init__(self, t_id: int, *, disabled: bool = False) -> None:
        self.t_id = t_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"tourney:bracket:{t_id}",
                label="Build Bracket",
                emoji="🎯",
                style=discord.ButtonStyle.success,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "BuildBracketButton":
        return cls(int(match["t_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "TournamentCog" = interaction.client.get_cog("TournamentCog")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_build_bracket(interaction, self.t_id)


class StartTourneyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"tourney:start:(?P<t_id>\d+)",
):
    def __init__(self, t_id: int, *, disabled: bool = False) -> None:
        self.t_id = t_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"tourney:start:{t_id}",
                label="Start Tournament",
                emoji="▶️",
                style=discord.ButtonStyle.success,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "StartTourneyButton":
        return cls(int(match["t_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "TournamentCog" = interaction.client.get_cog("TournamentCog")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_start(interaction, self.t_id)


class CancelTourneyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"tourney:cancel:(?P<t_id>\d+)",
):
    def __init__(self, t_id: int, *, disabled: bool = False) -> None:
        self.t_id = t_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"tourney:cancel:{t_id}",
                label="Cancel",
                emoji="🛑",
                style=discord.ButtonStyle.danger,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "CancelTourneyButton":
        return cls(int(match["t_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "TournamentCog" = interaction.client.get_cog("TournamentCog")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_cancel(interaction, self.t_id)


# =============================================================================
# View builders
# =============================================================================


def _registration_view(t_id: int, *, full: bool = False) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(JoinTourneyButton(t_id, disabled=full))
    v.add_item(LeaveTourneyButton(t_id))
    v.add_item(BuildBracketButton(t_id))
    v.add_item(CancelTourneyButton(t_id))
    return v


def _bracket_view(
    t_id: int, *, started: bool = False
) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(StartTourneyButton(t_id, disabled=started))
    v.add_item(CancelTourneyButton(t_id, disabled=started))
    return v


# =============================================================================
# Embed renderers
# =============================================================================


def _registration_embed(
    t: Tournament, participants: list[TournamentParticipant], coin_label: str
) -> discord.Embed:
    embed = discord.Embed(
        title="🏆 Rock-Paper-Scissors Tournament — Registration",
        color=discord.Color.orange(),
    )
    desc_lines = [
        f"**Max participants:** {t.max_participants}",
        f"**Rounds per match:** {t.rounds_per_match}",
        "",
        f"🥇 Top 1: **{t.top1_reward:,}** {coin_label}",
        f"🥈 Top 2: **{t.top2_reward:,}** {coin_label}",
        f"🥉 Top 3: **{t.top3_reward:,}** {coin_label}",
    ]
    if t.odd_out_reward > 0:
        desc_lines.append(
            f"🍀 Odd-one-out (no pair): **{t.odd_out_reward:,}** {coin_label}"
        )
    embed.description = "\n".join(desc_lines)

    p_lines = [
        f"`{i+1:>2}.` <@{p.user_id}>" for i, p in enumerate(participants)
    ] or ["_No sign-ups yet._"]
    embed.add_field(
        name=f"👥 Registered ({len(participants)}/{t.max_participants})",
        value="\n".join(p_lines)[:1024],
        inline=False,
    )
    embed.set_footer(
        text=(
            "Press Join to sign up • Admin presses Build Bracket when ready"
        )
    )
    return embed


def _format_player_for_bracket(
    user_id: int | None, winner_id: int | None, eliminated: bool
) -> str:
    """Pretty-print a slot. Winners get **bold**, eliminated get ~~strike~~,
    empty slots show as ⏳."""
    if user_id is None:
        return "⏳ _(waiting)_"
    s = f"<@{user_id}>"
    if winner_id == user_id:
        return f"✅ {s}"
    if eliminated and winner_id is not None:
        return f"❌ ~~{s}~~"
    return s


def _bracket_embed(
    t: Tournament,
    matches: list[TournamentMatch],
    coin_label: str,
) -> discord.Embed:
    if t.status == "finished":
        color = discord.Color.gold()
        title = "🏆 Tournament — FINAL RESULTS"
    elif t.status == "running":
        color = discord.Color.green()
        title = "🏆 Tournament — IN PROGRESS"
    else:
        color = discord.Color.blurple()
        title = "🏆 Tournament — BRACKET"

    embed = discord.Embed(title=title, color=color)

    # Sort matches by (round_number, pair_index)
    matches_sorted = sorted(matches, key=lambda m: (m.round_number, m.pair_index))
    max_round = max((m.round_number for m in matches_sorted), default=1)

    round_labels = {
        max_round: "🏁 Final",
        max_round - 1: "🥈 Semifinal",
        max_round - 2: "🥉 Quarterfinal",
    }

    for round_no in range(1, max_round + 1):
        round_matches = [m for m in matches_sorted if m.round_number == round_no]
        if not round_matches:
            continue
        label = round_labels.get(round_no, f"Round {round_no}")
        lines: list[str] = []
        for m in round_matches:
            if m.is_bye:
                lines.append(
                    f"`#{m.pair_index + 1}` 🍀 <@{m.player_a_id}> _(bye — eliminated + reward)_"
                )
                continue
            a_str = _format_player_for_bracket(m.player_a_id, m.winner_id, True)
            b_str = _format_player_for_bracket(m.player_b_id, m.winner_id, True)
            status_emoji = (
                "✅" if m.status == "finished"
                else "▶️" if m.status == "in_progress"
                else "⏳"
            )
            lines.append(
                f"`#{m.pair_index + 1}` {status_emoji} {a_str}  vs  {b_str}"
            )
        embed.add_field(name=label, value="\n".join(lines)[:1024], inline=False)

    if t.status == "finished":
        podium = []
        if t.winner_id:
            podium.append(f"🥇 <@{t.winner_id}> — **{t.top1_reward:,}** {coin_label}")
        if t.runnerup_id:
            podium.append(f"🥈 <@{t.runnerup_id}> — **{t.top2_reward:,}** {coin_label}")
        if t.third_id:
            podium.append(f"🥉 <@{t.third_id}> — **{t.top3_reward:,}** {coin_label}")
        if podium:
            embed.add_field(name="🏆 BỤC VINH QUANG", value="\n".join(podium), inline=False)

    return embed


# =============================================================================
# Cog
# =============================================================================


class TournamentCog(commands.Cog, name="TournamentCog"):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        # tournament_id -> lock to serialize bracket mutations
        self._locks: dict[int, asyncio.Lock] = {}

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

    def _lock(self, t_id: int) -> asyncio.Lock:
        if t_id not in self._locks:
            self._locks[t_id] = asyncio.Lock()
        return self._locks[t_id]

    # User-facing /tournament group — visible to all so creator-role
    # members can run /tournament create. Admin work lives under a
    # sibling /tournament-admin group below.
    tournament = app_commands.Group(
        name="tournament",
        description="Rock-Paper-Scissors tournament",
        guild_only=True,
    )

    tournament_admin = app_commands.Group(
        name="tournament-admin",
        description="(Owner) Tournament admin: setup, honor board",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ---------- helpers ----------

    async def _coin_label(self, guild_id: int) -> str:
        async with self.bot.db.session() as s:
            ecfg = await s.get(GuildEconomyConfig, guild_id)
        return ecfg.coin_name if ecfg else "coin"

    async def _fetch(self, t_id: int) -> Tournament | None:
        async with self.bot.db.session() as s:
            return await s.scalar(
                select(Tournament).where(Tournament.id == t_id).options(
                    selectinload(Tournament.participants),
                    selectinload(Tournament.matches),
                )
            )

    async def _refresh_registration(self, t_id: int) -> None:
        t = await self._fetch(t_id)
        if t is None or t.bracket_message_id is None:
            return
        if t.status != "registering":
            return
        guild = self.bot.get_guild(t.guild_id)
        if guild is None:
            return
        ch = guild.get_channel(t.bracket_channel_id)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await ch.fetch_message(t.bracket_message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        coin_label = await self._coin_label(t.guild_id)
        embed = _registration_embed(t, list(t.participants), coin_label)
        full = len(t.participants) >= t.max_participants
        try:
            await msg.edit(
                embed=embed,
                view=_registration_view(t_id, full=full),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            log.warning("tournament %s: registration refresh failed: %s", t_id, e)

    async def _refresh_bracket(self, t_id: int) -> None:
        t = await self._fetch(t_id)
        if t is None or t.bracket_message_id is None:
            return
        guild = self.bot.get_guild(t.guild_id)
        if guild is None:
            return
        ch = guild.get_channel(t.bracket_channel_id)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await ch.fetch_message(t.bracket_message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        coin_label = await self._coin_label(t.guild_id)
        embed = _bracket_embed(t, list(t.matches), coin_label)
        view: discord.ui.View | None
        if t.status == "bracket_ready":
            view = _bracket_view(t_id, started=False)
        elif t.status == "running":
            view = _bracket_view(t_id, started=True)
        else:
            view = None
        try:
            await msg.edit(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            log.warning("tournament %s: bracket refresh failed: %s", t_id, e)

    # ---------- /tournament-admin setup ----------

    @tournament_admin.command(
        name="setup",
        description="(Owner) Configure the tournament channels",
    )
    @app_commands.describe(
        bracket_channel="Channel where the bracket + registration is posted",
        match_channel="Channel where individual 1v1 matches are posted",
        announce_channel="Honor channel — where final results are posted",
    )
    async def tournament_setup(
        self,
        interaction: discord.Interaction,
        bracket_channel: discord.TextChannel | None = None,
        match_channel: discord.TextChannel | None = None,
        announce_channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            if bracket_channel is not None:
                cfg.tournament_bracket_channel_id = bracket_channel.id
            if match_channel is not None:
                cfg.tournament_match_channel_id = match_channel.id
            if announce_channel is not None:
                cfg.tournament_announce_channel_id = announce_channel.id
            await s.commit()
            cfg = await s.get(GuildConfig, interaction.guild_id)
        assert cfg is not None
        bch = f"<#{cfg.tournament_bracket_channel_id}>" if cfg.tournament_bracket_channel_id else "—"
        mch = f"<#{cfg.tournament_match_channel_id}>" if cfg.tournament_match_channel_id else "—"
        ach = f"<#{cfg.tournament_announce_channel_id}>" if cfg.tournament_announce_channel_id else "—"
        embed = discord.Embed(
            title="🏆 Tournament configuration", color=discord.Color.blurple(),
        )
        embed.add_field(name="Bracket channel", value=bch, inline=False)
        embed.add_field(name="Match channel", value=mch, inline=False)
        embed.add_field(name="Honor channel", value=ach, inline=False)
        await interaction.response.send_message(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /tournament create ----------

    @tournament.command(
        name="create",
        description="(Admin) Create a new tournament + open registration",
    )
    @app_commands.describe(
        participants="Max participants (2-32)",
        rounds_per_match="Rounds per match (1-15)",
        top1="Coin reward for top 1",
        top2="Coin reward for top 2",
        top3="Coin reward for top 3",
        odd_out="Coin reward for the odd-one-out (no pair)",
    )
    async def tournament_create(
        self,
        interaction: discord.Interaction,
        participants: app_commands.Range[int, 2, 32],
        rounds_per_match: app_commands.Range[int, 1, 15] = 3,
        top1: app_commands.Range[int, 0, 10_000_000] = 0,
        top2: app_commands.Range[int, 0, 10_000_000] = 0,
        top3: app_commands.Range[int, 0, 10_000_000] = 0,
        odd_out: app_commands.Range[int, 0, 10_000_000] = 0,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
        if cfg is None or not cfg.tournament_bracket_channel_id or not cfg.tournament_match_channel_id:
            await interaction.followup.send(
                "❌ Channels not configured. Run `/tournament-admin setup` first.",
                ephemeral=True,
            )
            return

        # Block creating a new tournament when there's already an active one
        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(Tournament).where(
                    Tournament.guild_id == interaction.guild_id,
                    Tournament.status.in_(["registering", "bracket_ready", "running"]),
                )
            )
        if existing is not None:
            await interaction.followup.send(
                f"❌ Tournament #{existing.id} is currently **{existing.status}**. "
                "Cancel it first if you want to create a new one.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        bracket_ch = guild.get_channel(cfg.tournament_bracket_channel_id) if guild else None
        match_ch = guild.get_channel(cfg.tournament_match_channel_id) if guild else None
        if not isinstance(bracket_ch, discord.TextChannel) or not isinstance(match_ch, discord.TextChannel):
            await interaction.followup.send(
                "❌ Configured channels are no longer valid. Re-run `/tournament-admin setup`.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            t = Tournament(
                guild_id=interaction.guild_id,
                creator_id=interaction.user.id,
                bracket_channel_id=bracket_ch.id,
                match_channel_id=match_ch.id,
                announce_channel_id=cfg.tournament_announce_channel_id,
                max_participants=int(participants),
                rounds_per_match=int(rounds_per_match),
                top1_reward=int(top1),
                top2_reward=int(top2),
                top3_reward=int(top3),
                odd_out_reward=int(odd_out),
                status="registering",
            )
            s.add(t)
            await s.commit()
            await s.refresh(t)
            t_id = t.id

        coin_label = await self._coin_label(interaction.guild_id)
        t = await self._fetch(t_id)
        assert t is not None
        embed = _registration_embed(t, [], coin_label)
        view = _registration_view(t_id, full=False)
        try:
            msg = await bracket_ch.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {bracket_ch.mention}.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            row = await s.get(Tournament, t_id)
            if row is not None:
                row.bracket_message_id = msg.id
                row.registration_message_id = msg.id
                await s.commit()

        await interaction.followup.send(
            f"✅ Tournament #{t_id} created in {bracket_ch.mention}. "
            "Users press 'Join' to sign up.",
            ephemeral=True,
        )

    # ---------- Join / Leave ----------

    async def handle_join(
        self, interaction: discord.Interaction, t_id: int
    ) -> None:
        async with self._lock(t_id):
            async with self.bot.db.session() as s:
                t = await s.get(Tournament, t_id)
                if t is None or t.status != "registering":
                    await interaction.response.send_message(
                        "Tournament is no longer accepting sign-ups.", ephemeral=True
                    )
                    return
                count = await s.scalar(
                    select(func.count(TournamentParticipant.id))
                    .where(TournamentParticipant.tournament_id == t_id)
                )
                if (count or 0) >= t.max_participants:
                    await interaction.response.send_message(
                        "Tournament is full.", ephemeral=True
                    )
                    return
                existing = await s.scalar(
                    select(TournamentParticipant).where(
                        TournamentParticipant.tournament_id == t_id,
                        TournamentParticipant.user_id == interaction.user.id,
                    )
                )
                if existing is not None:
                    await interaction.response.send_message(
                        "You've already signed up.", ephemeral=True
                    )
                    return
                s.add(TournamentParticipant(
                    tournament_id=t_id,
                    user_id=interaction.user.id,
                ))
                await s.commit()
        await interaction.response.send_message(
            "✅ Signed up. Waiting for admin to Build Bracket.",
            ephemeral=True,
        )
        await self._refresh_registration(t_id)

    async def handle_leave(
        self, interaction: discord.Interaction, t_id: int
    ) -> None:
        async with self._lock(t_id):
            async with self.bot.db.session() as s:
                t = await s.get(Tournament, t_id)
                if t is None or t.status != "registering":
                    await interaction.response.send_message(
                        "Tournament has started — you can't leave anymore.",
                        ephemeral=True,
                    )
                    return
                existing = await s.scalar(
                    select(TournamentParticipant).where(
                        TournamentParticipant.tournament_id == t_id,
                        TournamentParticipant.user_id == interaction.user.id,
                    )
                )
                if existing is None:
                    await interaction.response.send_message(
                        "You haven't signed up.", ephemeral=True
                    )
                    return
                await s.delete(existing)
                await s.commit()
        await interaction.response.send_message(
            "✅ Left the tournament.", ephemeral=True
        )
        await self._refresh_registration(t_id)

    # ---------- Build bracket ----------

    async def handle_build_bracket(
        self, interaction: discord.Interaction, t_id: int
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ Only admins (Manage Server) can Build Bracket.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with self._lock(t_id):
            t = await self._fetch(t_id)
            if t is None or t.status != "registering":
                await interaction.followup.send(
                    f"Tournament is in state {t.status if t else 'does not exist'}.",
                    ephemeral=True,
                )
                return
            participants = list(t.participants)
            if len(participants) < 2:
                await interaction.followup.send(
                    "❌ Need at least 2 participants to build a bracket.",
                    ephemeral=True,
                )
                return

            # Shuffle + handle odd
            shuffled = participants.copy()
            random.shuffle(shuffled)
            odd_out_user: int | None = None
            if len(shuffled) % 2 == 1:
                # Random pop the odd one
                victim = shuffled.pop(random.randrange(len(shuffled)))
                odd_out_user = victim.user_id

            # Build R1 matches
            async with self.bot.db.session() as s:
                if odd_out_user is not None:
                    # Mark as odd_out, eliminated
                    p_row = await s.scalar(
                        select(TournamentParticipant).where(
                            TournamentParticipant.tournament_id == t_id,
                            TournamentParticipant.user_id == odd_out_user,
                        )
                    )
                    if p_row is not None:
                        p_row.odd_out = True
                    # Also create a "bye" match row so the bracket display
                    # shows them gracefully.
                    s.add(TournamentMatch(
                        tournament_id=t_id,
                        round_number=1,
                        pair_index=len(shuffled) // 2,
                        player_a_id=odd_out_user,
                        is_bye=True,
                        status="bye",
                        winner_id=None,
                        finished_at=datetime.now(timezone.utc),
                    ))
                for i in range(0, len(shuffled), 2):
                    a = shuffled[i].user_id
                    b = shuffled[i + 1].user_id
                    s.add(TournamentMatch(
                        tournament_id=t_id,
                        round_number=1,
                        pair_index=i // 2,
                        player_a_id=a,
                        player_b_id=b,
                        status="pending",
                    ))
                row = await s.get(Tournament, t_id)
                if row is not None:
                    row.status = "bracket_ready"
                await s.commit()

        # Pay odd-out immediately
        if odd_out_user is not None and t.odd_out_reward > 0:
            try:
                await WalletService(self.bot).adjust(
                    t.guild_id, odd_out_user, t.odd_out_reward,
                    "tournament_odd_out",
                    description=f"Odd-out reward for tournament #{t_id}",
                    metadata={"tournament_id": t_id},
                )
            except Exception:
                log.exception("tournament %s: odd-out payout failed", t_id)

        await self._refresh_bracket(t_id)
        msg = (
            f"✅ Bracket built for tournament #{t_id}."
        )
        if odd_out_user is not None:
            msg += (
                f" 🍀 <@{odd_out_user}> was odd-one-out — eliminated + "
                f"rewarded **{t.odd_out_reward:,}** coin."
            )
        await interaction.followup.send(
            msg, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- Start ----------

    async def handle_start(
        self, interaction: discord.Interaction, t_id: int
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ Only admins (Manage Server) can Start.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        async with self._lock(t_id):
            t = await self._fetch(t_id)
            if t is None or t.status != "bracket_ready":
                await interaction.followup.send(
                    "Bracket isn't ready yet.", ephemeral=True
                )
                return
            async with self.bot.db.session() as s:
                row = await s.get(Tournament, t_id)
                if row is not None:
                    row.status = "running"
                    await s.commit()
        await self._refresh_bracket(t_id)
        await self._start_next_match(t_id)
        await interaction.followup.send(
            f"▶️ Tournament #{t_id} started. First match has been posted.",
            ephemeral=True,
        )

    # ---------- Cancel ----------

    async def handle_cancel(
        self, interaction: discord.Interaction, t_id: int
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ Only admins can cancel.", ephemeral=True
            )
            return
        async with self.bot.db.session() as s:
            t = await s.get(Tournament, t_id)
            if t is None:
                await interaction.response.send_message(
                    "Tournament doesn't exist.", ephemeral=True
                )
                return
            if t.status in ("finished", "cancelled"):
                await interaction.response.send_message(
                    f"Tournament is already **{t.status}**.",
                    ephemeral=True,
                )
                return
            t.status = "cancelled"
            t.finished_at = datetime.now(timezone.utc)
            await s.commit()
        await interaction.response.send_message(
            f"🛑 Tournament #{t_id} cancelled.", ephemeral=True
        )
        await self._refresh_bracket(t_id)

    # ---------- Match runner ----------

    async def _start_next_match(self, t_id: int) -> None:
        """Find the next pending match and post it as an oantuti game.

        If all matches in the current round are finished, advance the
        survivors into the next round (handling odd survivors via bye).
        """
        async with self._lock(t_id):
            t = await self._fetch(t_id)
            if t is None or t.status != "running":
                return
            matches = sorted(
                t.matches, key=lambda m: (m.round_number, m.pair_index)
            )
            # Are there any pending matches?
            pending = [m for m in matches if m.status == "pending"]
            if pending:
                # Pick the first by (round, pair_index)
                m = pending[0]
                await self._post_match(t, m)
                return

            # No pending — check if a new round needs to be built.
            current_round = max((m.round_number for m in matches), default=0)
            survivors = [
                m.winner_id for m in matches
                if m.round_number == current_round and m.winner_id is not None
            ]
            survivors = [s for s in survivors if s is not None]
            if len(survivors) <= 1:
                # Tournament finished
                await self._finalize(t_id)
                return

            # Build next round
            next_round = current_round + 1
            random.shuffle(survivors)
            odd_out_user: int | None = None
            if len(survivors) % 2 == 1:
                odd_out_user = survivors.pop(random.randrange(len(survivors)))

            async with self.bot.db.session() as s:
                if odd_out_user is not None:
                    s.add(TournamentMatch(
                        tournament_id=t_id,
                        round_number=next_round,
                        pair_index=len(survivors) // 2,
                        player_a_id=odd_out_user,
                        is_bye=True,
                        status="bye",
                        winner_id=None,
                        finished_at=datetime.now(timezone.utc),
                    ))
                    p_row = await s.scalar(
                        select(TournamentParticipant).where(
                            TournamentParticipant.tournament_id == t_id,
                            TournamentParticipant.user_id == odd_out_user,
                        )
                    )
                    if p_row is not None:
                        p_row.odd_out = True
                for i in range(0, len(survivors), 2):
                    s.add(TournamentMatch(
                        tournament_id=t_id,
                        round_number=next_round,
                        pair_index=i // 2,
                        player_a_id=survivors[i],
                        player_b_id=survivors[i + 1],
                        status="pending",
                    ))
                await s.commit()

        # Pay odd-out
        if odd_out_user is not None and t.odd_out_reward > 0:
            try:
                await WalletService(self.bot).adjust(
                    t.guild_id, odd_out_user, t.odd_out_reward,
                    "tournament_odd_out",
                    description=f"Odd-out reward for tournament #{t_id}",
                    metadata={"tournament_id": t_id, "round": next_round},
                )
            except Exception:
                log.exception("tournament %s: odd-out payout failed", t_id)
        await self._refresh_bracket(t_id)
        # Recurse to start the first match of the new round
        await self._start_next_match(t_id)

    async def _post_match(
        self, t: Tournament, m: TournamentMatch
    ) -> None:
        """Post an oantuti game for this match in the match-arena channel."""
        guild = self.bot.get_guild(t.guild_id)
        if guild is None:
            return
        ch = guild.get_channel(t.match_channel_id)
        if not isinstance(ch, discord.TextChannel):
            log.warning("tournament %s: match channel unusable", t.id)
            return

        # Create OantutiGame in 'running' state directly (skip accept) since
        # both players are committed by joining the tournament.
        from .oantuti import (  # local import to avoid cycle
            _build_round_embed, _build_round_view, _get_icons,
        )
        async with self.bot.db.session() as s:
            game = OantutiGame(
                guild_id=t.guild_id,
                channel_id=ch.id,
                player_a_id=m.player_a_id,
                player_b_id=m.player_b_id,
                rounds=t.rounds_per_match,
                bet_amount=0,
                status="running",
                started_at=datetime.now(timezone.utc),
                tournament_match_id=m.id,
            )
            s.add(game)
            await s.commit()
            await s.refresh(game)
            game_id = game.id
            # First round row
            from ..db.models import OantutiRound  # local import
            s.add(OantutiRound(game_id=game_id, round_index=0))
            await s.commit()

        icons = await _get_icons(self.bot, t.guild_id)
        coin_label = await self._coin_label(t.guild_id)
        async with self.bot.db.session() as s:
            g = await s.get(OantutiGame, game_id)
            from ..db.models import OantutiRound  # local import
            rnd = await s.scalar(
                select(OantutiRound).where(
                    OantutiRound.game_id == game_id,
                    OantutiRound.round_index == 0,
                )
            )
        assert g is not None and rnd is not None

        intro_embed = discord.Embed(
            title=(
                f"🏆 Tournament #{t.id} — Round {m.round_number} • Match {m.pair_index + 1}"
            ),
            description=(
                f"<@{m.player_a_id}> vs <@{m.player_b_id}>\n"
                f"Best of **{t.rounds_per_match}**"
            ),
            color=discord.Color.purple(),
        )
        try:
            await ch.send(
                content=f"<@{m.player_a_id}> <@{m.player_b_id}>",
                embed=intro_embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            round_msg = await ch.send(
                embed=_build_round_embed(g, rnd, icons, coin_label=coin_label),
                view=_build_round_view(game_id, icons),
            )
        except discord.Forbidden:
            log.warning("tournament %s: cannot post match", t.id)
            return

        async with self.bot.db.session() as s:
            row = await s.get(OantutiGame, game_id)
            if row is not None:
                row.message_id = round_msg.id
                await s.commit()
            mrow = await s.get(TournamentMatch, m.id)
            if mrow is not None:
                mrow.status = "in_progress"
                mrow.oantuti_game_id = game_id
                await s.commit()
        await self._refresh_bracket(t.id)

    # ---------- Listener ----------

    @commands.Cog.listener()
    async def on_oantuti_finished(self, game: OantutiGame) -> None:
        if game.tournament_match_id is None:
            return  # not a tournament game
        async with self.bot.db.session() as s:
            match = await s.get(TournamentMatch, game.tournament_match_id)
            if match is None:
                return
            if game.is_tie:
                # Coin flip to decide winner in tournament mode (RPS ties
                # are extremely unlucky to drag on; spec didn't mandate
                # extra rounds, so we resolve with a fair random pick).
                match.winner_id = random.choice([game.player_a_id, game.player_b_id])
            else:
                match.winner_id = game.winner_id
            match.status = "finished"
            match.finished_at = datetime.now(timezone.utc)
            t_id = match.tournament_id
            await s.commit()
        await self._refresh_bracket(t_id)
        # Continue the bracket on the next match
        await self._start_next_match(t_id)

    # ---------- Finalize ----------

    async def _finalize(self, t_id: int) -> None:
        async with self.bot.db.session() as s:
            t = await s.scalar(
                select(Tournament).where(Tournament.id == t_id).options(
                    selectinload(Tournament.matches),
                    selectinload(Tournament.participants),
                )
            )
            if t is None or t.status != "running":
                return

            matches = sorted(t.matches, key=lambda m: (m.round_number, m.pair_index))
            max_round = max((m.round_number for m in matches), default=1)
            final = next(
                (m for m in matches if m.round_number == max_round
                 and m.status == "finished" and not m.is_bye),
                None,
            )
            if final is None:
                return
            t.winner_id = final.winner_id
            t.runnerup_id = (
                final.player_b_id if final.winner_id == final.player_a_id
                else final.player_a_id
            )
            # Third place: semi-final losers (max_round - 1). If there's
            # only 1 SF loser → that's the third. If 2 → both share top3.
            if max_round >= 2:
                sf_losers: list[int] = []
                for m in matches:
                    if m.round_number == max_round - 1 and not m.is_bye and m.status == "finished":
                        loser = (
                            m.player_b_id if m.winner_id == m.player_a_id
                            else m.player_a_id
                        )
                        if loser is not None:
                            sf_losers.append(loser)
                if sf_losers:
                    t.third_id = sf_losers[0]
                    third_secondary = sf_losers[1] if len(sf_losers) > 1 else None
                else:
                    third_secondary = None
            else:
                third_secondary = None

            t.status = "finished"
            t.finished_at = datetime.now(timezone.utc)

            # Assign placement on participants
            for p in t.participants:
                if p.user_id == t.winner_id:
                    p.placement = 1
                elif p.user_id == t.runnerup_id:
                    p.placement = 2
                elif p.user_id == t.third_id or p.user_id == third_secondary:
                    p.placement = 3
            await s.commit()
            await s.refresh(t)

        # Payouts
        wallet_svc = WalletService(self.bot)
        payouts: list[tuple[int, int, str]] = []  # (user_id, amount, label)
        if t.winner_id and t.top1_reward > 0:
            payouts.append((t.winner_id, t.top1_reward, "top1"))
        if t.runnerup_id and t.top2_reward > 0:
            payouts.append((t.runnerup_id, t.top2_reward, "top2"))
        if t.top3_reward > 0:
            if t.third_id:
                payouts.append((t.third_id, t.top3_reward, "top3"))
            if third_secondary:
                payouts.append((third_secondary, t.top3_reward, "top3"))
        for uid, amount, label in payouts:
            try:
                await wallet_svc.adjust(
                    t.guild_id, uid, amount,
                    f"tournament_{label}",
                    description=f"Tournament #{t_id} reward ({label})",
                    metadata={"tournament_id": t_id, "placement": label},
                )
            except Exception:
                log.exception("tournament %s: payout failed for %s", t_id, uid)

        await self._refresh_bracket(t_id)
        await self._post_vinh_danh(t_id)
        # Tournament champion count just changed → refresh the leaderboard
        try:
            await self._refresh_vodich_board(t.guild_id)
        except Exception:
            log.exception("tournament: vodich board refresh failed")

    async def _post_vinh_danh(self, t_id: int) -> None:
        t = await self._fetch(t_id)
        if t is None or t.announce_channel_id is None:
            return
        guild = self.bot.get_guild(t.guild_id)
        if guild is None:
            return
        ch = guild.get_channel(t.announce_channel_id)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        coin_label = await self._coin_label(t.guild_id)

        lines = [f"🏆 **Tournament #{t_id} has ended!**", ""]
        if t.winner_id:
            lines.append(f"🥇 Champion: <@{t.winner_id}> — **{t.top1_reward:,}** {coin_label}")
        if t.runnerup_id:
            lines.append(f"🥈 Runner-up: <@{t.runnerup_id}> — **{t.top2_reward:,}** {coin_label}")
        if t.third_id:
            lines.append(f"🥉 Third: <@{t.third_id}> — **{t.top3_reward:,}** {coin_label}")
        embed = discord.Embed(
            title="🎉 TOURNAMENT HALL OF FAME — ROCK-PAPER-SCISSORS",
            description="\n".join(lines),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        try:
            await ch.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            log.warning("tournament %s: forbidden to post vinh danh", t_id)


    # =========================================================================
    # /tournament vodich — tournament champions leaderboard
    # =========================================================================

    vodich = app_commands.Group(
        name="vodich",
        description="Tournament champions leaderboard",
        parent=tournament_admin,
    )

    async def _build_vodich_embed(
        self, guild_id: int, display_count: int
    ) -> discord.Embed:
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(
                    Tournament.winner_id,
                    func.count(Tournament.id).label("wins"),
                )
                .where(
                    Tournament.guild_id == guild_id,
                    Tournament.status == "finished",
                    Tournament.winner_id.isnot(None),
                )
                .group_by(Tournament.winner_id)
                .order_by(
                    func.count(Tournament.id).desc(), Tournament.winner_id
                )
                .limit(display_count)
            )
            results = list(rows.all())

        guild = self.bot.get_guild(guild_id)
        guild_name = guild.name if guild else "Server"
        embed = discord.Embed(
            title="👑  ROCK-PAPER-SCISSORS — TOURNAMENT CHAMPIONS BOARD  👑",
            color=discord.Color.from_rgb(155, 89, 182),  # royal purple
        )
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if not results:
            embed.description = (
                "_No tournaments have finished yet._\n"
                "**Join and take home a title!**"
            )
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
                    f"{medal}  <@{uid}>  •  👑 `{wins:,}` championship(s)"
                )
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            embed.description = "\n".join(lines)
        embed.set_footer(
            text=f"{guild_name} • Top {display_count} • Auto-updates after every tournament"
        )
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    async def _build_play_view(
        self, guild_id: int,
    ) -> discord.ui.View:
        """Link button to the configured tournament bracket channel —
        the entry point where /tournament create posts its bracket embed.
        Empty view when no bracket channel set."""
        view = discord.ui.View(timeout=None)
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
        ch_id = cfg.tournament_bracket_channel_id if cfg else None
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

    async def _refresh_vodich_board(self, guild_id: int) -> None:
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "tournament",
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
        embed = await self._build_vodich_embed(guild_id, board.display_count)
        view = await self._build_play_view(guild_id)
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
                log.warning("tournament vodich: edit failed %s: %s", guild_id, e)
            return
        try:
            new_msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning("tournament vodich: forbidden to repost in %s", guild_id)
            return
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "tournament",
                )
            )
            if row is not None:
                row.message_id = new_msg.id
                await s.commit()

    @vodich.command(
        name="post",
        description="(Admin) Create/replace the Tournament Champions board",
    )
    @app_commands.describe(
        channel="Channel to post the board (any channel — usually the honor channel)",
        display_count="Number of users to show (1-25, default 10)",
    )
    async def t_vodich_post(
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
                    GameWinBoard.game_type == "tournament",
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

        embed = await self._build_vodich_embed(
            interaction.guild_id, int(display_count)
        )
        view = await self._build_play_view(interaction.guild_id)
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
                    GameWinBoard.game_type == "tournament",
                )
            )
            if board is None:
                board = GameWinBoard(
                    guild_id=interaction.guild_id,
                    game_type="tournament",
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
            f"✅ Tournament Champions board posted in {channel.mention} "
            f"(top {display_count}). It auto-updates after every tournament.",
            ephemeral=True,
        )

    @vodich.command(
        name="remove",
        description="(Admin) Remove the Tournament Champions board + stop auto-update",
    )
    async def t_vodich_remove(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "tournament",
                )
            )
            if board is None:
                await interaction.followup.send(
                    "No Tournament Champions board in this server.", ephemeral=True
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
            "✅ Tournament Champions board removed.", ephemeral=True
        )

    @vodich.command(
        name="refresh",
        description="(Admin) Refresh the Tournament Champions board immediately",
    )
    async def t_vodich_refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "tournament",
                )
            )
        if board is None:
            await interaction.followup.send(
                "No board configured. Run `/tournament-admin vodich post` first.",
                ephemeral=True,
            )
            return
        await self._refresh_vodich_board(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Refreshed Tournament Champions board in <#{board.channel_id}>.",
            ephemeral=True,
        )


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(TournamentCog(bot))
