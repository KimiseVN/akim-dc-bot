// Zustand store: one source of truth for what the renderer paints.
// Side-effecting actions (API calls, cache writes) live here too so
// components stay declarative.
import { create } from "zustand";
import {
  ActionKind,
  ActivePetResponse,
  ApiError,
  applyAction,
  createLink,
  DesktopProfile,
  getActivePet,
  getOwnedPets,
  OwnedPetSummary,
  pollLinkStatus,
  selectActivePet,
  setBase as apiSetBase,
  setToken as apiSetToken,
  syncDesktopProfile,
  UserPetInstance,
} from "../api/petApi";
import { loadBaseUrl, loadCache, loadToken, saveBaseUrl, saveCache, saveToken } from "../lib/cache";
import { LoadedAsset, loadAsset } from "../pet/manifest";
import { playActionSound } from "../pet/sound";

type LinkPhase =
  | { kind: "idle" }
  | { kind: "creating" }
  | { kind: "awaiting"; code: string; expiresAt: string }
  | { kind: "linking" }
  | { kind: "error"; message: string };

type ActivePhase =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "no-active-pet"; reason: string }
  | { kind: "error"; message: string };

type PetState = {
  // Bootstrap
  bootDone: boolean;
  base: string;
  token: string | null;

  // Link flow
  link: LinkPhase;

  // Active pet
  active: ActivePhase;
  activePet: UserPetInstance | null;
  asset: ActivePetResponse["asset"] | null;
  desktop: DesktopProfile | null;
  ownedPets: OwnedPetSummary[];

  // Loaded spritesheet asset for the current active pet (loaded
  // lazily whenever activePet.assetId changes).
  loadedAsset: LoadedAsset | null;

  // Transient: the action whose animation is currently playing on the
  // sprite. Cleared by PetSprite's onAnimationEnd via clearAction().
  pendingAction: ActionKind | null;

  // Per-action error message surfaced to the UI (cooldown etc).
  lastActionError: string | null;

  // Internal handle so we can cancel the link poller when the panel
  // closes or the link succeeds.
  _linkPollTimer: number | null;
  // Watchdog for pendingAction in case onAnimationEnd never fires
  // (animation got cancelled by a re-render etc.).
  _actionAnimTimer: number | null;

  // ---------------------------------------------------------------
  setBase: (url: string) => Promise<void>;
  bootstrap: () => Promise<void>;
  beginLink: () => Promise<void>;
  cancelLink: () => void;
  refreshActive: () => Promise<void>;
  refreshOwned: () => Promise<void>;
  pickPet: (petInstanceId: number) => Promise<void>;
  doAction: (action: ActionKind) => Promise<void>;
  syncProfile: (patch: Partial<DesktopProfile>) => Promise<void>;
  signOut: () => Promise<void>;
  // Sprite control
  clearPendingAction: () => void;
  reloadAsset: () => Promise<void>;
};

const DEFAULT_BASE =
  import.meta.env.VITE_DESK_API_BASE || "http://localhost:8080";

