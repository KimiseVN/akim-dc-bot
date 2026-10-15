from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, select

from ..db.models import GuildConfig, GuildMemberUid, Player, Server, Weapon
from ..views.lfg import (
    GuildWarCardButton,
    LFGCardCustomButton,
    LFGCardHeroRealmButton,
    LFGCardSwordTrialButton,
)
from ..views.registration import (
    BaodanhCardButton,
    BaodanhLinkUidButton,
    PlayerCardRegButton,
)

if TYPE_CHECKING:
    from ..bot import AkimBot


_UID_SPLIT = re.compile(r"[\s,;]+")
_UID_VALID = re.compile(r"^\d{4,16}$")

# Cap for JSON uploads. 5 MB is roughly ≥200k UIDs after JSON overhead
# — far beyond anything sane, and still cheap to parse in one gulp.
_UPLOAD_MAX_BYTES = 5 * 1024 * 1024


def _extract_uids_from_json(data: object) -> list[str] | None:
    """Pull a flat list of UID strings out of a permissive JSON shape.

    Accepted shapes (any of them):
      • ["12345", "67890", ...]                     — plain array
      • [12345, 67890, ...]                          — numeric array
      • [{"uid": "12345"}, {"uid": 67890}, ...]      — array of objects
      • {"uids":    [...]}   / {"UIDs": [...]}       — wrapped
      • {"members": [...]}   / {"uid_list": [...]}   — same, alt keys

    Returns None when nothing recognisable is found so the caller can
    reject with a clear error rather than a silent zero-add.
    """
    if isinstance(data, list):
        out: list[str] = []
        for item in data:
            if isinstance(item, (str, int)):
                out.append(str(item))
            elif isinstance(item, dict):
                # Common field names — first hit wins.
                for k in ("uid", "UID", "id", "member_uid"):
                    v = item.get(k)
                    if v is not None:
                        out.append(str(v))
                        break
        return out
    if isinstance(data, dict):
        for key in ("uids", "UIDs", "members", "uid_list"):
            if key in data:
                return _extract_uids_from_json(data[key])
    return None


