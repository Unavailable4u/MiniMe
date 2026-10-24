// W2.3b (Build Workbench plan) — tests for layoutPrefs.js.
//
// Loads the REAL lib/workbench/layoutPrefs.js through loadSource.mjs (no
// pasted copy). No `imports` are passed on purpose: layoutPrefs.js must
// stay dependency-free, and this load fails if someone adds an `import`.
//
// Run: node frontend/app/lib/workbench/__tests__/layoutPrefs.test.mjs
import { loadSource } from "./loadSource.mjs";

const {
  BOTTOM_TABS,
  SOURCE_IDS,
  DEFAULT_LAYOUT,
  normalizeLayout,
  sameLayout,
  layoutStorageKey,
  browserStorage,
  loadLayout,
  saveLayout,
  bottomPanelMaxHeight,
  previewMaxWidth,
  BOTTOM_PANEL_MIN_HEIGHT,
  BOTTOM_PANEL_DEFAULT_HEIGHT,
  PREVIEW_MIN_WIDTH,
  PREVIEW_DEFAULT_WIDTH,
  EDITOR_MIN_WIDTH,
  MAIN_ROW_MIN_HEIGHT,
  STATUS_BAR_HEIGHT,
} = loadSource("../layoutPrefs.js");

let failures = 0;
function assertEqual(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures++;
    console.error(`FAIL: ${msg}\n  expected: ${e}\n  actual:   ${a}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

// A minimal in-memory localStorage.
function fakeStorage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
    removeItem: (k) => {
      delete data[k];
    },
  };
}

// --- constants --------------------------------------------------------

assertEqual(
  BOTTOM_TABS.map((t) => t.id),
  ["problems", "console", "history"],
  "the bottom panel has three tabs, in order (the Terminal tab was removed in M2C B09)"
);
assertEqual(
  BOTTOM_TABS.some((t) => t.id === "terminal" || /terminal/i.test(t.label)),
  false,
  "no Terminal tab is offered any more"
);
assertEqual(DEFAULT_LAYOUT, { bottomOpen: false, bottomTab: "problems", previewOpen: false, source: "cloud" }, "defaults: panel and preview closed, Problems selected, cloud source");

// --- normalizeLayout --------------------------------------------------

assertEqual(normalizeLayout(undefined), DEFAULT_LAYOUT, "undefined normalizes to the defaults");
assertEqual(normalizeLayout(null), DEFAULT_LAYOUT, "null normalizes to the defaults");
assertEqual(normalizeLayout("open"), DEFAULT_LAYOUT, "a string normalizes to the defaults");
assertEqual(normalizeLayout([true, "history"]), DEFAULT_LAYOUT, "an array normalizes to the defaults");
assertEqual(normalizeLayout({}), DEFAULT_LAYOUT, "an empty object normalizes to the defaults");

assertEqual(
  normalizeLayout({ bottomOpen: true, bottomTab: "console", previewOpen: true, source: "cloud" }),
  { bottomOpen: true, bottomTab: "console", previewOpen: true, source: "cloud" },
  "a valid layout passes through unchanged"
);
assertEqual(
  normalizeLayout({ bottomOpen: "true", bottomTab: "search", previewOpen: 1, source: "nope" }),
  DEFAULT_LAYOUT,
  "wrong types, an unknown tab id and an unknown source each fall back to their default"
);
assertEqual(
  normalizeLayout({ bottomOpen: true, bottomTab: "nope", previewOpen: true }),
  { bottomOpen: true, bottomTab: "problems", previewOpen: true, source: "cloud" },
  "one bad field doesn't discard the good ones"
);
assertEqual(
  Object.keys(normalizeLayout({ bottomOpen: true, extra: 1, __proto__: { x: 1 } })).sort(),
  ["bottomOpen", "bottomTab", "previewOpen", "source"],
  "unknown keys are dropped"
);
assertEqual(
  normalizeLayout({ bottomTab: "nope" }, { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "cloud" }),
  { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "cloud" },
  "with a fallback, invalid/missing fields take the fallback's values instead of the defaults"
);
assertEqual(
  normalizeLayout({ bottomOpen: false }, { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "cloud" }),
  { bottomOpen: false, bottomTab: "history", previewOpen: true, source: "cloud" },
  "...and valid fields still win over the fallback"
);
assertEqual(Object.isFrozen(DEFAULT_LAYOUT) && !Object.isFrozen(normalizeLayout(null)), true, "DEFAULT_LAYOUT is frozen, but normalizeLayout returns a fresh object callers may own");

assertEqual(sameLayout(normalizeLayout(null), DEFAULT_LAYOUT), true, "sameLayout: equal layouts");
assertEqual(sameLayout({ ...DEFAULT_LAYOUT, bottomTab: "history" }, DEFAULT_LAYOUT), false, "sameLayout: a different tab");
assertEqual(sameLayout({ ...DEFAULT_LAYOUT, previewOpen: true }, DEFAULT_LAYOUT), false, "sameLayout: a different flag");
assertEqual(sameLayout({ ...DEFAULT_LAYOUT, source: "local" }, DEFAULT_LAYOUT), false, "sameLayout: still a plain field comparison (an un-normalized source differs)");

// --- storage ----------------------------------------------------------

assertEqual(layoutStorageKey("ws-1"), "minime_build_editor_layout:ws-1", "the storage key carries the workspace id");
assertEqual(layoutStorageKey("ws-1") === layoutStorageKey("ws-2"), false, "two workspaces don't share a layout");

let store = fakeStorage();
saveLayout(store, "ws-1", { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "cloud" });
assertEqual(
  loadLayout(store, "ws-1"),
  { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "cloud" },
  "a saved layout loads back identically"
);
assertEqual(loadLayout(store, "ws-2"), DEFAULT_LAYOUT, "another workspace still gets the defaults");

saveLayout(store, "ws-1", DEFAULT_LAYOUT);
assertEqual(Object.keys(store.data), [], "saving the defaults removes the key instead of writing it");

store = fakeStorage();
saveLayout(store, "ws-1", { bottomOpen: true, junk: "x" });
assertEqual(
  JSON.parse(store.data[layoutStorageKey("ws-1")]),
  { bottomOpen: true, bottomTab: "problems", previewOpen: false, source: "cloud" },
  "what's written is the normalized layout, never the caller's raw object"
);

store = fakeStorage({ [layoutStorageKey("ws-1")]: "{not json" });
assertEqual(loadLayout(store, "ws-1"), DEFAULT_LAYOUT, "an unparseable saved value loads as the defaults");
store = fakeStorage({ [layoutStorageKey("ws-1")]: JSON.stringify({ bottomOpen: true, bottomTab: "gone" }) });
assertEqual(
  loadLayout(store, "ws-1"),
  { bottomOpen: true, bottomTab: "problems", previewOpen: false, source: "cloud" },
  "a saved value naming a tab that no longer exists keeps the rest and defaults the tab"
);
store = fakeStorage({ [layoutStorageKey("ws-1")]: "null" });
assertEqual(loadLayout(store, "ws-1"), DEFAULT_LAYOUT, "a saved JSON null loads as the defaults");

assertEqual(loadLayout(null, "ws-1"), DEFAULT_LAYOUT, "no storage available: load returns the defaults");
saveLayout(null, "ws-1", { bottomOpen: true }); // must not throw
assertEqual(true, true, "no storage available: save is a silent no-op");

const throwing = {
  getItem: () => {
    throw new Error("blocked");
  },
  setItem: () => {
    throw new Error("quota");
  },
  removeItem: () => {
    throw new Error("blocked");
  },
};
assertEqual(loadLayout(throwing, "ws-1"), DEFAULT_LAYOUT, "storage that throws on read: load returns the defaults");
saveLayout(throwing, "ws-1", { bottomOpen: true }); // must not throw
saveLayout(throwing, "ws-1", DEFAULT_LAYOUT); // must not throw
assertEqual(true, true, "storage that throws on write/remove: save is swallowed");

assertEqual(browserStorage(), null, "browserStorage() is null outside a browser (no `window` under node)");

// --- M2C B09: a layout saved while the Terminal tab still existed ---------
//
// Browsers that last had the Terminal tab active hold `bottomTab:
// "terminal"` in localStorage. That id is no longer a tab, so it has to
// degrade to a valid one (never crash, never leave the panel blank), and
// the rest of the saved layout has to survive.

assertEqual(normalizeLayout({ bottomTab: "terminal" }).bottomTab, "problems", "a stale \"terminal\" tab id normalizes to the default tab");
assertEqual(
  normalizeLayout({ bottomOpen: true, bottomTab: "terminal", previewOpen: true, source: "cloud" }),
  { bottomOpen: true, bottomTab: "problems", previewOpen: true, source: "cloud" },
  "...and the other saved fields are kept"
);
assertEqual(
  normalizeLayout({ bottomTab: "terminal" }, { bottomOpen: true, bottomTab: "history", previewOpen: false, source: "cloud" }).bottomTab,
  "history",
  "applying an update that names \"terminal\" is ignored: the current tab stays (SET_LAYOUT shape)"
);
assertEqual(
  BOTTOM_TABS.some((t) => t.id === normalizeLayout({ bottomTab: "terminal" }).bottomTab),
  true,
  "the fallback is always one of BOTTOM_TABS, so the panel never has an unknown active tab"
);

store = fakeStorage({ [layoutStorageKey("ws-1")]: JSON.stringify({ bottomOpen: true, bottomTab: "terminal", previewOpen: false, source: "cloud" }) });
assertEqual(
  loadLayout(store, "ws-1"),
  { bottomOpen: true, bottomTab: "problems", previewOpen: false, source: "cloud" },
  "loadLayout: a saved \"terminal\" tab loads as Problems with the panel still open"
);
// The workbench re-saves whatever it loaded on mount (EditorWorkbench's
// persist effect), which must overwrite the stale id rather than keep it.
saveLayout(store, "ws-1", loadLayout(store, "ws-1"));
assertEqual(
  JSON.parse(store.data[layoutStorageKey("ws-1")]).bottomTab,
  "problems",
  "re-saving the loaded layout replaces the stale \"terminal\" value in storage"
);
store = fakeStorage({ [layoutStorageKey("ws-1")]: JSON.stringify({ bottomTab: "terminal" }) });
saveLayout(store, "ws-1", loadLayout(store, "ws-1"));
assertEqual(Object.keys(store.data), [], "a stale \"terminal\" on an otherwise default layout is cleaned up (saved as defaults, key removed)");

// --- M2C B10: Local folder mode removed — `source` is cloud-only --------
//
// The Explorer's Project files / Local folder switch is gone, so "cloud"
// is the only source the editor knows. A layout saved by an older build
// can still hold `source: "local"` in localStorage; it must restore as
// "cloud" — never throw, never leave the workbench without a provider —
// and the rest of the saved layout must survive.

assertEqual([...SOURCE_IDS], ["cloud"], "\"cloud\" is the only valid source id");
assertEqual(normalizeLayout({ source: "cloud" }).source, "cloud", "\"cloud\" passes through");
assertEqual(normalizeLayout({ source: "local" }).source, "cloud", "a saved \"local\" source normalizes to \"cloud\"");
assertEqual(normalizeLayout({ source: "dropbox" }).source, "cloud", "an unknown source falls back to cloud");
assertEqual(normalizeLayout({}).source, "cloud", "a missing source is cloud");
assertEqual(
  normalizeLayout({ bottomOpen: true, bottomTab: "history", previewOpen: true, source: "local" }),
  { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "cloud" },
  "...and the other saved fields are kept"
);
assertEqual(
  normalizeLayout({ source: "local" }, { ...DEFAULT_LAYOUT, source: "cloud" }).source,
  "cloud",
  "applying an update that names \"local\" leaves the source on cloud (SET_LAYOUT shape)"
);
assertEqual(
  normalizeLayout({}, { ...DEFAULT_LAYOUT, source: "local" }).source,
  "cloud",
  "a fallback that itself holds \"local\" never leaks it: still cloud"
);
assertEqual(
  normalizeLayout({ source: "local" }, { ...DEFAULT_LAYOUT, source: "local" }).source,
  "cloud",
  "\"local\" in both the update and the fallback still ends on cloud"
);

store = fakeStorage({
  [layoutStorageKey("ws-1")]: JSON.stringify({ bottomOpen: true, bottomTab: "history", previewOpen: true, source: "local" }),
});
assertEqual(
  loadLayout(store, "ws-1"),
  { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "cloud" },
  "loadLayout: a saved \"local\" source loads as cloud, with the rest of the layout intact"
);
// The workbench re-saves whatever it loaded on mount (EditorWorkbench's
// persist effect), which must overwrite the stale value rather than keep it.
saveLayout(store, "ws-1", loadLayout(store, "ws-1"));
assertEqual(
  JSON.parse(store.data[layoutStorageKey("ws-1")]).source,
  "cloud",
  "re-saving the loaded layout replaces the stale \"local\" value in storage"
);
store = fakeStorage({ [layoutStorageKey("ws-1")]: JSON.stringify({ source: "local" }) });
assertEqual(loadLayout(store, "ws-1"), DEFAULT_LAYOUT, "loadLayout: a saved layout that is only \"local\" loads as the defaults");
saveLayout(store, "ws-1", loadLayout(store, "ws-1"));
assertEqual(Object.keys(store.data), [], "a stale \"local\" on an otherwise default layout is cleaned up (saved as defaults, key removed)");
saveLayout(store, "ws-2", { bottomOpen: true, source: "local" });
assertEqual(
  JSON.parse(store.data[layoutStorageKey("ws-2")]),
  { bottomOpen: true, bottomTab: "problems", previewOpen: false, source: "cloud" },
  "saveLayout never writes \"local\" back, even when asked to"
);

// --- size bounds ------------------------------------------------------

// A comfortable 800px-tall workbench: 70% (560) vs. room (800 - 24 - 160 = 616) → 560.
assertEqual(bottomPanelMaxHeight(800), 560, "bottom panel: capped at 70% of a tall container");
// A 400px-tall one: 70% = 280, room = 400 - 24 - 160 = 216 → the room limit wins.
assertEqual(bottomPanelMaxHeight(400), 216, "bottom panel: the main row keeps its minimum height in a short container");
assertEqual(400 - STATUS_BAR_HEIGHT - bottomPanelMaxHeight(400) >= MAIN_ROW_MIN_HEIGHT, true, "...checked against the constants, not just the number above");
assertEqual(bottomPanelMaxHeight(150), BOTTOM_PANEL_MIN_HEIGHT, "bottom panel: a container too short for both never yields max < min");
assertEqual(bottomPanelMaxHeight(0), BOTTOM_PANEL_DEFAULT_HEIGHT * 3, "bottom panel: unmeasured (0) container → generous fixed bound");
assertEqual(bottomPanelMaxHeight(NaN), BOTTOM_PANEL_DEFAULT_HEIGHT * 3, "bottom panel: NaN container → generous fixed bound");
assertEqual(bottomPanelMaxHeight(undefined), BOTTOM_PANEL_DEFAULT_HEIGHT * 3, "bottom panel: undefined container → generous fixed bound");

// 1400 wide, explorer 240: 60% = 840, room = 1400 - 240 - 280 = 880 → 840.
assertEqual(previewMaxWidth(1400, 240), 840, "preview: capped at 60% of a wide container");
// 900 wide, explorer 240: 60% = 540, room = 900 - 240 - 280 = 380 → 380.
assertEqual(previewMaxWidth(900, 240), 380, "preview: the editor keeps its minimum width next to the explorer");
assertEqual(900 - 240 - previewMaxWidth(900, 240) >= EDITOR_MIN_WIDTH, true, "...checked against the constant");
assertEqual(previewMaxWidth(500, 240), PREVIEW_MIN_WIDTH, "preview: a container too narrow for both never yields max < min");
assertEqual(previewMaxWidth(1400, 480) < previewMaxWidth(1400, 240), true, "preview: a wider explorer leaves less room for the preview");
assertEqual(previewMaxWidth(0, 240), PREVIEW_DEFAULT_WIDTH * 2, "preview: unmeasured container → generous fixed bound");
assertEqual(previewMaxWidth(1400, undefined), 840, "preview: an unknown explorer width counts as 0");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
