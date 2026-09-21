"use client";
// frontend/app/hooks/useLongPress.js — W2.4 (Build Workbench plan).
// Touch long-press for a container whose children are many similar rows
// (the explorer's tree): put the returned handlers on the CONTAINER and
// read `event.target` in the callback to see which row was pressed —
// one timer per container, not one hook per row.
//
// Why this exists instead of relying on the browser's own
// `contextmenu` event: Android Chrome fires that on a long-press, but
// iOS Safari doesn't for anything that isn't a link or image, and the
// plan's mobile requirement is "long-press opens the same menu".
// Where both fire (Android) the caller just gets its menu opened twice
// at the same spot, which is harmless.
//
// A press that moves more than MOVE_TOLERANCE px is a scroll or a
// swipe, not a hold, and cancels. After a long-press fires, the click
// the browser then synthesizes on release is swallowed (via
// onClickCapture) — otherwise releasing your finger would also OPEN
// the file or toggle the folder you just asked a menu for.
import { useCallback, useEffect, useRef } from "react";

const MOVE_TOLERANCE = 10; // px

/**
 * @param {(press: {x: number, y: number, target: EventTarget}) => void} onLongPress
 * @param {{delay?: number}} [opts] - hold time in ms
 */
export function useLongPress(onLongPress, { delay = 500 } = {}) {
  const timer = useRef(null);
  const origin = useRef({ x: 0, y: 0 });
  const fired = useRef(false);
  const callback = useRef(onLongPress);
  callback.current = onLongPress;

  const cancel = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);
  useEffect(() => cancel, [cancel]); // never fire into an unmounted tree

  const onTouchStart = useCallback(
    (e) => {
      cancel();
      fired.current = false;
      if (e.touches.length !== 1) return; // a pinch / two-finger scroll isn't a hold
      const touch = e.touches[0];
      const target = e.target;
      origin.current = { x: touch.clientX, y: touch.clientY };
      timer.current = setTimeout(() => {
        timer.current = null;
        fired.current = true;
        callback.current({ x: origin.current.x, y: origin.current.y, target });
      }, delay);
    },
    [cancel, delay]
  );

  const onTouchMove = useCallback(
    (e) => {
      if (!timer.current) return;
      const touch = e.touches[0];
      if (
        Math.abs(touch.clientX - origin.current.x) > MOVE_TOLERANCE ||
        Math.abs(touch.clientY - origin.current.y) > MOVE_TOLERANCE
      ) {
        cancel();
      }
    },
    [cancel]
  );

  const onTouchEnd = useCallback(() => {
    cancel();
    // The synthesized click (if any) follows immediately; if the browser
    // sends none, don't leave the flag set to eat some later, real click.
    if (fired.current) {
      setTimeout(() => {
        fired.current = false;
      }, 400);
    }
  }, [cancel]);

  const onClickCapture = useCallback((e) => {
    if (!fired.current) return;
    fired.current = false;
    e.preventDefault();
    e.stopPropagation();
  }, []);

  return { onTouchStart, onTouchMove, onTouchEnd, onTouchCancel: onTouchEnd, onClickCapture };
}
