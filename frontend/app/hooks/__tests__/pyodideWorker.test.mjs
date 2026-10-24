// Tests for public/workers/pyodideWorker.js's loader (item 3 of the Build-tab
// audit). Uses a fake loadPyodide, so no WASM is downloaded.
//
// Patch B08 (M2C build plan) extends this file with the chat-artifact runner
// hardening: (a) lib/pyodideRunner.js -- timeout, Stop, restart -- driven by
// a fake Worker class; (b) the worker's Python wrapper -- tracebacks on the
// error stream, a clear input() message -- executed for real in CPython
// (python3) behind the same fake loader, so the Python text is what is tested,
// not a copy of it; (c) source-level checks that ArtifactRenderer.jsx really
// renders a Stop button wired to stop() (the component itself cannot be
// rendered under plain node; see PreviewPane.python.test.mjs for the same
// approach). If python3 is not on PATH, group (b) is SKIPPED loudly.
// Run: node frontend/app/hooks/__tests__/pyodideWorker.test.mjs
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { parse } from "@babel/parser";
import { loadSource } from "../../lib/workbench/__tests__/loadSource.mjs";

const workerPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../public/workers/pyodideWorker.js");
const workerSrc = fs.readFileSync(workerPath, "utf8");

let failures = 0;
function assert(cond, msg) {
  if (!cond) { failures++; console.error(`FAIL: ${msg}`); } else { console.log(`PASS: ${msg}`); }
}

// B08: run the worker's REAL Python wrapper text in CPython and feed its
// stdout/stderr lines to the callbacks the worker registered through
// setStdout/setStderr (Pyodide's `batched` mode hands over one line at a
// time, without the newline). Throws if the wrapper itself dies, which is
// what Pyodide does too (runPythonAsync rejects with a PythonError).
const PY = (() => { try { return spawnSync("python3", ["--version"]).status === 0 ? "python3" : null; } catch { return null; } })();
function feedLines(text, cb) {
  if (!cb || !text) return;
  const lines = text.split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  for (const l of lines) cb(l);
}
function runWrapperInCPython(wrapper, globalsSet, onOut, onErr) {
  const harness = "import sys, json\nd = json.loads(sys.stdin.read())\ng = dict(d['globals'])\nexec(d['wrapper'], g)\n";
  const r = spawnSync(PY, ["-c", harness], { input: JSON.stringify({ wrapper, globals: globalsSet }), encoding: "utf8" });
  feedLines(r.stdout, onOut);
  feedLines(r.stderr, onErr);
  if (r.status !== 0) throw new Error(`CPython exited ${r.status}: ${r.stderr}`);
  return "[]";
}

// Fresh worker sandbox per scenario. `sources` maps a base URL to "ok" | "fail".
function makeWorker({ sources, packageProblem = null, python = false }) {
  const posted = [];
  const globalsSet = {};
  let stdoutCb = null;
  let stderrCb = null;
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
        setStdout(o) { stdoutCb = o && o.batched; }, setStderr(o) { stderrCb = o && o.batched; },
        globals: { set(k, v) { globalsSet[k] = v; } },
        loadedPackages: {},
        async loadPackagesFromImports(code, o) { if (packageProblem && /import numpy/.test(code)) o.errorCallback(packageProblem); },
        async loadPackage() {},
        async runPythonAsync(wrapper) { return python ? runWrapperInCPython(wrapper, globalsSet, stdoutCb, stderrCb) : "[]"; },
      };
    };
  };
  vm.createContext(sandbox);
  vm.runInContext(workerSrc, sandbox);
  let id = 0;
  const run = async (code) => { const my = ++id; await sandbox.onmessage({ data: { id: my, code } }); return posted.find((p) => p.id === my); };
  return { run, imported, loadCalls, sources, posted };
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

// ===========================================================================
// Patch B08 -- worker wrapper behaviour (real CPython behind the fake loader)
// ===========================================================================

