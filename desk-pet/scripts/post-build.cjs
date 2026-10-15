// Electron expects .cjs for CommonJS modules when "type":"module" is
// in package.json, but tsconfig only emits .js. Rename the two compiled
// files in dist-electron so Electron's preload/main loaders find them.
//
// We don't set "type":"module" yet (the renderer is plain CJS-bundle
// via Vite), so .js is fine — but this keeps the script in place for
// the day we flip to ESM and need .cjs explicitly.
const fs = require("node:fs");
const path = require("node:path");

const outDir = path.join(__dirname, "..", "dist-electron");

function maybeRename(name) {
  const src = path.join(outDir, name + ".js");
  const dst = path.join(outDir, name + ".cjs");
  if (fs.existsSync(src)) {
    fs.copyFileSync(src, dst);
  }
}

maybeRename("main");
maybeRename("preload");
console.log("post-build: copied main.js → main.cjs, preload.js → preload.cjs");
