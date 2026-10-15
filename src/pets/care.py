"""Pet care system: visual state, decay schedule, stage/level math.

The Pet cog mutates UserPet fields directly; this module is pure
helpers + constants so the cog stays focused on Discord plumbing.

Decay model (per hour, active pets only):
  hunger      -2
  thirst      -3   (faster than hunger — drink water more often)
  cleanliness -1
  mood        -1   (extra -2 if any of hunger/thirst/cleanliness hits 0)
  health      -2   (only when at least one above is at 0)
              +2   (regen when the pet is well-cared-for: all of
                   hunger/thirst/cleanliness ≥ 50). Caps at 100.
  energy      ±0   (only changes via actions: play/train down, rest up)
  bond        ±0   (only goes up via interactions, never decays)

Action effects (clamped 0..100):
  feed   hunger +30,  mood +5,   energy +5
  water  thirst +30,  mood +2
  play   mood +20,    energy -10, bond +2
  clean  cleanliness +30, mood +5
  rest   energy +40,  mood +5,   health +8    (naps heal — main
                                              recovery action)
  train  exp +random(15,30), energy -25, hunger -10, bond +1
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..db.models import UserPet


# Visual state buckets (spec §21.1). Most important condition wins;
# evaluation order matters.
def get_visual_state(pet: UserPet) -> str:
    if pet.health <= 30:
        return "sick"
    if pet.hunger <= 25:
        return "hungry"
    if pet.thirst <= 25:
        return "thirsty"
    if pet.cleanliness <= 25:
        return "dirty"
    if pet.energy <= 20:
        return "sleepy"
    if pet.mood <= 30:
        return "sad"
    if pet.bond <= 20:
        return "lonely"
    if pet.mood >= 80:
        return "happy"
    return "normal"


# Spec §22 — stage thresholds.
STAGE_ORDER: tuple[str, ...] = ("baby", "teen", "adult", "mystic")
STAGE_MIN_LEVEL: dict[str, int] = {
    "baby": 1,
    "teen": 10,
    "adult": 25,
    "mystic": 40,
}

# Per-stage hourly passive income. Granted by the hourly decay loop to
# the user's *active* pet only, capped by STAGE_DAILY_INCOME_CAP so a
# perma-active Mystic can't print coin forever. Bond / health damage
# does not reduce the grant — feature 1's intent is "owning is
# rewarding" not "punish neglect twice".
STAGE_HOURLY_INCOME: dict[str, int] = {
    "baby":   1,
    "teen":   2,
    "adult":  3,
    "mystic": 5,
}
STAGE_DAILY_INCOME_CAP: dict[str, int] = {
    "baby":   20,
    "teen":   40,
    "adult":  60,
    "mystic": 100,
}

# Minigame reward buff applied via PetBuffService when the active pet
# is healthy. Bonus is rounded down — the helper returns an int.
STAGE_REWARD_BUFF_PCT: dict[str, int] = {
    "baby":   5,
    "teen":   8,
    "adult":  12,
    "mystic": 20,
}

# Stat that each action primarily refills. Used by the Pet cog's
# stat-full check to reject the action when that stat is already at
# 100/100 — prevents farming bond / EXP by re-applying an action whose
# only real effect now is the bonus stats (bond, mood +5, etc.).
#
# train + rest deliberately have no entry:
#   • train never refills a care stat (it consumes energy + hunger
#     for EXP); its 30 min cooldown already caps its rate.
#   • rest is the main health-recovery action. Blocking it at full
#     energy would leave a sick pet with no way to heal via naps
#     (energy doesn't decay). Its 30 min cooldown caps the +8
#     health/nap, so at worst a maxed-energy pet can nap ~48x/day
#     for +192 exp — not a farm loop.
ACTION_PRIMARY_STAT: dict[str, str] = {
    "feed":  "hunger",
    "water": "thirst",
    "play":  "mood",
    "clean": "cleanliness",
}


def calculate_stage(level: int) -> str:
    if level >= STAGE_MIN_LEVEL["mystic"]:
        return "mystic"
    if level >= STAGE_MIN_LEVEL["adult"]:
        return "adult"
    if level >= STAGE_MIN_LEVEL["teen"]:
        return "teen"
    return "baby"


def next_stage_and_level(current_stage: str) -> tuple[str | None, int | None]:
    """Return (next_stage_name, level_required) — or (None, None) when
    pet has already hit mystic."""
    try:
        idx = STAGE_ORDER.index(current_stage)
    except ValueError:
        return None, None
    if idx + 1 >= len(STAGE_ORDER):
        return None, None
    nxt = STAGE_ORDER[idx + 1]
    return nxt, STAGE_MIN_LEVEL[nxt]


def exp_for_next_level(current_level: int) -> int:
    """100 EXP at L1→2, scaling linearly. Tweakable later."""
    return 100 * current_level


def clamp_0_100(n: int) -> int:
    return max(0, min(100, int(n)))


# ----- decay -----


def apply_hourly_decay(pet: UserPet) -> None:
    """Mutate the UserPet row in-place per the model above. Safe to
    call N times for N hours of elapsed time — the caller decides
    how many hours have passed since the last sweep.

    Health rules:
      • -2/hr while any of hunger/thirst/cleanliness is at 0 (the
        pet is starving/dehydrated/filthy).
      • +2/hr while ALL of hunger/thirst/cleanliness are ≥ 50 (the
        pet is well-cared-for and passively recovers). Caps at 100.
      • No change in between — you have to actively care for the pet
        (feed/water/clean/rest) for health to move.
    """
    pet.hunger = clamp_0_100(pet.hunger - 2)
    pet.thirst = clamp_0_100(pet.thirst - 3)
    pet.cleanliness = clamp_0_100(pet.cleanliness - 1)
    pet.mood = clamp_0_100(pet.mood - 1)
    starving_or_filthy = (
        pet.hunger <= 0 or pet.thirst <= 0 or pet.cleanliness <= 0
    )
    if starving_or_filthy:
        pet.health = clamp_0_100(pet.health - 2)
        pet.mood = clamp_0_100(pet.mood - 2)
    elif (
        pet.hunger >= 50
        and pet.thirst >= 50
        and pet.cleanliness >= 50
        and pet.health < 100
    ):
        # Well-cared-for pets slowly heal on their own. Encourages
        # topping stats up above the 50 line instead of letting them
        # drift toward 0.
        pet.health = clamp_0_100(pet.health + 2)


# ----- actions -----


@dataclass
class ActionResult:
    """Aggregate of one care action so the cog can both update DB and
    show the user a delta-summary embed."""
    action: str
    delta: dict[str, int]
    exp_gained: int = 0
    level_up: bool = False
    new_stage: str | None = None  # set when evolved during this action
    failed_reason: str | None = None  # set when action couldn't apply


def _apply_delta(pet: UserPet, delta: dict[str, int]) -> dict[str, int]:
    """Apply a delta dict to the pet, returning the actual change
    after clamping (so the user-facing embed shows real numbers,
    not '+30' when only +5 happened due to the 100 cap)."""
    actual: dict[str, int] = {}
    for field, amount in delta.items():
        before = getattr(pet, field)
        after = clamp_0_100(before + amount)
        actual[field] = after - before
        setattr(pet, field, after)
    return actual


def do_feed(pet: UserPet) -> ActionResult:
    delta = _apply_delta(pet, {"hunger": 30, "mood": 5, "energy": 5})
    return _award_exp(pet, "feed", delta, exp_gain=3)


def do_water(pet: UserPet) -> ActionResult:
    delta = _apply_delta(pet, {"thirst": 30, "mood": 2})
    return _award_exp(pet, "water", delta, exp_gain=2)


def do_play(pet: UserPet) -> ActionResult:
    if pet.energy < 10:
        return ActionResult(
            "play", {}, failed_reason="Pet is too exhausted to play (energy < 10)",
        )
    delta = _apply_delta(pet, {"mood": 20, "energy": -10, "bond": 2})
    return _award_exp(pet, "play", delta, exp_gain=8)


def do_clean(pet: UserPet) -> ActionResult:
    delta = _apply_delta(pet, {"cleanliness": 30, "mood": 5})
    return _award_exp(pet, "clean", delta, exp_gain=3)


def do_rest(pet: UserPet) -> ActionResult:
    # Rest is the pet's main active recovery lever. Health +8 gives
    # the owner a concrete way to heal a sick pet (visual_state
    # "sick" triggers at health ≤ 30, so 4 naps clear it if nothing
    # else is starving). Combined with the passive regen in
    # apply_hourly_decay, a well-cared-for sick pet recovers in a
    # reasonable window without the owner having to grind cooldowns.
    delta = _apply_delta(pet, {"energy": 40, "mood": 5, "health": 8})
    return _award_exp(pet, "rest", delta, exp_gain=4)


def do_train(pet: UserPet) -> ActionResult:
    if pet.energy < 25:
        return ActionResult(
            "train", {}, failed_reason="Pet is too exhausted to train (energy < 25)",
        )
    exp = random.randint(15, 30)
    delta = _apply_delta(
        pet, {"energy": -25, "hunger": -10, "bond": 1},
    )
    return _award_exp(pet, "train", delta, exp_gain=exp)


def _award_exp(
    pet: UserPet, action: str, delta: dict[str, int], *, exp_gain: int,
) -> ActionResult:
    """Apply EXP gain + handle level-up + stage evolution. Shared by
    every action that contributes XP — care actions give small amounts
    (3-8), train gives 15-30."""
    pet.exp += exp_gain
    leveled_up = False
    new_stage: str | None = None
    safety = 0
    while pet.exp >= exp_for_next_level(pet.level) and safety < 50:
        pet.exp -= exp_for_next_level(pet.level)
        pet.level += 1
        leveled_up = True
        next_stage = calculate_stage(pet.level)
        if next_stage != pet.stage:
            pet.stage = next_stage
            new_stage = next_stage
        safety += 1
    return ActionResult(
        action, delta,
        exp_gained=exp_gain, level_up=leveled_up, new_stage=new_stage,
    )
