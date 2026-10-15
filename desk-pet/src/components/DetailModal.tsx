// Modal-ish overlay opened from the right-click menu. Shows the full
// stat list (incl. thirst/cleanliness/health that aren't on the
// always-visible mini panel) + level + exp + bond. Dismiss on outside
// click or Esc.

import { useEffect } from "react";
import { UserPetInstance } from "../api/petApi";

type Props = {
  pet: UserPetInstance;
  onClose: () => void;
};

type Row = { label: string; key: keyof UserPetInstance; emoji: string };

const ROWS: Row[] = [
  { label: "Hunger", key: "hunger", emoji: "🍖" },
  { label: "Thirst", key: "thirst", emoji: "💧" },
  { label: "Mood",   key: "mood",   emoji: "😊" },
  { label: "Energy", key: "energy", emoji: "⚡" },
  { label: "Clean",  key: "cleanliness", emoji: "🧼" },
  { label: "Bond",   key: "bond",   emoji: "💗" },
  { label: "Health", key: "health", emoji: "❤️" },
];

export default function DetailModal({ pet, onClose }: Props): JSX.Element {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>{pet.name}</strong>
          <button className="modal-close" onClick={onClose}>×</button>
        </div>
        <div className="modal-sub">
          {pet.stage} • lv {pet.level} • {pet.exp} exp
        </div>
        {ROWS.map((r) => {
          const v = pet[r.key] as number;
          return (
            <div className="detail-row" key={r.key}>
              <span style={{ width: 18 }}>{r.emoji}</span>
              <span style={{ width: 60, fontSize: 11 }}>{r.label}</span>
              <div className="mini-stat-bar" style={{ flex: 1 }}>
                <span style={{ width: `${Math.max(0, Math.min(100, v))}%`, background: "#a16eef" }} />
              </div>
              <span style={{ width: 30, textAlign: "right", fontSize: 11 }}>{v}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
