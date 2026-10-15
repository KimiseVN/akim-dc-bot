"""Asset function-key → (asset_type, state_key) classifier.

Direct port of spec §12.2. Each manifest entry declares `function`
like "moon_cat_baby_base"; the classifier:

  1. strips the {species_id}_{stage}_ prefix
  2. looks the remaining key up in CLASSIFICATION_MAP
  3. returns canonical (asset_type, state_key) — aliases collapse here

Unknown keys map to ("unknown", <original_key>) so the importer can
flag them as warnings without crashing.
"""

from __future__ import annotations


# Canonical map per spec §12.2. Aliases collapse multiple synonyms
# into the same (asset_type, state_key) — e.g. "level_up" and
# "level_up_effect" both become ("effect", "level_up").
CLASSIFICATION_MAP: dict[str, tuple[str, str]] = {
    # base
    "base": ("base", "base"),

    # 01_base_moods_effects — moods
    "mood_normal":   ("mood", "normal"),
    "mood_happy":    ("mood", "happy"),
    "mood_sad":      ("mood", "sad"),
    "mood_hungry":   ("mood", "hungry"),
    "mood_sleepy":   ("mood", "sleepy"),
    "mood_sick":     ("mood", "sick"),

    # 01_base_moods_effects — effects (alias group)
    "level_up":          ("effect", "level_up"),
    "level_up_effect":   ("effect", "level_up"),
    "evolve":            ("effect", "evolve"),
    "evolution_effect":  ("effect", "evolve"),

    # 01_base_moods_effects — portrait
    "portrait": ("portrait", "portrait"),

    # 02_interaction_pack — feed
    "feed_food":      ("interaction", "feed"),
    "feed_star_food": ("interaction", "feed"),

    # 02_interaction_pack — drink
    "drink_water":      ("interaction", "drink"),
    "drink_moon_water": ("interaction", "drink"),

    # 02_interaction_pack — play (alias group)
    "play":           ("interaction", "play"),
    "play_star_ball": ("interaction", "play"),
    "play_moon_orb":  ("interaction", "play"),
    "play_lunar_orb": ("interaction", "play"),

    # 02_interaction_pack — pet_touch
    "pet_touch": ("interaction", "pet_touch"),

    # 02_interaction_pack — clean (alias group)
    "clean":            ("interaction", "clean"),
    "clean_brush":      ("interaction", "clean"),
    "grooming":         ("interaction", "clean"),
    "cleansing_magic":  ("interaction", "clean"),

    # 02_interaction_pack — rest
    "rest":          ("interaction", "rest"),
    "rest_on_cloud": ("interaction", "rest"),

    # 02_interaction_pack — energy / love / training / explore
    "low_energy": ("interaction", "low_energy"),
    "love":       ("interaction", "love"),
    "love_bond":  ("interaction", "love"),

    "training":                  ("interaction", "train"),
    "training_magic":            ("interaction", "train"),
    "training_celestial_magic":  ("interaction", "train"),

    "explore":           ("interaction", "explore"),
    "patrol_explore":    ("interaction", "explore"),
    "patrol_starlight":  ("interaction", "explore"),

    # 02_interaction_pack — icons
    "coin_shop_icon":  ("icon", "shop"),
    "inventory_icon":  ("icon", "inventory"),

    # 03_care_state_pack — full set
    "full":      ("care_state", "full"),
    "thirsty":   ("care_state", "thirsty"),
    "dirty":     ("care_state", "dirty"),
    "bored":     ("care_state", "bored"),
    "lonely":    ("care_state", "lonely"),
    "scared":    ("care_state", "scared"),
    "angry":     ("care_state", "angry"),
    "surprised": ("care_state", "surprised"),
    "confused":  ("care_state", "confused"),
    "proud":     ("care_state", "proud"),

    # 04_reward_reaction_pack — core
    "reward_happy":  ("reward_reaction", "reward_happy"),
    "coin_gain":     ("reward_reaction", "coin_gain"),
    "exp_gain":      ("reward_reaction", "exp_gain"),
    "gift_open":     ("reward_reaction", "gift_open"),
    "daily_checkin": ("reward_reaction", "daily_checkin"),
    "trust_up":      ("reward_reaction", "trust_up"),
    "skill_unlock":  ("reward_reaction", "skill_unlock"),

    # 04_reward_reaction_pack — magic alias group
    "small_magic":      ("reward_reaction", "magic"),
    "moon_magic":       ("reward_reaction", "magic"),
    "celestial_magic":  ("reward_reaction", "magic"),

    # 04_reward_reaction_pack — victory alias group
    "dance":         ("reward_reaction", "victory"),
    "victory_pose":  ("reward_reaction", "victory"),

    # 04_reward_reaction_pack — fail
    "fail_reaction": ("reward_reaction", "fail"),
}


def parse_asset_function(
    function_key: str, species_id: str, stage: str,
) -> tuple[str, str]:
    """Strip the {species_id}_{stage}_ prefix and classify the
    remaining key. Returns (asset_type, state_key).

    Raises ValueError if the prefix doesn't match — the function key
    is supposed to be self-describing per spec §10.6.
    """
    prefix = f"{species_id}_{stage}_"
    if not function_key.startswith(prefix):
        raise ValueError(
            f"function `{function_key}` is missing prefix `{prefix}` "
            f"(expected `{species_id}_{stage}_<state>`)"
        )
    key = function_key[len(prefix):]
    return CLASSIFICATION_MAP.get(key, ("unknown", key))
