// W2.2 (Build Workbench plan) — reducer test for editorStore.js.
//
// No JS test runner (jest/vitest) exists in this repo yet (frontend/package.json
// has zero test deps, no jest.config/vitest.config anywhere), and
// editorStore.js's `EditorStoreProvider`/`useEditorStore` pull in
// `react`, which a dependency-free `node` invocation can't resolve
// without `node_modules` present — exactly the situation
// components/__tests__/wiringGraph.linkFilter.test.mjs's own header
// comment describes for the same reason (JSX there; a `react` import
// here). Same fix: `editorReducer` is kept byte-for-byte in sync below
// so this can run with plain `node` in CI with no install step. If a
// real JS test runner is ever added, this should be ported to import
// the actual reducer and this file deleted.
//
// Run: node frontend/app/lib/workbench/__tests__/editorStore.test.mjs

const initialState = {
  tabs: [],
  activePath: null,
  buffers: {},
  layout: {},
  proposals: [],
};

// Kept byte-for-byte in sync with editorReducer() in ../editorStore.js.
function editorReducer(state, action) {
  switch (action.type) {
    case "SET_ACTIVE_PATH": {
      const { path } = action;
      if (path == null) {
        return { ...state, activePath: null };
      }
      const tabs = state.tabs.includes(path) ? state.tabs : [...state.tabs, path];
      const existing = state.buffers[path];
      const buffers = existing && existing.stale
        ? { ...state.buffers, [path]: { ...existing, stale: false } }
        : state.buffers;
      return { ...state, activePath: path, tabs, buffers };
    }

    case "FILE_LOADED": {
      const { path, file } = action;
      return {
        ...state,
        buffers: {
          ...state.buffers,
          [path]: {
            saved: file.content || "",
            edited: file.content || "",
            version: file.version ?? 0,
            dirty: false,
            stale: false,
            language: file.language || null,
            updatedAt: file.updated_at || null,
          },
        },
      };
    }

    case "EDIT_BUFFER": {
      const { path, content } = action;
      const buffer = state.buffers[path];
      if (!buffer) return state;
      return {
        ...state,
        buffers: {
          ...state.buffers,
          [path]: { ...buffer, edited: content, dirty: content !== buffer.saved },
        },
      };
    }

    case "SAVE_SUCCESS": {
      const { path, file } = action;
      const buffer = state.buffers[path];
      return {
        ...state,
        buffers: {
          ...state.buffers,
          [path]: {
            ...(buffer || {}),
            saved: file.content || "",
            edited: file.content || "",
            version: file.version ?? buffer?.version ?? 0,
            dirty: false,
            stale: false,
            language: file.language ?? buffer?.language ?? null,
            updatedAt: file.updated_at || null,
          },
        },
      };
    }

    case "MARK_STALE": {
      const { path } = action;
      const buffer = state.buffers[path];
      if (!buffer) return state;
      return { ...state, buffers: { ...state.buffers, [path]: { ...buffer, stale: true } } };
    }

    case "CLEAR_STALE": {
      const { path } = action;
      const buffer = state.buffers[path];
      if (!buffer || !buffer.stale) return state;
      return { ...state, buffers: { ...state.buffers, [path]: { ...buffer, stale: false } } };
    }

    case "CLOSE_TAB": {
      const { path } = action;
      if (!(path in state.buffers) && !state.tabs.includes(path) && state.activePath !== path) {
        return state;
      }
      const { [path]: _removed, ...buffers } = state.buffers;
      const tabs = state.tabs.filter((p) => p !== path);
      const activePath = state.activePath === path ? null : state.activePath;
      return { ...state, tabs, buffers, activePath };
    }

    default:
      return state;
  }
}

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

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
