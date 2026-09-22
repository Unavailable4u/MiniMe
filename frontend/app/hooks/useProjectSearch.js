"use client";
// frontend/app/hooks/useProjectSearch.js — W2.6 (Build Workbench plan).
// The async half of Project Search (Cmd/Ctrl-Shift-F): gathers the text
// lib/workbench/projectSearch.js's pure searchProject() needs — an open
// tab's live buffer, or a provider.read() for anything closed — and
// hands the actual scanning to that module untouched. See
// projectSearch.js's own header for why there is no backend search
// route to call instead: this is a deliberate CLIENT-SIDE content
// search, the same posture fileProviders.js's `capabilities.search:
// false` already commits to.
//
// Debounced (~300ms after the last keystroke) so scanning a whole
// project doesn't re-read every closed file on every character typed.
// Closed-file reads are cached by the file's own `version`
// (filesMeta[path].version) — a save or an external update only
// invalidates THAT file's cache entry, not the whole thing, so typing
// "abc" one letter at a time re-fetches nothing after the first
// keystroke. A stale response can never land after a newer one: every
// run gets a sequence number, matching EditorWorkbench's own
// loadFileList()/refreshFromServer() convention, and only the latest
// one is allowed to touch state.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { searchProject, searchableFilePaths } from "../lib/workbench/projectSearch";

const DEBOUNCE_MS = 300;
const READ_CONCURRENCY = 6; // provider.read() calls in flight at once, across the whole project

/**
 * Runs `fn` over `items` with at most `limit` calls in flight at once.
 * No external dependency for something this small — same "obviously
 * correct beats a library" call the pure lib files already make.
 */
async function mapWithConcurrency(items, limit, fn) {
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      await fn(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
}

/**
 * @param {object} deps
 * @param {Record<string, object>|null} deps.filesMeta - provider.list()'s latest result
 * @param {object} deps.provider - the active FileProvider (needs .read)
 * @param {(path: string) => string|undefined} deps.getOpenText - the
 *   live buffer text for an open path (edited, unsaved edits included),
 *   or undefined if that path isn't open right now
 */
export function useProjectSearch({ filesMeta, provider, getOpenText }) {
  const [query, setQuery] = useState("");
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [results, setResults] = useState([]);
  const [matchCount, setMatchCount] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [filesScanned, setFilesScanned] = useState(0);

  const cacheRef = useRef(new Map()); // path -> {version, content}
  const seqRef = useRef(0);
  const timerRef = useRef(null);

  const searchablePaths = useMemo(() => searchableFilePaths(filesMeta), [filesMeta]);

  const resetToIdle = useCallback(() => {
    setResults([]);
    setMatchCount(0);
    setTruncated(false);
    setLoading(false);
    setError(null);
    setFilesScanned(0);
  }, []);

  const runSearch = useCallback(
    async (q, opts) => {
      const seq = ++seqRef.current;
      setLoading(true);
      setError(null);
      try {
        const fileTexts = {};
        let scanned = 0;
        await mapWithConcurrency(searchablePaths, READ_CONCURRENCY, async (path) => {
          const open = getOpenText(path);
          if (open !== undefined) {
            fileTexts[path] = open;
            scanned += 1;
            return;
          }
          const meta = filesMeta?.[path];
          const cached = cacheRef.current.get(path);
          if (cached && meta && cached.version === meta.version) {
            fileTexts[path] = cached.content;
            scanned += 1;
            return;
          }
          try {
            const file = await provider.read(path);
            if (seq !== seqRef.current) return; // superseded — don't bother caching a stale run's reads
            cacheRef.current.set(path, { version: file.version ?? meta?.version ?? 0, content: file.content || "" });
            fileTexts[path] = file.content || "";
            scanned += 1;
          } catch {
            // One unreadable file (deleted mid-scan, a transient network
            // blip) doesn't fail the whole search — it's just missing
            // from this run's results.
          }
        });
        if (seq !== seqRef.current) return;
        const outcome = searchProject(fileTexts, q, { caseSensitive: !!opts.caseSensitive });
        setResults(outcome.results);
        setMatchCount(outcome.matchCount);
        setTruncated(outcome.truncated);
        setFilesScanned(scanned);
      } catch (err) {
        if (seq === seqRef.current) setError(err.message || "Search failed.");
      } finally {
        if (seq === seqRef.current) setLoading(false);
      }
    },
    [searchablePaths, filesMeta, provider, getOpenText]
  );

  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (!query.trim()) {
      seqRef.current += 1; // invalidate any run still in flight
      resetToIdle();
      return undefined;
    }
    const q = query.trim();
    timerRef.current = setTimeout(() => runSearch(q, { caseSensitive }), DEBOUNCE_MS);
    return () => clearTimeout(timerRef.current);
  }, [query, caseSensitive, runSearch, resetToIdle]);

  useEffect(() => () => clearTimeout(timerRef.current), []);

  return {
    query,
    setQuery,
    caseSensitive,
    setCaseSensitive,
    results,
    matchCount,
    truncated,
    loading,
    error,
    filesScanned,
    totalFiles: searchablePaths.length,
  };
}
