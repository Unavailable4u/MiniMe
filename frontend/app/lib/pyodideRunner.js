// Patch B08 (M2C build plan) -- chat-artifact Python runner: worker
// lifecycle, run timeout and Stop.
//
// WHY THIS IS A SEPARATE FILE. hooks/usePyodideWorker.js is a React hook and
// cannot be loaded under plain `node`, but the interesting logic here --
// "terminate a stuck worker, build a fresh one, never leave a promise hanging,
// ignore anything the dead worker says afterwards" -- is exactly what must be
// tested without a browser. So the logic lives here as a dependency-free
// module (no imports, named exports only, so lib/workbench/__tests__/
// loadSource.mjs can evaluate the REAL file), and the hook is a thin wrapper
// that supplies `new Worker(...)` and ties dispose() to unmount.
//
// WHY TERMINATE. Python running inside a Web Worker cannot be interrupted
// from outside: the worker is busy in a synchronous WebAssembly call, so it
// never reads another message. (Pyodide's cooperative interrupt needs
// SharedArrayBuffer, which needs cross-origin-isolation headers this app does
// not send.) The only way to stop `while True: pass` is worker.terminate().
// The price is that the Pyodide runtime in that worker is gone, so the NEXT
// run starts a fresh worker and reloads the runtime (self-hosted, so it is
// quick after the first time; see public/workers/pyodideWorker.js).
//
// WHAT run() RESOLVES WITH
//   ok       -> the worker's payload { status:"ok", stdout, stderr, images }
//   stopped  -> { status:"stopped", message, stdout:"", stderr:"", images:[] }
//               (Stop pressed, or the component went away)
//   timeout  -> { status:"timeout", message, reason:"run"|"load", ... }
// and it REJECTS only for real failures: the worker reported an error (e.g.
// the runtime could not be loaded), the worker script itself failed, or a run
// was started while another one is active. A stopped or timed-out run is not
// an error, which is why it resolves: the UI shows a plain notice for it.
// Output the stopped run had produced is lost with the worker (it is buffered
// inside it); the message says so.
//
// TWO TIMERS, ONE ACTIVE RUN. A run is first in a "loading" phase (the worker
// is downloading the runtime and any packages the code imports) and then in a
// "running" phase (the worker posted {type:"started", runId}, see the worker).
// Only the running phase is held to `timeoutMs` (default 30 s): a slow first
// download on a bad network must not look like a runaway script and be killed
// and restarted forever. The loading phase has its own, much more generous
// `loadTimeoutMs`, so a load that never finishes still ends with a message.
// `timeoutMs` of 0, a negative number or Infinity means "no run timeout"
// (Stop still works). One run at a time per runner.
//
// Metering/telemetry style fail-open does not apply here: this is not a
// security boundary (the code runs in the visitor's own browser, in a worker,
// never on MiniMe's servers); it only keeps the page usable.

export const DEFAULT_RUN_TIMEOUT_MS = 30000;
export const DEFAULT_LOAD_TIMEOUT_MS = 120000;

function describeDuration(ms) {
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  return `${Number.isInteger(s) ? s : s.toFixed(1)} s`;
}

function limitOrNull(ms) {
  return typeof ms === "number" && Number.isFinite(ms) && ms > 0 ? ms : null;
}

function emptyOutcome(status, message, extra) {
  return { status, message, stdout: "", stderr: "", images: [], ...(extra || {}) };
}

const STOPPED_MESSAGE =
  "Run stopped. The Python runtime was restarted, so output from the stopped run is discarded " +
  "and the next run starts fresh.";
const CLOSED_MESSAGE = "Run cancelled because this card was closed.";

/**
 * @param {object} options
 * @param {() => Worker} options.createWorker  builds a new worker (the hook passes `() => new Worker(url)`)
 * @param {number} [options.timeoutMs]         default run timeout, ms (default 30 000)
 * @param {number} [options.loadTimeoutMs]     runtime-loading limit, ms (default 120 000)
 * @param {(fn: Function, ms: number) => any} [options.setTimer]      injectable for tests
 * @param {(handle: any) => void} [options.clearTimer]                injectable for tests
 */
