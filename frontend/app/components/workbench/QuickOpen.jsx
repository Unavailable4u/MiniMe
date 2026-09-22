"use client";
// frontend/app/components/workbench/QuickOpen.jsx — W2.6 (Build
// Workbench plan). Cmd/Ctrl-P's file palette: a text box over
// lib/workbench/quickOpen.js's subsequence ranking. Opened, closed and
// key-bound by EditorWorkbench.jsx (the workbench owns the keydown
// handler and the open/closed state, same "pane reports intent, the
// workbench decides what happens" split as Explorer's own file
// operations) — this component only renders the list and reports which
// path was chosen, through `onOpenFile`.
//
// ResponsiveSheet, not a hand-rolled overlay: same modal primitive
// every other dialog in this app uses since the Phase-4 mobile pass
// (ConfirmDialog, ConflictCompareView), which is what gives this
// Escape-to-close, backdrop-click-to-close and a scroll lock for free,
// and a full-screen sheet on a touch device with no physical keyboard
// to press Cmd/Ctrl-P on in the first place (the trigger is still
// keyboard-only for now — see EditorWorkbench.jsx's own keydown
// handler — this is just "don't render badly if it somehow opens").
import { useEffect, useMemo, useRef, useState } from "react";
import { basenameMatchIndices, defaultQuickOpenList, rankQuickOpen } from "../../lib/workbench/quickOpen";
import { basename, dirname } from "../../lib/workbench/fileTree";
import { fileIconKey } from "../../lib/workbench/fileIcons";
import { FILE_ICONS } from "./ExplorerRow";
import ResponsiveSheet from "../mobile/ResponsiveSheet";

function EntryIcon({ path }) {
  const [Icon, tone] = FILE_ICONS[fileIconKey(basename(path))] || FILE_ICONS.file;
  return <Icon size={12} className={`shrink-0 ${tone}`} />;
}

// `indices` are positions within `text` (already basename-relative, via
// basenameMatchIndices) to render in the accent color — the same
// "which characters actually matched" highlight a command palette is
// expected to show.
function Highlighted({ text, indices }) {
  if (!indices || indices.length === 0) return <>{text}</>;
  const marked = new Set(indices);
  return (
    <>
      {[...text].map((ch, i) =>
        marked.has(i) ? (
          <span key={i} className="text-[var(--accent)]">
            {ch}
          </span>
        ) : (
          <span key={i}>{ch}</span>
        )
      )}
    </>
  );
}

const RESULT_LIMIT = 50;

/**
 * @param {object} props
 * @param {boolean} props.open
 * @param {() => void} props.onClose
 * @param {string[]} props.paths - every real file path in the project
 * @param {string[]} props.recentPaths - most-recently-opened first
 * @param {(path: string) => void} props.onOpenFile
 */
export default function QuickOpen({ open, onClose, paths, recentPaths, onOpenFile }) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  // Fresh every time it opens — a palette that remembered last time's
  // typed text would show stale results for a beat before the person
  // even starts typing.
  useEffect(() => {
    if (!open) return undefined;
    setQuery("");
    setSelected(0);
    const id = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, [open]);

  const results = useMemo(() => {
    const q = query.trim();
    return q
      ? rankQuickOpen(paths, q, { limit: RESULT_LIMIT })
      : defaultQuickOpenList(paths, recentPaths, { limit: RESULT_LIMIT });
  }, [paths, recentPaths, query]);

  // Typing can shrink the list out from under whichever row was
  // highlighted; clamp rather than let `selected` point past the end.
  useEffect(() => {
    setSelected((s) => Math.min(s, Math.max(0, results.length - 1)));
  }, [results.length]);

  useEffect(() => {
    listRef.current?.children[selected]?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  function choose(path) {
    if (!path) return;
    onOpenFile(path);
    onClose();
  }

  function onKeyDown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((s) => Math.min(results.length - 1, s + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => Math.max(0, s - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(results[selected]?.path);
    }
    // Escape isn't handled here — ResponsiveSheet already closes on it.
  }

  return (
    <ResponsiveSheet
      open={open}
      onClose={onClose}
      maxWidth="max-w-lg"
      className="bg-[var(--neutral-950)] border border-[var(--neutral-700)] shadow-2xl"
    >
      <div role="dialog" aria-modal="true" aria-label="Quick open" onKeyDown={onKeyDown}>
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Go to file…"
          aria-label="Go to file"
          spellCheck={false}
          autoComplete="off"
          className="w-full bg-transparent px-3 py-2.5 text-sm text-[var(--neutral-100)] placeholder:text-[var(--neutral-600)] outline-none border-b border-[var(--neutral-800)]"
        />
        <div ref={listRef} role="listbox" aria-label="Files" className="max-h-[50vh] overflow-auto py-1">
          {results.length === 0 && (
            <p className="px-3 py-4 text-center text-xs text-[var(--neutral-600)]">
              {paths.length === 0 ? "No files in this project." : "No files match."}
            </p>
          )}
          {results.map((r, i) => {
            const name = basename(r.path);
            const dir = dirname(r.path);
            const nameIndices = basenameMatchIndices(r.path, r.indices);
            return (
              <button
                key={r.path}
                type="button"
                role="option"
                aria-selected={i === selected}
                onMouseEnter={() => setSelected(i)}
                onClick={() => choose(r.path)}
                className={`w-full flex items-center gap-2 px-3 py-1.5 text-left text-xs ${
                  i === selected ? "bg-[var(--neutral-800)]" : "hover:bg-[var(--neutral-900)]"
                }`}
              >
                <EntryIcon path={r.path} />
                <span className="min-w-0 flex-1 truncate text-[var(--neutral-100)]">
                  <Highlighted text={name} indices={nameIndices} />
                </span>
                {dir && <span className="shrink-0 max-w-[45%] truncate text-[var(--neutral-600)]">{dir}</span>}
              </button>
            );
          })}
        </div>
      </div>
    </ResponsiveSheet>
  );
}
