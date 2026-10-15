"""Shared helpers for building a Discord nickname from a Player record."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..db.models import GuildConfig, Player, Server

if TYPE_CHECKING:
    from ..bot import AkimBot


DISCORD_NICK_MAX = 32


def build_nickname(ingame_name: str, server: Server | None) -> str:
    """Build the target server nickname: '<ingame_name> - <icon><name>'.

    If no server selected, returns just the ingame_name. Always truncates to
    Discord's 32-character nickname limit.
    """
    base = ingame_name.strip()
    if server is None:
        return base[:DISCORD_NICK_MAX]
    suffix = f"{server.icon or ''}{server.name}".strip()
    if not suffix:
        return base[:DISCORD_NICK_MAX]
    full = f"{base} - {suffix}"
    if len(full) <= DISCORD_NICK_MAX:
        return full
    # Trim the ingame_name part so the server suffix is preserved.
    overflow = len(full) - DISCORD_NICK_MAX
    trimmed_name = base[: max(1, len(base) - overflow)]
    return f"{trimmed_name} - {suffix}"[:DISCORD_NICK_MAX]


async def sync_nickname(
    bot: "AkimBot",
    member: discord.Member,
    player: Player,
    *,
    reason: str = "Akim Bot nickname sync",
) -> str | None:
    """Try to set member.nick = build_nickname(...). Returns a human-readable
    warning string on failure, or None on success / no-op.

    Always silently no-ops on the server owner since Discord forbids that.
    Also no-ops when the guild owner has switched auto-rename OFF via
    /setup-baodanh auto_rename:False.
    """
    # Owner opt-out: NULL/TRUE keeps legacy auto-rename behavior; FALSE skips.
    async with bot.db.session() as s:
        cfg = await s.get(GuildConfig, member.guild.id)
    if cfg is not None and cfg.auto_rename_enabled is False:
        return None

    server: Server | None = player.server  # joined-load
    target = build_nickname(player.ingame_name, server)

    if member.guild.owner_id == member.id:
        return None  # silently skip
    if member.nick == target:
        return None  # no-op

    try:
        await member.edit(nick=target, reason=reason)
        return None
    except discord.Forbidden:
        return (
            "⚠️ Bot couldn't change the nickname. Needs **Manage Nicknames** "
            "and the bot's role must be above the user's role."
        )
    except discord.HTTPException as e:
        return f"⚠️ Nickname change failed: {e.text or e.status}"
