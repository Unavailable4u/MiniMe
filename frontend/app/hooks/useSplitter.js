// frontend/app/hooks/useSplitter.js
// NEW — W0.2 (build workbench plan): extracted from
// WorkspaceChatPanel.jsx's hand-rolled startWorkingPanelResize /
// startWorkingPanelResizeVertical (drag a panel's edge to resize its
// width or height). Same behavior, generalized:
//   - a window mousemove/mouseup pair is added only for the duration of
//     the drag, and removed on release (or on unmount mid-drag, via the
//     same cleanupRef-in-an-effect trick WorkspaceChatPanel uses);
//   - the size is clamped to [min, max] on every move;
//   - size is only written to localStorage once, on mouseup -- never on
//     every mousemove, to avoid hammering it during the drag.
// `min`/`max` accept either a plain number or a `() => number` -- the
// function form is for bounds that depend on the viewport (BuildTab's
// chat dock wants "max 60% of viewport width", which can't be a fixed
// constant the way WorkspaceChatPanel's WORKING_PANEL_MAX_WIDTH is).
"use client";
import { useState, useRef, useEffect, useCallback } from "react";

function resolveBound(bound) {
  return typeof bound === "function" ? bound() : bound;
}

/**
 * @param {object} opts
 * @param {"width"|"height"} [opts.axis="width"] - which pointer delta to track
 * @param {number} opts.defaultSize - used until a persisted value (if any) loads
 * @param {number|() => number} opts.min
 * @param {number|() => number} opts.max
 * @param {string} [opts.storageKey] - if set, size persists across reloads
 * @param {boolean} [opts.reverse=false] - true when the drag handle sits on
 *   the panel's leading edge such that dragging TOWARD the panel's origin
 *   should grow it (e.g. a handle on a right-docked panel's left edge --
 *   dragging left grows it, same as WorkspaceChatPanel's own dock today)
 * @returns {{size: number, setSize: Function, onHandleMouseDown: (e) => void}}
 */
export function useSplitter({ axis = "width", defaultSize, min, max, storageKey, reverse = false } = {}) {
  const [size, setSize] = useState(defaultSize);
  const cleanupRef = useRef(null);

  useEffect(() => {
    if (storageKey) {
      const saved = parseInt(localStorage.getItem(storageKey), 10);
      if (!Number.isNaN(saved)) {
        setSize(Math.min(resolveBound(max), Math.max(resolveBound(min), saved)));
      }
    }
    // If the panel unmounts mid-drag (switching tabs without releasing the
    // mouse), make sure the window listeners below don't leak -- same
    // guard WorkspaceChatPanel's own restore effect uses.
    return () => cleanupRef.current?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey]);

  const onHandleMouseDown = useCallback(
    (e) => {
      e.preventDefault();
      const startPos = axis === "width" ? e.clientX : e.clientY;
      const startSize = size;

      function clamp(v) {
        return Math.min(resolveBound(max), Math.max(resolveBound(min), v));
      }

      function onMouseMove(ev) {
        const pos = axis === "width" ? ev.clientX : ev.clientY;
        const delta = pos - startPos;
        setSize(clamp(startSize + (reverse ? -delta : delta)));
      }
      function onMouseUp() {
        cleanup();
        setSize((s) => {
          if (storageKey) localStorage.setItem(storageKey, String(s));
          return s;
        });
      }
      function cleanup() {
        window.removeEventListener("mousemove", onMouseMove);
        window.removeEventListener("mouseup", onMouseUp);
        cleanupRef.current = null;
      }

      window.addEventListener("mousemove", onMouseMove);
      window.addEventListener("mouseup", onMouseUp);
      cleanupRef.current = cleanup;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [axis, size, min, max, reverse, storageKey]
  );

  return { size, setSize, onHandleMouseDown };
}
