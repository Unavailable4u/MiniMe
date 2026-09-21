"use client";
// frontend/app/components/workbench/ConflictCompareView.jsx — W2.5
// (Build Workbench plan). The "Compare" choice on a save conflict
// bar (EditorWorkbench.jsx): a read-only, side-by-side diff of the
// server's current content against the edits that just failed to
// save. eo/workspace_code_files.py's VersionConflictError docstring
// names this exact "Reload / Keep mine / Compare" split as the reason
// a 409's body carries the whole current file instead of just a
// version number — the content shown on the left here is that same
// body, with no extra round trip.
//
// @codemirror/merge's MergeView, not the unifiedMergeView W5.3 will use
// for AI-edit review: that one edits ONE document in place with
// per-hunk accept/reject controls threaded through it; this one only
// ever shows two FINISHED documents next to each other so a person can
// decide which of Reload-theirs/Keep-mine they want (both real
// decisions live on the conflict bar itself, and are just echoed here
// as a convenience) — so neither side is editable and neither needs
// revertControls.
//
// A fresh MergeView per time this opens (mount = build it, close/
// unmount = destroy it) rather than a controlled instance kept in sync
// across re-opens: this shows two static snapshots, not something the
// person types into, so there's no undo-history/cursor state worth
// preserving between one conflict and the next the way CodeEditor.jsx
// goes to real lengths to preserve for an actual editing surface.
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
 * @param {string} [props.path] - only used to pick the syntax mode and
 *   for the header/filename; the file itself never changes while this
 *   is open (see the header comment on why this isn't a controlled,
 *   kept-alive-between-opens instance)
 * @param {string} props.theirs - VersionConflictError.current.content — the server's current content
 * @param {number} [props.theirsVersion]
 * @param {string} [props.theirsUpdatedBy]
 * @param {string} props.mine - the content this save attempt actually sent
 * @param {() => void} props.onClose
 * @param {() => void} props.onReloadTheirs - "Reload theirs": discard `mine`, load `theirs`
 * @param {() => void} props.onKeepMine - "Keep mine": the next Save will overwrite `theirs`
 */
export default function ConflictCompareView({
  open,
  path,
  theirs,
  theirsVersion,
  theirsUpdatedBy,
  mine,
  onClose,
  onReloadTheirs,
  onKeepMine,
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
        a: readOnlySide(theirs ?? "", languageExtension),
        b: readOnlySide(mine ?? "", languageExtension),
        highlightChanges: true,
        gutter: true,
        collapseUnchanged: { margin: 3 },
      });
      // @codemirror/merge's own docs: "views are not scrollable [by
      // default]. Style them (.cm-mergeView) with a height and
      // overflow: auto to make them scrollable" — set directly on the
      // instance's own dom rather than a stylesheet rule, so this
      // component doesn't need a CSS file of its own for one rule.
      view.dom.style.height = "100%";
      view.dom.style.overflow = "auto";
    })();

    return () => {
      disposed = true;
      view?.destroy();
    };
    // `mine`/`theirs` intentionally excluded: this effect keys off
    // `open`/`path` (a fresh compare is a fresh open, see the header
    // comment) so a parent re-render that only changes an unrelated
    // sibling prop doesn't tear down and rebuild the diff mid-view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, path]);

  return (
    <ResponsiveSheet open={open} onClose={onClose} maxWidth="max-w-5xl" className="bg-[var(--neutral-950)] border border-[var(--neutral-700)] text-[var(--neutral-200)] flex flex-col">
      <div className="shrink-0 flex items-center justify-between gap-3 px-4 py-3 border-b border-[var(--neutral-800)]">
        <h3 className="text-sm font-medium truncate">
          Comparing {path ? basename(path) : ""}
        </h3>
        <button type="button" onClick={onClose} className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-200)]">
          Close
        </button>
      </div>

      {/* Labels line up with MergeView's own default 50/50 split
          (orientation defaults to "a-b" = left/right, matching a/b
          below). */}
      <div className="shrink-0 grid grid-cols-2 text-[11px] text-[var(--neutral-500)] border-b border-[var(--neutral-800)]">
        <div className="px-3 py-1.5 border-r border-[var(--neutral-800)] truncate">
          Server&apos;s current version{theirsVersion != null ? ` · v${theirsVersion}` : ""}
          {theirsUpdatedBy ? ` · ${theirsUpdatedBy}` : ""}
        </div>
        <div className="px-3 py-1.5 truncate">Your unsaved edits</div>
      </div>

      <div ref={containerRef} className="h-[55vh]" />

      <div className="shrink-0 flex items-center justify-end gap-3 px-4 py-3 border-t border-[var(--neutral-800)] text-xs">
        <button type="button" onClick={onClose} className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
          Cancel
        </button>
        <button
          type="button"
          onClick={onReloadTheirs}
          className="rounded-lg border border-[var(--neutral-700)] px-3 py-1.5 font-medium hover:bg-[var(--neutral-800)]"
        >
          Reload theirs
        </button>
        <button
          type="button"
          onClick={onKeepMine}
          className="rounded-lg bg-[var(--accent)] text-[var(--accent-text)] px-3 py-1.5 font-medium"
        >
          Keep mine
        </button>
      </div>
    </ResponsiveSheet>
  );
}
