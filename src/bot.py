from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import Settings
from .db.session import Database
from .desk_api.config import DeskApiSettings
from .cogs.economy import (
    CheckinButton,
    CheckinReminderToggleButton,
    EconLangButton,
    HistoryButton,
)
from .cogs.sohoc import (
    BetButton as SohocBetButton,
    EndButton as SohocEndButton,
    ExplainButton as SohocExplainButton,
    JoinButton as SohocJoinButton,
    LangToggleButton as SohocLangToggleButton,
    RollButton as SohocRollButton,
    ThandeBoardLangButton,
)
from .cogs.giveaway import (
    CancelButton as GiveawayCancelButton,
    FinishButton as GiveawayFinishButton,
    JoinButton as GiveawayJoinButton,
    LangToggleButton as GiveawayLangToggleButton,
    RerollButton as GiveawayRerollButton,
    StartRollButton as GiveawayStartRollButton,
)
from .cogs.oantuti import (
    AcceptButton as OantutiAcceptButton,
    ChoiceButton as OantutiChoiceButton,
    DeclineButton as OantutiDeclineButton,
    EndGameButton as OantutiEndGameButton,
    LangToggleButton as OantutiLangToggleButton,
    RematchButton as OantutiRematchButton,
    RestartButton as OantutiRestartButton,
    VodichBoardLangButton,
)
from .cogs.tournament import (
    BuildBracketButton as TourneyBuildBracketButton,
    CancelTourneyButton,
    JoinTourneyButton,
    LeaveTourneyButton,
    StartTourneyButton,
)
from .cogs.vuatiengviet import (
    AnswerButton as VtvAnswerButton,
    BxhBoardLangButton,
    EndButton as VtvEndButton,
    NextButton as VtvNextButton,
    StartButton as VtvStartButton,
)
from .cogs.quiz import CunhanBoardLangButton
from .cogs.pet_admin import (
    CancelImportButton as PetCancelImportButton,
    ConfirmImportButton as PetConfirmImportButton,
)
from .cogs.pet import PetHouseButton, PetShopButton
from .cogs.poll import (
    PollCancelButton,
    PollCloseButton,
    PollVoteSelect,
)
from .cogs.giveaway_owner import (
    GwCancelButton,
    GwJoinButton,
    GwRollButton,
)
from .cogs.gvg import (
    GvgAddMoreButton,
    GvgAssignPickerButton,
    GvgAssignPositionSelect,
    GvgAssignReservePickerButton,
    GvgAssignReservePositionSelect,
    GvgAssignReserveTeamSelect,
    GvgAssignTeamSelect,
    GvgLeaderClearButton,
    GvgLeaderTeamSelect,
    GvgLeaderUserSelect,
    GvgBulkMoveButton,
    GvgMovePickerButton,
    GvgPositionsButton,
    GvgRefreshButton,
    GvgRemovePickerButton,
    GvgRemoveUserSelect,
    GvgRosterUserSelect,
    GvgSelfRegisterButton,
    GvgSelfReserveButton,
    GvgSetLeaderButton,
    GvgSettingsButton,
    GvgTeamsButton,
    GvgUnRegisterButton,
    GvgViewRosterButton,
)
from .views.approval import ApproveButton, DenyButton, ToggleGuildMemberButton
from .views.lfg import (
    GuildWarCardButton,
    GuildWarLeaveButton,
    GuildWarRegisterButton,
    LFGCardCustomButton,
    LFGCardHeroRealmButton,
    LFGCardSwordTrialButton,
    LFGEndButton,
    LFGJoinButton,
    LFGLangToggleButton,
    LFGLeaveButton,
    LFGStartButton,
)
from .views.registration import (
    BaodanhCardButton,
    BaodanhLinkUidButton,
    PlayerCardRegButton,
)

log = logging.getLogger("akim-bot")


INITIAL_COGS = (
    "src.cogs.registration",
    "src.cogs.admin",
    "src.cogs.lfg",
    "src.cogs.sync",
    "src.cogs.quiz",
    "src.cogs.sohoc",
    "src.cogs.economy",
    "src.cogs.oantuti",
    "src.cogs.tournament",
    "src.cogs.vuatiengviet",
    "src.cogs.pet_admin",
    "src.cogs.pet",
    "src.cogs.giveaway",
    "src.cogs.giveaway_owner",
    "src.cogs.poll",
    "src.cogs.gvg",
)

# Cogs loaded only when the desk-api is enabled — slash commands for
# the desktop integration shouldn't appear in a plain-bot deploy.
DESK_API_COGS = (
    "src.cogs.link_desktop",
)


