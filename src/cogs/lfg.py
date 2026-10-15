from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db.models import GuildConfig, LFGParty, LFGSchedule
from ..views.lfg import build_lfg_embed, build_lfg_view

if TYPE_CHECKING:
    from ..bot import AkimBot


log = logging.getLogger("akim-bot.lfg")

MAX_SLOT_PER_ROLE = 30

# How often the scheduler wakes to check for schedules that need to
# fire. 60s means a schedule at HH:MM lands within the correct minute
# after ~30s average — plenty tight for a party ping.
_SCHEDULER_TICK_SECONDS = 60

_WEEKDAY_LABELS = [
    "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
]

# Party-type presets shared by the manual /lfg subcommands AND
# /lfg-admin schedule-add. Each entry is (display_label, dps, healer,
# tank). Owners can still override slot counts on schedule-add; this
# just gives the "typical" defaults so the /lfg-admin form isn't a
# blank slate.
_PARTY_PRESETS: dict[str, tuple[str, int, int, int]] = {
    "hero_realm": ("Hero Realm", 6, 2, 2),
    "guild_war":  ("Guild War", 18, 6, 6),
    "sword_trial": ("Sword Trial", 3, 1, 1),
    # `custom` requires the caller to pass mode_name + slot counts
    # manually — no preset defaults.
    "custom":     ("", 0, 0, 0),
}


def _tz(offset: int) -> timezone:
    return timezone(timedelta(hours=offset))


def _next_run_at(
    now_utc: datetime,
    repeat_kind: str,
    weekday: int | None,
    hour: int,
    minute: int,
    tz_offset: int,
) -> datetime:
    """Compute the next UTC datetime at which this schedule should
    fire, based on now. For 'daily'/'weekly' the next run is the next
    matching HH:MM (weekday) at or after `now`. For 'once' the value
    picked here is a one-shot — the caller will disable the schedule
    after it lands."""
    tz = _tz(tz_offset)
    local_now = now_utc.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if repeat_kind == "weekly":
        assert weekday is not None
        # Roll forward to the specified weekday. weekday(): Mon=0..Sun=6.
        days_ahead = (weekday - candidate.weekday()) % 7
        if days_ahead == 0 and candidate <= local_now:
            days_ahead = 7
        candidate = candidate + timedelta(days=days_ahead)
    else:
        # daily / once — advance a day if the target time already passed.
        if candidate <= local_now:
            candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc)


