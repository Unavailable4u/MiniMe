// W7.3 (Build Workbench plan) — tests for lib/preview/sourceRef.js, the
// attribute-reader adapter. Loads the REAL file; its only static import
// is the sibling mmRange.js, supplied (really loaded, not stubbed)
// through loadSource's `imports` map. Its lazy `import("@babel/parser")`
// resolves at call time, same as instrument.test.mjs relies on — needs
// `npm install` run in frontend/ first.
//
// The fixtures are instrumentJsx()'s own ground truth where a range is
// asserted: "the adapter recovers the same end instrument.js (W6.3)
// would have stamped" is the property that matters, so the expected
// values below are computed by the real instrumenter, not hand-typed.
//
// Run: node frontend/app/lib/preview/__tests__/sourceRef.test.mjs
import * as parse5 from "parse5";
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

const mmRange = loadSource("../../preview/mmRange.js");
const { parseLocator, buildMm, mapToWorkspacePath, openingTagEnd, resolveSelectRef } = loadSource("../../preview/sourceRef.js", {
  imports: { "./mmRange": mmRange },
});
const { instrumentJsx } = loadSource("../../preview/instrument.js", { imports: { parse5 } });

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

// --- parseLocator -------------------------------------------------------------

assertEqual(parseLocator("/Users/me/app/src/App.jsx:12:4"), { path: "/Users/me/app/src/App.jsx", line: 12, col: 4 }, "unix absolute path:line:col");
assertEqual(parseLocator("C:\\Users\\me\\app\\src\\App.jsx:12:4"), { path: "C:\\Users\\me\\app\\src\\App.jsx", line: 12, col: 4 }, "a Windows drive letter's colon isn't mistaken for a separator (parsed from the right)");
assertEqual(parseLocator("src/App.jsx:1:0"), { path: "src/App.jsx", line: 1, col: 0 }, "relative path, column 0");
for (const bad of ["", "App.jsx", "App.jsx:3", "App.jsx:a:b", ":1:2", null, undefined, 7]) {
  assertEqual(parseLocator(bad), null, `rejects ${JSON.stringify(bad)}`);
}

assertEqual(buildMm("src/App.jsx", 3, 2, 5, 8), "src/App.jsx:3:2:5:8", "buildMm matches the data-mm contract's shape");

// --- mapToWorkspacePath -------------------------------------------------------

const known = new Set(["src/App.jsx", "src/components/Button.jsx", "index.html", "App.jsx", "src/Foo.JSX"]);
assertEqual(mapToWorkspacePath("src/App.jsx", known), "src/App.jsx", "an already-relative workspace path maps to itself");
assertEqual(mapToWorkspacePath("/Users/me/proj/src/components/Button.jsx", known), "src/components/Button.jsx", "an absolute disk path maps by its longest workspace suffix");
assertEqual(mapToWorkspacePath("C:\\Users\\me\\proj\\src\\components\\Button.jsx", known), "src/components/Button.jsx", "a Windows absolute path maps the same way");
assertEqual(mapToWorkspacePath("/Users/me/proj/src/App.jsx", known), "src/App.jsx", "the LONGEST suffix wins over the bare file name (src/App.jsx, not App.jsx)");
assertEqual(mapToWorkspacePath("/Users/me/proj/index.html", known), "index.html", "a root file maps");
assertEqual(mapToWorkspacePath("./src/App.jsx", known), "src/App.jsx", "a leading ./ is ignored");
assertEqual(mapToWorkspacePath("/Users/me/proj/node_modules/lib/Other.jsx", known), null, "a file the workspace doesn't have → null (never a guess)");
assertEqual(mapToWorkspacePath("/Users/me/proj/src/foo.jsx", known), "src/Foo.JSX", "case-insensitive fallback when exactly one file matches (Windows disks are case-insensitive)");
assertEqual(mapToWorkspacePath("/x/a.jsx", new Set(["A.jsx", "a.JSX"])), null, "…but two files differing only by case → null, not a coin flip");
assertEqual(mapToWorkspacePath("", known), null, "empty → null");
assertEqual(mapToWorkspacePath(null, known), null, "null → null");
assertEqual(mapToWorkspacePath("/", known), null, "a bare slash → null");
assertEqual(mapToWorkspacePath("/Users/me/proj/src/App.jsx", ["src/App.jsx"]), "src/App.jsx", "an array of known paths works as well as a Set");
assertEqual(mapToWorkspacePath("/Users/me/Other/App.jsx", known), "App.jsx", "documented limit: a bare file name still matches a root file of the same name");

