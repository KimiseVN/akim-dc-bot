/// <reference types="vite/client" />

// Typed env vars exposed via import.meta.env. Keep in sync with any
// VITE_-prefixed keys read in the app (see api/petApi.ts, store/petStore.ts).
interface ImportMetaEnv {
  readonly VITE_DESK_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
