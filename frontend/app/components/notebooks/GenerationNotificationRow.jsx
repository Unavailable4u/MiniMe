"use client";
import { Sparkles, Loader2, Check, AlertCircle, ChevronRight } from "lucide-react";
import { TARGETS } from "../../lib/notebookCapabilities";

// NEW — Notebooks Chat-First refinement, Phase 4 step 4.6. Renders one
// row per live entry in WorkspaceDockContext.jsx's `generationNotifications`
// (step 4.5) -- itself fed by eo/notify.py's generation_started/
// generation_done/generation_error events (step 4.3/4.4), pushed over the
// same session-${session_id} Pusher channel BranchRow's own chat-triggered
// "generation" message role already renders live steps from.
//
// Styled to match BranchRow.jsx's pill (same icon+label+status layout,
// same size/color tokens) so the two look identical in the thread, but
// kept as its own component rather than a BranchRow variant: the two
// read different shapes. BranchRow's `branch` is
// {panel_key, status: "running"|"done"|"error", result?, error?} --
// api/server.py's notebooks_generate() response shape (Phase 2).
// This file's `notification` is WorkspaceDockContext's own
// {panelKey, workspaceId, label, status: "started"|"done"|"error",
// timestamp} -- eo/notify.py's payload shape (step 4.3's VALID_KINDS
// comment: {panel_key, workspace_id, label}). Different status
// vocabulary ("started" vs "running") and no `result`/`error` object to
// read a sub-status or message off of, so translating one shape into
// the other at every call site would buy nothing over two small
// components that happen to render the same way.
//
// Distinct from MessageBubble.jsx too, per the plan step's own wording:
// these aren't a chat turn's response and may not correspond to any
// message in the thread at all (a generation Phase 3's proactive
// suggestions kick off, for instance, has no preceding user message to
// attach a bubble to) -- this is a standalone status ticker, mounted
// alongside the message list rather than as one of its entries.
//
// `label` here always shows the capability's display name (from the
// TARGETS manifest, or the raw panel_key as a last-resort fallback),
// same as BranchRow -- NOT notification.label, which for a "started"/
// "done" event mirrors that same display name (api/server.py's
// _capability_label()) but for an "error" event is instead the
// human-readable exception message (see eo/notify.py's VALID_KINDS
// comment and api/server.py's notebooks_generate()). That message is
// surfaced as the error pill's title/tooltip instead, exactly where
// BranchRow already puts branch.error.
//
// onNavigate is threaded through by WorkspaceChatPanel.jsx (step 4.7),
// via the exact same `onNavigateSubTab?.(subTab)` pass-through
// MessageBubble.jsx already gives BranchRow — so a "done" row's chevron
// opens the same sub-tab a BranchRow chevron would for the same
// panel_key. The button below only mounts once both a subTab exists on
// the target AND onNavigate was actually passed, so this component still
// degrades to a plain status pill (no chevron) for any future caller
// that doesn't wire navigation through.
const TARGETS_BY_KEY = Object.fromEntries(TARGETS.map((t) => [t.key, t]));

export default function GenerationNotificationRow({ notification, onNavigate }) {
  const target = TARGETS_BY_KEY[notification.panelKey];
  const label = target?.label || notification.panelKey;
  const Icon = target?.icon || Sparkles;

  return (
    <div className="flex items-center justify-between gap-2 px-2.5 py-1.5 rounded-lg border border-[var(--neutral-800)]">
      <div className="flex items-center gap-2 min-w-0">
        <Icon size={13} className="shrink-0 text-[var(--neutral-500)]" />
        <span className="text-xs text-[var(--neutral-200)] truncate">{label}</span>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {notification.status === "started" && (
          <Loader2 size={13} className="animate-spin text-[var(--neutral-500)]" />
        )}
        {notification.status === "done" && <Check size={13} className="text-green-400" />}
        {notification.status === "error" && (
          <span
            className="flex items-center gap-1 text-[10px] text-red-400"
            title={notification.label}
          >
            <AlertCircle size={12} /> Error
          </span>
        )}
        {notification.status === "done" && target?.subTab && onNavigate && (
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