class Admin(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot

    # ---------- LFG setup ----------

    @app_commands.command(
        name="setup-lfg",
        description="Configure the channel and (optionally) the roles allowed to use /lfg",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel where LFG party boards are posted",
        role1="Role allowed to use /lfg (leave all blank → everyone can use it)",
        role2="Second allowed role (optional)",
        role3="Third allowed role (optional)",
        role4="Fourth allowed role (optional)",
        role5="Fifth allowed role (optional)",
    )
    async def setup_lfg(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role1: discord.Role | None = None,
        role2: discord.Role | None = None,
        role3: discord.Role | None = None,
        role4: discord.Role | None = None,
        role5: discord.Role | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        # Defer up front so a slow config write can't blow Discord's
        # 3-second interaction deadline.
        await interaction.response.defer(ephemeral=True)

        # Dedupe in input order while preserving order
        seen: set[int] = set()
        roles: list[discord.Role] = []
        for r in (role1, role2, role3, role4, role5):
            if r and r.id not in seen:
                seen.add(r.id)
                roles.append(r)

        csv = ",".join(str(r.id) for r in roles) if roles else None

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            cfg.lfg_role_ids = csv
            cfg.lfg_channel_id = channel.id
            await s.commit()

        if roles:
            role_txt = ", ".join(r.mention for r in roles)
        else:
            role_txt = "@everyone (all members)"
        await interaction.followup.send(
            f"✅ LFG configured.\n"
            f"• Channel: {channel.mention}\n"
            f"• Allowed roles: {role_txt}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- Weapon management ----------

    weapon = app_commands.Group(
        name="weapon",
        description="Manage the in-game weapon list",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @weapon.command(name="add", description="Add a weapon to the database")
    @app_commands.describe(
        name="Display name (e.g. Snowparting Blade)",
        slot="Which slot this weapon can appear in",
        icon="Emoji or custom emoji (<:name:id>) — optional",
        sort_order="Display order (lower numbers appear first)",
    )
    async def weapon_add(
        self,
        interaction: discord.Interaction,
        name: str,
        slot: Literal["main", "sub", "both"] = "both",
        icon: str | None = None,
        sort_order: int = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            existing = await s.scalar(select(Weapon).where(Weapon.name == name))
            if existing:
                existing.icon = icon
                existing.slot = slot
                existing.sort_order = sort_order
                action = "Updated"
            else:
                s.add(Weapon(name=name, icon=icon, slot=slot, sort_order=sort_order))
                action = "Added"
            await s.commit()
        await interaction.followup.send(
            f"✅ {action}: {icon or ''} **{name}** ({slot})", ephemeral=True
        )

    @weapon.command(name="remove", description="Remove a weapon from the database")
    @app_commands.describe(name="Weapon name to remove")
    async def weapon_remove(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            result = await s.execute(delete(Weapon).where(Weapon.name == name))
            await s.commit()
        if result.rowcount:
            await interaction.followup.send(f"🗑 Removed **{name}**.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"Weapon **{name}** not found.", ephemeral=True
            )

    @weapon.command(name="list", description="List the weapons in the database")
    async def weapon_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            rows = await s.execute(select(Weapon).order_by(Weapon.sort_order, Weapon.name))
            weapons = list(rows.scalars().all())

        if not weapons:
            await interaction.followup.send(
                "No weapons yet. Use `/weapon add` to add one.", ephemeral=True
            )
            return

        lines = [
            f"{w.icon or '•'} **{w.name}** _(slot: {w.slot})_"
            for w in weapons
        ]
        embed = discord.Embed(
            title=f"Weapon list ({len(weapons)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


    # ---------- /setup-baodanh ----------

    @app_commands.command(
        name="setup-baodanh",
        description=(
            "Configure the /baodanh registration flow "
            "(approval mode + roles + channels)"
        ),
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        require_approval=(
            "TRUE = post pending card + wait for approver click "
            "(default). FALSE = auto-approve on Save + grant auto_role."
        ),
        auto_role=(
            "Role auto-granted when require_approval=FALSE. Required in "
            "that mode; ignored otherwise."
        ),
        unverified_role=(
            "Role that means 'waiting for approval' — bot removes it "
            "on approve. Required when require_approval=TRUE."
        ),
        approval_channel=(
            "Channel where the pending card is posted. Required when "
            "require_approval=TRUE."
        ),
        guild_name=(
            "In-game guild name — shown in the 'Are you a member of "
            "Guild <X>?' picker."
        ),
        approver_role1="Role allowed to Approve/Deny (default: Manage Server only)",
        approver_role2="Second approver role (optional)",
        approver_role3="Third approver role (optional)",
        auto_rename=(
            "TRUE = bot renames the user's Discord nickname to "
            "'<In-game Name> - <ServerIcon>ServerName' on Save/approve "
            "(default). FALSE = never touch the nickname."
        ),
    )
    async def setup_baodanh(
        self,
        interaction: discord.Interaction,
        require_approval: bool | None = None,
        auto_role: discord.Role | None = None,
        unverified_role: discord.Role | None = None,
        approval_channel: discord.TextChannel | None = None,
        guild_name: app_commands.Range[str, 1, 64] | None = None,
        approver_role1: discord.Role | None = None,
        approver_role2: discord.Role | None = None,
        approver_role3: discord.Role | None = None,
        auto_rename: bool | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        # Defer up front — the config write is a Postgres round-trip
        # (SELECT + INSERT/UPDATE + commit) and even a small hiccup on
        # the DB or the Discord API takes us past the 3-second
        # interaction deadline. Every DB-writing admin command should
        # follow this pattern.
        await interaction.response.defer(ephemeral=True)

        # Read current config first so validation can consider what's
        # already stored (owner may only be updating one field).
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
        current_require = (
            cfg.baodanh_require_approval if cfg is not None else None
        )
        effective_require = (
            require_approval
            if require_approval is not None
            else (True if current_require is None else current_require)
        )

        # Validate the combination for the mode we'll end up in.
        errors: list[str] = []
        if effective_require:
            has_unverified = (
                unverified_role is not None
                or (cfg is not None and cfg.unverified_role_id is not None)
            )
            has_channel = (
                approval_channel is not None
                or (cfg is not None and cfg.approval_channel_id is not None)
            )
            if not has_unverified:
                errors.append(
                    "`unverified_role` is required when "
                    "`require_approval=TRUE`."
                )
            if not has_channel:
                errors.append(
                    "`approval_channel` is required when "
                    "`require_approval=TRUE`."
                )
        else:
            has_auto_role = (
                auto_role is not None
                or (cfg is not None and cfg.baodanh_auto_role_id is not None)
            )
            if not has_auto_role:
                errors.append(
                    "`auto_role` is required when "
                    "`require_approval=FALSE` — pick the role to grant "
                    "on Save."
                )
        if errors:
            await interaction.followup.send(
                "❌ " + "\n❌ ".join(errors), ephemeral=True,
            )
            return

        seen: set[int] = set()
        approvers: list[discord.Role] = []
        for r in (approver_role1, approver_role2, approver_role3):
            if r and r.id not in seen:
                seen.add(r.id)
                approvers.append(r)
        approver_csv = ",".join(str(r.id) for r in approvers) if approvers else None

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            if require_approval is not None:
                cfg.baodanh_require_approval = bool(require_approval)
            if auto_role is not None:
                cfg.baodanh_auto_role_id = auto_role.id
            if auto_rename is not None:
                cfg.auto_rename_enabled = bool(auto_rename)
            if unverified_role is not None:
                cfg.unverified_role_id = unverified_role.id
            if approval_channel is not None:
                cfg.approval_channel_id = approval_channel.id
            if guild_name is not None:
                cfg.guild_name = guild_name
            # approver list is a full-replace (like today) only when the
            # caller passed at least one role; passing no roles leaves
            # the existing list alone.
            if approvers:
                cfg.approver_role_ids = approver_csv
            await s.commit()
            cfg = await s.get(GuildConfig, interaction.guild_id)

        assert cfg is not None
        mode_line = (
            "🔒 **Requires approval** — pending card sent to "
            "approval channel"
            if effective_require
            else "⚡ **Auto-approve** — Save grants the auto-role instantly"
        )
        lines = [
            "✅ /baodanh flow configured.",
            f"• Mode: {mode_line}",
        ]
        if cfg.unverified_role_id is not None:
            lines.append(f"• Unverified role: <@&{cfg.unverified_role_id}>")
        if cfg.approval_channel_id is not None:
            lines.append(f"• Approval channel: <#{cfg.approval_channel_id}>")
        if cfg.baodanh_auto_role_id is not None:
            lines.append(f"• Auto-approve role: <@&{cfg.baodanh_auto_role_id}>")
        if cfg.guild_name is not None:
            lines.append(f"• In-game guild name: **{cfg.guild_name}**")
        if cfg.approver_role_ids:
            role_ids = [
                x for x in cfg.approver_role_ids.split(",") if x.strip()
            ]
            lines.append(
                "• Approvers: "
                + ", ".join(f"<@&{x}>" for x in role_ids)
            )
        else:
            lines.append("• Approvers: _Manage Server admins only_")
        rename_on = cfg.auto_rename_enabled is not False
        lines.append(
            "• Auto-rename nickname: "
            + ("🟢 ON (default)" if rename_on else "⚪ OFF")
        )
        await interaction.followup.send(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /setup-baodanh-card + /setup-baodanh-link share these ----

    def _build_baodanh_card_embed(
        self,
        guild: discord.Guild,
        cfg: GuildConfig | None,
    ) -> discord.Embed:
        """Build the persistent registration card embed. The Link UID flow
        is exposed via a button (see _build_baodanh_card_view) rather
        than an inline mention so users get a single click instead of
        two (button → ephemeral with mention)."""
        desc_parts = [
            f"Welcome to **{guild.name}**!",
            "",
            "Press the **Register** button below to register or update "
            "your in-game character info.",
            "",
            "✅ After you submit, an admin will review and grant the "
            "matching role (or the role is granted automatically if "
            "approval is disabled).",
        ]
        if cfg and cfg.baodanh_link_command_id:
            desc_parts.extend([
                "",
                "🔗 After registering, press **Link UID** to link your "
                "in-game UID.",
            ])
        embed = discord.Embed(
            title="📝 Character Registration",
            description="\n".join(desc_parts),
            color=discord.Color.green(),
        )
        embed.set_footer(
            text="Persistent registration card — press a button to open the dialog.",
        )
        return embed

    def _build_baodanh_card_view(
        self,
        guild_id: int,
        cfg: GuildConfig | None,
    ) -> discord.ui.View:
        """View attached to the registration card. Always has the Register
        button; Link UID button is added only when the link mention is
        configured so we don't render a button that just errors when
        clicked."""
        view = discord.ui.View(timeout=None)
        view.add_item(BaodanhCardButton(guild_id))
        if cfg and cfg.baodanh_link_command_id:
            view.add_item(BaodanhLinkUidButton(guild_id))
        return view

    async def _refresh_baodanh_card_if_posted(
        self, guild: discord.Guild,
    ) -> bool:
        """Best-effort re-render the persistent card so a config tweak
        (eg. /setup-baodanh-link) shows up immediately. Returns True
        when the card existed and was edited; False when there's no
        live card to refresh. Failures are swallowed — the next time
        someone re-runs /setup-baodanh-card will fix it."""
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild.id)
        if cfg is None or not cfg.baodanh_card_channel_id or not cfg.baodanh_card_message_id:
            return False
        ch = guild.get_channel(cfg.baodanh_card_channel_id)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return False
        try:
            msg = await ch.fetch_message(cfg.baodanh_card_message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
        try:
            await msg.edit(
                embed=self._build_baodanh_card_embed(guild, cfg),
                view=self._build_baodanh_card_view(guild.id, cfg),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except discord.HTTPException:
            return False

    # ---------- /setup-baodanh-card ----------

    @app_commands.command(
        name="setup-baodanh-card",
        description="(Owner) Post the persistent Register card in a channel",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        channel="Channel to post the Register card (button opens the registration modal)",
    )
    async def setup_baodanh_card(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        assert interaction.guild is not None
        # Strict owner gate — Discord UI lets admins see this command,
        # we still cut them off if they aren't the actual guild owner.
        if not self.bot.is_owner(interaction.user.id, interaction.guild.owner_id):
            await interaction.response.send_message(
                "❌ Only the server owner can post the registration card.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        # Best-effort delete the previous card so we never leave two
        # live cards in the guild (one in the old channel, one new).
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            old_ch_id = cfg.baodanh_card_channel_id if cfg else None
            old_msg_id = cfg.baodanh_card_message_id if cfg else None
        if old_ch_id and old_msg_id:
            old_ch = interaction.guild.get_channel(old_ch_id)
            if isinstance(old_ch, (discord.TextChannel, discord.Thread)):
                try:
                    old_msg = await old_ch.fetch_message(old_msg_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        embed = self._build_baodanh_card_embed(interaction.guild, cfg)
        view = self._build_baodanh_card_view(interaction.guild_id, cfg)
        try:
            msg = await channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {channel.mention}.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            cfg.baodanh_card_channel_id = channel.id
            cfg.baodanh_card_message_id = msg.id
            await s.commit()

        await interaction.followup.send(
            f"✅ Registration card posted in {channel.mention}.",
            ephemeral=True,
        )

    # ---------- /setup-player-card ----------

    def _build_player_card_embed(
        self,
        guild: discord.Guild,
    ) -> discord.Embed:
        """Persistent /player help card — one Reg/Update button, view via
        the /player slash. Copy is finalised per manager sign-off."""
        embed = discord.Embed(
            title="👤 Player Info — Registered Character",
            description=(
                "Register once, view anytime. Your in-game info gets used "
                "by GvG, LFG, and other member-facing features.\n\n"
                "**🧾 First-time setup**\n"
                "Click button below to record your in-game **UID**, "
                "**Character name**, **Server**, **Main** + **Sub weapon**, "
                "and **Guild membership** status.\n\n"
                "**📋 View your info**\n"
                "Run `/player` to see the profile linked to your Discord "
                "account.\nWant to look someone else up? Use "
                "`/player user:@X` or `/player uid:0048985000`.\n\n"
                "**♻️ Update anytime**\n"
                "Weapon change, moved server, renamed in-game? Just click "
                "button below again — the form pre-fills your current data "
                "so you only edit what changed."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Player Info System by AKim")
        return embed

    def _build_player_card_view(
        self, guild_id: int,
    ) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(PlayerCardRegButton(guild_id))
        return view

    @app_commands.command(
        name="setup-player-card",
        description="(Owner/Manage Server) Post the persistent Player Info card in a channel",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel to post the Player Info card (button opens /player-reg)",
    )
    async def setup_player_card(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        assert interaction.guild is not None
        # Any manager (owner OR Manage Server) can post this card — the
        # Discord UI gate is manage_guild, and runtime re-checks so a
        # bare admin without manage_guild can't sneak through.
        is_owner = self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        is_manager = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )
        if not (is_owner or is_manager):
            await interaction.response.send_message(
                "❌ Only the server owner or a Manage Server admin can "
                "post the Player Info card.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        # Delete the previous card if any so a re-run doesn't leave two.
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            old_ch_id = cfg.player_card_channel_id if cfg else None
            old_msg_id = cfg.player_card_message_id if cfg else None
        if old_ch_id and old_msg_id:
            old_ch = interaction.guild.get_channel(old_ch_id)
            if isinstance(old_ch, (discord.TextChannel, discord.Thread)):
                try:
                    old_msg = await old_ch.fetch_message(old_msg_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        embed = self._build_player_card_embed(interaction.guild)
        view = self._build_player_card_view(interaction.guild_id)
        try:
            msg = await channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {channel.mention}.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            cfg.player_card_channel_id = channel.id
            cfg.player_card_message_id = msg.id
            await s.commit()

        await interaction.followup.send(
            f"✅ Player Info card posted in {channel.mention}.",
            ephemeral=True,
        )

    # ---------- /setup-gw-card ----------

    def _build_gw_card_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="⚔️ Guild War — Create Party",
            description=(
                "Click the button below to create a **Guild War** party.\n\n"
                "You will be asked for:\n"
                "• **Main Roster Limit** — total main slots (e.g. 30)\n"
                "• **Note** — optional notes for this party\n\n"
                "The party will be posted in the LFG channel."
            ),
            color=discord.Color.dark_red(),
        )
        embed.set_footer(text="Guild War System by AKim")
        return embed

    def _build_gw_card_view(self, guild_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(GuildWarCardButton(guild_id))
        return view

    @app_commands.command(
        name="setup-gw-card",
        description="(Owner/Manage Server) Post the persistent Guild War Card in a management channel",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel to post the Guild War Card (button opens Create GW modal)",
    )
    async def setup_gw_card(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        assert interaction.guild is not None
        is_owner = self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        is_manager = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )
        if not (is_owner or is_manager):
            await interaction.response.send_message(
                "❌ Only the server owner or a Manage Server admin can post the GW card.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        # Delete previous card if any
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            old_ch_id = cfg.gw_card_channel_id if cfg else None
            old_msg_id = cfg.gw_card_message_id if cfg else None
        if old_ch_id and old_msg_id:
            old_ch = interaction.guild.get_channel(old_ch_id)
            if isinstance(old_ch, (discord.TextChannel, discord.Thread)):
                try:
                    old_msg = await old_ch.fetch_message(old_msg_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        embed = self._build_gw_card_embed()
        view = self._build_gw_card_view(interaction.guild_id)
        try:
            msg = await channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {channel.mention}.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            cfg.gw_card_channel_id = channel.id
            cfg.gw_card_message_id = msg.id
            await s.commit()

        await interaction.followup.send(
            f"✅ Guild War card posted in {channel.mention}.",
            ephemeral=True,
        )

    # ---------- /setup-lfg-card ----------

    def _build_lfg_card_embed(self) -> discord.Embed:
        from ..views.lfg import _I18N
        ru = _I18N["ru"]
        en = _I18N["en"]
        embed = discord.Embed(
            title=f"{en['lfg_card_title']} / {ru['lfg_card_title']}",
            description=(
                f"{en['lfg_card_desc']}\n\n"
                "─────────────────────\n\n"
                f"{ru['lfg_card_desc']}"
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="LFG System by AKim")
        return embed

    def _build_lfg_card_view(self, guild_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(LFGCardHeroRealmButton(guild_id))
        view.add_item(LFGCardSwordTrialButton(guild_id))
        view.add_item(LFGCardCustomButton(guild_id))
        return view

    @app_commands.command(
        name="setup-lfg-card",
        description="(Owner/Manage Server) Post the LFG Card with Hero Realm / Sword Trial / Custom buttons",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel to post the LFG Card",
    )
    async def setup_lfg_card(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        assert interaction.guild is not None

        is_owner = self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        is_manager = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )
        if not (is_owner or is_manager):
            await interaction.response.send_message(
                "❌ You need **Manage Server** permission.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Delete previous card if any
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            old_ch_id = cfg.lfg_card_channel_id if cfg else None
            old_msg_id = cfg.lfg_card_message_id if cfg else None
        if old_ch_id and old_msg_id:
            old_ch = interaction.guild.get_channel(old_ch_id)
            if isinstance(old_ch, (discord.TextChannel, discord.Thread)):
                try:
                    old_msg = await old_ch.fetch_message(old_msg_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        embed = self._build_lfg_card_embed()
        view = self._build_lfg_card_view(interaction.guild_id)
        try:
            msg = await channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot can't post in {channel.mention} — check permissions.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            cfg.lfg_card_channel_id = channel.id
            cfg.lfg_card_message_id = msg.id
            await s.commit()

        await interaction.followup.send(
            f"✅ LFG card posted in {channel.mention}.",
            ephemeral=True,
        )

    # ---------- /setup-lfg-channels ----------

    @app_commands.command(
        name="setup-lfg-channels",
        description="(Owner/Manage Server) Configure per-type LFG post channels",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        hero_realm="Channel for Hero Realm parties (leave blank to use default)",
        sword_trial="Channel for Sword Trial parties (leave blank to use default)",
        custom="Channel for Custom parties (leave blank to use default)",
    )
    async def setup_lfg_channels(
        self,
        interaction: discord.Interaction,
        hero_realm: discord.TextChannel | None = None,
        sword_trial: discord.TextChannel | None = None,
        custom: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            if hero_realm is not None:
                cfg.lfg_hero_realm_channel_id = hero_realm.id
            if sword_trial is not None:
                cfg.lfg_sword_trial_channel_id = sword_trial.id
            if custom is not None:
                cfg.lfg_custom_channel_id = custom.id
            await s.commit()

        lines = ["✅ LFG per-type channels updated:"]
        if hero_realm:
            lines.append(f"• Hero Realm → {hero_realm.mention}")
        if sword_trial:
            lines.append(f"• Sword Trial → {sword_trial.mention}")
        if custom:
            lines.append(f"• Custom → {custom.mention}")
        if len(lines) == 1:
            lines.append("_(no changes — pass at least one channel)_")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ---------- /setup-baodanh-link ----------

    @app_commands.command(
        name="setup-baodanh-link",
        description="(Owner) Configure another bot's Link UID slash mention on the registration card",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        command_id=(
            "Slash command ID (NOT the bot ID). Enable Developer Mode → "
            "right-click the command in its menu → Copy Command ID."
        ),
        command_name="Slash command name (default: link)",
        bot_id=(
            "ID of the bot that owns the command (stored for reference). "
            "Example: 1436445818881314979"
        ),
    )
    async def setup_baodanh_link(
        self,
        interaction: discord.Interaction,
        command_id: app_commands.Range[str, 1, 32],
        command_name: app_commands.Range[str, 1, 64] = "link",
        bot_id: app_commands.Range[str, 1, 32] | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        assert interaction.guild is not None
        if not self.bot.is_owner(interaction.user.id, interaction.guild.owner_id):
            await interaction.response.send_message(
                "❌ Only the server owner can configure the registration card link.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        # Discord IDs are 17-20 digit snowflakes — validate before
        # stuffing into a BIGINT column so a typo doesn't get persisted.
        cid_clean = command_id.strip()
        if not cid_clean.isdigit() or not (15 <= len(cid_clean) <= 20):
            await interaction.followup.send(
                "❌ Invalid `command_id` — must be a snowflake ID "
                "(15-20 digits). Enable Developer Mode → right-click "
                "the slash command → Copy Command ID.",
                ephemeral=True,
            )
            return
        bid_clean: str | None = None
        if bot_id is not None:
            bid_clean = bot_id.strip()
            if not bid_clean.isdigit() or not (15 <= len(bid_clean) <= 20):
                await interaction.followup.send(
                    "❌ Invalid `bot_id` — must be a snowflake ID (15-20 digits).",
                    ephemeral=True,
                )
                return
        # Slash command names must match Discord's name regex —
        # lowercase letters, digits, hyphen, underscore, 1-32 chars.
        # We trim and lowercase for the user's convenience but reject
        # anything else outright.
        name_clean = command_name.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", name_clean):
            await interaction.followup.send(
                "❌ Invalid `command_name` — only lowercase letters, "
                "digits, `-`, `_`, up to 32 characters.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            cfg.baodanh_link_command_id = int(cid_clean)
            cfg.baodanh_link_command_name = name_clean
            cfg.baodanh_link_bot_id = int(bid_clean) if bid_clean else None
            await s.commit()

        refreshed = await self._refresh_baodanh_card_if_posted(
            interaction.guild,
        )
        mention = f"</{name_clean}:{cid_clean}>"
        bot_line = f"\n• Bot ID: `{bid_clean}`" if bid_clean else ""
        refresh_line = (
            "\n✅ Registration card refreshed with the new mention."
            if refreshed else
            "\n_(No registration card yet — run `/setup-baodanh-card` to create one.)_"
        )
        await interaction.followup.send(
            f"✅ Configured Link UID mention:\n"
            f"• Command: {mention}{bot_line}"
            f"{refresh_line}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /member group (owner-only) ----------

    # default_permissions=administrator hides the command from non-admins in
    # the Discord UI. The owner check inside each subcommand is the actual
    # enforcement, since an admin override could still expose the command.
    member = app_commands.Group(
        name="member",
        description="Manage the in-game Guild member UID list (owner only)",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is None or not self.bot.is_owner(interaction.user.id, guild.owner_id):
            await interaction.response.send_message(
                "❌ Only the server owner can use `/member`.", ephemeral=True
            )
            return False
        return True

    @member.command(name="add", description="Add one or more UIDs (separated by space/comma/newline)")
    @app_commands.describe(uids="e.g. 0048985000, 0012345678 ...")
    async def member_add(
        self,
        interaction: discord.Interaction,
        uids: app_commands.Range[str, 1, 1500],
    ) -> None:
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None

        raw = [u.strip() for u in _UID_SPLIT.split(uids) if u.strip()]
        if not raw:
            await interaction.response.send_message(
                "No valid UIDs found.", ephemeral=True
            )
            return

        # Slow path (Postgres round-trip) — defer so we don't miss the
        # 3-second deadline on a cold connection.
        await interaction.response.defer(ephemeral=True)

        valid: list[str] = []
        rejected: list[str] = []
        for u in raw:
            if _UID_VALID.match(u):
                valid.append(u)
            else:
                rejected.append(u)

        added: list[str] = []
        already: list[str] = []
        async with self.bot.db.session() as s:
            # Existing UIDs for this guild
            existing_rows = await s.execute(
                select(GuildMemberUid.uid).where(
                    GuildMemberUid.guild_id == interaction.guild_id,
                    GuildMemberUid.uid.in_(valid) if valid else False,
                )
            )
            existing_set = {row[0] for row in existing_rows.all()}
            for u in valid:
                if u in existing_set:
                    already.append(u)
                    continue
                s.add(
                    GuildMemberUid(
                        guild_id=interaction.guild_id,
                        uid=u,
                        added_by=interaction.user.id,
                    )
                )
                added.append(u)
            await s.commit()

        lines = [f"📥 **Add-UID result** ({len(raw)} input)"]
        if added:
            lines.append(f"✅ Added ({len(added)}): `{', '.join(added)}`")
        if already:
            lines.append(f"⏭ Already present ({len(already)}): `{', '.join(already)}`")
        if rejected:
            lines.append(
                f"❌ Invalid UIDs (need 4-16 digits) ({len(rejected)}): "
                f"`{', '.join(rejected[:20])}`"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @member.command(name="delete", description="Remove one or more UIDs from the list")
    @app_commands.describe(uids="UIDs to remove (separated by space/comma/newline)")
    async def member_delete(
        self,
        interaction: discord.Interaction,
        uids: app_commands.Range[str, 1, 1500],
    ) -> None:
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None

        targets = [u.strip() for u in _UID_SPLIT.split(uids) if u.strip()]
        if not targets:
            await interaction.response.send_message(
                "No valid UIDs found.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            result = await s.execute(
                delete(GuildMemberUid).where(
                    GuildMemberUid.guild_id == interaction.guild_id,
                    GuildMemberUid.uid.in_(targets),
                )
            )
            await s.commit()
        deleted = result.rowcount or 0
        not_found = len(targets) - deleted if deleted >= 0 else 0
        msg = f"🗑 Removed {deleted}/{len(targets)} UIDs."
        if not_found > 0:
            msg += f" {not_found} UID(s) not found."
        await interaction.followup.send(msg, ephemeral=True)

    @member.command(
        name="upload",
        description="Update the Guild UID list from a JSON file (auto-dedupes)",
    )
    @app_commands.describe(
        file=".json file containing a UID array or {\"uids\": [...]}",
        mode=(
            "'merge' (default): only add new UIDs, keep existing ones. "
            "'replace': wipe all existing UIDs then import from the file."
        ),
    )
    async def member_upload(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        mode: Literal["merge", "replace"] = "merge",
    ) -> None:
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None

        # Pre-flight validation before deferring so the user sees a
        # cheap-to-generate error immediately when they upload garbage.
        if not file.filename.lower().endswith(".json"):
            await interaction.response.send_message(
                "❌ File must have a **.json** extension.", ephemeral=True,
            )
            return
        if file.size > _UPLOAD_MAX_BYTES:
            await interaction.response.send_message(
                f"❌ File too large ({file.size:,} bytes). "
                f"Limit: **{_UPLOAD_MAX_BYTES:,} bytes** "
                f"(~{_UPLOAD_MAX_BYTES // (1024 * 1024)} MB).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Read + parse the attachment.
        try:
            raw_bytes = await file.read()
            data = json.loads(raw_bytes.decode("utf-8"))
        except UnicodeDecodeError:
            await interaction.followup.send(
                "❌ File is not valid UTF-8.", ephemeral=True,
            )
            return
        except json.JSONDecodeError as e:
            await interaction.followup.send(
                f"❌ Invalid JSON: `{e.msg}` (line {e.lineno}).",
                ephemeral=True,
            )
            return

        raw_uids = _extract_uids_from_json(data)
        if raw_uids is None:
            await interaction.followup.send(
                "❌ No UIDs found in file. Need one of these shapes:\n"
                "```\n"
                "[\"12345\", \"67890\"]\n"
                "{\"uids\": [\"12345\", \"67890\"]}\n"
                "[{\"uid\": \"12345\"}, {\"uid\": \"67890\"}]\n"
                "```",
                ephemeral=True,
            )
            return

        # Split into valid / invalid / dup-in-file. We keep dedupe here
        # (not DB-side) so we can report both "duplicated in file" and
        # "already in DB" separately — helps owners spot a bad export.
        valid: list[str] = []
        invalid: list[str] = []
        seen_in_file: set[str] = set()
        dup_in_file = 0
        for u in raw_uids:
            u = str(u).strip()
            if not u:
                continue
            if u in seen_in_file:
                dup_in_file += 1
                continue
            seen_in_file.add(u)
            if _UID_VALID.match(u):
                valid.append(u)
            else:
                invalid.append(u)

        # DB writes: replace-mode wipes existing rows first (per guild),
        # then both modes fall into the same insert-if-new path so we
        # never double-insert on 'replace' either.
        replaced = 0
        async with self.bot.db.session() as s:
            if mode == "replace":
                res = await s.execute(
                    delete(GuildMemberUid).where(
                        GuildMemberUid.guild_id == interaction.guild_id,
                    )
                )
                replaced = res.rowcount or 0

            existing_set: set[str] = set()
            if valid:
                existing_rows = await s.execute(
                    select(GuildMemberUid.uid).where(
                        GuildMemberUid.guild_id == interaction.guild_id,
                        GuildMemberUid.uid.in_(valid),
                    )
                )
                existing_set = {row[0] for row in existing_rows.all()}
            added: list[str] = []
            skipped_existing: list[str] = []
            for u in valid:
                if u in existing_set:
                    skipped_existing.append(u)
                    continue
                s.add(GuildMemberUid(
                    guild_id=interaction.guild_id,
                    uid=u,
                    added_by=interaction.user.id,
                ))
                added.append(u)
            await s.commit()

        lines = [
            f"📥 **Upload UID — {mode.upper()}** — `{file.filename}`",
            f"• Total UIDs in file: **{len(raw_uids):,}**",
        ]
        if dup_in_file:
            lines.append(f"• Duplicates in file: **{dup_in_file:,}**")
        if invalid:
            lines.append(f"• Bad format (skipped): **{len(invalid):,}**")
        if mode == "replace":
            lines.append(f"• 🗑 Removed **{replaced:,}** old UIDs before import.")
        lines.append(f"• ✅ Added to DB: **{len(added):,}**")
        if skipped_existing:
            lines.append(
                f"• ⏭ Already in DB (skipped): "
                f"**{len(skipped_existing):,}**"
            )
        if invalid:
            preview = ", ".join(f"`{u}`" for u in invalid[:10])
            more = (
                f" _(+{len(invalid) - 10} more)_"
                if len(invalid) > 10 else ""
            )
            lines.append(f"⚠️ Bad-format UIDs (first): {preview}{more}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @member.command(
        name="list",
        description="List Guild UIDs and cross-reference with registered players",
    )
    async def member_list(self, interaction: discord.Interaction) -> None:
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            uid_rows = await s.execute(
                select(GuildMemberUid)
                .where(GuildMemberUid.guild_id == interaction.guild_id)
                .order_by(GuildMemberUid.uid)
            )
            uids = list(uid_rows.scalars().all())
            if uids:
                player_rows = await s.execute(
                    select(Player).where(
                        Player.guild_id == interaction.guild_id,
                        Player.ingame_uid.in_([u.uid for u in uids]),
                    )
                )
                players_by_uid = {
                    p.ingame_uid: p for p in player_rows.scalars().all()
                }
            else:
                players_by_uid = {}

        if not uids:
            await interaction.followup.send(
                "No UIDs yet. Use `/member add` to add some.", ephemeral=True
            )
            return

        # Cross-reference: build one line per UID showing the owner if any.
        registered = 0
        unregistered = 0
        lines: list[str] = []
        for u in uids:
            p = players_by_uid.get(u.uid)
            if p is None:
                unregistered += 1
                lines.append(f"• `{u.uid}` — _not registered_")
            else:
                registered += 1
                lines.append(
                    f"• `{u.uid}` — <@{p.discord_id}> · `{p.ingame_name}`"
                )

        # Chunk to avoid embed description > 4000 chars
        chunks: list[str] = []
        current = ""
        for piece in lines:
            piece_nl = piece + "\n"
            if len(current) + len(piece_nl) > 3800:
                chunks.append(current)
                current = piece_nl
            else:
                current += piece_nl
        if current:
            chunks.append(current)

        title = (
            f"Guild UID list ({len(uids)}) — "
            f"✅ {registered} registered · ⚠️ {unregistered} not yet"
        )
        for i, c in enumerate(chunks):
            embed = discord.Embed(
                title=(
                    title if len(chunks) == 1
                    else f"{title} — page {i + 1}/{len(chunks)}"
                ),
                description=c,
                color=discord.Color.blurple(),
            )
            # Deferred at the top, so every chunk uses followup — the
            # first vs subsequent branch was needed only before the
            # defer was added.
            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


    # ---------- /setup-sync ----------

    @app_commands.command(
        name="setup-sync",
        description="Configure the /sync source bot (Discord User ID of WWM Workshop)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        bot_user_id=(
            "User ID of the WWM Workshop bot. Enable Developer Mode → "
            "right-click the WWM avatar → Copy User ID. Pass 0 to disable /sync."
        ),
    )
    async def setup_sync(
        self,
        interaction: discord.Interaction,
        bot_user_id: str,
    ) -> None:
        assert interaction.guild_id is not None
        try:
            bot_id = int(bot_user_id.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ User ID must be numeric. Enable Developer Mode in Discord, "
                "then Copy User ID.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            cfg.sync_source_bot_id = bot_id if bot_id > 0 else None
            await s.commit()

        if bot_id <= 0:
            await interaction.followup.send(
                "🔌 /sync disabled.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ /sync source bot configured: <@{bot_id}>\n"
            f"Users can now run `/sync UID:<uid>` to pull data from this bot.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="sync-status",
        description="(Admin) Check the current /sync configuration",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def sync_status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None and interaction.guild is not None
        # Defer up front: DB read + fetch_user (Discord API) can each
        # take >1s. Together they'd risk the 3-second deadline.
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)

        source_id = cfg.sync_source_bot_id if cfg else None
        embed = discord.Embed(
            title="🔌 /sync status", color=discord.Color.blurple()
        )

        if source_id is None:
            embed.description = (
                "❌ No source bot configured.\n"
                "Run `/setup-sync bot_user_id:<ID>` to set one."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Try to resolve the configured user as a bot member of this guild
        source_member = interaction.guild.get_member(source_id)
        if source_member is None:
            try:
                source_user = await self.bot.fetch_user(source_id)
            except (discord.NotFound, discord.HTTPException):
                source_user = None
            resolved_name = source_user.name if source_user else "not found"
            is_bot = source_user.bot if source_user else None
            in_guild = "❌ Not in this server"
        else:
            resolved_name = source_member.name
            is_bot = source_member.bot
            in_guild = "✅ In this server"

        embed.add_field(
            name="Configured source bot",
            value=f"<@{source_id}> (ID: `{source_id}`)",
            inline=False,
        )
        embed.add_field(name="Resolved name", value=resolved_name, inline=True)
        embed.add_field(
            name="Is a bot?",
            value="✅ Bot" if is_bot else ("❌ Not a bot" if is_bot is False else "❓"),
            inline=True,
        )
        embed.add_field(name="Present in this server", value=in_guild, inline=True)
        embed.add_field(
            name="💡 How to verify",
            value=(
                "1. Enable Developer Mode in Discord Settings → Advanced\n"
                "2. Right-click the **WWM Workshop** avatar in the server\n"
                "3. Click **Copy User ID** — compare with the ID above\n"
                "4. If different → run `/setup-sync bot_user_id:<correct-ID>`"
            ),
            inline=False,
        )
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /server group ----------

    server = app_commands.Group(
        name="server",
        description="Manage the in-game server list (EU/NA/JP/...)",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @server.command(name="add", description="Add an in-game server to the database")
    @app_commands.describe(
        name="Server name (e.g. EU, NA, JP, Asia)",
        icon="Emoji or custom emoji <:name:id> (optional)",
        sort_order="Display order (lower numbers appear first)",
    )
    async def server_add(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32],
        icon: str | None = None,
        sort_order: int = 0,
    ) -> None:
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(Server).where(
                    Server.guild_id == interaction.guild_id,
                    Server.name == name,
                )
            )
            if existing:
                await interaction.response.send_message(
                    f"❌ Server **{name}** already exists. Use `/server edit` to modify it.",
                    ephemeral=True,
                )
                return
            s.add(
                Server(
                    guild_id=interaction.guild_id,
                    name=name,
                    icon=icon,
                    sort_order=sort_order,
                )
            )
            await s.commit()
        await interaction.response.send_message(
            f"✅ Added server: {icon or ''} **{name}**", ephemeral=True
        )

    @server.command(name="edit", description="Edit server info")
    @app_commands.describe(
        current_name="Current server name",
        new_name="New name (leave blank = keep)",
        new_icon="New icon (leave blank = keep, type 'none' = remove)",
        new_sort_order="New display order (leave blank = keep)",
    )
    async def server_edit(
        self,
        interaction: discord.Interaction,
        current_name: app_commands.Range[str, 1, 32],
        new_name: app_commands.Range[str, 1, 32] | None = None,
        new_icon: str | None = None,
        new_sort_order: int | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            srv = await s.scalar(
                select(Server).where(
                    Server.guild_id == interaction.guild_id,
                    Server.name == current_name,
                )
            )
            if srv is None:
                await interaction.response.send_message(
                    f"❌ Server **{current_name}** not found.", ephemeral=True
                )
                return
            if new_name and new_name != current_name:
                clash = await s.scalar(
                    select(Server).where(
                        Server.guild_id == interaction.guild_id,
                        Server.name == new_name,
                    )
                )
                if clash:
                    await interaction.response.send_message(
                        f"❌ Name **{new_name}** already exists.", ephemeral=True
                    )
                    return
                srv.name = new_name
            if new_icon is not None:
                srv.icon = None if new_icon.strip().lower() == "none" else new_icon
            if new_sort_order is not None:
                srv.sort_order = new_sort_order
            await s.commit()
        await interaction.response.send_message(
            f"✅ Updated server **{current_name}**.", ephemeral=True
        )

    @server.command(name="delete", description="Remove a server from the list")
    @app_commands.describe(name="Server name to remove")
    async def server_delete(
        self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 32]
    ) -> None:
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            result = await s.execute(
                delete(Server).where(
                    Server.guild_id == interaction.guild_id,
                    Server.name == name,
                )
            )
            await s.commit()
        if result.rowcount:
            await interaction.response.send_message(
                f"🗑 Removed server **{name}**.\n"
                "ℹ️ Players who had this server selected will now show as 'not set'.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"Server **{name}** not found.", ephemeral=True
            )

    @server.command(name="list", description="List saved servers")
    async def server_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(Server)
                .where(Server.guild_id == interaction.guild_id)
                .order_by(Server.sort_order, Server.name)
            )
            servers = list(rows.scalars().all())
        if not servers:
            await interaction.response.send_message(
                "No servers yet. Use `/server add` to add one.", ephemeral=True
            )
            return
        lines = [
            f"{srv.icon or '•'} **{srv.name}** _(order: {srv.sort_order})_"
            for srv in servers
        ]
        embed = discord.Embed(
            title=f"Server list ({len(servers)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- /topic group ----------

    topic = app_commands.Group(
        name="topic",
        description="Set the channel topic — shown in the channel header",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @topic.command(
        name="set",
        description="Set the topic for a channel (opens a multi-line dialog, supports markdown)",
    )
    @app_commands.describe(channel="Channel whose topic to set")
    async def topic_set(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await interaction.response.send_modal(_TopicModal(channel))

    @topic.command(name="clear", description="Clear a channel's topic")
    @app_commands.describe(channel="Channel whose topic to clear")
    async def topic_clear(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        try:
            await channel.edit(
                topic=None, reason=f"/topic clear by {interaction.user}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot is missing **Manage Channels** on this channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"❌ Discord error: {e.text or e.status}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🗑 Cleared topic of {channel.mention}.", ephemeral=True
        )


class _TopicModal(discord.ui.Modal):
    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__(title=f"Topic — #{channel.name}"[:45])
        self.channel = channel
        # Discord enforces: label ≤ 45 chars, placeholder ≤ 100, default ≤ max_length.
        # Default could be a previously-set topic up to 1024 chars; truncate
        # defensively in case it ever exceeds.
        default_topic = (channel.topic or "")[:1024]
        self.text = discord.ui.TextInput(
            label="Topic content (max 1024 characters)",
            placeholder="**bold** *italic* __underline__ ~~strike~~ `code`",
            style=discord.TextStyle.paragraph,
            max_length=1024,
            required=False,
            default=default_topic,
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_topic = str(self.text.value)
        try:
            await self.channel.edit(
                topic=new_topic or None,
                reason=f"/topic set by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot is missing **Manage Channels** on this channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"❌ Discord error: {e.text or e.status}", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"✅ Topic set for #{self.channel.name}",
            description=new_topic or "_(topic cleared because input was empty)_",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(Admin(bot))
