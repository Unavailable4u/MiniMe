"use client";
// frontend/app/components/workbench/PendingTray.jsx — W5.4 (Build
// Workbench plan). StatusBar.jsx's "Pending changes (N)" chip opens
// this: every PENDING proposal EditorWorkbench.jsx currently knows
// about (editorStore's `proposals`, loaded at mount from GET
// .../code/proposals?status=pending and kept live over Pusher — see
// that component's own effects for how). "The tray is the source of
// truth... the proposal survives closing the tab" (plan §5 W5.4),
// independent of whatever WorkspaceChatPanel.jsx's own `code_proposal`
// chat cards happen to still be scrolled into view in this session.
//
// Presentational, same split ReviewPanel.jsx uses: proposal data and
// the resolve/regenerate round trips live in EditorWorkbench.jsx; this
// only renders what it's given and turns clicks into the callbacks
// below. ResponsiveSheet, not a hand-rolled overlay — same modal
// primitive QuickOpen.jsx already uses (Escape/backdrop-click/scroll
// lock, a full-screen sheet on mobile, all for free).
import { useState } from "react";
import { AlertTriangle, Check, ChevronRight, Loader2, RefreshCw, X } from "lucide-react";
import { fileDiffStats } from "../../lib/workbench/reviewMode";
import ResponsiveSheet from "../mobile/ResponsiveSheet";

const OP_LABELS = { create: "new", delete: "deleted" }; // mirrors ReviewPanel.jsx's own copy

function ProposalRow({ proposal, onReview, onKeepAll, onReject, onRegenerate }) {
  const [busy, setBusy] = useState(null); // "keep" | "reject" | "regenerate" | null

  const run = async (mode, action) => {
    setBusy(mode);
    try {
      await action?.(proposal);
    } finally {
      setBusy(null);
    }
  };

  return (
    <li className="space-y-2 rounded-md border border-[var(--neutral-800)] bg-[var(--neutral-900)] p-2.5">
      <p className="truncate text-sm text-[var(--neutral-200)]" title={proposal.instruction || undefined}>
        {proposal.summary || proposal.instruction || "AI edit"}
      </p>

      {proposal.files?.length > 0 && (
        <ul className="space-y-1">
          {proposal.files.map((f) => {
            const { added, removed } = fileDiffStats(f.original || "", f.proposed || "");
            return (
              <li key={f.path} className="flex items-center gap-1.5 text-[11px] text-[var(--neutral-400)]">
                <span className="truncate">{f.path}</span>
                {OP_LABELS[f.op] && (
                  <span className="shrink-0 rounded bg-[var(--neutral-700)] px-1 text-[10px] uppercase tracking-wide text-[var(--neutral-300)]">
                    {OP_LABELS[f.op]}
                  </span>
                )}
                <span className="flex shrink-0 gap-1 font-mono">
                  <span className="text-emerald-400">+{added}</span>
                  <span className="text-red-400">−{removed}</span>
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {/* W5.4 stale banner — editorStore.js's PROPOSAL_FILES_CHANGED sets
          this from a code_file_updated path match, not a base_hash
          recompute (see that reducer case's own comment for why). */}
      {proposal.possiblyStale && (
        <div className="flex items-center justify-between gap-2 rounded border border-amber-900/60 bg-amber-950/30 px-2 py-1.5 text-[11px] text-amber-300">
          <span className="flex items-center gap-1.5">
            <AlertTriangle size={12} className="shrink-0" />
            File changed since this edit was proposed
          </span>
          <span className="flex shrink-0 items-center gap-2.5">
            <button
              type="button"
              onClick={() => run("regenerate", onRegenerate)}
              disabled={busy != null}
              className="flex items-center gap-1 underline underline-offset-2 hover:text-amber-200 disabled:opacity-50"
            >
              {busy === "regenerate" ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />} Regenerate
            </button>
            <button
              type="button"
              onClick={() => onReview?.(proposal)}
              className="underline underline-offset-2 hover:text-amber-200"
            >
              Review anyway
            </button>
          </span>
        </div>
      )}

      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => onReview?.(proposal)}
          disabled={busy != null}
          className="flex items-center gap-1 rounded bg-[var(--accent)] px-2 py-1 text-[11px] font-medium text-[var(--accent-text)] hover:opacity-90 disabled:opacity-50"
        >
          Review <ChevronRight size={11} />
        </button>
        <button
          type="button"
          onClick={() => run("keep", onKeepAll)}
          disabled={busy != null}
          className="flex items-center gap-1 rounded border border-[var(--neutral-700)] px-2 py-1 text-[11px] text-[var(--neutral-300)] hover:bg-[var(--neutral-800)] disabled:opacity-50"
        >
          {busy === "keep" ? <Loader2 size={11} className="animate-spin" /> : <Check size={11} />} Keep all
        </button>
        <button
          type="button"
          onClick={() => run("reject", onReject)}
          disabled={busy != null}
          className="flex items-center gap-1 rounded border border-[var(--neutral-700)] px-2 py-1 text-[11px] text-[var(--neutral-400)] hover:bg-[var(--neutral-800)] disabled:opacity-50"
        >
          {busy === "reject" ? <Loader2 size={11} className="animate-spin" /> : <X size={11} />} Reject
        </button>
      </div>
    </li>
  );
}

/**
 * @param {object} props
 * @param {boolean} props.open
 * @param {() => void} props.onClose
 * @param {object[]} props.proposals - PENDING proposals only; the caller (EditorWorkbench.jsx) filters
 * @param {(proposal: object) => void} props.onReview
 * @param {(proposal: object) => Promise<void>} props.onKeepAll
 * @param {(proposal: object) => Promise<void>} props.onReject
 * @param {(proposal: object) => Promise<void>} props.onRegenerate
 */
export default function PendingTray({ open, onClose, proposals, onReview, onKeepAll, onReject, onRegenerate }) {
  return (
    <ResponsiveSheet
      open={open}
      onClose={onClose}
      maxWidth="max-w-md"
      className="bg-[var(--neutral-950)] border border-[var(--neutral-700)] shadow-2xl"
    >
      <div role="dialog" aria-modal="true" aria-label="Pending changes" className="flex max-h-[70vh] flex-col">
        <div className="flex items-center justify-between border-b border-[var(--neutral-800)] px-3 py-2">
          <h2 className="text-sm font-medium text-[var(--neutral-100)]">Pending changes</h2>
          <button type="button" onClick={onClose} className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
            <X size={14} />
          </button>
        </div>
        <div className="overflow-y-auto p-3">
          {proposals.length === 0 ? (
            <p className="py-4 text-center text-xs text-[var(--neutral-500)]">
              AI-proposed edits waiting for your review will be listed here.
            </p>
          ) : (
            <ul className="space-y-2">
              {proposals.map((p) => (
                <ProposalRow
                  key={p.id}
                  proposal={p}
                  onReview={onReview}
                  onKeepAll={onKeepAll}
                  onReject={onReject}
                  onRegenerate={onRegenerate}
                />
              ))}
            </ul>
          )}
        </div>
      </div>
    </ResponsiveSheet>
  );
}
