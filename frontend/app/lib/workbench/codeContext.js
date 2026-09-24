// frontend/app/lib/workbench/codeContext.js — W4.1 (Build Workbench
// plan). "Code context store, selection actions, chips" — the shared
// store the plan's own model describes (§5, step W4.1): a small
// reducer-in-a-context, mounted in BuildTab.jsx ABOVE both
// EditorWorkbench and WorkspaceChatPanel (they're siblings there), so
// either side can add/read/remove refs without prop-drilling through
// the other. Same split as editorStore.js: a pure reducer (exported
// directly, so it can be exercised by a dependency-free `.mjs` test —
// see __tests__/codeContext.test.mjs) + a thin provider/hook pair
// wired to React.
//
// Ref model (exactly the plan's own): `{id, kind, path, fromLine,
// toLine, snippet, hash, provider}`, plus a couple of fields the
// reducer fills in that the plan's shorthand didn't spell out but its
// own "Build" bullet requires:
//   - `from`/`to` — character offsets into the file at the moment the
//     ref was added. Needed for "range chips track edits via CM6
//     ChangeSet.mapPos" (see remapRefs() below); a "file"/"folder" ref
//     has no single position, so these stay null for those kinds.
//   - `truncated` — true when `snippet` was cut down to the ≤400-line
//     cap (truncateSnippet() below).
// `provider` is the FileProvider id ("cloud" | "local") the ref's path
// was read through, not a CM6/React provider — same word, unrelated
// meaning, matching fileProviders.js's own naming.
//
// What this file does NOT do (by design, kept for later steps):
//   - It doesn't talk to the chat composer at all — codeRefs on a
//     chat message, the Ask/Edit mode toggle, and bypassing the intent
//     classifier for a chip-bearing send are W4.2's job
//     (WorkspaceChatPanel.jsx).
//   - It doesn't expand a folder ref into files, or budget which ones
//     — that's W5.5, server-side.
//   - It doesn't know about CodeMirror, Pusher, or `code_file_updated`
//     — `hash` is computed once at add-time (hashString() below) so a
//     LATER step (staleness banners, akin to W5.4's `base_hash` check)
//     has something to compare against; nothing here watches for it to
//     go stale on its own.
"use client";
import { createContext, createElement, useContext, useMemo, useReducer } from "react";

// Plan §5 W4.1: "Caps: ≤400 lines per snippet, ≤60k chars total, warn
// beyond." Exported so ContextChips.jsx can render the same numbers in
// its warning strip instead of a second copy going stale.
export const MAX_SNIPPET_LINES = 400;
export const MAX_TOTAL_CHARS = 60000;

/**
 * A fast, dependency-free hash — NOT cryptographic, just enough to
 * tell "this snippet is probably the same bytes" from "it changed"
 * (djb2, the same algorithm many small hash-map implementations use
 * for string keys). Good enough for a later step's staleness check;
 * nothing here should ever need this to be collision-proof.
 */
export function hashString(str) {
  let h = 5381;
  for (let i = 0; i < str.length; i++) {
    h = (h * 33) ^ str.charCodeAt(i);
  }
  // >>> 0 folds the signed 32-bit result into an unsigned one before
  // the hex conversion, so this never emits a leading "-".
  return (h >>> 0).toString(16);
}

/**
 * Enforces the ≤400-line cap on one snippet. Splitting on "\n" (never
 * "\r\n") is safe here because every snippet this module receives
 * either came out of a CodeMirror doc (CodeEditor.jsx always reports
 * LF, see that file's own header) or a FileProvider read whose exact
 * line-ending isn't this store's concern — losing a trailing "\r" on a
 * truncation boundary changes nothing about what's SHOWN in a chip.
 */
export function truncateSnippet(snippet) {
  const lines = snippet.split("\n");
  if (lines.length <= MAX_SNIPPET_LINES) return { snippet, truncated: false };
  const kept = lines.slice(0, MAX_SNIPPET_LINES);
  return {
    snippet: `${kept.join("\n")}\n… (truncated at ${MAX_SNIPPET_LINES} lines)`,
    truncated: true,
  };
}

