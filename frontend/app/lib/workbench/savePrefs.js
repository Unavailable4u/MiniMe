// frontend/app/lib/workbench/savePrefs.js — W2.5 (Build Workbench
// plan). The two save-flow toggles the plan calls out by name:
// autosave and format-on-save. Both default OFF ("optional autosave...
// off by default"; "Format-on-save... optional toggle") and are
// workbench-wide, not per-workspace — unlike layoutPrefs.js's panel
// layout, "how I like Save to behave" is a personal habit that
// shouldn't reset when you switch projects, so this is ONE storage key
// shared across every workspace rather than layoutPrefs.js's
// `:${workspaceId}` family.
//
// Same "never throws, unknown/wrong-typed fields fall back
// individually, a value equal to the defaults isn't written" shape as
// layoutPrefs.js's normalizeLayout()/loadLayout()/saveLayout() trio.
// Like that file this one takes the storage object as an ARGUMENT and
// imports nothing, so it loads under plain `node` in its test — callers
// get the real one from layoutPrefs.js's browserStorage(), which
// EditorWorkbench.jsx already imports for the layout.

const STORAGE_KEY = "minime_build_editor_save_prefs";

export const DEFAULT_SAVE_PREFS = Object.freeze({
  autosave: false,
  formatOnSave: false,
});

/**
 * Turns anything into a valid prefs object — total, never throws. See
 * layoutPrefs.normalizeLayout()'s own doc comment for why `fallback`
 * exists (loading a saved value vs. applying a toggle to the current
 * one) and why a wrong-typed field degrades ALONE rather than
 * discarding the whole object.
 *
 * @param {unknown} raw
 * @param {unknown} [fallback]
 */
export function normalizeSavePrefs(raw, fallback) {
  const src = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const base = fallback === undefined ? DEFAULT_SAVE_PREFS : normalizeSavePrefs(fallback);
  return {
    autosave: typeof src.autosave === "boolean" ? src.autosave : base.autosave,
    formatOnSave: typeof src.formatOnSave === "boolean" ? src.formatOnSave : base.formatOnSave,
  };
}

/** Field-by-field equality of two normalized prefs objects. */
export function sameSavePrefs(a, b) {
  return a.autosave === b.autosave && a.formatOnSave === b.formatOnSave;
}

/**
 * @param {{getItem: (k: string) => string|null}|null} storage
 * @returns {{autosave: boolean, formatOnSave: boolean}}
 */
export function loadSavePrefs(storage) {
  try {
    const raw = storage ? storage.getItem(STORAGE_KEY) : null;
    return normalizeSavePrefs(raw ? JSON.parse(raw) : null);
  } catch {
    return normalizeSavePrefs(null); // unreadable / not JSON — start from the defaults
  }
}

/**
 * Saves `prefs`. Equal to the defaults removes the key instead of
 * writing it, so nobody who never touched these toggles leaves
 * anything behind. Storage failures (quota, blocked) are swallowed —
 * same posture as layoutPrefs.saveLayout(), for the same reason: not
 * persisting a toggle across a reload isn't worth surfacing an error
 * over.
 *
 * @param {{setItem: Function, removeItem: Function}|null} storage
 * @param {object} prefs
 */
export function saveSavePrefs(storage, prefs) {
  if (!storage) return;
  try {
    const clean = normalizeSavePrefs(prefs);
    if (sameSavePrefs(clean, DEFAULT_SAVE_PREFS)) storage.removeItem(STORAGE_KEY);
    else storage.setItem(STORAGE_KEY, JSON.stringify(clean));
  } catch {
    // ignore — see above
  }
}
