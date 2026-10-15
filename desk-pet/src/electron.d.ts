// Renderer-side type shim for the contextBridge API exposed by
// electron/preload.ts. Keep in sync with that file's `api` object.
export type SecretBlob = { encrypted: boolean; payload: string };
export type Rect = { x: number; y: number; width: number; height: number };

export type AkimDeskPetApi = {
  cache: {
    read: (name: string) => Promise<string | null>;
    write: (name: string, content: string) => Promise<boolean>;
  };
  secret: {
    encrypt: (plaintext: string) => Promise<SecretBlob>;
    decrypt: (blob: SecretBlob | null) => Promise<string | null>;
  };
  window: {
    setAlwaysOnTop: (on: boolean) => Promise<boolean>;
    hide: () => Promise<boolean>;
    getBounds: () => Promise<Rect | null>;
    setPosition: (x: number, y: number) => Promise<boolean>;
    setSize: (width: number, height: number) => Promise<boolean>;
  };
  screen: {
    getWorkArea: () => Promise<Rect | null>;
  };
};

declare global {
  interface Window {
    akimDeskPet: AkimDeskPetApi;
  }
}

export {};
