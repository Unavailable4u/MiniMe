"use client";
import { AlertTriangle } from "lucide-react";

export default function ConfirmDialog({
  open, title, message, confirmLabel = "Delete", tone = "danger", onConfirm, onCancel,
}) {
  if (!open) return null;
  const accent = tone === "danger" ? "var(--cyber-magenta)" : "var(--cyber-cyan)";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onCancel}
    >
      <div
        className="w-80 rounded-lg p-4"
        style={{
          background: "var(--cyber-panel)",
          border: "1px solid var(--cyber-border)",
          boxShadow: `0 0 24px ${accent}22`,
        }}
        onClick={(e) => e.stopPropagation()}
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
      </div>
    </div>
  );
}