export function createPyodideRunner(options = {}) {
  const {
    createWorker,
    timeoutMs = DEFAULT_RUN_TIMEOUT_MS,
    loadTimeoutMs = DEFAULT_LOAD_TIMEOUT_MS,
    setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = (handle) => clearTimeout(handle),
  } = options;
  if (typeof createWorker !== "function") {
    throw new TypeError("createPyodideRunner needs a createWorker() function");
  }

  let worker = null;
  let active = null; // { id, resolve, reject, timer, runLimitMs, onStart }
  let lastId = 0;

  function discardWorker() {
    const old = worker;
    worker = null;
    if (old) {
      try { old.terminate(); } catch { /* already gone */ }
    }
  }

  function ensureWorker() {
    if (worker) return worker;
    const w = createWorker();
    // Handlers check `w === worker`: anything a discarded worker still says
    // (a late result, a late "started") must not touch a later run.
    w.onmessage = (event) => handleMessage(w, event);
    w.onerror = (event) => handleWorkerError(w, event);
    worker = w;
    return w;
  }

  // Terminate and immediately build the replacement. If building fails here
  // the next run() tries again and reports the problem to its caller.
  function restartWorker() {
    discardWorker();
    try { ensureWorker(); } catch { /* retried lazily by the next run() */ }
  }

  function finish(run, settle) {
    if (active !== run) return; // already settled by something else
    if (run.timer !== null) { clearTimer(run.timer); run.timer = null; }
    active = null;
    settle();
  }

  function arm(run, ms, onFire) {
    run.timer = setTimer(() => { run.timer = null; onFire(); }, ms);
  }

  function onRunTimeout(run, ms) {
    finish(run, () => {
      restartWorker();
      run.resolve(emptyOutcome(
        "timeout",
        `Run timed out after ${describeDuration(ms)} and was stopped. ` +
          "The Python runtime was restarted, so output from that run is discarded and the next run reloads it. " +
          "If the code is meant to run longer, it needs to finish sooner or do less work.",
        { reason: "run" }
      ));
    });
  }

  function onLoadTimeout(run, ms) {
    finish(run, () => {
      restartWorker();
      run.resolve(emptyOutcome(
        "timeout",
        `The Python runtime did not finish loading within ${describeDuration(ms)}, so the run was cancelled ` +
          "and the runtime restarted. Check your network connection and press Run again.",
        { reason: "load" }
      ));
    });
  }

  function markStarted(run) {
    if (run.timer !== null) { clearTimer(run.timer); run.timer = null; }
    if (run.runLimitMs !== null) arm(run, run.runLimitMs, () => onRunTimeout(run, run.runLimitMs));
    if (typeof run.onStart === "function") {
      try { run.onStart(); } catch { /* a UI callback must never break the run */ }
    }
  }

  function handleMessage(w, event) {
    if (w !== worker || !active) return;
    const data = (event && event.data) || {};
    const run = active;
    if (data.type === "started") {
      if (data.runId === run.id && !run.started) { run.started = true; markStarted(run); }
      return;
    }
    const { id, ...payload } = data;
    if (id !== run.id) return;
    finish(run, () => {
      if (payload.status === "error") run.reject(new Error(payload.error));
      else run.resolve(payload);
    });
  }

  // A worker-level error (the script failed to load -- offline, ad-blocker,
  // 404 -- or threw outside onmessage) fires onerror instead of onmessage and
  // would otherwise leave the run hanging. The worker is unusable afterwards,
  // so it is discarded; the next run() builds a fresh one.
  function handleWorkerError(w, event) {
    if (w !== worker) return;
    const message = (event && event.message) || "Pyodide worker failed to load";
    const run = active;
    discardWorker();
    if (run) finish(run, () => run.reject(new Error(message)));
  }

  function run(code, opts = {}) {
    if (active) {
      return Promise.reject(new Error("A Python run is already in progress. Stop it before starting another."));
    }
    let w;
    try { w = ensureWorker(); } catch (err) { return Promise.reject(err); }
    const id = ++lastId;
    return new Promise((resolve, reject) => {
      const entry = {
        id,
        resolve,
        reject,
        timer: null,
        started: false,
        runLimitMs: limitOrNull(opts.timeoutMs !== undefined ? opts.timeoutMs : timeoutMs),
        onStart: opts.onStart,
      };
      active = entry;
      const loadLimit = limitOrNull(loadTimeoutMs);
      if (loadLimit !== null) arm(entry, loadLimit, () => onLoadTimeout(entry, loadLimit));
      try {
        w.postMessage({ id, code });
      } catch (err) {
        finish(entry, () => { discardWorker(); reject(err); });
      }
    });
  }

  /** Stop the active run. Returns true if there was one. */
  function stop() {
    const entry = active;
    if (!entry) return false;
    finish(entry, () => {
      restartWorker();
      entry.resolve(emptyOutcome("stopped", STOPPED_MESSAGE));
    });
    return true;
  }

  /**
   * Release the worker (component unmount). Settles an active run so nothing
   * hangs, and does NOT build a replacement. The runner stays usable: React
   * StrictMode runs effect cleanups once during development and then mounts
   * again, and the next run() simply creates a new worker.
   */
  function dispose() {
    const entry = active;
    if (entry) {
      finish(entry, () => {
        discardWorker();
        entry.resolve(emptyOutcome("stopped", CLOSED_MESSAGE));
      });
    } else {
      discardWorker();
    }
  }

  return { run, stop, dispose, isRunning: () => active !== null };
}
