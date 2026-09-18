"use client";
import { useEffect } from "react";
import { useViewport } from "../../hooks/useViewport";

// Phase 4's shared modal primitive (see MOBILE_PLAN.md). Desktop renders
// the centered backdrop+box shape every modal in this app already
// hand-rolls; mobile renders a full-screen bottom sheet that slides up
// instead — a centered w-96 box with three separate controls doesn't
// work at phone width the way it does on desktop, same reasoning
// mobile/MobileDrawer.jsx already applies to side drawers (and reuses
// the same escape/scroll-lock/backdrop-click handling as that file,
// rather than a third copy of it).
//
// Contract for callers: `className` carries background/border/padding/
// shadow/text-color only — the same box styling a caller's own modal
// already sets today. Do NOT include `rounded-*` or a width/max-width
// utility in it: this component owns rounding (rounded-lg on desktop,
// rounded-t-2xl on mobile, so a bottom sheet's top corners are always
// rounded regardless of what a caller was using for its desktop-only
// box) and width (the `maxWidth` prop — a Tailwind max-w-* class,
// e.g. "max-w-sm" — sets the desktop cap; mobile always goes full
// width, since a bottom sheet that doesn't span the screen reads as a
// bug, not a design choice). `style` is passed through unchanged for
// callers (like ConfirmDialog) whose box color depends on a runtime
// value and can't be a static Tailwind class.
export default function ResponsiveSheet({ open, onClose, children, className = "", style, maxWidth = "max-w-md" }) {
  const [viewport] = useViewport();
  const isMobile = viewport === "mobile";

  useEffect(() => {
    if (!open) return;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function onKeyDown(e) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  if (!open) return null;

  if (isMobile) {
    return (
      <div className="fixed inset-0 z-50 flex items-end" onClick={onClose}>
        <div className="absolute inset-0 bg-black/60" />
        <div
          className={`relative w-full max-h-[92vh] overflow-y-auto rounded-t-2xl pt-2 ${className}`}
          style={style}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Drag-handle affordance — purely visual, this sheet closes via
              the backdrop tap or Escape, not an actual drag gesture (a
              real swipe-to-dismiss is more than this primitive needs to
              take on for its first two callers; revisit if a later
              caller wants it). */}
          <div className="flex justify-center pb-1">
            <div className="w-10 h-1 rounded-full bg-[var(--neutral-700)]" />
          </div>
          {children}
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className={`${maxWidth} w-full max-h-[80vh] overflow-y-auto rounded-lg ${className}`}
        style={style}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}
