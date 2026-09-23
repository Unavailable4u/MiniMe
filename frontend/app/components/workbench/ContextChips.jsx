"use client";
// frontend/app/components/workbench/ContextChips.jsx — W4.1 (Build
// Workbench plan). Renders lib/workbench/codeContext.js's `refs` as a
// row of dismissible chips with click-to-jump — the plan's own "chips
// with ×, click-to-jump" bullet.
//
// Reads the store directly via useCodeContext() rather than taking
// `refs`/callbacks as props: BuildTab.jsx mounts CodeContextProvider
// above both this and EditorWorkbench as siblings (see codeContext.js's
// own header), so there's nothing for BuildTab to prop-drill — it just
// renders <ContextChips /> wherever it wants the tray to show today.
// W4.2 is expected to relocate that render to sit just above the chat
// composer ("chips render above the composer" — plan §5 W4.2) without
// this component itself changing at all.
//
// Deliberately NOT wired into any chat message yet — that's W4.2. This
// is only the tray: add / see / remove / jump.
import { memo } from "react";
import { File, Folder, MessageSquareCode, X } from "lucide-react";
import { contextBudget, useCodeContext } from "../../lib/workbench/codeContext";
import { basename } from "../../lib/workbench/fileTree";

const KIND_ICONS = {
  range: MessageSquareCode,
  file: File,
  folder: Folder,
  element: MessageSquareCode,
  error: MessageSquareCode,
};

function chipLabel(ref) {
  const name = basename(ref.path) || ref.path;
  if (ref.kind === "range") {
    return ref.fromLine === ref.toLine ? `${name} L${ref.fromLine}` : `${name} L${ref.fromLine}-${ref.toLine}`;
  }
  if (ref.kind === "folder") return `${name}/`;
  return name;
}

function Chip({ entry, onRemove, onJump }) {
  const Icon = KIND_ICONS[entry.kind] || File;
  const jumpable = typeof onJump === "function";
  return (
    <span className="group inline-flex items-center gap-1 rounded-full border border-[var(--neutral-700)] bg-[var(--neutral-900)] pl-2 pr-1 py-0.5 text-[11px] text-[var(--neutral-300)]">
      <Icon size={11} className="shrink-0 text-[var(--neutral-500)]" />
      <button
        type="button"
        disabled={!jumpable}
        onClick={onJump}
        title={jumpable ? `Jump to ${entry.path}` : entry.path}
        className={`truncate max-w-[12rem] ${jumpable ? "hover:underline underline-offset-2" : "cursor-default"}`}
      >
        {chipLabel(entry)}
      </button>
      {entry.truncated && (
        <span title={`Only the first lines were kept (over the size cap)`} className="text-amber-400">
          …
        </span>
      )}
      <button
        type="button"
        onClick={onRemove}
        title="Remove from chat context"
        aria-label={`Remove ${entry.path} from chat context`}
        className="touch-target shrink-0 rounded-full p-0.5 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
      >
        <X size={11} />
      </button>
    </span>
  );
}

/**
 * @param {object} props
 * @param {string} [props.className] - wrapper class; the caller
 *   decides padding/placement (today: a strip above EditorWorkbench in
 *   BuildTab.jsx's Editor sub-tab; W4.2 may move it above the chat
 *   composer instead).
 */
function ContextChips({ className }) {
  const { refs, removeRef, requestJump } = useCodeContext();

  if (refs.length === 0) return null;

  const budget = contextBudget(refs);

  return (
    <div className={className}>
      {budget.overBudget && (
        <p className="text-[10px] text-amber-300 mb-1">
          That&apos;s {Math.round(budget.totalChars / 1000)}k characters of context — consider removing a few chips.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        {refs.map((ref) => (
          <Chip
            key={ref.id}
            entry={ref}
            onRemove={() => removeRef(ref.id)}
            // Folder refs have no single position to jump to — W5.5
            // expands them server-side into a file set, and until then
            // there's nowhere to scroll.
            onJump={ref.kind === "folder" ? undefined : () => requestJump(ref)}
          />
        ))}
      </div>
    </div>
  );
}

export default memo(ContextChips);
