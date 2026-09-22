// frontend/app/lib/workbench/layoutPrefs.js — W2.3b (Build Workbench
// plan). The workbench's persisted layout: which bottom-panel tab is
// showing, whether the panel and the preview column are open, and the
// pixel bounds the two new splitters clamp to.
//
// Split out of the components for the same reason tabUtils.js is: the
// interesting parts are rules ("what does a garbage saved value turn
// into?", "how tall may the panel get in a container this size?"), and
// a rule is easier to keep right as a function with a test than as an
// effect. No imports, and nothing here touches `window` at load time —
// see fileTree.js's header; __tests__/layoutPrefs.test.mjs loads this
// file with no `imports` map, so an added `import` line fails it.
//
// What is persisted where (per workspace, in localStorage):
//   - the flags below            → ONE json key (layoutStorageKey)
//   - explorer / preview widths,
//     bottom panel height        → one number each, written by
//                                  useSplitter's own `storageKey`, same
//                                  as the explorer width since W2.3a
// Sizes stay with useSplitter (it already clamps and saves on mouseup);
// only the on/off/which-tab flags live in the editor store's `layout`,
// because those are the ones other parts of the workbench will want to
// change from elsewhere (search results opening a panel tab in W2.6,
// "Fix with AI" opening Console in W6.2, the preview toggle in W6.1).

/** Bottom panel tabs, in display order. Ids are what gets persisted. */
export const BOTTOM_TABS = [
  { id: "problems", label: "Problems" },
  { id: "console", label: "Console" },
  { id: "terminal", label: "Terminal" },
  { id: "history", label: "History" },
];

const BOTTOM_TAB_IDS = new Set(BOTTOM_TABS.map((t) => t.id));

// Closed by default: until the panel has real content (W2.6 History,
// W6.2 Console, W3.1 Terminal) an open-by-default empty panel would
// only take height away from the editor. The tab strip is still
// visible when closed, so it stays discoverable.
//
// `source` (W3.1): which FileProvider the Explorer/tabs/search/history
// are all currently pointed at — "cloud" (workspace_code_files) or
// "local" (a paired daemon folder). Persisted here rather than as its
// own storage key for the same reason the panel flags are: it's a
// sticky per-workspace UI preference, not buffer state, so it belongs
// wherever bottomOpen/previewOpen already live rather than a second
// read/write path doing the same job. Switching it is NOT a plain
// SET_LAYOUT, though — see editorStore.js's SWITCH_SOURCE, which resets
// tabs/buffers at the same time (a path under one source means nothing
// under the other).
export const LOCAL_SOURCE_IDS = new Set(["cloud", "local"]);

export const DEFAULT_LAYOUT = Object.freeze({
  bottomOpen: false,
  bottomTab: "problems",
  previewOpen: false,
  source: "cloud",
});

// Sizes (px). Defaults/minimums for the splitters; the maximums depend
// on the container, see the two *Max* functions below.
export const BOTTOM_PANEL_DEFAULT_HEIGHT = 208;
export const BOTTOM_PANEL_MIN_HEIGHT = 96;
export const PREVIEW_DEFAULT_WIDTH = 420;
export const PREVIEW_MIN_WIDTH = 240;

// What the editor column must keep, whatever the panels are dragged to.
export const EDITOR_MIN_WIDTH = 280;
export const MAIN_ROW_MIN_HEIGHT = 160; // tab strip + a few lines of editor
export const STATUS_BAR_HEIGHT = 24; // StatusBar's `h-6`

/**
 * Turns anything into a valid layout. Total: never throws, never
 * returns a partial object. Unknown keys are dropped and wrong types
 * fall back, for THAT key only — a saved value from an older/newer
 * build (say, a tab id that no longer exists) degrades to one default
 * instead of discarding the whole layout.
 *
 * `fallback` is what a wrong-typed field falls back TO: the defaults
 * when omitted (loading a saved value), or the current layout when
 * applying an update (SET_LAYOUT), where an invalid value should mean
 * "ignored", not "reset".
 *
 * @param {unknown} raw
 * @param {unknown} [fallback]
 * @returns {{bottomOpen: boolean, bottomTab: string, previewOpen: boolean}}
 */