class LFG(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        self._scheduler_task: asyncio.Task | None = None

    # ---------- Groups -----------------------------------------------------

    lfg = app_commands.Group(
        name="lfg",
        description="Looking For Group",
        guild_only=True,
    )

    # Owner-only recurring-schedule management. Runtime owner check
    # inside each handler; the @default_permissions gate hides the
    # commands from anyone without Manage Server so regular members
    # aren't confused by admin-only commands.
    lfg_admin = app_commands.Group(
        name="lfg-admin",
        description="(Owner) Manage the /lfg auto-schedule",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    # ---------- Lifecycle --------------------------------------------------

    async def cog_load(self) -> None:
        # Kick the scheduler as soon as the cog loads; it becomes a
        # no-op until the bot is ready + the first schedule lands.
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="lfg-scheduler",
        )

    async def cog_unload(self) -> None:
        t = self._scheduler_task
        if t is not None and not t.done():
            t.cancel()

    # ---------- Permission helper ------------------------------------------

    async def _check_permission(
        self, interaction: discord.Interaction
    ) -> GuildConfig | None:
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
        if cfg is None or cfg.lfg_channel_id is None:
            await interaction.response.send_message(
                "❌ LFG isn't configured for this server. An admin needs to run `/setup-lfg` first.",
                ephemeral=True,
            )
            return None

        allowed_ids = cfg.allowed_role_ids()
        if allowed_ids:
            assert isinstance(interaction.user, discord.Member)
            member_role_ids = {r.id for r in interaction.user.roles}
            if not (member_role_ids & set(allowed_ids)) and not interaction.user.guild_permissions.manage_guild:
                guild = interaction.guild
                role_mentions: list[str] = []
                if guild is not None:
                    for rid in allowed_ids:
                        r = guild.get_role(rid)
                        if r:
                            role_mentions.append(r.mention)
                allowed_txt = ", ".join(role_mentions) if role_mentions else "the configured role"
                await interaction.response.send_message(
                    f"❌ You need one of these roles to use `/lfg`: {allowed_txt}",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return None
        return cfg

    # ---------- Owner check ------------------------------------------------

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild is not None
            and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        ):
            return True
        msg = "❌ Only the server owner can manage the auto-schedule."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    # =======================================================================
    # Shared spawn helper — used by /lfg subcommands AND the scheduler.
    # Takes no interaction, only the ingredients. Returns the created
    # LFGParty id, or None on any config-gone-bad path.
    # =======================================================================

    async def _spawn_party(
        self,
        *,
        guild_id: int,
        creator_id: int,
        mode: str,
        dps: int,
        healer: int,
        tank: int,
        note: str | None,
        voice_channel_id: int | None,
        header_prefix: str | None = None,
        main_limit: int | None = None,
        gw_role_selection: bool = True,
        lfg_type: str | None = None,
        start_time: str | None = None,
    ) -> LFGParty | None:
        """Persist an LFGParty + post the embed. Returns the fresh row
        (with members loaded) on success, None if the guild config is
        broken (missing channel, bot forbidden, etc). Not interaction-
        aware so the scheduler can call it too."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            log.warning("lfg spawn: guild %s not in cache", guild_id)
            return None
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
        if cfg is None or cfg.lfg_channel_id is None:
            log.warning("lfg spawn: guild %s has no lfg_channel_id", guild_id)
            return None

        # Resolve per-type channel (falls back to default lfg_channel_id)
        per_type_ch_id: int | None = None
        if lfg_type == "hero_realm":
            per_type_ch_id = cfg.lfg_hero_realm_channel_id
        elif lfg_type == "sword_trial":
            per_type_ch_id = cfg.lfg_sword_trial_channel_id
        elif lfg_type == "custom":
            per_type_ch_id = cfg.lfg_custom_channel_id
        resolved_ch_id = per_type_ch_id or cfg.lfg_channel_id

        channel = guild.get_channel(resolved_ch_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            if per_type_ch_id is not None:
                channel = guild.get_channel(cfg.lfg_channel_id)  # type: ignore[arg-type]
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.warning(
                "lfg spawn: lfg_channel_id %s in guild %s no longer valid",
                cfg.lfg_channel_id, guild_id,
            )
            return None

        async with self.bot.db.session() as s:
            party = LFGParty(
                guild_id=guild_id,
                channel_id=channel.id,
                creator_id=creator_id,
                mode=mode,
                dps_slots=dps,
                healer_slots=healer,
                tank_slots=tank,
                main_limit=main_limit,
                gw_role_selection=gw_role_selection,
                voice_channel_id=voice_channel_id,
                note=note,
                start_time=start_time,
                is_open=True,
            )
            s.add(party)
            await s.commit()
            party = await s.scalar(
                select(LFGParty)
                .where(LFGParty.id == party.id)
                .options(selectinload(LFGParty.members))
            )
        assert party is not None
        embed = await build_lfg_embed(self.bot, party, guild)
        view = build_lfg_view(party)

        creator_mention = f"<@{creator_id}>"
        content = (
            header_prefix if header_prefix
            else f"📢 **New party** • made by {creator_mention}"
        )
        try:
            msg = await channel.send(content=content, embed=embed, view=view)
        except discord.Forbidden:
            log.warning(
                "lfg spawn: forbidden to post in channel %s (guild %s)",
                channel.id, guild_id,
            )
            # Delete the orphan party row we just committed — it has
            # no message to pair with.
            async with self.bot.db.session() as s:
                orphan = await s.get(LFGParty, party.id)
                if orphan is not None:
                    await s.delete(orphan)
                    await s.commit()
            return None
        async with self.bot.db.session() as s:
            p = await s.get(LFGParty, party.id)
            if p is not None:
                p.message_id = msg.id
                await s.commit()

        # Auto-create a linked GVG match for Guild War parties
        if main_limit is not None:
            gvg_cog = self.bot.cogs.get("GvG")
            if gvg_cog is not None:
                try:
                    gvg_match_id = await gvg_cog.auto_create_for_gw(  # type: ignore[union-attr]
                        guild=guild,
                        lfg_party_id=party.id,
                        name="Guild War",
                        main_limit=main_limit,
                        creator_id=creator_id,
                        note=note,
                    )
                    if gvg_match_id is not None:
                        async with self.bot.db.session() as s:
                            p = await s.get(LFGParty, party.id)
                            if p is not None:
                                p.gvg_match_id = gvg_match_id
                                await s.commit()
                except Exception:
                    log.exception(
                        "lfg: failed to auto-create GVG match for GW party %s",
                        party.id,
                    )

        return party

    # =======================================================================
    # Manual /lfg commands (kept minimal — just call _spawn_party)
    # =======================================================================

    async def _create_party(
        self,
        interaction: discord.Interaction,
        mode_label: str,
        dps: int,
        healer: int,
        tank: int,
        note: str | None,
        voice: discord.VoiceChannel | discord.StageChannel | None = None,
        lfg_type: str | None = None,
        start_time: str | None = None,
    ) -> None:
        # Validate slot counts up-front while we still have the
        # response window open (send_message must precede any deferred
        # follow-up).
        for n, v in (("DPS", dps), ("Healer", healer), ("Tank", tank)):
            if v < 0 or v > MAX_SLOT_PER_ROLE:
                await interaction.response.send_message(
                    f"❌ {n} count must be between 0 and {MAX_SLOT_PER_ROLE}.",
                    ephemeral=True,
                )
                return
        if dps + healer + tank < 1:
            await interaction.response.send_message(
                "❌ Party must have at least 1 slot.", ephemeral=True
            )
            return
        cfg = await self._check_permission(interaction)
        if cfg is None:
            return

        assert interaction.guild_id is not None
        party = await self._spawn_party(
            guild_id=interaction.guild_id,
            creator_id=interaction.user.id,
            mode=mode_label,
            dps=dps, healer=healer, tank=tank,
            note=note,
            voice_channel_id=voice.id if voice else None,
            lfg_type=lfg_type,
            start_time=start_time,
        )
        if party is None:
            await interaction.response.send_message(
                "❌ Couldn't create the party — the LFG channel may have been "
                "deleted or the bot lacks permission to post. Admin should re-run `/setup-lfg`.",
                ephemeral=True,
            )
            return

        if party.channel_id == interaction.channel_id:
            await interaction.response.send_message(
                "✅ Party created ↑", ephemeral=True
            )
        else:
            channel_mention = f"<#{party.channel_id}>"
            await interaction.response.send_message(
                f"✅ Party created in {channel_mention}", ephemeral=True
            )

    # ---------- Subcommands ----------

    @lfg.command(name="hero-realm", description="Create a Hero Realm party (10 members)")
    @app_commands.describe(
        dps="Number of DPS slots",
        healer="Number of Healer slots",
        tank="Number of Tank slots",
        voice="Voice channel Start Match will pull the team into (optional)",
        note="Note (optional)",
        start_time="Start time: DD/MM HH:MM or shorthand 13102130 (optional)",
    )
    async def hero_realm(
        self,
        interaction: discord.Interaction,
        dps: app_commands.Range[int, 0, 10] = 6,
        healer: app_commands.Range[int, 0, 10] = 2,
        tank: app_commands.Range[int, 0, 10] = 2,
        voice: discord.VoiceChannel | discord.StageChannel | None = None,
        note: str | None = None,
        start_time: str | None = None,
    ) -> None:
        await self._create_party(interaction, "Hero Realm", dps, healer, tank, note, voice, "hero_realm", start_time)

    @lfg.command(
        name="guild-war",
        description="Create a Guild War vote — members pick a day, auto Main/Reserve",
    )
    @app_commands.describe(
        main_limit="Maximum main roster size (default 30)",
        voice="Voice channel Start Match will pull the team into (optional)",
        note="Note (optional)",
        start_time="Start time: DD/MM HH:MM or shorthand 13102130 (optional)",
    )
    async def guild_war(
        self,
        interaction: discord.Interaction,
        main_limit: app_commands.Range[int, 1, 100] = 30,
        voice: discord.VoiceChannel | discord.StageChannel | None = None,
        note: str | None = None,
        start_time: str | None = None,
    ) -> None:
        cfg = await self._check_permission(interaction)
        if cfg is None:
            return
        assert interaction.guild_id is not None
        party = await self._spawn_party(
            guild_id=interaction.guild_id,
            creator_id=interaction.user.id,
            mode="Guild War",
            dps=0, healer=0, tank=0,
            main_limit=int(main_limit),
            gw_role_selection=False,
            note=note,
            voice_channel_id=voice.id if voice else None,
            start_time=start_time,
        )
        if party is None:
            await interaction.response.send_message(
                "❌ Failed to create party — the LFG channel may have been deleted or the bot lacks permissions.",
                ephemeral=True,
            )
            return
        if party.channel_id == interaction.channel_id:
            await interaction.response.send_message("✅ Guild War vote created ↑", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"✅ Guild War vote created in <#{party.channel_id}>", ephemeral=True
            )

    @lfg.command(name="sword-trial", description="Create a Sword Trial party (5 members)")
    @app_commands.describe(
        dps="Number of DPS slots",
        healer="Number of Healer slots",
        tank="Number of Tank slots",
        voice="Voice channel Start Match will pull the team into (optional)",
        note="Note (optional)",
        start_time="Start time: DD/MM HH:MM or shorthand 13102130 (optional)",
    )
    async def sword_trial(
        self,
        interaction: discord.Interaction,
        dps: app_commands.Range[int, 0, 5] = 3,
        healer: app_commands.Range[int, 0, 5] = 1,
        tank: app_commands.Range[int, 0, 5] = 1,
        voice: discord.VoiceChannel | discord.StageChannel | None = None,
        note: str | None = None,
        start_time: str | None = None,
    ) -> None:
        await self._create_party(interaction, "Sword Trial", dps, healer, tank, note, voice, "sword_trial", start_time)

    @lfg.command(name="custom", description="Create a party with a custom mode name")
    @app_commands.describe(
        mode_name="Mode name (e.g. Weekly Boss, PvP 3v3...)",
        dps="Number of DPS slots",
        healer="Number of Healer slots",
        tank="Number of Tank slots",
        voice="Voice channel Start Match will pull the team into (optional)",
        note="Note (optional)",
        start_time="Start time: DD/MM HH:MM or shorthand 13102130 (optional)",
    )
    async def custom(
        self,
        interaction: discord.Interaction,
        mode_name: app_commands.Range[str, 1, 32],
        dps: app_commands.Range[int, 0, 30] = 0,
        healer: app_commands.Range[int, 0, 30] = 0,
        tank: app_commands.Range[int, 0, 30] = 0,
        voice: discord.VoiceChannel | discord.StageChannel | None = None,
        note: str | None = None,
        start_time: str | None = None,
    ) -> None:
        await self._create_party(interaction, mode_name, dps, healer, tank, note, voice, "custom", start_time)

    # =======================================================================
    # /lfg-admin schedule-*  — owner-managed recurring spawns
    # =======================================================================

    @lfg_admin.command(
        name="schedule-add",
        description="(Owner) Add an auto-schedule for party creation",
    )
    @app_commands.describe(
        party_type=(
            "Party type. Preset (hero-realm/guild-war/sword-trial) auto-fills "
            "slots; 'custom' requires dps/healer/tank + mode_name."
        ),
        mode_name=(
            "Display name. Leave blank for a preset to use the default name. "
            "REQUIRED when party_type=custom."
        ),
        dps="DPS slots (leave blank to use preset default)",
        healer="Healer slots (leave blank to use preset default)",
        tank="Tank slots (leave blank to use preset default)",
        repeat="Repeat cycle: once / daily / weekly",
        run_time="Spawn time — 'HH:MM' 24h format (e.g. 21:00)",
        weekday="Only used when repeat=weekly. Mon..Sun.",
        tz_offset="Timezone (e.g. 7 = VN, 0 = UTC, -5 = EST). Range -12..14",
        voice="Voice channel Start Match will pull into (optional)",
        note="Extra note on the party (optional)",
    )
    @app_commands.choices(
        party_type=[
            app_commands.Choice(name="Hero Realm (10 players)", value="hero_realm"),
            app_commands.Choice(name="Guild War (30 players)",  value="guild_war"),
            app_commands.Choice(name="Sword Trial (5 players)", value="sword_trial"),
            app_commands.Choice(name="Custom (pick slot counts)", value="custom"),
        ],
        repeat=[
            app_commands.Choice(name="once", value="once"),
            app_commands.Choice(name="daily", value="daily"),
            app_commands.Choice(name="weekly", value="weekly"),
        ],
        weekday=[
            app_commands.Choice(name="Mon", value=0),
            app_commands.Choice(name="Tue", value=1),
            app_commands.Choice(name="Wed", value=2),
            app_commands.Choice(name="Thu", value=3),
            app_commands.Choice(name="Fri", value=4),
            app_commands.Choice(name="Sat", value=5),
            app_commands.Choice(name="Sun", value=6),
        ],
    )
    async def schedule_add(
        self,
        interaction: discord.Interaction,
        party_type: app_commands.Choice[str],
        repeat: app_commands.Choice[str],
        run_time: app_commands.Range[str, 4, 5],
        mode_name: app_commands.Range[str, 1, 32] | None = None,
        dps: app_commands.Range[int, 0, 30] | None = None,
        healer: app_commands.Range[int, 0, 30] | None = None,
        tank: app_commands.Range[int, 0, 30] | None = None,
        weekday: app_commands.Choice[int] | None = None,
        tz_offset: app_commands.Range[int, -12, 14] = 7,
        voice: discord.VoiceChannel | discord.StageChannel | None = None,
        note: str | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return

        # Resolve mode_name + slot counts from the preset, letting the
        # explicit params override on a per-field basis.
        preset_label, preset_dps, preset_healer, preset_tank = (
            _PARTY_PRESETS[party_type.value]
        )
        if party_type.value == "custom":
            if not mode_name:
                await interaction.response.send_message(
                    "❌ `custom` requires `mode_name`.", ephemeral=True,
                )
                return
            if dps is None or healer is None or tank is None:
                await interaction.response.send_message(
                    "❌ `custom` requires `dps` + `healer` + `tank`.",
                    ephemeral=True,
                )
                return
            resolved_mode = mode_name
            resolved_dps = int(dps)
            resolved_healer = int(healer)
            resolved_tank = int(tank)
        else:
            resolved_mode = mode_name or preset_label
            resolved_dps = int(dps) if dps is not None else preset_dps
            resolved_healer = (
                int(healer) if healer is not None else preset_healer
            )
            resolved_tank = int(tank) if tank is not None else preset_tank

        if resolved_dps + resolved_healer + resolved_tank < 1:
            await interaction.response.send_message(
                "❌ Party must have at least 1 slot.", ephemeral=True,
            )
            return
        # Parse HH:MM.
        try:
            h_str, m_str = run_time.split(":", 1)
            hour, minute = int(h_str), int(m_str)
            assert 0 <= hour <= 23 and 0 <= minute <= 59
        except (ValueError, AssertionError):
            await interaction.response.send_message(
                "❌ `run_time` must use `HH:MM` format (e.g. `21:00`, `08:30`).",
                ephemeral=True,
            )
            return

        repeat_kind = repeat.value
        weekday_val: int | None = None
        if repeat_kind == "weekly":
            if weekday is None:
                await interaction.response.send_message(
                    "❌ `weekly` requires `weekday` (Mon..Sun).", ephemeral=True,
                )
                return
            weekday_val = weekday.value

        now = datetime.now(timezone.utc)
        next_run = _next_run_at(
            now, repeat_kind, weekday_val, hour, minute, tz_offset,
        )

        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            row = LFGSchedule(
                guild_id=interaction.guild_id,
                created_by=interaction.user.id,
                mode=resolved_mode,
                dps_slots=resolved_dps,
                healer_slots=resolved_healer,
                tank_slots=resolved_tank,
                voice_channel_id=voice.id if voice else None,
                note=note,
                repeat_kind=repeat_kind,
                weekday=weekday_val,
                run_hour=hour,
                run_minute=minute,
                tz_offset=int(tz_offset),
                enabled=True,
                next_run_at=next_run,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)

        embed = discord.Embed(
            title=f"✅ Schedule #{row.id} added",
            color=discord.Color.green(),
        )
        embed.add_field(name="Party type", value=party_type.name, inline=True)
        embed.add_field(name="Mode", value=resolved_mode, inline=True)
        embed.add_field(
            name="Slots",
            value=(
                f"DPS {resolved_dps} • Healer {resolved_healer} "
                f"• Tank {resolved_tank} "
                f"(total {resolved_dps + resolved_healer + resolved_tank})"
            ),
            inline=False,
        )
        embed.add_field(name="Repeat", value=repeat_kind, inline=True)
        when_txt = f"{hour:02d}:{minute:02d}"
        if repeat_kind == "weekly":
            when_txt = f"{_WEEKDAY_LABELS[weekday_val]} {when_txt}"
        when_txt += f" (UTC{tz_offset:+d})"
        embed.add_field(name="Run time", value=when_txt, inline=True)
        embed.add_field(
            name="Next run",
            value=f"<t:{int(next_run.timestamp())}:F> "
                  f"(<t:{int(next_run.timestamp())}:R>)",
            inline=False,
        )
        if voice is not None:
            embed.add_field(name="Voice", value=voice.mention, inline=True)
        if note is not None:
            embed.add_field(name="Note", value=note, inline=False)
        await interaction.followup.send(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @lfg_admin.command(
        name="schedule-list",
        description="(Owner) List all auto-schedules for this server",
    )
    async def schedule_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            rows = list((await s.execute(
                select(LFGSchedule)
                .where(LFGSchedule.guild_id == interaction.guild_id)
                .order_by(LFGSchedule.enabled.desc(), LFGSchedule.next_run_at)
            )).scalars().all())
        if not rows:
            await interaction.followup.send(
                "_No schedules yet. Use `/lfg-admin schedule-add`._",
                ephemeral=True,
            )
            return
        lines: list[str] = []
        for r in rows:
            state = "✅" if r.enabled else "🚫"
            when = f"{r.run_hour:02d}:{r.run_minute:02d}"
            if r.repeat_kind == "weekly" and r.weekday is not None:
                when = f"{_WEEKDAY_LABELS[r.weekday]} {when}"
            lines.append(
                f"{state} `#{r.id}` **{r.mode}** — "
                f"{r.dps_slots}D/{r.healer_slots}H/{r.tank_slots}T • "
                f"`{r.repeat_kind}` {when} (UTC{r.tz_offset:+d})\n"
                f"       Next: <t:{int(r.next_run_at.timestamp())}:R>"
            )
        embed = discord.Embed(
            title=f"📅 LFG schedules ({len(rows)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @lfg_admin.command(
        name="schedule-remove",
        description="(Owner) Remove a schedule",
    )
    @app_commands.describe(schedule_id="Schedule ID (see schedule-list)")
    async def schedule_remove(
        self,
        interaction: discord.Interaction,
        schedule_id: app_commands.Range[int, 1, 9_999_999],
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            row = await s.get(LFGSchedule, int(schedule_id))
            if row is None or row.guild_id != interaction.guild_id:
                await interaction.followup.send(
                    f"❌ Schedule `#{schedule_id}` not found in this server.",
                    ephemeral=True,
                )
                return
            mode = row.mode
            await s.delete(row)
            await s.commit()
        await interaction.followup.send(
            f"🗑 Removed schedule `#{schedule_id}` ({mode}).", ephemeral=True,
        )

    @lfg_admin.command(
        name="schedule-toggle",
        description="(Owner) Enable/disable a schedule",
    )
    @app_commands.describe(schedule_id="Schedule ID (see schedule-list)")
    async def schedule_toggle(
        self,
        interaction: discord.Interaction,
        schedule_id: app_commands.Range[int, 1, 9_999_999],
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            row = await s.get(LFGSchedule, int(schedule_id))
            if row is None or row.guild_id != interaction.guild_id:
                await interaction.followup.send(
                    f"❌ Schedule `#{schedule_id}` not found.", ephemeral=True,
                )
                return
            row.enabled = not row.enabled
            # If we just re-enabled a schedule whose next_run_at is in
            # the past (was disabled long enough to lapse), advance it
            # so it doesn't fire N times in a row.
            if row.enabled and row.next_run_at <= datetime.now(timezone.utc):
                row.next_run_at = _next_run_at(
                    datetime.now(timezone.utc),
                    row.repeat_kind, row.weekday,
                    row.run_hour, row.run_minute, row.tz_offset,
                )
            new_state = row.enabled
            await s.commit()
        await interaction.followup.send(
            f"{'✅ Enabled' if new_state else '🚫 Disabled'} schedule `#{schedule_id}`.",
            ephemeral=True,
        )

    # =======================================================================
    # Scheduler loop
    # =======================================================================

    async def _scheduler_loop(self) -> None:
        """Poll every _SCHEDULER_TICK_SECONDS, fire any enabled
        schedule whose next_run_at has lapsed, then advance
        next_run_at (or disable for 'once'). Long-running: lives for
        the cog's lifetime and is cancelled by cog_unload."""
        # Wait for the bot to be ready so guild caches are populated —
        # otherwise the first tick misses everything.
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._scheduler_tick()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("lfg scheduler tick crashed")
            try:
                await asyncio.sleep(_SCHEDULER_TICK_SECONDS)
            except asyncio.CancelledError:
                return

    async def _scheduler_tick(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.bot.db.session() as s:
            due = list((await s.execute(
                select(LFGSchedule).where(
                    LFGSchedule.enabled.is_(True),
                    LFGSchedule.next_run_at <= now,
                )
            )).scalars().all())
        if not due:
            return
        for row in due:
            try:
                party = await self._spawn_party(
                    guild_id=row.guild_id,
                    creator_id=row.created_by,
                    mode=row.mode,
                    dps=row.dps_slots,
                    healer=row.healer_slots,
                    tank=row.tank_slots,
                    note=row.note,
                    voice_channel_id=row.voice_channel_id,
                    header_prefix=(
                        f"📅 **Auto-schedule** — {row.mode} • "
                        f"set by <@{row.created_by}>"
                    ),
                )
            except Exception:
                log.exception(
                    "lfg scheduler: spawn crashed for schedule %s", row.id,
                )
                party = None

            # Advance next_run_at regardless of spawn success — if the
            # guild config is broken we don't want to hammer it every
            # tick until the owner fixes it. They'll see the missed
            # runs in schedule-list.
            new_next = _next_run_at(
                now, row.repeat_kind, row.weekday,
                row.run_hour, row.run_minute, row.tz_offset,
            )
            async with self.bot.db.session() as s:
                fresh = await s.get(LFGSchedule, row.id)
                if fresh is None:
                    continue
                fresh.last_run_at = now
                if party is not None:
                    fresh.last_party_id = party.id
                if fresh.repeat_kind == "once":
                    # One-shot done. Leave the row for audit; owner
                    # can delete via schedule-remove.
                    fresh.enabled = False
                else:
                    fresh.next_run_at = new_next
                await s.commit()


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(LFG(bot))
