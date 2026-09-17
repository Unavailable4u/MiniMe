"use client";
import { useEffect } from "react";

// Generic slide-in overlay drawer. Both current mobile forks that need
// one (ChatSidebar — left, WorkingPanelDrawer — right) share this
// instead of each rolling their own backdrop/escape/scroll-lock, same
// "one shared primitive instead of N near-identical forks" reasoning
// the plan already applies to the ~8 modal components (Phase 4). This
// one is deliberately narrower than that future ResponsiveSheet: a side
// rail and a bottom sheet are different enough shapes (see
// WorkingPanelDrawer.jsx's own header comment) that trying to cover
// both here would just mean a pile of side-only props on a component
// half the callers don't use.
//
// Deliberately does NOT set a width on the sliding panel itself. Only
// setting `left-0` (or `right-0`) — not both — on an absolutely
// positioned box makes it shrink-wrap to whatever width its *content*
// asks for, so each caller sizes its own children (ChatSidebar reuses
// the desktop component's existing `w-64`; WorkingPanelDrawer sets its
// own width on the wrapper it passes in) instead of fighting a width
// this component picked for them. `max-w-[90vw]` is just a safety cap
// so nothing can ever demand more than the viewport has.
export default function MobileDrawer({ side = "left", open, onClose, children }) {
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

  const isLeft = side === "left";

  return (
    <div
      className="fixed inset-0 z-50"
      style={{ pointerEvents: open ? "auto" : "none" }}
      aria-hidden={!open}
    >
      {/* Backdrop */}
      <div
        onClick={onClose}
        className={`absolute inset-0 bg-black/60 transition-opacity duration-200 ${
          open ? "opacity-100" : "opacity-0"
        }`}
      />
      {/* Sliding panel */}
      <div
        className={`absolute inset-y-0 ${isLeft ? "left-0" : "right-0"} h-full max-w-[90vw] bg-[var(--neutral-950)] shadow-2xl transition-transform duration-200 ease-out ${
          open ? "translate-x-0" : isLeft ? "-translate-x-full" : "translate-x-full"
        }`}
      >
        {children}
      </div>
    </div>
  );
}
