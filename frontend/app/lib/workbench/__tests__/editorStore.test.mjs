// W2.2 (Build Workbench plan) — reducer test for editorStore.js;
// extended in W2.3a for the tab-strip actions.
//
// Runs the REAL reducer. Until W2.3a this file kept a byte-for-byte pasted
// copy of editorReducer() (editorStore.js pulls in `react`, which plain
// `node` couldn't resolve without node_modules) and said the copy had to be
// kept in sync by hand — which is how a test ends up green against code
// that has moved on. loadSource.mjs now reads editorStore.js itself and
// satisfies its two imports from the map below: a stub for `react` (only
// its top-level createContext() call runs at load time; the provider and
// hook aren't exercised here) and the real tabUtils.js.
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
const { editorReducer } = loadSource("../editorStore.js", {
  imports: { react: reactStub, "./tabUtils": tabUtils },
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

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
