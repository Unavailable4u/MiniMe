// W2.2 (Build Workbench plan) — reducer test for editorStore.js;
// extended in W2.3a for the tab-strip actions, in W2.3b for SET_LAYOUT, in
// W2.4 for RENAME_PATHS and in W2.5 for the save-conflict resolutions.
//
// Runs the REAL reducer. Until W2.3a this file kept a byte-for-byte pasted
// copy of editorReducer() (editorStore.js pulls in `react`, which plain
// `node` couldn't resolve without node_modules) and said the copy had to be
// kept in sync by hand — which is how a test ends up green against code
// that has moved on. loadSource.mjs now reads editorStore.js itself and
// satisfies its two imports from the map below: a stub for `react` (only
// its top-level createContext() call runs at load time; the provider and
// hook aren't exercised here) and the real tabUtils.js / layoutPrefs.js /
// fileTree.js.
//
// Run: node frontend/app/lib/workbench/__tests__/editorStore.test.mjs
import { loadSource } from "./loadSource.mjs";

const reactStub = {
  createContext: () => ({}),
  createElement: () => null,
  useContext: () => null,
  useMemo: (fn) => fn(),
  useReducer: () => [],
};
const tabUtils = loadSource("../tabUtils.js");
const layoutPrefs = loadSource("../layoutPrefs.js");
const fileTree = loadSource("../fileTree.js");
const { editorReducer } = loadSource("../editorStore.js", {
  imports: { react: reactStub, "./tabUtils": tabUtils, "./layoutPrefs": layoutPrefs, "./fileTree": fileTree },
});

const initialState = {
  tabs: [],
  activePath: null,
  buffers: {},
  layout: {},
  proposals: [],
};

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

// --- SET_ACTIVE_PATH -------------------------------------------------

let state = editorReducer(initialState, { type: "SET_ACTIVE_PATH", path: "src/App.jsx" });
assertEqual(state.activePath, "src/App.jsx", "opening a file sets activePath");
assertEqual(state.tabs, ["src/App.jsx"], "opening a new file adds it to tabs");

state = editorReducer(state, { type: "SET_ACTIVE_PATH", path: "src/App.jsx" });
assertEqual(state.tabs, ["src/App.jsx"], "re-opening the same file doesn't duplicate its tab");

state = editorReducer(state, { type: "SET_ACTIVE_PATH", path: null });
assertEqual(state.activePath, null, "SET_ACTIVE_PATH(null) clears the active path without touching tabs");
assertEqual(state.tabs, ["src/App.jsx"], "clearing activePath leaves tabs alone");

// --- FILE_LOADED / EDIT_BUFFER / dirty tracking -----------------------

state = editorReducer(initialState, { type: "SET_ACTIVE_PATH", path: "a.py" });
state = editorReducer(state, {
  type: "FILE_LOADED",
  path: "a.py",
  file: { content: "print(1)", language: "python", version: 3, updated_at: "2026-01-01T00:00:00Z" },
});
assertEqual(state.buffers["a.py"].dirty, false, "a freshly loaded file is not dirty");
assertEqual(state.buffers["a.py"].saved, "print(1)", "FILE_LOADED sets saved to the server content");
assertEqual(state.buffers["a.py"].edited, "print(1)", "FILE_LOADED sets edited equal to saved");

state = editorReducer(state, { type: "EDIT_BUFFER", path: "a.py", content: "print(2)" });
assertEqual(state.buffers["a.py"].dirty, true, "editing away from saved content marks the buffer dirty");
assertEqual(state.buffers["a.py"].saved, "print(1)", "editing never touches the saved copy");

state = editorReducer(state, { type: "EDIT_BUFFER", path: "a.py", content: "print(1)" });
assertEqual(state.buffers["a.py"].dirty, false, "editing back to the saved content clears dirty");

state = editorReducer(state, { type: "EDIT_BUFFER", path: "never-opened.py", content: "x" });
assertEqual(state.buffers["never-opened.py"], undefined, "EDIT_BUFFER on a path with no buffer is a no-op");

// --- SAVE_SUCCESS clears dirty and stale ------------------------------

