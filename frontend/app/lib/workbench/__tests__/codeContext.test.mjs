// W4.1 (Build Workbench plan) — reducer test for codeContext.js.
//
// Same shape as editorStore.test.mjs: loadSource() reads the REAL file
// and evaluates it under plain `node`, so this can't go green against
// code that's since moved on (see that test's own header for why the
// repo does it this way instead of a pasted-in copy of the reducer).
// codeContext.js's only import is `react`, for createContext() et al.
// at module-load time — codeContextReducer() itself never touches any
// of them, so the same trivial stub editorStore.test.mjs uses is enough
// here too (the provider/hook pair aren't exercised by this file; that
// needs a real DOM/React test runner this repo doesn't have yet).
//
// Run: node frontend/app/lib/workbench/__tests__/codeContext.test.mjs
import { loadSource } from "./loadSource.mjs";

const reactStub = {
  createContext: () => ({}),
  createElement: () => null,
  useContext: () => null,
  useMemo: (fn) => fn(),
  useReducer: () => [],
};
const { codeContextReducer, hashString, truncateSnippet, contextBudget, MAX_SNIPPET_LINES, MAX_TOTAL_CHARS } =
  loadSource("../codeContext.js", { imports: { react: reactStub } });

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
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

const initialState = { refs: [], nextId: 1, pendingJump: null, pendingReview: null };

// --- ADD_REF -----------------------------------------------------------

let state = codeContextReducer(initialState, {
  type: "ADD_REF",
  ref: { kind: "range", path: "src/App.jsx", provider: "cloud", fromLine: 10, toLine: 12, from: 100, to: 140, snippet: "abc" },
});
assertEqual(state.refs.length, 1, "ADD_REF adds one ref");
assertEqual(state.refs[0].id, "ref-1", "ADD_REF assigns a sequential id");
assertEqual(state.refs[0].truncated, false, "a short snippet isn't truncated");
assertEqual(state.refs[0].hash, hashString("abc"), "ADD_REF hashes the (possibly-truncated) snippet");
assertEqual(state.nextId, 2, "ADD_REF advances nextId");

state = codeContextReducer(state, {
  type: "ADD_REF",
  ref: { kind: "range", path: "src/App.jsx", provider: "cloud", fromLine: 10, toLine: 12, from: 999, to: 999, snippet: "xyz" },
});
assertEqual(state.refs.length, 1, "adding the identical (kind, path, fromLine, toLine) again is a no-op");

state = codeContextReducer(state, {
  type: "ADD_REF",
  ref: { kind: "file", path: "src/App.jsx", provider: "cloud", snippet: "whole file" },
});
assertEqual(state.refs.length, 2, "a 'file' ref for the same path as a 'range' ref is still a distinct chip");
assertEqual(state.refs[1].fromLine, null, "a 'file' ref has no fromLine");
assertEqual(state.refs[1].toLine, null, "a 'file' ref has no toLine");

state = codeContextReducer(state, {
  type: "ADD_REF",
  ref: { kind: "folder", path: "src/components", provider: "local", snippet: "" },
});
assertEqual(state.refs.length, 3, "ADD_REF accepts a 'folder' ref");
assertEqual(state.refs[2].hash, hashString(""), "an empty snippet still gets a (stable) hash");

// --- truncateSnippet / the 400-line cap ---------------------------------

const shortSnippet = "line\n".repeat(5).trimEnd();
assertEqual(truncateSnippet(shortSnippet), { snippet: shortSnippet, truncated: false }, "under the cap: unchanged");

const longSnippet = Array.from({ length: MAX_SNIPPET_LINES + 50 }, (_, i) => `line ${i}`).join("\n");
const { snippet: cut, truncated } = truncateSnippet(longSnippet);
assert(truncated, "over the cap: truncated is true");
assertEqual(cut.split("\n").length, MAX_SNIPPET_LINES + 1, "kept lines + one trailing '(truncated…)' marker line");
assert(cut.startsWith("line 0\n"), "truncation keeps the FIRST lines, not the last");

state = codeContextReducer(initialState, {
  type: "ADD_REF",
  ref: { kind: "file", path: "src/big.js", provider: "cloud", snippet: longSnippet },
});
assert(state.refs[0].truncated, "ADD_REF itself applies the same per-snippet cap");
assertEqual(state.refs[0].snippet, cut, "ADD_REF stores the truncated snippet, not the original");

// --- REMOVE_REF ----------------------------------------------------------

state = codeContextReducer(initialState, {
  type: "ADD_REF",
  ref: { kind: "file", path: "a.js", provider: "cloud", snippet: "a" },
});
state = codeContextReducer(state, {
  type: "ADD_REF",
  ref: { kind: "file", path: "b.js", provider: "cloud", snippet: "b" },
});
const afterRemove = codeContextReducer(state, { type: "REMOVE_REF", id: "ref-1" });
assertEqual(afterRemove.refs.map((r) => r.path), ["b.js"], "REMOVE_REF drops only the matching chip");

const noopRemove = codeContextReducer(afterRemove, { type: "REMOVE_REF", id: "not-a-real-id" });
assert(noopRemove === afterRemove, "REMOVE_REF with an unknown id returns the SAME state object");

// --- CLEAR_REFS ------------------------------------------------------------

const cleared = codeContextReducer(afterRemove, { type: "CLEAR_REFS" });
assertEqual(cleared.refs, [], "CLEAR_REFS empties the ref list");
const clearedAgain = codeContextReducer(cleared, { type: "CLEAR_REFS" });
assert(clearedAgain === cleared, "CLEAR_REFS on an already-empty list returns the SAME state object");

// --- REMAP_REFS (CM6 ChangeSet.mapPos-style tracking) -----------------------

