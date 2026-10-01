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

const initialState = { refs: [], nextId: 1, pendingJump: null, pendingReview: null, pendingChatMode: null, pendingDraft: null, pendingUsage: null, usage: null };

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

// --- element refs (W6.5) ---------------------------------------------------

const elementInfo = { tag: "button", classes: ["btn-primary"], textPreview: "Save", styles: { color: "red" }, dynamic: false, instanceCount: 1 };
let elState = codeContextReducer(initialState, {
  type: "ADD_REF",
  ref: { kind: "element", path: "index.html", provider: "cloud", fromLine: 4, toLine: 7, from: 30, to: 90, snippet: '<div class="a">\n</div>', element: elementInfo },
});
assertEqual(elState.refs[0].element, elementInfo, "ADD_REF carries an element ref's `element` info through onto the stored ref");
assertEqual(elState.refs[0].kind, "element", "...as kind 'element'");
assertEqual("element" in codeContextReducer(initialState, { type: "ADD_REF", ref: { kind: "range", path: "a.js", fromLine: 1, toLine: 1, from: 0, to: 1, snippet: "x" } }).refs[0], false, "a non-element ref does NOT gain an `element` key (every other kind keeps exactly its old shape)");

const elShifted = codeContextReducer(elState, {
  type: "REMAP_REFS",
  path: "index.html",
  mapRange: (from, to) => ({ from: from + 5, to: to + 5, fromLine: 5, toLine: 8 }),
});
assertEqual([elShifted.refs[0].from, elShifted.refs[0].to, elShifted.refs[0].fromLine, elShifted.refs[0].toLine], [35, 95, 5, 8], "REMAP_REFS shifts an element chip with the edit, same as a range chip");
assertEqual(elShifted.refs[0].element, elementInfo, "...and keeps its element info while doing so");
assertEqual(codeContextReducer(elState, { type: "REMAP_REFS", path: "index.html", mapRange: () => null }).refs.length, 0, "an element chip whose whole range was deleted is dropped, not left pointing at nothing");

// --- pending chat mode (W6.2) --------------------------------------------

const withChatMode = codeContextReducer(initialState, { type: "SET_PENDING_CHAT_MODE", mode: "ask" });
assertEqual(withChatMode.pendingChatMode, "ask", "SET_PENDING_CHAT_MODE records the requested mode");
assertEqual(withChatMode.pendingReview, null, "...without touching pendingReview");
const afterClearChatMode = codeContextReducer(withChatMode, { type: "CLEAR_PENDING_CHAT_MODE" });
assertEqual(afterClearChatMode.pendingChatMode, null, "CLEAR_PENDING_CHAT_MODE resets it");
const noopClearChatMode = codeContextReducer(afterClearChatMode, { type: "CLEAR_PENDING_CHAT_MODE" });
assert(noopClearChatMode === afterClearChatMode, "CLEAR_PENDING_CHAT_MODE with nothing pending returns the SAME state object");

// Same "still a fresh object on a repeat click" requirement as
// SET_PENDING_REVIEW above — a second "Fix with AI" click before the
// first request is consumed must still register as a change.
const setChatModeAgain = codeContextReducer(withChatMode, { type: "SET_PENDING_CHAT_MODE", mode: "ask" });
assertEqual(setChatModeAgain.pendingChatMode, "ask", "SET_PENDING_CHAT_MODE with the same mode still produces a fresh state object");
assert(setChatModeAgain !== withChatMode, "...i.e. it is NOT short-circuited to the same object");

// --- pending draft (W8.4) ----------------------------------------------------

