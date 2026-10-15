"""`/link-desktop` — confirm a link code shown in the Desktop Pet app.

Flow:
1. User opens akim-desk-pet → app calls POST /api/desktop/link/create
   → shows a code like AKIM-Q4P7XW.
2. User opens Discord (in the guild whose pets they want to display),
   runs `/link-desktop code:AKIM-Q4P7XW`.
3. This cog calls link_service.confirm_link_session directly (same
   process — no HTTP round-trip), which sets discord_user_id +
   guild_id on the session and pre-computes the JWT hash.
4. Desktop's next /status poll picks up the token and starts polling
   the pet endpoints.

The cog is loaded only when the desk-api is enabled (DESKTOP_TOKEN_SECRET
configured) — otherwise the command would always error out for users.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..desk_api.config import DeskApiSettings
from ..desk_api.link_service import LinkError, confirm_link_session

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.link-desktop")

# AKIM- prefix + 6 chars from the same alphabet as auth.py. Loose match
# so we can still hand back a friendly error rather than failing the
# Range validator.
_CODE_RE = re.compile(r"^AKIM-[A-Z0-9]{4,12}$", re.IGNORECASE)


class LinkDesktop(commands.Cog):
    def __init__(self, bot: "AkimBot", desk_settings: DeskApiSettings) -> None:
        self.bot = bot
        self.desk_settings = desk_settings

    @app_commands.command(
        name="link-desktop",
        description="Link your Discord account with akim-desk-pet",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        code="Link code shown in the desktop app (e.g. AKIM-Q4P7XW)",
    )
    async def link_desktop(
        self,
        interaction: discord.Interaction,
        code: app_commands.Range[str, 6, 32],
    ) -> None:
        assert interaction.guild_id is not None
        cleaned = code.strip().upper()
        if not _CODE_RE.match(cleaned):
            await interaction.response.send_message(
                "❌ Code has the wrong format. Must be `AKIM-XXXXXX`.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            try:
                confirmed = await confirm_link_session(
                    s, self.desk_settings,
                    code=cleaned,
                    discord_user_id=interaction.user.id,
                    guild_id=interaction.guild_id,
                )
            except LinkError as e:
                await interaction.response.send_message(
                    f"❌ {e.message}", ephemeral=True,
                )
                return

        guild_name = interaction.guild.name if interaction.guild else "this server"
        embed = discord.Embed(
            title="🔗 Desktop Pet linked",
            description=(
                f"Code `{confirmed.code}` is linked with <@"
                f"{interaction.user.id}> in **{guild_name}**.\n\n"
                "Return to the desktop app — your pet will appear in a few "
                "seconds after the app polls its next link status."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(
            text=(
                "Each link is tied to one server. Switching servers means "
                "running the command again in that server with a new code."
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: "AkimBot") -> None:
    # The cog reads desk-api settings via the bot instance so callers
    # don't have to thread them through manually.
    desk_settings = getattr(bot, "desk_settings", None)
    if desk_settings is None:
        log.warning(
            "LinkDesktop cog loaded but bot.desk_settings is missing — "
            "skipping registration."
        )
        return
    await bot.add_cog(LinkDesktop(bot, desk_settings))