state = editorReducer(state, { type: "EDIT_BUFFER", path: "a.py", content: "print(3)" });
state = editorReducer(state, { type: "MARK_STALE", path: "a.py" });
assertEqual(state.buffers["a.py"].stale, true, "MARK_STALE flags the buffer");

state = editorReducer(state, {
  type: "SAVE_SUCCESS",
  path: "a.py",
  file: { content: "print(3)", language: "python", version: 4, updated_at: "2026-01-02T00:00:00Z" },
});
assertEqual(state.buffers["a.py"].dirty, false, "SAVE_SUCCESS clears dirty");
assertEqual(state.buffers["a.py"].stale, false, "SAVE_SUCCESS also clears any stale flag");
assertEqual(state.buffers["a.py"].version, 4, "SAVE_SUCCESS adopts the server's returned version");

// --- MARK_STALE / CLEAR_STALE / re-activating clears stale ------------

state = editorReducer(state, { type: "MARK_STALE", path: "a.py" });
assertEqual(state.buffers["a.py"].stale, true, "MARK_STALE sets stale on an open buffer");

let afterClear = editorReducer(state, { type: "CLEAR_STALE", path: "a.py" });
assertEqual(afterClear.buffers["a.py"].stale, false, "CLEAR_STALE (Keep mine) unsets stale");

let afterReopen = editorReducer(state, { type: "SET_ACTIVE_PATH", path: "a.py" });
assertEqual(afterReopen.buffers["a.py"].stale, false, "re-selecting a stale file's tab also clears stale (Reload's path)");

assertEqual(
  editorReducer(state, { type: "MARK_STALE", path: "never-opened.py" }).buffers["never-opened.py"],
  undefined,
  "MARK_STALE on a path with no buffer is a no-op"
);

// --- CLOSE_TAB ---------------------------------------------------------

state = editorReducer(initialState, { type: "SET_ACTIVE_PATH", path: "b.py" });
state = editorReducer(state, {
  type: "FILE_LOADED",
  path: "b.py",
  file: { content: "x = 1", language: "python", version: 1, updated_at: null },
});
state = editorReducer(state, { type: "CLOSE_TAB", path: "b.py" });
assertEqual(state.tabs, [], "CLOSE_TAB removes the path from tabs");
assertEqual(state.buffers["b.py"], undefined, "CLOSE_TAB removes the path's buffer");
assertEqual(state.activePath, null, "CLOSE_TAB clears activePath when it was the closed file");

assertEqual(
  editorReducer(initialState, { type: "CLOSE_TAB", path: "never-opened.py" }),
  initialState,
  "CLOSE_TAB on a path that was never open is a no-op"
);

// --- unknown action ------------------------------------------------

assertEqual(
  editorReducer(initialState, { type: "SOMETHING_UNKNOWN" }),
  initialState,
  "an unrecognized action type returns state unchanged"
);

// --- W2.3a: ACTIVATE_TAB -----------------------------------------------

function openWith(paths, active = paths[paths.length - 1]) {
  let st = initialState;
  for (const path of paths) {
    st = editorReducer(st, { type: "SET_ACTIVE_PATH", path });
    st = editorReducer(st, {
      type: "FILE_LOADED",
      path,
      file: { content: `// ${path}`, language: "javascript", version: 1, updated_at: null },
    });
  }
  return editorReducer(st, { type: "ACTIVATE_TAB", path: active });
}

state = openWith(["a.js", "b.js", "c.js"], "a.js");
assertEqual(state.activePath, "a.js", "ACTIVATE_TAB switches to an already-open tab");
assertEqual(state.tabs, ["a.js", "b.js", "c.js"], "ACTIVATE_TAB never reorders tabs");

assertEqual(
  editorReducer(state, { type: "ACTIVATE_TAB", path: "nope.js" }),
  state,
  "ACTIVATE_TAB on a path that isn't open is ignored, not invented into a tab"
);
assertEqual(
  editorReducer(state, { type: "ACTIVATE_TAB", path: "a.js" }),
  state,
  "ACTIVATE_TAB on the already-active tab returns the same state"
);