// 6. The worker announces that execution is about to start, AFTER the runtime
//    and packages are loaded -- the hook only starts its run timeout then, so
//    a slow first-time download is never mistaken for a runaway script. The
//    message carries no `id` field on purpose: the older assertions above find
//    a run's result with posted.find(p => p.id === n).
{
  const w = makeWorker({ sources: { [LOCAL]: "ok" } });
  const r = await w.run("print(1)");
  const startedAt = w.posted.findIndex((p) => p.type === "started" && p.runId === 1);
  const resultAt = w.posted.findIndex((p) => p.id === 1);
  assert(startedAt >= 0 && resultAt > startedAt, "worker posts {type:'started', runId} before the result");
  assert(!("id" in w.posted[startedAt]), "the started message has no `id`, so result lookup by id is unaffected");
  assert(r.status === "ok", "the result message is unchanged");
  const bad = makeWorker({ sources: {} });
  await bad.run("print(1)");
  assert(!bad.posted.some((p) => p.type === "started"), "no started message when the runtime failed to load");
}

if (!PY) {
  console.warn("SKIP: python3 not found on PATH -- the worker-wrapper tests (traceback stream, input() message) did NOT run.");
} else {
  // 7. Tracebacks go to the error stream (stderr -> the existing amber styling).
  {
    const w = makeWorker({ sources: { [LOCAL]: "ok" }, python: true });
    const ok = await w.run('print("hi")');
    assert(ok.status === "ok" && ok.stdout === "hi\n" && ok.stderr === "", "plain print still lands on stdout and stderr stays empty");
    const t = await w.run('def f():\n    raise ValueError("boom")\nf()\n');
    assert(t.status === "ok", "a Python exception is a finished run with output, not a worker error");
    assert(/Traceback \(most recent call last\)/.test(t.stderr) && /ValueError: boom/.test(t.stderr), "the traceback is delivered on the error stream (stderr)");
    assert(!/Traceback|ValueError/.test(t.stdout), "the traceback no longer leaks into stdout");
    const after = await w.run('print("after")');
    assert(after.stdout === "after\n" && after.stderr === "", "output of a failed run does not leak into the next run");
  }

  // 8. input() gives one clear message instead of hanging or an obscure error.
  {
    const w = makeWorker({ sources: { [LOCAL]: "ok" }, python: true });
    const r = await w.run('name = input("Name? ")\nprint("hello", name)');
    assert(/input\(\) is not supported in the browser runner/.test(r.stderr), "input() reports that it is not supported in the browser runner (on the error stream)");
    assert(/RuntimeError/.test(r.stderr) && r.stdout === "", "it surfaces as a normal Python error and nothing else is printed");
    assert(/variable/.test(r.stderr), "the message tells the user what to do instead");
    const r2 = await w.run("import sys\nsys.stdin.readline()");
    assert(/input\(\) is not supported in the browser runner/.test(r2.stderr), "sys.stdin.readline() gets the same message");
    const r3 = await w.run("import sys\nfor line in sys.stdin:\n    pass");
    assert(/input\(\) is not supported in the browser runner/.test(r3.stderr), "iterating sys.stdin gets the same message");
    const r4 = await w.run('try:\n    input()\nexcept RuntimeError as e:\n    print("caught")');
    assert(r4.stdout === "caught\n", "code can still catch it like any other error");
  }
}

// ===========================================================================
// Patch B08 -- lib/pyodideRunner.js (timeout, Stop, restart) with a fake Worker
// ===========================================================================
const { createPyodideRunner, DEFAULT_RUN_TIMEOUT_MS, DEFAULT_LOAD_TIMEOUT_MS } = loadSource("../../pyodideRunner.js");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const HUNG = Symbol("hung promise");
const settle = (p, ms = 1500) => Promise.race([p, sleep(ms).then(() => HUNG)]);

// A stand-in for the browser Worker. `emit` is what a live worker can do;
// `forceEmit` is a late/buggy delivery from a worker that was already terminated.
function makeFleet(behavior = () => {}) {
  const instances = [];
  class FakeWorker {
    constructor(url) { this.url = url; this.terminated = false; this.posted = []; this.onmessage = null; this.onerror = null; instances.push(this); }
    postMessage(msg) { if (this.terminated) throw new Error("postMessage on a terminated worker"); this.posted.push(msg); behavior(this, msg, instances); }
    terminate() { this.terminated = true; }
    emit(data) { if (!this.terminated && this.onmessage) this.onmessage({ data }); }
    forceEmit(data) { if (this.onmessage) this.onmessage({ data }); }
    fail(message) { if (!this.terminated && this.onerror) this.onerror({ message }); }
  }
  return { instances, createWorker: () => new FakeWorker("/workers/pyodideWorker.js") };
}
const started = (w, m) => w.emit({ type: "started", runId: m.id });
const finished = (w, m, stdout) => w.emit({ id: m.id, status: "ok", stdout, stderr: "", images: [] });
const echoOk = (w, m) => { started(w, m); finished(w, m, `ran:${m.code}\n`); };
const hangAfterStart = (w, m) => started(w, m);
// first worker spins forever (like `while True: pass`), every later one works
const firstHangsThenOk = (w, m, instances) => (instances.indexOf(w) === 0 ? hangAfterStart(w, m) : echoOk(w, m));

