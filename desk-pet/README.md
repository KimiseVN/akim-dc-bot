# akim-desk-pet

Desktop client for the **akim-dc-bot** Pet system. Electron + React + TypeScript + Zustand. Talks to the bot's FastAPI module (see `src/desk_api/`) for link, active pet, owned pets, and care actions.

## Status

| Phase | What | Status |
|---|---|---|
| 3.1 — App skeleton | Electron window, link UI, API client, Zustand store | ✅ |
| 3.2 — Sprite renderer | CSS-spritesheet animator, manifest loader, bundled fallback pet | ✅ |
| 3.3 — Motion engine | Window drag, random walk across the work area, click-to-pet | ✅ |
| 3.4 — Object overlays | Food bowl / water bowl / toy / heart spawn during care actions | ✅ |
| 3.5 — Sound + polish | Procedural Web Audio SFX, settings panel, NSIS installer config | ✅ |

### Settings & sound (3.5)

Right-click → **Cài đặt** opens a toggle panel for: tự đi dạo (random walk),
bong bóng thoại (speech bubble), âm thanh (sound), and luôn nổi trên cùng
(always-on-top). Each toggle writes through to the server profile via
`/api/pet/desktop/sync`; always-on-top also drives the live Electron window
flag, and the persisted value is re-applied on launch.

Care actions play a short synthesized motif (`src/pet/sound.ts`) — we use the
Web Audio API rather than Howler.js so there are no audio binaries to bundle
or license. Sound respects the `soundEnabled` toggle.

The NSIS installer is configured in `package.json` (`build`); run
`npm run package` to produce `release/`. It currently uses Electron's default
icon — add a `build.win.icon` path for branding.

### Motion (3.3)

The pet wanders by moving its own transparent window horizontally across the
display's work area (`src/pet/motion.ts`), playing walkLeft/walkRight while
travelling and standing in its mood pose during a random dwell pause. It
holds still while the cursor is over it, while a care animation plays, or
while a menu/modal is open. **Left-click the pet** to vuốt ve (the `pet`
affection action); **right-click** for the full action menu; drag the
speech bubble or stat pill to reposition manually. Honours the
`randomWalkEnabled` desktop profile flag.

## Setup

```bash
cd desk-pet
npm install
```

## Dev

```bash
npm run start:dev
```

Spawns Vite at `http://localhost:5173`, builds the Electron main process, then launches Electron pointing at the dev URL.

## Build installer

```bash
npm run package
```

Output in `release/`. Windows NSIS by default — adjust `package.json` → `build.win.target` for other formats.

## Link flow

1. Set the server URL on the first screen (defaults to `http://localhost:8080` — change to your Railway URL).
2. Click **Tạo link code** → app shows `AKIM-XXXXXX`.
3. In Discord, run `/link-desktop code:AKIM-XXXXXX` in the guild whose pets you want to see.
4. App polls `/api/desktop/link/status` every 2s. When status flips to `linked`, the token gets stored encrypted via Electron's safeStorage in `%APPDATA%/akim-desk-pet/token.bin`.

## Spritesheet layout (OpenPets convention)

Bundled assets follow the OpenPets sheet layout: **8 columns × 9 rows of
192×208 frames** (sheet 1536×1872). The 9 rows, in order, are:

| row | OpenPets pose | akim animation(s) |
|---|---|---|
| 0 | idle | `idle` |
| 1 | run-right | `walkRight` |
| 2 | run-left | `walkLeft` |
| 3 | waving | `petting` (also clean / vuốt ve) |
| 4 | jumping | `happy`, `jump` |
| 5 | failed / head-down | `eat`, `drink`, `play`, `sad` |
| 6 | waiting (low/lying) | `sleep` |
| 7 | running in place | (unused) |
| 8 | review | `train`, `sick` |

`pet.json` declares `frameWidth`/`frameHeight`/`rows` plus the per-state row,
frame count, duration, and iterations. A new pet only needs a sheet in this
layout + a matching `pet.json`.

## Care actions

The right-click menu exposes the six care features, mapped to the bot's
`care.py` and animated via the rows above: **Cho ăn** (feed), **Cho uống**
(drink/water), **Chơi** (play), **Training** (exercise — costs energy),
**Tắm rửa** (clean), **Ngủ** (sleep/rest). Left-clicking the pet is a quick
**vuốt ve** (pet) affection tap. Each runs through `/api/pet/action/<verb>`.

## Notes

- Stat semantics follow the bot's native model (hunger/thirst/cleanliness = fullness, high = good).
- One link = one guild. To switch guilds, hit ⏏ (sign out) and re-link from that server.
- Bundled assets live in `assets/pets/{assetId}/`. The bot maps a per-guild species to a bundled asset via `/pet-admin link-desktop-asset species_id:<x> desktop_asset_id:<id>`.
