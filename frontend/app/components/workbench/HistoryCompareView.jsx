"use client";
// frontend/app/components/workbench/HistoryCompareView.jsx — W2.6
// (Build Workbench plan). HistoryPanel's "click a version -> read-only
// diff vs current" (the plan's own wording for this step). Same
// @codemirror/merge MergeView-of-two-finished-documents shape as
// ConflictCompareView.jsx (see that file's own header for why a FRESH
// instance per open, rather than one kept alive and updated between
// opens) — split into its own component rather than reused directly
// because the two are about different DECISIONS at the bottom (Reload
// theirs / Keep mine vs a single Restore) even though the diff
// plumbing underneath is identical.
import { useEffect, useRef } from "react";
import { EditorState } from "@codemirror/state";
import { EditorView, lineNumbers } from "@codemirror/view";
import { MergeView } from "@codemirror/merge";
import { buildEditorTheme } from "../../lib/workbench/cmTheme";
import { basename } from "../../lib/workbench/fileTree";
import { loadLanguageExtension } from "./CodeEditor";
import ResponsiveSheet from "../mobile/ResponsiveSheet";

function readOnlySide(doc, languageExtension) {
  return {
    doc,
    extensions: [
      lineNumbers(),
      buildEditorTheme(),
      languageExtension,
      EditorState.readOnly.of(true),
      EditorView.editable.of(false),
    ],
  };
}

/**
 * @param {object} props
 * @param {boolean} props.open
 * @param {string} [props.path] - picks the syntax mode and the header/filename only
 * @param {string} props.older - the historical version's content (MergeView side "a")
 * @param {number} [props.olderVersion]
 * @param {string} [props.olderUpdatedBy]
 * @param {string} props.current - the file's current SAVED content (side "b")
 * @param {number} [props.currentVersion]
 * @param {() => void} props.onClose
 * @param {() => void} props.onRestore - opens the Restore confirmation for `older`
 */
export default function HistoryCompareView({
  open,
  path,
  older,
  olderVersion,
  olderUpdatedBy,
  current,
  currentVersion,
  onClose,
  onRestore,
}) {
  const containerRef = useRef(null);

  useEffect(() => {
    if (!open || !containerRef.current) return;
    let disposed = false;
    let view = null;

    (async () => {
      // Both sides are the same file, so one lazy-loaded language
      // extension covers both — same per-file, not per-editor, cost
      // CodeEditor.jsx's own loadLanguageExtension() already has.
      const languageExtension = await loadLanguageExtension(path);
      if (disposed || !containerRef.current) return;
      view = new MergeView({
        parent: containerRef.current,
        a: readOnlySide(older ?? "", languageExtension),
        b: readOnlySide(current ?? "", languageExtension),
        highlightChanges: true,
        gutter: true,
        collapseUnchanged: { margin: 3 },
      });
      view.dom.style.height = "100%";
      view.dom.style.overflow = "auto";
    })();

    return () => {
      disposed = true;
      view?.destroy();
    };
    // `older`/`current` intentionally excluded — a fresh compare is a
    // fresh open (see ConflictCompareView.jsx's own header for why).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, path]);

  return (
    <ResponsiveSheet
      open={open}
      onClose={onClose}
      maxWidth="max-w-5xl"
      className="bg-[var(--neutral-950)] border border-[var(--neutral-700)] text-[var(--neutral-200)] flex flex-col"
    >
      <div className="shrink-0 flex items-center justify-between gap-3 px-4 py-3 border-b border-[var(--neutral-800)]">
        <h3 className="text-sm font-medium truncate">History — {path ? basename(path) : ""}</h3>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
        >
          Close
        </button>
      </div>

      <div className="shrink-0 grid grid-cols-2 text-[11px] text-[var(--neutral-500)] border-b border-[var(--neutral-800)]">
        <div className="px-3 py-1.5 border-r border-[var(--neutral-800)] truncate">
          {olderVersion != null ? `v${olderVersion}` : "Older version"}
          {olderUpdatedBy ? ` · ${olderUpdatedBy}` : ""}
        </div>
        <div className="px-3 py-1.5 truncate">Current{currentVersion != null ? ` · v${currentVersion}` : ""}</div>
      </div>

      <div ref={containerRef} className="h-[55vh]" />

      <div className="shrink-0 flex items-center justify-end gap-3 px-4 py-3 border-t border-[var(--neutral-800)] text-xs">
        <button type="button" onClick={onClose} className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
          Close
        </button>
        <button
          type="button"
          onClick={onRestore}
          className="rounded-lg bg-[var(--accent)] text-[var(--accent-text)] px-3 py-1.5 font-medium"
        >
          Restore this version
        </button>
      </div>
    </ResponsiveSheet>
  );
}
