// Tests for public/workers/pyodideWorker.js's loader (item 3 of the Build-tab
// audit). Uses a fake loadPyodide, so no WASM is downloaded.
// Run: node frontend/app/hooks/__tests__/pyodideWorker.test.mjs
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const workerPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../public/workers/pyodideWorker.js");
const workerSrc = fs.readFileSync(workerPath, "utf8");

let failures = 0;
function assert(cond, msg) {
  if (!cond) { failures++; console.error(`FAIL: ${msg}`); } else { console.log(`PASS: ${msg}`); }
}

// Fresh worker sandbox per scenario. `sources` maps a base URL to "ok" | "fail".
function makeWorker({ sources, packageProblem = null }) {
  const posted = [];
  const imported = [];
  const loadCalls = [];
  const sandbox = { console };
  sandbox.self = sandbox;
  sandbox.postMessage = (m) => posted.push(m);
  sandbox.importScripts = (url) => {
    imported.push(url);
    const base = url.replace(/pyodide\.js$/, "");
    if (sources[base] !== "ok") throw new Error(`NetworkError: ${url} failed to load`);
    sandbox.loadPyodide = async (opts) => {
      loadCalls.push(opts);
      return {
        runPython() {},
        setStdout() {}, setStderr() {},
        globals: { set() {} },
        loadedPackages: {},
        async loadPackagesFromImports(code, o) { if (packageProblem && /import numpy/.test(code)) o.errorCallback(packageProblem); },
        async loadPackage() {},
        async runPythonAsync() { return "[]"; },
      };
    };
  };
  vm.createContext(sandbox);
  vm.runInContext(workerSrc, sandbox);
  let id = 0;
  const run = async (code) => { const my = ++id; await sandbox.onmessage({ data: { id: my, code } }); return posted.find((p) => p.id === my); };
  return { run, imported, loadCalls, sources };
}

const LOCAL = "/pyodide/";
const CDN = /^https:\/\/cdn\.jsdelivr\.net\/pyodide\/v314\.0\.\d+\/full\/$/;
const cdnBase = (w) => w.imported.map((u) => u.replace(/pyodide\.js$/, "")).find((b) => CDN.test(b));

// 1. Local copy present -> used first, CDN never touched.
{
  const w = makeWorker({ sources: { [LOCAL]: "ok" } });
  const r = await w.run("print(1)");
  assert(r.status === "ok", "runs when the self-hosted copy loads");
  assert(w.imported.length === 1 && w.imported[0] === "/pyodide/pyodide.js", "self-hosted core is tried first and the CDN is not contacted");
  assert(w.loadCalls[0].indexURL === LOCAL, "loadPyodide gets indexURL pointing at the local copy");
}

// 2. Local missing -> falls back to the CDN.
{
  const w = makeWorker({ sources: {} });
  // find the CDN base the worker will use, then mark it ok
  await w.run("print(1)");
  const base = cdnBase(w);
  assert(!!base, "CDN fallback URL is version-pinned jsdelivr");
  const w2 = makeWorker({ sources: { [base]: "ok" } });
  const r = await w2.run("print(1)");
  assert(r.status === "ok", "falls back to the CDN when the local copy is missing");
  assert(w2.imported[0] === "/pyodide/pyodide.js" && w2.imported[1] === base + "pyodide.js", "order is local, then CDN");
}

// 3. Both fail -> one error naming both sources; a LATER run retries (failure is not cached).
{
  const w = makeWorker({ sources: {} });
  const r1 = await w.run("print(1)");
  assert(r1.status === "error" && /self-hosted/.test(r1.error) && /jsdelivr CDN/.test(r1.error), "error names both sources that were tried");
  assert(/npm install/.test(r1.error), "error tells the user how to fix the self-hosted copy");
  const before = w.imported.length;
  await w.run("print(2)");
  assert(w.imported.length > before, "a failed load is retried on the next Run instead of being cached forever");
  w.sources;
}

// 4. Failed load, then the source comes back -> next Run succeeds without a page reload.
{
  const sources = {};
  const w = makeWorker({ sources });
  const r1 = await w.run("print(1)");
  sources[LOCAL] = "ok";
  const r2 = await w.run("print(1)");
  assert(r1.status === "error" && r2.status === "ok", "recovers on a later Run once the runtime becomes reachable");
}

// 5. Package download problems are surfaced, not swallowed into a ModuleNotFoundError.
{
  const w = makeWorker({ sources: { [LOCAL]: "ok" }, packageProblem: "Failed to load numpy: request failed." });
  const r = await w.run("import numpy as np");
  assert(r.status === "error" && /Could not download the Python packages/.test(r.error) && /numpy/.test(r.error), "failed package download becomes an explicit error");
  const r2 = await w.run("print('no imports')");
  assert(r2.status === "ok", "code with no imports is unaffected by package problems");
}

if (failures > 0) { console.error(`\n${failures} assertion(s) failed.`); process.exit(1); }
console.log("\nAll assertions passed.");
