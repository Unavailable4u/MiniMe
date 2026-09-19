"use client";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { MoreVertical, Pencil, Trash2 } from "lucide-react";

// Shared "⋮" options menu for list rows in sidebars and drawers. One
// button opens one dropdown carrying every per-row action, instead of a
// cluster of separate icon buttons.
//
// Extracted from ChatSidebar.jsx's per-chat menu so the Chat tab and
// every stage tab's nested chat rows (Notebooks/Research/Plan/Build/
// Test/Growth) behave identically instead of each keeping its own copy
// of the markup and outside-click handling. Three things differ from
// that original, all for touch:
//
//   - The trigger uses `row-reveal` (see globals.css): hover-to-reveal
//     with a mouse, always visible on touch. The old button was
//     `opacity-0 group-hover:opacity-100`, so on a phone it never showed.
//   - Outside-click listens for `pointerdown`, not `mousedown`. iOS
//     Safari doesn't dispatch mouse events to `document` for a tap on a
//     non-interactive element, so a `mousedown` listener can leave the
//     menu stuck open; pointer events fire for every touch.
//   - The dropdown flips upward when opening downward would run it out
//     of the scrolling list it sits in (the last row of a drawer's
//     list, most often), rather than clipping or forcing a scroll.
//
// `items`: array of { key, label, icon?, danger?, onSelect } — falsy
// entries are skipped so callers can write `cond && { ... }` inline.
// The menu closes itself before calling `onSelect`.
export default function RowMenu({ title = "Options", items, iconSize = 13, className = "" }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e) {
      if (!rootRef.current?.contains(e.target)) setOpen(false);
    }
    function onKeyDown(e) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const visible = (items || []).filter(Boolean);
  if (visible.length === 0) return null;

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        type="button"
        // stopPropagation: every caller's row is itself clickable
        // (open this chat / select this project) — opening the menu
        // must not also trigger that.
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
        title={title}
        aria-label={title}
        aria-haspopup="menu"
        aria-expanded={open}
        className={`row-reveal touch-target p-1 -m-1 rounded text-[var(--neutral-500)] hover:text-[var(--neutral-200)] hover:bg-[var(--neutral-800)] ${className}`}
      >
        <MoreVertical size={iconSize} />
      </button>
      {open && <RowMenuPanel items={visible} anchorRef={rootRef} onClose={() => setOpen(false)} />}
    </div>
  );
}

// Its own component (rather than inline JSX above) so the measuring
// layout effect only exists while the menu is open — and never runs
// during server rendering, where useLayoutEffect has no DOM to read.
function RowMenuPanel({ items, anchorRef, onClose }) {
  const panelRef = useRef(null);
  const [openUp, setOpenUp] = useState(false);

  useLayoutEffect(() => {
    const panel = panelRef.current;
    const anchor = anchorRef.current;
    if (!panel || !anchor) return;
    // The nearest scrolling ancestor is the box that would clip the
    // panel; fall back to the window if there isn't one.
    let clip = anchor.parentElement;
    while (clip && clip !== document.body) {
      if (getComputedStyle(clip).overflowY !== "visible") break;
      clip = clip.parentElement;
    }
    const hasClip = clip && clip !== document.body;
    const clipRect = hasClip ? clip.getBoundingClientRect() : null;
    const limitTop = clipRect ? Math.max(clipRect.top, 0) : 0;
    const limitBottom = clipRect ? Math.min(clipRect.bottom, window.innerHeight) : window.innerHeight;
    const a = anchor.getBoundingClientRect();
    const needed = panel.offsetHeight + 4;
    const roomBelow = limitBottom - a.bottom;
    const roomAbove = a.top - limitTop;
    // Only flip when it actually helps: not enough room below AND more
    // room above. Otherwise stay down, the conventional position.
    setOpenUp(roomBelow < needed && roomAbove > roomBelow);
  }, [anchorRef]);

  return (
    <div
      ref={panelRef}
      role="menu"
      // Menu clicks must not fall through to the row's own click handler.
      onClick={(e) => e.stopPropagation()}
      className={`absolute right-0 z-30 w-44 rounded-lg border border-[var(--neutral-700)] bg-[var(--neutral-900)] py-1 shadow-lg ${
        openUp ? "bottom-full mb-1" : "top-full mt-1"
      }`}
    >
      {items.map((item) => {
        const Icon = item.icon;
        return (
          <button
            key={item.key}
            type="button"
            role="menuitem"
            onClick={() => { onClose(); item.onSelect(); }}
            className={`touch-row flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-[var(--neutral-800)] ${
              item.danger
                ? "text-[var(--neutral-400)] hover:text-red-400"
                : "text-[var(--neutral-300)]"
            }`}
          >
            {Icon && <Icon size={12} />}
            {item.label}
          </button>
        );
      })}
    </div>
  );
}

// The two actions every nested project-chat row offers, identical in
// all six stage tabs. Chat rows in the global ChatSidebar carry more
// (create project, add to project, share memory) and build their own
// `items` for RowMenu directly.
export function ChatRowMenu({ onRename, onDelete }) {
  return (
    <RowMenu
      title="Chat options"
      iconSize={12}
      items={[
        { key: "rename", label: "Rename", icon: Pencil, onSelect: onRename },
        { key: "delete", label: "Delete", icon: Trash2, danger: true, onSelect: onDelete },
      ]}
    />
  );
}
