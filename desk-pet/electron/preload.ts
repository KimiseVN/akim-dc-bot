// Renderer-facing API exposed via contextBridge. Keeps the renderer
// sandboxed (no fs / process access) while still letting it persist
// state through the main process.
import { contextBridge, ipcRenderer } from "electron";

type SecretBlob = { encrypted: boolean; payload: string };
type Rect = { x: number; y: number; width: number; height: number };

const api = {
  cache: {
    read: (name: string) => ipcRenderer.invoke("cache:read", name) as Promise<string | null>,
    write: (name: string, content: string) =>
      ipcRenderer.invoke("cache:write", name, content) as Promise<boolean>,
  },
  secret: {
    encrypt: (plaintext: string) =>
      ipcRenderer.invoke("secret:encrypt", plaintext) as Promise<SecretBlob>,
    decrypt: (blob: SecretBlob | null) =>
      ipcRenderer.invoke("secret:decrypt", blob) as Promise<string | null>,
  },
  window: {
    setAlwaysOnTop: (on: boolean) =>
      ipcRenderer.invoke("window:set-always-on-top", on) as Promise<boolean>,
    hide: () => ipcRenderer.invoke("window:hide") as Promise<boolean>,
    getBounds: () =>
      ipcRenderer.invoke("window:get-bounds") as Promise<Rect | null>,
    setPosition: (x: number, y: number) =>
      ipcRenderer.invoke("window:set-position", x, y) as Promise<boolean>,
    setSize: (width: number, height: number) =>
      ipcRenderer.invoke("window:set-size", width, height) as Promise<boolean>,
  },
  screen: {
    getWorkArea: () =>
      ipcRenderer.invoke("screen:get-work-area") as Promise<Rect | null>,
  },
};

contextBridge.exposeInMainWorld("akimDeskPet", api);

declare global {
  // For renderer-side typing — duplicated in src/electron.d.ts so the
  // renderer's tsconfig sees it without pulling in electron.
}
export type AkimDeskPetApi = typeof api;
