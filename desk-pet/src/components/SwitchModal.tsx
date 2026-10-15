// Owned-pet picker triggered from the right-click menu. Lists every
// pet the user has in the linked guild; clicking one calls /select.

import { useEffect } from "react";
import { usePetStore } from "../store/petStore";

export default function SwitchModal({ onClose }: { onClose: () => void }): JSX.Element {
  const ownedPets = usePetStore((s) => s.ownedPets);
  const pickPet = usePetStore((s) => s.pickPet);

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
          <strong>Đổi pet active</strong>
          <button className="modal-close" onClick={onClose}>×</button>
        </div>
        {ownedPets.length === 0 ? (
          <p className="muted">Bạn chưa có pet nào.</p>
        ) : (
          <div className="owned-list">
            {ownedPets.map((p) => (
              <button
                key={p.id}
                className={p.isActive ? "active" : ""}
                onClick={() => {
                  void pickPet(p.id);
                  onClose();
                }}
              >
                <strong>{p.name}</strong>{" "}
                <span className="muted">— {p.displayName} (lv {p.level})</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
