// Fetch wrapper for the akim-dc-bot desk-api. All requests run from
// the renderer, so CORS on the API side has to allow whatever origin
// the Electron window reports (file:// for prod, http://localhost:5173
// for dev) — DESK_API_CORS_ORIGINS=* is the path of least resistance
// during MVP.

const DEFAULT_BASE =
  // Override at build time if needed; this is also patched at runtime
  // by App.tsx when the user enters a custom server URL on the link
  // screen.
  import.meta.env.VITE_DESK_API_BASE || "http://localhost:8080";

let _base = DEFAULT_BASE;
let _token: string | null = null;

export function setBase(url: string): void {
  // Strip trailing slashes so request URLs are well-formed.
  _base = url.replace(/\/+$/, "");
}

export function getBase(): string {
  return _base;
}

export function setToken(token: string | null): void {
  _token = token;
}

export type ApiErrorBody = {
  error: true;
  code: string;
  message: string;
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (_token) {
    headers.set("Authorization", `Bearer ${_token}`);
  }
  const res = await fetch(`${_base}${path}`, { ...init, headers });
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const err = (body as { detail?: ApiErrorBody } | null)?.detail;
    throw new ApiError(
      res.status,
      err?.code || `HTTP_${res.status}`,
      err?.message || res.statusText || "Request failed",
    );
  }
  return body as T;
}

// --- Link flow --------------------------------------------------------------

export type LinkCreate = { code: string; expiresAt: string };

export function createLink(): Promise<LinkCreate> {
  return request<LinkCreate>("/api/desktop/link/create", { method: "POST" });
}

export type LinkStatus = {
  status: "pending" | "linked" | "expired" | "consumed";
  accessToken: string | null;
  linkedAt: string | null;
};

export function pollLinkStatus(code: string): Promise<LinkStatus> {
  return request<LinkStatus>(
    `/api/desktop/link/status?code=${encodeURIComponent(code)}`,
  );
}

// --- Active pet -------------------------------------------------------------

export type PetMood =
  | "sick"
  | "hungry"
  | "thirsty"
  | "dirty"
  | "sleepy"
  | "sad"
  | "lonely"
  | "happy"
  | "normal";

export type UserPetInstance = {
  id: number;
  ownerDiscordId: string;
  name: string;
  petTypeId: string;
  species: string;
  assetId: string | null;
  isActive: boolean;
  stage: string;
  level: number;
  exp: number;
  hunger: number;
  thirst: number;
  energy: number;
  mood: number;
  cleanliness: number;
  bond: number;
  health: number;
  currentMood: PetMood;
  lastFedAt: string | null;
  lastDrinkAt: string | null;
  lastPlayedAt: string | null;
  lastSleptAt: string | null;
  updatedAt: string;
};

export type PetAssetInfo = {
  assetId: string | null;
  displayName: string;
  localPath: string | null;
  manifestUrl: string | null;
  spritesheetUrl: string | null;
  version: string | null;
};

export type DesktopProfile = {
  petInstanceId: number;
  discordUserId: string;
  positionX: number;
  positionY: number;
  scale: number;
  selectedSkin: string;
  selectedAccessory: string | null;
  bubbleEnabled: boolean;
  soundEnabled: boolean;
  randomWalkEnabled: boolean;
  alwaysOnTop: boolean;
  lastSyncAt: string;
};

export type ActivePetResponse = {
  activePet: UserPetInstance;
  asset: PetAssetInfo;
  desktop: DesktopProfile;
};

export type OwnedPetSummary = {
  id: number;
  name: string;
  petTypeId: string;
  assetId: string | null;
  displayName: string;
  level: number;
  stage: string;
  isActive: boolean;
};

export function getActivePet(): Promise<ActivePetResponse> {
  return request<ActivePetResponse>("/api/pet/active");
}

export function getOwnedPets(): Promise<{ pets: OwnedPetSummary[] }> {
  return request<{ pets: OwnedPetSummary[] }>("/api/pet/owned");
}

export function selectActivePet(
  petInstanceId: number,
): Promise<ActivePetResponse> {
  return request<ActivePetResponse>("/api/pet/active/select", {
    method: "POST",
    body: JSON.stringify({ petInstanceId }),
  });
}

export type ActionResponse = ActivePetResponse & {
  action: string;
  delta: Record<string, number>;
  expGained: number;
  levelUp: boolean;
  newStage: string | null;
  message: string | null;
};

export type ActionKind =
  | "feed"
  | "drink"
  | "play"
  | "clean"
  | "sleep"
  | "train"
  | "pet";

export function applyAction(action: ActionKind): Promise<ActionResponse> {
  return request<ActionResponse>(`/api/pet/action/${action}`, {
    method: "POST",
    body: JSON.stringify({ source: "desktop" }),
  });
}

export function syncDesktopProfile(
  patch: Partial<Omit<DesktopProfile, "petInstanceId" | "discordUserId" | "lastSyncAt">>,
): Promise<ActivePetResponse> {
  return request<ActivePetResponse>("/api/pet/desktop/sync", {
    method: "POST",
    body: JSON.stringify(patch),
  });
}
