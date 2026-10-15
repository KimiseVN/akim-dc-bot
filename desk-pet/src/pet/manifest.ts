// PetManifest types + loader. The renderer fetches pet.json from
// `<base>/assets/pets/<assetId>/pet.json` (Vite serves public/ at the
// document root in both dev and Electron prod). When the file is
// missing or malformed we fall back to the bundled `fallback` asset.

export type PetAnimationState =
  | "idle"
  | "walkRight"
  | "walkLeft"
  | "happy"
  | "sad"
  | "eat"
  | "drink"
  | "play"
  | "sleep"
  | "petting"
  | "jump"
  | "train"
  | "sick";

export type SpriteAnimationDefinition = {
  row: number;
  frames: number;
  durationMs: number;
  // CSS `animation-iteration-count` — "infinite" or a positive integer.
  iterations: number | "infinite";
};

// Per-state grid spritesheets (the newer art model): one webp per
// (stage, state), each a uniform cols×rows grid stepped by a JS ticker.
// Resolves a file like `<assetId>/<stage>-stage/<state>.webp`.
export type StateSheetTiming = {
  fps: number;
  loop: boolean;
};
export type StateSheetConfig = {
  // e.g. "{stage}-stage/{state}.webp"
  pathTemplate: string;
  cols: number;
  rows: number;
  frames: number;
  // On-screen height (px) the cell is scaled to; width keeps aspect.
  displayHeight?: number;
  // Per-state fps + loop. Keys are state names (idle, move, eat, ...).
  states: Record<string, StateSheetTiming>;
};

export type PetManifest = {
  id: string;
  displayName: string;
  description?: string;
  species?: string;
  spritesheetPath?: string;
  thumbnailPath?: string;
  frameWidth?: number;
  frameHeight?: number;
  columns?: number;
  rows?: number;
  defaultScale?: number;
  animations?: Record<string, SpriteAnimationDefinition>;
  stateSheet?: StateSheetConfig;
};

export type LoadedAsset = {
  manifest: PetManifest;
  spritesheetUrl: string | null;
  assetId: string;
  // Non-null when the asset ships per-state grid sheets.
  stateSheet: StateSheetConfig | null;
};

const KNOWN_STAGES = new Set(["baby", "teen", "adult", "mystic"]);

/** Resolve a per-state grid sheet URL for a given stage + state. */
export function stateSheetUrl(
  assetId: string,
  config: StateSheetConfig,
  stage: string,
  state: string,
): string {
  const safeStage = KNOWN_STAGES.has(stage) ? stage : "baby";
  const rel = config.pathTemplate
    .replace("{stage}", safeStage)
    .replace("{state}", state);
  return assetUrl(assetId, rel);
}

const FALLBACK_ID = "fallback";

// Build a URL that works in both Vite dev (http://localhost:5173) and
// Electron prod (file://). document.baseURI honours Vite's base
// config so we don't have to fork the path manually.
function assetUrl(assetId: string, rel: string): string {
  return new URL(`assets/pets/${assetId}/${rel}`, document.baseURI).href;
}

async function fetchManifest(assetId: string): Promise<PetManifest | null> {
  try {
    const res = await fetch(assetUrl(assetId, "pet.json"), { cache: "no-cache" });
    if (!res.ok) return null;
    return (await res.json()) as PetManifest;
  } catch {
    return null;
  }
}

/**
 * Resolve the asset for a given id. Falls back to the bundled fallback
 * pet when the manifest can't be fetched OR when the species hasn't
 * been linked to a desktop asset yet (assetId == null).
 *
 * The spritesheet URL is returned as a plain HTTP URL; the renderer
 * sets it as a CSS `background-image`. No <img> preload because the
 * sheet is a static file and Chromium will cache it.
 */
export async function loadAsset(assetId: string | null): Promise<LoadedAsset> {
  if (assetId) {
    const m = await fetchManifest(assetId);
    if (m) {
      return {
        manifest: m,
        spritesheetUrl: m.spritesheetPath
          ? assetUrl(assetId, m.spritesheetPath)
          : null,
        assetId,
        stateSheet: m.stateSheet ?? null,
      };
    }
  }
  // Fallback
  const fb = await fetchManifest(FALLBACK_ID);
  return {
    manifest: fb ?? {
      id: FALLBACK_ID,
      displayName: "Mystery Pet",
      animations: {},
    },
    spritesheetUrl: fb?.spritesheetPath
      ? assetUrl(FALLBACK_ID, fb.spritesheetPath)
      : null,
    assetId: FALLBACK_ID,
    stateSheet: fb?.stateSheet ?? null,
  };
}
