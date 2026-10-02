// Copies the Pyodide core runtime from node_modules/pyodide into
// public/pyodide/, where public/workers/pyodideWorker.js looks for it
// first (/pyodide/pyodide.js). Runs from package.json's postinstall, so a
// plain `npm install` / `npm ci` is all anyone needs to do.
//
// public/pyodide/ is gitignored (13 MB of generated files, not source).
//
// Failure policy:
//   - pyodide not installed / files missing  -> warn, exit 0. The worker
//     falls back to the jsdelivr CDN, so nothing is broken, just not
//     self-hosted.
//   - installed version != the worker's PYODIDE_VERSION -> exit 1. The CDN
//     fallback URL and any wheels downloaded from it are version-pinned, so
//     a core/wheel mismatch would fail in confusing ways at run time.
import { cp, mkdir, readFile, rm, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const srcDir = path.join(frontendDir, "node_modules", "pyodide");
const destDir = path.join(frontendDir, "public", "pyodide");
const workerPath = path.join(frontendDir, "public", "workers", "pyodideWorker.js");

// Everything pyodide.js needs at startup. Package wheels (numpy, ...) are
// NOT part of the npm package -- see the note in pyodideWorker.js.
const FILES = ["pyodide.js", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json"];

async function exists(p) {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

async function main() {
  if (!(await exists(path.join(srcDir, "package.json")))) {
    console.warn("[vendor-pyodide] node_modules/pyodide not found; skipping (the worker will use the CDN).");
    return;
  }

  const installed = JSON.parse(await readFile(path.join(srcDir, "package.json"), "utf8")).version;
  const workerSrc = await readFile(workerPath, "utf8");
  const wanted = /PYODIDE_VERSION\s*=\s*"([^"]+)"/.exec(workerSrc)?.[1];
  if (wanted && wanted !== installed) {
    console.error(
      `[vendor-pyodide] version mismatch: node_modules/pyodide is ${installed} but ` +
        `public/workers/pyodideWorker.js has PYODIDE_VERSION = "${wanted}". Make them the same.`
    );
    process.exit(1);
  }

  const missing = [];
  for (const f of FILES) {
    if (!(await exists(path.join(srcDir, f)))) missing.push(f);
  }
  if (missing.length) {
    console.warn(`[vendor-pyodide] node_modules/pyodide ${installed} lacks ${missing.join(", ")}; skipping (the worker will use the CDN).`);
    return;
  }

  // Start clean so files from an older Pyodide version never linger.
  await rm(destDir, { recursive: true, force: true });
  await mkdir(destDir, { recursive: true });
  for (const f of FILES) await cp(path.join(srcDir, f), path.join(destDir, f));
  console.log(`[vendor-pyodide] copied Pyodide ${installed} core (${FILES.length} files) to public/pyodide/`);
}

main().catch((err) => {
  console.warn(`[vendor-pyodide] failed (${err?.message || err}); the worker will use the CDN.`);
});
