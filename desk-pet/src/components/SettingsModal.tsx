// Settings panel from the right-click menu. Each toggle writes through
// to the server profile via syncProfile (optimistic — the store swaps
// in the response). always_on_top additionally drives the Electron
// window flag through the preload bridge so the change is visible
// without a restart.

import { useEffect } from "react";
import { DesktopProfile } from "../api/petApi";
import { usePetStore } from "../store/petStore";

type ToggleKey =
  | "bubbleEnabled"
  | "soundEnabled"
  | "randomWalkEnabled"
  | "alwaysOnTop";

const TOGGLES: { key: ToggleKey; label: string; hint: string }[] = [
  { key: "randomWalkEnabled", label: "Tự đi dạo", hint: "Pet tự di chuyển quanh màn hình" },
  { key: "bubbleEnabled", label: "Bong bóng thoại", hint: "Hiện lời thoại phía trên pet" },
  { key: "soundEnabled", label: "Âm thanh", hint: "Tiếng động khi chăm sóc" },
  { key: "alwaysOnTop", label: "Luôn nổi trên cùng", hint: "Giữ pet trên mọi cửa sổ" },
];

export default function SettingsModal({
  onClose,
}: {
  onClose: () => void;
}): JSX.Element {
  const desktop = usePetStore((s) => s.desktop);
  const syncProfile = usePetStore((s) => s.syncProfile);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const onToggle = (key: ToggleKey, next: boolean) => {
    const patch: Partial<DesktopProfile> = { [key]: next };
    void syncProfile(patch);
    if (key === "alwaysOnTop") {
      void window.akimDeskPet?.window?.setAlwaysOnTop(next);
    }
  };

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <strong>Cài đặt</strong>
          <button className="modal-close" onClick={onClose}>×</button>
        </div>
        {!desktop ? (
          <p className="muted">Chưa tải được hồ sơ pet.</p>
        ) : (
          <div className="settings-list">
            {TOGGLES.map((t) => {
              const checked = desktop[t.key];
              return (
                <label key={t.key} className="setting-row">
                  <span className="setting-text">
                    <span className="setting-label">{t.label}</span>
                    <span className="setting-hint muted">{t.hint}</span>
                  </span>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={checked}
                    className={`switch ${checked ? "on" : ""}`}
                    onClick={() => onToggle(t.key, !checked)}
                  >
                    <span className="switch-knob" />
                  </button>
                </label>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
