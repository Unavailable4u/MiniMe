// W6.5 (Build Workbench plan) — tests for lib/workbench/pulse.js.
//
// Exercises the StateField against a bare EditorState (no EditorView,
// so no DOM): a transaction carrying the setPulse effect must put
// exactly one mark decoration in the field, a null effect must clear
// it, and edits must shift it. pulseRange()'s timer/dispatch wrapper
// needs a live view and is covered by the build + lint, not here —
// same "state logic is testable, the view glue isn't" line
// gotoPosition.js's own header draws.
//
// Run: node frontend/app/lib/workbench/__tests__/pulse.test.mjs
import * as cmState from "@codemirror/state";
import * as cmView from "@codemirror/view";
import { loadSource } from "./loadSource.mjs";

const { pulseField, setPulse, pulseExtension, pulseRange } = loadSource("../pulse.js", {
  imports: { "@codemirror/state": cmState, "@codemirror/view": cmView },
});

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

function ranges(state) {
  const out = [];
  const iter = state.field(pulseField).iter();
  while (iter.value) {
    out.push([iter.from, iter.to]);
    iter.next();
  }
  return out;
}

const base = cmState.EditorState.create({ doc: "0123456789", extensions: [pulseField] });
assertEqual(ranges(base), [], "no pulse to start with");

const pulsed = base.update({ effects: setPulse.of({ from: 2, to: 6 }) }).state;
assertEqual(ranges(pulsed), [[2, 6]], "the setPulse effect adds exactly one mark over the range");

const cleared = pulsed.update({ effects: setPulse.of(null) }).state;
assertEqual(ranges(cleared), [], "a null setPulse clears it");

const shifted = pulsed.update({ changes: { from: 0, insert: "ab" } }).state;
assertEqual(ranges(shifted), [[4, 8]], "typing before the pulse shifts it with the text");

assertEqual(ranges(base.update({ effects: setPulse.of({ from: 3, to: 3 }) }).state), [], "an empty range is skipped (a mark decoration may not be zero-length)");
assertEqual(ranges(base.update({ effects: setPulse.of({ from: 8, to: 99 }) }).state), [[8, 10]], "a range running past the end is clamped to the document");
assertEqual(ranges(base.update({ effects: setPulse.of({ from: 50, to: 60 }) }).state), [], "a range entirely past the end collapses to nothing rather than throwing");

// Dispatching the effect at a state WITHOUT the field installed is a
// harmless no-op — callers never have to check which editors have it.
const bare = cmState.EditorState.create({ doc: "abc" });
let threw = false;
try {
  bare.update({ effects: setPulse.of({ from: 0, to: 2 }) });
} catch {
  threw = true;
}
assertEqual(threw, false, "a setPulse effect against an editor with no pulseField installed doesn't throw");

assertEqual(Array.isArray(pulseExtension) && pulseExtension.includes(pulseField), true, "pulseExtension includes the field");
assertEqual(typeof pulseRange, "function", "pulseRange is exported");
pulseRange(null, 0, 5); // a missing view is a no-op, not a throw
assertEqual(true, true, "pulseRange(null, ...) is a no-op");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll pulse.js tests passed.");
}
