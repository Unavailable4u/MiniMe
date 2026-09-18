"use client";
import { AlertTriangle } from "lucide-react";
import ResponsiveSheet from "./mobile/ResponsiveSheet";

// CHANGED — Phase 4 (mobile modal primitive, see MOBILE_PLAN.md): the
// backdrop/centered-box/escape/scroll-lock this hand-rolled itself now
// all come from ResponsiveSheet, which also gives this a full-screen
// bottom sheet on mobile for free — same visual box (color, border,
// glow) either way, just moved from this component's own wrapper div
// onto ResponsiveSheet's `style` passthrough since it depends on
// `tone`, a runtime value, and can't be a static Tailwind class.
export default function ConfirmDialog({
  open, title, message, confirmLabel = "Delete", tone = "danger", onConfirm, onCancel,
}) {
  const accent = tone === "danger" ? "var(--cyber-magenta)" : "var(--cyber-cyan)";

  return (
    <ResponsiveSheet
      open={open}
      onClose={onCancel}
      maxWidth="max-w-xs"
      className="p-4"
      style={{
        background: "var(--cyber-panel)",
        border: "1px solid var(--cyber-border)",
        boxShadow: `0 0 24px ${accent}22`,
      }}
    >
      <div className="flex items-center gap-2 mb-2">
        <AlertTriangle size={16} style={{ color: accent }} />
        <h3 className="text-sm font-display" style={{ color: "var(--cyber-text)" }}>
          {title}
        </h3>
      </div>
      <p className="text-xs mb-4" style={{ color: "var(--cyber-dim)" }}>
        {message}
      </p>
      <div className="flex justify-end gap-2">
        <button
          onClick={onCancel}
          className="text-xs px-3 py-1.5 rounded"
          style={{ color: "var(--cyber-dim)" }}
        >
          Cancel
        </button>
        <button
          onClick={onConfirm}
          className="text-xs px-3 py-1.5 rounded font-medium"
          style={{
            background: accent,
            color: "var(--cyber-bg)",
            boxShadow: `var(--cyber-glow)`,
          }}
        >
          {confirmLabel}
        </button>
      </div>
    </ResponsiveSheet>
  );
}
