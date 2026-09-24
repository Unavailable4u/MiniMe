"use client";
// frontend/app/components/workbench/ReviewPanel.jsx — W5.3 (Build
// Workbench plan). The Copilot-style review of one AI-proposed edit:
// a toolbar (progress, previous/next change, Keep all, Undo all,
// Cancel, Done), a list of the files the edit touches with +/- badges,
// and one merge-view editor per file (CodeEditor in review mode — see
// that file's header for how a hunk's Keep / Undo works).
//
// It REPLACES the tabs + editor area while a review is open (EditorWorkbench
// keeps the normal editors mounted but hidden underneath, so closing the
// review puts every tab back exactly as it was — undo history, cursor,
// scroll). The Explorer, bottom panel and status bar stay put.
//
// Presentational: the proposal, the per-file state (`current`,
// `remaining`, `added`, `removed`) and the submit lifecycle all live in
// the editor store's `review` slice, and the resolve() round trip is
// EditorWorkbench's — this component only renders that state and turns
// clicks into the callbacks below. The one thing it owns is a map of its
// editors' imperative handles, which is how Keep all / Undo all and the
// previous/next buttons reach the merge views without the store ever
// holding a CodeMirror object.
//
// Every file's editor is mounted at once and the inactive ones hidden
// (same as EditorPane): a merge view's decisions live in its editor
// state, so unmounting one on a file switch would silently un-decide
// every hunk in it. The store's copy (`current`) is what Done sends;
// the mounted editors are what make switching files lossless.
import { memo, useCallback, useEffect, useMemo, useRef } from "react";
import { AlertTriangle, Check, ChevronDown, ChevronUp, Loader2, Sparkles } from "lucide-react";
import { reviewProgress } from "../../lib/workbench/reviewMode";
import { tabLabels } from "../../lib/workbench/tabUtils";
import CodeEditor from "./CodeEditor";

const OP_LABELS = { create: "new", delete: "deleted" };

/**
 * One reviewed file's merge editor. memo()'d on primitives only — `op`,
 * `original` and `proposed` never change for a review's lifetime, so an
 * update to a sibling file (or to this file's own `current`) doesn't
 * re-render the others.
 */
const ReviewEditor = memo(function ReviewEditor({ path, op, original, proposed, active, visible, register, onChange }) {
  const editorRef = useRef(null);
  const shown = active && visible;
  const review = useMemo(() => ({ original }), [original]);

  // A display:none editor can't measure itself — see EditorPane's header.
  useEffect(() => {
    if (shown) editorRef.current?.getView()?.requestMeasure();
  }, [shown]);

  useEffect(() => {
    register(path, editorRef);
    return () => register(path, null);
  }, [path, register]);

  return (
    <div className={active ? "absolute inset-0" : "hidden"}>
      <CodeEditor
        ref={editorRef}
        filePath={path}
        value={proposed}
        review={review}
        // A deletion has nothing to edit — the person's only choices are
        // Keep (delete the file) and Undo (keep it).
        readOnly={op === "delete"}
        onReviewChange={(snap) => onChange(path, snap)}
      />
    </div>
  );
});