assert(DEFAULT_RUN_TIMEOUT_MS === 30000, "default run timeout is 30 s");
assert(DEFAULT_LOAD_TIMEOUT_MS > DEFAULT_RUN_TIMEOUT_MS, "runtime loading gets a more generous limit than a run");

// R1. lazy creation, normal result, and the timer is cleared on completion.
{
  const fleet = makeFleet(echoOk);
  const runner = createPyodideRunner({ createWorker: fleet.createWorker, timeoutMs: 60 });
  assert(fleet.instances.length === 0, "no worker is created until the first run()");
  const r = await settle(runner.run("print(1)"));
  assert(r !== HUNG && r.status === "ok" && r.stdout === "ran:print(1)\n", "a normal run resolves with the worker's payload");
  assert(fleet.instances.length === 1 && fleet.instances[0].posted[0].code === "print(1)", "the code is posted to the worker");
  assert(runner.isRunning() === false, "isRunning() is false once the run is done");
  await sleep(150);
  assert(fleet.instances.length === 1 && !fleet.instances[0].terminated, "a finished run's timer is cleared -- no restart later");
}

// R2. timeout terminates, restarts, resolves with a clear status; next run works.
{
  const fleet = makeFleet(firstHangsThenOk);
  const runner = createPyodideRunner({ createWorker: fleet.createWorker, timeoutMs: 40 });
  const r1 = await settle(runner.run("while True: pass"));
  assert(r1 !== HUNG, "a timed-out run never leaves the promise hanging");
  assert(r1.status === "timeout" && /timed out/i.test(r1.message), "it resolves with status 'timeout' and a clear message");
  assert(fleet.instances[0].terminated, "the stuck worker is terminated");
  assert(fleet.instances.length === 2 && !fleet.instances[1].terminated, "a fresh worker is created in its place");
  assert(runner.isRunning() === false, "isRunning() is false after a timeout");
  const r2 = await settle(runner.run("print(2)"));
  assert(r2 !== HUNG && r2.status === "ok" && r2.stdout === "ran:print(2)\n", "the next run works");
  assert(fleet.instances[1].posted.length === 1 && fleet.instances[0].posted.length === 1, "the next run went to the fresh worker, not the old one");
  assert(fleet.instances.length === 2, "no further restarts happened");
}

// R3. the timeout is configurable per runner and per call; 0 / Infinity mean no limit.
{
  const fleet = makeFleet(hangAfterStart);
  const runner = createPyodideRunner({ createWorker: fleet.createWorker, timeoutMs: 5000 });
  const t0 = Date.now();
  const r = await settle(runner.run("x", { timeoutMs: 30 }));
  assert(r !== HUNG && r.status === "timeout" && Date.now() - t0 < 1500, "a per-call timeoutMs overrides the default");
  const p = runner.run("y", { timeoutMs: 0 });
  assert(await settle(p, 120) === HUNG, "timeoutMs: 0 means no run timeout (still pending after 120 ms)");
  runner.stop();
  assert((await settle(p)).status === "stopped", "...and Stop still ends it");
}

// R4. Stop: resolves 'stopped', terminates, restarts, next run works, no late timeout.
{
  const fleet = makeFleet(firstHangsThenOk);
  const runner = createPyodideRunner({ createWorker: fleet.createWorker, timeoutMs: 60 });
  const p = runner.run("while True: pass");
  await sleep(10);
  assert(runner.isRunning() === true, "isRunning() is true while code runs");
  assert(runner.stop() === true, "stop() reports that it stopped something");
  const r = await settle(p);
  assert(r !== HUNG && r.status === "stopped" && /stopped/i.test(r.message), "a stopped run resolves with status 'stopped' and a message");
  assert(fleet.instances[0].terminated && fleet.instances.length === 2 && !fleet.instances[1].terminated, "Stop terminates the worker and creates a fresh one");
  assert(runner.isRunning() === false, "isRunning() is false after Stop");
  await sleep(120);
  assert(fleet.instances.length === 2, "the cancelled run's timeout did not fire afterwards");
  const r2 = await settle(runner.run("print(2)"));
  assert(r2 !== HUNG && r2.status === "ok", "the next run after Stop works on the fresh worker");
}

