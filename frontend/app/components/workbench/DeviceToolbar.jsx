"use client";
// frontend/app/components/workbench/DeviceToolbar.jsx — the preview
// pane's top toolbar (device presets, inspect crosshair, warnings,
// reload, open-in-new-tab) and its preset list, moved out of
// PreviewPane.jsx unchanged in W7.3 so UrlPreview.jsx can use the same
// toolbar without importing PreviewPane (which imports UrlPreview — a
// cycle). Behavior and markup are exactly what W6.1/W6.4 shipped.
import { AlertTriangle, Crosshair, ExternalLink, Monitor, RefreshCw, Smartphone, Tablet } from "lucide-react";

export const DEVICE_PRESETS = [
  { id: "mobile", label: "Mobile", Icon: Smartphone, width: 390 },
  { id: "tablet", label: "Tablet", Icon: Tablet, width: 768 },
  { id: "desktop", label: "Desktop", Icon: Monitor, width: null }, // null = fill available width
];

export function DeviceToolbar({ device, onDeviceChange, onReload, onOpenNewTab, warnings, inspecting, onToggleInspect }) {
  return (
    <div className="shrink-0 flex items-center justify-between gap-2 px-2 h-8 border-b border-[var(--neutral-800)]">
      <div className="flex items-center gap-0.5">
        {DEVICE_PRESETS.map(({ id, label, Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => onDeviceChange(id)}
            title={label}
            aria-label={label}
            aria-pressed={device === id}
            className={`touch-target p-1 rounded ${
              device === id ? "bg-white/10 text-[var(--neutral-100)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            <Icon size={13} />
          </button>
        ))}
        {onToggleInspect && (
          <>
            <span className="mx-1 h-4 w-px bg-[var(--neutral-800)]" />
            <button
              type="button"
              onClick={onToggleInspect}
              title={inspecting ? "Stop inspecting (Esc)" : "Inspect element"}
              aria-label="Inspect element"
              aria-pressed={inspecting}
              className={`touch-target p-1 rounded ${
                inspecting ? "bg-[var(--accent)] text-[var(--accent-text)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
              }`}
            >
              <Crosshair size={13} />
            </button>
          </>
        )}
      </div>
      <div className="flex items-center gap-1">
        {warnings?.length > 0 && (
          <span title={warnings.join("\n")} className="flex items-center gap-1 text-[10px] text-amber-400">
            <AlertTriangle size={11} />
            {warnings.length}
          </span>
        )}
        <button type="button" onClick={onReload} title="Reload" aria-label="Reload" className="touch-target p-1 rounded text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
          <RefreshCw size={13} />
        </button>
        <button
          type="button"
          onClick={onOpenNewTab}
          title="Open in new tab"
          aria-label="Open in new tab"
          className="touch-target p-1 rounded text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
        >
          <ExternalLink size={13} />
        </button>
      </div>
    </div>
  );
}
