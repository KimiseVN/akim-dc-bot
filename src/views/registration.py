from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

import discord
from sqlalchemy import select

from ..db.models import GuildConfig, Player, Server, Weapon
from .approval import post_pending_card
from .nickname import sync_nickname

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.registration.view")

# Optional post-save hook: called after RegistrationView.do_save has
# committed the Player row and finished its own follow-up messages.
# Used by the GvG self-register flow to attach a roster assignment
# in the same interaction. Hook receives the CURRENT interaction and
# the persisted `player.id`; a hook exception is logged but never
# rolls back the registration itself.
PostSaveHook = Callable[[discord.Interaction, int], Awaitable[None]]


MAX_SELECT_OPTIONS = 25


def _weapon_to_option(w: Weapon, default: bool = False) -> discord.SelectOption:
    emoji = None
    if w.icon:
        try:
            emoji = discord.PartialEmoji.from_str(w.icon)
        except Exception:
            emoji = None
    return discord.SelectOption(
        label=w.name[:100],
        value=str(w.id),
        emoji=emoji,
        default=default,
    )


async def load_weapons(bot: "AkimBot") -> list[Weapon]:
    async with bot.db.session() as s:
        rows = await s.execute(select(Weapon).order_by(Weapon.sort_order, Weapon.name))
        return list(rows.scalars().all())


async def load_servers(bot: "AkimBot", guild_id: int) -> list[Server]:
    async with bot.db.session() as s:
        rows = await s.execute(
            select(Server)
            .where(Server.guild_id == guild_id)
            .order_by(Server.sort_order, Server.name)
        )
        return list(rows.scalars().all())


async def load_guild_config(bot: "AkimBot", guild_id: int) -> GuildConfig | None:
    async with bot.db.session() as s:
        return await s.get(GuildConfig, guild_id)


