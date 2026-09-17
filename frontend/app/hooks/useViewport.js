"use client";
import { useCallback, useEffect, useState } from "react";

// Mobile/tablet/desktop viewport classification. Same "apply immediately
// at module load, read/write via a hook, sync across instances via a
// custom event" pattern as useDensity.js — the one real difference is
// that density only changes when the user explicitly toggles it in
// Settings, while viewport changes on its own whenever the window
// resizes, so the module-level matchMedia listeners below stay live for
// the life of the tab instead of being a one-time read on import.
//
// The actual switch is a `data-viewport` attribute on <html>, mirroring
// `data-density` in globals.css. Two different kinds of consumer read it
// two different ways:
//   - Cosmetic-only differences (spacing, hiding a column, font size):
//     key off `[data-viewport="mobile"] .foo {}` in CSS, or a
//     `--viewport-*` custom property — same file, no JS branch needed.
//   - Structural differences (different nav model, a genuinely different
//     component tree): call this hook, branch in JS, and render the
//     matching file under components/mobile/ instead.
// See components/mobile/README.md for that distinction in more detail.
export const VIEWPORT_OVERRIDE_KEY = "minime_viewport_override"; // dev-only forced value, see readOverride()
const VIEWPORT_EVENT = "minime-viewport-change";
const VALID = new Set(["mobile", "tablet", "desktop"]);

// Tailwind's own md/lg breakpoints (768px/1024px, tailwind.config.js
// doesn't override the defaults) — kept in sync deliberately so "looks
// different in the browser" and "data-viewport says something
// different" never disagree.
const MOBILE_QUERY = "(max-width: 767px)";
const TABLET_QUERY = "(min-width: 768px) and (max-width: 1023px)";

let mobileMql = null;
let tabletMql = null;

function detectNatural() {
  if (typeof window === "undefined") return "desktop";
  if (window.matchMedia(MOBILE_QUERY).matches) return "mobile";
  if (window.matchMedia(TABLET_QUERY).matches) return "tablet";
  return "desktop";
}

// Dev/testing escape hatch: ?forceViewport=mobile in the URL pins the
// value regardless of actual window size. AppShell has no per-tab
// routing (everything lives under one client-rendered shell — see
// AppShell.jsx), so a query param won't naturally survive once you
// navigate away from it; this persists the choice to localStorage so it
// sticks across reloads until explicitly cleared via setOverride(null).
// Normal (non-forced) usage never touches this key.
function readOverride() {
  if (typeof window === "undefined") return null;
  const fromUrl = new URLSearchParams(window.location.search).get("forceViewport");
  if (VALID.has(fromUrl)) {
    localStorage.setItem(VIEWPORT_OVERRIDE_KEY, fromUrl);
    return fromUrl;
  }
  const saved = localStorage.getItem(VIEWPORT_OVERRIDE_KEY);
  return VALID.has(saved) ? saved : null;
}

function computeViewport() {
  return readOverride() || detectNatural();
}

function applyViewport(viewport) {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.viewport = viewport;
}

function handleChange() {
  applyViewport(computeViewport());
  window.dispatchEvent(new Event(VIEWPORT_EVENT));
}

// Applied once at module load (same reasoning as useDensity.js: a reload
// shouldn't flash the desktop shell for a frame before snapping to
// mobile) and kept live via matchMedia listeners for the rest of the
// tab's lifetime — unlike density, viewport can change without any
// component re-rendering to notice, so this can't be a one-time read.
if (typeof window !== "undefined") {
  applyViewport(computeViewport());
  mobileMql = window.matchMedia(MOBILE_QUERY);
  tabletMql = window.matchMedia(TABLET_QUERY);
  mobileMql.addEventListener("change", handleChange);
  tabletMql.addEventListener("change", handleChange);
}

export function useViewport() {
  // BUGFIX — hydration mismatch (surfaced once mobile/AppShell.jsx and
  // WorkspaceChatPanel.jsx's mobile drawer started branching actual JSX
  // *structure* on this value, not just CSS). `computeViewport()` was
  // being used directly as useState's lazy initializer, which runs
  // DURING the render itself — on the server that always sees
  // `window === undefined` and falls back to "desktop" (per
  // detectNatural() above), but on the real client, the very first
  // render (the one React hydrates the server markup against) already
  // sees the actual matchMedia result, e.g. "mobile" on a phone. Server
  // said "desktop", client's first paint said "mobile" — before
  // hydration ever gets a chance to reconcile them — which is exactly a
  // "Expected server HTML to contain a matching <div> in <header>"-
  // style hydration error for any component that renders a different
  // element tree per viewport.
  //
  // Starting from the same fixed "desktop" value on every first render,
  // server and client alike, keeps that first hydration pass identical
  // no matter what the real device is. The effect below (which only
  // ever runs on the client, never during SSR) then corrects it to the
  // real value immediately after mount — a normal post-hydration state
  // update, not a mismatch. Same category of fix as the usual
  // "isClient" / useSyncExternalStore workaround for any window-
  // dependent value read during render.
  const [viewport, setViewportState] = useState("desktop");

  useEffect(() => {
    // Catch up in case a resize fired between module load and this
    // component mounting, and stay in sync with later resizes, other
    // component instances, and the "storage" event (override changed in
    // another tab). This also does the one-time "desktop" -> real-value
    // correction described above, since it always runs once on mount.
    setViewportState(computeViewport());
    function onChange() {
      setViewportState(computeViewport());
    }
    window.addEventListener(VIEWPORT_EVENT, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(VIEWPORT_EVENT, onChange);
      window.removeEventListener("storage", onChange);
    };
  }, []);

  // Dev-only: force a value regardless of actual window size (pass null
  // to clear the override and fall back to real detection). Not wired
  // into any end-user-facing UI yet — reach it via
  // ?forceViewport=mobile|tablet|desktop while that's true.
  const setOverride = useCallback((next) => {
    if (next === null) {
      localStorage.removeItem(VIEWPORT_OVERRIDE_KEY);
    } else if (VALID.has(next)) {
      localStorage.setItem(VIEWPORT_OVERRIDE_KEY, next);
    } else {
      return;
    }
    handleChange();
  }, []);

  return [viewport, setOverride];
}
