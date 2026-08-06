"use client";
import { useRef, useCallback, useEffect } from "react";

// Phase CO, CO2 (Master Guide v2, §5) — thin wrapper around the Pyodide
// Web Worker (public/workers/pyodideWorker.js). Lazily creates the worker
// on the first run() call, not on mount -- so simply having a python
// artifact present in a chat message doesn't force every visitor to
// download Pyodide's WASM runtime unprompted; it only loads once someone
// actually clicks Run (see ArtifactRenderer.jsx).
//
// Each hook instance owns its own worker and terminates it on unmount.
// If a page ever renders several python artifacts at once and warming up
// one shared Pyodide instance for all of them becomes worth the added
// complexity, that's a reasonable follow-up (a page-level context) --
// not needed for this patch's scope.
export function usePyodideWorker() {
  const workerRef = useRef(null);
  const pendingRef = useRef(new Map()); // id -> { resolve, reject }
  const idRef = useRef(0);

  useEffect(() => {
    return () => {
      workerRef.current?.terminate();
      workerRef.current = null;
      pendingRef.current.clear();
    };
  }, []);

  const run = useCallback((code) => {
    if (!workerRef.current) {
      workerRef.current = new Worker("/workers/pyodideWorker.js");
      workerRef.current.onmessage = (event) => {
        const { id, ...payload } = event.data || {};
        const pending = pendingRef.current.get(id);
        if (!pending) return;
        pendingRef.current.delete(id);
        if (payload.status === "error") pending.reject(new Error(payload.error));
        else pending.resolve(payload);
      };
      workerRef.current.onerror = (event) => {
        // A worker-level error (e.g. the CDN script itself failed to
        // load -- offline, ad-blocker, CDN outage) fires here instead of
        // onmessage, and would otherwise leave every pending run() call
        // hanging forever with no response.
        for (const pending of pendingRef.current.values()) {
          pending.reject(new Error(event.message || "Pyodide worker failed to load"));
        }
        pendingRef.current.clear();
      };
    }
    const id = ++idRef.current;
    return new Promise((resolve, reject) => {
      pendingRef.current.set(id, { resolve, reject });
      workerRef.current.postMessage({ id, code });
    });
  }, []);

  return { run };
}
