// Electron main process — owns the BrowserWindow lifecycle and the
// IPC bridge that lets the renderer use Electron's safeStorage to
// encrypt the access token at rest. The renderer NEVER touches Node
// APIs directly (contextIsolation:true) — preload.ts mediates.
import { app, BrowserWindow, ipcMain, Menu, safeStorage, screen, Tray } from "electron";
import * as fs from "node:fs";
import * as path from "node:path";

const isDev = process.env.DESK_PET_DEV === "1";
const DEV_URL = "http://localhost:5173";

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;

// Where we keep the encrypted token + cache files. userData path is
// platform-correct: %APPDATA%/akim-desk-pet on Windows, ~/.config on
// Linux, ~/Library/Application Support on macOS.
function cachePath(name: string): string {
  return path.join(app.getPath("userData"), name);
}

function createWindow(): void {
  // Compact OpenPets-style proportions sized to the real sprite frame
  // (192×208 at scale 1.0) plus the speech bubble above and the mini
  // stat panel below. 300×470 leaves the cat clear of both without
  // clipping its feet into the stat panel.
  mainWindow = new BrowserWindow({
    width: 300,
    height: 470,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    hasShadow: false,
    skipTaskbar: false,
    backgroundColor: "#00000000",
    title: "Akim Desk Pet",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // Hide the menu bar — pet window doesn't need File/Edit etc.
  Menu.setApplicationMenu(null);

  if (isDev) {
    mainWindow.loadURL(DEV_URL);
    mainWindow.webContents.openDevTools({ mode: "detach" });
  } else {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// --- Tray with Exit / Toggle window ----------------------------------------

function createTray(): void {
  // A 1x1 transparent PNG so the tray icon always loads — packaged
  // installer can swap in a real icon via assets/tray.png later.
  const iconPath = path.join(__dirname, "..", "assets", "tray.png");
  let trayIcon: Electron.NativeImage;
  try {
    if (fs.existsSync(iconPath)) {
      trayIcon = require("electron").nativeImage.createFromPath(iconPath);
    } else {
      trayIcon = require("electron").nativeImage.createFromBuffer(
        Buffer.from(
          // 1x1 transparent PNG
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
          "base64",
        ),
      );
    }
  } catch {
    return;
  }
  tray = new Tray(trayIcon);
  tray.setToolTip("Akim Desk Pet");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: "Hiện cửa sổ",
        click: () => mainWindow?.show(),
      },
      {
        label: "Ẩn cửa sổ",
        click: () => mainWindow?.hide(),
      },
      { type: "separator" },
      { label: "Thoát", click: () => app.quit() },
    ]),
  );
  tray.on("click", () => {
    if (!mainWindow) {
      return;
    }
    mainWindow.isVisible() ? mainWindow.hide() : mainWindow.show();
  });
}

// --- IPC: cache + safeStorage ----------------------------------------------

// Renderer writes/reads opaque JSON strings via these handlers. The
// auth-token blob runs through safeStorage so the cache file isn't
// plaintext on disk. Everything else (active pet, profile) is plain
// JSON since losing it is harmless.
ipcMain.handle("cache:read", async (_e, name: string) => {
  try {
    const data = await fs.promises.readFile(cachePath(name), "utf8");
    return data;
  } catch {
    return null;
  }
});

ipcMain.handle("cache:write", async (_e, name: string, content: string) => {
  await fs.promises.mkdir(path.dirname(cachePath(name)), { recursive: true });
  await fs.promises.writeFile(cachePath(name), content, "utf8");
  return true;
});

ipcMain.handle("secret:encrypt", async (_e, plaintext: string) => {
  if (!safeStorage.isEncryptionAvailable()) {
    // No OS-level keychain — return a marker so the renderer knows it
    // can't be sure of secret safety. Better to store plaintext than
    // to crash; the threat model is a stolen laptop, not a state
    // actor with disk access.
    return { encrypted: false, payload: plaintext };
  }
  const buf = safeStorage.encryptString(plaintext);
  return { encrypted: true, payload: buf.toString("base64") };
});

ipcMain.handle(
  "secret:decrypt",
  async (
    _e,
    blob: { encrypted: boolean; payload: string } | null,
  ): Promise<string | null> => {
    if (!blob) {
      return null;
    }
    if (!blob.encrypted) {
      return blob.payload;
    }
    if (!safeStorage.isEncryptionAvailable()) {
      return null;
    }
    try {
      return safeStorage.decryptString(Buffer.from(blob.payload, "base64"));
    } catch {
      return null;
    }
  },
);

// Always-on-top toggle from the renderer right-click menu.
ipcMain.handle("window:set-always-on-top", async (_e, on: boolean) => {
  mainWindow?.setAlwaysOnTop(!!on);
  return true;
});

// Hide the window to the tray (e.g. the × on the link panel). The app
// keeps running; the tray "Hiện cửa sổ" item brings it back, and tray
// "Thoát" quits for good.
ipcMain.handle("window:hide", async () => {
  mainWindow?.hide();
  return true;
});

// --- IPC: window geometry for the random-walk motion engine ----------------
// The renderer drives the walk loop (it owns the timing + animation state)
// but can't touch the BrowserWindow directly under contextIsolation, so it
// reaches geometry through these handlers.

ipcMain.handle("window:get-bounds", async () => {
  return mainWindow ? mainWindow.getBounds() : null;
});

ipcMain.handle("window:set-position", async (_e, x: number, y: number) => {
  if (!mainWindow) return false;
  // setPosition throws on non-finite ints; a single NaN from a renderer
  // math glitch would otherwise crash the walk loop's IPC call.
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  mainWindow.setPosition(Math.round(x), Math.round(y));
  return true;
});

ipcMain.handle("screen:get-work-area", async () => {
  if (!mainWindow) return null;
  // workArea (not full bounds) keeps the pet above the taskbar; matching
  // on the window's current rect keeps it on its own monitor.
  return screen.getDisplayMatching(mainWindow.getBounds()).workArea;
});

ipcMain.handle("window:set-size", async (_e, width: number, height: number) => {
  if (!mainWindow) return false;
  if (!Number.isFinite(width) || !Number.isFinite(height)) return false;
  // setSize works even though the window is resizable:false (that flag
  // only blocks user drag-resize). Clamp to a sane floor so a mis-measure
  // can't collapse the window to nothing.
  mainWindow.setSize(
    Math.max(160, Math.round(width)),
    Math.max(120, Math.round(height)),
  );
  return true;
});

// --- Lifecycle --------------------------------------------------------------

app.whenReady().then(() => {
  createWindow();
  createTray();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  // Keep the tray icon alive on Windows/Linux so the user can re-open
  // the window. Real quit only via the tray Thoát item or Cmd-Q on mac.
  if (process.platform === "darwin") {
    return;
  }
  // Don't quit — tray stays. User has to click Thoát.
});
