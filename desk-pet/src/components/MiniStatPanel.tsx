// Compact 2x2 grid showing the four headline stats from the OpenPets
// reference: Food / Energy / Play / Bond.
//
// Mapping into the bot's stat model:
//   Food   = hunger (fullness, high=good)
//   Energy = energy
//   Play   = mood     (the "I'm having fun" signal, not cleanliness)
//   Bond   = bond
//
// thirst/cleanliness/health stay in the right-click "Chi tiết" panel
// so the always-on UI doesn't get noisy.

import { UserPetInstance } from "../api/petApi";

type Stat = {
  key: keyof Pick<UserPetInstance, "hunger" | "energy" | "mood" | "bond">;
  label: string;
  emoji: string;
  // Tailwind-ish HEX pair: bar + bar-bg
  color: string;
};

const STATS: Stat[] = [
  { key: "hunger", label: "Food",   emoji: "🍖", color: "#f59e0b" },
  { key: "energy", label: "Energy", emoji: "⚡", color: "#fbbf24" },
  { key: "mood",   label: "Play",   emoji: "🎾", color: "#60a5fa" },
  { key: "bond",   label: "Bond",   emoji: "💗", color: "#f472b6" },
];

export default function MiniStatPanel({ pet }: { pet: UserPetInstance }): JSX.Element {
  return (
    <div className="mini-stats">
      {STATS.map((s) => {
        const value = pet[s.key];
        return (
          <div className="mini-stat" key={s.key} title={`${s.label}: ${value}/100`}>
            <span className="mini-stat-emoji" style={{ backgroundColor: s.color + "33" }}>
              {s.emoji}
            </span>
            <div className="mini-stat-body">
              <span className="mini-stat-label">{s.label}</span>
              <div className="mini-stat-bar">
                <span style={{
                  width: `${Math.max(0, Math.min(100, value))}%`,
                  background: s.color,
                }} />
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
