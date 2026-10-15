// Speech-bubble copy. Picked from a pool per visual state so the pet
// doesn't always say the exact same line. Action bubbles override
// the mood bubble for the duration of the action animation.

import { PetMood, ActionKind } from "../api/petApi";

const MOOD_LINES: Record<PetMood, string[]> = {
  sick: [
    "Tôi không khoẻ lắm...",
    "Mệt quá... cho tôi nghỉ tí.",
    "Hắt xì! Tôi đang ốm rồi.",
  ],
  hungry: [
    "Bụng đang réo ò ò...",
    "Cho tôi ăn gì với!",
    "Đói meo rồi đó.",
  ],
  thirsty: [
    "Khát nước quá...",
    "Tôi cần nước.",
    "Một ngụm nước đi mà?",
  ],
  dirty: [
    "Tôi cần tắm rồi.",
    "Lông tôi bẩn quá...",
    "Mùi mùi rồi đó!",
  ],
  sleepy: [
    "Buồn ngủ quá... *zzZ*",
    "Cho tôi nghỉ chút nhé.",
    "Mí mắt nặng trĩu rồi...",
  ],
  sad: [
    "Tôi đang buồn...",
    "Cần một cái ôm.",
    "Hôm nay không vui lắm.",
  ],
  lonely: [
    "Bạn ở đâu vậy?",
    "Tôi cô đơn quá...",
    "Lâu lắm không gặp bạn.",
  ],
  happy: [
    "Vui quá đi mất!",
    "Yêu bạn nhiều lắm!",
    "Tuyệt vời quá!",
  ],
  normal: [
    "Đang thư giãn ~",
    "Mọi thứ ổn áp.",
    "Hôm nay là một ngày đẹp.",
  ],
};

const ACTION_LINES: Record<ActionKind, string[]> = {
  feed:  ["Ngon quá!", "Yêu bạn lắm!", "Hồi sinh rồi!"],
  drink: ["Mát quá!", "Đã khát rồi.", "Cảm ơn nha!"],
  play:  ["Vui quá!", "Chơi tiếp đi!", "Vui ghê!"],
  clean: ["Sạch sẽ rồi!", "Thơm tho quá!", "Lông mềm xốp!"],
  sleep: ["Zzz...", "Mơ đẹp nha...", "Tỉnh dậy sẽ khoẻ hơn."],
  train: ["Cố lên nào!", "Khoẻ hơn rồi!", "Một, hai, một, hai!"],
  pet:   ["Hihi~", "Vuốt nữa đi!", "Thích lắm đó!"],
};

// Deterministic seed: same mood + same updated_at → same line, so a
// re-render doesn't flicker between lines mid-animation.
function pick(lines: string[], seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i += 1) {
    h = (h * 31 + seed.charCodeAt(i)) | 0;
  }
  return lines[Math.abs(h) % lines.length];
}

export function moodBubble(mood: PetMood, seed: string): string {
  return pick(MOOD_LINES[mood] || MOOD_LINES.normal, seed);
}

export function actionBubble(action: ActionKind, seed: string): string {
  return pick(ACTION_LINES[action], seed);
}
