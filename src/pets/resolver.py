"""Pet image resolver (spec §20).

Given a pet's (species, stage) and an optional action/visual-state,
pick the most appropriate PetAsset row to show. Priority per §20:

  1. action image          (interaction/effect/reward_reaction)
  2. care_state image      (hungry, thirsty, dirty, ...)
  3. mood image            (happy, sad, normal, ...)
  4. base image
  5. portrait image
  6. None — caller must handle (default placeholder)

Shop display has its own priority (spec §23):
  icon/shop → portrait → mood/normal → base
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import PetAsset


# Asset-type buckets to try when an action key is supplied. An action
# like "feed" lives under "interaction"; level_up lives under "effect";
# coin_gain lives under "reward_reaction". The resolver tries each in
# this order so a single action string can resolve from any of them.
_ACTION_BUCKETS: tuple[str, ...] = (
    "interaction",
    "effect",
    "reward_reaction",
    "icon",
)


async def _fetch_asset(
    session: AsyncSession,
    guild_id: int, species_id: str, stage: str,
    asset_type: str, state_key: str,
) -> PetAsset | None:
    return await session.scalar(
        select(PetAsset).where(
            PetAsset.guild_id == guild_id,
            PetAsset.species_id == species_id,
            PetAsset.stage == stage,
            PetAsset.asset_type == asset_type,
            PetAsset.state_key == state_key,
            PetAsset.is_active.is_(True),
        )
    )


async def resolve_pet_image(
    session: AsyncSession,
    *,
    guild_id: int,
    species_id: str,
    stage: str,
    visual_state: str | None = None,
    action: str | None = None,
) -> PetAsset | None:
    """Main resolver. Returns the best-matching PetAsset row or None
    when even the portrait fallback is missing."""
    # 1. action — try each asset-type bucket
    if action:
        for bucket in _ACTION_BUCKETS:
            asset = await _fetch_asset(
                session, guild_id, species_id, stage, bucket, action,
            )
            if asset is not None:
                return asset

    # 2. care_state (hungry, thirsty, dirty, etc.)
    if visual_state:
        asset = await _fetch_asset(
            session, guild_id, species_id, stage, "care_state", visual_state,
        )
        if asset is not None:
            return asset
        # 3. mood (happy, sad, normal, ...)
        asset = await _fetch_asset(
            session, guild_id, species_id, stage, "mood", visual_state,
        )
        if asset is not None:
            return asset

    # 4. base
    asset = await _fetch_asset(
        session, guild_id, species_id, stage, "base", "base",
    )
    if asset is not None:
        return asset

    # 5. portrait
    return await _fetch_asset(
        session, guild_id, species_id, stage, "portrait", "portrait",
    )


async def resolve_shop_image(
    session: AsyncSession,
    *,
    guild_id: int,
    species_id: str,
    stage: str = "baby",
) -> PetAsset | None:
    """Shop card image (spec §23): icon/shop → portrait → mood/normal → base."""
    candidates: tuple[tuple[str, str], ...] = (
        ("icon", "shop"),
        ("portrait", "portrait"),
        ("mood", "normal"),
        ("base", "base"),
    )
    for asset_type, state_key in candidates:
        asset = await _fetch_asset(
            session, guild_id, species_id, stage, asset_type, state_key,
        )
        if asset is not None:
            return asset
    return None
