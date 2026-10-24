"use client";
import { useRef, useCallback, useEffect } from "react";
import { createPyodideRunner, DEFAULT_RUN_TIMEOUT_MS } from "../lib/pyodideRunner";

// Phase CO, CO2 (Master Guide v2, §5) — thin wrapper around the Pyodide
// Web Worker (public/workers/pyodideWorker.js). Lazily creates the worker
// on the first run() call, not on mount -- so simply having a python
// artifact present in a chat message doesn't force every visitor to
// download Pyodide's WASM runtime unprompted; it only loads once someone
// actually clicks Run (see ArtifactRenderer.jsx).
//
// Patch B08 (M2C build plan) -- runner hardening. The worker management that
// used to live here (create on first run, terminate on unmount) moved into
// lib/pyodideRunner.js, a dependency-free module that is tested under plain
// node with a fake Worker; this hook only supplies `new Worker(...)` and ties
// the runner's lifetime to the component. What the runner adds:
//   - stop(): ends the active run. Python in a worker cannot be interrupted,
//     so this terminates the worker and builds a fresh one; the next run
//     reloads the runtime.
//   - a run timeout (default 30 s, `usePyodideWorker({ timeoutMs })` or
//     run(code, { timeoutMs })) that does the same thing on its own. The clock
//     starts when the worker reports that execution began, so downloading the
//     runtime is not counted.
//   - run() resolves -- never hangs -- with { status: "stopped" | "timeout",
//     message } for those two cases; real failures still reject with an Error.
//   - run(code, { onStart }) lets the card switch from "Starting Python" to
//     "Running" when execution begins.
//
// Each hook instance owns its own runner (and so its own worker) and releases
// it on unmount. If a page ever renders several python artifacts at once and
// warming up one shared Pyodide instance for all of them becomes worth the
// added complexity, that's a reasonable follow-up (a page-level context) --
// not needed for this patch's scope.
const WORKER_URL = "/workers/pyodideWorker.js";

export function usePyodideWorker({ timeoutMs = DEFAULT_RUN_TIMEOUT_MS } = {}) {
  const runnerRef = useRef(null);
  if (runnerRef.current === null) {
    // No Worker is created here -- createWorker() is only called by run().
    runnerRef.current = createPyodideRunner({
      createWorker: () => new Worker(WORKER_URL),
      timeoutMs,
    });
  }
  const timeoutRef = useRef(timeoutMs);
  useEffect(() => {
    timeoutRef.current = timeoutMs;
  }, [timeoutMs]);

  useEffect(() => {
    const runner = runnerRef.current;
    return () => runner.dispose();
  }, []);

  const run = useCallback(
    (code, options) => runnerRef.current.run(code, { timeoutMs: timeoutRef.current, ...options }),
    []
  );
  const stop = useCallback(() => runnerRef.current.stop(), []);

  return { run, stop };
}