function ToolbarButton({ onClick, disabled, title, children, primary = false }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
        primary
          ? "bg-[var(--accent)] text-[var(--accent-text)] hover:opacity-90"
          : "border border-[var(--neutral-700)] text-[var(--neutral-200)] hover:bg-[var(--neutral-800)]"
      }`}
    >
      {children}
    </button>
  );
}

/**
 * @param {object} props
 * @param {object} props.review - the editor store's `review` slice
 * @param {string[]} props.dirtyPaths - reviewed files that also have unsaved edits open in a normal tab
 * @param {boolean} props.visible - false while the whole editor column is hidden (phone layout showing the explorer)
 * @param {(path: string) => void} props.onSelectFile
 * @param {(path: string, snap: {current: string, remaining: number, added: number, removed: number}) => void} props.onFileChange
 * @param {() => void} props.onDone - send every file's decision to resolve()
 * @param {() => void} props.onClose - leave the review; the proposal stays pending on the server
 */
function ReviewPanel({ review, dirtyPaths, visible, onSelectFile, onFileChange, onDone, onClose }) {
  const editorRefs = useRef(new Map());
  const register = useCallback((path, ref) => {
    if (ref) editorRefs.current.set(path, ref);
    else editorRefs.current.delete(path);
  }, []);

  const progress = reviewProgress(review);
  const busy = review.submitting;
  const canDone = progress.allResolved && !busy && !review.stale;
  const labels = useMemo(() => tabLabels(review.order), [review.order]);

  const forEachEditor = (fn) => {
    for (const ref of editorRefs.current.values()) if (ref.current) fn(ref.current);
  };
  const active = () => editorRefs.current.get(review.activePath)?.current;

  const statusText = !progress.ready
    ? "Loading changes…"
    : progress.remaining === 0
    ? "All changes reviewed"
    : `${progress.remaining} change${progress.remaining === 1 ? "" : "s"} left in ${progress.filesLeft} file${
        progress.filesLeft === 1 ? "" : "s"
      }`;

  const doneTitle = review.stale
    ? "This edit can no longer be applied"
    : !progress.ready
    ? "Still loading"
    : progress.remaining > 0
    ? "Keep or undo every change first — Keep all / Undo all settle the rest at once"
    : "Apply your decisions";

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <div className="shrink-0 flex flex-col gap-2 border-b border-[var(--neutral-800)] bg-[var(--neutral-900)] px-3 py-2">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <div className="flex min-w-0 items-center gap-2">
            <Sparkles size={13} className="shrink-0 text-[var(--accent)]" />
            <span
              className="min-w-0 truncate text-xs text-[var(--neutral-200)]"
              title={review.instruction || review.summary || undefined}
            >
              {review.summary || review.instruction || "AI edit"}
            </span>
          </div>
          <span role="status" aria-live="polite" className="shrink-0 text-[11px] text-[var(--neutral-500)]">
            {statusText}
          </span>
        </div>

        {review.order.length > 0 && (
          <div role="tablist" aria-label="Files in this edit" className="flex gap-1 overflow-x-auto">
            {labels.map(({ path, name, hint }) => {
              const f = review.files[path];
              const isActive = path === review.activePath;
              const resolved = f.remaining === 0;
              return (
                <button
                  key={path}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => onSelectFile(path)}
                  title={path}
                  className={`shrink-0 flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] transition-colors ${
                    isActive
                      ? "border-[var(--neutral-600)] bg-[var(--neutral-800)] text-[var(--neutral-100)]"
                      : "border-[var(--neutral-800)] text-[var(--neutral-400)] hover:bg-[var(--neutral-800)]"
                  }`}
                >
                  <span className="max-w-[12rem] truncate">
                    {name}
                    {hint ? <span className="text-[var(--neutral-600)]"> · {hint}</span> : null}
                  </span>
                  {OP_LABELS[f.op] && (
                    <span className="rounded bg-[var(--neutral-700)] px-1 text-[10px] uppercase tracking-wide text-[var(--neutral-300)]">
                      {OP_LABELS[f.op]}
                    </span>
                  )}
                  {f.remaining == null ? null : resolved ? (
                    <Check size={11} className="text-emerald-400" aria-label="All changes in this file reviewed" />
                  ) : (
                    <span className="flex gap-1 font-mono text-[10px]">
                      <span className="text-emerald-400">+{f.added}</span>
                      <span className="text-red-400">−{f.removed}</span>
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}

        <div role="toolbar" aria-label="Review actions" className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <ToolbarButton onClick={() => active()?.prevChange()} disabled={busy} title="Previous change in this file">
              <ChevronUp size={12} /> Prev
            </ToolbarButton>
            <ToolbarButton onClick={() => active()?.nextChange()} disabled={busy} title="Next change in this file">
              <ChevronDown size={12} /> Next
            </ToolbarButton>
            <span className="mx-1 h-4 w-px bg-[var(--neutral-800)]" aria-hidden="true" />
            <ToolbarButton
              onClick={() => forEachEditor((ed) => ed.keepAll())}
              disabled={busy || review.stale || progress.remaining === 0}
              title="Keep every remaining change in every file"
            >
              Keep all
            </ToolbarButton>
            <ToolbarButton
              onClick={() => forEachEditor((ed) => ed.undoAll())}
              disabled={busy || review.stale || progress.remaining === 0}
              title="Undo every remaining change in every file"
            >
              Undo all
            </ToolbarButton>
          </div>
          <div className="flex items-center gap-1.5">
            <ToolbarButton
              onClick={onClose}
              disabled={busy}
              title="Close the review — the edit stays pending and you can reopen it"
            >
              {review.stale ? "Close" : "Cancel"}
            </ToolbarButton>
            <ToolbarButton onClick={onDone} disabled={!canDone} title={doneTitle} primary>
              {busy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
              {busy ? "Applying…" : "Done"}
            </ToolbarButton>
          </div>
        </div>
      </div>

      {dirtyPaths.length > 0 && (
        <div className="shrink-0 flex items-start gap-2 border-b border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[11px] text-amber-300">
          <AlertTriangle size={12} className="mt-px shrink-0" />
          <span>
            You have unsaved edits in {dirtyPaths.join(", ")}. This edit was made against the saved version, so keeping
            it will mark {dirtyPaths.length === 1 ? "that file" : "those files"} as changed on the server.
          </span>
        </div>
      )}

      {review.error && (
        <div
          role="alert"
          className="shrink-0 flex items-start gap-2 border-b border-red-500/30 bg-red-500/10 px-3 py-1.5 text-[11px] text-red-300"
        >
          <AlertTriangle size={12} className="mt-px shrink-0" />
          <span>{review.error}</span>
        </div>
      )}

      <div className="relative flex-1 min-h-0">
        {review.order.map((path) => {
          const f = review.files[path];
          return (
            <ReviewEditor
              key={path}
              path={path}
              op={f.op}
              original={f.original}
              proposed={f.proposed}
              active={path === review.activePath}
              visible={visible}
              register={register}
              onChange={onFileChange}
            />
          );
        })}
      </div>
    </div>
  );
}

export default memo(ReviewPanel);
