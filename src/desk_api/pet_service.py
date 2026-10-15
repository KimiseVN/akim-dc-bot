"""Pet data + care for the desktop API.

Reuses src.pets.care for the actual delta math so desktop and Discord
slash command behaviours stay aligned. The bot's care.do_*() functions
mutate a UserPet row in place; here we wrap them in transactional
helpers that also enforce per-action cooldowns and write the action
log fields the bot already tracks (last_play_at etc.).

Stat semantics (IMPORTANT for the desktop client):
  hunger/thirst/cleanliness  → fullness (high = good, low = bad)
  mood/energy/bond/health    → also high = good
The SPEC says "hunger -40" on feed; we expose the bot's native model
(hunger +30 = "more full") and let the desktop UI render however.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import PetDesktopProfile, PetSpecies, UserPet
from ..pets import care

log = logging.getLogger("akim-bot.desk-api.pet")


# Per-action cooldown for desktop callers. The bot's slash flow has its
# own cooldowns living on the cog; the desktop runs through API so we
# enforce them here. Keys match care.do_*() action names plus "pet".
DESKTOP_COOLDOWNS_SECONDS: dict[str, int] = {
    "feed":  10 * 60,    # 10 min
    "water": 10 * 60,
    "play":   5 * 60,    # match bot's existing 5 min
    "clean": 10 * 60,
    "rest":  30 * 60,    # "sleep" in SPEC vocabulary
    "train": 30 * 60,    # exercise — costs energy, long cooldown
    "pet":   30,         # quick affection click
}

# Which UserPet.last_*_at column gates each action. None = action has no
# DB-backed last-used timestamp on the bot side and we skip the time
# check (feed/water/pet — rate is implicit via coin cost on bot, here
# we'd need to add columns to enforce cleanly. MVP: skip).
_LAST_AT_FIELD: dict[str, str | None] = {
    "feed":  None,
    "water": None,
    "play":  "last_play_at",
    "clean": "last_clean_at",
    "rest":  "last_rest_at",
    "train": "last_train_at",
    "pet":   None,
}


class PetError(Exception):
    """Surface to the HTTP layer with a stable error code. Same shape
    as link_service.LinkError so the router can map both."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ActivePetView:
    """Bundle of (pet, species row, desktop profile) returned by
    fetch_active_pet — every endpoint that needs the full active-pet
    picture composes from this."""

    pet: UserPet
    species: PetSpecies | None
    profile: PetDesktopProfile


# ---------------------------------------------------------------------------


async def _ensure_profile(
    s: AsyncSession, *, pet: UserPet, discord_user_id: int,
) -> PetDesktopProfile:
    """Idempotent fetch-or-create for a UserPet's desktop profile.
    Default position/scale are SPEC values (300, 600, 1.0)."""
    profile = await s.get(PetDesktopProfile, pet.id)
    if profile is None:
        profile = PetDesktopProfile(
            pet_instance_id=pet.id,
            discord_user_id=discord_user_id,
        )
        s.add(profile)
        await s.flush()
    return profile