// R5. Stop with nothing running is a harmless no-op.
{
  const fleet = makeFleet(echoOk);
  const runner = createPyodideRunner({ createWorker: fleet.createWorker });
  assert(runner.stop() === false && fleet.instances.length === 0, "stop() with no run does nothing and creates nothing");
  await settle(runner.run("1"));
  assert(runner.stop() === false && !fleet.instances[0].terminated, "stop() after a finished run does not touch the worker");
}

// R6. Stop while the runtime is still loading (before 'started').
{
  const fleet = makeFleet(() => {});
  const runner = createPyodideRunner({ createWorker: fleet.createWorker, timeoutMs: 30 });
  const p = runner.run("print(1)");
  await sleep(5);
  runner.stop();
  const r = await settle(p);
  assert(r !== HUNG && r.status === "stopped", "Stop works during runtime loading too");
}

// R7. Runtime loading is NOT counted against the run timeout; it has its own limit.
{
  const slowLoad = (w, m) => { setTimeout(() => started(w, m), 80); setTimeout(() => finished(w, m, "ok\n"), 95); };
  const fleet = makeFleet(slowLoad);
  const runner = createPyodideRunner({ createWorker: fleet.createWorker, timeoutMs: 40 });
  const r = await settle(runner.run("print(1)"));
  assert(r !== HUNG && r.status === "ok", "an 80 ms load does not trip a 40 ms run timeout (the clock starts at 'started')");
  const stuck = makeFleet(() => {});
  const runner2 = createPyodideRunner({ createWorker: stuck.createWorker, timeoutMs: 5000, loadTimeoutMs: 30 });
  const r2 = await settle(runner2.run("print(1)"));
  assert(r2 !== HUNG && r2.status === "timeout" && /loading/i.test(r2.message), "a runtime that never starts hits the load limit with its own message");
  assert(stuck.instances[0].terminated && stuck.instances.length === 2, "...and the worker is restarted");
}

// R8. Late messages from a terminated worker cannot resolve or disturb a later run.
{
  const fleet = makeFleet((w, m, instances) => { if (instances.indexOf(w) === 0) hangAfterStart(w, m); else { started(w, m); setTimeout(() => finished(w, m, "fresh\n"), 40); } });
  const runner = createPyodideRunner({ createWorker: fleet.createWorker, timeoutMs: 500 });
  await settle(runner.run("while True: pass", { timeoutMs: 20 })); // times out; run id 1
  const p2 = runner.run("print(2)"); // run id 2 on the fresh worker
  fleet.instances[0].forceEmit({ id: 2, status: "ok", stdout: "STALE\n", stderr: "", images: [] });
  fleet.instances[0].forceEmit({ type: "started", runId: 2 });
  const r2 = await settle(p2, 1500);
  assert(r2 !== HUNG && r2.stdout === "fresh\n", "a stale result from the old worker is ignored");
}

// R9. Errors: worker 'error' status rejects; a worker-level error rejects and the next run recovers.
{
  const fleet = makeFleet((w, m) => { if (m.code === "bad") w.emit({ id: m.id, status: "error", error: "Could not load the Python runtime" }); else echoOk(w, m); });
  const runner = createPyodideRunner({ createWorker: fleet.createWorker });
  let err = null;
  try { await settle(runner.run("bad")); } catch (e) { err = e; }
  assert(err instanceof Error && /Could not load the Python runtime/.test(err.message), "a worker 'error' result rejects with an Error carrying its message");
  assert((await settle(runner.run("ok"))).status === "ok", "the same worker still serves the next run");

  const fleet2 = makeFleet((w) => { if (fleet2.instances.indexOf(w) === 0) setTimeout(() => w.fail("Failed to fetch worker script"), 5); else echoOk(w, { id: 2, code: "x" }); });
  const runner2 = createPyodideRunner({ createWorker: fleet2.createWorker, timeoutMs: 500 });
  let err2 = null;
  const t0 = Date.now();
  try { await settle(runner2.run("x")); } catch (e) { err2 = e; }
  assert(err2 && /Failed to fetch worker script/.test(err2.message) && Date.now() - t0 < 400, "a worker-level error rejects the run immediately (not after the timeout)");
  assert(fleet2.instances[0].terminated, "the broken worker is discarded");
  const fleet3 = makeFleet(echoOk);
  const runner3 = createPyodideRunner({ createWorker: fleet3.createWorker });
  fleet3.instances.length = 0;
  assert((await settle(runner3.run("x"))).status === "ok" && fleet3.instances.length === 1, "a runner builds a fresh worker when it has none");
}

