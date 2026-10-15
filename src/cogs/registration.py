from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, select

from pathlib import Path

from ..db.models import GuildConfig, PetAsset, PetSpecies, Player, UserPet
from ..pets.resolver import resolve_pet_image
from ..views.approval import (
    build_pending_view,
    is_approver,
    post_pending_card,
)
from ..views.registration import UidModal

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.registration")


class Registration(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot

    baodanh = app_commands.Group(
        name="baodanh",
        description="Character registration + approval management",
        guild_only=True,
    )

    # ---------- Lifecycle: scrub data when a member leaves ----------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Drop the Player row for a member who left/got kicked/was banned.

        Combined with /baodanh's 'every fresh registration → pending' behavior,
        this means a returning member has no record → must register again →
        the new submission lands in the approval queue automatically.

        Also tries to clean up the still-visible pending card so the
        approval channel doesn't accumulate dead cards for users who
        won't be around to be approved.
        """
        async with self.bot.db.session() as s:
            player = await s.scalar(
                select(Player).where(
                    Player.guild_id == member.guild.id,
                    Player.discord_id == member.id,
                )
            )
            if player is None:
                return
            pending_channel_id = player.pending_channel_id
            pending_message_id = player.pending_message_id
            was_pending = player.approval_status == "pending"
            await s.execute(delete(Player).where(Player.id == player.id))
            await s.commit()

        log.info(
            "Member %s (%s) left guild %s — Player row deleted",
            member.display_name,
            member.id,
            member.guild.id,
        )

        # Best-effort cleanup of the pending card so admins don't click a
        # dead Approve button. Edits are quieter than deletes, so flip the
        # message to a 'user left' note instead of deleting outright.
        if was_pending and pending_channel_id and pending_message_id:
            channel = member.guild.get_channel(pending_channel_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await channel.fetch_message(pending_message_id)
                    notice = discord.Embed(
                        title="🚪 User left the server",
                        description=(
                            f"<@{member.id}> (`{member.name}`) left before "
                            "being approved. Registration deleted from DB."
                        ),
                        color=discord.Color.dark_gray(),
                    )
                    await msg.edit(embed=notice, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    async def _open_form(self, interaction: discord.Interaction) -> None:
        """Step 1: ask for UID via a small modal. Step 2 (selection view)
        is opened from inside the modal's on_submit."""
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(Player).where(
                    Player.guild_id == interaction.guild_id,
                    Player.discord_id == interaction.user.id,
                )
            )
        await interaction.response.send_modal(UidModal(self.bot, prefill=existing))

    # ---------- /baodanh moi ----------

    @baodanh.command(
        name="moi",
        description="Register or update your character info",
    )
    async def baodanh_moi(self, interaction: discord.Interaction) -> None:
        await self._open_form(interaction)

    # ---------- /baodanh pending (admin) ----------

    @baodanh.command(
        name="pending",
        description="(Admin) List registrations waiting for approval",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def baodanh_pending(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            return
        if not await is_approver(self.bot, interaction.user):
            await interaction.response.send_message(
                "❌ You don't have permission to view the pending list.",
                ephemeral=True,
            )
            return
        assert interaction.guild_id is not None and interaction.guild is not None

        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(Player)
                .where(
                    Player.guild_id == interaction.guild_id,
                    Player.approval_status == "pending",
                )
                .order_by(Player.updated_at.asc())
            )
            pending = list(rows.scalars().all())

        if not pending:
            await interaction.response.send_message(
                "🎉 No registrations are waiting for approval.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"📋 **{len(pending)} registration(s) pending approval.** "
            "Each card is sent as a followup below (only you see them).",
            ephemeral=True,
        )

        # Send each pending player as its own ephemeral card with working
        # Approve/Deny buttons. Buttons are persistent (DynamicItem) so they
        # work the same as the original posted card.
        from ..views.approval import build_pending_embed  # local import to avoid cycle

        for p in pending:
            embed = await build_pending_embed(self.bot, p, interaction.guild)
            view = build_pending_view(p.id)
            link_field = ""
            if p.pending_channel_id and p.pending_message_id:
                link_field = (
                    f"\n🔗 Original card: "
                    f"https://discord.com/channels/{interaction.guild_id}/"
                    f"{p.pending_channel_id}/{p.pending_message_id}"
                )
            await interaction.followup.send(
                content=f"⏳ Registration #{p.id}{link_field}",
                embed=embed,
                view=view,
                ephemeral=True,
            )

    # ---------- /baodanh approved | denied ----------

    @baodanh.command(
        name="approved",
        description="(Admin) List approved registrations",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def baodanh_approved(self, interaction: discord.Interaction) -> None:
        await self._list_by_status(
            interaction,
            status="approved",
            emoji="✅",
            label="approved",
            color=discord.Color.green(),
        )

    @baodanh.command(
        name="denied",
        description="(Admin) List denied registrations",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def baodanh_denied(self, interaction: discord.Interaction) -> None:
        await self._list_by_status(
            interaction,
            status="denied",
            emoji="❌",
            label="denied",
            color=discord.Color.red(),
        )

    async def _list_by_status(
        self,
        interaction: discord.Interaction,
        *,
        status: str,
        emoji: str,
        label: str,
        color: discord.Color,
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            return
        if not await is_approver(self.bot, interaction.user):
            await interaction.response.send_message(
                "❌ You don't have permission to view this list.",
                ephemeral=True,
            )
            return
        assert interaction.guild_id is not None

        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(Player)
                .where(
                    Player.guild_id == interaction.guild_id,
                    Player.approval_status == status,
                )
                .order_by(
                    Player.approved_at.desc().nullslast(),
                    Player.updated_at.desc(),
                )
            )
            players = list(rows.scalars().all())

        if not players:
            await interaction.response.send_message(
                f"_(No users in **{label}** state yet.)_",
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for p in players:
            line = (
                f"`#{p.id}` `{p.ingame_uid}` **{p.ingame_name}** — <@{p.discord_id}>"
            )
            if p.approved_at:
                actor = f"<@{p.approved_by}>" if p.approved_by else "—"
                when = p.approved_at.strftime("%d/%m/%Y %H:%M UTC")
                tail = f"  └ {label} by {actor} • {when}"
                if status == "approved" and p.approved_role_id:
                    tail += f" • role <@&{p.approved_role_id}>"
                line += "\n" + tail
            lines.append(line)

        # Chunk lines so each embed description stays under Discord's 4096 limit
        chunks: list[str] = []
        current = ""
        for line in lines:
            piece = line + "\n\n"
            if len(current) + len(piece) > 3800:
                chunks.append(current)
                current = piece
            else:
                current += piece
        if current:
            chunks.append(current)

        title = f"{emoji} {label.capitalize()} list ({len(players)})"
        for i, c in enumerate(chunks):
            embed = discord.Embed(
                title=(
                    title if len(chunks) == 1
                    else f"{title} — page {i + 1}/{len(chunks)}"
                ),
                description=c,
                color=color,
            )
            if i == 0:
                await interaction.response.send_message(
                    embed=embed,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.followup.send(
                    embed=embed,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

    # ---------- /baodanh resend (admin convenience) ----------

    @baodanh.command(
        name="resend",
        description=(
            "(Admin) Repost the approval card for a pending registration "
            "(if the original was deleted)"
        ),
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(user="Which user's approval card to repost")
    async def baodanh_resend(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            return
        if not await is_approver(self.bot, interaction.user):
            await interaction.response.send_message(
                "❌ You don't have permission.", ephemeral=True,
            )
            return
        assert interaction.guild_id is not None and interaction.guild is not None

        async with self.bot.db.session() as s:
            player = await s.scalar(
                select(Player).where(
                    Player.guild_id == interaction.guild_id,
                    Player.discord_id == user.id,
                )
            )
        if player is None:
            await interaction.response.send_message(
                f"{user.mention} hasn't registered yet.", ephemeral=True,
            )
            return
        if player.approval_status != "pending":
            await interaction.response.send_message(
                f"{user.mention} isn't in pending status "
                f"(currently: **{player.approval_status}**).",
                ephemeral=True,
            )
            return

        msg = await post_pending_card(self.bot, interaction.guild, player)
        if msg is None:
            await interaction.response.send_message(
                "❌ Couldn't post the card. Owner should run "
                "`/setup-baodanh` first.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"✅ Approval card reposted: {msg.jump_url}",
                ephemeral=True,
            )


# ---------- /capnhat + /info still top-level (compat) ----------


class RegistrationTopLevel(commands.Cog):
    """Top-level commands that share the same flow."""

    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot

    async def _open_form(
        self,
        interaction: discord.Interaction,
        *,
        target_user_id: int | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        lookup_uid = target_user_id or interaction.user.id
        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(Player).where(
                    Player.guild_id == interaction.guild_id,
                    Player.discord_id == lookup_uid,
                )
            )
        await interaction.response.send_modal(UidModal(
            self.bot, prefill=existing, target_user_id=target_user_id,
        ))

    @app_commands.command(name="capnhat", description="Update character info")
    @app_commands.guild_only()
    async def capnhat(self, interaction: discord.Interaction) -> None:
        await self._open_form(interaction)

    @app_commands.command(
        name="player-reg",
        description="Register or update character info (optionally for another user, owner/manager only)",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user=(
            "Target user to register (leave empty to register yourself). "
            "Only server owner / manager / approver can use this option."
        ),
    )
    async def player_reg(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        # Same handler as /baodanh moi and /capnhat — surface the modal
        # under a shorter, English name so members don't have to remember
        # the /baodanh <subcommand> layout.
        #
        # When `user` is supplied, this is a proxy registration: the
        # caller must have Manage Server or the configured approver
        # role (see is_approver), otherwise refuse. Bots can't be
        # registered — this is a player profile, not a bot record.
        if user is not None:
            actor = interaction.user
            if not isinstance(actor, discord.Member):
                await interaction.response.send_message(
                    "❌ Registering for another user requires this to be "
                    "run inside a server.",
                    ephemeral=True,
                )
                return
            if not await is_approver(self.bot, actor):
                await interaction.response.send_message(
                    "❌ You need Manage Server (or the configured "
                    "approver role) to register on someone else's "
                    "behalf.",
                    ephemeral=True,
                )
                return
            if user.bot:
                await interaction.response.send_message(
                    "❌ Bots can't be registered as players.",
                    ephemeral=True,
                )
                return
            await self._open_form(interaction, target_user_id=user.id)
            return
        await self._open_form(interaction)

    @app_commands.command(
        name="player",
        description="View your (or another member's) registered character info",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="Discord user to view (default: you)",
        uid="Or look up directly by in-game UID",
    )
    async def player(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        uid: app_commands.Range[str, 4, 16] | None = None,
    ) -> None:
        await self._render_info(interaction, user=user, uid=uid, share=False)

    @app_commands.command(
        name="info",
        description=(
            "View character info by Discord user or in-game UID"
        ),
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="Discord user to view (default: you)",
        uid="Or look up directly by in-game UID",
        share="True = show to the whole channel (default: only you see)",
    )
    async def info(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        uid: app_commands.Range[str, 4, 16] | None = None,
        share: bool = False,
    ) -> None:
        await self._render_info(interaction, user=user, uid=uid, share=share)

    async def _render_info(
        self,
        interaction: discord.Interaction,
        *,
        user: discord.Member | None,
        uid: str | None,
        share: bool,
    ) -> None:
        assert interaction.guild_id is not None
        guild = interaction.guild
        assert guild is not None

        # Resolve which Player we're looking up. UID takes priority if both
        # are given; otherwise fall back to user (or interaction.user).
        async with self.bot.db.session() as s:
            if uid is not None:
                uid_clean = uid.strip()
                if not uid_clean.isdigit():
                    await interaction.response.send_message(
                        "❌ UID must be digits only.", ephemeral=True,
                    )
                    return
                player = await s.scalar(
                    select(Player).where(
                        Player.guild_id == interaction.guild_id,
                        Player.ingame_uid == uid_clean,
                    )
                )
                if not player:
                    await interaction.response.send_message(
                        f"❌ No player with UID `{uid_clean}` in this server.",
                        ephemeral=True,
                    )
                    return
            else:
                target_member = user or interaction.user
                player = await s.scalar(
                    select(Player).where(
                        Player.guild_id == interaction.guild_id,
                        Player.discord_id == target_member.id,
                    )
                )
                if not player:
                    await interaction.response.send_message(
                        f"{target_member.mention} hasn't registered yet. "
                        "Use `/player-reg` (or `/baodanh moi`) to sign up.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

        # Resolve a display target (for avatar + mention). Member first, then
        # fall back to a fetched User if they've left the guild.
        display_member = guild.get_member(player.discord_id)
        display_user: discord.User | discord.Member | None = display_member
        if display_user is None:
            try:
                display_user = await self.bot.fetch_user(player.discord_id)
            except (discord.NotFound, discord.HTTPException):
                display_user = None

        if display_member is not None:
            discord_field = display_member.mention
        elif display_user is not None:
            discord_field = f"{display_user.mention} _(left the server)_"
        else:
            discord_field = f"<@{player.discord_id}> _(unknown)_"

        status_emoji = {"approved": "✅", "pending": "⏳", "denied": "❌"}.get(
            player.approval_status, "❓"
        )
        embed = discord.Embed(
            title=f"{status_emoji} Info: {player.ingame_name}",
            color=discord.Color.blurple(),
        )
        if display_user is not None:
            embed.set_thumbnail(url=display_user.display_avatar.url)
        embed.add_field(name="Discord", value=discord_field, inline=True)
        embed.add_field(name="UID", value=player.ingame_uid, inline=True)
        if player.level is not None:
            embed.add_field(name="Level", value=str(player.level), inline=True)
        if player.elegance is not None:
            embed.add_field(
                name="Elegance", value=f"{player.elegance:,}", inline=True
            )
        embed.add_field(
            name="Approval status",
            value=player.approval_status,
            inline=True,
        )
        if player.main_weapon:
            embed.add_field(
                name="Main Weapon",
                value=f"{player.main_weapon.icon or ''} {player.main_weapon.name}".strip(),
                inline=True,
            )
        if player.sub_weapon:
            embed.add_field(
                name="Sub Weapon",
                value=f"{player.sub_weapon.icon or ''} {player.sub_weapon.name}".strip(),
                inline=True,
            )
        embed.add_field(
            name="Guild member?",
            value="✅ Yes" if player.is_guild_member else "❌ No",
            inline=True,
        )
        if player.server:
            embed.add_field(
                name="Server",
                value=f"{player.server.icon or ''}{player.server.name}".strip(),
                inline=True,
            )

        # Pet row — rendered only if the player toggled "Show Info" on
        # their active pet (Pet House panel). Active pet only; if the
        # user has multiple pets with the flag, only the is_active one
        # shows since /info is a single-user surface.
        async with self.bot.db.session() as s:
            pet = await s.scalar(
                select(UserPet).where(
                    UserPet.guild_id == interaction.guild_id,
                    UserPet.user_id == player.discord_id,
                    UserPet.is_active.is_(True),
                    UserPet.show_in_info.is_(True),
                )
            )
            species_name: str | None = None
            portrait_asset: PetAsset | None = None
            if pet is not None:
                sp = await s.scalar(
                    select(PetSpecies).where(
                        PetSpecies.guild_id == interaction.guild_id,
                        PetSpecies.species_id == pet.species_id,
                    )
                )
                species_name = sp.display_name if sp else pet.species_id
                # Prefer the portrait of the current stage so the
                # ava changes as the pet evolves. Fall back to the
                # general resolver (mood / base / portrait priority)
                # if portrait isn't imported for this stage.
                portrait_asset = await s.scalar(
                    select(PetAsset).where(
                        PetAsset.guild_id == interaction.guild_id,
                        PetAsset.species_id == pet.species_id,
                        PetAsset.stage == pet.stage,
                        PetAsset.asset_type == "portrait",
                        PetAsset.state_key == "portrait",
                        PetAsset.is_active.is_(True),
                    )
                )
                if portrait_asset is None:
                    portrait_asset = await resolve_pet_image(
                        s, guild_id=interaction.guild_id,
                        species_id=pet.species_id, stage=pet.stage,
                    )

        footer_parts = [
            f"Last updated: {player.updated_at.strftime('%Y-%m-%d %H:%M UTC')}"
        ]
        if player.last_synced_at:
            footer_parts.append(
                f"WWM sync: {player.last_synced_at.strftime('%Y-%m-%d %H:%M UTC')}"
            )
        embed.set_footer(text="  •  ".join(footer_parts))

        # Multi-embed payload when the user has a pet to show, so the
        # pet portrait can sit as the second embed's thumbnail at the
        # same render size as the user avatar on the first embed.
        embeds: list[discord.Embed] = [embed]
        pet_file: discord.File | None = None
        if pet is not None:
            stage_icon = {
                "baby": "🍼", "teen": "🐤",
                "adult": "🐲", "mystic": "✨",
            }.get(pet.stage, "🐾")
            label = pet.nickname or species_name or pet.species_id
            pet_embed = discord.Embed(
                title=f"🐾 Pet: {label}",
                description=(
                    f"{stage_icon} Stage **{pet.stage}** • Level **{pet.level}**"
                ),
                color=discord.Color.from_rgb(155, 89, 182),
            )
            if portrait_asset is not None:
                path = Path(portrait_asset.storage_path)
                if path.exists():
                    safe = path.name
                    pet_file = discord.File(str(path), filename=safe)
                    pet_embed.set_thumbnail(url=f"attachment://{safe}")
            embeds.append(pet_embed)

        # Default ephemeral, public only when the caller explicitly opts in.
        send_kwargs: dict = {
            "embeds": embeds,
            "ephemeral": not share,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if pet_file is not None:
            send_kwargs["file"] = pet_file
        await interaction.response.send_message(**send_kwargs)


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(Registration(bot))
    await bot.add_cog(RegistrationTopLevel(bot))
