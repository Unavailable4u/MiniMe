"use client";
import { Sparkles, Loader2, Check, AlertCircle, ChevronRight } from "lucide-react";
import { TARGETS } from "../../lib/notebookCapabilities";

// NEW — Notebooks Chat-First refinement, Phase 2 step 2.10. Pulled out
// of NotebooksGeneratePicker.jsx -- byte-for-byte the same body it used
// to have inline -- so MessageBubble.jsx can render the exact same
// running/done/error pill for a chat-triggered generation as the
// picker's own popover already renders for a manually-triggered one.
// One row component now backs both places instead of a second copy that
// could silently drift from this one. NotebooksGeneratePicker.jsx
// imports this instead of defining its own copy (see that file); that
// import site goes away entirely once step 2.9 removes the popover.
const TARGETS_BY_KEY = Object.fromEntries(TARGETS.map((t) => [t.key, t]));

export default function BranchRow({ branch, onNavigate }) {
  const target = TARGETS_BY_KEY[branch.panel_key];
  const label = target?.label || branch.panel_key;
  const Icon = target?.icon || Sparkles;
  return (
    <div className="flex items-center justify-between gap-2 px-2.5 py-1.5 rounded-lg border border-[var(--neutral-800)]">
      <div className="flex items-center gap-2 min-w-0">
        <Icon size={13} className="shrink-0 text-[var(--neutral-500)]" />
        <span className="text-xs text-[var(--neutral-200)] truncate">{label}</span>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {branch.status === "running" && <Loader2 size={13} className="animate-spin text-[var(--neutral-500)]" />}
        {branch.status === "done" && branch.result?.status === "up_to_date" && (
          <span className="text-[10px] text-[var(--neutral-500)]">up to date</span>
        )}
        {branch.status === "done" && branch.result?.status !== "up_to_date" && (
          <Check size={13} className="text-green-400" />
        )}
        {branch.status === "error" && (
          <span className="flex items-center gap-1 text-[10px] text-red-400" title={branch.error}>
            <AlertCircle size={12} /> Error
          </span>
        )}
        {branch.status === "done" && target?.subTab && (
          <button
            type="button"
            onClick={() => onNavigate(target.subTab)}
            title={`Open ${label}`}
            className="text-[var(--neutral-500)] hover:text-[var(--cyber-cyan)]"
          >
            <ChevronRight size={13} />
          </button>
        )}
      </div>
    </div>
  );
}