// R10. A second run while one is active is refused, and does not disturb the first.
{
  const fleet = makeFleet(hangAfterStart);
  const runner = createPyodideRunner({ createWorker: fleet.createWorker, timeoutMs: 5000 });
  const p1 = runner.run("a");
  let err = null;
  try { await runner.run("b"); } catch (e) { err = e; }
  assert(err && /already in progress/i.test(err.message), "a concurrent run() is rejected with a clear message");
  assert(!fleet.instances[0].terminated && fleet.instances[0].posted.length === 1, "the active run is untouched");
  runner.stop();
  assert((await settle(p1)).status === "stopped", "the first run can still be stopped");
}

// R11. dispose(): settles the active run, terminates, and the runner stays reusable
//      (React StrictMode mounts, unmounts and re-mounts effects in development).
{
  const fleet = makeFleet(hangAfterStart);
  const runner = createPyodideRunner({ createWorker: fleet.createWorker, timeoutMs: 5000 });
  const p = runner.run("while True: pass");
  runner.dispose();
  const r = await settle(p);
  assert(r !== HUNG && r.status === "stopped", "dispose() settles an active run instead of leaving it hanging");
  assert(fleet.instances[0].terminated && fleet.instances.length === 1, "dispose() terminates the worker and does not build a new one");
  const fleet2 = makeFleet(echoOk);
  const runner2 = createPyodideRunner({ createWorker: fleet2.createWorker });
  runner2.dispose(); // StrictMode's simulated unmount, before any run
  assert((await settle(runner2.run("1"))).status === "ok", "a runner that was disposed can run again");
}

// R12. onStart fires once, when execution begins (lets the UI switch from 'Starting Python' to 'Running').
{
  const calls = [];
  const fleet = makeFleet((w, m) => { started(w, m); started(w, m); finished(w, m, "x"); });
  const runner = createPyodideRunner({ createWorker: fleet.createWorker });
  await settle(runner.run("1", { onStart: () => calls.push("start") }));
  assert(calls.length === 1, "onStart is called exactly once per run");
  const fleet2 = makeFleet(echoOk);
  const runner2 = createPyodideRunner({ createWorker: fleet2.createWorker });
  const r = await settle(runner2.run("1", { onStart: () => { throw new Error("UI bug"); } }));
  assert(r.status === "ok", "an exception inside onStart cannot break the run");
}

// R13. Injected timers: no timer is left outstanding after a normal run, Stop or a timeout,
//      and the timeout can be fired deterministically (no real waiting).
{
  const timers = new Map(); let n = 0;
  const setTimer = (fn) => { const h = ++n; timers.set(h, fn); return h; };
  const clearTimer = (h) => { timers.delete(h); };
  const fire = () => { const [h, fn] = [...timers.entries()][0]; timers.delete(h); fn(); };

  const ok = makeFleet(echoOk);
  const r1 = createPyodideRunner({ createWorker: ok.createWorker, setTimer, clearTimer });
  await settle(r1.run("1"));
  assert(timers.size === 0, "no timer is left outstanding after a normal run");

  const hang = makeFleet(firstHangsThenOk);
  const r2 = createPyodideRunner({ createWorker: hang.createWorker, setTimer, clearTimer });
  const p = r2.run("while True: pass");
  assert(timers.size === 1, "exactly one timer (the run timeout) is armed once execution has started");
  fire();
  const t = await settle(p);
  assert(t !== HUNG && t.status === "timeout" && timers.size === 0, "firing it resolves 'timeout' and leaves no timer behind");
  const p2 = r2.run("print(2)");
  assert((await settle(p2)).status === "ok" && timers.size === 0, "the next run works after a fired timeout");

  const hang2 = makeFleet(hangAfterStart);
  const r3 = createPyodideRunner({ createWorker: hang2.createWorker, setTimer, clearTimer });
  const p3 = r3.run("while True: pass");
  r3.stop();
  await settle(p3);
  assert(timers.size === 0, "Stop leaves no timer behind");
}