const withDraft = codeContextReducer(initialState, { type: "SET_PENDING_DRAFT", text: "  Work on this build step: \"Flash the firmware\".  " });
assertEqual(withDraft.pendingDraft, "Work on this build step: \"Flash the firmware\".", "SET_PENDING_DRAFT records the text, trimmed");
assertEqual([withDraft.pendingChatMode, withDraft.pendingReview, withDraft.refs.length], [null, null, 0], "...without touching the other pending slots or the chips");
assert(codeContextReducer(initialState, { type: "SET_PENDING_DRAFT", text: "   " }) === initialState, "SET_PENDING_DRAFT with whitespace-only text is a no-op (same state object)");
assert(codeContextReducer(initialState, { type: "SET_PENDING_DRAFT", text: "" }) === initialState, "...and so is empty text");
assert(codeContextReducer(initialState, { type: "SET_PENDING_DRAFT", text: null }) === initialState, "...and a non-string");
const afterClearDraft = codeContextReducer(withDraft, { type: "CLEAR_PENDING_DRAFT" });
assertEqual(afterClearDraft.pendingDraft, null, "CLEAR_PENDING_DRAFT resets it");
assert(codeContextReducer(afterClearDraft, { type: "CLEAR_PENDING_DRAFT" }) === afterClearDraft, "CLEAR_PENDING_DRAFT with nothing pending returns the SAME state object");
const draftThenMode = codeContextReducer(withDraft, { type: "SET_PENDING_CHAT_MODE", mode: "edit" });
assertEqual([draftThenMode.pendingDraft === withDraft.pendingDraft, draftThenMode.pendingChatMode], [true, "edit"], "a draft request and a chat-mode request coexist (Work on this sets both)");
const draftSurvivesClearRefs = codeContextReducer(codeContextReducer(withDraft, { type: "ADD_REF", ref: { kind: "file", path: "a.js", snippet: "x" } }), { type: "CLEAR_REFS" });
assertEqual(draftSurvivesClearRefs.pendingDraft, withDraft.pendingDraft, "CLEAR_REFS (after a chat send) doesn't discard a draft that is still waiting for the composer");

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

// --- W7.1b: usage-site lookup state --------------------------------------------

