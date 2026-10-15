from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from sqlalchemy import select

from ..db.models import GuildConfig, GuildMemberUid, Player, Server, Weapon
from .nickname import sync_nickname

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.approval")


# ---------------- Permission helper ----------------


async def is_approver(
    bot: "AkimBot",
    member: discord.Member,
) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    async with bot.db.session() as s:
        cfg = await s.get(GuildConfig, member.guild.id)
    if cfg is None:
        return False
    approver_ids = set(cfg.approver_ids())
    if not approver_ids:
        return False
    return any(r.id in approver_ids for r in member.roles)


# ---------------- Embed builder ----------------


async def build_pending_embed(
    bot: "AkimBot",
    player: Player,
    guild: discord.Guild,
) -> discord.Embed:
    user = guild.get_member(player.discord_id)
    user_mention = user.mention if user else f"<@{player.discord_id}>"

    # UID match against the guild member UID whitelist
    async with bot.db.session() as s:
        uid_match = await s.scalar(
            select(GuildMemberUid).where(
                GuildMemberUid.guild_id == player.guild_id,
                GuildMemberUid.uid == player.ingame_uid,
            )
        )
        main_w = (
            await s.get(Weapon, player.main_weapon_id)
            if player.main_weapon_id
            else None
        )
        sub_w = (
            await s.get(Weapon, player.sub_weapon_id)
            if player.sub_weapon_id
            else None
        )

    if player.approval_status == "pending":
        color = discord.Color.orange()
        title = "⏳ Registration — Pending"
    elif player.approval_status == "approved":
        color = discord.Color.green()
        title = "✅ Registration — Approved"
    else:
        color = discord.Color.red()
        title = "❌ Registration — Denied"

    embed = discord.Embed(title=title, color=color)
    if user and user.display_avatar:
        embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Discord", value=user_mention, inline=True)
    embed.add_field(name="In-game Name", value=player.ingame_name, inline=True)
    embed.add_field(name="UID", value=f"`{player.ingame_uid}`", inline=True)
    embed.add_field(
        name="Claims Guild membership?",
        value="✅ Yes" if player.is_guild_member else "❌ No",
        inline=True,
    )
    if main_w:
        embed.add_field(
            name="Main Weapon",
            value=f"{main_w.icon or ''} {main_w.name}".strip(),
            inline=True,
        )
    if sub_w:
        embed.add_field(
            name="Sub Weapon",
            value=f"{sub_w.icon or ''} {sub_w.name}".strip(),
            inline=True,
        )
    if player.server is not None:
        embed.add_field(
            name="Server",
            value=f"{player.server.icon or ''}{player.server.name}".strip(),
            inline=True,
        )

    if uid_match:
        embed.add_field(
            name="🔎 UID Check",
            value=(
                f"✅ **UID `{player.ingame_uid}` is on the Guild list.**"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="🔎 UID Check",
            value=(
                f"⚠️ **UID `{player.ingame_uid}` is NOT on the Guild list.**\n"
                f"Self-declared: {'Yes' if player.is_guild_member else 'No'}."
            ),
            inline=False,
        )

    if player.approval_status == "approved" and player.approved_by:
        approver = guild.get_member(player.approved_by)
        approver_name = approver.mention if approver else f"<@{player.approved_by}>"
        role = (
            guild.get_role(player.approved_role_id)
            if player.approved_role_id
            else None
        )
        embed.add_field(
            name="Approved by",
            value=f"{approver_name}"
            + (f" — granted role {role.mention}" if role else ""),
            inline=False,
        )
        if player.approved_at:
            embed.set_footer(
                text=(
                    f"Approved at "
                    f"{player.approved_at.astimezone(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}"
                )
            )
    elif player.approval_status == "denied" and player.approved_by:
        denier = guild.get_member(player.approved_by)
        embed.add_field(
            name="Denied by",
            value=denier.mention if denier else f"<@{player.approved_by}>",
            inline=False,
        )

    embed.set_footer(
        text=embed.footer.text
        or (
            f"Submitted at "
            f"{player.updated_at.astimezone(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}"
        )
    )
    return embed


# ---------------- Persistent buttons ----------------


class ApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"baodanh:approve:(?P<player_id>\d+)",
):
    def __init__(self, player_id: int, *, disabled: bool = False) -> None:
        self.player_id = player_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"baodanh:approve:{player_id}",
                label="Approve",
                emoji="✅",
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
    ) -> "ApproveButton":
        return cls(int(match["player_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await _handle_approve_click(bot, interaction, self.player_id)


class DenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"baodanh:deny:(?P<player_id>\d+)",
):
    def __init__(self, player_id: int, *, disabled: bool = False) -> None:
        self.player_id = player_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"baodanh:deny:{player_id}",
                label="Deny",
                emoji="❌",
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
    ) -> "DenyButton":
        return cls(int(match["player_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await _handle_deny(bot, interaction, self.player_id)


class ToggleGuildMemberButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"baodanh:togglegm:(?P<player_id>\d+)",
):
    """Approver toggles is_guild_member if the user mis-claimed."""

    def __init__(self, player_id: int, *, disabled: bool = False) -> None:
        self.player_id = player_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"baodanh:togglegm:{player_id}",
                label="Toggle Guild Y/N",
                emoji="🔁",
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
    ) -> "ToggleGuildMemberButton":
        return cls(int(match["player_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await _handle_toggle_guild_member(bot, interaction, self.player_id)


def build_pending_view(player_id: int, *, disabled: bool = False) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ApproveButton(player_id, disabled=disabled))
    v.add_item(DenyButton(player_id, disabled=disabled))
    # Toggle is only meaningful while still pending.
    if not disabled:
        v.add_item(ToggleGuildMemberButton(player_id, disabled=False))
    return v


# ---------------- Approve / Deny handlers ----------------


async def _refresh_pending_message(
    bot: "AkimBot",
    interaction: discord.Interaction,
    player: Player,
    disable: bool,
) -> None:
    if (
        interaction.guild is None
        or player.pending_channel_id is None
        or player.pending_message_id is None
    ):
        return
    channel = interaction.guild.get_channel(player.pending_channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    try:
        msg = await channel.fetch_message(player.pending_message_id)
    except (discord.NotFound, discord.Forbidden):
        return
    embed = await build_pending_embed(bot, player, interaction.guild)
    view = build_pending_view(player.id, disabled=disable)
    await msg.edit(embed=embed, view=view)


async def _delete_pending_message(
    bot: "AkimBot",
    interaction: discord.Interaction,
    player_id: int,
) -> None:
    """Delete the public pending card after a terminal action and null the
    DB pointers so /baodanh resend won't try to follow a dangling ref."""
    if interaction.guild is None:
        return
    async with bot.db.session() as s:
        player = await s.get(Player, player_id)
        if player is None:
            return
        channel_id = player.pending_channel_id
        message_id = player.pending_message_id
        if channel_id and message_id:
            player.pending_channel_id = None
            player.pending_message_id = None
            await s.commit()
    if not channel_id or not message_id:
        return
    channel = interaction.guild.get_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    try:
        msg = await channel.fetch_message(message_id)
        await msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        # Already deleted, no perms to delete, or transient — ignore.
        pass


class _ApproveRoleSelectView(discord.ui.View):
    def __init__(self, bot: "AkimBot", player_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.player_id = player_id

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        min_values=1,
        max_values=1,
        placeholder="Select the role to grant this user",
    )
    async def role_pick(
        self,
        interaction: discord.Interaction,
        select: discord.ui.RoleSelect,
    ) -> None:
        await _finalize_approve(
            self.bot, interaction, self.player_id, select.values[0]
        )
        self.stop()


async def _handle_approve_click(
    bot: "AkimBot", interaction: discord.Interaction, player_id: int
) -> None:
    if not isinstance(interaction.user, discord.Member):
        return
    if not await is_approver(bot, interaction.user):
        await interaction.response.send_message(
            "❌ You don't have permission to approve registrations.",
            ephemeral=True,
        )
        return

    async with bot.db.session() as s:
        player = await s.get(Player, player_id)
    if player is None:
        await interaction.response.send_message(
            "Registration record not found.", ephemeral=True
        )
        return
    if player.approval_status != "pending":
        await interaction.response.send_message(
            f"This registration is already **{player.approval_status}** "
            "and cannot be reviewed again.",
            ephemeral=True,
        )
        return

    view = _ApproveRoleSelectView(bot, player_id)
    await interaction.response.send_message(
        f"Select a role to grant <@{player.discord_id}>:",
        view=view,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _finalize_approve(
    bot: "AkimBot",
    interaction: discord.Interaction,
    player_id: int,
    role: discord.Role,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return

    async with bot.db.session() as s:
        player = await s.get(Player, player_id)
        if player is None or player.approval_status != "pending":
            await interaction.response.edit_message(
                content="This registration is no longer pending.", view=None
            )
            return
        cfg = await s.get(GuildConfig, player.guild_id)

    target = interaction.guild.get_member(player.discord_id)
    if target is None:
        await interaction.response.edit_message(
            content=f"User <@{player.discord_id}> is no longer in the server.",
            view=None,
        )
        return

    me = interaction.guild.me
    issues: list[str] = []

    # Role hierarchy / bot perms check
    if me is None or role >= me.top_role:
        issues.append(
            f"Role {role.mention} is above the bot's top role and cannot be "
            "assigned. Move the bot's role higher and try again."
        )
    else:
        try:
            await target.add_roles(role, reason="Akim Bot /baodanh approve")
        except discord.Forbidden:
            issues.append(
                f"Bot is missing Manage Roles permission to grant {role.mention}."
            )
        except discord.HTTPException as e:
            issues.append(f"Error granting role: {e.text or e.status}")

        if cfg and cfg.unverified_role_id:
            unverified = interaction.guild.get_role(cfg.unverified_role_id)
            if unverified and unverified in target.roles:
                try:
                    await target.remove_roles(
                        unverified, reason="Akim Bot /baodanh approve"
                    )
                except discord.Forbidden:
                    issues.append(
                        f"Granted {role.mention} but failed to remove role "
                        f"{unverified.mention} (missing Manage Roles or bot role too low)."
                    )
                except discord.HTTPException as e:
                    issues.append(
                        f"Error removing Unverified role: {e.text or e.status}"
                    )

    # Try to sync the user's nickname (uses '<name> - <icon><server>' format)
    # using the same helper as /baodanh so behavior stays consistent.
    nick_warning = await sync_nickname(
        bot, target, player, reason="Akim Bot approve sync"
    )
    if nick_warning:
        issues.append(nick_warning)

    async with bot.db.session() as s:
        player = await s.get(Player, player_id)
        if player is not None:
            player.approval_status = "approved"
            player.approved_by = interaction.user.id
            player.approved_role_id = role.id
            player.approved_at = datetime.now(timezone.utc)
            await s.commit()
            # Re-read with relations populated for embed builder
            player = await s.get(Player, player_id)

    # Notify economy so the approved member gets their /economy setup
    # approval_reward (if any). RewardEngine.grant_approval_reward is
    # idempotent, so a duplicate dispatch (e.g. admin re-approves an
    # already-paid user) is safe.
    if player is not None and interaction.guild_id is not None:
        bot.dispatch(
            "player_approved",
            interaction.guild_id, player.discord_id,
        )

    # Drop the public pending card so the approval channel stays clean.
    await _delete_pending_message(bot, interaction, player_id)

    msg = (
        f"✅ Approved <@{player.discord_id if player else 0}> and granted "
        f"role {role.mention}."
    )
    if issues:
        msg += "\n⚠️ " + "\n⚠️ ".join(issues)
    await interaction.response.edit_message(
        content=msg, view=None, embed=None,
    )


async def _handle_deny(
    bot: "AkimBot", interaction: discord.Interaction, player_id: int
) -> None:
    if not isinstance(interaction.user, discord.Member):
        return
    if not await is_approver(bot, interaction.user):
        await interaction.response.send_message(
            "❌ You don't have permission to review registrations.",
            ephemeral=True,
        )
        return

    async with bot.db.session() as s:
        player = await s.get(Player, player_id)
        if player is None:
            await interaction.response.send_message(
                "Registration record not found.", ephemeral=True
            )
            return
        if player.approval_status != "pending":
            await interaction.response.send_message(
                f"This registration is already **{player.approval_status}**.",
                ephemeral=True,
            )
            return
        player.approval_status = "denied"
        player.approved_by = interaction.user.id
        player.approved_at = datetime.now(timezone.utc)
        await s.commit()
        player = await s.get(Player, player_id)

    # Ephemeral first so we own the interaction response slot, then drop
    # the public card. Doing the public delete first would race the
    # 3-second response budget on slow guilds.
    await interaction.response.send_message(
        f"❌ Denied registration from <@{player.discord_id if player else 0}>.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await _delete_pending_message(bot, interaction, player_id)


async def _handle_toggle_guild_member(
    bot: "AkimBot", interaction: discord.Interaction, player_id: int
) -> None:
    """Approver overrides the user's self-reported Guild membership."""
    if not isinstance(interaction.user, discord.Member):
        return
    if not await is_approver(bot, interaction.user):
        await interaction.response.send_message(
            "❌ You don't have permission to edit registrations.",
            ephemeral=True,
        )
        return

    async with bot.db.session() as s:
        player = await s.get(Player, player_id)
        if player is None:
            await interaction.response.send_message(
                "Registration record not found.", ephemeral=True
            )
            return
        if player.approval_status != "pending":
            await interaction.response.send_message(
                f"This registration is already **{player.approval_status}** "
                "and can no longer be edited.",
                ephemeral=True,
            )
            return
        new_value = not player.is_guild_member
        player.is_guild_member = new_value
        await s.commit()
        player = await s.get(Player, player_id)  # refresh with relations

    if player is not None and interaction.guild is not None:
        embed = await build_pending_embed(bot, player, interaction.guild)
        view = build_pending_view(player.id)
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"⚠️ DB updated but failed to refresh the card: {e.text or e.status}",
                ephemeral=True,
            )
            return
    label = "✅ Yes" if new_value else "❌ No"
    await interaction.followup.send(
        f"🔁 Admin toggled `Guild member?` → **{label}** "
        f"(by {interaction.user.mention}).",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ---------------- Posting a new pending card ----------------


async def post_pending_card(
    bot: "AkimBot",
    guild: discord.Guild,
    player: Player,
) -> discord.Message | None:
    """Send the pending-approval card to the configured approval channel.

    Returns None (with a warning log) instead of raising when the bot
    lacks Send Messages / View Channel there. The user's registration data
    has already been committed by the caller — we shouldn't make that
    save look like it failed just because the admin misconfigured the
    approval channel permissions.
    """
    async with bot.db.session() as s:
        cfg = await s.get(GuildConfig, guild.id)
    if cfg is None or cfg.approval_channel_id is None:
        log.warning("post_pending_card: no approval channel configured for %s", guild.id)
        return None
    channel = guild.get_channel(cfg.approval_channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        log.warning("post_pending_card: approval channel %s invalid", cfg.approval_channel_id)
        return None

    embed = await build_pending_embed(bot, player, guild)
    view = build_pending_view(player.id)

    mention_lines: list[str] = []
    for rid in cfg.approver_ids():
        r = guild.get_role(rid)
        if r and r.mentionable:
            mention_lines.append(r.mention)
        elif r:
            mention_lines.append(r.name)
    ping = " ".join(mention_lines) if mention_lines else ""

    try:
        msg = await channel.send(
            content=(f"🔔 New registration awaiting approval {ping}".strip()),
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
    except discord.Forbidden:
        # Bot is in the guild but has no View Channel / Send Messages in
        # the approval channel. Owner needs to fix the channel perms —
        # loud log, don't crash the caller.
        log.warning(
            "post_pending_card: 403 Forbidden posting to #%s (%s) in guild %s — "
            "bot is missing View Channel or Send Messages there. Grant "
            "the bot role Send Messages + Embed Links in that channel.",
            getattr(channel, "name", "?"), channel.id, guild.id,
        )
        return None
    except discord.HTTPException as e:
        # Any other Discord failure (rate limit slipped through, huge
        # embed, etc.) shouldn't destroy the save either.
        log.warning(
            "post_pending_card: HTTPException posting to %s in guild %s: %s",
            channel.id, guild.id, e,
        )
        return None

    async with bot.db.session() as s:
        p = await s.get(Player, player.id)
        if p is not None:
            p.pending_channel_id = channel.id
            p.pending_message_id = msg.id
            await s.commit()

    return msg