class _WeaponSelect(discord.ui.Select):
    def __init__(self, kind: str, weapons: list[Weapon], current_id: int | None) -> None:
        self.kind = kind  # "main" or "sub"
        valid = [w for w in weapons if w.slot in (kind, "both")][:MAX_SELECT_OPTIONS]
        row = 0 if kind == "main" else 1
        if not valid:
            options = [
                discord.SelectOption(
                    label="(No weapons in database)",
                    value="__none__",
                    description="Admin needs to seed the weapon list",
                )
            ]
            super().__init__(
                placeholder=(
                    "Main weapon" if kind == "main" else "Sub weapon"
                ),
                options=options,
                disabled=True,
                min_values=1,
                max_values=1,
                row=row,
            )
            return

        options = [_weapon_to_option(w, default=(w.id == current_id)) for w in valid]
        super().__init__(
            placeholder=(
                "Main weapon" if kind == "main" else "Sub weapon"
            ),
            options=options,
            min_values=1,
            max_values=1,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RegistrationView" = self.view  # type: ignore[assignment]
        chosen = self.values[0]
        if chosen == "__none__":
            await interaction.response.defer()
            return
        if self.kind == "main":
            view.main_weapon_id = int(chosen)
        else:
            view.sub_weapon_id = int(chosen)
        for opt in self.options:
            opt.default = opt.value == chosen
        await interaction.response.edit_message(view=view)


class _GuildMemberSelect(discord.ui.Select):
    def __init__(self, guild_name: str | None, current: bool | None) -> None:
        prompt = (
            f"Are you a member of Guild {guild_name}?"
            if guild_name
            else "Are you a member of the Guild?"
        )
        options = [
            discord.SelectOption(
                label="Yes, I'm a Guild member",
                value="yes",
                emoji="✅",
                default=current is True,
            ),
            discord.SelectOption(
                label="No, I don't belong to this Guild",
                value="no",
                emoji="❌",
                default=current is False,
            ),
        ]
        super().__init__(
            placeholder=prompt[:150],
            options=options,
            min_values=1,
            max_values=1,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RegistrationView" = self.view  # type: ignore[assignment]
        view.is_guild_member = self.values[0] == "yes"
        for opt in self.options:
            opt.default = opt.value == self.values[0]
        await interaction.response.edit_message(view=view)


class _ServerSelect(discord.ui.Select):
    def __init__(self, servers: list[Server], current_id: int | None) -> None:
        if not servers:
            super().__init__(
                placeholder="Server (none configured)",
                options=[
                    discord.SelectOption(
                        label="(Admin hasn't added any server yet)",
                        value="__none__",
                        description="Use /server add to create one",
                    )
                ],
                disabled=True,
                min_values=1,
                max_values=1,
                row=3,
            )
            return
        options: list[discord.SelectOption] = []
        for srv in servers[:MAX_SELECT_OPTIONS]:
            emoji = None
            if srv.icon:
                try:
                    emoji = discord.PartialEmoji.from_str(srv.icon)
                except Exception:
                    emoji = None
            options.append(
                discord.SelectOption(
                    label=srv.name[:100],
                    value=str(srv.id),
                    emoji=emoji,
                    default=srv.id == current_id,
                )
            )
        super().__init__(
            placeholder="Pick your in-game server",
            options=options,
            min_values=1,
            max_values=1,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RegistrationView" = self.view  # type: ignore[assignment]
        chosen = self.values[0]
        if chosen == "__none__":
            await interaction.response.defer()
            return
        view.server_id = int(chosen)
        for opt in self.options:
            opt.default = opt.value == chosen
        await interaction.response.edit_message(view=view)


class _SaveButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Save",
            emoji="💾",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RegistrationView" = self.view  # type: ignore[assignment]
        if view.main_weapon_id is None or view.sub_weapon_id is None:
            await interaction.response.send_message(
                "You need to pick both main and sub weapons.",
                ephemeral=True,
            )
            return
        if view.requires_guild_question and view.is_guild_member is None:
            guild_name = view.guild_name or "Guild"
            await interaction.response.send_message(
                f"Please answer Yes / No to the "
                f"'member of Guild {guild_name}' question.",
                ephemeral=True,
            )
            return
        if view.servers_available and view.server_id is None:
            await interaction.response.send_message(
                "Please pick your in-game server.", ephemeral=True,
            )
            return
        await view.do_save(interaction)


class RegistrationView(discord.ui.View):
    def __init__(
        self,
        bot: "AkimBot",
        weapons: list[Weapon],
        servers: list[Server],
        *,
        uid: str,
        ingame_name: str,
        prefill: Player | None = None,
        guild_name: str | None = None,
        ask_guild_question: bool = True,
        target_user_id: int | None = None,
        post_save: PostSaveHook | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.prefill = prefill
        self.uid = uid
        self.ingame_name = ingame_name
        self.guild_name = guild_name
        self.requires_guild_question = ask_guild_question
        # None → the actor is registering themselves (default). Set to
        # a Discord user id when an owner / manager runs /player-reg
        # with the optional `user` param — do_save then writes the
        # Player row against that user instead of the caller.
        self.target_user_id = target_user_id
        # Optional post-save hook; see PostSaveHook docstring.
        self.post_save = post_save
        self.servers_available = bool(servers)
        self.main_weapon_id: int | None = prefill.main_weapon_id if prefill else None
        self.sub_weapon_id: int | None = prefill.sub_weapon_id if prefill else None
        self.is_guild_member: bool | None = (
            prefill.is_guild_member if (prefill and ask_guild_question) else None
        )
        self.server_id: int | None = prefill.server_id if prefill else None

        self.add_item(_WeaponSelect("main", weapons, self.main_weapon_id))
        self.add_item(_WeaponSelect("sub", weapons, self.sub_weapon_id))
        if ask_guild_question:
            self.add_item(_GuildMemberSelect(guild_name, self.is_guild_member))
        if servers:
            self.add_item(_ServerSelect(servers, self.server_id))
        self.add_item(_SaveButton())

    async def do_save(self, interaction: discord.Interaction) -> None:
        """Persist the selections + UID against the user's Player row."""
        # Acknowledge the interaction immediately so we get the 15-min
        # followup window. Otherwise the DB writes + post_pending_card
        # (which itself does a channel.send) can race past Discord's
        # 3-second response deadline → 'This interaction failed'.
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        # target_user_id set → owner/manager is registering FOR another
        # user; every write and every downstream event fires against
        # that user, not the caller.
        user_id = self.target_user_id or interaction.user.id
        acting_user_id = interaction.user.id
        is_proxy = self.target_user_id is not None
        guild = interaction.guild
        assert guild_id is not None and guild is not None

        cfg = await load_guild_config(self.bot, guild_id)
        if is_proxy:
            # `member` is the *target* member (the one being registered),
            # since it drives role add + nickname sync.
            member = guild.get_member(user_id)
        else:
            member = (
                interaction.user
                if isinstance(interaction.user, discord.Member)
                else None
            )

        # Approval-mode flag. NULL/TRUE → legacy pending flow; FALSE
        # + baodanh_auto_role_id → save is auto-approved on the spot.
        require_approval = True
        auto_role_id: int | None = None
        if cfg is not None:
            if cfg.baodanh_require_approval is False:
                require_approval = False
            auto_role_id = cfg.baodanh_auto_role_id
        # Auto-approve only if BOTH the flag is off AND a role is
        # configured — otherwise we'd approve a user with no role to
        # grant, which is a broken state.
        auto_approve = (not require_approval) and (auto_role_id is not None)

        # ingame_name comes from the modal (required field) so we always
        # have a real character name, even before /sync fills in
        # level / combat power / elegance from the source bot.
        # In practice `self.ingame_name` is always present (required
        # field on the modal); the fallback covers a defensive edge.
        # Never fall back to the acting user's name when this is a
        # proxy registration — that would misattribute the target.
        fallback_name = (
            member.display_name if member is not None
            else (str(user_id) if is_proxy else interaction.user.name)
        )
        char_name = self.ingame_name.strip()[:64] or (
            (self.prefill.ingame_name if self.prefill else None)
            or fallback_name
        )[:64]
        placeholder_luc_chien = self.prefill.luc_chien if self.prefill else 0

        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(Player).where(
                    Player.guild_id == guild_id,
                    Player.discord_id == user_id,
                )
            )

            # Status decision:
            #   • Already-approved users stay approved on re-submit.
            #   • Everyone else: 'approved' if auto_approve mode is on,
            #     otherwise 'pending' (posts an approval card).
            if existing is not None and existing.approval_status == "approved":
                target_status = "approved"
            elif auto_approve:
                target_status = "approved"
            else:
                target_status = "pending"

            if existing is None:
                player = Player(
                    guild_id=guild_id,
                    discord_id=user_id,
                    ingame_name=char_name,
                    ingame_uid=self.uid,
                    luc_chien=placeholder_luc_chien,
                    hang_dau_truong=None,
                    main_weapon_id=self.main_weapon_id,
                    sub_weapon_id=self.sub_weapon_id,
                    server_id=self.server_id,
                    is_guild_member=bool(self.is_guild_member),
                    approval_status=target_status,
                )
                s.add(player)
                action = "Registered"
            else:
                existing.ingame_name = char_name
                existing.ingame_uid = self.uid
                existing.main_weapon_id = self.main_weapon_id
                existing.sub_weapon_id = self.sub_weapon_id
                existing.server_id = self.server_id
                existing.is_guild_member = bool(self.is_guild_member)
                existing.approval_status = target_status
                player = existing
                action = "Updated"

            # For fresh auto-approved rows we also stamp the audit
            # fields the /baodanh approve button would set, so a later
            # admin viewing the record sees the "self-approved" origin.
            is_fresh_auto_approve = (
                auto_approve
                and target_status == "approved"
                and (
                    existing is None
                    or existing.approval_status != "approved"
                )
            )
            if is_fresh_auto_approve:
                from datetime import datetime as _dt, timezone as _tz
                player.approved_by = interaction.user.id
                player.approved_role_id = auto_role_id
                player.approved_at = _dt.now(_tz.utc)
            await s.commit()
            await s.refresh(player)
            player_id = player.id
            player_status = player.approval_status
            # The modal now requires the user to type their in-game name
            # up front, so nickname sync is always safe on save.
            has_real_name = True

        # Pull related entities for the confirmation embed.
        async with self.bot.db.session() as s:
            fresh_player = await s.get(Player, player_id)
            main_w = await s.get(Weapon, self.main_weapon_id)
            sub_w = await s.get(Weapon, self.sub_weapon_id)
            srv = (
                await s.get(Server, self.server_id)
                if self.server_id is not None
                else None
            )

        # Pending submission: post approval card and short-circuit.
        if player_status == "pending":
            posted_card = None
            if fresh_player is not None:
                posted_card = await post_pending_card(
                    self.bot, guild, fresh_player
                )
            # post_pending_card returns None on any Discord failure —
            # data is already committed, but the admin notification
            # didn't land. Tell the user their registration is saved
            # and nudge them to ping an admin manually.
            who = f"<@{user_id}>" if is_proxy else "You"
            posted_who = f"<@{user_id}>'s" if is_proxy else "Your"
            if posted_card is None:
                confirmation = (
                    f"📝 **{posted_who} registration has been saved.**\n"
                    "⚠️ Bot couldn't notify the admins (approval "
                    "channel not configured or bot lacks permission "
                    "to post there). Please DM an admin to get "
                    "approved."
                )
            else:
                confirmation = (
                    f"📨 **{posted_who} registration was sent to the "
                    "admins for approval.**\n"
                    f"{who} will get the official role once approved."
                )
            await interaction.followup.send(
                content=confirmation,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self._run_post_save(interaction, player_id)
            return

        # Auto-approve grant: when we just flipped a fresh submission
        # into 'approved' status (require_approval=False + role
        # configured), grant the role now + remove the unverified
        # role. Safe to no-op if member is None (rare edge case).
        auto_role_grant_note: str | None = None
        auto_role_used: discord.Role | None = None
        if is_fresh_auto_approve and member is not None and auto_role_id is not None:
            role = guild.get_role(auto_role_id)
            if role is None:
                auto_role_grant_note = (
                    f"⚠️ Configured auto-role (id `{auto_role_id}`) "
                    "no longer exists on this server."
                )
            else:
                auto_role_used = role
                me = guild.me
                if me is None or role >= me.top_role:
                    auto_role_grant_note = (
                        f"⚠️ Role {role.mention} is above the bot's "
                        "top role — couldn't grant. Owner: raise the "
                        "bot's role above it."
                    )
                else:
                    try:
                        await member.add_roles(
                            role, reason="Akim Bot /baodanh auto-approve",
                        )
                    except discord.Forbidden:
                        auto_role_grant_note = (
                            f"⚠️ Bot lacks Manage Roles to grant "
                            f"{role.mention}."
                        )
                    except discord.HTTPException as e:
                        auto_role_grant_note = (
                            f"⚠️ Discord rejected role grant: "
                            f"`{e.text or e.status}`."
                        )
                    else:
                        # Also strip the unverified role if configured.
                        if cfg and cfg.unverified_role_id is not None:
                            unverified = guild.get_role(
                                cfg.unverified_role_id
                            )
                            if unverified and unverified in member.roles:
                                try:
                                    await member.remove_roles(
                                        unverified,
                                        reason=(
                                            "Akim Bot /baodanh "
                                            "auto-approve"
                                        ),
                                    )
                                except (discord.Forbidden, discord.HTTPException):
                                    # Non-fatal — the primary grant
                                    # went through.
                                    pass
            # Fire the same event admin-approve fires, so downstream
            # cogs (economy approval_reward, etc.) treat the two paths
            # identically.
            if auto_role_used is not None:
                self.bot.dispatch(
                    "player_approved", guild_id, user_id,
                )

        # Approved path: only sync the nickname once /sync has filled in a
        # real ingame_name; otherwise leave the Discord nickname alone so
        # users don't suddenly get renamed to '<Discord-display> - 🇪🇺EU'
        # before they've run /sync.
        nick_note: str | None = None
        if has_real_name and member is not None and fresh_player is not None:
            nick_note = await sync_nickname(
                self.bot, member, fresh_player, reason="Akim Bot /baodanh sync"
            )

        title_prefix = (
            "✅ Auto-approved — info"
            if is_fresh_auto_approve
            else f"✅ {action} character info"
        )
        if is_proxy:
            title_prefix += " (proxy)"
        embed = discord.Embed(
            title=title_prefix,
            color=discord.Color.green(),
        )
        if is_proxy:
            embed.description = f"Saved to <@{user_id}>'s profile."
        embed.add_field(name="In-game Name", value=char_name, inline=True)
        embed.add_field(name="UID", value=self.uid, inline=True)
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
        if srv:
            embed.add_field(
                name="Server",
                value=f"{srv.icon or ''}{srv.name}".strip(),
                inline=True,
            )
        embed.add_field(
            name="Guild member?",
            value="✅ Yes" if self.is_guild_member else "❌ No",
            inline=True,
        )
        if auto_role_used is not None:
            embed.add_field(
                name="Auto-granted role",
                value=auto_role_used.mention,
                inline=True,
            )
        if auto_role_grant_note is not None:
            embed.add_field(
                name="Role grant note",
                value=auto_role_grant_note,
                inline=False,
            )
        if nick_note:
            embed.add_field(name="Nickname update", value=nick_note, inline=False)
        await interaction.followup.send(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._run_post_save(interaction, player_id)

    async def _run_post_save(
        self, interaction: discord.Interaction, player_id: int,
    ) -> None:
        """Fire the optional post-save hook. Never lets an exception
        inside the hook wipe the registration confirmation — logs
        and swallows so the caller's registration still succeeds."""
        if self.post_save is None:
            return
        try:
            await self.post_save(interaction, player_id)
        except Exception:
            log.exception(
                "RegistrationView post_save hook failed "
                "(player_id=%s)", player_id,
            )


class UidModal(discord.ui.Modal):
    """Step 1 of /baodanh + /player-reg — collect UID and in-game name,
    then open the selection view.

    When ``target_user_id`` is set the modal is being opened by an
    owner/manager on behalf of that user; ``prefill`` should already
    reflect that user's existing Player row and the resulting save
    writes against ``target_user_id`` instead of the caller.

    ``post_save`` runs after the RegistrationView finishes writing —
    used by the GvG board's Register-Self flow to attach a roster
    assignment once the user's Player row exists. Signature:
    ``await post_save(interaction, player_id)``. Exceptions raised
    inside the hook are logged but never wipe the registration."""

    def __init__(
        self,
        bot: "AkimBot",
        prefill: Player | None,
        *,
        target_user_id: int | None = None,
        post_save: "PostSaveHook | None" = None,
    ) -> None:
        # Discord caps modal titles at 45 chars — the earlier "for
        # other user" variant hit 50 and raised ValueError inside
        # send_modal, so the interaction never got a response.
        title = (
            "Register (proxy) — UID + Name"
            if target_user_id is not None
            else "Registration — Step 1: UID + Name"
        )
        super().__init__(title=title)
        self.bot = bot
        self.prefill = prefill
        self.target_user_id = target_user_id
        self.post_save = post_save
        self.uid_input = discord.ui.TextInput(
            label="In-game UID",
            placeholder="e.g. 0048985000 (4-16 digits)",
            min_length=4,
            max_length=16,
            required=True,
            default=prefill.ingame_uid if prefill else None,
        )
        # Pre-fill both UID and character name from any existing Player
        # row so a returning member sees the values they registered
        # last time (rather than a blank name field that would prompt
        # them to retype). Fresh registrations start empty.
        default_name = prefill.ingame_name if prefill is not None else None
        self.name_input = discord.ui.TextInput(
            label="In-game Character Name",
            placeholder="Your character name in-game",
            min_length=1,
            max_length=64,
            required=True,
            default=default_name,
        )
        self.add_item(self.uid_input)
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        uid = str(self.uid_input.value).strip()
        if not uid.isdigit() or not (4 <= len(uid) <= 16):
            await interaction.response.send_message(
                "❌ UID must be 4-16 digits.", ephemeral=True
            )
            return
        ingame_name = str(self.name_input.value).strip()
        if not ingame_name:
            await interaction.response.send_message(
                "❌ In-game character name is required.", ephemeral=True
            )
            return
        assert interaction.guild_id is not None

        weapons = await load_weapons(self.bot)
        servers = await load_servers(self.bot, interaction.guild_id)
        cfg = await load_guild_config(self.bot, interaction.guild_id)
        guild_name = cfg.guild_name if cfg else None

        view = RegistrationView(
            self.bot,
            weapons,
            servers,
            uid=uid,
            ingame_name=ingame_name,
            prefill=self.prefill,
            guild_name=guild_name,
            ask_guild_question=True,
            target_user_id=self.target_user_id,
            post_save=self.post_save,
        )

        if self.target_user_id is not None:
            title = (
                "Update character info (proxy)"
                if self.prefill else
                "Registration (proxy)"
            )
        else:
            title = "Update character info" if self.prefill else "Registration"
        msg = (
            f"**UID:** `{uid}`\n"
            "Pick weapons, server, answer the Guild question → click **Save**:"
        )
        if self.target_user_id is not None:
            msg = (
                f"⚙️ **Registering for <@{self.target_user_id}>** "
                "(saved to their profile, not yours).\n\n" + msg
            )
        if not weapons:
            msg += "\n\n⚠️ No weapons configured. Admin: run `/weapon add`."
        if not servers:
            msg += "\n\nℹ️ No servers configured. Admin: run `/server add`."
        if not cfg or not cfg.guild_name:
            msg += (
                "\n\nℹ️ Admin hasn't set the in-game guild name via "
                "`/setup-baodanh` yet."
            )
        embed = discord.Embed(
            title=title, description=msg, color=discord.Color.blurple()
        )
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )


# =============================================================================
# Persistent "Register" button — posted by /setup-baodanh-card
# =============================================================================


class BaodanhCardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"baodanh:card:(?P<guild_id>\d+)",
):
    """Single-button card the owner posts to a public channel via
    /setup-baodanh-card. Clicking it kicks off the same UidModal flow
    /baodanh moi triggers — looks up any existing Player row for
    prefill, then opens the modal as an ephemeral. custom_id carries
    guild_id so the persistent registration in bot.setup_hook can
    rebuild the button without per-guild bookkeeping."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"baodanh:card:{guild_id}",
                label="Register",
                emoji="📝",
                style=discord.ButtonStyle.success,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            return
        # Prefill from any existing Player row so a returning member's
        # current data is pre-populated — same behaviour as /baodanh moi.
        bot = interaction.client
        async with bot.db.session() as s:  # type: ignore[attr-defined]
            existing = await s.scalar(
                select(Player).where(
                    Player.guild_id == interaction.guild_id,
                    Player.discord_id == interaction.user.id,
                )
            )
        await interaction.response.send_modal(UidModal(bot, prefill=existing))  # type: ignore[arg-type]


class PlayerCardRegButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"player:card:reg:(?P<guild_id>\d+)",
):
    """Single-button card the owner posts to a public channel via
    /setup-player-card. Clicking it opens the same registration modal
    /player-reg triggers — with prefill from an existing Player row so
    a returning member's current data is pre-populated."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"player:card:reg:{guild_id}",
                label="Reg/Update my info",
                emoji="📝",
                style=discord.ButtonStyle.primary,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            return
        bot = interaction.client
        async with bot.db.session() as s:  # type: ignore[attr-defined]
            existing = await s.scalar(
                select(Player).where(
                    Player.guild_id == interaction.guild_id,
                    Player.discord_id == interaction.user.id,
                )
            )
        await interaction.response.send_modal(UidModal(bot, prefill=existing))  # type: ignore[arg-type]


class BaodanhLinkUidButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"baodanh:link:(?P<guild_id>\d+)",
):
    """Companion button next to BaodanhCardButton. Discord refuses to
    let one bot invoke another bot's slash command from a button
    callback, so we do the next-best thing: on click we send an
    ephemeral containing the </name:id> app mention for the OTHER
    bot's /link command. The user then taps the inline mention which
    pops that bot's command picker in their composer.

    The card builder only attaches this button when GuildConfig has a
    link command_id configured, so an unconfigured guild never gets
    a button that opens an error toast."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"baodanh:link:{guild_id}",
                label="Link UID",
                emoji="🔗",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            return
        bot = interaction.client
        async with bot.db.session() as s:  # type: ignore[attr-defined]
            cfg = await s.get(GuildConfig, interaction.guild_id)
        # Re-check at click time — owner may have cleared the link
        # config after the card was posted; rather than show a broken
        # mention we tell the clicker the feature isn't live.
        if cfg is None or not cfg.baodanh_link_command_id:
            await interaction.response.send_message(
                "❌ Admin hasn't configured Link UID yet. Ask them to "
                "run `/setup-baodanh-link` to enable it.",
                ephemeral=True,
            )
            return
        name = cfg.baodanh_link_command_name or "link"
        mention = f"</{name}:{cfg.baodanh_link_command_id}>"
        await interaction.response.send_message(
            f"🔗 Click here to **Link your UID**:\n{mention}",
            ephemeral=True,
        )