export function normalizeLayout(raw, fallback) {
  const src = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const base = fallback === undefined ? DEFAULT_LAYOUT : normalizeLayout(fallback);
  return {
    bottomOpen: typeof src.bottomOpen === "boolean" ? src.bottomOpen : base.bottomOpen,
    bottomTab: BOTTOM_TAB_IDS.has(src.bottomTab) ? src.bottomTab : base.bottomTab,
    previewOpen: typeof src.previewOpen === "boolean" ? src.previewOpen : base.previewOpen,
    source: LOCAL_SOURCE_IDS.has(src.source) ? src.source : base.source,
  };
}

/** Field-by-field equality of two normalized layouts. */
export function sameLayout(a, b) {
  return (
    a.bottomOpen === b.bottomOpen &&
    a.bottomTab === b.bottomTab &&
    a.previewOpen === b.previewOpen &&
    a.source === b.source
  );
}

/** Per-workspace key, same `minime_build_editor_*:${id}` family as the splitters'. */
export function layoutStorageKey(workspaceId) {
  return `minime_build_editor_layout:${workspaceId}`;
}

/**
 * localStorage, or null where it isn't usable. Merely READING
 * `window.localStorage` throws a SecurityError when storage is blocked
 * (some private-browsing modes, cookies disabled), so the try/catch is
 * around the property access, not just around getItem.
 */
export function browserStorage() {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

/**
 * @param {{getItem: (k: string) => string|null}|null} storage
 * @param {string} workspaceId
 */
export function loadLayout(storage, workspaceId) {
  try {
    const raw = storage ? storage.getItem(layoutStorageKey(workspaceId)) : null;
    return normalizeLayout(raw ? JSON.parse(raw) : null);
  } catch {
    return normalizeLayout(null); // unreadable / not JSON — start from the defaults
  }
}

/**
 * Saves `layout`. A layout equal to the defaults removes the key
 * instead of writing it, so a workspace nobody customised leaves
 * nothing behind. Storage failures (quota, blocked) are swallowed: the
 * layout just won't survive a reload, which is not worth an error.
 *
 * @param {{setItem: Function, removeItem: Function}|null} storage
 * @param {string} workspaceId
 * @param {object} layout
 */
export function saveLayout(storage, workspaceId, layout) {
  if (!storage) return;
  try {
    const clean = normalizeLayout(layout);
    const key = layoutStorageKey(workspaceId);
    if (sameLayout(clean, DEFAULT_LAYOUT)) storage.removeItem(key);
    else storage.setItem(key, JSON.stringify(clean));
  } catch {
    // ignore — see above
  }
}

/**
 * Tallest the bottom panel may be dragged, given the workbench's
 * height: at most 70% of it, and never so tall that the main row
 * (tabs + editor) drops below MAIN_ROW_MIN_HEIGHT. Never below the
 * panel's own minimum, so the splitter's [min, max] range is always
 * valid even in a container too short for both. Before the container
 * has been measured (0/NaN) it returns a generous fixed bound rather
 * than clamping a saved height to nothing.
 */
export function bottomPanelMaxHeight(containerHeight) {
  if (!(containerHeight > 0)) return BOTTOM_PANEL_DEFAULT_HEIGHT * 3;
  const room = containerHeight - STATUS_BAR_HEIGHT - MAIN_ROW_MIN_HEIGHT;
  return Math.max(BOTTOM_PANEL_MIN_HEIGHT, Math.min(containerHeight * 0.7, room));
}

/**
 * Widest the preview column may be dragged: at most 60% of the
 * workbench's width, and never so wide that the editor column drops
 * below EDITOR_MIN_WIDTH next to the explorer. Same floor/unmeasured
 * rules as bottomPanelMaxHeight.
 */
export function previewMaxWidth(containerWidth, explorerWidth) {
  if (!(containerWidth > 0)) return PREVIEW_DEFAULT_WIDTH * 2;
  const room = containerWidth - (explorerWidth > 0 ? explorerWidth : 0) - EDITOR_MIN_WIDTH;
  return Math.max(PREVIEW_MIN_WIDTH, Math.min(containerWidth * 0.6, room));
}