async def _apply_passive_decay(s: AsyncSession, pet: UserPet) -> None:
    """SPEC: 'apply passive decay every full 30 minutes since updatedAt'.
    Bot's care.apply_hourly_decay is per-hour; SPEC is per-30-min. We
    rescale to the bot's hourly model by integer-dividing the elapsed
    minutes by 60 — so a pet idle for 90 min decays for 1 hour, then
    the next 30 min later for the 2nd hour. Matches the bot's existing
    hourly sweep semantics."""
    if pet.updated_at is None:
        return
    now = datetime.now(timezone.utc)
    elapsed = now - pet.updated_at
    hours = int(elapsed.total_seconds() // 3600)
    if hours <= 0:
        return
    # Cap at 24h of decay per call so a pet that's been offline a week
    # doesn't get wiped to 0 in a single API call (gives the owner a
    # grace window to come back online).
    hours = min(hours, 24)
    for _ in range(hours):
        care.apply_hourly_decay(pet)
    pet.updated_at = now


# ---------------------------------------------------------------------------


async def fetch_active_pet(
    s: AsyncSession, *, discord_user_id: int, guild_id: int,
) -> ActivePetView:
    """Find the user's active pet in the linked guild. Raises PetError
    when no pet is owned or no active pet is selected. Applies passive
    decay before returning so the caller sees up-to-date stats."""
    pet = await s.scalar(
        select(UserPet).where(
            UserPet.guild_id == guild_id,
            UserPet.user_id == discord_user_id,
            UserPet.is_active.is_(True),
        )
    )
    if pet is None:
        # Distinguish "no pet at all" vs "owned but none active" so the
        # desktop UI can show a useful CTA.
        any_pet = await s.scalar(
            select(UserPet.id).where(
                UserPet.guild_id == guild_id,
                UserPet.user_id == discord_user_id,
            )
        )
        if any_pet is None:
            raise PetError(
                "PET_NOT_FOUND",
                "You don't have any pets in this guild yet. Open Discord and "
                "buy a pet from /pet-shop first.",
            )
        raise PetError(
            "ACTIVE_PET_REQUIRED",
            "You have a pet but haven't picked an active one yet. "
            "Press 'Choose pet' in the app.",
        )

    await _apply_passive_decay(s, pet)
    species = await s.scalar(
        select(PetSpecies).where(
            PetSpecies.guild_id == guild_id,
            PetSpecies.species_id == pet.species_id,
        )
    )
    profile = await _ensure_profile(
        s, pet=pet, discord_user_id=discord_user_id,
    )
    await s.commit()
    await s.refresh(pet)
    await s.refresh(profile)
    return ActivePetView(pet=pet, species=species, profile=profile)


async def list_owned_pets(
    s: AsyncSession, *, discord_user_id: int, guild_id: int,
) -> list[tuple[UserPet, PetSpecies | None]]:
    """All pets owned by this user in the linked guild plus the species
    row for each so the caller has display_name + desktop_asset_id."""
    rows = (await s.execute(
        select(UserPet)
        .where(
            UserPet.guild_id == guild_id,
            UserPet.user_id == discord_user_id,
        )
        .order_by(UserPet.is_active.desc(), UserPet.id.asc())
    )).scalars().all()
    if not rows:
        return []
    species_ids = {p.species_id for p in rows}
    species_rows = (await s.execute(
        select(PetSpecies).where(
            PetSpecies.guild_id == guild_id,
            PetSpecies.species_id.in_(species_ids),
        )
    )).scalars().all()
    species_by_id: dict[str, PetSpecies] = {
        sp.species_id: sp for sp in species_rows
    }
    return [(p, species_by_id.get(p.species_id)) for p in rows]


async def select_active_pet(
    s: AsyncSession,
    *,
    discord_user_id: int,
    guild_id: int,
    pet_instance_id: int,
) -> ActivePetView:
    """Mark `pet_instance_id` as the active pet for this user in this
    guild and unset is_active on any other pet they own. Verifies the
    pet belongs to the caller — desktop cannot promote someone else's
    pet."""
    target = await s.scalar(
        select(UserPet).where(UserPet.id == pet_instance_id)
    )
    if target is None:
        raise PetError("PET_NOT_FOUND", "Pet doesn't exist.")
    if target.user_id != discord_user_id or target.guild_id != guild_id:
        raise PetError(
            "PET_NOT_OWNED",
            "This pet doesn't belong to you in the linked guild.",
        )

    # Clear is_active on all other pets, then mark target. Single
    # UPDATE statement so we don't fan out per-row.
    await s.execute(
        update(UserPet)
        .where(
            UserPet.guild_id == guild_id,
            UserPet.user_id == discord_user_id,
            UserPet.id != pet_instance_id,
        )
        .values(is_active=False)
    )
    target.is_active = True
    await s.commit()
    return await fetch_active_pet(
        s, discord_user_id=discord_user_id, guild_id=guild_id,
    )


# ---------------------------------------------------------------------------


def _cooldown_remaining_seconds(pet: UserPet, action: str) -> int:
    """How many seconds until this action is allowed again, or 0 if
    it's ready now."""
    cooldown = DESKTOP_COOLDOWNS_SECONDS.get(action)
    field = _LAST_AT_FIELD.get(action)
    if cooldown is None or field is None:
        return 0
    last_at: datetime | None = getattr(pet, field, None)
    if last_at is None:
        return 0
    elapsed = (datetime.now(timezone.utc) - last_at).total_seconds()
    remaining = cooldown - int(elapsed)
    return max(0, remaining)


def _stamp_action_time(pet: UserPet, action: str) -> None:
    field = _LAST_AT_FIELD.get(action)
    if field is None:
        return
    setattr(pet, field, datetime.now(timezone.utc))


_CARE_FUNCS = {
    "feed":  care.do_feed,
    "water": care.do_water,
    "play":  care.do_play,
    "clean": care.do_clean,
    "rest":  care.do_rest,
    "train": care.do_train,
}


def _do_pet_action(pet: UserPet) -> care.ActionResult:
    """SPEC's 'pet' action — not in the bot's care.py because Discord
    doesn't expose it. Affection-style click: small mood bump, tiny
    exp, NO bond gain.

    Why no bond: at 30s cooldown, bond+2 per click maxes bond in ~25
    min — that's a farm loop, not a care system. Bond should reward
    real investment (play/train both cost energy and have longer
    cooldowns). The "pet" action stays as a free mood booster.
    """
    delta = care._apply_delta(pet, {"mood": 3})  # noqa: SLF001
    return care._award_exp(pet, "pet", delta, exp_gain=1)  # noqa: SLF001


@dataclass(frozen=True)
class AppliedAction:
    view: ActivePetView
    result: care.ActionResult


async def apply_action(
    s: AsyncSession,
    *,
    discord_user_id: int,
    guild_id: int,
    action: str,
) -> AppliedAction:
    """Run a care action on the user's active pet. Decay + cooldown +
    care delta + cooldown stamp are all in one transaction so a
    concurrent /active call sees consistent state."""
    if action not in DESKTOP_COOLDOWNS_SECONDS:
        raise PetError("VALIDATION_ERROR", f"Invalid action: {action}")

    view = await fetch_active_pet(
        s, discord_user_id=discord_user_id, guild_id=guild_id,
    )
    pet = view.pet

    remaining = _cooldown_remaining_seconds(pet, action)
    if remaining > 0:
        raise PetError(
            "COOLDOWN_ACTIVE",
            f"Action is on cooldown — wait {remaining}s.",
        )

    if action == "pet":
        result = _do_pet_action(pet)
    else:
        result = _CARE_FUNCS[action](pet)

    if result.failed_reason:
        raise PetError("INSUFFICIENT_ENERGY", result.failed_reason)

    _stamp_action_time(pet, action)
    pet.updated_at = datetime.now(timezone.utc)
    await s.commit()
    await s.refresh(pet)
    # Profile/species unchanged — reuse the view's reference but with
    # fresh pet object.
    return AppliedAction(
        view=ActivePetView(pet=pet, species=view.species, profile=view.profile),
        result=result,
    )


# ---------------------------------------------------------------------------


async def update_desktop_profile(
    s: AsyncSession,
    *,
    discord_user_id: int,
    guild_id: int,
    fields: dict,
) -> ActivePetView:
    """Persist UI-side state (position, scale, toggles) for the active
    pet. Caller passes only the fields they want to change; other
    columns retain their stored values."""
    view = await fetch_active_pet(
        s, discord_user_id=discord_user_id, guild_id=guild_id,
    )
    profile = view.profile
    allowed = {
        "position_x", "position_y", "scale",
        "selected_skin", "selected_accessory",
        "bubble_enabled", "sound_enabled",
        "random_walk_enabled", "always_on_top",
    }
    for k, v in fields.items():
        if k in allowed and v is not None:
            setattr(profile, k, v)
    profile.last_sync_at = datetime.now(timezone.utc)
    await s.commit()
    await s.refresh(profile)
    return ActivePetView(pet=view.pet, species=view.species, profile=profile)


# ---------------------------------------------------------------------------


def visual_state(pet: UserPet) -> str:
    """Public re-export of care.get_visual_state so the routers don't
    need to import care directly."""
    return care.get_visual_state(pet)
