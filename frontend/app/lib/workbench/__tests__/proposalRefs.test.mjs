// W7.1b (Build Workbench plan) — tests for lib/workbench/proposalRefs.js.
// Run: node frontend/app/lib/workbench/__tests__/proposalRefs.test.mjs
import { loadSource } from "./loadSource.mjs";

// No `imports` passed on purpose: this file must stay import-free.
const { toProposalRef } = loadSource("../proposalRefs.js");

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

const base = { id: "ref-1", path: "src/App.jsx", provider: "cloud", fromLine: 3, toLine: 5, from: 40, to: 90, snippet: "<button>", hash: "h1", truncated: false };
const element = {
  tag: "button",
  classes: ["btn", "btn-primary"],
  textPreview: "Buy now",
  styles: { color: "rgb(255, 255, 255)", fontSize: "14px" },
  dynamic: false,
  instanceCount: 3,
};

// --- the bug W7.1b fixes -----------------------------------------------------
assertEqual(toProposalRef({ ...base, kind: "element", element }).element, element, "an element ref carries its `element` to the server");

// --- every other kind keeps the exact shape it always had --------------------
assertEqual(
  toProposalRef({ ...base, kind: "range" }),
  { id: "ref-1", kind: "range", path: "src/App.jsx", fromLine: 3, toLine: 5, snippet: "<button>", hash: "h1", provider: "cloud" },
  "a range ref is trimmed to the original eight fields (no from/to/truncated)"
);
assertEqual("element" in toProposalRef({ ...base, kind: "range", element }), false, "a stray `element` on a range ref is not sent");
assertEqual("element" in toProposalRef({ ...base, kind: "file", element }), false, "a stray `element` on a file ref is not sent");
assertEqual(
  toProposalRef({ id: "r", kind: "file", path: "a.css" }),
  { id: "r", kind: "file", path: "a.css", fromLine: null, toLine: null, snippet: null, hash: null, provider: null },
  "missing optional fields become null, as before"
);

// --- an element ref without a usable element -------------------------------
assertEqual("element" in toProposalRef({ ...base, kind: "element" }), false, "element ref with no element: key omitted");
assertEqual("element" in toProposalRef({ ...base, kind: "element", element: "junk" }), false, "element ref with a non-object element: key omitted");
assertEqual("element" in toProposalRef({ ...base, kind: "element", element: [element] }), false, "element ref with an array element: key omitted");

// --- shape filter ------------------------------------------------------------
const noisy = toProposalRef({
  ...base,
  kind: "element",
  element: { ...element, rect: { x: 1 }, innerHTML: "<b>", styles: { ...element.styles, zIndex: "9", display: 4 } },
});
assertEqual(Object.keys(noisy.element).sort(), ["classes", "dynamic", "instanceCount", "styles", "tag", "textPreview"], "unknown element keys are dropped");
assertEqual(noisy.element.styles, { color: "rgb(255, 255, 255)", fontSize: "14px" }, "unknown / non-string style values are dropped");

const partial = toProposalRef({ ...base, kind: "element", element: { tag: 5, classes: ["a", 7, "", "b"], instanceCount: 0 } });
assertEqual(partial.element, { tag: "", classes: ["a", "b"], textPreview: "", styles: {}, dynamic: false, instanceCount: 1 }, "wrong-typed fields fall back to safe defaults");
assertEqual(toProposalRef({ ...base, kind: "element", element: { ...element, instanceCount: 2.9 } }).element.instanceCount, 2, "instanceCount is floored");
assertEqual(toProposalRef({ ...base, kind: "element", element: { ...element, dynamic: "true" } }).element.dynamic, false, "dynamic must be exactly true");

// --- inputs are not mutated ---------------------------------------------------
const input = { ...base, kind: "element", element: JSON.parse(JSON.stringify(element)) };
const snapshot = JSON.stringify(input);
toProposalRef(input);
assertEqual(JSON.stringify(input), snapshot, "the input ref is never mutated");

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("\nAll proposalRefs tests passed");