state = editorReducer(openWith(["a.js", "b.js"], "a.js"), { type: "EDIT_BUFFER", path: "a.js", content: "changed" });
state = editorReducer(state, { type: "MARK_STALE", path: "a.js" });
state = editorReducer(state, { type: "ACTIVATE_TAB", path: "b.js" });
state = editorReducer(state, { type: "ACTIVATE_TAB", path: "a.js" });
assertEqual(state.buffers["a.js"].stale, true, "switching away from a stale tab and back does NOT dismiss its warning");
assertEqual(
  editorReducer(state, { type: "SET_ACTIVE_PATH", path: "a.js" }).buffers["a.js"].stale,
  false,
  "...whereas SET_ACTIVE_PATH (opening it from the explorer) still does, as before"
);

// --- W2.3a: which tab becomes active when the active one closes --------

state = openWith(["a.js", "b.js", "c.js"], "b.js");
let closed = editorReducer(state, { type: "CLOSE_TAB", path: "b.js" });
assertEqual(closed.activePath, "c.js", "closing the active tab activates its right neighbour");
assertEqual(closed.tabs, ["a.js", "c.js"], "...and removes it from the strip");

state = openWith(["a.js", "b.js", "c.js"], "c.js");
closed = editorReducer(state, { type: "CLOSE_TAB", path: "c.js" });
assertEqual(closed.activePath, "b.js", "closing the LAST active tab activates its left neighbour");

state = openWith(["a.js", "b.js", "c.js"], "a.js");
closed = editorReducer(state, { type: "CLOSE_TAB", path: "c.js" });
assertEqual(closed.activePath, "a.js", "closing a background tab leaves the active tab alone");
assertEqual(closed.buffers["c.js"], undefined, "...and still drops that tab's buffer");

// --- W2.3a: CLOSE_TABS ---------------------------------------------------------

state = openWith(["a.js", "b.js", "c.js", "d.js"], "b.js");
closed = editorReducer(state, { type: "CLOSE_TABS", paths: ["a.js", "b.js"] });
assertEqual(closed.tabs, ["c.js", "d.js"], "CLOSE_TABS removes every listed tab");
assertEqual(Object.keys(closed.buffers), ["c.js", "d.js"], "...and every listed buffer");
assertEqual(closed.activePath, "c.js", "when the active tab is among them, the nearest survivor to its right takes over");

closed = editorReducer(state, { type: "CLOSE_TABS", paths: ["a.js", "c.js", "d.js"] });
assertEqual(closed.activePath, "b.js", "'Close others' from b: b stays active");
assertEqual(closed.tabs, ["b.js"], "'Close others' leaves just the one tab");

closed = editorReducer(state, { type: "CLOSE_TABS", paths: [...state.tabs] });
assertEqual([closed.tabs, closed.activePath, closed.buffers], [[], null, {}], "'Close all' leaves an empty workbench");

assertEqual(
  editorReducer(state, { type: "CLOSE_TABS", paths: ["never-open.js"] }),
  state,
  "CLOSE_TABS naming only tabs that aren't open is a no-op"
);
assertEqual(editorReducer(state, { type: "CLOSE_TABS", paths: [] }), state, "CLOSE_TABS with no paths is a no-op");

state = editorReducer(openWith(["a.js", "b.js"], "a.js"), { type: "SET_ACTIVE_PATH", path: "loading.js" });
closed = editorReducer(state, { type: "CLOSE_TABS", paths: ["loading.js"] });
assertEqual(closed.tabs, ["a.js", "b.js"], "a tab whose file is still loading (tab but no buffer) can be closed");
assertEqual(closed.activePath, "b.js", "...and the active tab moves to its left neighbour (nothing to its right)");

// --- W2.3a: SAVE_SUCCESS keepEdited -------------------------------------

