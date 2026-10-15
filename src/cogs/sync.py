"""Paste-based /sync from WWM Workshop ephemeral results.

Why paste, not listener
-----------------------
The WWM Workshop `/player` command returns an ephemeral message (only the
caller sees it). Discord does not deliver ephemeral messages to other
bots' gateway events, so Akim physically cannot read the result via
`on_message`. The workaround is to have the user copy the text out of the
ephemeral and paste it into a modal. Akim then parses the paste.

Flow
----
1. User runs `/player` in WWM Workshop, enters UID, sees the ephemeral
   result card.
2. User selects the body text of that ephemeral and copies (Ctrl+A in the
   embed area, then Ctrl+C).
3. User runs `/sync uid:<uid>` in Akim — Akim opens a modal with one big
   textarea immediately.
4. User pastes the copied text and submits.
5. Akim regex-parses the paste for Level, UID, Build Power, Guild,
   Elegance, updates the Player row, and confirms with a diff.

Why all the DB work runs INSIDE the modal
-----------------------------------------
`send_modal` has to be the FIRST response to a slash-command interaction
— you cannot `defer()` first and then open a modal. That means every
line of code between the interaction handler entry and `send_modal` runs
against Discord's hard 3-second deadline. A slow Postgres round-trip or
a large `players` scan there breaks the whole flow with a "did not
respond" toast.

So the slash handler now only does a synchronous UID regex check, then
opens the modal. The player lookup, ownership check, parse, and DB
update all live in `_PasteModal.on_submit`, where we CAN defer and get
the 15-minute followup window.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from ..db.models import GuildConfig, Player

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.sync")


_UID_VALID = re.compile(r"^\d{4,16}$")


@dataclass
class ParsedWWM:
    ingame_name: str | None = None
    uid: str | None = None
    level: int | None = None
    build_power: int | None = None
    elegance: int | None = None
    guild: str | None = None


# Field labels we recognize in the pasted text (lowercased).
_KNOWN_LABELS = {
    "level",
    "uid",
    "build power",
    "guild",
    "sect",  # known but not stored
    "elegance",
    "likes",  # known but not stored
    "playtime",  # known but not stored
    "status",  # known but not stored
    "server",  # known but not stored
    "platform",
    "cross match",
    "has illness",
    "in team",
    "solo level",
    "pf solo",
    "1v1 arena",
    "account created",
}


def _first_number(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"-?[\d,]+", text)
    if not m:
        return None
    raw = m.group(0).replace(",", "")
    try:
        return int(raw)
    except ValueError:
        return None


def parse_wwm_text(text: str) -> ParsedWWM:
    """Parse the text copied from a WWM Workshop player ephemeral embed.

    Discord renders embed fields as 'label\\nvalue' lines when selected and
    copied. The bot's title (player ingame name) appears on its own line
    before the first labeled field. We walk the lines, and whenever the
    line matches a known label we take the next non-empty line as the
    value.
    """
    out = ParsedWWM()
    lines = [ln.strip() for ln in text.splitlines()]
    # Drop empty lines but remember positions so we can still find the
    # "value comes after label" pattern.
    nonempty = [ln for ln in lines if ln]
    if not nonempty:
        return out

    # Heuristic for ingame name: first non-empty line that ISN'T a known
    # label. Stops as soon as we hit a label line so subtitle lines like
    # "Guild Leader" are skipped if they appear first.
    for ln in nonempty:
        if ln.lower() in _KNOWN_LABELS:
            break
        # Skip obvious metadata-only lines
        if ln.lower().startswith(("choose how", "searching", "looking up")):
            continue
        if out.ingame_name is None:
            out.ingame_name = ln
            continue
        # If we already have a name, additional pre-label lines (e.g.
        # "Guild Leader") are role descriptions — ignore.

    # Walk non-empty lines as (label, value) pairs
    i = 0
    while i < len(nonempty):
        label = nonempty[i].lower()
        if label not in _KNOWN_LABELS:
            i += 1
            continue
        # The next non-empty line that ISN'T a known label is our value.
        j = i + 1
        value_lines: list[str] = []
        while j < len(nonempty):
            if nonempty[j].lower() in _KNOWN_LABELS:
                break
            value_lines.append(nonempty[j])
            j += 1
        value = " ".join(value_lines).strip()

        if label == "uid":
            digits = re.sub(r"\D", "", value)
            if digits:
                out.uid = digits
        elif label == "level":
            out.level = _first_number(value)
        elif label == "build power":
            out.build_power = _first_number(value)
        elif label == "elegance":
            out.elegance = _first_number(value)
        elif label == "guild":
            out.guild = value or None

        i = j if j > i else i + 1

    return out


class _PasteModal(discord.ui.Modal):
    """Paste target for the WWM Workshop ephemeral. Owns the full DB
    lookup + auth check + apply pipeline so the slash handler stays
    fast enough to always beat Discord's 3-second response deadline."""

    def __init__(self, bot: "AkimBot", expected_uid: str) -> None:
        super().__init__(title="Paste WWM Workshop result")
        self.bot = bot
        self.expected_uid = expected_uid
        self.payload = discord.ui.TextInput(
            label="WWM Workshop embed content",
            placeholder=(
                "Copy the full embed text from WWM Workshop (Ctrl+A inside "
                "the embed, Ctrl+C), then paste it here."
            ),
            style=discord.TextStyle.paragraph,
            min_length=20,
            max_length=4000,
            required=True,
        )
        self.add_item(self.payload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Defer immediately so we have 15 minutes to do the DB round-trips
        # + parse + apply. Modal on_submit is a fresh interaction, so
        # defer here even though the slash handler already responded (its
        # response was the modal itself, this is a new interaction).
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            # Someone else already responded (shouldn't happen from a fresh
            # modal submit, but be defensive). Continue best-effort with
            # followup semantics.
            pass

        parsed = parse_wwm_text(str(self.payload.value))

        if not parsed.uid:
            await interaction.followup.send(
                "❌ No UID found in the pasted text. "
                "Make sure you copied the full 'UID' field from the embed.",
                ephemeral=True,
            )
            return
        if parsed.uid != self.expected_uid:
            await interaction.followup.send(
                f"❌ UID in the paste (`{parsed.uid}`) doesn't match the UID "
                f"you ran /sync with (`{self.expected_uid}`). Sync cancelled "
                "so we don't write the wrong data.",
                ephemeral=True,
            )
            return

        assert interaction.guild_id is not None

        # Player lookup — was in the slash handler before; moved here so
        # the slash path can't blow the 3s response deadline on a slow DB.
        async with self.bot.db.session() as s:
            player = await s.scalar(
                select(Player).where(
                    Player.guild_id == interaction.guild_id,
                    Player.ingame_uid == self.expected_uid,
                )
            )
        if player is None:
            await interaction.followup.send(
                f"❌ No player with UID `{self.expected_uid}` in this server. "
                "Register with /player-reg first, then sync.",
                ephemeral=True,
            )
            return

        # Ownership / admin gate. Owner-only unless the caller has
        # manage_guild — matches the previous UX exactly.
        is_owner = player.discord_id == interaction.user.id
        is_admin = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )
        if not (is_owner or is_admin):
            await interaction.followup.send(
                "❌ This UID isn't yours. Only the UID owner or an admin "
                "can sync it.",
                ephemeral=True,
            )
            return

        changes: list[str] = []
        player_id = player.id
        async with self.bot.db.session() as s:
            player = await s.get(Player, player_id)
            if player is None:
                await interaction.followup.send(
                    "❌ Player record no longer exists.", ephemeral=True,
                )
                return

            cfg = await s.get(GuildConfig, player.guild_id)

            if parsed.ingame_name and parsed.ingame_name != player.ingame_name:
                changes.append(
                    f"Name: `{player.ingame_name}` → `{parsed.ingame_name}`"
                )
                player.ingame_name = parsed.ingame_name[:64]
            if parsed.build_power is not None and parsed.build_power != player.luc_chien:
                changes.append(
                    f"Build Power: `{player.luc_chien:,}` → `{parsed.build_power:,}`"
                )
                player.luc_chien = parsed.build_power
            if parsed.level is not None and parsed.level != player.level:
                changes.append(
                    f"Level: `{player.level or '—'}` → `{parsed.level}`"
                )
                player.level = parsed.level
            if parsed.elegance is not None and parsed.elegance != player.elegance:
                changes.append(
                    f"Elegance: `{player.elegance or '—'}` → `{parsed.elegance:,}`"
                )
                player.elegance = parsed.elegance
            if parsed.guild is not None and cfg and cfg.guild_name:
                is_member = parsed.guild.strip() == cfg.guild_name.strip()
                if is_member != player.is_guild_member:
                    changes.append(
                        f"Guild member: `{player.is_guild_member}` → `{is_member}`"
                    )
                    player.is_guild_member = is_member

            player.last_synced_at = datetime.now(timezone.utc)
            await s.commit()
            player_id = player.id

        async with self.bot.db.session() as s:
            fresh = await s.get(Player, player_id)

        # Now that the player has a real ingame_name (and possibly server),
        # update the target Discord nickname. Resolve from player.discord_id,
        # not interaction.user — an admin syncing another member's UID must
        # rename that member, not themselves.
        nick_warning: str | None = None
        if fresh is not None and interaction.guild is not None:
            target_member = interaction.guild.get_member(fresh.discord_id)
            if target_member is not None:
                from ..views.nickname import sync_nickname
                nick_warning = await sync_nickname(
                    self.bot, target_member, fresh, reason="Akim Bot /sync"
                )

        embed = discord.Embed(
            title=f"✅ Synced UID `{parsed.uid}`", color=discord.Color.green()
        )
        if nick_warning:
            embed.add_field(name="Nickname update", value=nick_warning, inline=False)
        embed.add_field(
            name="In-game Name", value=parsed.ingame_name or "—", inline=True
        )
        if parsed.level is not None:
            embed.add_field(name="Level", value=str(parsed.level), inline=True)
        if parsed.build_power is not None:
            embed.add_field(
                name="Build Power", value=f"{parsed.build_power:,}", inline=True
            )
        if parsed.elegance is not None:
            embed.add_field(
                name="Elegance", value=f"{parsed.elegance:,}", inline=True
            )
        if parsed.guild is not None:
            embed.add_field(name="Guild", value=parsed.guild, inline=True)
        if changes:
            embed.add_field(
                name=f"Changes ({len(changes)})",
                value="\n".join(f"• {c}" for c in changes)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Changes",
                value="_No changes — data in DB already matches._",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


class Sync(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot

    @app_commands.command(
        name="sync",
        description="Sync player info — paste the ephemeral text from WWM Workshop",
    )
    @app_commands.guild_only()
    @app_commands.describe(uid="In-game UID to sync (4-16 digits)")
    async def sync(
        self,
        interaction: discord.Interaction,
        uid: app_commands.Range[str, 4, 16],
    ) -> None:
        # Fast path only — anything slow moves into the modal's on_submit.
        # Discord requires send_modal to be the FIRST response, so we can't
        # defer here. Any DB call or slow lookup between here and
        # send_modal risks a "did not respond" toast.
        assert interaction.guild_id is not None
        uid_clean = uid.strip()

        if not _UID_VALID.match(uid_clean):
            await interaction.response.send_message(
                "❌ UID must be 4-16 digits.", ephemeral=True
            )
            return

        await interaction.response.send_modal(_PasteModal(self.bot, uid_clean))


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(Sync(bot))
