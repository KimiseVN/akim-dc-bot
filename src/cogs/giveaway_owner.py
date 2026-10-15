"""/giveaway — owner-driven multi-winner raffle.

Distinct from the older /quaythuong (leader-driven, single winner,
reroll-until-happy). This cog is the simpler flow the user asked for:

  1. Owner runs /giveaway create channel:# name:"iPhone 15" winners:3
     → card posted in the target channel with 🎫 Tham Gia + 🎰 Roll
       + 🛑 Hủy buttons; entry list updates live as members click.
  2. Owner clicks 🎰 Roll → a 10-second cycling animation reveals
     ONE winner, then repeats for each remaining winner slot, each
     round excluding all previously-picked winners.
  3. When the last winner is revealed the card is frozen with the
     final list.

Animation cadence (per winner):
  • Fast phase — 7s at 0.35s/frame (20 frames, ~2.9 names/sec, well
    under Discord's per-message edit budget).
  • Slow phase — 5 frames ramping 0.40 → 1.10s (~3.5s total) for the
    "wheel slowing down" reveal.
Frames use a shuffled cycle over the entry pool so every participant
appears at least once during the fast phase for small pools.
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

from ..db.models import Giveaway, GiveawayJoin

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.giveaway_owner")


# =============================================================================
# Constants + animation shape
# =============================================================================

_FAST_INTERVAL = 0.35           # per fast-phase frame
_FAST_FRAMES = 20               # 20 × 0.35 = 7.0s
_SLOW_INTERVALS = (             # 0.40 + 0.55 + 0.75 + 0.95 + 1.10 = 3.75s (~3s ask)
    0.40, 0.55, 0.75, 0.95, 1.10,
)

# Cap on winners the owner can request in one /giveaway. Above ~20 the
# combined animation would run > 3 minutes wall-clock and users lose
# patience.
_MAX_WINNERS = 20
_MAX_NAME_LEN = 200
_MAX_ENTRY_LIST_SHOW = 40


# =============================================================================
# Card rendering
# =============================================================================


def _entry_list_text(joins: list[GiveawayJoin]) -> str:
    if not joins:
        return "_No entries yet. Press **🎫 Join** to enter._"
    lines = [
        f"`{i:>2}.` <@{j.user_id}>"
        for i, j in enumerate(joins[:_MAX_ENTRY_LIST_SHOW], start=1)
    ]
    if len(joins) > _MAX_ENTRY_LIST_SHOW:
        lines.append(f"_(+{len(joins) - _MAX_ENTRY_LIST_SHOW} more)_")
    return "\n".join(lines)


def _build_open_embed(gw: Giveaway) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎁 GIVEAWAY: {gw.name}",
        color=discord.Color.blurple(),
    )
    embed.description = (
        f"🎯 **Winners:** {gw.winners_count}\n"
        f"👤 **Created by:** <@{gw.creator_id}>\n\n"
        "Press **🎫 Join** to enter. "
        "Only the **owner** can press **🎰 Roll**."
    )
    embed.add_field(
        name=f"👥 Entries ({len(gw.joins)})",
        value=_entry_list_text(list(gw.joins)),
        inline=False,
    )
    embed.set_footer(text=f"Giveaway #{gw.id}")
    return embed


def _build_rolling_embed(
    gw: Giveaway,
    joins: list[GiveawayJoin],
    spotlight_id: int,
    current_round: int,
    picked_so_far: list[int],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎰 ROLLING WINNER {current_round}/{gw.winners_count}",
        color=discord.Color.gold(),
    )
    lines = [
        f"🎁 **{gw.name}**",
        "━━━━━━━━━━━━━━━━━━━━",
        f"### 👉 <@{spotlight_id}>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if picked_so_far:
        lines.append("**Winners so far:**")
        for i, uid in enumerate(picked_so_far, start=1):
            lines.append(f"🏆 `{i}.` <@{uid}>")
    embed.description = "\n".join(lines)
    embed.set_footer(
        text=f"Giveaway #{gw.id} • {len(joins)} entries"
    )
    return embed


def _build_finished_embed(gw: Giveaway) -> discord.Embed:
    winner_ids = gw.winner_ids()
    total = len(gw.joins)
    color = discord.Color.green() if winner_ids else discord.Color.dark_gray()
    embed = discord.Embed(
        title=f"🎉 GIVEAWAY RESULT: {gw.name}",
        color=color,
    )
    if winner_ids:
        lines = ["━━━━━━━━━━━━━━━━━━━━"]
        for i, uid in enumerate(winner_ids, start=1):
            lines.append(f"🏆 **Winner {i}:** <@{uid}>")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(
            f"👥 **{total}** entries\n"
            "_Congrats to the winners! DM the owner to claim your prize._"
        )
        embed.description = "\n".join(lines)
    else:
        embed.description = (
            f"👥 **{total}** entries\n"
            "😢 No winner — the pool was empty."
        )
    embed.set_footer(text=f"Giveaway #{gw.id} • Result finalized")
    return embed


def _build_cancelled_embed(gw: Giveaway) -> discord.Embed:
    embed = discord.Embed(
        title=f"🛑 GIVEAWAY: {gw.name} — Cancelled",
        color=discord.Color.dark_gray(),
    )
    embed.description = (
        f"👤 Created by <@{gw.creator_id}>\n"
        f"👥 **{len(gw.joins)}** entries\n\n"
        "Owner cancelled before rolling."
    )
    embed.set_footer(text=f"Giveaway #{gw.id}")
    return embed


# =============================================================================
# Persistent buttons
# =============================================================================


class GwJoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gwown:join:(?P<giveaway_id>\d+)",
):
    def __init__(self, giveaway_id: int, *, disabled: bool = False) -> None:
        self.giveaway_id = giveaway_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gwown:join:{giveaway_id}",
                label="Join",
                emoji="🎫",
                style=discord.ButtonStyle.primary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["giveaway_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GiveawayOwner" = interaction.client.get_cog(  # type: ignore[assignment]
            "GiveawayOwner",
        )
        if cog is None:
            return
        await cog.handle_join(interaction, self.giveaway_id)


class GwRollButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gwown:roll:(?P<giveaway_id>\d+)",
):
    def __init__(self, giveaway_id: int, *, disabled: bool = False) -> None:
        self.giveaway_id = giveaway_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gwown:roll:{giveaway_id}",
                label="Roll",
                emoji="🎰",
                style=discord.ButtonStyle.success,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["giveaway_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GiveawayOwner" = interaction.client.get_cog(  # type: ignore[assignment]
            "GiveawayOwner",
        )
        if cog is None:
            return
        await cog.handle_roll(interaction, self.giveaway_id)


class GwCancelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gwown:cancel:(?P<giveaway_id>\d+)",
):
    def __init__(self, giveaway_id: int, *, disabled: bool = False) -> None:
        self.giveaway_id = giveaway_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"gwown:cancel:{giveaway_id}",
                label="Hủy",
                emoji="🛑",
                style=discord.ButtonStyle.danger,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["giveaway_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "GiveawayOwner" = interaction.client.get_cog(  # type: ignore[assignment]
            "GiveawayOwner",
        )
        if cog is None:
            return
        await cog.handle_cancel(interaction, self.giveaway_id)


def _build_view(gw: Giveaway) -> discord.ui.View:
    """View shape depends on status.

    open       → Join + Roll + Cancel
    rolling    → all disabled (animation in flight)
    finished / cancelled → no interactive buttons
    """
    v = discord.ui.View(timeout=None)
    if gw.status == "open":
        v.add_item(GwJoinButton(gw.id))
        v.add_item(GwRollButton(gw.id))
        v.add_item(GwCancelButton(gw.id))
    elif gw.status == "rolling":
        v.add_item(GwJoinButton(gw.id, disabled=True))
        v.add_item(GwRollButton(gw.id, disabled=True))
        v.add_item(GwCancelButton(gw.id, disabled=True))
    # finished / cancelled → empty view (buttons stripped)
    return v


def _render_for_status(gw: Giveaway) -> discord.Embed:
    if gw.status == "open":
        return _build_open_embed(gw)
    if gw.status == "finished":
        return _build_finished_embed(gw)
    if gw.status == "cancelled":
        return _build_cancelled_embed(gw)
    # rolling — the animation loop draws its own embed each frame; if
    # we ever hit this path (e.g. bot restart mid-roll) fall back to
    # the open card so the message doesn't look broken.
    return _build_open_embed(gw)


# =============================================================================
# Cog
# =============================================================================


class GiveawayOwner(commands.Cog, name="GiveawayOwner"):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        self._recovered = False

    giveaway = app_commands.Group(
        name="giveaway",
        description="Tạo giveaway cho members (owner-only)",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ------------------------------------------------------------------
    # Recovery — flip any 'rolling' giveaway on restart to 'open' so
    # the buttons come alive again (the animation task was in RAM only).
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._recovered:
            return
        self._recovered = True
        try:
            await self._recover_stuck_rolls()
        except Exception:
            log.exception("giveaway_owner: recovery pass failed")

    async def _recover_stuck_rolls(self) -> None:
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(Giveaway).where(Giveaway.status == "rolling")
            )
            stuck = list(rows.scalars().all())
            for g in stuck:
                g.status = "open"
                log.info(
                    "giveaway_owner: giveaway #%s stuck rolling → open",
                    g.id,
                )
            if stuck:
                await s.commit()
        for g in stuck:
            await self._refresh_card(g.id)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild is not None
            and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        ):
            return True
        msg = "❌ Only the **server owner** can use this command / button."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    async def _fetch(self, giveaway_id: int) -> Giveaway | None:
        async with self.bot.db.session() as s:
            return await s.scalar(
                select(Giveaway)
                .where(Giveaway.id == giveaway_id)
                .options(selectinload(Giveaway.joins))
            )

    async def _refresh_card(
        self,
        giveaway_id: int,
        *,
        override_embed: discord.Embed | None = None,
    ) -> None:
        gw = await self._fetch(giveaway_id)
        if gw is None or gw.message_id is None:
            return
        guild = self.bot.get_guild(gw.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(gw.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await channel.fetch_message(gw.message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        embed = override_embed or _render_for_status(gw)
        view = _build_view(gw)
        try:
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            log.warning("giveaway_owner refresh failed: %s", e)

    # ------------------------------------------------------------------
    # /giveaway create
    # ------------------------------------------------------------------

    @giveaway.command(
        name="create",
        description="Create a new giveaway in a channel",
    )
    @app_commands.describe(
        channel="Channel where the giveaway embed is posted",
        name="Giveaway name (e.g. iPhone 15)",
        winners="Number of winners (1-20)",
    )
    async def create_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        name: app_commands.Range[str, 1, _MAX_NAME_LEN],
        winners: app_commands.Range[int, 1, _MAX_WINNERS] = 1,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return

        # Insert first so the buttons carry a real giveaway id.
        async with self.bot.db.session() as s:
            gw = Giveaway(
                guild_id=interaction.guild_id,
                channel_id=channel.id,
                creator_id=interaction.user.id,
                name=name.strip(),
                winners_count=int(winners),
                status="open",
            )
            s.add(gw)
            await s.commit()
            await s.refresh(gw)
            giveaway_id = gw.id

        fresh = await self._fetch(giveaway_id)
        assert fresh is not None
        embed = _build_open_embed(fresh)
        view = _build_view(fresh)
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {channel.mention}.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            row = await s.get(Giveaway, giveaway_id)
            if row is not None:
                row.message_id = msg.id
                await s.commit()

        await interaction.followup.send(
            f"✅ Giveaway `#{giveaway_id}` created in {channel.mention}.\n"
            f"🎁 **{name}** • 🎯 **{winners}** winner(s)",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    async def handle_join(
        self, interaction: discord.Interaction, giveaway_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            gw = await s.get(Giveaway, giveaway_id)
            if gw is None:
                await interaction.response.send_message(
                    "Giveaway no longer exists.", ephemeral=True,
                )
                return
            if gw.status != "open":
                await interaction.response.send_message(
                    "⏱ Entries are closed.", ephemeral=True,
                )
                return
            existing = await s.scalar(
                select(GiveawayJoin).where(
                    GiveawayJoin.giveaway_id == giveaway_id,
                    GiveawayJoin.user_id == interaction.user.id,
                )
            )
            if existing is not None:
                await interaction.response.send_message(
                    "✅ You've already joined. Waiting for the owner to Roll!",
                    ephemeral=True,
                )
                return
            s.add(GiveawayJoin(
                giveaway_id=giveaway_id,
                user_id=interaction.user.id,
            ))
            await s.commit()
        await interaction.response.send_message(
            "🎫 Entry recorded — good luck!", ephemeral=True,
        )
        await self._refresh_card(giveaway_id)

    async def handle_roll(
        self, interaction: discord.Interaction, giveaway_id: int,
    ) -> None:
        # Owner-only guard + status check in one transaction so a
        # second Roll click can't race in.
        async with self.bot.db.session() as s:
            gw = await s.scalar(
                select(Giveaway)
                .where(Giveaway.id == giveaway_id)
                .options(selectinload(Giveaway.joins))
            )
            if gw is None:
                await interaction.response.send_message(
                    "Giveaway no longer exists.", ephemeral=True,
                )
                return
            if not (
                interaction.guild is not None
                and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
            ):
                await interaction.response.send_message(
                    "❌ Only the **server owner** can Roll.", ephemeral=True,
                )
                return
            if gw.status != "open":
                await interaction.response.send_message(
                    f"Giveaway is **{gw.status}** — can't roll.",
                    ephemeral=True,
                )
                return
            entries = list(gw.joins)
            if not entries:
                await interaction.response.send_message(
                    "❌ No entries yet — nothing to roll.",
                    ephemeral=True,
                )
                return
            # Cap the effective winner count at pool size.
            effective_winners = min(gw.winners_count, len(entries))
            gw.status = "rolling"
            await s.commit()

        await interaction.response.send_message(
            f"🎰 Rolling **{effective_winners}** winner(s)…",
            ephemeral=True,
        )
        # Run animation in background so this handler returns promptly.
        asyncio.create_task(
            self._run_multi_winner_animation(giveaway_id, effective_winners)
        )

    async def handle_cancel(
        self, interaction: discord.Interaction, giveaway_id: int,
    ) -> None:
        async with self.bot.db.session() as s:
            gw = await s.get(Giveaway, giveaway_id)
            if gw is None:
                await interaction.response.send_message(
                    "Giveaway no longer exists.", ephemeral=True,
                )
                return
            is_creator = interaction.user.id == gw.creator_id
            is_owner = (
                interaction.guild is not None
                and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
            )
            if not (is_creator or is_owner):
                await interaction.response.send_message(
                    "❌ Only the creator or server owner can Cancel.",
                    ephemeral=True,
                )
                return
            if gw.status != "open":
                await interaction.response.send_message(
                    f"Giveaway is **{gw.status}** — can no longer be cancelled.",
                    ephemeral=True,
                )
                return
            gw.status = "cancelled"
            gw.finished_at = datetime.now(timezone.utc)
            await s.commit()
        await interaction.response.send_message(
            "🛑 Giveaway cancelled.", ephemeral=True,
        )
        await self._refresh_card(giveaway_id)

    # ------------------------------------------------------------------
    # Roll animation (multi-winner, sequential)
    # ------------------------------------------------------------------

    async def _run_multi_winner_animation(
        self, giveaway_id: int, winners_needed: int,
    ) -> None:
        """Fire-and-forget task. Draws winners one at a time, each with
        its own 10s cycling animation, excluding previously-picked
        winners between rounds. On any error, best-effort marks the
        giveaway finished with whatever winners we managed to pick so
        the card doesn't freeze mid-roll."""
        winners_picked: list[int] = []
        try:
            gw = await self._fetch(giveaway_id)
            if gw is None:
                return
            entries = list(gw.joins)
            if not entries:
                await self._settle_finished(giveaway_id, [])
                return

            guild = self.bot.get_guild(gw.guild_id)
            channel = guild.get_channel(gw.channel_id) if guild else None
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                # No visible channel — settle DB anyway.
                pool_ids = [j.user_id for j in entries]
                random.shuffle(pool_ids)
                await self._settle_finished(
                    giveaway_id, pool_ids[:winners_needed],
                )
                return
            try:
                msg = await channel.fetch_message(gw.message_id or 0)
            except (discord.NotFound, discord.Forbidden):
                pool_ids = [j.user_id for j in entries]
                random.shuffle(pool_ids)
                await self._settle_finished(
                    giveaway_id, pool_ids[:winners_needed],
                )
                return

            remaining_pool = [j.user_id for j in entries]
            for round_idx in range(1, winners_needed + 1):
                if not remaining_pool:
                    break  # exhausted — everyone already won
                winner_uid = await self._animate_one_winner(
                    gw, entries, remaining_pool, msg,
                    round_idx, winners_needed, winners_picked,
                )
                winners_picked.append(winner_uid)
                remaining_pool = [
                    uid for uid in remaining_pool if uid != winner_uid
                ]
                # Brief pause between rounds so the picked winner
                # lingers on-screen before the next spin.
                if round_idx < winners_needed and remaining_pool:
                    await asyncio.sleep(1.5)

            await self._settle_finished(giveaway_id, winners_picked)
        except Exception:
            log.exception(
                "giveaway_owner: multi-winner animation crashed for %s",
                giveaway_id,
            )
            # Best-effort finish so the card unfreezes.
            try:
                await self._settle_finished(giveaway_id, winners_picked)
            except Exception:
                log.exception("giveaway_owner: fallback settle also failed")

    async def _animate_one_winner(
        self,
        gw: Giveaway,
        all_entries: list[GiveawayJoin],
        pool_ids: list[int],
        msg: discord.Message,
        round_idx: int,
        total_rounds: int,
        picked_so_far: list[int],
    ) -> int:
        """Run the ~10s cycling animation for ONE round and return the
        picked winner user_id. Winner is pre-selected up front so the
        animation is a pure visual sequence — the final frame equals
        the persisted winner every time."""
        winner_uid = random.choice(pool_ids)
        # Shuffled pool for the fast phase so every candidate has a
        # chance to be spotlighted at least once when pool ≤ frames.
        shuffled = pool_ids.copy()
        random.shuffle(shuffled)
        view = _build_view(gw)  # buttons all disabled during rolling
        last_spot: int | None = None

        def _pick_spotlight(idx: int) -> int:
            nonlocal last_spot
            # Cycle through the shuffled pool during fast phase; on
            # each pass, reshuffle so we don't loop the same order.
            if idx % len(shuffled) == 0 and idx > 0:
                random.shuffle(shuffled)
            candidate = shuffled[idx % len(shuffled)]
            # Avoid spotlighting the same user two frames in a row —
            # a repeat looks like the embed glitched.
            if candidate == last_spot and len(pool_ids) > 1:
                candidate = shuffled[(idx + 1) % len(shuffled)]
            last_spot = candidate
            return candidate

        # Fast phase.
        for i in range(_FAST_FRAMES):
            spot = _pick_spotlight(i)
            embed = _build_rolling_embed(
                gw, all_entries, spot, round_idx, picked_so_far,
            )
            try:
                await msg.edit(embed=embed, view=view)
            except discord.HTTPException as e:
                log.warning(
                    "giveaway_owner anim: fast edit failed frame %d: %s",
                    i, e,
                )
            await asyncio.sleep(_FAST_INTERVAL)

        # Slow phase — decelerate. Reveal the pre-picked winner on the
        # very last frame.
        for i, interval in enumerate(_SLOW_INTERVALS):
            is_final = (i == len(_SLOW_INTERVALS) - 1)
            spot = winner_uid if is_final else _pick_spotlight(_FAST_FRAMES + i)
            embed = _build_rolling_embed(
                gw, all_entries, spot, round_idx, picked_so_far,
            )
            try:
                await msg.edit(embed=embed, view=view)
            except discord.HTTPException as e:
                log.warning(
                    "giveaway_owner anim: slow edit failed frame %d: %s",
                    i, e,
                )
            await asyncio.sleep(interval)

        return winner_uid

    async def _settle_finished(
        self, giveaway_id: int, winners: list[int],
    ) -> None:
        """Persist final winners + flip status to 'finished' + repaint
        the card. Idempotent-ish: no-op if the row already finished."""
        async with self.bot.db.session() as s:
            gw = await s.get(Giveaway, giveaway_id)
            if gw is None:
                return
            if gw.status not in ("rolling", "open"):
                return
            gw.status = "finished"
            gw.winners_csv = ",".join(str(u) for u in winners) if winners else None
            gw.finished_at = datetime.now(timezone.utc)
            channel_id = gw.channel_id
            name = gw.name
            await s.commit()
        await self._refresh_card(giveaway_id)
        # Public shout so winners get an actual ping (embed mentions
        # don't ping).
        if winners:
            guild_id = None
            for g in self.bot.guilds:
                ch = g.get_channel(channel_id)
                if isinstance(ch, (discord.TextChannel, discord.Thread)):
                    guild_id = g.id
                    break
            if guild_id is not None:
                guild = self.bot.get_guild(guild_id)
                if guild is not None:
                    ch = guild.get_channel(channel_id)
                    if isinstance(ch, (discord.TextChannel, discord.Thread)):
                        mentions = " ".join(f"<@{u}>" for u in winners)
                        try:
                            await ch.send(
                                f"🎉 Congrats {mentions} — you won the "
                                f"**{name}** giveaway! DM the owner to "
                                "claim your prize.",
                                allowed_mentions=discord.AllowedMentions(
                                    users=True,
                                ),
                            )
                        except discord.Forbidden:
                            log.warning(
                                "giveaway_owner: forbidden to ping winners in %s",
                                channel_id,
                            )


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(GiveawayOwner(bot))
