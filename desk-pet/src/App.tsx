// Root. Renders LinkPanel on first run, PetView once a token is held.
// No app-level titlebar — the OpenPets-style card has no chrome; the
// whole window is the pet. Right-click on the pet for actions.
import { useEffect, useLayoutEffect, useRef } from "react";
import { usePetStore } from "./store/petStore";
import LinkPanel from "./components/LinkPanel";
import PetView from "./components/PetView";

// The pet view needs the full sprite-sized window; the shell views
// (link / loading / empty) are short, so we shrink the window to fit
// them instead of leaving dead space below the content.
const WINDOW_WIDTH = 300;
const PET_VIEW_HEIGHT = 470;

export default function App(): JSX.Element {
  const bootDone = usePetStore((s) => s.bootDone);
  const token = usePetStore((s) => s.token);
  const bootstrap = usePetStore((s) => s.bootstrap);
  const refreshActive = usePetStore((s) => s.refreshActive);
  const active = usePetStore((s) => s.active);
  const link = usePetStore((s) => s.link);
  const shellRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  // Poll active pet every 60s so a stat-decay tick from the bot side
  // (or a /pet feed in Discord) shows up without manual refresh.
  useEffect(() => {
    if (!token) return;
    const t = window.setInterval(() => {
      void refreshActive();
    }, 60_000);
    return () => window.clearInterval(t);
  }, [token, refreshActive]);

  // Resize the OS window to match the current view: measured shell
  // height for the compact screens, full height for the pet. Runs after
  // layout so the measurement reflects the freshly-rendered content, and
  // re-runs whenever the view or link/active state changes its height.
  useLayoutEffect(() => {
    const api = window.akimDeskPet?.window;
    if (!api?.setSize) return;
    const el = shellRef.current;
    if (el) {
      // offsetHeight = the shell's full natural border-box height (no
      // clamp, no scroll), which is exactly what the window should be.
      void api.setSize(WINDOW_WIDTH, el.offsetHeight);
    } else {
      void api.setSize(WINDOW_WIDTH, PET_VIEW_HEIGHT);
    }
  }, [bootDone, token, link, active]);

  if (!bootDone) {
    return (
      <div className="app-shell loading" ref={shellRef}>
        <span className="muted">Đang khởi động...</span>
      </div>
    );
  }

  if (!token) {
    return (
      <div className="app-shell" ref={shellRef}>
        <LinkPanel />
      </div>
    );
  }

  if (active.kind === "no-active-pet" || active.kind === "error" || active.kind === "loading") {
    return (
      <div className="app-shell" ref={shellRef}>
        <NoActivePet />
      </div>
    );
  }

  return <PetView />;
}

// Small inline component for the "no active pet" path so the OpenPets
// card mode and the empty-state mode don't share component code (they
// have totally different layout).
function NoActivePet(): JSX.Element {
  const active = usePetStore((s) => s.active);
  const ownedPets = usePetStore((s) => s.ownedPets);
  const pickPet = usePetStore((s) => s.pickPet);
  const refreshActive = usePetStore((s) => s.refreshActive);
  const signOut = usePetStore((s) => s.signOut);

  if (active.kind === "loading") {
    return <p className="muted" style={{ padding: 16 }}>Đang tải pet...</p>;
  }
  if (active.kind === "error") {
    return (
      <div style={{ padding: 16 }}>
        <h2>Lỗi</h2>
        <p className="error">{active.message}</p>
        <button className="primary" onClick={refreshActive}>Thử lại</button>
      </div>
    );
  }
  return (
    <div style={{ padding: 16 }}>
      <h2>Chưa có pet active</h2>
      <p className="muted">{active.kind === "no-active-pet" ? active.reason : ""}</p>
      {ownedPets.length > 0 && (
        <>
          <p className="muted">Chọn 1 pet bên dưới để active:</p>
          <div className="owned-list">
            {ownedPets.map((p) => (
              <button key={p.id} onClick={() => pickPet(p.id)}>
                <strong>{p.name}</strong>{" "}
                <span className="muted">— {p.displayName} (lv {p.level})</span>
              </button>
            ))}
          </div>
        </>
      )}
      <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
        <button className="ghost" onClick={refreshActive}>↻ Reload</button>
        <button className="ghost" onClick={() => void signOut()}>Đăng xuất</button>
      </div>
    </div>
  );
}
