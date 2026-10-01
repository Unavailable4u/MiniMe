"use client";
// frontend/app/components/workbench/AiUpdateToast.jsx — W8.2 (Build
// Workbench plan). "AI updated 4 files": what appears when a chat
// code-generation run rewrites files that are open in the editor,
// listing each one with whether your unsaved edits were kept and a way
// to see the difference. The decisions (which files, which state, the
// wording) are lib/workbench/aiUpdates.js's; this only renders them and
// turns clicks into the callbacks below. EditorWorkbench.jsx owns the
// data and the actual reload / keep-mine work, the same split
// PendingTray.jsx and ReviewPanel.jsx use.
//
// A toast, not a modal: it must never block typing, and the whole point
// of W8.2 is that nothing about it is silent but nothing about it is in
// the way either. It stays until dismissed — a person mid-keystroke
// shouldn't have to race a timer to read which of their files changed.
//
// Placement: bottom-left of the workbench, above the status bar. The
// chat dock's corner (`reserveCorner`) is bottom-right, so the two
// never overlap. Positioned against the workbench root, which
// EditorWorkbench.jsx makes `relative` for this.
import { X } from "lucide-react";
import { basename } from "../../lib/workbench/fileTree";
import { aiUpdateSubtitle, aiUpdateTitle } from "../../lib/workbench/aiUpdates";

const STATE_LABEL = {
  "kept-yours": "Your edits kept",
  reloaded: "Reloaded",
};

/**
 * @param {object} props
 * @param {{path: string, state: "kept-yours"|"reloaded", added: number, removed: number}[]} props.items
 *   already filtered to what's still relevant (see visibleAiUpdates()),
 *   with line stats attached by the caller
 * @param {(path: string) => void} props.onOpen - switch to that file's tab
 * @param {(path: string) => void} props.onCompare
 * @param {(path: string) => void} props.onTakeTheirs - "Use AI's": replace your unsaved edits with the new version
 * @param {(path: string) => void} props.onKeepMine
 * @param {(path: string) => void} props.onDismissOne
 * @param {() => void} props.onDismissAll
 */
export default function AiUpdateToast({
  items,
  onOpen,
  onCompare,
  onTakeTheirs,
  onKeepMine,
  onDismissOne,
  onDismissAll,
}) {
  if (!items || items.length === 0) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="absolute bottom-9 left-3 z-30 w-[min(28rem,calc(100%-1.5rem))] rounded-lg border border-[var(--neutral-700)] bg-[var(--neutral-900)] text-[var(--neutral-200)] shadow-lg"
    >
      <div className="flex items-start justify-between gap-3 px-3 pt-2.5 pb-2">
        <div className="min-w-0">
          <p className="text-xs font-medium">{aiUpdateTitle(items.length)}</p>
          <p className="mt-0.5 text-[11px] text-[var(--neutral-500)]">{aiUpdateSubtitle(items)}</p>
        </div>
        <button
          type="button"
          onClick={onDismissAll}
          title="Dismiss"
          aria-label="Dismiss"
          className="shrink-0 rounded p-0.5 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
        >
          <X size={14} />
        </button>
      </div>

      <ul className="max-h-64 overflow-y-auto border-t border-[var(--neutral-800)]">
        {items.map((item) => {
          const kept = item.state === "kept-yours";
          return (
            <li
              key={item.path}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[var(--neutral-800)] px-3 py-2 last:border-b-0"
            >
              <div className="min-w-0 flex-1 basis-40">
                <button
                  type="button"
                  onClick={() => onOpen(item.path)}
                  title={`Open ${item.path}`}
                  className="block max-w-full truncate text-left text-xs hover:underline"
                >
                  {basename(item.path)}
                </button>
                <p className="mt-0.5 flex items-center gap-2 text-[11px]">
                  <span className={kept ? "text-amber-300" : "text-[var(--neutral-500)]"}>
                    {STATE_LABEL[item.state]}
                  </span>
                  <span className="text-emerald-400">+{item.added}</span>
                  <span className="text-red-400">−{item.removed}</span>
                </p>
              </div>

              <div className="flex shrink-0 items-center gap-3 text-[11px]">
                <button
                  type="button"
                  onClick={() => onCompare(item.path)}
                  title={kept ? "See the AI's version next to yours" : "See what changed"}
                  className="underline hover:text-[var(--accent)]"
                >
                  Compare
                </button>
                {kept ? (
                  <>
                    <button
                      type="button"
                      onClick={() => onTakeTheirs(item.path)}
                      title="Discard your unsaved edits and load the AI's version"
                      className="underline hover:text-[var(--accent)]"
                    >
                      Use AI&apos;s
                    </button>
                    <button
                      type="button"
                      onClick={() => onKeepMine(item.path)}
                      title="Keep your edits — your next Save replaces the AI's version"
                      className="underline hover:text-[var(--accent)]"
                    >
                      Keep mine
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() => onDismissOne(item.path)}
                    title="Dismiss"
                    aria-label={`Dismiss ${basename(item.path)}`}
                    className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
                  >
                    <X size={13} />
                  </button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