export const usePetStore = create<PetState>((set, get) => ({
  bootDone: false,
  base: DEFAULT_BASE,
  token: null,
  link: { kind: "idle" },
  active: { kind: "idle" },
  activePet: null,
  asset: null,
  desktop: null,
  ownedPets: [],
  loadedAsset: null,
  pendingAction: null,
  lastActionError: null,
  _linkPollTimer: null,
  _actionAnimTimer: null,

  async setBase(url) {
    const trimmed = url.trim().replace(/\/+$/, "");
    apiSetBase(trimmed);
    await saveBaseUrl(trimmed);
    set({ base: trimmed });
  },

  async bootstrap() {
    if (get().bootDone) return;
    const [storedBase, token, cache] = await Promise.all([
      loadBaseUrl(),
      loadToken(),
      loadCache(),
    ]);
    const base = storedBase ?? DEFAULT_BASE;
    apiSetBase(base);
    apiSetToken(token);
    set({
      base,
      token,
      bootDone: true,
      activePet: cache.activePet,
    });
    if (token) {
      // Fire-and-forget — UI will reflect the result via the store.
      void get().refreshActive();
      void get().refreshOwned();
    }
  },

  async beginLink() {
    get().cancelLink();
    set({ link: { kind: "creating" } });
    try {
      const created = await createLink();
      set({
        link: { kind: "awaiting", code: created.code, expiresAt: created.expiresAt },
      });
      const timer = window.setInterval(async () => {
        try {
          const status = await pollLinkStatus(created.code);
          if (status.status === "pending") {
            return;
          }
          if (status.status === "linked" && status.accessToken) {
            window.clearInterval(timer);
            set({ link: { kind: "linking" }, _linkPollTimer: null });
            const token = status.accessToken;
            apiSetToken(token);
            await saveToken(token);
            set({ token, link: { kind: "idle" } });
            await get().refreshActive();
            await get().refreshOwned();
            return;
          }
          // expired / consumed — terminal failure, ask user to retry
          window.clearInterval(timer);
          set({
            _linkPollTimer: null,
            link: {
              kind: "error",
              message:
                status.status === "expired"
                  ? "Code đã hết hạn. Bấm 'Tạo code mới' để thử lại."
                  : "Code đã được dùng. Bấm 'Tạo code mới'.",
            },
          });
        } catch (e) {
          // Network blips — keep polling silently. Surface only if it
          // becomes a hard error from the server (4xx with code).
          if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
            window.clearInterval(timer);
            set({
              _linkPollTimer: null,
              link: { kind: "error", message: e.message },
            });
          }
        }
      }, 2000);
      set({ _linkPollTimer: timer });
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      set({ link: { kind: "error", message } });
    }
  },

  cancelLink() {
    const t = get()._linkPollTimer;
    if (t !== null) {
      window.clearInterval(t);
    }
    set({ _linkPollTimer: null });
  },

  async refreshActive() {
    set({ active: { kind: "loading" }, lastActionError: null });
    try {
      const res = await getActivePet();
      set({
        active: { kind: "ready" },
        activePet: res.activePet,
        asset: res.asset,
        desktop: res.desktop,
      });
      // Load the spritesheet asset if it changed (or first load).
      const prev = get().loadedAsset?.assetId;
      const wantedId = res.asset.assetId;
      if (prev !== wantedId) {
        const loaded = await loadAsset(wantedId);
        set({ loadedAsset: loaded });
      }
      const cache = await loadCache();
      cache.activePet = res.activePet;
      cache.desktopProfilesByPetId[res.activePet.id] = res.desktop;
      await saveCache(cache);
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.code === "ACTIVE_PET_REQUIRED" || e.code === "PET_NOT_FOUND") {
          set({
            active: { kind: "no-active-pet", reason: e.message },
            activePet: null,
            asset: null,
            desktop: null,
          });
          void get().refreshOwned();
          return;
        }
        if (e.status === 401) {
          // Token expired or revoked — drop it and go back to link.
          await saveToken(null);
          apiSetToken(null);
          set({
            token: null,
            active: { kind: "idle" },
            activePet: null,
            asset: null,
            desktop: null,
          });
          return;
        }
      }
      const message = e instanceof Error ? e.message : String(e);
      set({ active: { kind: "error", message } });
    }
  },

  async refreshOwned() {
    try {
      const res = await getOwnedPets();
      set({ ownedPets: res.pets });
    } catch {
      // Owned list is a nice-to-have; ignore failures.
    }
  },

  async pickPet(petInstanceId) {
    try {
      const res = await selectActivePet(petInstanceId);
      set({
        active: { kind: "ready" },
        activePet: res.activePet,
        asset: res.asset,
        desktop: res.desktop,
      });
      void get().refreshOwned();
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      set({ lastActionError: message });
    }
  },

  async doAction(action) {
    set({ lastActionError: null });
    try {
      const res = await applyAction(action);
      // Sound is gated on the freshly-returned profile so a just-toggled
      // setting takes effect immediately. The call is inside the click
      // handler chain, satisfying the browser autoplay gesture rule.
      if (res.desktop.soundEnabled) {
        playActionSound(action);
      }
      set({
        active: { kind: "ready" },
        activePet: res.activePet,
        asset: res.asset,
        desktop: res.desktop,
        pendingAction: action,
      });
      // Watchdog: if PetSprite.onAnimationEnd doesn't fire within ~3s
      // (animation got cancelled by a re-render, or asset has no
      // matching row) we clear pendingAction ourselves so the sprite
      // returns to the mood animation.
      const prev = get()._actionAnimTimer;
      if (prev !== null) window.clearTimeout(prev);
      const t = window.setTimeout(() => {
        set({ pendingAction: null, _actionAnimTimer: null });
      }, 3000);
      set({ _actionAnimTimer: t });
    } catch (e) {
      if (e instanceof ApiError) {
        // A 404 here means the bot is running an older build that
        // doesn't have this action's endpoint yet (e.g. /action/train
        // before the bot was restarted). Give an actionable hint rather
        // than the raw "Request failed".
        const message =
          e.status === 404
            ? "Máy chủ chưa hỗ trợ hành động này — cập nhật / khởi động lại bot."
            : e.message;
        set({ lastActionError: message });
        return;
      }
      const message = e instanceof Error ? e.message : String(e);
      set({ lastActionError: message });
    }
  },

  clearPendingAction() {
    const t = get()._actionAnimTimer;
    if (t !== null) window.clearTimeout(t);
    set({ pendingAction: null, _actionAnimTimer: null });
  },

  async reloadAsset() {
    const assetId = get().asset?.assetId ?? null;
    const loaded = await loadAsset(assetId);
    set({ loadedAsset: loaded });
  },

  async syncProfile(patch) {
    try {
      const res = await syncDesktopProfile(patch);
      set({
        activePet: res.activePet,
        asset: res.asset,
        desktop: res.desktop,
      });
    } catch (e) {
      // Sync failures are non-blocking — log to console for now.
      console.warn("syncProfile failed", e);
    }
  },

  async signOut() {
    get().cancelLink();
    get().clearPendingAction();
    await saveToken(null);
    apiSetToken(null);
    set({
      token: null,
      active: { kind: "idle" },
      activePet: null,
      asset: null,
      desktop: null,
      ownedPets: [],
      loadedAsset: null,
      link: { kind: "idle" },
    });
  },
}));
