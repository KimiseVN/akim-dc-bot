"""User-facing pet cog (Phase 3).

Slash surface
  /pet shop                       — list shop-enabled species
  /pet info species_id            — full species card with image
  /pet buy species_id [nickname]  — deduct coin, create UserPet
  /pet list                       — your owned pets
  /pet active species_id          — set the active pet
  /pet rename species_id nickname — rename
  /pet status [user]              — show stats + image of active pet
  /pet feed | water | play |
      clean | rest | train        — care actions on the active pet

Background
  Hourly decay task — for every active UserPet across all guilds,
  decrement hunger/thirst/cleanliness/mood per src/pets/care.py.
  Inactive pets stay frozen (no decay) so a user with many pets
  can switch around without losing care on the inactive ones.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from ..db.models import (
    PetAsset,
    PetGuildConfig,
    PetMissionProgress,
    PetSpecies,
    UserPet,
)
from ..economy.service import (
    InsufficientBalanceError,
    REASON_PET_BUY, REASON_PET_FEED, REASON_PET_MISSION, REASON_PET_PASSIVE,
    REASON_PET_REFUND, REASON_PET_RENAME, REASON_PET_WATER,
    WalletService,
)
from ..pets import care, missions as pet_missions
from ..pets.resolver import resolve_pet_image, resolve_shop_image
from ..pets.storage import PetStorage

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.pet")


_VISUAL_STATE_LABEL: dict[str, str] = {
    "sick": "🩺 sick",
    "hungry": "🍖 hungry",
    "thirsty": "💧 thirsty",
    "dirty": "🧼 dirty",
    "sleepy": "😴 sleepy",
    "sad": "😢 sad",
    "lonely": "💔 lonely",
    "happy": "😊 happy",
    "normal": "😌 normal",
}


# Per-action cooldown (seconds). Feed/water are limited by coin cost
# instead of a timer. Configurable via /pet-admin setup later if needed;
# hardcoded for now to keep the schema lean.
ACTION_COOLDOWNS_SECONDS: dict[str, int] = {
    "play": 5 * 60,
    "clean": 10 * 60,
    "rest": 15 * 60,
    "train": 30 * 60,
}

# Mapping from action name → (UserPet cooldown column name, care.do_X fn).
# Cooldown column is None when the action doesn't track timestamps.
_ACTION_TABLE: dict[str, tuple[str | None, str]] = {
    "feed":  (None,            "feed"),
    "water": (None,            "drink"),
    "play":  ("last_play_at",  "play"),
    "clean": ("last_clean_at", "clean"),
    "rest":  ("last_rest_at",  "rest"),
    "train": ("last_train_at", "train"),
}


def _file_from_asset(asset: PetAsset) -> tuple[discord.File | None, str | None]:
    """Build a discord.File handle from a PetAsset row. Returns
    (file, attachment_url) — pass attachment_url to embed.set_image."""
    if asset is None:
        return None, None
    path = Path(asset.storage_path)
    if not path.exists():
        return None, None
    # Sanitize filename — discord rejects exotic chars in attachment refs.
    safe = path.name
    return discord.File(str(path), filename=safe), f"attachment://{safe}"


def _format_bar(value: int, width: int = 10) -> str:
    """Tiny ASCII progress bar for stats."""
    filled = max(0, min(width, value * width // 100))
    return "█" * filled + "░" * (width - filled)


# =============================================================================
# Pet House — persistent embed + ephemeral action panel
# =============================================================================


def _build_pet_house_embed(server_name: str) -> discord.Embed:
    """The persistent card admins post via /pet-admin pet-house post.
    server_name is interpolated into the greeting line — usually the
    guild.name, but owners can override via /pet-admin pet-house
    setup-name (e.g. for a server with a long Discord name they want
    shortened in this embed)."""
    embed = discord.Embed(
        title="🏠 Pet House",
        description=(
            f"Welcome to the **Pet House** of **{server_name}**!\n\n"
            "🐾 Press **My pets** to open the care panel for your active "
            "pet — feed, water, play, clean, rest, train, "
            "and **share** your pet with others.\n\n"
            "🛒 Press **Pet Shop** to browse pets you can buy."
        ),
        color=discord.Color.from_rgb(155, 89, 182),
    )
    embed.set_footer(text="Pet House uses an ephemeral panel — only you see it.")
    return embed


class PetHouseButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"pet:house:(?P<guild_id>\d+)",
):
    """The single button on the persistent Pet House card. Custom_id
    carries guild_id so the same class handles every server's house."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"pet:house:{guild_id}",
                label="My pets",
                emoji="🐾",
                style=discord.ButtonStyle.success,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Pet" = interaction.client.get_cog("Pet")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_pet_house_open(interaction)


class PetShopButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"pet:shop:(?P<guild_id>\d+)",
):
    """Quick-access shop button on the Pet House card. Opens an
    ephemeral with the same content as /pet shop so users don't have
    to type the slash command."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"pet:shop:{guild_id}",
                label="Pet Shop",
                emoji="🛒",
                style=discord.ButtonStyle.secondary,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Pet" = interaction.client.get_cog("Pet")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_pet_shop_open(interaction)


def _build_pet_house_view(guild_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(PetHouseButton(guild_id))
    v.add_item(PetShopButton(guild_id))
    return v


class _PetActionView(discord.ui.View):
    """Ephemeral panel with care + flex buttons.

    Layout
      row 0: feed / water / play / clean       care actions
      row 1: rest / train / share / close      care + meta
      row 2: 📋 Mission / 👁️ Show in Info   progression UI

    The Info button is added programmatically so its label can reflect
    the current show_in_info flag (active = green ✅, inactive = grey
    ⬜). Every other button stays a class-level decorator so the
    persistent callbacks bind once at import time.
    """

    def __init__(
        self,
        cog: "Pet",
        pet_id: int,
        owner_id: int,
        *,
        show_in_info: bool = False,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.pet_id = pet_id
        self.owner_id = owner_id
        self.show_in_info = show_in_info
        # Last attachment filename shown on this panel. When the next
        # edit resolves to the same asset filename we skip the
        # discord.File upload entirely (Discord keeps the existing
        # attachment when we omit `attachments=`), saving ~200-400ms
        # of upload latency per care click.
        self._last_image_filename: str | None = None
        # Info toggle — added programmatically after class items so it
        # lands between Share (decorated, row 2) and Close (also
        # programmatic below). Discord renders class-decorated items
        # first, then add_item appends in call order — so:
        #   Share (decorated row 2) → Info (this) → Close (below)
        info_btn = discord.ui.Button(
            label="Show Info" if show_in_info else "Show in Info",
            emoji="✅" if show_in_info else "🔘",
            style=(
                discord.ButtonStyle.success
                if show_in_info else discord.ButtonStyle.secondary
            ),
            row=2,
        )
        info_btn.callback = self._info_toggle_callback  # type: ignore[assignment]
        self.add_item(info_btn)
        # Help between Info and Close — explains how to raise Bond
        # (the stat that gates the 💔 lonely → 😌 normal transition).
        help_btn = discord.ui.Button(
            label="Help", emoji="❓",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        help_btn.callback = self._help_callback  # type: ignore[assignment]
        self.add_item(help_btn)
        close_btn = discord.ui.Button(
            label="Close", emoji="✖️",
            style=discord.ButtonStyle.danger,
            row=2,
        )
        close_btn.callback = self._close_callback  # type: ignore[assignment]
        self.add_item(close_btn)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This Pet House panel belongs to someone else.",
                ephemeral=True,
            )
            return False
        return True

    async def _info_toggle_callback(
        self, interaction: discord.Interaction,
    ) -> None:
        await self.cog._house_info_toggle(interaction, self)

    async def _help_callback(
        self, interaction: discord.Interaction,
    ) -> None:
        await self.cog._house_help(interaction)

    async def _close_callback(
        self, interaction: discord.Interaction,
    ) -> None:
        try:
            await interaction.response.edit_message(
                content="_(Pet House panel closed.)_",
                embed=None, view=None, attachments=[],
            )
        except discord.HTTPException:
            pass

    # row 0 — care actions
    @discord.ui.button(
        label="Feed", emoji="🍖",
        style=discord.ButtonStyle.primary, row=0,
    )
    async def btn_feed(self, interaction, _button) -> None:
        await self.cog._house_action(interaction, self, "feed", "feed")

    @discord.ui.button(
        label="Water", emoji="💧",
        style=discord.ButtonStyle.primary, row=0,
    )
    async def btn_water(self, interaction, _button) -> None:
        await self.cog._house_action(interaction, self, "water", "drink")

    @discord.ui.button(
        label="Play", emoji="🎾",
        style=discord.ButtonStyle.primary, row=0,
    )
    async def btn_play(self, interaction, _button) -> None:
        await self.cog._house_action(interaction, self, "play", "play")

    @discord.ui.button(
        label="Clean", emoji="🧼",
        style=discord.ButtonStyle.primary, row=0,
    )
    async def btn_clean(self, interaction, _button) -> None:
        await self.cog._house_action(interaction, self, "clean", "clean")

    # row 1 — rest + train + mission + rename
    @discord.ui.button(
        label="Rest", emoji="💤",
        style=discord.ButtonStyle.primary, row=1,
    )
    async def btn_rest(self, interaction, _button) -> None:
        await self.cog._house_action(interaction, self, "rest", "rest")

    @discord.ui.button(
        label="Train", emoji="💪",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_train(self, interaction, _button) -> None:
        await self.cog._house_action(interaction, self, "train", "train")

    @discord.ui.button(
        label="Mission", emoji="📋",
        style=discord.ButtonStyle.primary, row=1,
    )
    async def btn_mission(self, interaction, _button) -> None:
        await self.cog._house_mission_open(interaction, self)

    @discord.ui.button(
        label="Rename", emoji="✏️",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_rename(self, interaction, _button) -> None:
        await self.cog._house_rename_open(interaction, self)

    # row 2 — share (decorator), info toggle (programmatic in __init__),
    # close (programmatic in __init__ so it sits AFTER info). Discord
    # renders class-decorated items first then add_item appends, so
    # making close programmatic is how we get the Share → Info → Close
    # order in row 2 with Info sandwiched between them.
    @discord.ui.button(
        label="Share", emoji="📤",
        style=discord.ButtonStyle.success, row=2,
    )
    async def btn_share(self, interaction, _button) -> None:
        await self.cog._house_share(interaction, self)


class _PetRenameModal(discord.ui.Modal):
    """Nickname-only modal opened by the Pet House Rename button.
    The Pet cog charges pet_rename_cost (set via /pet-admin setup)
    on submit; on insufficient balance the modal returns an error
    instead of partially applying the rename."""

    nickname = discord.ui.TextInput(
        label="New pet name",
        placeholder="1-64 characters",
        min_length=1,
        max_length=64,
        required=True,
    )

    def __init__(self, cog: "Pet", view: "_PetActionView") -> None:
        super().__init__(title="Rename pet")
        self.cog = cog
        self.parent_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_name = str(self.nickname.value).strip()
        if not new_name:
            await interaction.response.send_message(
                "❌ New name can't be empty.", ephemeral=True,
            )
            return
        await self.cog._house_rename_submit(
            interaction, self.parent_view, new_name,
        )


class _PetMissionView(discord.ui.View):
    """One ephemeral listing daily + weekly missions. Each ready
    mission (progress >= target AND not yet claimed) gets a Claim
    button. Once claimed the button drops off via refresh. A single
    Close button dismisses the ephemeral."""

    def __init__(
        self,
        cog: "Pet",
        owner_id: int,
        state: list[dict],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.state = state
        # Per-mission Claim buttons. Up to 6 missions (4 daily + 2
        # weekly) all-ready × 5 buttons-per-row caps at row 1.
        ready = [s for s in state if s["ready"] and not s["claimed"]]
        for idx, st in enumerate(ready[:10]):
            btn = discord.ui.Button(
                label=f"Claim {st['icon']}",
                style=discord.ButtonStyle.success,
                row=idx // 5,
            )
            btn.callback = self._make_claim(st["key"])  # type: ignore[assignment]
            self.add_item(btn)
        close = discord.ui.Button(
            label="Close", emoji="✖️",
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        close.callback = self._close  # type: ignore[assignment]
        self.add_item(close)

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This mission board belongs to someone else.", ephemeral=True,
            )
            return False
        return True

    def _make_claim(self, mission_key: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog._house_mission_claim(interaction, self, mission_key)
        return _cb

    async def _close(self, interaction: discord.Interaction) -> None:
        # Drop the cached reference so future care actions don't try
        # to edit this now-emptied ephemeral.
        self.cog._mission_messages.pop(
            (interaction.guild_id, self.owner_id), None,
        )
        try:
            await interaction.response.edit_message(
                content="_(Mission board closed.)_",
                embed=None, view=None, attachments=[],
            )
        except discord.HTTPException:
            pass


# =============================================================================
# Pet Shop browser — paginated card per species, evolution preview, buy modal
# =============================================================================


class _BuyPetModal(discord.ui.Modal):
    """Nickname prompt that fires when a user clicks Mua in the shop
    browser and has enough coin. Submitting completes the purchase via
    Pet._purchase_species. Empty nickname = no override (species name)."""

    nickname = discord.ui.TextInput(
        label="Nickname for your pet (optional)",
        placeholder="Leave blank → use the species name",
        max_length=64,
        required=False,
    )

    def __init__(self, cog: "Pet", species: PetSpecies) -> None:
        # Modal title is limited to 45 chars; clip the species display name.
        super().__init__(title=f"Mua {species.display_name}"[:45])
        self.cog = cog
        self.species = species

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        raw = str(self.nickname.value) if self.nickname.value else ""
        nick = raw.strip() or None
        await self.cog._purchase_species(interaction, self.species, nick)


class _ShopSelect(discord.ui.Select):
    """Dropdown listing every shop-enabled species (up to 25). Selecting
    jumps the browser directly to that pet without paging through Prev/
    Next. The default option matches the view's current_idx so the user
    sees their current position highlighted."""

    def __init__(
        self, species_list: list[PetSpecies], current_idx: int,
    ) -> None:
        options: list[discord.SelectOption] = []
        # Discord Select cap is 25 — same as the shop browser's hard limit.
        for i, sp in enumerate(species_list[:25]):
            label = f"{sp.display_name} ({sp.rarity})"
            desc = f"🪙 {sp.price:,} • {sp.species_id}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(i),
                    description=desc[:100],
                    default=(i == current_idx),
                )
            )
        super().__init__(
            placeholder="Pick a pet to view…",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "_ShopBrowserView" = self.view  # type: ignore[assignment]
        view.current_idx = int(self.values[0])
        await view.cog._refresh_shop_browser(interaction, view)


class _ShopBrowserView(discord.ui.View):
    """Ephemeral paginated shop view. One species per page:

      row 0: species dropdown
      row 1: ◀ Prev · Next ▶ · 🦋 View Evolution · 🛒 Buy · ✖️ Close

    Buy opens a nickname modal (after balance pre-check). View Evolution
    spins up a side ephemeral with 4 stage previews using each stage's
    level_up_effect image."""

    def __init__(
        self,
        cog: "Pet",
        species_list: list[PetSpecies],
        current_idx: int,
        owner_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.species_list = species_list
        self.current_idx = current_idx
        self.owner_id = owner_id
        # Select is rebuilt each refresh so the `default` highlight stays
        # in sync with current_idx.
        self.add_item(_ShopSelect(species_list, current_idx))

    async def interaction_check(
        self, interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This Pet Shop belongs to someone else.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Prev", emoji="◀️",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_prev(self, interaction, _btn) -> None:
        self.current_idx = (self.current_idx - 1) % len(self.species_list)
        await self.cog._refresh_shop_browser(interaction, self)

    @discord.ui.button(
        label="Sau", emoji="▶️",
        style=discord.ButtonStyle.secondary, row=1,
    )
    async def btn_next(self, interaction, _btn) -> None:
        self.current_idx = (self.current_idx + 1) % len(self.species_list)
        await self.cog._refresh_shop_browser(interaction, self)

    @discord.ui.button(
        label="View Evolution", emoji="🦋",
        style=discord.ButtonStyle.primary, row=1,
    )
    async def btn_evolution(self, interaction, _btn) -> None:
        sp = self.species_list[self.current_idx]
        await self.cog._show_evolution(interaction, sp)

    @discord.ui.button(
        label="Mua", emoji="🛒",
        style=discord.ButtonStyle.success, row=1,
    )
    async def btn_buy(self, interaction, _btn) -> None:
        sp = self.species_list[self.current_idx]
        await self.cog._initiate_buy_flow(interaction, sp)

    @discord.ui.button(
        label="Close", emoji="✖️",
        style=discord.ButtonStyle.danger, row=1,
    )
    async def btn_close(self, interaction, _btn) -> None:
        try:
            await interaction.response.edit_message(
                content="_(Pet Shop closed.)_",
                embed=None, view=None, attachments=[],
            )
        except discord.HTTPException:
            pass


# =============================================================================
# Cog
# =============================================================================


class Pet(commands.Cog, name="Pet"):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        self.storage = PetStorage()
        self.wallet = WalletService(bot)
        self._decay_task: asyncio.Task | None = None
        # Per (guild_id, user_id) → the live Pet Mission ephemeral.
        # Captured when /house_mission_open sends it and edited from
        # _refresh_mission_panel_if_open whenever the user does a
        # care action (whether via the Pet House panel buttons or the
        # /pet slash commands). Cleared on Close click or when a
        # follow-up edit fails (token expired / message dismissed by
        # the user). Memory cost is one WebhookMessage per active
        # session; bounded by online users with the board open.
        self._mission_messages: dict[
            tuple[int, int], discord.WebhookMessage,
        ] = {}

        # --- Hot-path TTL caches ------------------------------------
        # Pet House button clicks used to fire 5-7 sequential DB
        # round-trips per action (species, pet-cfg, coin-label,
        # asset lookup, wallet, apply, image resolver). The pet
        # rows change every action, but the ambient lookups
        # (species metadata, guild config, coin label, resolved
        # asset row) rarely do. Memoize those with a short TTL so
        # subsequent clicks within the same panel session hit
        # memory rather than Postgres.
        #
        # Each cache stores (expiry_epoch_seconds, value). Lookups
        # honor the TTL, misses fall through to a fresh DB fetch
        # and repopulate. On admin write (species-config, pet
        # admin setup) the caller should call
        # invalidate_pet_ambient_caches(guild_id) to force a
        # refresh — the impact of a stale entry for 30-300s is
        # only cosmetic (wrong coin name in a summary line etc.).
        self._species_cache: dict[
            tuple[int, str], tuple[float, PetSpecies | None]
        ] = {}
        self._pet_cfg_cache: dict[
            int, tuple[float, PetGuildConfig | None]
        ] = {}
        self._coin_label_cache: dict[
            int, tuple[float, tuple[str, str]]
        ] = {}
        # (guild_id, species_id, stage, visual_state, action) → PetAsset
        # Resolved from src.pets.resolver.resolve_pet_image; the
        # storage_path on disk shouldn't change once assets are
        # imported, so 5-minute TTL is safe.
        self._pet_asset_cache: dict[
            tuple[int, str, str, str, str | None],
            tuple[float, "PetAsset | None"],
        ] = {}

    pet = app_commands.Group(
        name="pet",
        description="Pet system — buy, care, track",
        guild_only=True,
    )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._decay_task is None or self._decay_task.done():
            self._decay_task = asyncio.create_task(self._hourly_decay_loop())

    # ---- helpers ----

    async def _fetch_species(
        self, guild_id: int, species_id: str,
    ) -> PetSpecies | None:
        # 60s TTL cache — species metadata (display_name, rarity,
        # description, desktop_asset_id) is admin-managed and rarely
        # changes mid-play. Any change routes through /pet-admin
        # species-config which invalidates via
        # invalidate_pet_ambient_caches(guild_id).
        key = (guild_id, species_id)
        now = time.monotonic()
        hit = self._species_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(PetSpecies).where(
                    PetSpecies.guild_id == guild_id,
                    PetSpecies.species_id == species_id,
                )
            )
        self._species_cache[key] = (now + 60.0, row)
        return row

    async def _fetch_user_pets(
        self, guild_id: int, user_id: int,
    ) -> list[UserPet]:
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(UserPet).where(
                    UserPet.guild_id == guild_id,
                    UserPet.user_id == user_id,
                ).order_by(UserPet.created_at)
            )
            return list(rows.scalars().all())

    async def _fetch_active_pet(
        self, guild_id: int, user_id: int,
    ) -> UserPet | None:
        async with self.bot.db.session() as s:
            return await s.scalar(
                select(UserPet).where(
                    UserPet.guild_id == guild_id,
                    UserPet.user_id == user_id,
                    UserPet.is_active.is_(True),
                )
            )

    async def _coin_label_parts(self, guild_id: int) -> tuple[str, str]:
        # 5-minute TTL cache. This is fetched inside every care action
        # summary + the mission board + several embeds; the underlying
        # GuildEconomyConfig row basically never changes at play time.
        now = time.monotonic()
        hit = self._coin_label_cache.get(guild_id)
        if hit is not None and hit[0] > now:
            return hit[1]
        from ..db.models import GuildEconomyConfig
        async with self.bot.db.session() as s:
            ecfg = await s.get(GuildEconomyConfig, guild_id)
        pair = ("Coin", "🪙") if ecfg is None else (
            ecfg.coin_name, (ecfg.coin_icon or "🪙")
        )
        self._coin_label_cache[guild_id] = (now + 300.0, pair)
        return pair

    # =========================================================================
    # /pet shop
    # =========================================================================

    @pet.command(name="shop", description="Browse & buy pets (image browser + Buy + Evolution)")
    async def pet_shop(self, interaction: discord.Interaction) -> None:
        await self._open_shop_browser(interaction)

    async def _open_shop_browser(
        self, interaction: discord.Interaction,
    ) -> None:
        """Shared open path for /pet shop and the Pet House Shop button.
        Loads every shop-enabled species, then sends the first card as
        an ephemeral with _ShopBrowserView attached. The view paginates
        in place; Mua opens the nickname modal."""
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(PetSpecies).where(
                    PetSpecies.guild_id == interaction.guild_id,
                    PetSpecies.is_shop_enabled.is_(True),
                    PetSpecies.is_active.is_(True),
                ).order_by(PetSpecies.price, PetSpecies.species_id)
            )
            species_list = list(rows.scalars().all())
        if not species_list:
            await interaction.followup.send(
                "_No pets available in the shop. The server owner needs to "
                "run `/pet-admin species-enable` to enable one._",
                ephemeral=True,
            )
            return
        embed, file = await self._build_shop_card_embed(
            interaction.guild_id, species_list[0], 0, len(species_list),
        )
        view = _ShopBrowserView(
            self, species_list, 0, interaction.user.id,
        )
        if file is not None:
            await interaction.followup.send(
                embed=embed, view=view, file=file, ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=embed, view=view, ephemeral=True,
            )

    async def _build_shop_card_embed(
        self,
        guild_id: int,
        sp: PetSpecies,
        idx: int,
        total: int,
    ) -> tuple[discord.Embed, discord.File | None]:
        """One species card. Image priority falls through resolve_shop_image
        which already prefers icon/shop (i.e. ***_coin_shop_icon.png) per
        spec §23."""
        coin_name, coin_icon = await self._coin_label_parts(guild_id)
        embed = discord.Embed(
            title=f"🛒 {sp.display_name}",
            description=sp.concept or "_(no concept)_",
            color=discord.Color.from_rgb(155, 89, 182),
        )
        embed.add_field(
            name="Species ID", value=f"`{sp.species_id}`", inline=True,
        )
        embed.add_field(name="Rarity", value=sp.rarity, inline=True)
        embed.add_field(
            name="Price",
            value=f"**{sp.price:,}** {coin_icon} {coin_name}",
            inline=True,
        )
        if sp.description:
            embed.add_field(
                name="Description", value=sp.description[:1024], inline=False,
            )
        embed.add_field(
            name="🦋 Evolution",
            value=(
                f"🍼 Baby (L1) → 🐤 Teen (L{care.STAGE_MIN_LEVEL.get('teen', 10)}) "
                f"→ 🐲 Adult (L{care.STAGE_MIN_LEVEL.get('adult', 25)}) "
                f"→ ✨ Mystic (L{care.STAGE_MIN_LEVEL.get('mystic', 40)})"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"Pet {idx + 1}/{total} • Press 🦋 View Evolution to see "
                f"images of all 4 stages • Press 🛒 Buy to nickname & purchase"
            )
        )
        async with self.bot.db.session() as s:
            asset = await resolve_shop_image(
                s, guild_id=guild_id, species_id=sp.species_id,
            )
        f, url = _file_from_asset(asset) if asset else (None, None)
        if f and url:
            embed.set_image(url=url)
        return embed, f

    async def _refresh_shop_browser(
        self,
        interaction: discord.Interaction,
        view: _ShopBrowserView,
    ) -> None:
        """Edit the ephemeral with the species at view.current_idx.
        Rebuilds the view so the Select's `default` highlight stays in
        sync with current_idx. Deferred for the same 3s-deadline reason
        as _house_action: _build_shop_card_embed opens a DB session +
        resolves the shop image."""
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        sp = view.species_list[view.current_idx]
        embed, file = await self._build_shop_card_embed(
            interaction.guild_id, sp,
            view.current_idx, len(view.species_list),
        )
        new_view = _ShopBrowserView(
            self, view.species_list, view.current_idx, view.owner_id,
        )
        attachments = [file] if file is not None else []
        try:
            await interaction.edit_original_response(
                embed=embed, view=new_view, attachments=attachments,
            )
        except discord.HTTPException as e:
            log.warning("pet shop browser refresh failed: %s", e)

    async def _show_evolution(
        self,
        interaction: discord.Interaction,
        sp: PetSpecies,
    ) -> None:
        """Spin up a side ephemeral with 1 embed per stage, each showing
        the species' level_up_effect image at that stage. Falls back to
        any image the resolver can find when level_up is missing."""
        await interaction.response.defer(ephemeral=True)
        stages: tuple[tuple[str, str], ...] = (
            ("baby",   "🍼 Baby"),
            ("teen",   "🐤 Teen"),
            ("adult",  "🐲 Adult"),
            ("mystic", "✨ Mystic"),
        )
        embeds: list[discord.Embed] = [
            discord.Embed(
                title=f"🦋 Evolution of {sp.display_name}",
                description=(
                    "The 4 evolution stages of this species. "
                    "Care for your pet to level up and evolve through each stage!"
                ),
                color=discord.Color.gold(),
            )
        ]
        files: list[discord.File] = []
        async with self.bot.db.session() as s:
            for stage_id, label in stages:
                min_lvl = (
                    1 if stage_id == "baby"
                    else care.STAGE_MIN_LEVEL.get(stage_id, 0)
                )
                e = discord.Embed(
                    title=f"{label} — Level {min_lvl}+",
                    color=discord.Color.from_rgb(155, 89, 182),
                )
                asset = await resolve_pet_image(
                    s, guild_id=interaction.guild_id,
                    species_id=sp.species_id, stage=stage_id,
                    action="level_up",
                )
                if asset is not None:
                    path = Path(asset.storage_path)
                    if path.exists():
                        # Same source filename can repeat across stages —
                        # namespace by stage so Discord doesn't dedupe.
                        safe = f"{stage_id}_{path.name}"
                        files.append(discord.File(str(path), filename=safe))
                        e.set_image(url=f"attachment://{safe}")
                    else:
                        e.description = "_(image file missing on volume)_"
                else:
                    e.description = "_(no level_up_effect image imported for this stage)_"
                embeds.append(e)
        # Discord caps at 10 embeds / 10 attachments per message — we use
        # 5 embeds and at most 4 files, well under both limits.
        await interaction.followup.send(
            embeds=embeds, files=files, ephemeral=True,
        )

    async def _initiate_buy_flow(
        self,
        interaction: discord.Interaction,
        sp: PetSpecies,
    ) -> None:
        """Pre-flight a buy from the shop browser's Mua button:
          1. Species still shop-enabled?
          2. User doesn't already own this species?
          3. Wallet has enough coin?
        Pass → open the nickname modal. Fail → ephemeral error. We
        cannot defer here because send_modal requires an unused
        response."""
        if not sp.is_shop_enabled or not sp.is_active:
            await interaction.response.send_message(
                f"❌ Species `{sp.species_id}` is not available to buy.",
                ephemeral=True,
            )
            return
        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(UserPet).where(
                    UserPet.guild_id == interaction.guild_id,
                    UserPet.user_id == interaction.user.id,
                    UserPet.species_id == sp.species_id,
                )
            )
        if existing is not None:
            await interaction.response.send_message(
                f"❌ You already own pet `{sp.species_id}` "
                f"(nickname: {existing.nickname or '—'}). Each species can only be bought once.",
                ephemeral=True,
            )
            return
        wallet = await self.wallet.get_or_create_wallet(
            interaction.guild_id, interaction.user.id,
        )
        coin_name, coin_icon = await self._coin_label_parts(
            interaction.guild_id,
        )
        if wallet.balance < sp.price:
            await interaction.response.send_message(
                f"❌ Not enough coins. Need **{sp.price:,}** {coin_icon}, "
                f"you have **{wallet.balance:,}** {coin_icon} {coin_name}.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(_BuyPetModal(self, sp))

    async def _purchase_species(
        self,
        interaction: discord.Interaction,
        sp: PetSpecies,
        nickname: str | None,
    ) -> None:
        """Actual buy: re-check ownership (race protection), deduct
        coin, create UserPet, send confirmation. Assumes the interaction
        has already been deferred (either via /pet buy or modal submit).
        Always replies via followup."""
        species_id = sp.species_id
        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(UserPet).where(
                    UserPet.guild_id == interaction.guild_id,
                    UserPet.user_id == interaction.user.id,
                    UserPet.species_id == species_id,
                )
            )
        if existing is not None:
            await interaction.followup.send(
                f"❌ You already own pet `{species_id}`.", ephemeral=True,
            )
            return
        try:
            await self.wallet.adjust(
                interaction.guild_id, interaction.user.id, -sp.price,
                REASON_PET_BUY,
                description=f"Mua pet {species_id}",
                metadata={"species_id": species_id},
            )
        except InsufficientBalanceError as e:
            await interaction.followup.send(
                f"❌ Not enough coins: {e}", ephemeral=True,
            )
            return
        existing_pets = await self._fetch_user_pets(
            interaction.guild_id, interaction.user.id,
        )
        is_first = not existing_pets
        nickname_clean = nickname.strip() if nickname else None
        if nickname_clean == "":
            nickname_clean = None
        async with self.bot.db.session() as s:
            new_pet = UserPet(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                species_id=species_id,
                nickname=nickname_clean,
                level=1, exp=0,
                hunger=100, thirst=100, mood=100, energy=100,
                cleanliness=100, bond=0, health=100,
                stage="baby",
                is_active=is_first,
            )
            s.add(new_pet)
            await s.commit()
            await s.refresh(new_pet)

        coin_name, coin_icon = await self._coin_label_parts(
            interaction.guild_id,
        )
        embed = discord.Embed(
            title=f"🎉 Bought {sp.display_name}!",
            description=(
                f"💰 Deducted **{sp.price:,}** {coin_icon} {coin_name}\n"
                f"🍼 Stage: **baby** (Level 1) — care for it to evolve\n"
                f"🦋 Evolution: Teen "
                f"(L{care.STAGE_MIN_LEVEL.get('teen', 10)}) → Adult "
                f"(L{care.STAGE_MIN_LEVEL.get('adult', 25)}) → Mystic "
                f"(L{care.STAGE_MIN_LEVEL.get('mystic', 40)})\n"
                + (f"🪪 Nickname: **{new_pet.nickname}**\n"
                   if new_pet.nickname else "")
                + ("✅ This pet is automatically set as **active**.\n"
                   if is_first else
                   f"ℹ️ Use `/pet active species_id:{species_id}` to make it "
                   "your active pet.\n")
                + "\n_Use the Pet House panel or `/pet feed | water | play "
                "| clean | rest | train` to care for your pet._"
            ),
            color=discord.Color.green(),
        )
        async with self.bot.db.session() as s:
            asset = await resolve_pet_image(
                s, guild_id=interaction.guild_id,
                species_id=species_id, stage="baby",
                visual_state="happy",
            )
        f, url = _file_from_asset(asset) if asset else (None, None)
        if f and url:
            embed.set_image(url=url)
            await interaction.followup.send(
                embed=embed, file=f, ephemeral=True,
            )
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    # =========================================================================
    # /pet info
    # =========================================================================

    @pet.command(name="info", description="View details of one species (with image)")
    @app_commands.describe(species_id="ID species (snake_case)")
    async def pet_info(
        self, interaction: discord.Interaction, species_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        species_id = species_id.strip().lower()
        sp = await self._fetch_species(interaction.guild_id, species_id)
        if sp is None or not sp.is_active:
            await interaction.followup.send(
                f"❌ Species `{species_id}` not found.",
                ephemeral=True,
            )
            return
        coin_name, coin_icon = await self._coin_label_parts(interaction.guild_id)
        embed = discord.Embed(
            title=f"🐾 {sp.display_name}",
            description=sp.concept or "_(no concept)_",
            color=discord.Color.from_rgb(155, 89, 182),
        )
        embed.add_field(name="Species ID", value=f"`{sp.species_id}`", inline=True)
        embed.add_field(name="Rarity", value=sp.rarity, inline=True)
        embed.add_field(
            name="Price",
            value=f"{sp.price:,} {coin_icon} {coin_name}", inline=True,
        )
        embed.add_field(
            name="Shop", value="✅ enabled" if sp.is_shop_enabled else "❌ disabled",
            inline=True,
        )
        embed.add_field(
            name="Assets", value=f"{sp.asset_count} ảnh / {sp.stage_count} stages",
            inline=True,
        )
        if sp.description:
            embed.add_field(name="Description", value=sp.description[:1024], inline=False)
        embed.add_field(
            name="🦋 Evolution",
            value=(
                "🍼 **Baby** _(buy from shop, Level 1)_\n"
                "↓ Level 10\n"
                "🐤 **Teen**\n"
                "↓ Level 25\n"
                "🐲 **Adult**\n"
                "↓ Level 40\n"
                "✨ **Mystic**\n\n"
                "_Care actions (feed/water/play/clean/rest) grant small EXP • "
                "train grants larger EXP._"
            ),
            inline=False,
        )
        # Image (portrait or shop icon)
        async with self.bot.db.session() as s:
            asset = await resolve_shop_image(
                s, guild_id=interaction.guild_id, species_id=sp.species_id,
            )
        f, url = _file_from_asset(asset) if asset else (None, None)
        if f and url:
            embed.set_image(url=url)
            await interaction.followup.send(embed=embed, file=f, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    # =========================================================================
    # /pet buy
    # =========================================================================

    @pet.command(name="buy", description="Buy a pet with coins (legacy slash — Pet Shop browser also works)")
    @app_commands.describe(
        species_id="Species ID to buy",
        nickname="Personal nickname (optional)",
    )
    async def pet_buy(
        self,
        interaction: discord.Interaction,
        species_id: str,
        nickname: app_commands.Range[str, 1, 64] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        species_id = species_id.strip().lower()
        sp = await self._fetch_species(interaction.guild_id, species_id)
        if sp is None or not sp.is_active:
            await interaction.followup.send(
                f"❌ Species `{species_id}` not found.", ephemeral=True,
            )
            return
        if not sp.is_shop_enabled:
            await interaction.followup.send(
                f"❌ Species `{species_id}` is not enabled in the shop.",
                ephemeral=True,
            )
            return
        await self._purchase_species(
            interaction, sp, str(nickname) if nickname else None,
        )

    # =========================================================================
    # /pet list
    # =========================================================================

    @pet.command(name="list", description="View the pets you own")
    async def pet_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        pets = await self._fetch_user_pets(
            interaction.guild_id, interaction.user.id,
        )
        if not pets:
            await interaction.followup.send(
                "_You have no pets. Use `/pet shop` then `/pet buy` to purchase one._",
                ephemeral=True,
            )
            return
        # Resolve display names
        species_ids = {p.species_id for p in pets}
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(PetSpecies).where(
                    PetSpecies.guild_id == interaction.guild_id,
                    PetSpecies.species_id.in_(species_ids),
                )
            )
            species_by_id = {sp.species_id: sp for sp in rows.scalars().all()}
        lines = []
        for p in pets:
            sp = species_by_id.get(p.species_id)
            species_name = sp.display_name if sp else p.species_id
            # Once nicknamed, the nickname IS the pet's display name —
            # species label is dropped. species_id (the canonical key)
            # stays so /pet active species_id:<id> still works.
            label = p.nickname if p.nickname else species_name
            mark = "⭐" if p.is_active else "  "
            next_stage, need_level = care.next_stage_and_level(p.stage)
            evo_tag = (
                f" → 🦋 {next_stage} (L{need_level})"
                if next_stage else " ✨"
            )
            lines.append(
                f"{mark} `{p.species_id}` — **{label}**\n"
                f"     Stage: **{p.stage}** • Level **{p.level}** • "
                f"EXP {p.exp}/{care.exp_for_next_level(p.level)}{evo_tag}"
            )
        embed = discord.Embed(
            title=f"🐾 Pet của {interaction.user.display_name} ({len(pets)})",
            description="\n\n".join(lines)[:4000],
            color=discord.Color.from_rgb(155, 89, 182),
        )
        embed.set_footer(text="⭐ = active pet • /pet active species_id:<id> to switch")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # =========================================================================
    # /pet active
    # =========================================================================

    @pet.command(name="active", description="Set a pet as active (your main pet)")
    @app_commands.describe(species_id="Species ID to set as active")
    async def pet_active(
        self, interaction: discord.Interaction, species_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        species_id = species_id.strip().lower()
        async with self.bot.db.session() as s:
            target = await s.scalar(
                select(UserPet).where(
                    UserPet.guild_id == interaction.guild_id,
                    UserPet.user_id == interaction.user.id,
                    UserPet.species_id == species_id,
                )
            )
            if target is None:
                await interaction.followup.send(
                    f"❌ You don't own pet `{species_id}`.",
                    ephemeral=True,
                )
                return
            # Unset other active pets
            others = await s.execute(
                select(UserPet).where(
                    UserPet.guild_id == interaction.guild_id,
                    UserPet.user_id == interaction.user.id,
                    UserPet.is_active.is_(True),
                )
            )
            for p in others.scalars().all():
                p.is_active = False
            target.is_active = True
            await s.commit()
        await interaction.followup.send(
            f"⭐ Set `{species_id}` as your active pet.", ephemeral=True
        )

    # =========================================================================
    # /pet rename
    # =========================================================================

    @pet.command(name="rename", description="Rename a pet")
    @app_commands.describe(
        species_id="Species ID to rename",
        nickname="New name (1-64 chars)",
    )
    async def pet_rename(
        self,
        interaction: discord.Interaction,
        species_id: str,
        nickname: app_commands.Range[str, 1, 64],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        species_id = species_id.strip().lower()
        async with self.bot.db.session() as s:
            pet = await s.scalar(
                select(UserPet).where(
                    UserPet.guild_id == interaction.guild_id,
                    UserPet.user_id == interaction.user.id,
                    UserPet.species_id == species_id,
                )
            )
            if pet is None:
                await interaction.followup.send(
                    f"❌ You don't own pet `{species_id}`.",
                    ephemeral=True,
                )
                return
            pet.nickname = nickname.strip()
            await s.commit()
        await interaction.followup.send(
            f"✏️ Renamed `{species_id}` to **{nickname}**.",
            ephemeral=True,
        )

    # =========================================================================
    # /pet status
    # =========================================================================

    @pet.command(name="status", description="Xem stats + ảnh pet active")
    @app_commands.describe(user="View another user's pet (default: you)")
    async def pet_status(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        target = user or interaction.user
        pet = await self._fetch_active_pet(
            interaction.guild_id, target.id,
        )
        if pet is None:
            await interaction.followup.send(
                f"❌ {target.display_name} has no active pet yet.",
                ephemeral=True,
            )
            return
        embed, file = await self._build_status_embed_with_image(
            interaction.guild_id, pet, target,
        )
        if file is not None:
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    async def _build_status_embed_with_image(
        self,
        guild_id: int,
        pet: UserPet,
        target_user: discord.User | discord.Member,
        *,
        action: str | None = None,
    ) -> tuple[discord.Embed, discord.File | None]:
        """Shared builder for /pet status, Pet House view, and share
        snapshots. Returns (embed, file) — file is None when no image
        could be resolved (volume unmounted, asset missing, etc.)."""
        sp = await self._fetch_species(guild_id, pet.species_id)
        species_name = sp.display_name if sp else pet.species_id
        # Once the owner has nicknamed the pet, the species label is
        # treated as redundant — only the chosen name shows. Pets with
        # no nickname fall back to the species display name.
        name_line = f"**{pet.nickname}**" if pet.nickname else f"**{species_name}**"
        visual_state = care.get_visual_state(pet)
        next_stage, need_level = care.next_stage_and_level(pet.stage)
        if next_stage is not None:
            levels_left = max(0, need_level - pet.level)
            if levels_left == 0:
                evolution_line = (
                    f"🦋 Ready to evolve to **{next_stage}** at the next level!"
                )
            else:
                evolution_line = (
                    f"🦋 Evolves to **{next_stage}** at Level {need_level} "
                    f"({levels_left} levels left)"
                )
        else:
            evolution_line = "✨ Reached the top stage (mystic)"
        embed = discord.Embed(
            title=f"🐾 {name_line}",
            description=(
                f"👤 Chủ: {target_user.mention}\n"
                f"🌱 Stage: **{pet.stage}** • Level **{pet.level}**\n"
                f"EXP: {pet.exp:,} / {care.exp_for_next_level(pet.level):,}\n"
                f"{evolution_line}\n"
                f"State: {_VISUAL_STATE_LABEL.get(visual_state, visual_state)}"
            ),
            color=discord.Color.from_rgb(155, 89, 182),
        )
        for label, value in (
            ("🍖 Hunger", pet.hunger),
            ("💧 Thirst", pet.thirst),
            ("😊 Mood", pet.mood),
            ("⚡ Energy", pet.energy),
            ("🧼 Clean", pet.cleanliness),
            ("🩺 Health", pet.health),
        ):
            embed.add_field(
                name=label, value=f"`{_format_bar(value)}` {value}/100",
                inline=True,
            )
        embed.add_field(name="❤️ Bond", value=f"{pet.bond}/100", inline=True)
        # 5-minute TTL — this is the biggest per-click DB save on Pet
        # House care actions. See _resolve_pet_image_cached.
        asset = await self._resolve_pet_image_cached(
            guild_id=guild_id,
            species_id=pet.species_id, stage=pet.stage,
            visual_state=visual_state, action=action,
        )
        if asset is not None:
            f, url = _file_from_asset(asset)
            if f and url:
                embed.set_image(url=url)
                return embed, f
        return embed, None

    # =========================================================================
    # Care actions
    # =========================================================================

    async def _try_apply_action(
        self,
        guild_id: int, user_id: int,
        db_pet: UserPet,
        action_name: str,
    ) -> tuple[care.ActionResult | None, str | None, int]:
        """Run cooldown + coin checks, apply the action, and update the
        cooldown timestamp on success. Returns (result, error_msg,
        coin_spent). On success the first two are (result, None); on
        failure (None, error_msg). coin_spent reports the amount
        debited from the wallet so the caller can surface it in the
        embed (0 when no charge was applied, or when refunded)."""
        now = datetime.now(timezone.utc)
        cooldown_field, image_action = _ACTION_TABLE[action_name]

        # 1. Cooldown
        cooldown_s = ACTION_COOLDOWNS_SECONDS.get(action_name, 0)
        if cooldown_field and cooldown_s:
            last_at = getattr(db_pet, cooldown_field, None)
            if last_at is not None:
                elapsed = (now - last_at).total_seconds()
                if elapsed < cooldown_s:
                    unlock = last_at + timedelta(seconds=cooldown_s)
                    return None, (
                        f"⏳ Pet is resting after its last `{action_name}`. "
                        f"Try again <t:{int(unlock.timestamp())}:R>."
                    ), 0

        # 2. Primary-stat cap — prevent farming bond / EXP by spamming
        # an action whose target stat is already at 100. Sits BEFORE
        # the coin charge so a rejected feed/water doesn't debit the
        # user's wallet. train has no entry in ACTION_PRIMARY_STAT so
        # it skips this check (it consumes energy/hunger rather than
        # refilling a stat; rate-limited by its 30 min cooldown).
        primary_stat = care.ACTION_PRIMARY_STAT.get(action_name)
        if primary_stat is not None:
            current_val = int(getattr(db_pet, primary_stat, 0))
            if current_val >= 100:
                stat_label = {
                    "hunger": "🍖 Hunger",
                    "thirst": "💧 Thirst",
                    "mood": "😊 Mood",
                    "cleanliness": "🧼 Sạch",
                    "energy": "⚡ Energy",
                }.get(primary_stat, primary_stat)
                return None, (
                    f"✅ Your pet's {stat_label} is full "
                    f"(**{current_val}/100**). No need for another `{action_name}` — "
                    "try a different care action or wait for the stat to drop."
                ), 0

        # 3. Coin cost (feed / water only)
        cost, reason = 0, None
        if action_name in ("feed", "water"):
            cfg = await self._fetch_pet_cfg(guild_id)
            if action_name == "feed":
                cost = cfg.pet_feed_cost if cfg else 10
                reason = REASON_PET_FEED
            else:
                cost = cfg.pet_water_cost if cfg else 5
                reason = REASON_PET_WATER
        if cost > 0 and reason is not None:
            try:
                await self.wallet.adjust(
                    guild_id, user_id, -cost, reason,
                    description=f"Pet {action_name} ({db_pet.species_id})",
                    metadata={"species_id": db_pet.species_id, "pet_id": db_pet.id},
                )
            except InsufficientBalanceError as e:
                return None, (
                    f"❌ Not enough coins. Need **{cost}** — {e}"
                ), 0

        # 4. Apply care action
        fn_map = {
            "feed": care.do_feed, "water": care.do_water,
            "play": care.do_play, "clean": care.do_clean,
            "rest": care.do_rest, "train": care.do_train,
        }
        result = fn_map[action_name](db_pet)
        if result.failed_reason:
            # Refund the coin we just charged so the user isn't penalized
            # for an action the care helper rejected (low energy etc.)
            if cost > 0 and reason is not None:
                try:
                    await self.wallet.adjust(
                        guild_id, user_id, cost, REASON_PET_REFUND,
                        description=f"Refund pet {action_name}",
                        metadata={"species_id": db_pet.species_id},
                    )
                except Exception:
                    log.exception("pet: refund failed")
            return None, f"❌ {result.failed_reason}", 0

        # 5. Record cooldown for next call
        if cooldown_field and cooldown_s:
            setattr(db_pet, cooldown_field, now)
        # 6. Mission progress — care action bumps every mission tied
        # to this action; level_up bumps the weekly milestone. Failures
        # are swallowed so a mission-tracker bug can't sink the care
        # action itself. Auto-refresh the user's Mission ephemeral (if
        # one is open) so the progress bars update live as they care
        # for their pet, no re-open required.
        try:
            await self._increment_action_missions(
                guild_id, user_id, action_name,
            )
            if result.level_up:
                await self._increment_mission(
                    guild_id, user_id, "level_up_3", delta=1,
                )
        except Exception:
            log.exception("pet: mission progress failed")
        try:
            await self._refresh_mission_panel_if_open(guild_id, user_id)
        except Exception:
            log.exception("pet: mission panel auto-refresh failed")
        return result, None, cost

    # =========================================================================
    # Mission system — daily/weekly task progress + claim
    # =========================================================================

    async def _increment_action_missions(
        self, guild_id: int, user_id: int, action_name: str,
    ) -> None:
        """Bump every mission in ACTION_TO_MISSIONS[action_name]. Each
        bump is +1 progress — single-action missions complete in one
        click, multi-action missions accumulate."""
        for mkey in pet_missions.ACTION_TO_MISSIONS.get(action_name, ()):
            await self._increment_mission(guild_id, user_id, mkey, delta=1)

    async def _mission_reset_config(
        self, guild_id: int,
    ) -> tuple[int, int]:
        """Return (tz_offset, reset_hour) from PetGuildConfig so every
        mission period calc reads the same source. Falls back to the
        VN 00:00 default if no config row exists yet."""
        async with self.bot.db.session() as s:
            cfg = await s.get(PetGuildConfig, guild_id)
        if cfg is None:
            return (pet_missions.VN_TZ_OFFSET_HOURS, 0)
        return (int(cfg.pet_mission_tz_offset), int(cfg.pet_mission_reset_hour))

    async def _increment_mission(
        self, guild_id: int, user_id: int, mission_key: str, delta: int = 1,
    ) -> None:
        m = pet_missions.get_mission(mission_key)
        if m is None:
            return
        tz, hr = await self._mission_reset_config(guild_id)
        period = pet_missions.period_for_mission(m, tz, hr)
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(PetMissionProgress).where(
                    PetMissionProgress.guild_id == guild_id,
                    PetMissionProgress.user_id == user_id,
                    PetMissionProgress.mission_key == mission_key,
                    PetMissionProgress.period_start == period,
                )
            )
            if row is None:
                row = PetMissionProgress(
                    guild_id=guild_id, user_id=user_id,
                    mission_key=mission_key, period_start=period,
                    progress=delta,
                )
                s.add(row)
            else:
                # Already claimed → don't keep ticking, but allow
                # progress to reach target visually (no-op increment).
                if row.claimed_at is not None:
                    return
                row.progress = min(row.progress + delta, m.target)
            await s.commit()

    async def _fetch_mission_state(
        self, guild_id: int, user_id: int,
    ) -> list[dict]:
        """Return one dict per mission in display order (dailies then
        weeklies). Each dict carries enough info for both the embed
        renderer and the claim flow:
          key / label / icon / target / reward / period
          progress (capped at target) / ready / claimed
        Missing rows show as progress=0 / ready=False / claimed=False."""
        out: list[dict] = []
        tz, hr = await self._mission_reset_config(guild_id)
        async with self.bot.db.session() as s:
            for m in pet_missions.ALL_MISSIONS:
                period = pet_missions.period_for_mission(m, tz, hr)
                row = await s.scalar(
                    select(PetMissionProgress).where(
                        PetMissionProgress.guild_id == guild_id,
                        PetMissionProgress.user_id == user_id,
                        PetMissionProgress.mission_key == m.key,
                        PetMissionProgress.period_start == period,
                    )
                )
                progress = row.progress if row else 0
                claimed = bool(row and row.claimed_at is not None)
                out.append({
                    "key": m.key,
                    "label": m.label,
                    "icon": m.icon,
                    "target": m.target,
                    "reward": m.reward,
                    "period": m.period,
                    "progress": min(progress, m.target),
                    "ready": progress >= m.target and not claimed,
                    "claimed": claimed,
                })
        return out

    def _build_mission_embed(
        self,
        state: list[dict],
        *,
        coin_icon: str = "🪙",
        coin_name: str = "coin",
        tz_offset: int = 7,
        reset_hour: int = 0,
    ) -> discord.Embed:
        """Embed builder. coin_icon / coin_name come from the guild's
        economy config (/economy setup) so the mission rewards render
        with the server's branded coin emoji + label. tz_offset +
        reset_hour come from PetGuildConfig (/pet-admin setup) so the
        reset-clock blurb matches what the bot actually uses to bucket
        progress."""
        daily = [s for s in state if s["period"] == "daily"]
        weekly = [s for s in state if s["period"] == "weekly"]
        sign = "+" if tz_offset >= 0 else "-"
        tz_label = f"UTC{sign}{abs(tz_offset)}"
        embed = discord.Embed(
            title="📋 Pet Mission",
            description=(
                f"Complete missions to earn {coin_name}. "
                f"**Daily** resets `{reset_hour:02d}:00` ({tz_label}), "
                f"**Weekly** resets Monday `{reset_hour:02d}:00` ({tz_label})."
            ),
            color=discord.Color.from_rgb(155, 89, 182),
        )

        def _render_one(s: dict) -> str:
            if s["claimed"]:
                mark = "☑️"
                trail = "  _(claimed)_"
            elif s["ready"]:
                mark = "✅"
                trail = f"  → press **Claim {s['icon']}** to redeem"
            else:
                mark = "⬜"
                trail = ""
            return (
                f"{mark} {s['icon']} {s['label']}  "
                f"`{s['progress']}/{s['target']}` • "
                f"**{s['reward']}** {coin_icon}{trail}"
            )

        if daily:
            embed.add_field(
                name="🗓️ Daily",
                value="\n".join(_render_one(s) for s in daily),
                inline=False,
            )
        if weekly:
            embed.add_field(
                name="📅 Weekly",
                value="\n".join(_render_one(s) for s in weekly),
                inline=False,
            )
        ready_total = sum(s["reward"] for s in state if s["ready"])
        if ready_total > 0:
            embed.set_footer(
                text=(
                    f"{ready_total} {coin_name} ready to claim — "
                    "press the Claim buttons below."
                ),
            )
        return embed

    async def _house_mission_claim(
        self,
        interaction: discord.Interaction,
        view: _PetMissionView,
        mission_key: str,
    ) -> None:
        """Mark the mission claimed + grant the coin + refresh the
        ephemeral. Idempotent via the claimed_at NULL check inside
        the transaction — a double-click can't double-pay.

        Deferred up front: this handler chains a mission lookup + DB
        row select + wallet adjust + full mission-board rebuild, which
        easily exceeds Discord's 3s deadline on a slow DB link.
        """
        m = pet_missions.get_mission(mission_key)
        if m is None:
            await interaction.response.send_message(
                "❌ Invalid mission.", ephemeral=True,
            )
            return

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

        tz, hr = await self._mission_reset_config(interaction.guild_id)
        period = pet_missions.period_for_mission(m, tz, hr)
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(PetMissionProgress).where(
                    PetMissionProgress.guild_id == interaction.guild_id,
                    PetMissionProgress.user_id == interaction.user.id,
                    PetMissionProgress.mission_key == mission_key,
                    PetMissionProgress.period_start == period,
                )
            )
            if row is None or row.progress < m.target:
                await interaction.followup.send(
                    "❌ Mission not complete yet.", ephemeral=True,
                )
                return
            if row.claimed_at is not None:
                await interaction.followup.send(
                    "_(This mission has already been claimed.)_", ephemeral=True,
                )
                return
            row.claimed_at = datetime.now(timezone.utc)
            await s.commit()

        try:
            await self.wallet.adjust(
                interaction.guild_id, interaction.user.id,
                m.reward, REASON_PET_MISSION,
                description=f"Pet mission claim: {m.label}",
                metadata={"mission_key": mission_key, "period": period},
            )
        except Exception:
            log.exception("pet mission: wallet grant failed")

        # Refresh the mission board in place via response.edit_message
        # so the just-claimed mission flips to ☑️ and its Claim button
        # drops off. Toast goes via followup for confirmation feel.
        state = await self._fetch_mission_state(
            interaction.guild_id, interaction.user.id,
        )
        coin_name, coin_icon = await self._coin_label_parts(
            interaction.guild_id,
        )
        tz, hr = await self._mission_reset_config(interaction.guild_id)
        embed = self._build_mission_embed(
            state, coin_icon=coin_icon, coin_name=coin_name,
            tz_offset=tz, reset_hour=hr,
        )
        new_view = _PetMissionView(self, interaction.user.id, state)
        try:
            await interaction.edit_original_response(
                embed=embed, view=new_view,
            )
        except discord.HTTPException as e:
            log.warning("pet mission: refresh edit failed: %s", e)
        coin_name, coin_icon = await self._coin_label_parts(
            interaction.guild_id,
        )
        try:
            await interaction.followup.send(
                f"🎉 Claim **{m.label}** — +{m.reward} {coin_icon} {coin_name}!",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass

    async def _fetch_pet_cfg(self, guild_id: int) -> PetGuildConfig | None:
        # 60s TTL cache — action coin costs + pet_house channel etc.
        # Owners re-running /pet-admin setup should call
        # invalidate_pet_ambient_caches(guild_id) after their write.
        now = time.monotonic()
        hit = self._pet_cfg_cache.get(guild_id)
        if hit is not None and hit[0] > now:
            return hit[1]
        async with self.bot.db.session() as s:
            row = await s.get(PetGuildConfig, guild_id)
        self._pet_cfg_cache[guild_id] = (now + 60.0, row)
        return row

    def invalidate_pet_ambient_caches(self, guild_id: int) -> None:
        """Drop every cached ambient row (species, cfg, coin, asset)
        for `guild_id`. Called by admin write paths so a config
        change surfaces immediately instead of waiting for the TTL.
        Cheap to call — the caches are tiny."""
        self._pet_cfg_cache.pop(guild_id, None)
        self._coin_label_cache.pop(guild_id, None)
        # Species + asset caches are keyed by tuples that start with
        # guild_id; scan for the match.
        for key in list(self._species_cache):
            if key[0] == guild_id:
                self._species_cache.pop(key, None)
        for key in list(self._pet_asset_cache):
            if key[0] == guild_id:
                self._pet_asset_cache.pop(key, None)

    async def _resolve_pet_image_cached(
        self,
        *,
        guild_id: int,
        species_id: str,
        stage: str,
        visual_state: str,
        action: str | None,
    ) -> "PetAsset | None":
        """5-minute TTL wrapper around src.pets.resolver.resolve_pet_image.

        The resolver joins pet_assets on species+stage+action to pick
        the right image row. That result is stable once assets are
        imported — pet.stage transitions and mood changes rotate
        THROUGH cached entries rather than mutating them. Anywhere an
        admin re-imports assets should call
        invalidate_pet_ambient_caches(guild_id).
        """
        key = (guild_id, species_id, stage, visual_state, action)
        now = time.monotonic()
        hit = self._pet_asset_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        async with self.bot.db.session() as s:
            asset = await resolve_pet_image(
                s,
                guild_id=guild_id,
                species_id=species_id, stage=stage,
                visual_state=visual_state, action=action,
            )
        self._pet_asset_cache[key] = (now + 300.0, asset)
        return asset

    async def _do_action(
        self,
        interaction: discord.Interaction,
        action_name: str,
    ) -> None:
        """Slash-command entry point. Looks up active pet, runs the
        shared limits/effect pipeline, then sends an ephemeral with the
        delta summary + action image."""
        await interaction.response.defer(ephemeral=True)
        pet = await self._fetch_active_pet(
            interaction.guild_id, interaction.user.id,
        )
        if pet is None:
            await interaction.followup.send(
                "❌ You have no active pet. Buy one with `/pet buy` first.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            db_pet = await s.get(UserPet, pet.id)
            if db_pet is None:
                await interaction.followup.send(
                    "Pet no longer exists.", ephemeral=True
                )
                return
            result, err, coin_spent = await self._try_apply_action(
                interaction.guild_id, interaction.user.id,
                db_pet, action_name,
            )
            if err:
                await interaction.followup.send(err, ephemeral=True)
                return
            await s.commit()
            await s.refresh(db_pet)

        # Build summary embed
        coin_name, coin_icon = await self._coin_label_parts(interaction.guild_id)
        delta_lines = []
        if coin_spent > 0:
            delta_lines.append(
                f"💰 Coin: **-{coin_spent}** {coin_icon} _({coin_name})_"
            )
        for field, change in result.delta.items():
            if change == 0:
                continue
            label = {
                "hunger": "🍖 Hunger", "thirst": "💧 Thirst",
                "mood": "😊 Mood", "energy": "⚡ Energy",
                "cleanliness": "🧼 Clean", "bond": "❤️ Bond",
                "health": "🩺 Health",
            }.get(field, field)
            sign = "+" if change > 0 else ""
            delta_lines.append(f"{label}: **{sign}{change}**")
        title_emoji = {
            "feed": "🍖", "water": "💧", "play": "🎾",
            "clean": "🧼", "rest": "💤", "train": "💪",
        }.get(result.action, "🐾")
        title_action = {
            "feed": "Feed", "water": "Water", "play": "Play",
            "clean": "Clean", "rest": "Rest", "train": "Train",
        }.get(result.action, result.action)
        embed = discord.Embed(
            title=f"{title_emoji} {title_action} successful",
            color=discord.Color.green(),
        )
        if delta_lines:
            embed.add_field(
                name="Changes", value="\n".join(delta_lines), inline=False,
            )
        if result.exp_gained:
            embed.add_field(
                name="✨ EXP gained", value=f"+{result.exp_gained}", inline=True,
            )
        if result.level_up:
            embed.add_field(
                name="🆙 Level Up!",
                value=f"Pet leveled up to Level **{db_pet.level}**!",
                inline=False,
            )
        if result.new_stage:
            embed.add_field(
                name="🦋 Evolved!",
                value=f"Pet evolved to stage **{result.new_stage}**!",
                inline=False,
            )

        # Action image
        _, image_action = _ACTION_TABLE[action_name]
        async with self.bot.db.session() as s:
            asset = await resolve_pet_image(
                s,
                guild_id=interaction.guild_id,
                species_id=db_pet.species_id, stage=db_pet.stage,
                action=image_action,
                visual_state=care.get_visual_state(db_pet),
            )
        f, url = _file_from_asset(asset) if asset else (None, None)
        if f and url:
            embed.set_image(url=url)
            await interaction.followup.send(embed=embed, file=f, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @pet.command(name="feed", description="Feed your pet (hunger +30, costs coins)")
    async def pet_feed(self, interaction: discord.Interaction) -> None:
        await self._do_action(interaction, "feed")

    @pet.command(name="water", description="Water your pet (thirst +30, costs coins)")
    async def pet_water(self, interaction: discord.Interaction) -> None:
        await self._do_action(interaction, "water")

    @pet.command(name="play", description="Play (mood +20, 5-min cooldown)")
    async def pet_play(self, interaction: discord.Interaction) -> None:
        await self._do_action(interaction, "play")

    @pet.command(name="clean", description="Bathe (cleanliness +30, 10-min cooldown)")
    async def pet_clean(self, interaction: discord.Interaction) -> None:
        await self._do_action(interaction, "clean")

    @pet.command(name="rest", description="Rest (energy +40, 15-min cooldown)")
    async def pet_rest(self, interaction: discord.Interaction) -> None:
        await self._do_action(interaction, "rest")

    @pet.command(
        name="train",
        description="Training (EXP +15-30, 30-min cooldown)",
    )
    async def pet_train(self, interaction: discord.Interaction) -> None:
        await self._do_action(interaction, "train")

    # =========================================================================
    # Hourly decay loop
    # =========================================================================

    async def _hourly_decay_loop(self) -> None:
        """Once per hour, decay every active pet. Inactive pets stay
        frozen so a user with many pets can rotate without losing stats."""
        log.info("pet: hourly decay loop started")
        while True:
            try:
                now = datetime.now(timezone.utc)
                target = (now + timedelta(hours=1)).replace(
                    minute=5, second=0, microsecond=0
                )
                if target <= now:
                    target += timedelta(hours=1)
                await asyncio.sleep((target - now).total_seconds())
                await self._sweep_decay()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("pet decay loop crashed; retry in 60s")
                await asyncio.sleep(60)

    async def _sweep_decay(self) -> None:
        """Once an hour: decay stats AND grant passive income to every
        active pet. The two passes share the same loaded set so we
        only round-trip once. Income credits are persisted on the pet
        row; the wallet grant is a separate await per pet so a wallet
        failure on one pet doesn't roll back the decay for the others."""
        income_grants: list[tuple[int, int, str, int]] = []
        # Cache (tz_offset, reset_hour) → today_iso per guild so the
        # decay sweep computes the daily-reset date once per guild
        # instead of once per active pet.
        per_guild_today: dict[int, str] = {}
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(UserPet).where(UserPet.is_active.is_(True))
            )
            pets = list(rows.scalars().all())
            for pet in pets:
                care.apply_hourly_decay(pet)
                # Resolve today_iso under THIS pet's guild reset clock.
                today_iso = per_guild_today.get(pet.guild_id)
                if today_iso is None:
                    tz, hr = await self._mission_reset_config(pet.guild_id)
                    today_iso = pet_missions.current_daily_period(tz, hr)
                    per_guild_today[pet.guild_id] = today_iso
                # Passive income — gated by daily cap. Reset counter
                # at the start of a new mission-day per the guild's
                # reset clock.
                if pet.daily_income_date != today_iso:
                    pet.daily_income_credits = 0
                    pet.daily_income_date = today_iso
                rate = care.STAGE_HOURLY_INCOME.get(pet.stage, 0)
                cap = care.STAGE_DAILY_INCOME_CAP.get(pet.stage, 0)
                if rate > 0 and pet.daily_income_credits < cap:
                    grant = min(rate, cap - pet.daily_income_credits)
                    pet.daily_income_credits += grant
                    income_grants.append(
                        (pet.guild_id, pet.user_id, pet.species_id, grant)
                    )
            await s.commit()
        # Issue the wallet grants outside the DB session so a wallet
        # error doesn't poison the pet decay commit.
        for guild_id, user_id, species_id, amount in income_grants:
            try:
                await self.wallet.adjust(
                    guild_id, user_id, amount, REASON_PET_PASSIVE,
                    description=f"Pet passive ({species_id})",
                    metadata={"species_id": species_id},
                )
            except Exception:
                log.exception(
                    "pet passive grant failed for guild=%s user=%s",
                    guild_id, user_id,
                )
        log.info(
            "pet decay: applied to %d active pets, granted %d incomes",
            len(pets), len(income_grants),
        )

    # =========================================================================
    # Pet House handlers (called from PetHouseButton + _PetActionView)
    # =========================================================================

    async def handle_pet_shop_open(
        self, interaction: discord.Interaction,
    ) -> None:
        """Quick-access shop button on the Pet House card opens the
        same browser ephemeral as /pet shop."""
        await self._open_shop_browser(interaction)

    async def handle_pet_house_open(
        self, interaction: discord.Interaction,
    ) -> None:
        """Open the user's pet panel as an ephemeral with action buttons."""
        await interaction.response.defer(ephemeral=True)
        pet = await self._fetch_active_pet(
            interaction.guild_id, interaction.user.id,
        )
        if pet is None:
            await interaction.followup.send(
                "❌ You have no active pet. Use `/pet shop` then "
                "`/pet buy` to purchase one first.",
                ephemeral=True,
            )
            return
        embed, file = await self._build_status_embed_with_image(
            interaction.guild_id, pet, interaction.user,
        )
        view = _PetActionView(
            self, pet.id, interaction.user.id,
            show_in_info=bool(pet.show_in_info),
        )
        if file is not None:
            await interaction.followup.send(
                embed=embed, view=view, file=file, ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=embed, view=view, ephemeral=True,
            )

    async def _house_help(
        self, interaction: discord.Interaction,
    ) -> None:
        """Pop a Help ephemeral explaining how Bond works — what gates
        the 💔 lonely state, which actions raise it, and rough timing
        for exiting lonely. Numbers come straight from care.py do_*
        helpers so this doesn't drift if those deltas are retuned."""
        embed = discord.Embed(
            title="❓ How to raise your pet's Bond",
            description=(
                "**Bond** = your pet's affection for you. A freshly bought "
                "pet has `bond=0` → the **💔 lonely** state until bond ≥ 30."
            ),
            color=discord.Color.from_rgb(155, 89, 182),
        )
        embed.add_field(
            name="🎾 Play — main source of bond",
            value=(
                "**+2 bond** per use\n"
                "Cooldown: **5 min** • Needs `energy ≥ 10`\n"
                "Up to ~12 times/hour = **24 bond/hour**"
            ),
            inline=False,
        )
        embed.add_field(
            name="💪 Train — bonus bond + EXP",
            value=(
                "**+1 bond** per use\n"
                "Cooldown: **30 min** • Needs `energy ≥ 25`\n"
                "Mainly for leveling + evolution, bond is a bonus"
            ),
            inline=False,
        )
        embed.add_field(
            name="🍖 Feed / 💧 Water / 🧼 Clean / 💤 Rest",
            value=(
                "**No direct bond gain.** Used to restore "
                "hunger/thirst/cleanliness/energy — keep the pet in shape "
                "to Play / Train."
            ),
            inline=False,
        )
        embed.add_field(
            name="⚡ Fast path out of 💔 lonely",
            value=(
                "Play every 5 min → bond 0 → 30 in "
                "**~1-1.5 hours**. Keep hunger/thirst topped up so energy "
                "doesn't drop below 10 (Play would reject)."
            ),
            inline=False,
        )
        embed.add_field(
            name="🛡️ Anti-farm",
            value=(
                "A care action on a stat already at 100/100 is rejected → "
                "you can't spam Feed/Water to farm bond indirectly. "
                "Wait for the stat to drop before caring again."
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                "Bond doesn't decay over time — it only grows. "
                "Once it hits 30+, the pet leaves lonely for good "
                "(unless other care stats drop even lower)."
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _house_info_toggle(
        self,
        interaction: discord.Interaction,
        view: _PetActionView,
    ) -> None:
        """Flip UserPet.show_in_info on the panel's active pet and
        re-render with a fresh view so the button label reflects the
        new state. Deferred like _house_action for the same 3s-deadline
        reason (DB commit + status-embed image resolution)."""
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        async with self.bot.db.session() as s:
            pet = await s.get(UserPet, view.pet_id)
            if pet is None or pet.user_id != view.owner_id:
                await interaction.followup.send(
                    "❌ Pet no longer exists.", ephemeral=True,
                )
                return
            pet.show_in_info = not pet.show_in_info
            new_state = pet.show_in_info
            await s.commit()
            await s.refresh(pet)

        embed, file = await self._build_status_embed_with_image(
            interaction.guild_id, pet, interaction.user,
        )
        new_view = _PetActionView(
            self, view.pet_id, view.owner_id, show_in_info=new_state,
        )
        prefix = (
            "✅ Pet will **show** on your `/info` card.\n\n"
            if new_state else
            "🔘 Pet will **not show** on your `/info` card.\n\n"
        )
        embed.description = (prefix + (embed.description or ""))[:4000]
        attachments = [file] if file is not None else []
        try:
            await interaction.edit_original_response(
                embed=embed, view=new_view, attachments=attachments,
            )
        except discord.HTTPException as e:
            log.warning("pet house info-toggle edit failed: %s", e)

    async def _house_rename_open(
        self,
        interaction: discord.Interaction,
        view: _PetActionView,
    ) -> None:
        """Pre-flight rename: confirm the pet still exists and the
        clicker has enough coin for pet_rename_cost. Pass → open the
        modal; fail → ephemeral error. send_modal MUST be the
        interaction's first response, so the balance lookup runs but
        we do NOT defer."""
        async with self.bot.db.session() as s:
            pet = await s.get(UserPet, view.pet_id)
            if pet is None or pet.user_id != view.owner_id:
                await interaction.response.send_message(
                    "❌ Pet no longer exists.", ephemeral=True,
                )
                return
        cfg = await self._fetch_pet_cfg(interaction.guild_id)
        cost = int(cfg.pet_rename_cost) if cfg else 50
        if cost > 0:
            wallet = await self.wallet.get_or_create_wallet(
                interaction.guild_id, interaction.user.id,
            )
            if wallet.balance < cost:
                coin_name, coin_icon = await self._coin_label_parts(
                    interaction.guild_id,
                )
                await interaction.response.send_message(
                    f"❌ Not enough coins. Rename costs **{cost:,}** "
                    f"{coin_icon}, you have **{wallet.balance:,}** "
                    f"{coin_icon} {coin_name}.",
                    ephemeral=True,
                )
                return
        await interaction.response.send_modal(_PetRenameModal(self, view))

    async def _house_rename_submit(
        self,
        interaction: discord.Interaction,
        view: _PetActionView,
        new_name: str,
    ) -> None:
        """Charge pet_rename_cost + update the nickname + re-render
        the panel. Coin debit and nickname write happen in sequence
        with an InsufficientBalance recheck because the user could
        have spent coin between _house_rename_open and modal submit."""
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            pet = await s.get(UserPet, view.pet_id)
            if pet is None or pet.user_id != view.owner_id:
                await interaction.followup.send(
                    "❌ Pet no longer exists.", ephemeral=True,
                )
                return
            cfg = await s.get(PetGuildConfig, interaction.guild_id)
            cost = int(cfg.pet_rename_cost) if cfg else 50
            old_name = pet.nickname

        if cost > 0:
            try:
                await self.wallet.adjust(
                    interaction.guild_id, interaction.user.id, -cost,
                    REASON_PET_RENAME,
                    description=f"Pet rename ({pet.species_id})",
                    metadata={
                        "pet_id": pet.id,
                        "species_id": pet.species_id,
                        "old_nickname": old_name,
                        "new_nickname": new_name,
                    },
                )
            except InsufficientBalanceError as e:
                await interaction.followup.send(
                    f"❌ Not enough coins: {e}", ephemeral=True,
                )
                return

        async with self.bot.db.session() as s:
            pet = await s.get(UserPet, view.pet_id)
            if pet is None:
                # Pet vanished between debit and write — refund.
                if cost > 0:
                    try:
                        await self.wallet.adjust(
                            interaction.guild_id, interaction.user.id,
                            cost, REASON_PET_REFUND,
                            description=f"Refund pet rename (pet gone)",
                            metadata={"pet_id": view.pet_id},
                        )
                    except Exception:
                        log.exception("pet rename: refund failed")
                await interaction.followup.send(
                    "❌ Pet was deleted before the rename finished. "
                    "Coins refunded.", ephemeral=True,
                )
                return
            pet.nickname = new_name
            await s.commit()
            await s.refresh(pet)

        # Modal submit is a separate interaction from the panel's
        # original ephemeral — we can't reach back to edit that
        # panel's message from here. Send a fresh confirmation ephemeral
        # instead; the user can re-open the panel to see the new name
        # (handle_pet_house_open always re-reads from DB).
        coin_name, coin_icon = await self._coin_label_parts(
            interaction.guild_id,
        )
        cost_note = (
            f" (charged **{cost:,}** {coin_icon} {coin_name})"
            if cost > 0 else " (free)"
        )
        old_note = f" (was **{old_name}**)" if old_name else ""
        await interaction.followup.send(
            f"✏️ Renamed pet to **{new_name}**{old_note}{cost_note}.\n"
            "_Reopen Pet House to see the new name on the panel._",
            ephemeral=True,
        )

    async def _house_mission_open(
        self,
        interaction: discord.Interaction,
        view: _PetActionView,
    ) -> None:
        """Render the mission state as a *new* ephemeral (not an edit
        of the panel) so the player can keep both surfaces visible:
        action panel + mission board. Each ready mission gets its own
        Claim button in _PetMissionView. The returned WebhookMessage
        is cached so care actions can edit the board in place when
        progress changes."""
        await interaction.response.defer(ephemeral=True)
        state = await self._fetch_mission_state(
            interaction.guild_id, interaction.user.id,
        )
        coin_name, coin_icon = await self._coin_label_parts(
            interaction.guild_id,
        )
        tz, hr = await self._mission_reset_config(interaction.guild_id)
        embed = self._build_mission_embed(
            state, coin_icon=coin_icon, coin_name=coin_name,
            tz_offset=tz, reset_hour=hr,
        )
        mview = _PetMissionView(self, interaction.user.id, state)
        msg = await interaction.followup.send(
            embed=embed, view=mview, ephemeral=True,
        )
        # Replaces any older reference — if the user re-opens the board
        # the previous ephemeral becomes a snapshot the refresh helper
        # no longer tracks.
        self._mission_messages[
            (interaction.guild_id, interaction.user.id)
        ] = msg

    async def _refresh_mission_panel_if_open(
        self, guild_id: int, user_id: int,
    ) -> None:
        """If the user has a Mission ephemeral cached, recompute state
        and edit the message in place so progress bars / claim buttons
        reflect the new state without the user having to re-open.
        Silently drops the ref on 404 (user dismissed the ephemeral
        manually) or on token expiry."""
        key = (guild_id, user_id)
        msg = self._mission_messages.get(key)
        if msg is None:
            return
        try:
            state = await self._fetch_mission_state(guild_id, user_id)
            coin_name, coin_icon = await self._coin_label_parts(guild_id)
            tz, hr = await self._mission_reset_config(guild_id)
            embed = self._build_mission_embed(
                state, coin_icon=coin_icon, coin_name=coin_name,
                tz_offset=tz, reset_hour=hr,
            )
            new_view = _PetMissionView(self, user_id, state)
            await msg.edit(embed=embed, view=new_view)
        except discord.NotFound:
            self._mission_messages.pop(key, None)
        except discord.HTTPException as e:
            log.debug("pet mission auto-refresh edit failed: %s", e)
            # Don't pop here — could be a transient hiccup. Next try
            # will either work or hit NotFound, which does pop.

    async def _house_action(
        self,
        interaction: discord.Interaction,
        view: _PetActionView,
        action_name: str,
        image_action: str,
    ) -> None:
        """Apply a care action + re-render the ephemeral with new
        stats and action image. Cooldown / coin checks happen in
        _try_apply_action.

        Defers upfront: this handler chains several DB queries
        (_try_apply_action + _build_status_embed_with_image, which
        itself opens a fresh session and resolves the pet asset file),
        and on a slow DB link that easily blows Discord's 3s
        interaction deadline. Without the defer, users saw
        "This interaction failed" even though the action was applied
        server-side — stats only updated when they reopened the panel.
        """
        # Defer as an update to the existing message (button flow) so
        # edit_original_response later works and no "thinking" spinner
        # appears on the source message.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            # Already responded somehow — fall through and hope
            # edit_original_response still works.
            pass

        async with self.bot.db.session() as s:
            pet = await s.get(UserPet, view.pet_id)
            if pet is None or pet.user_id != view.owner_id:
                await interaction.followup.send(
                    "❌ Pet no longer exists.", ephemeral=True
                )
                return
            result, err, coin_spent = await self._try_apply_action(
                interaction.guild_id, view.owner_id, pet, action_name,
            )
            if err:
                await interaction.followup.send(err, ephemeral=True)
                return
            await s.commit()
            await s.refresh(pet)

        target = interaction.user  # owner_id already verified by interaction_check
        embed, file = await self._build_status_embed_with_image(
            interaction.guild_id, pet, target, action=image_action,
        )

        # Prepend an action summary so the player sees what changed.
        summary_lines: list[str] = []
        title_action = {
            "feed": "🍖 Feed", "water": "💧 Water",
            "play": "🎾 Play", "clean": "🧼 Bathe",
            "rest": "💤 Nghỉ", "train": "💪 Training",
        }.get(result.action, result.action)
        summary_lines.append(f"**{title_action}** xong:")
        if coin_spent > 0:
            _, coin_icon_house = await self._coin_label_parts(interaction.guild_id)
            summary_lines.append(
                f"  💰 Coin: **-{coin_spent}** {coin_icon_house}"
            )
        for field, change in result.delta.items():
            if change == 0:
                continue
            label = {
                "hunger": "🍖 Hunger", "thirst": "💧 Thirst",
                "mood": "😊 Mood", "energy": "⚡ Energy",
                "cleanliness": "🧼 Clean", "bond": "❤️ Bond",
                "health": "🩺 Health",
            }.get(field, field)
            sign = "+" if change > 0 else ""
            summary_lines.append(f"  {label}: **{sign}{change}**")
        if result.exp_gained:
            summary_lines.append(f"  ✨ EXP: **+{result.exp_gained}**")
        if result.level_up:
            summary_lines.append(f"🆙 **Level Up!** → Level {pet.level}")
        if result.new_stage:
            summary_lines.append(f"🦋 **Evolved!** → {result.new_stage}")

        new_desc = "\n".join(summary_lines) + "\n\n" + (embed.description or "")
        embed.description = new_desc[:4000]

        # Skip re-uploading the same attachment. When the resolved
        # image filename matches what's already on the message we
        # simply don't pass `attachments=` — Discord keeps the
        # existing file, embed.set_image already points at
        # attachment://<same-name>, and we shave the full file
        # upload round-trip out of the click's latency.
        image_filename = getattr(file, "filename", None) if file else None
        edit_kwargs: dict = {"embed": embed, "view": view}
        if file is None:
            # No image at all — explicitly clear any existing attachments.
            edit_kwargs["attachments"] = []
            view._last_image_filename = None
        elif image_filename != view._last_image_filename:
            edit_kwargs["attachments"] = [file]
            view._last_image_filename = image_filename
        # else: same filename → send no attachments= at all; the file
        # handle we built is discarded (its bytes weren't read yet).
        try:
            # We deferred at the top, so update via edit_original_response
            # rather than response.edit_message. The former also works
            # for deferred interactions where the latter would fail with
            # InteractionResponded.
            await interaction.edit_original_response(**edit_kwargs)
        except discord.HTTPException as e:
            log.warning("pet house action edit failed: %s", e)

    async def _house_share(
        self,
        interaction: discord.Interaction,
        view: _PetActionView,
    ) -> None:
        """Post a public snapshot of the user's pet to the configured
        pet_share_channel_id. Ephemeral confirmation to the clicker."""
        async with self.bot.db.session() as s:
            pet = await s.get(UserPet, view.pet_id)
            if pet is None or pet.user_id != view.owner_id:
                await interaction.response.send_message(
                    "❌ Pet no longer exists.", ephemeral=True,
                )
                return
            cfg = await s.get(PetGuildConfig, interaction.guild_id)
        if cfg is None or cfg.pet_share_channel_id is None:
            await interaction.response.send_message(
                "❌ Owner hasn't configured a share channel yet. Please ask an admin to run "
                "chạy `/pet-admin pet-house setup-share channel:#...`.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None:
            return
        channel = guild.get_channel(cfg.pet_share_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "❌ Share channel is no longer valid.", ephemeral=True,
            )
            return

        target = interaction.user
        embed, file = await self._build_status_embed_with_image(
            interaction.guild_id, pet, target,
        )
        embed.set_footer(text=f"Shared by {target.display_name}")
        try:
            if file is not None:
                await channel.send(
                    content=f"📤 {target.mention} is showing off their pet!",
                    embed=embed, file=file,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            else:
                await channel.send(
                    content=f"📤 {target.mention} is showing off their pet!",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Bot lacks permission to post in {channel.mention}.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"✅ Pet shared to {channel.mention}!", ephemeral=True,
        )


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(Pet(bot))