/**
 * Total context size across every current ref, and whether it's past
 * the plan's 60k-char budget. A selector rather than something the
 * reducer stores on each ref — the budget is a property of the WHOLE
 * set, not any one ref, so recomputing it from `refs` is both cheaper
 * to keep correct (nothing to update when a ref is removed) and
 * matches how ContextChips.jsx wants to render it: one warning strip
 * above the chip list, not a flag per chip.
 */
export function contextBudget(refs) {
  const totalChars = refs.reduce((sum, ref) => sum + (ref.snippet ? ref.snippet.length : 0), 0);
  return { totalChars, overBudget: totalChars > MAX_TOTAL_CHARS };
}

// Two refs are "the same chip" if they point at the same kind of thing
// in the same place — adding an already-present selection again (a
// double-click on "Add to chat", the same Mod-L twice) is a no-op
// rather than a second identical chip.
function refKey(ref) {
  return `${ref.kind}:${ref.path}:${ref.fromLine ?? ""}:${ref.toLine ?? ""}`;
}

const initialState = { refs: [], nextId: 1, pendingJump: null, pendingReview: null };

/**
 * Pure reducer — see editorStore.js's own header for why this shape
 * (exported directly, no React in sight) is what makes
 * __tests__/codeContext.test.mjs possible without mounting a provider.
 */
export function codeContextReducer(state, action) {
  switch (action.type) {
    // A selection ("range"), a whole file, or a whole folder was added
    // via CodeEditor's floating toolbar/Mod-L, or Explorer's "Add to
    // chat" menu item. `action.ref` carries everything the caller
    // already knows (kind, path, provider, snippet, and for a range:
    // from/to/fromLine/toLine); this fills in `id`, applies the
    // per-snippet truncation cap, and computes `hash`.
    case "ADD_REF": {
      const incoming = action.ref;
      if (state.refs.some((r) => refKey(r) === refKey(incoming))) return state;
      const { snippet, truncated } = truncateSnippet(incoming.snippet || "");
      const ref = {
        id: `ref-${state.nextId}`,
        kind: incoming.kind,
        path: incoming.path,
        provider: incoming.provider ?? null,
        fromLine: incoming.fromLine ?? null,
        toLine: incoming.toLine ?? null,
        from: incoming.from ?? null,
        to: incoming.to ?? null,
        snippet,
        truncated,
        hash: hashString(snippet),
      };
      return { ...state, nextId: state.nextId + 1, refs: [...state.refs, ref] };
    }

    // The chip's own × button.
    case "REMOVE_REF": {
      const refs = state.refs.filter((r) => r.id !== action.id);
      return refs.length === state.refs.length ? state : { ...state, refs };
    }

    // W4.2 will likely call this on send (Ask mode keeps only
    // {path, fromLine, toLine} on the persisted message — see the
    // plan's own W4.2 "Build" note — so the composer's own chip tray
    // starts empty again for the next message); not called by
    // anything yet in this patch.
    case "CLEAR_REFS":
      return state.refs.length === 0 ? state : { ...state, refs: [] };

    // "Range chips track edits via CM6 ChangeSet.mapPos so they don't
    // drift while you type" (plan §5 W4.1). `mapRange(from, to)` is
    // built by CodeEditor.jsx from that transaction's own ChangeSet —
    // this reducer has no CodeMirror import of its own (see this
    // file's header on why), so the actual position math happens at
    // the call site and arrives here as a plain function. Returning
    // `null` from it (the range's text was deleted outright) drops the
    // ref instead of leaving a chip pointing at nothing.
    case "REMAP_REFS": {
      const { path, mapRange } = action;
      let changed = false;
      const refs = [];
      for (const ref of state.refs) {
        if (ref.kind !== "range" || ref.path !== path || ref.from == null || ref.to == null) {
          refs.push(ref);
          continue;
        }
        const mapped = mapRange(ref.from, ref.to);
        changed = true;
        if (mapped) {
          refs.push({ ...ref, from: mapped.from, to: mapped.to, fromLine: mapped.fromLine, toLine: mapped.toLine });
        }
      }
      return changed ? { ...state, refs } : state;
    }

    // ContextChips.jsx's click-to-jump. One pending jump at a time —
    // a second click before the first lands just replaces it, which is
    // what you want from a chip list (no queue of stale jumps).
    case "SET_PENDING_JUMP":
      return { ...state, pendingJump: action.ref };

    case "CLEAR_PENDING_JUMP":
      return state.pendingJump == null ? state : { ...state, pendingJump: null };

    // W5.3: WorkspaceChatPanel.jsx's proposals list "Review" button, by
    // way of BuildTab.jsx's CodeAwareChatPanel wrapper. One pending
    // review request at a time, same reasoning as pendingJump above — a
    // second click before the first lands just replaces it. Carries
    // only the proposal's id: EditorWorkbench.jsx's own effect always
    // re-fetches (GET .../code/proposals/{id}) rather than trusting
    // whatever shape the caller happened to have, so a review opened
    // from a stale list entry (W5.4's tray, a proposal another tab
    // already resolved) still sees the CURRENT status.
    case "SET_PENDING_REVIEW":
      return { ...state, pendingReview: action.proposalId };

    case "CLEAR_PENDING_REVIEW":
      return state.pendingReview == null ? state : { ...state, pendingReview: null };

    default:
      return state;
  }
}