state = openWith(["a.js"]);
state = editorReducer(state, { type: "EDIT_BUFFER", path: "a.js", content: "v2" });
// the person keeps typing while the save of "v2" is in flight
state = editorReducer(state, { type: "EDIT_BUFFER", path: "a.js", content: "v2 and more" });
let afterSave = editorReducer(state, {
  type: "SAVE_SUCCESS",
  path: "a.js",
  file: { content: "v2", language: "javascript", version: 2, updated_at: "2026-01-03T00:00:00Z" },
  keepEdited: true,
});
assertEqual(afterSave.buffers["a.js"].edited, "v2 and more", "keepEdited: keystrokes typed during the save survive it");
assertEqual(afterSave.buffers["a.js"].saved, "v2", "keepEdited: `saved` records what the server confirmed");
assertEqual(afterSave.buffers["a.js"].dirty, true, "keepEdited: the buffer is still dirty, because edited no longer matches saved");
assertEqual(afterSave.buffers["a.js"].version, 2, "keepEdited: the server's new version is still adopted");

afterSave = editorReducer(state, {
  type: "SAVE_SUCCESS",
  path: "a.js",
  file: { content: "v2 and more", language: "javascript", version: 2, updated_at: null },
});
assertEqual(afterSave.buffers["a.js"].dirty, false, "without keepEdited, a save is the plain 'saved and clean' it always was");
assertEqual(afterSave.buffers["a.js"].edited, "v2 and more", "...with edited swapped to the server's copy");

afterSave = editorReducer(initialState, {
  type: "SAVE_SUCCESS",
  path: "x.js",
  file: { content: "new", version: 1, updated_at: null },
  keepEdited: true,
});
assertEqual(
  [afterSave.buffers["x.js"].edited, afterSave.buffers["x.js"].dirty],
  ["new", false],
  "keepEdited with no existing buffer (tab closed mid-save) falls back to the server's copy, clean"
);

// --- RENAME_PATHS (W2.4) -----------------------------------------------

const buf = (over = {}) => ({ saved: "x", edited: "x", dirty: false, stale: false, version: 3, language: "javascript", ...over });
const openState = {
  ...initialState,
  tabs: ["src/App.jsx", "src/lib/util.js", "README.md"],
  activePath: "src/lib/util.js",
  buffers: {
    "src/App.jsx": buf({ version: 3 }),
    "src/lib/util.js": buf({ version: 5, saved: "a", edited: "a + unsaved", dirty: true }),
    "README.md": buf({ version: 1 }),
  },
};

let ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "src/App.jsx", to: "src/Main.jsx" }] });
assertEqual(ren.tabs, ["src/Main.jsx", "src/lib/util.js", "README.md"], "renaming a file re-paths its tab in place (order kept)");
assertEqual(ren.activePath, "src/lib/util.js", "...and leaves the active tab alone when it wasn't the renamed one");
assertEqual(Object.keys(ren.buffers).sort(), ["README.md", "src/Main.jsx", "src/lib/util.js"], "the buffer moves to the new path; none is left behind at the old one");

ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "src/lib/util.js", to: "src/lib/helpers.js" }] });
assertEqual(ren.activePath, "src/lib/helpers.js", "renaming the ACTIVE file keeps it active under its new path");
assertEqual(ren.buffers["src/lib/helpers.js"].edited, "a + unsaved", "unsaved edits travel with a rename");
assertEqual(ren.buffers["src/lib/helpers.js"].dirty, true, "...and it is still dirty");
assertEqual(ren.buffers["src/lib/helpers.js"].saved, "a", "...with the same saved baseline");

ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "src", to: "app" }] });
assertEqual(ren.tabs, ["app/App.jsx", "app/lib/util.js", "README.md"], "renaming a FOLDER re-paths every open tab under it");
assertEqual(ren.activePath, "app/lib/util.js", "...including the active one");
assertEqual(Object.keys(ren.buffers).sort(), ["README.md", "app/App.jsx", "app/lib/util.js"], "...and their buffers");

ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "README.md", to: "docs/README.md" }, { from: "src/App.jsx", to: "App.jsx" }] });
assertEqual(ren.tabs, ["App.jsx", "src/lib/util.js", "docs/README.md"], "several renames in one action (a multi-item drop) all apply");

ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "srcx", to: "y" }, { from: "other/a.js", to: "b.js" }] });
assertEqual(ren === openState, true, "renames that touch no open tab return the SAME state object");
assertEqual(editorReducer(openState, { type: "RENAME_PATHS", renames: [] }) === openState, true, "an empty rename list is a no-op");
assertEqual(editorReducer(openState, { type: "RENAME_PATHS" }) === openState, true, "a missing rename list is a no-op");
assertEqual(editorReducer(initialState, { type: "RENAME_PATHS", renames: [{ from: "a", to: "b" }] }) === initialState, true, "nothing open: no-op");

ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "src/App.jsx", to: "src/Main.jsx" }], versions: { "src/Main.jsx": 4 } });
assertEqual(ren.buffers["src/Main.jsx"].version, 4, "a buffer exactly one version behind the move adopts the new version");
ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "src/App.jsx", to: "src/Main.jsx" }], versions: { "src/Main.jsx": 9 } });
assertEqual(ren.buffers["src/Main.jsx"].version, 3, "a buffer further behind keeps its own version, so the next sync sees the server is ahead");
ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "src/App.jsx", to: "src/Main.jsx" }] });
assertEqual(ren.buffers["src/Main.jsx"].version, 3, "no versions given: buffer versions are untouched");
ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "src", to: "app" }], versions: { "app/App.jsx": 4, "app/lib/util.js": 6 } });
assertEqual([ren.buffers["app/App.jsx"].version, ren.buffers["app/lib/util.js"].version], [4, 6], "a folder move takes each file's own new version");
ren = editorReducer(openState, { type: "RENAME_PATHS", renames: [{ from: "src/App.jsx", to: "src/Main.jsx" }], versions: { "README.md": 2 } });
assertEqual(ren.buffers["README.md"].version, 1, "a version for a path that wasn't renamed is ignored");

