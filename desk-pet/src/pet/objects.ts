// Action → spawned object. When a care action fires, PetView spawns a
// little prop next to the pet for the duration of the action animation
// (a food bowl when feeding, a ball when playing, hearts when petting,
// …). Emoji-based for the MVP — no extra assets to bundle — but the
// shape is ready to swap each entry for a sprite later.

import { ActionKind } from "../api/petApi";

export type ActionObjectMotion = "drop" | "bounce" | "float";
export type ActionObjectAnchor = "feet" | "head";

export type ActionObjectDef = {
  emoji: string;
  // How it enters: drop = falls in from above (bowls), bounce = hops on
  // the ground (toys), float = drifts upward and fades (hearts / Zzz).
  motion: ActionObjectMotion;
  // Where it sits relative to the sprite — on the ground by the pet's
  // feet, or up by its head.
  anchor: ActionObjectAnchor;
  label: string;
};

const OBJECTS: Record<ActionKind, ActionObjectDef> = {
  feed:  { emoji: "🍖", motion: "drop",   anchor: "feet", label: "Đồ ăn" },
  drink: { emoji: "🥣", motion: "drop",   anchor: "feet", label: "Bát nước" },
  play:  { emoji: "🎾", motion: "bounce", anchor: "feet", label: "Đồ chơi" },
  clean: { emoji: "🫧", motion: "float",  anchor: "head", label: "Bong bóng" },
  sleep: { emoji: "💤", motion: "float",  anchor: "head", label: "Ngủ" },
  train: { emoji: "💪", motion: "bounce", anchor: "head", label: "Tập luyện" },
  pet:   { emoji: "💗", motion: "float",  anchor: "head", label: "Yêu thương" },
};

export function actionObject(action: ActionKind): ActionObjectDef {
  return OBJECTS[action];
}