let remapState = codeContextReducer(initialState, {
  type: "ADD_REF",
  ref: { kind: "range", path: "src/App.jsx", provider: "cloud", fromLine: 5, toLine: 5, from: 40, to: 50, snippet: "const x = 1;" },
});
remapState = codeContextReducer(remapState, {
  type: "ADD_REF",
  ref: { kind: "file", path: "src/App.jsx", provider: "cloud", snippet: "whole file" },
});
remapState = codeContextReducer(remapState, {
  type: "ADD_REF",
  ref: { kind: "range", path: "other/File.jsx", provider: "cloud", fromLine: 1, toLine: 1, from: 0, to: 5, snippet: "hi" },
});

// Typing 10 chars before the range shifts it forward by 10, on the one
// path the edit happened in — the "range" chip on a DIFFERENT path is
// left alone, and so is the "file" chip (no from/to of its own).
const shiftBy10 = codeContextReducer(remapState, {
  type: "REMAP_REFS",
  path: "src/App.jsx",
  mapRange: (from, to) => ({ from: from + 10, to: to + 10, fromLine: 6, toLine: 6 }),
});
const shifted = shiftBy10.refs.find((r) => r.kind === "range" && r.path === "src/App.jsx");
assertEqual([shifted.from, shifted.to, shifted.fromLine], [50, 60, 6], "REMAP_REFS applies mapRange to a matching 'range' chip");
const untouchedFile = shiftBy10.refs.find((r) => r.kind === "file");
assertEqual([untouchedFile.from, untouchedFile.to], [null, null], "REMAP_REFS never touches a 'file' chip");
const untouchedOtherPath = shiftBy10.refs.find((r) => r.path === "other/File.jsx");
assertEqual([untouchedOtherPath.from, untouchedOtherPath.to], [0, 5], "REMAP_REFS ignores chips on a different path");

// Deleting the referenced text outright: mapRange signals that with null,
// and the chip is dropped rather than left pointing at nothing.
const afterDelete = codeContextReducer(remapState, {
  type: "REMAP_REFS",
  path: "src/App.jsx",
  mapRange: () => null,
});
assert(
  !afterDelete.refs.some((r) => r.kind === "range" && r.path === "src/App.jsx"),
  "REMAP_REFS drops a 'range' chip whose text was deleted outright"
);
assertEqual(afterDelete.refs.length, 2, "...and only that one chip is dropped");

// --- pending jump ------------------------------------------------------------

const jumpRef = remapState.refs[0];
const withJump = codeContextReducer(remapState, { type: "SET_PENDING_JUMP", ref: jumpRef });
assertEqual(withJump.pendingJump, jumpRef, "SET_PENDING_JUMP records the ref to jump to");
const afterClearJump = codeContextReducer(withJump, { type: "CLEAR_PENDING_JUMP" });
assertEqual(afterClearJump.pendingJump, null, "CLEAR_PENDING_JUMP resets it");
const noopClear = codeContextReducer(afterClearJump, { type: "CLEAR_PENDING_JUMP" });
assert(noopClear === afterClearJump, "CLEAR_PENDING_JUMP with nothing pending returns the SAME state object");

// --- pending review (W5.3) ----------------------------------------------

const withReview = codeContextReducer(initialState, { type: "SET_PENDING_REVIEW", proposalId: "prop_1" });
assertEqual(withReview.pendingReview, "prop_1", "SET_PENDING_REVIEW records the proposal id");
assertEqual(withReview.pendingJump, null, "...without touching pendingJump");
const afterClearReview = codeContextReducer(withReview, { type: "CLEAR_PENDING_REVIEW" });
assertEqual(afterClearReview.pendingReview, null, "CLEAR_PENDING_REVIEW resets it");
const noopClearReview = codeContextReducer(afterClearReview, { type: "CLEAR_PENDING_REVIEW" });
assert(noopClearReview === afterClearReview, "CLEAR_PENDING_REVIEW with nothing pending returns the SAME state object");

// Clicking Review a second time before the first lands (same proposal,
// still just re-set) must still register as a change to consumers — a
// same-value SET_PENDING_REVIEW is NOT collapsed to a no-op the way
// CLEAR_PENDING_REVIEW is, since EditorWorkbench.jsx's effect keys off
// this changing at all, not off the id being new.
const setAgain = codeContextReducer(withReview, { type: "SET_PENDING_REVIEW", proposalId: "prop_1" });
assertEqual(setAgain.pendingReview, "prop_1", "SET_PENDING_REVIEW with the same id still produces a fresh state object");
assert(setAgain !== withReview, "...i.e. it is NOT short-circuited to the same object");

// --- contextBudget -----------------------------------------------------------

assertEqual(contextBudget([]).totalChars, 0, "contextBudget of no refs is 0");
assertEqual(contextBudget([]).overBudget, false, "...and never over budget");

const bigRefs = [
  { snippet: "x".repeat(MAX_TOTAL_CHARS - 10) },
  { snippet: "y".repeat(20) },
];
const budget = contextBudget(bigRefs);
assertEqual(budget.totalChars, MAX_TOTAL_CHARS + 10, "contextBudget sums every ref's snippet length");
assert(budget.overBudget, "...and flags overBudget once the sum passes MAX_TOTAL_CHARS");

// --- hashString ----------------------------------------------------------------

assertEqual(hashString("abc"), hashString("abc"), "hashString is deterministic");
assert(hashString("abc") !== hashString("abd"), "hashString differs for different input");
assert(!hashString("abc").startsWith("-"), "hashString never emits a leading '-' (unsigned)");

// -------------------------------------------------------------------------------

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll codeContext.js tests passed.");
}
