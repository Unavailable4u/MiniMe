// frontend/app/lib/workbench/gotoPosition.js — W2.6 (Build Workbench
// plan). Turns a 1-based (line, column) — what
// projectSearch.js's searchFileContent() reports and what a person
// reads off the status bar's own Ln/Col — into a CM6 doc position and
// puts the caret (or a selection) there, scrolled into view.
//
// Not part of the "no imports" pure-function family (fileTree.js,
// tabUtils.js, quickOpen.js, projectSearch.js): this one's whole job is
// dispatching a transaction against a LIVE EditorView, so unlike those
// it needs @codemirror/state and can't be exercised by a plain-`node`
// test. Kept out of CodeEditor.jsx itself because nothing about it is
// CodeEditor's own concern — it's a caller-side helper ("the workbench
// asked this view to jump somewhere"), same relationship
// fileProviders.js has to the routes it wraps.
import { EditorSelection } from "@codemirror/state";

/**
 * @param {import("@codemirror/view").EditorView|null|undefined} view -
 *   a no-op if the view isn't mounted yet (or has already been torn
 *   down) — callers that might race a pane's mount (Project Search's
 *   jump, which can fire before a just-opened file's editor exists)
 *   are expected to retry rather than this function queuing anything.
 * @param {{line: number, column?: number, endColumn?: number}} pos -
 *   `line` and `column` are 1-based, matching both
 *   searchFileContent()'s own numbering and CodeMirror's line/column
 *   gutter; `column` defaults to the line's start. `endColumn`
 *   (1-based, exclusive) selects through there instead of only placing
 *   the caret — a caller with a match's [start,end) range (0-based)
 *   passes `column: start + 1, endColumn: end + 1`. Both are clamped
 *   to the line's own length rather than throwing on a stale position
 *   (the line the search matched may have been edited since).
 */
export function jumpToPosition(view, { line, column = 1, endColumn } = {}) {
  if (!view) return;
  const doc = view.state.doc;
  const clampedLine = Math.min(Math.max(1, Math.floor(line) || 1), doc.lines);
  const lineInfo = doc.line(clampedLine);
  const from = Math.min(lineInfo.to, lineInfo.from + Math.max(0, (column || 1) - 1));
  const to =
    endColumn != null ? Math.min(lineInfo.to, Math.max(from, lineInfo.from + endColumn - 1)) : from;

  view.dispatch({
    selection: EditorSelection.single(from, to),
    scrollIntoView: true,
  });
  view.focus();
}