class AkimBot(commands.Bot):
    def __init__(
        self, settings: Settings, desk_settings: DeskApiSettings | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.members = True  # member display names + on_member_join
        intents.voice_states = True  # on_voice_state_update for voice rewards
        # message_content is a PRIVILEGED intent. Requesting it without
        # enabling it in the Discord Developer Portal makes the bot fail
        # to log in (PrivilegedIntentsRequired) — every slash command then
        # reports 'The application did not respond'. Keep it off by
        # default; the daily text reward simply skips its length check
        # when content is unavailable.
        super().__init__(
            command_prefix=commands.when_mentioned,  # slash-only; prefix unused
            intents=intents,
            application_id=settings.discord_app_id,
        )
        self.settings = settings
        self.desk_settings = desk_settings
        self.db = Database(settings.database_url)

    def is_owner(self, user_id: int, guild_owner_id: int | None) -> bool:
        """Return True when user_id is the guild owner OR a bot-level
        super-user (SUPER_USER_IDS env var).  Pass guild.owner_id as the
        second argument; None is treated as no-match (e.g. DM context)."""
        return user_id == guild_owner_id or user_id in self.settings.super_user_ids

    async def setup_hook(self) -> None:
        await self.db.init_models()

        # Register dynamic persistent buttons
        self.add_dynamic_items(
            LFGJoinButton,
            LFGEndButton,
            LFGLeaveButton,
            LFGStartButton,
            GuildWarRegisterButton,
            GuildWarLeaveButton,
            GuildWarCardButton,
            LFGLangToggleButton,
            LFGCardHeroRealmButton,
            LFGCardSwordTrialButton,
            LFGCardCustomButton,
            ApproveButton,
            DenyButton,
            ToggleGuildMemberButton,
            SohocJoinButton,
            SohocRollButton,
            SohocEndButton,
            SohocBetButton,
            SohocExplainButton,
            SohocLangToggleButton,
            GiveawayJoinButton,
            GiveawayStartRollButton,
            GiveawayRerollButton,
            GiveawayFinishButton,
            GiveawayCancelButton,
            GiveawayLangToggleButton,
            CheckinButton,
            HistoryButton,
            CheckinReminderToggleButton,
            EconLangButton,
            OantutiAcceptButton,
            OantutiDeclineButton,
            OantutiChoiceButton,
            OantutiRematchButton,
            OantutiRestartButton,
            OantutiEndGameButton,
            OantutiLangToggleButton,
            JoinTourneyButton,
            LeaveTourneyButton,
            TourneyBuildBracketButton,
            StartTourneyButton,
            CancelTourneyButton,
            VtvStartButton,
            VtvEndButton,
            VtvNextButton,
            VtvAnswerButton,
            PetConfirmImportButton,
            PetCancelImportButton,
            PetHouseButton,
            PetShopButton,
            BaodanhCardButton,
            BaodanhLinkUidButton,
            PlayerCardRegButton,
            PollVoteSelect,
            PollCloseButton,
            PollCancelButton,
            GwJoinButton,
            GwRollButton,
            GwCancelButton,
            GvgAssignPickerButton,
            GvgBulkMoveButton,
            GvgMovePickerButton,
            GvgTeamsButton,
            GvgRefreshButton,
            GvgAddMoreButton,
            GvgRosterUserSelect,
            GvgAssignTeamSelect,
            GvgAssignPositionSelect,
            GvgSetLeaderButton,
            GvgLeaderTeamSelect,
            GvgLeaderUserSelect,
            GvgLeaderClearButton,
            GvgRemovePickerButton,
            GvgRemoveUserSelect,
            GvgAssignReservePickerButton,
            GvgAssignReserveTeamSelect,
            GvgAssignReservePositionSelect,
            GvgViewRosterButton,
            GvgSettingsButton,
            GvgPositionsButton,
            GvgSelfRegisterButton,
            GvgSelfReserveButton,
            GvgUnRegisterButton,
            ThandeBoardLangButton,
            VodichBoardLangButton,
            BxhBoardLangButton,
            CunhanBoardLangButton,
        )

        for cog in INITIAL_COGS:
            await self.load_extension(cog)
            log.info("Loaded cog: %s", cog)

        if self.desk_settings is not None and self.desk_settings.enabled:
            for cog in DESK_API_COGS:
                await self.load_extension(cog)
                log.info("Loaded desk-api cog: %s", cog)

        # Slash-command sync target selection.
        #   1. DEV_GUILD_IDS (unchanged) — highest priority; historically
        #      the fast-iterate override.
        #   2. ALLOWED_GUILD_IDS — private-bot mode. If set, we skip the
        #      slow global sync and push commands only to the whitelisted
        #      guilds so they appear instantly.
        #   3. Neither set → legacy global sync (may take up to ~1h to
        #      propagate).
        sync_targets: tuple[int, ...] = ()
        sync_label = "dev"
        if self.settings.dev_guild_ids:
            sync_targets = self.settings.dev_guild_ids
        elif self.settings.allowed_guild_ids:
            sync_targets = self.settings.allowed_guild_ids
            sync_label = "allowed"
        if sync_targets:
            for gid in sync_targets:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info(
                    "Synced %d commands to %s guild %s",
                    len(synced), sync_label, gid,
                )
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id if self.user else None)
        await self.change_presence(
            activity=discord.Game(name="/baodanh • /lfg"),
            status=discord.Status.online,
        )
        # Guild whitelist sweep. Empty allowed_guild_ids = no enforcement
        # (legacy behavior), so an accidentally-blank env var can't
        # strand the bot by leaving every guild.
        allowed = self.settings.allowed_guild_ids
        if not allowed:
            return
        allowed_set = set(allowed)
        for guild in list(self.guilds):
            if guild.id not in allowed_set:
                log.warning(
                    "guild whitelist: leaving %s (%s) — not in "
                    "ALLOWED_GUILD_IDS",
                    guild.name, guild.id,
                )
                try:
                    await guild.leave()
                except discord.HTTPException as e:
                    log.warning(
                        "guild whitelist: leave failed for %s: %s",
                        guild.id, e,
                    )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Enforce the guild whitelist on new invites. Fires whenever
        the bot is added to a server. No-op when the whitelist is empty
        (legacy behavior)."""
        allowed = self.settings.allowed_guild_ids
        if not allowed:
            return
        if guild.id in set(allowed):
            log.info(
                "guild whitelist: %s (%s) admitted", guild.name, guild.id,
            )
            return
        log.warning(
            "guild whitelist: refusing invite to %s (%s) — leaving now",
            guild.name, guild.id,
        )
        try:
            await guild.leave()
        except discord.HTTPException as e:
            log.warning(
                "guild whitelist: leave failed for %s: %s", guild.id, e,
            )

    async def close(self) -> None:
        try:
            await self.db.close()
        finally:
            await super().close()