// --- openingTagEnd ------------------------------------------------------------

{
  const t = '<button onClick={() => go(1)} title="a > b">hi</button>';
  assertEqual(t.slice(0, openingTagEnd(t, 0)), '<button onClick={() => go(1)} title="a > b">', "skips '>' inside a {} expression (arrow =>) and inside a quoted attribute");
  assertEqual(openingTagEnd("<img src='x.png' />", 0), "<img src='x.png' />".length, "a self-closing tag ends after '/>'");
  assertEqual(openingTagEnd("abc", 0), -1, "not at a '<' → -1");
  assertEqual(openingTagEnd("<div", 0), -1, "an unterminated tag → -1");
  assertEqual(openingTagEnd("<div style={{a: {b: 1}}}>x</div>", 0), "<div style={{a: {b: 1}}}>".length, "nested braces are balanced");
}

// --- resolveSelectRef: data-mm / data-locatorjs-id route (frame already resolved a full range) ---

const noRead = async () => {
  throw new Error("must not read a file for a full range");
};
{
  const r = await resolveSelectRef({ mm: "/Users/me/proj/src/App.jsx:3:2:5:8" }, { knownPaths: known, readText: noRead });
  assertEqual(r, { mm: "src/App.jsx:3:2:5:8", approx: false }, "a full range keeps its numbers and has its absolute path mapped into the workspace");
}
{
  const r = await resolveSelectRef({ mm: "src/App.jsx:3:2:5:8" }, { knownPaths: known, readText: noRead });
  assertEqual(r, { mm: "src/App.jsx:3:2:5:8", approx: false }, "a data-mm already in workspace form passes through, no file read");
}
assertEqual(await resolveSelectRef({ mm: "/elsewhere/Nope.jsx:1:0:1:5" }, { knownPaths: known, readText: noRead }), { error: "not-in-workspace", path: "/elsewhere/Nope.jsx" }, "a path outside the workspace is reported, not guessed");
assertEqual(await resolveSelectRef({ mm: "garbage" }, { knownPaths: known, readText: noRead }), { error: "unreadable" }, "an unparseable mm is 'unreadable'");
assertEqual(await resolveSelectRef({}, { knownPaths: known, readText: noRead }), { error: "no-ref" }, "neither mm nor locator → no-ref");
assertEqual(await resolveSelectRef(null, { knownPaths: known, readText: noRead }), { error: "no-ref" }, "null data → no-ref, no throw");
assertEqual(await resolveSelectRef({ mm: { evil: 1 }, locator: 5 }, { knownPaths: known, readText: noRead }), { error: "no-ref" }, "non-string refs are ignored");

// --- resolveSelectRef: data-locatorjs route (start only; end recovered from the file) -------

const APP = `import React from "react";

export default function App() {
  const items = [1, 2];
  return (
    <main className="app">
      <button onClick={() => alert("a > b")} className="primary">
        Save {items.length}
      </button>
      <ul>
        {items.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
      <>
        <img src="x.png" alt="" />
      </>
    </main>
  );
}
`;
const instrumented = (await instrumentJsx(APP, "src/App.jsx")).code;
function truthFor(needle) {
  // The mm the REAL instrumenter stamped on the element whose opening tag begins with `needle`.
  const idx = instrumented.indexOf(needle);
  const m = /data-mm="([^"]+)"/.exec(instrumented.slice(idx));
  return m[1];
}
function startOf(mm) {
  const p = mmRange.parseMm(mm);
  return { line: p.startLine, col: p.startCol };
}

