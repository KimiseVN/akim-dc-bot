// Main pet view: portrait placeholder (emoji until sprite engine
// lands in Phase 4), stat bars, and action buttons. Owned-pet list
// at the bottom lets the user swap active pet without leaving the
// app.
import { ActionKind } from "../api/petApi";
import { usePetStore } from "../store/petStore";

const MOOD_EMOJI: Record<string, string> = {
  sick: "🤒",
  hungry: "🍖",
  thirsty: "💧",
  dirty: "🧼",
  sleepy: "😴",
  sad: "😢",
  lonely: "💔",
  happy: "😊",
  normal: "😌",
};

const ACTIONS: { kind: ActionKind; label: string; emoji: string }[] = [
  { kind: "feed", label: "Cho ăn", emoji: "🍖" },
  { kind: "drink", label: "Cho uống", emoji: "💧" },
  { kind: "play", label: "Chơi", emoji: "🎾" },
  { kind: "sleep", label: "Ngủ", emoji: "💤" },
  { kind: "pet", label: "Vuốt ve", emoji: "🤚" },
];

function StatBar({ label, value }: { label: string; value: number }): JSX.Element {
  return (
    <div className="stat-row">
      <span style={{ width: 64 }}>{label}</span>
      <div className="bar">
        <span style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
      </div>
      <span style={{ width: 38, textAlign: "right" }}>{value}</span>
    </div>
  );
}

export default function ActivePetCard(): JSX.Element {
  const activePet = usePetStore((s) => s.activePet);
  const asset = usePetStore((s) => s.asset);
  const ownedPets = usePetStore((s) => s.ownedPets);
  const lastActionError = usePetStore((s) => s.lastActionError);
  const active = usePetStore((s) => s.active);
  const doAction = usePetStore((s) => s.doAction);
  const pickPet = usePetStore((s) => s.pickPet);
  const refreshActive = usePetStore((s) => s.refreshActive);

  if (!activePet) {
    return (
      <div className="content">
        {active.kind === "loading" && <p>Đang tải pet...</p>}
        {active.kind === "no-active-pet" && (
          <>
            <h2>Chưa có pet active</h2>
            <p className="muted">{active.reason}</p>
            {ownedPets.length > 0 && (
              <>
                <p className="muted">Chọn 1 pet bên dưới để active:</p>
                <div className="owned-list">
                  {ownedPets.map((p) => (
                    <button key={p.id} onClick={() => pickPet(p.id)}>
                      <strong>{p.name}</strong> <span className="muted">— {p.displayName} (lv {p.level})</span>
                    </button>
                  ))}
                </div>
              </>
            )}
            <button
              className="ghost"
              style={{ marginTop: 12 }}
              onClick={refreshActive}
            >
              ↻ Reload
            </button>
          </>
        )}
        {active.kind === "error" && (
          <>
            <h2>Lỗi</h2>
            <p className="error">{active.message}</p>
            <button className="primary" onClick={refreshActive}>
              Thử lại
            </button>
          </>
        )}
      </div>
    );
  }

  const emoji = MOOD_EMOJI[activePet.currentMood] || "🐾";

  return (
    <div className="content">
      <div className="pet-portrait" title={asset?.displayName || activePet.species}>
        {emoji}
      </div>
      <h2>
        {activePet.name}{" "}
        <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>
          {activePet.stage} • lv {activePet.level} • {activePet.exp} exp
        </span>
      </h2>
      <StatBar label="Hunger" value={activePet.hunger} />
      <StatBar label="Thirst" value={activePet.thirst} />
      <StatBar label="Mood" value={activePet.mood} />
      <StatBar label="Energy" value={activePet.energy} />
      <StatBar label="Clean" value={activePet.cleanliness} />
      <StatBar label="Bond" value={activePet.bond} />
      <StatBar label="Health" value={activePet.health} />

      <div className="actions-grid">
        {ACTIONS.map((a) => (
          <button key={a.kind} onClick={() => doAction(a.kind)}>
            <div style={{ fontSize: 20 }}>{a.emoji}</div>
            <div>{a.label}</div>
          </button>
        ))}
      </div>

      {lastActionError && <p className="error">{lastActionError}</p>}

      {ownedPets.length > 1 && (
        <>
          <p className="muted" style={{ marginTop: 16 }}>
            Đổi pet active:
          </p>
          <div className="owned-list">
            {ownedPets
              .filter((p) => !p.isActive)
              .map((p) => (
                <button key={p.id} onClick={() => pickPet(p.id)}>
                  <strong>{p.name}</strong>{" "}
                  <span className="muted">
                    — {p.displayName} (lv {p.level})
                  </span>
                </button>
              ))}
          </div>
        </>
      )}
    </div>
  );
}
