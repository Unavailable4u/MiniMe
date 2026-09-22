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
  ["problems", "console", "terminal", "history"],
  "the bottom panel has the four planned tabs, in order"
);
assertEqual(DEFAULT_LAYOUT, { bottomOpen: false, bottomTab: "problems", previewOpen: false, source: "cloud" }, "defaults: panel and preview closed, Problems selected, cloud source");

// --- normalizeLayout --------------------------------------------------

assertEqual(normalizeLayout(undefined), DEFAULT_LAYOUT, "undefined normalizes to the defaults");
assertEqual(normalizeLayout(null), DEFAULT_LAYOUT, "null normalizes to the defaults");
assertEqual(normalizeLayout("open"), DEFAULT_LAYOUT, "a string normalizes to the defaults");
assertEqual(normalizeLayout([true, "history"]), DEFAULT_LAYOUT, "an array normalizes to the defaults");
assertEqual(normalizeLayout({}), DEFAULT_LAYOUT, "an empty object normalizes to the defaults");

assertEqual(
  normalizeLayout({ bottomOpen: true, bottomTab: "console", previewOpen: true, source: "local" }),
  { bottomOpen: true, bottomTab: "console", previewOpen: true, source: "local" },
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
  normalizeLayout({ bottomTab: "nope" }, { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "local" }),
  { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "local" },
  "with a fallback, invalid/missing fields take the fallback's values instead of the defaults"
);
assertEqual(
  normalizeLayout({ bottomOpen: false }, { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "local" }),
  { bottomOpen: false, bottomTab: "history", previewOpen: true, source: "local" },
  "...and valid fields still win over the fallback"
);
assertEqual(Object.isFrozen(DEFAULT_LAYOUT) && !Object.isFrozen(normalizeLayout(null)), true, "DEFAULT_LAYOUT is frozen, but normalizeLayout returns a fresh object callers may own");

assertEqual(sameLayout(normalizeLayout(null), DEFAULT_LAYOUT), true, "sameLayout: equal layouts");
assertEqual(sameLayout({ ...DEFAULT_LAYOUT, bottomTab: "history" }, DEFAULT_LAYOUT), false, "sameLayout: a different tab");
assertEqual(sameLayout({ ...DEFAULT_LAYOUT, previewOpen: true }, DEFAULT_LAYOUT), false, "sameLayout: a different flag");
assertEqual(sameLayout({ ...DEFAULT_LAYOUT, source: "local" }, DEFAULT_LAYOUT), false, "sameLayout: a different source");

// --- storage ----------------------------------------------------------

assertEqual(layoutStorageKey("ws-1"), "minime_build_editor_layout:ws-1", "the storage key carries the workspace id");
assertEqual(layoutStorageKey("ws-1") === layoutStorageKey("ws-2"), false, "two workspaces don't share a layout");

let store = fakeStorage();
saveLayout(store, "ws-1", { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "local" });
assertEqual(
  loadLayout(store, "ws-1"),
  { bottomOpen: true, bottomTab: "history", previewOpen: true, source: "local" },
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

// --- W3.1: source ------------------------------------------------------

assertEqual(normalizeLayout({ source: "local" }).source, "local", "a valid source passes through");
assertEqual(normalizeLayout({ source: "dropbox" }).source, "cloud", "an unknown source falls back to cloud");
assertEqual(normalizeLayout({ source: "local" }, { ...DEFAULT_LAYOUT, source: "cloud" }).source, "local", "a valid source still wins over the fallback");
assertEqual(normalizeLayout({}, { ...DEFAULT_LAYOUT, source: "local" }).source, "local", "a missing source takes the fallback's value");

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
