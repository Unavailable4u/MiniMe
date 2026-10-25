// Patch B12 (M2C build plan): the Pyodide version has to be the same number in
// four places, or the install / the run-time CDN fallback breaks in confusing
// ways:
//   1. package.json            "pyodide": "<exact version>"   (no range)
//   2. package-lock.json       the root "dependencies" entry AND the resolved
//                              node_modules/pyodide entry (version + tarball URL)
//   3. public/workers/pyodideWorker.js   const PYODIDE_VERSION = "<same>"
//   4. node_modules/pyodide    what is actually installed (only checked when
//                              present, so the test also runs before `npm ci`)
// scripts/vendor-pyodide.mjs already refuses to finish an install when (3) and
// (4) differ; this test catches (1)/(2)/(3) drifting apart in a pull request,
// before anyone runs an install. It reads files only: no network, no WASM.
// Run: node frontend/app/hooks/__tests__/pyodideVersionLockstep.test.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const readJson = (rel) => JSON.parse(fs.readFileSync(path.join(frontendDir, rel), "utf8"));

let failures = 0;
function assert(cond, msg) {
  if (!cond) { failures++; console.error(`FAIL: ${msg}`); } else { console.log(`PASS: ${msg}`); }
}

const pkg = readJson("package.json");
const lock = readJson("package-lock.json");
const workerSrc = fs.readFileSync(path.join(frontendDir, "public/workers/pyodideWorker.js"), "utf8");

const pinned = pkg.dependencies?.pyodide;
const workerVersion = /PYODIDE_VERSION\s*=\s*"([^"]+)"/.exec(workerSrc)?.[1];
const lockRoot = lock.packages?.[""]?.dependencies?.pyodide;
const lockEntry = lock.packages?.["node_modules/pyodide"];

// An exact, stable pin: three numeric parts, no ^ ~ >= ranges, no -alpha/-rc.
assert(typeof pinned === "string" && /^\d+\.\d+\.\d+$/.test(pinned), `package.json pins pyodide exactly, with no range or prerelease (got ${JSON.stringify(pinned)})`);
assert(typeof workerVersion === "string" && /^\d+\.\d+\.\d+$/.test(workerVersion), `the worker's PYODIDE_VERSION is a stable x.y.z (got ${JSON.stringify(workerVersion)})`);

assert(workerVersion === pinned, `worker PYODIDE_VERSION (${workerVersion}) equals the package.json pin (${pinned})`);
assert(lockRoot === pinned, `package-lock.json root dependency (${lockRoot}) equals the package.json pin (${pinned})`);
assert(lockEntry?.version === pinned, `package-lock.json resolved version (${lockEntry?.version}) equals the package.json pin (${pinned})`);
assert(
  typeof lockEntry?.resolved === "string" && lockEntry.resolved.endsWith(`/pyodide-${pinned}.tgz`),
  `package-lock.json tarball URL points at pyodide-${pinned}.tgz (got ${lockEntry?.resolved})`,
);
assert(typeof lockEntry?.integrity === "string" && lockEntry.integrity.startsWith("sha512-"), "package-lock.json carries a sha512 integrity hash for pyodide");

// The self-hosted core and the CDN fallback must be the same Python build.
const cdnLine = /CDN_BASE\s*=\s*`([^`]+)`/.exec(workerSrc)?.[1] ?? "";
assert(cdnLine.includes("${PYODIDE_VERSION}"), "the CDN fallback URL is built from PYODIDE_VERSION, not a second hard-coded number");
assert(!/pyodide\/v\d+\.\d+\.\d+\//.test(workerSrc), "no other literal pyodide/vX.Y.Z URL is hard-coded in the worker");

// Installed copy (skipped when node_modules is absent, e.g. before `npm ci`).
const installedPkg = path.join(frontendDir, "node_modules/pyodide/package.json");
if (fs.existsSync(installedPkg)) {
  const installed = JSON.parse(fs.readFileSync(installedPkg, "utf8")).version;
  assert(installed === pinned, `node_modules/pyodide (${installed}) equals the package.json pin (${pinned})`);
} else {
  console.log("SKIP: node_modules/pyodide not installed; installed-version check not run");
}

if (failures) { console.error(`${failures} failure(s)`); process.exit(1); }
console.log("all pyodide version lock-step checks passed");
