"use client";
import { useState } from "react";
import { Maximize2, X } from "lucide-react";
import { useIsTouchDevice } from "../hooks/useIsTouchDevice";
import { useViewport } from "../hooks/useViewport";

// NEW — WorkingPanel touch-scroll fix. RoutingTraceGraph (react-force-
// graph canvas) and DependencyGraph (ReactFlow) both bind their own
// pan/zoom/drag handling straight to pointer events so the diagram
// itself can be panned and zoomed — exactly what makes them awkward
// inline in a scrollable panel on a touchscreen: a finger placed on the
// diagram to keep scrolling the page instead starts dragging a node or
// panning the canvas. A mouse/trackpad has no such conflict (scrolling
// happens on the wheel/trackpad, not by touching the canvas) — see
// useIsTouchDevice.js for why `pointer: coarse` is a different test
// than screen width, and why it's the right one for a real device.
//
// FIX — collapse gate was `isTouch` alone. That's correct for a real
// phone/tablet (touch and narrow width coincide there), but it means
// this never collapses when "mobile" is being checked the way this
// app's own dev tooling checks it: useViewport.js's `?forceViewport=
// mobile` override, or just narrowing a desktop browser window/using
// devtools responsive mode without a touch-emulating device preset —
// all of those keep `pointer: coarse` false (still mouse-driven) while
// `data-viewport` flips to "mobile". Symptom matched exactly: the
// routing/dependency/topic graphs stayed fully expanded whenever
// "mobile" was checked that way, looking like the wrap silently didn't
// apply. Collapsing on EITHER signal fixes that without weakening the
// original touch case: a real touch device still collapses (isTouch
// true regardless of viewport, including a wide-landscape touch
// tablet — the case isTouch alone existed to catch), and now so does
// anything the app itself classifies as `viewport === "mobile"`, which
// is the more relevant bar for "does this look right on mobile" during
// day-to-day dev testing than a physical-input check nobody testing
// via a resized window will ever satisfy.
//
// `label` names the diagram in the collapsed box and the expanded
// header ("routing graph", "dependency graph", etc.) — keep it a plain
// lowercase noun phrase, it's interpolated into "Tap to view {label}".
export default function TouchCollapsibleGraph({ label = "diagram", children }) {
  const isTouch = useIsTouchDevice();
  const [viewport] = useViewport();
  const shouldCollapse = isTouch || viewport === "mobile";
  const [open, setOpen] = useState(false);

  // Neither signal fired (desktop/tablet, mouse-driven): unchanged
  // pass-through, no wrapper markup — the diagram renders inline
  // exactly as it did before this component existed.
  if (!shouldCollapse) return children;

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
