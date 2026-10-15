// Right-click popup. Items dispatch into the Zustand store; closing
// is handled by an outside-click listener on the host. The menu is
// a regular div (not a native Electron Menu) so it can be styled
// flush with the OpenPets card aesthetic.

import { useEffect, useRef } from "react";
import { ActionKind } from "../api/petApi";
import { usePetStore } from "../store/petStore";

type Position = { x: number; y: number };

type Props = {
  position: Position;
  onClose: () => void;
  onOpenDetail: () => void;
  onOpenSwitch: () => void;
  onOpenSettings: () => void;
};

type Item =
  | { type: "header"; label: string }
  | { type: "action"; kind: ActionKind; label: string; emoji: string }
  | { type: "divider" }
  | { type: "callback"; label: string; emoji: string; onClick: () => void };

export default function PetContextMenu({
  position, onClose, onOpenDetail, onOpenSwitch, onOpenSettings,
}: Props): JSX.Element {
  const ref = useRef<HTMLDivElement | null>(null);
  const doAction = usePetStore((s) => s.doAction);
  const refreshActive = usePetStore((s) => s.refreshActive);
  const signOut = usePetStore((s) => s.signOut);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const items: Item[] = [
    { type: "header", label: "Chăm sóc" },
    { type: "action", kind: "feed",  label: "Cho ăn",     emoji: "🍖" },
    { type: "action", kind: "drink", label: "Cho uống",   emoji: "💧" },
    { type: "action", kind: "play",  label: "Chơi",       emoji: "🎾" },
    { type: "action", kind: "train", label: "Training",   emoji: "💪" },
    { type: "action", kind: "clean", label: "Tắm rửa",    emoji: "🧼" },
    { type: "action", kind: "sleep", label: "Ngủ",        emoji: "💤" },
    { type: "action", kind: "pet",   label: "Vuốt ve",    emoji: "🤚" },
    { type: "divider" },
    { type: "callback", label: "Chi tiết stats", emoji: "📊", onClick: onOpenDetail },
    { type: "callback", label: "Đổi pet active", emoji: "🔁", onClick: onOpenSwitch },
    { type: "callback", label: "Cài đặt",        emoji: "⚙️", onClick: onOpenSettings },
    { type: "callback", label: "Reload",         emoji: "↻",  onClick: () => void refreshActive() },
    { type: "divider" },
    { type: "callback", label: "Đăng xuất",      emoji: "⏏", onClick: () => void signOut() },
  ];

  return (
    <div
      ref={ref}
      className="context-menu"
      style={{ left: position.x, top: position.y }}
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((item, i) => {
        if (item.type === "divider") {
          return <div key={i} className="context-divider" />;
        }
        if (item.type === "header") {
          return <div key={i} className="context-header">{item.label}</div>;
        }
        if (item.type === "action") {
          return (
            <button
              key={i}
              className="context-item"
              onClick={() => {
                void doAction(item.kind);
                onClose();
              }}
            >
              <span className="context-emoji">{item.emoji}</span>
              {item.label}
            </button>
          );
        }
        return (
          <button
            key={i}
            className="context-item"
            onClick={() => {
              item.onClick();
              onClose();
            }}
          >
            <span className="context-emoji">{item.emoji}</span>
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
