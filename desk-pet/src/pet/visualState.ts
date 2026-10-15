// Map between the bot's "currentMood" buckets (sick/hungry/.../happy/
// normal) and the desktop animation states.
//
// Action animations live separately — when the user clicks Feed/Drink/
// Play/Clean/Sleep/Pet, the renderer plays a one-shot animation
// (eat/drink/play/idle/sleep/petting) before returning to the
// idle/mood state.

import { PetMood, ActionKind } from "../api/petApi";
import { PetAnimationState } from "./manifest";

const MOOD_TO_ANIM: Record<PetMood, PetAnimationState> = {
  sick:    "sick",
  hungry:  "sad",
  thirsty: "sad",
  dirty:   "sad",
  sleepy:  "sleep",
  sad:     "sad",
  lonely:  "sad",
  happy:   "happy",
  normal:  "idle",
};

const ACTION_TO_ANIM: Record<ActionKind, PetAnimationState> = {
  feed:  "eat",
  drink: "drink",
  play:  "play",
  // The OpenPets sheet has no dedicated bath/groom row; the "waving"
  // pose (row 3) reads as a paw-scrub, so clean + pet both use it.
  clean: "petting",
  sleep: "sleep",
  pet:   "petting",
  // Training → the "running in place" pose (row 7) reads as exercise.
  train: "train",
};

export function moodAnimation(mood: PetMood): PetAnimationState {
  return MOOD_TO_ANIM[mood] || "idle";
}

export function actionAnimation(action: ActionKind): PetAnimationState {
  return ACTION_TO_ANIM[action];
}

// If a pet asset doesn't include a particular animation, fall back to
// the closest available one (SPEC §8). Walk the list in order; first
// hit wins. Always terminates because "idle" is mandatory.
const FALLBACK_CHAIN: Record<PetAnimationState, PetAnimationState[]> = {
  idle:      ["idle"],
  walkRight: ["walkRight", "walkLeft", "idle"],
  walkLeft:  ["walkLeft", "walkRight", "idle"],
  happy:     ["happy", "idle"],
  sad:       ["sad", "idle"],
  eat:       ["eat", "happy", "idle"],
  drink:     ["drink", "eat", "happy", "idle"],
  play:      ["play", "jump", "happy", "idle"],
  sleep:     ["sleep", "idle"],
  petting:   ["petting", "happy", "idle"],
  jump:      ["jump", "play", "happy", "idle"],
  train:     ["train", "play", "jump", "idle"],
  sick:      ["sick", "sad", "idle"],
};

export function resolveAnimation(
  target: PetAnimationState,
  availableRows: Set<PetAnimationState>,
): PetAnimationState {
  for (const candidate of FALLBACK_CHAIN[target]) {
    if (availableRows.has(candidate)) return candidate;
  }
  return "idle";
}

// --- Per-state grid sheets (newer art model) ------------------------------

// Map an animation state to a state-sheet name (idle/move/eat/...). The
// sheets cover the core interactions; moods without their own sheet reuse
// a sensible one so the art style stays consistent (one white cat, never
// falling back to the OpenPets brown cat once state sheets exist).
const ANIM_TO_STATE: Record<PetAnimationState, string> = {
  idle:      "idle",
  walkRight: "move",
  walkLeft:  "move",
  eat:       "eat",
  drink:     "drink",
  play:      "play",
  sleep:     "sleep",
  train:     "train",
  petting:   "clean", // clean action + vuốt ve both use the groom pose
  happy:     "idle",
  sad:       "idle",
  jump:      "play",
  sick:      "idle",
};

/** Pick the state-sheet name for an animation, falling back within the
 * available sheets (→ idle → move → first available). Returns null when
 * the asset has no state sheets at all. */
export function resolveStateSheet(
  anim: PetAnimationState,
  available: Set<string>,
): string | null {
  if (available.size === 0) return null;
  const want = ANIM_TO_STATE[anim];
  if (available.has(want)) return want;
  for (const fb of ["idle", "move"]) {
    if (available.has(fb)) return fb;
  }
  return available.values().next().value ?? null;
}