{
  const el = { kind: "element", path: "src/Button.jsx", fromLine: 3, toLine: 5, snippet: "<button/>", element: { tag: "button", classes: ["btn"] } };
  let s = codeContextReducer(initialState, { type: "ADD_REF", ref: el });
  const ref = s.refs[0];
  assertEqual(ref.element, el.element, "an element ref keeps its `element` through ADD_REF (what toProposalRef() later sends)");

  s = codeContextReducer(s, { type: "REQUEST_USAGE_SITES", ref });
  assertEqual(s.pendingUsage.id, ref.id, "REQUEST_USAGE_SITES parks the request for the workbench");
  assertEqual([s.usage.refId, s.usage.status, s.usage.sites], [ref.id, "loading", []], "…and shows a loading list meanwhile");

  const before = s;
  assert(codeContextReducer(s, { type: "REQUEST_USAGE_SITES", ref: null }) === before, "a request with no ref is ignored");
  assert(codeContextReducer(s, { type: "REQUEST_USAGE_SITES", ref: { path: "x" } }) === before, "a request with no ref id is ignored");

  s = codeContextReducer(s, { type: "CLEAR_PENDING_USAGE" });
  assertEqual(s.pendingUsage, null, "CLEAR_PENDING_USAGE consumes the request");
  assert(codeContextReducer(s, { type: "CLEAR_PENDING_USAGE" }) === s, "clearing an empty slot is a no-op (same state object)");
  assertEqual(s.usage.status, "loading", "…and leaves the loading list alone");

  const site = { path: "src/App.jsx", name: "Button", fromLine: 5, toLine: 5, from: 10, to: 20, snippet: "<Button>", preview: "<Button>" };
  s = codeContextReducer(s, { type: "SET_USAGE_RESULT", refId: ref.id, status: "done", names: ["Button"], sites: [site], truncated: true, message: "note" });
  assertEqual([s.usage.status, s.usage.names, s.usage.sites.length, s.usage.truncated, s.usage.message], ["done", ["Button"], 1, true, "note"], "SET_USAGE_RESULT fills the list in");
  assertEqual(s.usage.refId, ref.id, "…for the same chip");

  assert(codeContextReducer(s, { type: "SET_USAGE_RESULT", refId: "ref-999", status: "done", sites: [] }) === s, "a result for a different chip is ignored");
  assert(codeContextReducer(initialState, { type: "SET_USAGE_RESULT", refId: "ref-1", status: "done", sites: [site] }) === initialState, "a result with no lookup showing is ignored");

  const err = codeContextReducer(s, { type: "SET_USAGE_RESULT", refId: ref.id, status: "error", message: "boom" });
  assertEqual([err.usage.status, err.usage.sites, err.usage.message], ["error", [], "boom"], "an error result clears stale sites and carries the message");
  assertEqual(codeContextReducer(s, { type: "SET_USAGE_RESULT", refId: ref.id, status: "anything" }).usage.status, "done", "any non-error status is stored as done");

  assertEqual(codeContextReducer(s, { type: "CLEAR_USAGE" }).usage, null, "CLEAR_USAGE dismisses the list");
  assert(codeContextReducer(initialState, { type: "CLEAR_USAGE" }) === initialState, "CLEAR_USAGE with nothing showing is a no-op");

  // Removing the chip the lookup was for drops its list — and a result that
  // arrives afterwards does not bring it back.
  const removed = codeContextReducer(s, { type: "REMOVE_REF", id: ref.id });
  assertEqual(removed.usage, null, "removing the source chip drops its usage list");
  assert(codeContextReducer(removed, { type: "SET_USAGE_RESULT", refId: ref.id, status: "done", sites: [site] }) === removed, "a late result for a removed chip is ignored");

  // Removing a chip the lookup ISN'T for leaves it alone.
  let two = codeContextReducer(s, { type: "ADD_REF", ref: { kind: "file", path: "a.css", snippet: "" } });
  const other = two.refs.find((r) => r.kind === "file");
  two = codeContextReducer(two, { type: "REMOVE_REF", id: other.id });
  assertEqual(two.usage.refId, ref.id, "removing an unrelated chip keeps the usage list");

  // A pending request for a chip that is removed before the workbench picks it up.
  let pend = codeContextReducer(codeContextReducer(initialState, { type: "ADD_REF", ref: el }), { type: "REQUEST_USAGE_SITES", ref });
  pend = codeContextReducer(pend, { type: "REMOVE_REF", id: ref.id });
  assertEqual([pend.pendingUsage, pend.usage], [null, null], "removing the chip also cancels a not-yet-consumed request");

  // CLEAR_REFS (sent after a chat send) clears it all.
  let cleared = codeContextReducer(s, { type: "REQUEST_USAGE_SITES", ref });
  cleared = codeContextReducer(cleared, { type: "CLEAR_REFS" });
  assertEqual([cleared.refs.length, cleared.usage, cleared.pendingUsage], [0, null, null], "CLEAR_REFS clears the usage list and any pending request");
  assert(codeContextReducer(initialState, { type: "CLEAR_REFS" }) === initialState, "CLEAR_REFS on an empty store is still a no-op");

  // Adding a usage site through ADD_REF is an ordinary range chip (tracked on edits).
  let added = codeContextReducer(s, { type: "ADD_REF", ref: { kind: "range", path: site.path, from: site.from, to: site.to, fromLine: site.fromLine, toLine: site.toLine, snippet: site.snippet } });
  assertEqual(added.refs.filter((r) => r.kind === "range").length, 1, "a usage site is added as a range chip");
  assert(codeContextReducer(added, { type: "ADD_REF", ref: { kind: "range", path: site.path, from: site.from, to: site.to, fromLine: site.fromLine, toLine: site.toLine, snippet: site.snippet } }) === added, "adding the same usage site twice is deduped");
  assertEqual(added.usage.status, "done", "…and adding a chip doesn't disturb the list");
}

// -------------------------------------------------------------------------------

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll codeContext.js tests passed.");
}
