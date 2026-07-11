"use client";
import { useCallback, useEffect, useState } from "react";

// Comfortable/Compact density toggle. Same "read localStorage on mount,
// write back on change" pattern AppShell already uses for
// SIDEBAR_KEY/WORKING_PANEL_KEY — the one difference is density is read
// from more than one place (SettingsTab's toggle, plus every card/bubble
// that just wants the current CSS vars), so instead of lifting state
// into SessionContext this stays a standalone module: the actual switch
// is a `data-density` attribute on <html>, driven entirely by CSS
// variables in globals.css. Components never need to know the value —
// they just reference var(--density-*) in a class name — and the one
// place that *does* need the value (the Settings toggle UI) reads it
// through this hook.
export const DENSITY_KEY = "minime_density";
const DENSITY_EVENT = "minime-density-change";
const VALID = new Set(["comfortable", "compact"]);

function readDensity() {
  if (typeof window === "undefined") return "comfortable";
  const saved = localStorage.getItem(DENSITY_KEY);
  return VALID.has(saved) ? saved : "comfortable";
}

function applyDensity(density) {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.density = density;
}

// Applied once at module load (i.e. as soon as this file is imported
// anywhere in the bundle, not just when a component using the hook
// happens to mount) so a reload doesn't flash comfortable spacing for a
// frame before snapping to a saved "compact" preference.
if (typeof window !== "undefined") {
  applyDensity(readDensity());
}

export function useDensity() {
  const [density, setDensityState] = useState(readDensity);

  useEffect(() => {
    // Catch up in case the module-load application above happened
    // before this component existed, and stay in sync with changes
    // made from any other component instance (or another tab, via the
    // native "storage" event).
    setDensityState(readDensity());
    function onChange() {
      setDensityState(readDensity());
    }
    window.addEventListener(DENSITY_EVENT, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(DENSITY_EVENT, onChange);
      window.removeEventListener("storage", onChange);
    };
  }, []);

  const setDensity = useCallback((next) => {
    if (!VALID.has(next)) return;
    localStorage.setItem(DENSITY_KEY, next);
    applyDensity(next);
    window.dispatchEvent(new Event(DENSITY_EVENT));
  }, []);

  return [density, setDensity];
}
