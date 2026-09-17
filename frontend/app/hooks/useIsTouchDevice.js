"use client";
import { useEffect, useState } from "react";

// NEW — WorkingPanel touch-scroll fix: whether this device's PRIMARY
// pointer is coarse (touch) rather than fine (mouse/trackpad), via the
// CSS `pointer` media feature.
//
// Deliberately NOT the same test as useViewport.js's mobile/tablet/
// desktop width breakpoints, even though the end result usually lines
// up: this app has components (RoutingTraceGraph/DependencyGraph, via
// react-force-graph / ReactFlow) that bind their own pan/zoom/drag
// handling straight to pointer/touch events, which fights a touchscreen
// user's plain attempt to scroll the page past them — a laptop trackpad
// or mouse never has that conflict in the first place, no matter how
// narrow the window is. Width and input type are different axes: a
// laptop window resized down to "mobile" width is still mouse-driven
// (fine pointer, no touch-scroll conflict to fix), and a large-screen
// tablet in landscape can sit well past the "mobile"/"tablet" width
// breakpoints while staying touch-primary (exactly the case this exists
// to catch). `pointer` (not `any-pointer`) reflects the browser's own
// notion of the PRIMARY input mechanism, so a touchscreen laptop that's
// still mouse-driven day to day correctly reads as fine/non-touch here.
const TOUCH_QUERY = "(pointer: coarse)";

function detect() {
  if (typeof window === "undefined") return false;
  return window.matchMedia(TOUCH_QUERY).matches;
}

export function useIsTouchDevice() {
  // Same SSR-hydration-safe shape as useViewport.js: a fixed value
  // (false — i.e. render exactly as before) on every first render,
  // server and client alike, so hydration never has to reconcile a
  // server guess against a real client reading. The effect below
  // corrects it to the real value immediately after mount, same as
  // useViewport.js's own "desktop" -> real-value correction.
  const [isTouch, setIsTouch] = useState(false);

  useEffect(() => {
    const mql = window.matchMedia(TOUCH_QUERY);
    setIsTouch(mql.matches);
    function onChange() {
      setIsTouch(mql.matches);
    }
    // A handful of real devices (2-in-1s with a detachable/foldable
    // keyboard+mouse) genuinely change their primary pointer type at
    // runtime, so this stays live for the tab's lifetime rather than a
    // one-time read, same reasoning as useViewport.js's own listeners.
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  return isTouch;
}