const loadingTab = { ...initialState, tabs: ["a.js"], activePath: "a.js", buffers: {} };
ren = editorReducer(loadingTab, { type: "RENAME_PATHS", renames: [{ from: "a.js", to: "b.js" }] });
assertEqual([ren.tabs, ren.activePath, ren.buffers], [["b.js"], "b.js", {}], "a tab whose file hasn't loaded yet is re-pathed too");

ren = editorReducer({ ...openState, tabs: ["a.js", "b.js"], activePath: "a.js", buffers: { "a.js": buf(), "b.js": buf() } }, { type: "RENAME_PATHS", renames: [{ from: "a.js", to: "b.js" }] });
assertEqual(ren.tabs, ["b.js"], "two tabs that end up on one path collapse to a single tab");

// --- SET_LAYOUT (W2.3b) ----------------------------------------------

const defaultLayout = { bottomOpen: false, bottomTab: "problems", previewOpen: false };
const withLayout = { ...initialState, layout: { ...defaultLayout } };

let laid = editorReducer(withLayout, { type: "SET_LAYOUT", layout: { bottomOpen: true } });
assertEqual(laid.layout, { bottomOpen: true, bottomTab: "problems", previewOpen: false }, "SET_LAYOUT merges a partial over the current layout");

laid = editorReducer(laid, { type: "SET_LAYOUT", layout: { bottomTab: "history", previewOpen: true } });
assertEqual(laid.layout, { bottomOpen: true, bottomTab: "history", previewOpen: true }, "SET_LAYOUT can change several fields at once, leaving the rest alone");

const before = laid;
assertEqual(
  editorReducer(before, { type: "SET_LAYOUT", layout: { bottomOpen: true, bottomTab: "history" } }) === before,
  true,
  "SET_LAYOUT that changes nothing returns the SAME state object (no re-render, no persist)"
);

laid = editorReducer(before, { type: "SET_LAYOUT", layout: { bottomTab: "nope", previewOpen: "yes" } });
assertEqual(laid.layout, before.layout, "SET_LAYOUT re-validates: an unknown tab id and a non-boolean are ignored, the current values stay");
assertEqual(laid === before, true, "...and an update made only of invalid fields is a no-op (same state object)");

laid = editorReducer(before, { type: "SET_LAYOUT", layout: { bottomTab: "nope", bottomOpen: false } });
assertEqual(
  laid.layout,
  { bottomOpen: false, bottomTab: "history", previewOpen: true },
  "...while the valid fields in the same update still apply"
);

laid = editorReducer(before, { type: "SET_LAYOUT", layout: { somethingElse: 1 } });
assertEqual(Object.keys(laid.layout).sort(), ["bottomOpen", "bottomTab", "previewOpen"], "SET_LAYOUT drops keys that aren't layout fields");

