"""Pet system — admin tier (Phase 1).

What ships in Phase 1
---------------------
Schema: PetGuildConfig, PetSpecies, PetAsset, PetAssetImportJob, UserPet
(see src/db/models.py). All five tables get auto-created by create_all
on the next bot start; no PetAsset rows are written yet because the
zip importer is Phase 2.

Slash commands (all under /pet-admin):
  setup admin-role role:<role>        owner-only
  species-create-manual ...           owner OR pet-admin (Phase 1 helper)
  species-list                        owner OR pet-admin
  species-info species_id:<id>        owner OR pet-admin
  species-config species_id [rarity] [price] [description] [concept]
                                       owner OR pet-admin
  species-enable species_id:<id>      owner OR pet-admin
  species-disable species_id:<id>     owner OR pet-admin
  preview-pack | import-pack | preview-stage | import-stage
                                       — stubs returning "🚧 Phase 2"

Permission tiers
  - Guild owner: always allowed
  - Pet admin role: allowed if cfg.admin_role_id is set
  - Administrator perm: allowed only if cfg.allow_administrator_pet_admin

Phase 2 will add: zip upload, manifest parsing, file storage layer,
asset classification (parseAssetFunction → assetType/stateKey), image
resolver and the user-facing /pet shop|buy|status|feed|... commands.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select

from ..db.models import (
    PetAsset,
    PetAssetImportJob,
    PetGuildConfig,
    PetSpecies,
    UserPet,
)
from ..pets.importer import ScanResult, perform_import, scan_pack
from ..pets.manifest import REQUIRED_FOLDERS
from ..pets.storage import PetStorage

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.pet-admin")


# /^[a-z0-9_]{2,64}$/ per spec section 10.3
_SPECIES_ID_RE = re.compile(r"^[a-z0-9_]{2,64}$")
_ALLOWED_RARITIES = ("common", "rare", "epic", "legendary")


def _temp_zip_path(job_id: int) -> Path:
    """Deterministic temp file for a pending import. The Confirm
    button reads from this path; Cancel deletes it."""
    base = Path(tempfile.gettempdir()) / "akim-pet-imports"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{job_id}.zip"


def _build_preview_embed(
    scan: ScanResult, mode: str, zip_filename: str,
) -> discord.Embed:
    """Spec §17 — preview embed shape (stages, folders, totals,
    warnings, errors)."""
    if not scan.is_valid:
        color = discord.Color.red()
        title = "🐾 Pet Pack — INVALID"
    else:
        color = discord.Color.from_rgb(155, 89, 182)
        title = "🐾 Pet Pack Preview"
    embed = discord.Embed(title=title, color=color)
    desc_lines = [
        f"**Pet:** {scan.display_name or '_(unknown)_'}",
        f"**Species ID:** `{scan.species_id or '?'}`",
        f"**Mode:** `{mode}`",
        f"**File:** `{zip_filename}`",
    ]
    if scan.rarity:
        desc_lines.append(f"**Rarity:** {scan.rarity}")
    if scan.price is not None:
        desc_lines.append(f"**Price:** {scan.price:,} coin")
    embed.description = "\n".join(desc_lines)

    # Stage breakdown
    by_stage: dict[str, int] = {}
    for p in scan.planned:
        by_stage[p.stage] = by_stage.get(p.stage, 0) + 1
    if by_stage:
        stage_lines = [
            f"✅ {s.capitalize()} — {n} images"
            for s, n in sorted(by_stage.items())
        ]
        embed.add_field(
            name=f"Stages ({len(by_stage)})",
            value="\n".join(stage_lines), inline=True,
        )

    # Folder breakdown
    by_folder: dict[str, int] = {}
    for p in scan.planned:
        by_folder[p.pack_id] = by_folder.get(p.pack_id, 0) + 1
    if by_folder:
        folder_lines = []
        for f in REQUIRED_FOLDERS:
            mark = "✅" if f in by_folder else "❌"
            folder_lines.append(
                f"{mark} `{f}` — {by_folder.get(f, 0)} images"
            )
        embed.add_field(
            name="Folders", value="\n".join(folder_lines), inline=True,
        )

    embed.add_field(
        name="Total",
        value=f"**{len(scan.planned)}** images",
        inline=False,
    )

    if scan.errors:
        errs = "\n".join(f"• {e}" for e in scan.errors[:10])
        if len(scan.errors) > 10:
            errs += f"\n• _(+{len(scan.errors) - 10} more errors)_"
        embed.add_field(
            name=f"❌ Errors ({len(scan.errors)})",
            value=errs[:1024], inline=False,
        )
    if scan.warnings:
        warns = "\n".join(f"• {w}" for w in scan.warnings[:5])
        if len(scan.warnings) > 5:
            warns += f"\n• _(+{len(scan.warnings) - 5} more warnings)_"
        embed.add_field(
            name=f"⚠️ Warnings ({len(scan.warnings)})",
            value=warns[:1024], inline=False,
        )

    if scan.is_valid:
        embed.set_footer(
            text="Press Confirm to import, Cancel to abort. "
            "Confirm writes the files to storage + DB."
        )
    else:
        embed.set_footer(
            text="Fix the ZIP and re-upload — packs with errors can't import."
        )
    return embed


# =============================================================================
# Persistent Confirm / Cancel buttons (DynamicItem)
# =============================================================================


class ConfirmImportButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"pet:confirm-import:(?P<job_id>\d+)",
):
    def __init__(self, job_id: int, *, disabled: bool = False) -> None:
        self.job_id = job_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"pet:confirm-import:{job_id}",
                label="Confirm Import",
                emoji="✅",
                style=discord.ButtonStyle.success,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["job_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "PetAdmin" = interaction.client.get_cog("PetAdmin")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_confirm_import(interaction, self.job_id)


class CancelImportButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"pet:cancel-import:(?P<job_id>\d+)",
):
    def __init__(self, job_id: int, *, disabled: bool = False) -> None:
        self.job_id = job_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"pet:cancel-import:{job_id}",
                label="Cancel",
                emoji="❌",
                style=discord.ButtonStyle.danger,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ):
        return cls(int(match["job_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "PetAdmin" = interaction.client.get_cog("PetAdmin")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_cancel_import(interaction, self.job_id)


def _build_import_view(job_id: int, *, disabled: bool = False) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ConfirmImportButton(job_id, disabled=disabled))
    v.add_item(CancelImportButton(job_id, disabled=disabled))
    return v


# =============================================================================
# Cog
# =============================================================================


class PetAdmin(commands.Cog, name="PetAdmin"):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        # Filesystem storage layer (Railway volume — PET_STORAGE_PATH).
        # Instantiation just records the base path; no disk access yet.
        self.storage = PetStorage()

    pet_admin = app_commands.Group(
        name="pet-admin",
        description="Manage the Pet system (owner / pet-admin role)",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ---- helpers ----

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        )

    async def _get_cfg(self, guild_id: int) -> PetGuildConfig:
        async with self.bot.db.session() as s:
            cfg = await s.get(PetGuildConfig, guild_id)
            if cfg is None:
                cfg = PetGuildConfig(guild_id=guild_id)
                s.add(cfg)
                await s.commit()
                await s.refresh(cfg)
            return cfg

    async def _can_pet_admin(self, interaction: discord.Interaction) -> bool:
        """Owner OR has the configured pet-admin role OR
        (administrator perm AND allow_administrator_pet_admin flag).
        Owner is the only one allowed for /pet-admin setup."""
        if self._is_owner(interaction):
            return True
        if not isinstance(interaction.user, discord.Member):
            return False
        cfg = await self._get_cfg(interaction.guild_id)
        if cfg.admin_role_id is not None:
            user_role_ids = {r.id for r in interaction.user.roles}
            if cfg.admin_role_id in user_role_ids:
                return True
        if (
            cfg.allow_administrator_pet_admin
            and interaction.user.guild_permissions.administrator
        ):
            return True
        return False

    async def _require_pet_admin(self, interaction: discord.Interaction) -> bool:
        if await self._can_pet_admin(interaction):
            return True
        msg = (
            "❌ You don't have permission for this command. You need to be the "
            "**server owner**, hold the **pet admin role**, or be an "
            "administrator (when that flag is enabled)."
        )
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if self._is_owner(interaction):
            return True
        msg = "❌ Only the **server owner** can use this command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    async def _fetch_species(
        self, guild_id: int, species_id: str,
    ) -> PetSpecies | None:
        async with self.bot.db.session() as s:
            return await s.scalar(
                select(PetSpecies).where(
                    PetSpecies.guild_id == guild_id,
                    PetSpecies.species_id == species_id,
                )
            )

    # =========================================================================
    # /pet-admin setup
    # =========================================================================

    @pet_admin.command(
        name="setup",
        description="(Owner) Configure the pet-admin role + allow_administrator flag",
    )
    @app_commands.describe(
        admin_role="Role allowed to use /pet-admin commands (in addition to the owner)",
        allow_administrator="Also allow the Administrator permission to use /pet-admin",
        feed_cost="Coin cost for /pet feed (default 10)",
        water_cost="Coin cost for /pet water (default 5)",
        rename_cost="Coin cost for Rename in Pet House (default 50, set 0 = free)",
        mission_tz_offset="Timezone offset for mission reset (-12..14, default 7 = VN)",
        mission_reset_hour="Hour of day for mission reset in that timezone (0-23, default 0 = midnight)",
    )
    async def pet_admin_setup(
        self,
        interaction: discord.Interaction,
        admin_role: discord.Role | None = None,
        allow_administrator: bool | None = None,
        feed_cost: app_commands.Range[int, 0, 1_000_000] | None = None,
        water_cost: app_commands.Range[int, 0, 1_000_000] | None = None,
        rename_cost: app_commands.Range[int, 0, 1_000_000] | None = None,
        mission_tz_offset: app_commands.Range[int, -12, 14] | None = None,
        mission_reset_hour: app_commands.Range[int, 0, 23] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(PetGuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = PetGuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            if admin_role is not None:
                cfg.admin_role_id = admin_role.id
            if allow_administrator is not None:
                cfg.allow_administrator_pet_admin = bool(allow_administrator)
            if feed_cost is not None:
                cfg.pet_feed_cost = int(feed_cost)
            if water_cost is not None:
                cfg.pet_water_cost = int(water_cost)
            if rename_cost is not None:
                cfg.pet_rename_cost = int(rename_cost)
            if mission_tz_offset is not None:
                cfg.pet_mission_tz_offset = int(mission_tz_offset)
            if mission_reset_hour is not None:
                cfg.pet_mission_reset_hour = int(mission_reset_hour)
            await s.commit()
            await s.refresh(cfg)
        embed = discord.Embed(
            title="🐾 Pet System configuration",
            color=discord.Color.from_rgb(155, 89, 182),
        )
        role_value = (
            f"<@&{cfg.admin_role_id}>" if cfg.admin_role_id else "—"
        )
        embed.add_field(name="Pet admin role", value=role_value, inline=True)
        embed.add_field(
            name="Allow Administrator",
            value="✅" if cfg.allow_administrator_pet_admin else "❌",
            inline=True,
        )
        embed.add_field(
            name="ZIP limits",
            value=f"max {cfg.max_zip_size_mb}MB • {cfg.max_files_per_zip} files",
            inline=False,
        )
        rename_label = (
            f"**{cfg.pet_rename_cost:,}** coin"
            if cfg.pet_rename_cost > 0 else "**free**"
        )
        embed.add_field(
            name="Action costs",
            value=(
                f"🍖 /pet feed: **{cfg.pet_feed_cost:,}** coin\n"
                f"💧 /pet water: **{cfg.pet_water_cost:,}** coin\n"
                f"✏️ Pet House Rename: {rename_label}\n"
                "🎾 play / 🧼 clean / 💤 rest / 💪 train: free, on cooldown"
            ),
            inline=False,
        )
        tz_sign = "+" if cfg.pet_mission_tz_offset >= 0 else "-"
        tz_label = f"UTC{tz_sign}{abs(cfg.pet_mission_tz_offset)}"
        embed.add_field(
            name="Mission reset",
            value=(
                f"⏰ Daily `{cfg.pet_mission_reset_hour:02d}:00` "
                f"({tz_label})\n"
                f"📅 Weekly: Monday at the same hour"
            ),
            inline=False,
        )
        await interaction.followup.send(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # =========================================================================
    # /pet-admin species-create-manual (Phase 1 helper — no zip required)
    # =========================================================================

    @pet_admin.command(
        name="species-create-manual",
        description="Create a species manually (Phase 1 — no zip upload needed)",
    )
    @app_commands.describe(
        species_id="Snake_case ID (a-z, 0-9, _; 2-64 chars). e.g. moon_cat",
        display_name="Display name. e.g. Moon Cat",
        rarity="common | rare | epic | legendary",
        price="Coin price when a user buys the pet",
        concept="Short concept description — optional",
    )
    async def species_create_manual(
        self,
        interaction: discord.Interaction,
        species_id: str,
        display_name: app_commands.Range[str, 1, 128],
        rarity: Literal["common", "rare", "epic", "legendary"] = "common",
        price: app_commands.Range[int, 0, 10_000_000] = 500,
        concept: app_commands.Range[str, 1, 500] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        species_id = species_id.strip().lower()
        if not _SPECIES_ID_RE.match(species_id):
            await interaction.followup.send(
                "❌ Invalid `species_id`. Format: lowercase snake_case, "
                "[a-z0-9_], 2-64 chars. e.g. `moon_cat`.",
                ephemeral=True,
            )
            return
        existing = await self._fetch_species(interaction.guild_id, species_id)
        if existing is not None:
            await interaction.followup.send(
                f"❌ Species `{species_id}` already exists in this server.",
                ephemeral=True,
            )
            return
        async with self.bot.db.session() as s:
            sp = PetSpecies(
                guild_id=interaction.guild_id,
                species_id=species_id,
                display_name=display_name.strip(),
                rarity=rarity,
                price=int(price),
                concept=concept.strip() if concept else None,
                is_shop_enabled=False,
                is_active=True,
                imported_by=interaction.user.id,
                imported_at=datetime.now(timezone.utc),
            )
            s.add(sp)
            await s.commit()
            await s.refresh(sp)
        await interaction.followup.send(
            f"✅ Species `{species_id}` created (**{sp.display_name}**, "
            f"{sp.rarity}, {sp.price:,} coin).\n"
            "ℹ️ Assets (images) come in Phase 2 (zip upload). "
            "The species can be listed in the shop now, but has no images yet.",
            ephemeral=True,
        )

    # =========================================================================
    # /pet-admin species-list
    # =========================================================================

    @pet_admin.command(
        name="species-list",
        description="List every species in this server",
    )
    async def species_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(PetSpecies)
                .where(PetSpecies.guild_id == interaction.guild_id)
                .order_by(PetSpecies.species_id)
            )
            species = list(rows.scalars().all())
        if not species:
            await interaction.followup.send(
                "_No species yet. Use `/pet-admin species-create-manual` "
                "to create one._",
                ephemeral=True,
            )
            return
        lines = []
        for sp in species:
            shop = "🛒" if sp.is_shop_enabled else "🚫"
            active = "" if sp.is_active else " _[disabled]_"
            lines.append(
                f"{shop} `{sp.species_id}` — **{sp.display_name}** "
                f"({sp.rarity}, {sp.price:,} coin) • "
                f"{sp.asset_count} assets{active}"
            )
        embed = discord.Embed(
            title=f"🐾 Pet Species ({len(species)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.from_rgb(155, 89, 182),
        )
        embed.set_footer(text="🛒 = shop enabled, 🚫 = shop off")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # =========================================================================
    # /pet-admin species-info
    # =========================================================================

    @pet_admin.command(
        name="species-info",
        description="Show details for one species",
    )
    @app_commands.describe(species_id="ID species (snake_case)")
    async def species_info(
        self,
        interaction: discord.Interaction,
        species_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        species_id = species_id.strip().lower()
        # Self-heal: recompute stage_count + asset_count live from
        # PetAsset rows so any stale value from older single-stage
        # imports gets corrected just by looking at it.
        async with self.bot.db.session() as s:
            sp = await s.scalar(
                select(PetSpecies).where(
                    PetSpecies.guild_id == interaction.guild_id,
                    PetSpecies.species_id == species_id,
                )
            )
            if sp is None:
                await interaction.followup.send(
                    f"❌ Species `{species_id}` not found.", ephemeral=True
                )
                return
            stages_q = await s.execute(
                select(PetAsset.stage).where(
                    PetAsset.guild_id == interaction.guild_id,
                    PetAsset.species_id == species_id,
                    PetAsset.is_active.is_(True),
                ).distinct()
            )
            distinct_stages = {row[0] for row in stages_q.all()}
            assets_count = await s.scalar(
                select(func.count(PetAsset.id)).where(
                    PetAsset.guild_id == interaction.guild_id,
                    PetAsset.species_id == species_id,
                    PetAsset.is_active.is_(True),
                )
            ) or 0
            # Only write back if values drifted (avoid bumping updated_at
            # on every read)
            if sp.stage_count != len(distinct_stages) or sp.asset_count != int(assets_count):
                sp.stage_count = len(distinct_stages)
                sp.asset_count = int(assets_count)
                await s.commit()
                await s.refresh(sp)
        embed = discord.Embed(
            title=f"🐾 {sp.display_name}",
            description=sp.concept or "_(no concept)_",
            color=(
                discord.Color.gold() if sp.rarity == "legendary"
                else discord.Color.purple() if sp.rarity == "epic"
                else discord.Color.blue() if sp.rarity == "rare"
                else discord.Color.dark_gray()
            ),
        )
        embed.add_field(name="Species ID", value=f"`{sp.species_id}`", inline=True)
        embed.add_field(name="Rarity", value=sp.rarity, inline=True)
        embed.add_field(name="Price", value=f"{sp.price:,} coin", inline=True)
        embed.add_field(
            name="Shop", value="✅ enabled" if sp.is_shop_enabled else "❌ disabled",
            inline=True,
        )
        embed.add_field(
            name="Active", value="✅" if sp.is_active else "❌", inline=True,
        )
        embed.add_field(
            name="Assets",
            value=f"{sp.asset_count} images • {sp.stage_count}/4 stages",
            inline=True,
        )
        if sp.description:
            embed.add_field(name="Description", value=sp.description[:1024], inline=False)
        if sp.imported_by:
            ts = (
                f" • <t:{int(sp.imported_at.timestamp())}:R>"
                if sp.imported_at else ""
            )
            embed.add_field(
                name="Imported by",
                value=f"<@{sp.imported_by}>{ts}",
                inline=False,
            )
        await interaction.followup.send(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # =========================================================================
    # /pet-admin link-desktop-asset
    # =========================================================================

    @pet_admin.command(
        name="link-desktop-asset",
        description="(Bridge) Assign a desktop_asset_id so akim-desk-pet can render the species",
    )
    @app_commands.describe(
        species_id="Server species ID (see /pet-admin species-list)",
        desktop_asset_id=(
            "Asset ID in akim-desk-pet (e.g. moon_cat). Leave empty to unlink."
        ),
    )
    async def link_desktop_asset(
        self,
        interaction: discord.Interaction,
        species_id: str,
        desktop_asset_id: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        clean_id = (desktop_asset_id or "").strip().lower() or None
        if clean_id is not None and not re.fullmatch(r"[a-z0-9_]{1,64}", clean_id):
            await interaction.followup.send(
                "❌ `desktop_asset_id` may only contain `a-z 0-9 _`, up to 64 characters.",
                ephemeral=True,
            )
            return
        async with self.bot.db.session() as s:
            sp = await s.scalar(
                select(PetSpecies).where(
                    PetSpecies.guild_id == interaction.guild_id,
                    PetSpecies.species_id == species_id,
                )
            )
            if sp is None:
                await interaction.followup.send(
                    f"❌ Species `{species_id}` not found in this server.",
                    ephemeral=True,
                )
                return
            previous = sp.desktop_asset_id
            sp.desktop_asset_id = clean_id
            await s.commit()
        if clean_id is None:
            await interaction.followup.send(
                f"🔗 Unlinked species `{species_id}` from desktop "
                f"(was: `{previous or '—'}`).",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"🔗 Species `{species_id}` linked to desktop asset `{clean_id}`. "
                "Owners will see the asset in the desktop app on their next "
                "/pet/active poll.",
                ephemeral=True,
            )

    # =========================================================================
    # /pet-admin species-config
    # =========================================================================

    @pet_admin.command(
        name="species-config",
        description="Update a species' rarity / price / description / concept",
    )
    @app_commands.describe(
        species_id="Species ID to update",
        rarity="common | rare | epic | legendary",
        price="New coin price",
        description="Full description",
        concept="Short concept",
    )
    async def species_config(
        self,
        interaction: discord.Interaction,
        species_id: str,
        rarity: Literal["common", "rare", "epic", "legendary"] | None = None,
        price: app_commands.Range[int, 0, 10_000_000] | None = None,
        description: app_commands.Range[str, 1, 2000] | None = None,
        concept: app_commands.Range[str, 1, 500] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        species_id = species_id.strip().lower()
        async with self.bot.db.session() as s:
            sp = await s.scalar(
                select(PetSpecies).where(
                    PetSpecies.guild_id == interaction.guild_id,
                    PetSpecies.species_id == species_id,
                )
            )
            if sp is None:
                await interaction.followup.send(
                    f"❌ Species `{species_id}` not found.", ephemeral=True
                )
                return
            if rarity is not None:
                sp.rarity = rarity
            if price is not None:
                sp.price = int(price)
            if description is not None:
                sp.description = description.strip()
            if concept is not None:
                sp.concept = concept.strip()
            await s.commit()
            await s.refresh(sp)
        await interaction.followup.send(
            f"✅ Updated `{species_id}` → "
            f"**{sp.display_name}** ({sp.rarity}, {sp.price:,} coin).",
            ephemeral=True,
        )

    # =========================================================================
    # /pet-admin species-rename
    # =========================================================================

    @pet_admin.command(
        name="species-rename",
        description="Change a species' display name",
    )
    @app_commands.describe(
        species_id="Species ID to rename",
        display_name="New display name (1-64 chars)",
    )
    async def species_rename(
        self,
        interaction: discord.Interaction,
        species_id: str,
        display_name: app_commands.Range[str, 1, 64],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        species_id = species_id.strip().lower()
        new_name = display_name.strip()
        async with self.bot.db.session() as s:
            sp = await s.scalar(
                select(PetSpecies).where(
                    PetSpecies.guild_id == interaction.guild_id,
                    PetSpecies.species_id == species_id,
                )
            )
            if sp is None:
                await interaction.followup.send(
                    f"❌ Species `{species_id}` not found.",
                    ephemeral=True,
                )
                return
            old_name = sp.display_name
            sp.display_name = new_name
            await s.commit()
        await interaction.followup.send(
            f"✏️ Renamed species `{species_id}`:\n"
            f"  **{old_name}** → **{new_name}**\n\n"
            f"_(species_id is unchanged — assets, owned pets, and shop buttons still link correctly.)_",
            ephemeral=True,
        )

    # =========================================================================
    # /pet-admin species-enable / species-disable
    # =========================================================================

    @pet_admin.command(
        name="species-enable",
        description="Enable a species in the shop (users can buy)",
    )
    @app_commands.describe(species_id="Species ID to enable")
    async def species_enable(
        self,
        interaction: discord.Interaction,
        species_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        await self._toggle_shop(
            interaction, species_id.strip().lower(), enable=True,
        )

    @pet_admin.command(
        name="species-disable",
        description="Disable a species from the shop (users can't buy)",
    )
    @app_commands.describe(species_id="Species ID to disable")
    async def species_disable(
        self,
        interaction: discord.Interaction,
        species_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        await self._toggle_shop(
            interaction, species_id.strip().lower(), enable=False,
        )

    # =========================================================================
    # /pet-admin pet-house — owner setup for the persistent house embed
    # =========================================================================

    pet_house = app_commands.Group(
        name="pet-house",
        description="Create / configure the Pet House embed",
        parent=pet_admin,
    )

    @pet_house.command(
        name="setup-share",
        description="(Owner / pet-admin) Channel public nơi Share button post",
    )
    @app_commands.describe(
        channel="Channel that receives the snapshot when a user presses Share in the Pet House",
    )
    async def pet_house_setup_share(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(PetGuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = PetGuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            cfg.pet_share_channel_id = channel.id
            await s.commit()
        await interaction.followup.send(
            f"✅ Pet share channel set → {channel.mention}.",
            ephemeral=True,
        )

    @pet_house.command(
        name="setup-name",
        description="(Owner / pet-admin) Set the server name shown in the Pet House",
    )
    @app_commands.describe(
        name="Display name (e.g. 'WWM Server'). Leave blank to use guild.name.",
    )
    async def pet_house_setup_name(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 128] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(PetGuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = PetGuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            # name=None → clear the override; trimmed string → store.
            cfg.pet_house_server_name = name.strip() if name else None
            old_ch = cfg.pet_house_channel_id
            old_msg = cfg.pet_house_message_id
            await s.commit()

        effective = name.strip() if name else (
            interaction.guild.name if interaction.guild else "Server"
        )
        # Auto-refresh the existing card so the new greeting shows up
        # without an explicit re-post.
        refreshed = False
        if old_ch and old_msg and interaction.guild:
            from .pet import _build_pet_house_embed, _build_pet_house_view
            ch = interaction.guild.get_channel(old_ch)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await ch.fetch_message(old_msg)
                    await msg.edit(
                        embed=_build_pet_house_embed(effective),
                        view=_build_pet_house_view(interaction.guild_id),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    refreshed = True
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        verb = "set" if name else "cleared override — will use `guild.name`"
        suffix = (
            "\n✅ Pet House card refreshed with the new name."
            if refreshed else
            "\n_(No Pet House to refresh yet — run `post` to create one.)_"
        )
        await interaction.followup.send(
            f"✅ {verb}: **{effective}**.{suffix}",
            ephemeral=True,
        )

    @pet_house.command(
        name="post",
        description="(Owner / pet-admin) Create/replace the Pet House embed in a channel",
    )
    @app_commands.describe(channel="Channel to post the Pet House in")
    async def pet_house_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        # Local imports to avoid the cog ↔ cog circular at module load.
        from .pet import _build_pet_house_embed, _build_pet_house_view
        async with self.bot.db.session() as s:
            cfg = await s.get(PetGuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = PetGuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
                await s.commit()
                await s.refresh(cfg)
            old_ch = cfg.pet_house_channel_id
            old_msg = cfg.pet_house_message_id
        # Best-effort delete the previous card so we don't have two
        # houses floating around the server.
        if old_ch and old_msg and interaction.guild:
            prev_ch = interaction.guild.get_channel(old_ch)
            if isinstance(prev_ch, (discord.TextChannel, discord.Thread)):
                try:
                    prev = await prev_ch.fetch_message(old_msg)
                    await prev.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        # Resolve display name: custom override OR live guild.name fallback.
        server_name = (
            (cfg.pet_house_server_name if cfg else None)
            or (interaction.guild.name if interaction.guild else "Server")
        )
        try:
            msg = await channel.send(
                embed=_build_pet_house_embed(server_name),
                view=_build_pet_house_view(interaction.guild_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot lacks permission to post in {channel.mention}.",
                ephemeral=True,
            )
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(PetGuildConfig, interaction.guild_id)
            if cfg is not None:
                cfg.pet_house_channel_id = channel.id
                cfg.pet_house_message_id = msg.id
                await s.commit()
        share_ch = (
            f" • Share channel: <#{cfg.pet_share_channel_id}>"
            if cfg and cfg.pet_share_channel_id else
            "\n⚠️ No share channel configured — run "
            "`/pet-admin pet-house setup-share channel:#...` to enable the Share button."
        )
        await interaction.followup.send(
            f"✅ Pet House posted in {channel.mention}.{share_ch}",
            ephemeral=True,
        )

    @pet_house.command(
        name="remove",
        description="(Owner / pet-admin) Remove the Pet House embed",
    )
    async def pet_house_remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(PetGuildConfig, interaction.guild_id)
            if cfg is None or cfg.pet_house_message_id is None:
                await interaction.followup.send(
                    "_No Pet House exists in this server._", ephemeral=True,
                )
                return
            old_ch = cfg.pet_house_channel_id
            old_msg = cfg.pet_house_message_id
            cfg.pet_house_channel_id = None
            cfg.pet_house_message_id = None
            await s.commit()
        if old_ch and old_msg and interaction.guild:
            ch = interaction.guild.get_channel(old_ch)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await ch.fetch_message(old_msg)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        await interaction.followup.send(
            "🗑 Pet House removed.", ephemeral=True,
        )

    @pet_admin.command(
        name="species-delete",
        description="Permanently delete a species (DB rows + filesystem archived)",
    )
    @app_commands.describe(
        species_id="Species ID to delete",
        force=(
            "True = also delete owned UserPet rows (loses player data). "
            "False = only delete when no one owns it."
        ),
    )
    async def species_delete(
        self,
        interaction: discord.Interaction,
        species_id: str,
        force: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        species_id = species_id.strip().lower()
        async with self.bot.db.session() as s:
            sp = await s.scalar(
                select(PetSpecies).where(
                    PetSpecies.guild_id == interaction.guild_id,
                    PetSpecies.species_id == species_id,
                )
            )
            if sp is None:
                await interaction.followup.send(
                    f"❌ Species `{species_id}` not found.",
                    ephemeral=True,
                )
                return
            owner_count = await s.scalar(
                select(func.count(UserPet.id)).where(
                    UserPet.guild_id == interaction.guild_id,
                    UserPet.species_id == species_id,
                )
            ) or 0
            if owner_count > 0 and not force:
                await interaction.followup.send(
                    f"⚠️ **{owner_count}** user(s) own pet `{species_id}`. "
                    "Set `force:True` to also delete their pets "
                    "(they won't get their coin back).",
                    ephemeral=True,
                )
                return
            # Cascade delete: UserPet → PetAsset → PetSpecies
            user_pet_deleted = 0
            if owner_count > 0:
                res = await s.execute(
                    delete(UserPet).where(
                        UserPet.guild_id == interaction.guild_id,
                        UserPet.species_id == species_id,
                    )
                )
                user_pet_deleted = res.rowcount or 0
            assets_res = await s.execute(
                delete(PetAsset).where(
                    PetAsset.guild_id == interaction.guild_id,
                    PetAsset.species_id == species_id,
                )
            )
            assets_deleted = assets_res.rowcount or 0
            display_name = sp.display_name
            await s.delete(sp)
            await s.commit()
        # Move on-disk folder to .disabled-{ts} so the bytes aren't
        # gone forever (recoverable by an admin via SSH if needed).
        files_archived = self.storage.disable_species(
            interaction.guild_id, species_id,
        )
        await interaction.followup.send(
            f"🗑 Deleted species `{species_id}` (**{display_name}**):\n"
            f"• {assets_deleted} PetAsset rows\n"
            f"• {user_pet_deleted} UserPet rows\n"
            f"• {files_archived} files → archived under `.disabled-<ts>`",
            ephemeral=True,
        )

    async def _toggle_shop(
        self, interaction: discord.Interaction,
        species_id: str, *, enable: bool,
    ) -> None:
        async with self.bot.db.session() as s:
            sp = await s.scalar(
                select(PetSpecies).where(
                    PetSpecies.guild_id == interaction.guild_id,
                    PetSpecies.species_id == species_id,
                )
            )
            if sp is None:
                await interaction.followup.send(
                    f"❌ Species `{species_id}` not found.", ephemeral=True
                )
                return
            sp.is_shop_enabled = enable
            await s.commit()
        verb = "✅ Enabled" if enable else "🚫 Disabled"
        await interaction.followup.send(
            f"{verb} shop for `{species_id}`.", ephemeral=True
        )

    # =========================================================================
    # Zip workflow — preview-pack / import-pack / preview-stage / import-stage
    # =========================================================================

    async def _download_zip(
        self, attachment: discord.Attachment, max_bytes: int,
    ) -> tuple[bytes | None, str | None]:
        """Read the attachment with the configured size cap. Returns
        (bytes, error_msg) — exactly one is non-None."""
        if not attachment.filename.lower().endswith(".zip"):
            return None, "Only `.zip` files are accepted."
        if attachment.size > max_bytes:
            return None, (
                f"File too large: {attachment.size // 1024} KB "
                f"exceeds the {max_bytes // (1024 * 1024)} MB limit."
            )
        try:
            data = await attachment.read()
        except discord.HTTPException as e:
            return None, f"Couldn't download the file: {e}"
        return data, None

    async def _scan_and_reply(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        is_single_stage: bool,
    ) -> None:
        """Read-only preview flow for /pet-admin preview-pack /
        preview-stage. Does NOT create a job row or write to storage."""
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        cfg = await self._get_cfg(interaction.guild_id)
        max_bytes = cfg.max_zip_size_mb * 1024 * 1024
        data, err = await self._download_zip(file, max_bytes)
        if data is None:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        scan = scan_pack(
            data,
            is_single_stage=is_single_stage,
            max_size_bytes=max_bytes,
            max_files=cfg.max_files_per_zip,
        )
        embed = _build_preview_embed(scan, mode="preview", zip_filename=file.filename)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @pet_admin.command(
        name="preview-pack",
        description="Preview a full pet pack (.zip) — no DB writes",
    )
    @app_commands.describe(file=".zip containing pet-pack.manifest.json + 4 stages")
    async def preview_pack(
        self, interaction: discord.Interaction, file: discord.Attachment,
    ) -> None:
        await self._scan_and_reply(interaction, file, is_single_stage=False)

    @pet_admin.command(
        name="preview-stage",
        description="Preview a single-stage pack (.zip) — no DB writes",
    )
    @app_commands.describe(file=".zip containing manifest.json + 4 folders at root")
    async def preview_stage(
        self, interaction: discord.Interaction, file: discord.Attachment,
    ) -> None:
        await self._scan_and_reply(interaction, file, is_single_stage=True)

    async def _import_workflow(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        mode: str,
        is_single_stage: bool,
    ) -> None:
        """Validate + create job row + show Confirm/Cancel buttons.
        Actual storage + DB writes happen in handle_confirm_import."""
        await interaction.response.defer(ephemeral=True)
        if not await self._require_pet_admin(interaction):
            return
        cfg = await self._get_cfg(interaction.guild_id)
        max_bytes = cfg.max_zip_size_mb * 1024 * 1024
        data, err = await self._download_zip(file, max_bytes)
        if data is None:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        scan = scan_pack(
            data,
            is_single_stage=is_single_stage,
            max_size_bytes=max_bytes,
            max_files=cfg.max_files_per_zip,
        )
        if not scan.is_valid:
            embed = _build_preview_embed(scan, mode=mode, zip_filename=file.filename)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Create job row in READY_TO_CONFIRM state
        async with self.bot.db.session() as s:
            job = PetAssetImportJob(
                guild_id=interaction.guild_id,
                uploaded_by=interaction.user.id,
                species_id=scan.species_id,
                display_name=scan.display_name,
                mode=mode,
                status="READY_TO_CONFIRM",
                zip_filename=file.filename[:256],
                zip_size=len(data),
                total_assets=len(scan.planned),
            )
            s.add(job)
            await s.commit()
            await s.refresh(job)
            job_id = job.id

        # Persist zip to a temp file so Confirm can re-read after the
        # 15-min interaction window without re-downloading.
        try:
            _temp_zip_path(job_id).write_bytes(data)
        except OSError as e:
            log.exception("pet: failed to write temp zip")
            await interaction.followup.send(
                f"❌ Couldn't write temp file: {e}", ephemeral=True
            )
            return

        embed = _build_preview_embed(scan, mode=mode, zip_filename=file.filename)
        view = _build_import_view(job_id)
        await interaction.followup.send(
            embed=embed, view=view, ephemeral=True,
        )

    @pet_admin.command(
        name="import-pack",
        description="Import a full pet pack — preview then press Confirm",
    )
    @app_commands.describe(
        file=".zip file",
        mode="create = new • update = add/update • replace = overwrite everything",
    )
    async def import_pack(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        mode: Literal["create", "update", "replace"] = "create",
    ) -> None:
        await self._import_workflow(
            interaction, file, mode, is_single_stage=False,
        )

    @pet_admin.command(
        name="import-stage",
        description="Import a single-stage pack — preview then press Confirm",
    )
    @app_commands.describe(
        file=".zip file",
        mode="create / update / replace",
    )
    async def import_stage(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        mode: Literal["create", "update", "replace"] = "create",
    ) -> None:
        await self._import_workflow(
            interaction, file, mode, is_single_stage=True,
        )

    # =========================================================================
    # Confirm / Cancel handlers
    # =========================================================================

    async def handle_confirm_import(
        self, interaction: discord.Interaction, job_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            job = await s.get(PetAssetImportJob, job_id)
            if job is None:
                await interaction.followup.send(
                    "❌ Job doesn't exist.", ephemeral=True
                )
                return
            if job.guild_id != interaction.guild_id:
                await interaction.followup.send(
                    "❌ Job belongs to another server.", ephemeral=True
                )
                return
            if job.status != "READY_TO_CONFIRM":
                await interaction.followup.send(
                    f"❌ Job is `{job.status}` — can't confirm.",
                    ephemeral=True,
                )
                return
            # Permission: uploader OR pet-admin (other than uploader)
            is_uploader = interaction.user.id == job.uploaded_by
            if not is_uploader and not await self._can_pet_admin(interaction):
                await interaction.followup.send(
                    "❌ Only the uploader (or a pet-admin) can Confirm.",
                    ephemeral=True,
                )
                return

            # Read the persisted zip
            zip_path = _temp_zip_path(job_id)
            if not zip_path.exists():
                job.status = "FAILED"
                job.errors_json = json.dumps(
                    ["Temp file is missing (bot restart). Re-run the command."],
                    ensure_ascii=False,
                )
                await s.commit()
                await interaction.followup.send(
                    "❌ Temp file is missing (bot may have restarted). "
                    "Re-upload with `/pet-admin import-pack`.",
                    ephemeral=True,
                )
                return
            data = zip_path.read_bytes()

            # Re-scan + import
            cfg = await self._get_cfg(interaction.guild_id)
            scan = scan_pack(
                data,
                is_single_stage=(job.mode == "stage"),  # we don't track this
                max_size_bytes=cfg.max_zip_size_mb * 1024 * 1024,
                max_files=cfg.max_files_per_zip,
            )
            # The mode field stores create/update/replace — we infer
            # single-stage from the original zip layout. Try full pack
            # first; if it errored out without finding stages, retry as
            # single-stage.
            if not scan.is_valid and any(
                "not found" in e.lower() or "stage" in e.lower()
                for e in scan.errors
            ):
                scan = scan_pack(
                    data, is_single_stage=True,
                    max_size_bytes=cfg.max_zip_size_mb * 1024 * 1024,
                    max_files=cfg.max_files_per_zip,
                )

            job.status = "IMPORTING"
            await s.commit()

            try:
                imported, failed = await perform_import(
                    s, self.storage, scan,
                    guild_id=interaction.guild_id,
                    uploaded_by=job.uploaded_by,
                    mode=job.mode,
                    job=job,
                )
                await s.commit()
            except Exception as e:
                log.exception("pet import: perform_import crashed")
                job.status = "FAILED"
                job.errors_json = json.dumps(
                    [f"Crash: {type(e).__name__}: {e}"], ensure_ascii=False,
                )
                job.completed_at = datetime.now(timezone.utc)
                await s.commit()
                await interaction.followup.send(
                    f"❌ Import crash: {e}", ephemeral=True,
                )
                # Best-effort cleanup
                try:
                    zip_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return

        # Cleanup temp zip
        try:
            zip_path.unlink(missing_ok=True)
        except Exception:
            log.exception("pet: failed to delete temp zip %s", zip_path)

        if job.status == "FAILED":
            errs = json.loads(job.errors_json or "[]")
            msg = "❌ Import failed:\n" + "\n".join(
                f"• {e}" for e in errs[:10]
            )
            try:
                await interaction.edit_original_response(
                    content=msg[:2000], embed=None, view=None,
                )
            except discord.HTTPException:
                pass
            return

        # Success embed (spec §18)
        embed = discord.Embed(
            title="✅ Pet imported successfully",
            color=discord.Color.green(),
        )
        embed.description = (
            f"**Pet:** {scan.display_name or scan.species_id}\n"
            f"**Species ID:** `{scan.species_id}`\n"
            f"**Stages:** {len(set(p.stage for p in scan.planned))}/4\n"
            f"**Assets imported:** {imported}\n"
            + (f"**Failed:** {failed}\n" if failed else "")
            + "**Shop:** ❌ Disabled\n\n"
            "**Next steps:**\n"
            f"1. `/pet-admin species-config species_id:{scan.species_id} "
            "rarity:... price:...`\n"
            f"2. `/pet-admin species-enable species_id:{scan.species_id}`"
        )
        try:
            await interaction.edit_original_response(
                embed=embed, view=None, content=None,
            )
        except discord.HTTPException:
            pass

    async def handle_cancel_import(
        self, interaction: discord.Interaction, job_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            job = await s.get(PetAssetImportJob, job_id)
            if job is None:
                await interaction.followup.send(
                    "❌ Job doesn't exist.", ephemeral=True
                )
                return
            if job.guild_id != interaction.guild_id:
                await interaction.followup.send(
                    "❌ Job belongs to another server.", ephemeral=True
                )
                return
            is_uploader = interaction.user.id == job.uploaded_by
            if not is_uploader and not await self._can_pet_admin(interaction):
                await interaction.followup.send(
                    "❌ Only the uploader (or a pet-admin) can Cancel.",
                    ephemeral=True,
                )
                return
            if job.status != "READY_TO_CONFIRM":
                await interaction.followup.send(
                    f"❌ Job is `{job.status}` — can't cancel.",
                    ephemeral=True,
                )
                return
            job.status = "CANCELLED"
            job.completed_at = datetime.now(timezone.utc)
            await s.commit()

        # Cleanup temp
        try:
            _temp_zip_path(job_id).unlink(missing_ok=True)
        except Exception:
            pass

        try:
            await interaction.edit_original_response(
                content=f"🛑 Import #{job_id} cancelled.", embed=None, view=None,
            )
        except discord.HTTPException:
            pass


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(PetAdmin(bot))