// ===========================================================================
// Patch B08 -- source-level wiring checks (the React pieces cannot run under node)
// ===========================================================================
function walk(node, visit, parents = []) {
  if (!node || typeof node.type !== "string") return;
  visit(node, parents);
  for (const key of Object.keys(node)) {
    if (key === "loc" || key === "extra") continue;
    const v = node[key];
    if (Array.isArray(v)) v.forEach((c) => walk(c, visit, [...parents, node]));
    else if (v && typeof v.type === "string") walk(v, visit, [...parents, node]);
  }
}
const appDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const readApp = (rel) => fs.readFileSync(path.join(appDir, rel), "utf8");
const parseJsx = (src) => parse(src, { sourceType: "module", plugins: ["jsx"] });

// the hook: built on the runner, returns { run, stop }
{
  const ast = parseJsx(readApp("hooks/usePyodideWorker.js"));
  let importsRunner = false, returnsStop = false;
  walk(ast, (n) => {
    if (n.type === "ImportDeclaration" && /pyodideRunner$/.test(n.source.value) && n.specifiers.some((s) => s.imported && s.imported.name === "createPyodideRunner")) importsRunner = true;
    if (n.type === "ReturnStatement" && n.argument && n.argument.type === "ObjectExpression") {
      const keys = n.argument.properties.map((p) => p.key && p.key.name);
      if (keys.includes("run") && keys.includes("stop")) returnsStop = true;
    }
  });
  assert(importsRunner, "usePyodideWorker imports createPyodideRunner from lib/pyodideRunner");
  assert(returnsStop, "usePyodideWorker returns { run, stop }");
}

// the card: Stop button, only while busy, wired to stop()
{
  const ast = parseJsx(readApp("components/ArtifactRenderer.jsx"));
  let destructuresStop = false, stopButtonUnderBusy = false, stopButtonHasLabel = false, handleStopCallsStop = false, shows = { stopped: false, timeout: false };
  walk(ast, (n, parents) => {
    if (n.type === "VariableDeclarator" && n.init && n.init.type === "CallExpression" && n.init.callee.name === "usePyodideWorker" && n.id.type === "ObjectPattern") {
      if (n.id.properties.some((p) => p.key && p.key.name === "stop")) destructuresStop = true;
    }
    if (n.type === "JSXOpeningElement" && n.name.name === "button") {
      const onClick = n.attributes.find((a) => a.name && a.name.name === "onClick");
      if (onClick && onClick.value.expression && onClick.value.expression.name === "handleStop") {
        if (parents.some((p) => p.type === "LogicalExpression" && p.operator === "&&" && p.left.type === "Identifier" && p.left.name === "busy")) stopButtonUnderBusy = true;
      }
    }
    if (n.type === "JSXElement" && n.openingElement.name.name === "button") {
      const onClick = n.openingElement.attributes.find((a) => a.name && a.name.name === "onClick");
      if (onClick && onClick.value.expression && onClick.value.expression.name === "handleStop") {
        if (n.children.some((c) => c.type === "JSXText" && c.value.trim() === "Stop")) stopButtonHasLabel = true;
      }
    }
    if (n.type === "FunctionDeclaration" && n.id && n.id.name === "handleStop") {
      walk(n.body, (m) => { if (m.type === "CallExpression" && m.callee.name === "stop") handleStopCallsStop = true; });
    }
  });
  assert(destructuresStop, "PythonArtifact takes `stop` from usePyodideWorker()");
  assert(stopButtonUnderBusy, "a <button onClick={handleStop}> is rendered only while `busy`");
  assert(handleStopCallsStop, "handleStop() calls stop()");
  const src = readApp("components/ArtifactRenderer.jsx");
  assert(stopButtonHasLabel, "the Stop control has a visible text label");
  assert(/"stopped"/.test(src) && /"timeout"/.test(src), "the card handles the 'stopped' and 'timeout' results");
}

if (failures > 0) { console.error(`\n${failures} assertion(s) failed.`); process.exit(1); }
console.log("\nAll assertions passed.");
