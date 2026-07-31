"use client";
import { useCallback, useEffect, useState } from "react";

// Notebooks Chat-First refinement, Phase 3 step 3.7: a global, per-browser
// opt-out for the two proactive-suggestion surfaces Phase 3 adds —
// notebookAffinities.js's post-generation cross-sell (step 3.1/3.2/3.3,
// gated in WorkspaceDockContext.jsx before it ever appends a suggestion
// message) and eo/prerequisite_suggestions.py's related-topic nudge
// (step 3.4/3.5, gated in MessageBubble.jsx right before it would render
// PrerequisiteSuggestions). Same "read localStorage on mount, write back
// on change" shape as useDensity.js — copied rather than generalized
// into a shared "useLocalStorageToggle" helper, same reasoning
// notebookAffinities.js gave for not refactoring TARGETS_BY_KEY's
// existing duplicates: keep this step's diff to "one more toggle," not
// a refactor of an unrelated, already-shipped hook.
//
// Deliberately a plain per-browser preference (localStorage), not a
// per-workspace server setting — nothing about either suggestion
// surface is workspace-specific in how annoying/useful it is, and a
// server round trip to read one boolean before every chat response
// would be a real latency cost for a preference that changes rarely.
// If a future step wants this synced across devices, this is the one
// module that would need to grow a backend-backed variant — every
// caller below already goes through readProactiveSuggestionsEnabled()
// rather than touching localStorage directly, so that swap wouldn't
// require hunting down call sites.
export const PROACTIVE_SUGGESTIONS_KEY = "minime_proactive_suggestions_enabled";
const PROACTIVE_SUGGESTIONS_EVENT = "minime-proactive-suggestions-change";

// Default ON — the guide's own open-questions section leans conservative
// on HOW OFTEN to suggest (one suggestion, dismissible, never repeated
// per pairing per session — see step 3.8), not on whether the feature
// starts enabled at all. Absence of a saved value (first run, or a
// browser with storage cleared) means "on," matching every other
// default in notebookCapabilities.js's manifest (`enabled` there
// defaults true the same way).
export function readProactiveSuggestionsEnabled() {
  if (typeof window === "undefined") return true;
  const saved = localStorage.getItem(PROACTIVE_SUGGESTIONS_KEY);
  return saved === null ? true : saved !== "0";
}

function writeProactiveSuggestionsEnabled(enabled) {
  if (typeof window === "undefined") return;
  localStorage.setItem(PROACTIVE_SUGGESTIONS_KEY, enabled ? "1" : "0");
  window.dispatchEvent(new Event(PROACTIVE_SUGGESTIONS_EVENT));
}

// The hook — only SettingsTab.jsx's toggle needs to re-render on
// change; WorkspaceDockContext.jsx and MessageBubble.jsx (non-hook
// contexts, or a plain per-render check) call
// readProactiveSuggestionsEnabled() directly instead, same split
// useDensity.js draws between readDensity() and useDensity().
export function useProactiveSuggestions() {
  const [enabled, setEnabledState] = useState(readProactiveSuggestionsEnabled);

  useEffect(() => {
    setEnabledState(readProactiveSuggestionsEnabled());
    function onChange() {
      setEnabledState(readProactiveSuggestionsEnabled());
    }
    window.addEventListener(PROACTIVE_SUGGESTIONS_EVENT, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(PROACTIVE_SUGGESTIONS_EVENT, onChange);
      window.removeEventListener("storage", onChange);
    };
  }, []);

  const setEnabled = useCallback((next) => {
    writeProactiveSuggestionsEnabled(!!next);
  }, []);

  return [enabled, setEnabled];
}
