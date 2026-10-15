// OpenPets-style main view: speech bubble → sprite → mini-stats.
// Right-click anywhere to open the action menu. Drag the whole card
// (the .pet-card wrapper is marked draggable in CSS; everything
// interactive carries no-drag).

import { useEffect, useState } from "react";
import { usePetStore } from "../store/petStore";
import { actionAnimation, moodAnimation, resolveStateSheet } from "../pet/visualState";
import { useRandomWalk } from "../pet/motion";
import { actionBubble, moodBubble } from "../pet/bubbleMessages";
import ActionObject from "./ActionObject";
import PetSprite from "./PetSprite";
import StateSprite from "./StateSprite";
import SpeechBubble from "./SpeechBubble";
import MiniStatPanel from "./MiniStatPanel";
import PetContextMenu from "./PetContextMenu";
import DetailModal from "./DetailModal";
import SwitchModal from "./SwitchModal";
import SettingsModal from "./SettingsModal";

export default function PetView(): JSX.Element | null {
  const activePet = usePetStore((s) => s.activePet);
  const loadedAsset = usePetStore((s) => s.loadedAsset);
  const desktop = usePetStore((s) => s.desktop);
  const pendingAction = usePetStore((s) => s.pendingAction);
  const clearPendingAction = usePetStore((s) => s.clearPendingAction);
  const doAction = usePetStore((s) => s.doAction);
  const lastActionError = usePetStore((s) => s.lastActionError);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [showDetail, setShowDetail] = useState(false);
  const [showSwitch, setShowSwitch] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  // Cursor over the window → hold the pet still so it's easy to click /
  // drag, and so an open context menu or modal isn't chasing a moving
  // window.
  const [hovered, setHovered] = useState(false);

  // Auto-dismiss the cooldown / error toast after 3s so it doesn't
  // sit there forever.
  useEffect(() => {
    if (!lastActionError) return;
    const t = window.setTimeout(() => {
      usePetStore.setState({ lastActionError: null });
    }, 3000);
    return () => window.clearTimeout(t);
  }, [lastActionError]);

  // Random walk drives the window position; pause it while interacting
  // (hover / action animation / any open overlay).
  const randomWalkEnabled = desktop?.randomWalkEnabled ?? true;
  const overlayOpen = menu !== null || showDetail || showSwitch || showSettings;
  const walkAnim = useRandomWalk({
    enabled: !!activePet && randomWalkEnabled,
    paused: hovered || pendingAction !== null || overlayOpen,
  });

  // Apply the persisted always-on-top preference to the live window
  // whenever it changes (e.g. restored from the server on launch).
  const alwaysOnTop = desktop?.alwaysOnTop;
  useEffect(() => {
    if (alwaysOnTop === undefined) return;
    void window.akimDeskPet?.window?.setAlwaysOnTop(alwaysOnTop);
  }, [alwaysOnTop]);

  if (!activePet) return null;

  // Animation priority: a one-shot action wins, then a live walk cycle,
  // then the resting mood pose.
  const animKey = pendingAction
    ? actionAnimation(pendingAction)
    : walkAnim ?? moodAnimation(activePet.currentMood);
  const bubbleText = pendingAction
    ? actionBubble(pendingAction, activePet.updatedAt + pendingAction)
    : moodBubble(activePet.currentMood, activePet.updatedAt);

  const hasSprite =
    loadedAsset && loadedAsset.spritesheetUrl && loadedAsset.manifest.animations;

  // Prefer the per-state grid sheets when the asset ships them; resolve
  // the current animation to a state-sheet name (idle/move/eat/...) for
  // the pet's current stage.
  const stateSheet = loadedAsset?.stateSheet ?? null;
  const stateName = stateSheet
    ? resolveStateSheet(animKey, new Set(Object.keys(stateSheet.states)))
    : null;

  return (
    <div
      className="pet-card"
      onContextMenu={(e) => {
        e.preventDefault();
        setMenu({ x: e.clientX, y: e.clientY });
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <div className="pet-card-inner">
        {(desktop?.bubbleEnabled ?? true) && <SpeechBubble text={bubbleText} />}

        <div
          className="pet-sprite-wrap pet-sprite-clickable"
          title="Bấm để vuốt ve"
          onClick={() => void doAction("pet")}
        >
          {/* The state sheets already draw props (bowl, toy…), so the
              emoji overlay is only for the old OpenPets fallback. */}
          {pendingAction && !(stateSheet && stateName) && (
            <ActionObject key={pendingAction} action={pendingAction} />
          )}
          {stateSheet && stateName ? (
            <StateSprite
              assetId={loadedAsset!.assetId}
              stage={activePet.stage || "baby"}
              config={stateSheet}
              state={stateName}
              flipX={animKey === "walkLeft"}
              onAnimationEnd={clearPendingAction}
            />
          ) : hasSprite ? (
            <PetSprite
              asset={loadedAsset}
              animation={animKey}
              scale={loadedAsset.manifest.defaultScale ?? 1}
              onAnimationEnd={clearPendingAction}
            />
          ) : (
            <span style={{ fontSize: 96 }}>🐾</span>
          )}
        </div>

        <MiniStatPanel pet={activePet} />

        {lastActionError && <div className="toast-error">{lastActionError}</div>}
      </div>

      {menu && (
        <PetContextMenu
          position={menu}
          onClose={() => setMenu(null)}
          onOpenDetail={() => setShowDetail(true)}
          onOpenSwitch={() => setShowSwitch(true)}
          onOpenSettings={() => setShowSettings(true)}
        />
      )}
      {showDetail && (
        <DetailModal pet={activePet} onClose={() => setShowDetail(false)} />
      )}
      {showSwitch && <SwitchModal onClose={() => setShowSwitch(false)} />}
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </div>
  );
}
