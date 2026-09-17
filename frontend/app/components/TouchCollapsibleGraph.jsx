"use client";
import { useState } from "react";
import { Maximize2, X } from "lucide-react";
import { useIsTouchDevice } from "../hooks/useIsTouchDevice";

// NEW — WorkingPanel touch-scroll fix. RoutingTraceGraph (react-force-
// graph canvas) and DependencyGraph (ReactFlow) both bind their own
// pan/zoom/drag handling straight to pointer events so the diagram
// itself can be panned and zoomed — exactly what makes them awkward
// inline in a scrollable panel on a touchscreen: a finger placed on the
// diagram to keep scrolling the page instead starts dragging a node or
// panning the canvas. A mouse/trackpad has no such conflict (scrolling
// happens on the wheel/trackpad, not by touching the canvas), so this
// only changes anything on a coarse-pointer (touch) device — see
// useIsTouchDevice.js for why that's a different test than screen
// width.
//
// `label` names the diagram in the collapsed box and the expanded
// header ("routing graph", "dependency graph", etc.) — keep it a plain
// lowercase noun phrase, it's interpolated into "Tap to view {label}".
export default function TouchCollapsibleGraph({ label = "diagram", children }) {
  const isTouch = useIsTouchDevice();
  const [open, setOpen] = useState(false);

  // Non-touch (laptop/PC): unchanged pass-through, no wrapper markup —
  // the diagram renders inline exactly as it did before this component
  // existed.
  if (!isTouch) return children;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="w-full flex items-center justify-center gap-2 rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-900)] text-[var(--neutral-500)] text-xs py-6 hover:text-[var(--neutral-300)] hover:border-[var(--neutral-700)] transition-colors"
      >
        <Maximize2 size={13} />
        Tap to view {label}
      </button>
      {/* Full-screen overlay rather than an inline expand — an inline
          expand would just reintroduce the same "diagram sitting in a
          scrollable column" conflict this whole component exists to
          avoid. Same overlay shape as ConfirmDialog.jsx (backdrop closes
          on click, content stops propagation so tapping inside the
          diagram doesn't dismiss it), sized to actually show the
          diagram rather than ConfirmDialog's small fixed-width box. */}
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-3"
          onClick={() => setOpen(false)}
        >
          <div
            className="w-full max-w-[95vw] h-[80vh] rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-950)] flex flex-col overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="h-11 px-3 flex items-center justify-between border-b border-[var(--neutral-800)] shrink-0">
              <span className="text-xs font-medium text-[var(--neutral-400)] capitalize">{label}</span>
              <button
                onClick={() => setOpen(false)}
                title="Close"
                className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)] p-1"
              >
                <X size={16} />
              </button>
            </div>
            {/* The diagram's own pan/zoom/drag is exactly what the user
                tapped in for, so it's left free to capture touch here —
                overflow-auto is just a fallback for content taller than
                the modal (e.g. DependencyGraph's fixed 260px height
                stacking oddly), not the primary way to navigate it. */}
            <div className="flex-1 min-h-0 overflow-auto p-2">{children}</div>
          </div>
        </div>
      )}
    </>
  );
}
