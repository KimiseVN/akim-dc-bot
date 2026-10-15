// Persistent cache backed by Electron's userData directory + safeStorage
// for the auth token. The renderer goes through window.akimDeskPet
// (preload bridge) — no fs/process access here.
import type { SecretBlob } from "../electron.d";
import type { DesktopProfile, UserPetInstance } from "../api/petApi";

const CACHE_VERSION = "1";
const CACHE_FILE = "cache.json";
const TOKEN_FILE = "token.bin";
const BASE_FILE = "base.txt";

export type LocalCache = {
  version: string;
  activePet: UserPetInstance | null;
  desktopProfilesByPetId: Record<number, DesktopProfile>;
  lastLoadedAt: string;
};

const empty: LocalCache = {
  version: CACHE_VERSION,
  activePet: null,
  desktopProfilesByPetId: {},
  lastLoadedAt: new Date(0).toISOString(),
};

export async function loadCache(): Promise<LocalCache> {
  const raw = await window.akimDeskPet.cache.read(CACHE_FILE);
  if (!raw) {
    return { ...empty };
  }
  try {
    const parsed = JSON.parse(raw) as LocalCache;
    if (parsed.version !== CACHE_VERSION) {
      return { ...empty };
    }
    return {
      ...empty,
      ...parsed,
      desktopProfilesByPetId: parsed.desktopProfilesByPetId || {},
    };
  } catch {
    return { ...empty };
  }
}

export async function saveCache(cache: LocalCache): Promise<void> {
  cache.version = CACHE_VERSION;
  cache.lastLoadedAt = new Date().toISOString();
  await window.akimDeskPet.cache.write(CACHE_FILE, JSON.stringify(cache));
}

// --- Token (encrypted) ------------------------------------------------------

export async function loadToken(): Promise<string | null> {
  const raw = await window.akimDeskPet.cache.read(TOKEN_FILE);
  if (!raw) {
    return null;
  }
  try {
    const blob = JSON.parse(raw) as SecretBlob;
    return await window.akimDeskPet.secret.decrypt(blob);
  } catch {
    return null;
  }
}

export async function saveToken(token: string | null): Promise<void> {
  if (token === null) {
    await window.akimDeskPet.cache.write(TOKEN_FILE, "");
    return;
  }
  const blob = await window.akimDeskPet.secret.encrypt(token);
  await window.akimDeskPet.cache.write(TOKEN_FILE, JSON.stringify(blob));
}

// --- API base URL -----------------------------------------------------------

export async function loadBaseUrl(): Promise<string | null> {
  const raw = await window.akimDeskPet.cache.read(BASE_FILE);
  return raw && raw.trim() ? raw.trim() : null;
}

export async function saveBaseUrl(url: string): Promise<void> {
  await window.akimDeskPet.cache.write(BASE_FILE, url.trim());
}
