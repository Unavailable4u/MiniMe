"use client";
// frontend/app/components/workbench/ContextMenu.jsx — W2.3a (Build
// Workbench plan). A right-click menu positioned at the pointer. The
// tab strip uses it now ("Close others" etc.); W2.4's explorer menu
// (New file / Rename / Delete / Add to chat…) is the second caller,
// which is why it's its own component instead of markup inside
// EditorTabs.jsx.
//
// Not RowMenu.jsx: that one is a "⋮" button that owns its own open
// state and drops a panel under the button. This one has no trigger —
// the caller decides WHERE (the contextmenu event's x/y) and WHEN, and
// this only renders and dismisses.
//
// Rendered through a portal to <body> because the workbench sits in
// overflow-hidden / flex containers, and a `position: fixed` element is
// only viewport-relative if no ancestor creates a containing block
// (transform, filter, contain…) — a portal makes that a non-question.
//
// `items`: `{ key, label, icon?, hint?, danger?, disabled?, title?,
// checked?, onSelect }` (`hint` = a keyboard shortcut shown at the right,
// W2.4; `checked` = W2.5's on/off toggles — a boolean makes the row a
// `menuitemcheckbox` with a tick slot where the icon would be, while
// leaving it undefined keeps an ordinary `menuitem`); falsy
// entries are skipped (so callers can write `cond && {...}` inline) and
// `{ key, separator: true }` draws a divider. The menu closes itself
// before calling `onSelect`. Dismissal: outside pointerdown (not
// mousedown — iOS Safari doesn't send mouse events for taps on
// non-interactive elements, same reason RowMenu uses pointerdown),
// Escape, window resize/blur, or any scroll.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check } from "lucide-react";

const EDGE_MARGIN = 6; // keep the menu this far inside the viewport

export default function ContextMenu({ x, y, items, onClose }) {
  const ref = useRef(null);
  const [pos, setPos] = useState({ left: x, top: y });

  // Measure once the real size is known, then pull the menu back inside
  // the viewport if opening at (x, y) would run it off the right/bottom
  // edge. useLayoutEffect so there's no frame at the wrong spot.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { offsetWidth: w, offsetHeight: h } = el;
    setPos({
      left: Math.max(EDGE_MARGIN, Math.min(x, window.innerWidth - w - EDGE_MARGIN)),
      top: Math.max(EDGE_MARGIN, Math.min(y, window.innerHeight - h - EDGE_MARGIN)),
    });
  }, [x, y]);

  useEffect(() => {
    function onPointerDown(e) {
      if (!ref.current?.contains(e.target)) onClose();
    }
    function onKeyDown(e) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onClose);
    window.addEventListener("blur", onClose);
    // Capture, so a scroll inside any nested scroller (the tab strip,
    // the explorer) dismisses it too — scroll events don't bubble.
    window.addEventListener("scroll", onClose, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("blur", onClose);
      window.removeEventListener("scroll", onClose, true);
    };
  }, [onClose]);

  const visible = (items || []).filter(Boolean);
  if (visible.length === 0 || typeof document === "undefined") return null;

  return createPortal(
    <div
      ref={ref}
      role="menu"
      // A right-click ON the menu shouldn't open the browser's own.
      onContextMenu={(e) => e.preventDefault()}
      style={{ position: "fixed", left: pos.left, top: pos.top }}
      className="z-50 min-w-[10rem] rounded-lg border border-[var(--neutral-700)] bg-[var(--neutral-900)] py-1 shadow-lg"
    >
      {visible.map((item) => {
        if (item.separator) {
          return <div key={item.key} role="separator" className="my-1 border-t border-[var(--neutral-800)]" />;
        }
        const Icon = item.icon;
        const checkable = typeof item.checked === "boolean";
        return (
          <button
            key={item.key}
            type="button"
            role={checkable ? "menuitemcheckbox" : "menuitem"}
            aria-checked={checkable ? item.checked : undefined}
            disabled={item.disabled}
            title={item.title}
            onClick={() => {
              onClose();
              item.onSelect?.();
            }}
            className={`touch-row flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-[var(--neutral-800)] disabled:opacity-40 disabled:hover:bg-transparent ${
              item.danger
                ? "text-[var(--neutral-400)] hover:text-red-400"
                : "text-[var(--neutral-300)]"
            }`}
          >
            {checkable ? (
              <span className="flex h-3 w-3 shrink-0 items-center justify-center text-[var(--accent)]">
                {item.checked && <Check size={12} />}
              </span>
            ) : (
              Icon && <Icon size={12} className="shrink-0" />
            )}
            {item.label}
            {item.hint && <span className="ml-auto pl-6 text-[10px] text-[var(--neutral-500)]">{item.hint}</span>}
          </button>
        );
      })}
    </div>,
    document.body
  );
}
