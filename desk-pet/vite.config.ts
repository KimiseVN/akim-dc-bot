import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Electron loads dist/index.html via the file:// scheme in prod and via
// the Vite dev server (http://localhost:5173) in dev — so use a
// relative base or assets won't resolve in the packaged build.
//
// publicDir copies straight into dist/ at build time, so
// public/assets/pets/moon_cat/spritesheet.webp lands at
// dist/assets/pets/moon_cat/spritesheet.webp. The bundle JS goes
// into dist/bundle/ instead of dist/assets/ to avoid pet asset paths
// colliding with Vite's hashed JS filenames.
export default defineConfig({
  base: "./",
  plugins: [react()],
  publicDir: "public",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "bundle",
  },
  server: {
    port: 5173,
    strictPort: true,
  },
});
