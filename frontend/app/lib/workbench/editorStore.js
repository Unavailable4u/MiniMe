// frontend/app/lib/workbench/editorStore.js — W2.2 (Build Workbench
// plan). A reducer in a context, scoped to wherever it's mounted (the
// Build tab's Code sub-tab today; the W2.3 workbench shell tomorrow) —
// the single source of truth an editor, a preview, and chat chips can
// all read so they agree on what's actually in a buffer, including
// edits that haven't been saved yet (plan §5, step W2.2's own "Touches"
// line: "the single source of truth the editor, preview, and chat
// chips all read... this is what lets the preview show unsaved edits
// live").
//
// State shape (exactly the plan's own): `{tabs, activePath, buffers,
// layout, proposals}`. W2.2 only populates `tabs`/`activePath`/
// `buffers` — CodeView has no tab strip and nothing to propose yet, so
// `layout`/`proposals` stay as empty placeholders a future step fills
// in without another shape change.
//
// `buffers[path]` = `{saved, edited, version, dirty, stale, language,
// updatedAt}` — `saved` is the last content the server confirmed
// (either from a read or from a successful write); `edited` is what's
// in the textarea/editor right now; `dirty` is `edited !== saved`;
// `stale` is W0.1's "changed on the server while you had unsaved
// edits" flag, now per-buffer instead of the single component-wide
// staleBanner it used to be.
//
// No JSX here on purpose — every other file directly under `lib/`
// (cmTheme.js, editorUtils.js) is plain functions/data, not components,
// so the provider component below is written with `createElement`
// rather than becoming this directory's first `.jsx` file for one
// wrapper.
"use client";
import { createContext, createElement, useContext, useMemo, useReducer } from "react";

const EditorStoreContext = createContext(null);

const initialState = {
  tabs: [],
  activePath: null,
  buffers: {},
  layout: {},
  proposals: [],
};

/**
 * Pure reducer — no React, no fetch, nothing but state in/state out —
 * so it can be exercised by a dependency-free `.mjs` test with plain
 * `node` (repo convention, see
 * components/__tests__/wiringGraph.linkFilter.test.mjs). Exported
 * directly rather than only reachable through the hook below, which is
 * what makes that test possible without mounting a provider.
 */
export function editorReducer(state, action) {
  switch (action.type) {
    // Opening (or re-clicking) a file. Adds it to `tabs` the first time
    // (harmless today — CodeView doesn't render a tab strip — but
    // W2.3's explorer/tabs read the exact same store, so this already
    // does the right thing for them). Re-selecting a file that has a
    // buffer clears any pending `stale` flag on it — the previous
    // component-wide staleBanner was dismissed by clicking the file
    // again too (openFile() used to call setStaleBanner(null) up
    // front), so this preserves that timing per-buffer.
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

    // A provider.read(path) resolved. `file` is the FileProvider's
    // file shape (content/language/version/updated_at). `saved` and
    // `edited` start equal — nothing to save until the user types.
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

    // Every keystroke in the editor. No-ops on a path with no buffer
    // yet (can't happen from CodeView's own UI — the textarea only
    // exists once a file is loaded — but a reducer should never throw
    // on an ordering it doesn't expect).
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

    // A provider.write(path, ...) resolved. `file` is the server's
    // confirmed shape post-save — swapped in as BOTH `saved` and
    // `edited` (matching the pre-W2.2 `setFileContent(saved);
    // setEditedContent(saved.content || "")` pair), so a save also
    // clears `dirty`/`stale`.
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

    // A subscribe() callback named this file while it had unsaved
    // edits — the W0.1 "don't clobber the textarea, show a banner
    // instead" case. No-ops if the buffer isn't open (nothing to flag).
    case "MARK_STALE": {
      const { path } = action;
      const buffer = state.buffers[path];
      if (!buffer) return state;
      return { ...state, buffers: { ...state.buffers, [path]: { ...buffer, stale: true } } };
    }

    // "Keep mine" — dismiss the banner without discarding the edit.
    // ("Reload" doesn't need its own action: it just re-opens the file,
    // which is SET_ACTIVE_PATH + FILE_LOADED, same as any other open.)
    case "CLEAR_STALE": {
      const { path } = action;
      const buffer = state.buffers[path];
      if (!buffer || !buffer.stale) return state;
      return { ...state, buffers: { ...state.buffers, [path]: { ...buffer, stale: false } } };
    }

    // The file list refreshed and this path is no longer in it (deleted
    // or renamed elsewhere) — drop its tab and buffer, and clear
    // activePath if it was the one showing.
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

export function EditorStoreProvider({ children }) {
  const [state, dispatch] = useReducer(editorReducer, initialState);

  // Action creators are memoized on `dispatch` alone — React guarantees
  // `dispatch`'s identity never changes across a component's lifetime,
  // so this object (and therefore every action function on it) is
  // stable across re-renders too. That's what lets CodeViewBody's own
  // Pusher-handler effect below list `markStale` in a dependency array
  // without re-subscribing on every keystroke.
  const actions = useMemo(
    () => ({
      setActivePath: (path) => dispatch({ type: "SET_ACTIVE_PATH", path }),
      fileLoaded: (path, file) => dispatch({ type: "FILE_LOADED", path, file }),
      editBuffer: (path, content) => dispatch({ type: "EDIT_BUFFER", path, content }),
      saveSuccess: (path, file) => dispatch({ type: "SAVE_SUCCESS", path, file }),
      markStale: (path) => dispatch({ type: "MARK_STALE", path }),
      clearStale: (path) => dispatch({ type: "CLEAR_STALE", path }),
      closeTab: (path) => dispatch({ type: "CLOSE_TAB", path }),
    }),
    [dispatch]
  );

  const value = useMemo(() => ({ state, ...actions }), [state, actions]);

  return createElement(EditorStoreContext.Provider, { value }, children);
}

/**
 * Split from EditorStoreProvider into its own hook rather than having
 * one component render the provider AND call this — a context
 * provider's own value isn't visible to hooks called in the same
 * component that mounts it. Same reason every provider/consumer pair
 * in this codebase is already two components (see
 * WorkspaceDockContext.jsx's own Provider + the hooks that read it).
 */
export function useEditorStore() {
  const ctx = useContext(EditorStoreContext);
  if (!ctx) {
    throw new Error("useEditorStore must be used within an EditorStoreProvider");
  }
  return ctx;
}