const CodeContextContext = createContext(null);

/**
 * @param {object} props
 * @param {React.ReactNode} props.children
 *
 * No `key`/reset prop of its own: BuildTab.jsx mounts this with
 * `key={selected?.id}` (the same trick EditorWorkbench's own
 * per-project remount already relies on for editorStore) so switching
 * build projects starts with an empty chip tray rather than carrying
 * refs that point at a different workspace's files.
 */
export function CodeContextProvider({ children }) {
  const [state, dispatch] = useReducer(codeContextReducer, initialState);

  // See editorStore.js's own comment on why these are memoized on
  // `dispatch` alone (stable identity for the component's whole life).
  const actions = useMemo(
    () => ({
      addRef: (ref) => dispatch({ type: "ADD_REF", ref }),
      removeRef: (id) => dispatch({ type: "REMOVE_REF", id }),
      clearRefs: () => dispatch({ type: "CLEAR_REFS" }),
      remapRefs: (path, mapRange) => dispatch({ type: "REMAP_REFS", path, mapRange }),
      requestJump: (ref) => dispatch({ type: "SET_PENDING_JUMP", ref }),
      clearJump: () => dispatch({ type: "CLEAR_PENDING_JUMP" }),
      requestReview: (proposalId) => dispatch({ type: "SET_PENDING_REVIEW", proposalId }),
      clearReview: () => dispatch({ type: "CLEAR_PENDING_REVIEW" }),
    }),
    [dispatch]
  );

  const value = useMemo(
    () => ({ refs: state.refs, pendingJump: state.pendingJump, pendingReview: state.pendingReview, ...actions }),
    [state.refs, state.pendingJump, state.pendingReview, actions]
  );

  return createElement(CodeContextContext.Provider, { value }, children);
}

/**
 * Split from CodeContextProvider into its own hook for the same reason
 * useEditorStore() is split from EditorStoreProvider — a provider's
 * own value isn't visible to hooks called in the same component that
 * mounts it. BuildTab.jsx, which mounts CodeContextProvider, reads it
 * only indirectly (its descendants — EditorWorkbench, ContextChips —
 * call this hook themselves).
 */
export function useCodeContext() {
  const ctx = useContext(CodeContextContext);
  if (!ctx) {
    throw new Error("useCodeContext must be used within a CodeContextProvider");
  }
  return ctx;
}