for (const [needle, label] of [["<main", "main"], ["<button", "button (arrow fn + '>' in a string inside its tag)"], ["<ul", "ul"], ["<li", "li inside a .map()"], ["<img", "self-closing img inside a fragment"]]) {
  const truth = truthFor(needle);
  const { line, col } = startOf(truth);
  const r = await resolveSelectRef(
    { locator: `/Users/me/proj/src/App.jsx:${line}:${col}` },
    { knownPaths: ["src/App.jsx"], readText: async () => APP }
  );
  assertEqual(r, { mm: truth, approx: false }, `${label}: the recovered range equals the instrumenter's own`);
}

{
  // The element selected by that range really is the whole element.
  const truth = truthFor("<button");
  const parsed = mmRange.parseMm(truth);
  const text = mmRange.rangeFromMm(APP, parsed).snippet;
  assertEqual(text.startsWith("<button") && text.endsWith("</button>"), true, "the range covers opening tag through closing tag");
}

// readText sees the workspace path, not the disk path
{
  let asked = null;
  await resolveSelectRef({ locator: "/Users/me/proj/src/App.jsx:6:6" }, { knownPaths: ["src/App.jsx"], readText: async (p) => ((asked = p), APP) });
  assertEqual(asked, "src/App.jsx", "the file is read by its workspace path");
}

// Degradations — never fail where a usable selection exists
{
  const r = await resolveSelectRef({ locator: "src/App.jsx:6:6" }, { knownPaths: ["src/App.jsx"], readText: async () => "export default function App() {\n  return (\n    <main className=\"a\"\n  );\n}\n" });
  assertEqual(r.error, undefined, "a file that no longer parses (mid-edit) still resolves to something");
  assertEqual(r.approx, true, "…flagged approximate");
}
{
  const vue = `<template>\n  <button class="x" @click="go">\n    Hi\n  </button>\n</template>\n`;
  const r = await resolveSelectRef({ locator: "src/Comp.vue:2:2" }, { knownPaths: ["src/Comp.vue"], readText: async () => vue });
  assertEqual(r, { mm: "src/Comp.vue:2:2:2:32", approx: true }, "a non-JS file (Vue) selects the opening tag, flagged approximate");
  assertEqual(mmRange.rangeFromMm(vue, mmRange.parseMm(r.mm)).snippet, '<button class="x" @click="go">', "…and that range is exactly the opening tag");
}
{
  const r = await resolveSelectRef({ locator: "src/App.jsx:1:0" }, { knownPaths: ["src/App.jsx"], readText: async () => "plain text, no tag here\nsecond line" });
  assertEqual(r, { mm: "src/App.jsx:1:0:1:23", approx: true }, "a position that isn't a tag falls back to the end of that line");
}
{
  const r = await resolveSelectRef({ locator: "src/App.jsx:1:0" }, { knownPaths: ["src/App.jsx"], readText: async () => { throw new Error("gone"); } });
  assertEqual(r, { error: "unreadable-file", path: "src/App.jsx" }, "a file that can't be read is reported");
}
assertEqual(await resolveSelectRef({ locator: "/x/Unknown.jsx:1:0" }, { knownPaths: known, readText: async () => "" }), { error: "not-in-workspace", path: "/x/Unknown.jsx" }, "a locator path outside the workspace is reported");
assertEqual(await resolveSelectRef({ locator: "nonsense" }, { knownPaths: known, readText: async () => "" }), { error: "unreadable" }, "an unparseable locator is 'unreadable'");

// A hostile, enormous ref is capped before parsing
{
  const r = await resolveSelectRef({ mm: "a".repeat(100000) + ":1:0:1:5" }, { knownPaths: known, readText: noRead });
  assertEqual(r.error !== undefined, true, "a 100KB ref is cut at the cap and refused rather than parsed whole");
}

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll sourceRef.js tests passed.");
}
