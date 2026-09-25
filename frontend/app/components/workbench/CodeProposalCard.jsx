"use client";
// frontend/app/components/workbench/CodeProposalCard.jsx — W5.4 (Build
// Workbench plan). The persisted "code_proposal" chat-message role
// (MessageBubble.jsx's own early-return branch, same shape as its
// "generation"/"suggestion" cards) — a scrollback-visible record of an
// Edit-mode send, with the same Review / Keep all / Reject affordances
// EditorWorkbench.jsx's PendingTray.jsx gives the cross-session tray.
//
// Deliberately NOT the source of truth (plan §5 W5.4: "the tray is the
// source of truth — the chat card is a convenience... the proposal
// survives closing the tab"): this card's `status` only ever updates
// live, while this tab stays open, via subscribeToProposalEvents'
// onResolved (WorkspaceChatPanel.jsx's own effect) or this card's own
// buttons succeeding — a page reload shows whatever status was
// persisted at send time, the same "convenience, not authority"
// trade-off runGenerateTarget's `role:"generation"` card makes by not
// persisting itself at all. Unlike that one, this DOES persist
// (dock.persistMessage) — see sendCodeEditProposal's own comment in
// WorkspaceChatPanel.jsx for why a proposal's card earns that where a
// generation run's didn't.
import { useState } from "react";
import { Check, ChevronRight, Loader2, X } from "lucide-react";
import { fileDiffStats, unreviewableReason } from "../../lib/workbench/reviewMode";

const OP_LABELS = { create: "new", delete: "deleted" }; // mirrors ReviewPanel.jsx's own copy

const STATUS_STYLES = {
  pending: { label: "Pending review", text: "text-amber-300" },
  accepted: { label: "Applied", text: "text-emerald-400" },
  rejected: { label: "Discarded", text: "text-[var(--neutral-500)]" },
  partial: { label: "Partly applied", text: "text-violet-400" },
  stale: { label: "Out of date", text: "text-amber-300" },
  failed: { label: "Failed", text: "text-red-400" },
};

/**
 * @param {object} props
 * @param {object} props.message - a persisted `{role: "code_proposal", ...}` message (WorkspaceChatPanel.jsx's sendCodeEditProposal builds the shape)
 * @param {(message: object) => void} props.onReview
 * @param {(message: object) => Promise<void>} props.onKeepAll
 * @param {(message: object) => Promise<void>} props.onReject
 */
export default function CodeProposalCard({ message, onReview, onKeepAll, onReject }) {
  const [busy, setBusy] = useState(null); // "keep" | "reject" | null

  const status = message.status || "pending";
  const style = STATUS_STYLES[status] || { label: status, text: "text-[var(--neutral-400)]" };
  const isPending = status === "pending";

  const run = async (mode, action) => {
    setBusy(mode);
    try {
      await action?.(message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex justify-start">
      <div className="w-full max-w-[85%] space-y-2 rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-900)] px-3 py-2.5 text-sm">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate font-medium text-[var(--neutral-200)]" title={message.instruction || undefined}>
            {message.summary || message.instruction || "AI edit"}
          </span>
          <span className={`shrink-0 text-[11px] font-medium ${style.text}`}>{style.label}</span>
        </div>

        {message.files?.length > 0 && (
          <ul className="space-y-1">
            {message.files.map((f) => {
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

        {!isPending && (
          <p className="text-[11px] text-[var(--neutral-500)]">{unreviewableReason({ status, files: message.files })}</p>
        )}

        {isPending && (
          <div className="flex items-center gap-1.5 pt-0.5">
            <button
              type="button"
              onClick={() => onReview?.(message)}
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
        )}
      </div>
    </div>
  );
}