laid = editorReducer(initialState, { type: "SET_LAYOUT", layout: { previewOpen: true } });
assertEqual(laid.layout.previewOpen, true, "SET_LAYOUT works from an empty `layout: {}` (fields default, not undefined)");
assertEqual(laid.layout.bottomTab, "problems", "...and the missing fields are filled from the defaults");

const opened = editorReducer(withLayout, { type: "SET_ACTIVE_PATH", path: "a.js" });
assertEqual(opened.layout, withLayout.layout, "unrelated actions leave layout untouched");

// --- W2.5: resolving a save conflict (409) ---------------------------------
//
// EditorWorkbench.jsx adds NO reducer action for these: "Reload theirs" is
// FILE_LOADED with the 409 body's `current`, and "Keep mine" is SAVE_SUCCESS
// with keepEdited over that same body. These pin down that the two existing
// actions really give the plan's Done-when — the W1.1 conflict is
// recoverable without losing either side.
{
  // I opened a.js at v3, then typed. Meanwhile someone else saved v5, so my Save (base_version 3) got a 409 whose body is v5.
  let st = editorReducer(initialState, { type: "SET_ACTIVE_PATH", path: "a.js" });
  st = editorReducer(st, {
    type: "FILE_LOADED",
    path: "a.js",
    file: { content: "base\n", version: 3, language: "javascript", updated_at: "t3" },
  });
  st = editorReducer(st, { type: "EDIT_BUFFER", path: "a.js", content: "mine\n" });
  const theirs = { content: "theirs\n", version: 5, language: "javascript", updated_at: "t5", updated_by: "someone" };

  // Keep mine
  const kept = editorReducer(st, { type: "SAVE_SUCCESS", path: "a.js", file: theirs, keepEdited: true });
  const k = kept.buffers["a.js"];
  assertEqual(k.edited, "mine\n", "Keep mine: the person's text is untouched — nothing of theirs is lost");
  assertEqual(k.saved, "theirs\n", "Keep mine: `saved` becomes the server's current content");
  assertEqual(k.version, 5, "Keep mine: adopts the server's version, so the next Save's base_version matches and goes through");
  assertEqual(k.dirty, true, "Keep mine: still dirty — the next Save is the deliberate overwrite");
  assertEqual(k.stale, false, "Keep mine: clears any stale flag");

  // ...and if their text and mine turn out identical, there is nothing left to save.
  let same = editorReducer(st, { type: "EDIT_BUFFER", path: "a.js", content: "theirs\n" });
  same = editorReducer(same, { type: "SAVE_SUCCESS", path: "a.js", file: theirs, keepEdited: true });
  assertEqual(same.buffers["a.js"].dirty, false, "Keep mine with text equal to the server's isn't dirty");

  // Reload theirs
  const reloaded = editorReducer(st, { type: "FILE_LOADED", path: "a.js", file: theirs });
  const r = reloaded.buffers["a.js"];
  assertEqual([r.edited, r.saved, r.version, r.dirty], ["theirs\n", "theirs\n", 5, false], "Reload theirs: buffer becomes the server's file, clean, at its version");

  // A file deleted on the server comes back as version 0 / empty; Keep mine then targets base_version 0, which the server treats as "create it".
  const gone = { content: "", version: 0, language: null, updated_at: null, updated_by: null };
  const recreate = editorReducer(st, { type: "SAVE_SUCCESS", path: "a.js", file: gone, keepEdited: true });
  assertEqual(
    [recreate.buffers["a.js"].edited, recreate.buffers["a.js"].version, recreate.buffers["a.js"].dirty],
    ["mine\n", 0, true],
    "Keep mine over a deleted file: my text stays, version 0, dirty — the next Save re-creates the file"
  );
  assertEqual(recreate.buffers["a.js"].language, "javascript", "...and the buffer keeps its own language when the empty file has none");

  // Neither resolution touches the tab strip.
  assertEqual([kept.tabs, kept.activePath], [st.tabs, st.activePath], "resolving a conflict doesn't change tabs or the active file");
}

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
