// W6.3 (Build Workbench plan) — golden-file tests for lib/preview/instrument.js.
//
// Loads the REAL instrument.js through loadSource.mjs (no pasted copy —
// see that helper's own header for why). Reused from lib/workbench/'s
// __tests__ directory rather than duplicated here: it's a generic
// source-transform utility with nothing workbench-specific in it.
//
// instrument.js's only STATIC import is `parse5` (`import * as parse5
// from "parse5"`), so that's the only thing this file needs to supply
// through loadSource's `imports` map — it's a real, actually-installed
// package, not a stub, loaded here the normal ESM way since this test
// file runs as real ESM (.mjs) regardless of instrument.js's own module
// system quirks. @babel/parser is pulled in by instrument.js via a
// dynamic `await import("@babel/parser")` inside instrumentJsx, which
// loadSource's import-stripping regex only matches static
// `import ... from "spec";` lines and so never touches — it resolves at
// real call time instead, exactly as it would when webpack (or plain
// node, as proven by this file passing) actually runs it. That resolves
// relative to node_modules from this file's own location upward, so it
// needs `npm install` run in frontend/ first (parse5 + @babel/parser are
// this patch's two new dependencies — see package.json).
//
// Run: node frontend/app/lib/preview/__tests__/instrument.test.mjs
import * as parse5 from "parse5";
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

// NOTE: loadSource() resolves its relPath argument relative to
// loadSource.mjs's OWN location (lib/workbench/__tests__/), not to this
// file — hence "../../preview/instrument.js" rather than "../instrument.js".
const { instrumentHtml, instrumentJsx, instrumentSource } = loadSource("../../preview/instrument.js", {
  imports: { parse5 },
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
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

// Pulls every data-mm value out of a string, in document order, without
// caring about instrumentHtml vs instrumentJsx's very different output
// shapes (serialized parse5 tree vs. spliced original source) — both
// just need to contain the literal attribute somewhere.
function mmValues(code) {
  return [...code.matchAll(/data-mm="([^"]+)"/g)].map((m) => m[1]);
}

// --- instrumentHtml ----------------------------------------------------

{
  const html =
    "<!DOCTYPE html>\n" +
    "<html><head><title>x</title><meta charset=\"utf-8\"><style>.a{color:red}</style></head>\n" +
    "<body>\n" +
    '  <div class="a">\n' +
    "    <p>hi</p>\n" +
    '    <img src="x.png">\n' +
    "  </div>\n" +
    "  <script>console.log(1)</script>\n" +
    "</body></html>";

  const result = instrumentHtml(html, "index.html");
  assert(result.instrumented, "instrumentHtml: a plain document reports instrumented:true");

  const values = mmValues(result.code);
  assert(values.some((v) => v.startsWith("index.html:4:")), "instrumentHtml: the div gets a data-mm starting at its real line (4)");
  assert(values.some((v) => v.startsWith("index.html:5:")), "instrumentHtml: the p gets a data-mm starting at its real line (5)");
  assert(values.some((v) => v.startsWith("index.html:6:")), "instrumentHtml: the (unclosed, void) img still gets one");

  for (const skipped of ["html", "head", "title", "meta", "style", "script"]) {
    assert(
      !result.code.includes(`<${skipped} data-mm=`),
      `instrumentHtml: <${skipped}> is on the skip list and never gets data-mm`
    );
  }

  // Contract check: parse5 reports 1-based columns; instrumentHtml must
  // convert to the 0-based contract. "  <div..." — the div's `<` is the
  // 3rd character on its line, i.e. 0-based column 2.
  const divValue = values.find((v) => v.startsWith("index.html:4:"));
  assertEqual(divValue, "index.html:4:2:7:8", "instrumentHtml: div's data-mm is exactly path:line:col:line:col, 0-based columns");
}

{
  // Re-running on already-instrumented output must not double up.
  const once = instrumentHtml("<body><p>hi</p></body>", "a.html");
  const twice = instrumentHtml(once.code, "a.html");
  assertEqual(mmValues(twice.code).length, mmValues(once.code).length, "instrumentHtml: re-instrumenting already-tagged output adds nothing new");
  assertEqual(twice.instrumented, false, "instrumentHtml: a no-op second pass reports instrumented:false");
}

// --- instrumentJsx: host elements, components, self-closing -----------

{
  const src = [
    "function Card({title}) {",
    '  return <div className="card"><span>{title}</span><Icon name="x" /></div>;',
    "}",
  ].join("\n");

  const result = await instrumentJsx(src, "src/Card.jsx");
  assert(result.instrumented, "instrumentJsx: reports instrumented:true when host elements are found");
  assert(result.code.includes('<div data-mm="src/Card.jsx:2:9:2:74" className="card">'), "instrumentJsx: div gets the exact contract value, spliced right after the tag name");
  assert(result.code.includes("<span data-mm="), "instrumentJsx: span (nested host element) is instrumented too");
  assert(!result.code.includes("<Icon data-mm="), "instrumentJsx: capitalized component tag gets nothing (v1 scope, see module header)");

  // Splice correctness: the result must still be valid, re-parseable JSX.
  let reparsed = null;
  try {
    const babelParser = await import("@babel/parser");
    reparsed = babelParser.parse(result.code, { sourceType: "module", plugins: ["jsx", "typescript"] });
  } catch {
    reparsed = null;
  }
  assert(reparsed !== null, "instrumentJsx: spliced output re-parses as valid JSX");
}

{
  // Self-closing host element — no separate closing tag, but still one
  // whole-element range ending at the `/>`.
  const src = 'const el = <img src="x.png" />;';
  const result = await instrumentJsx(src, "a.jsx");
  assert(result.code.includes('<img data-mm="a.jsx:1:11:1:30" src="x.png" />'), "instrumentJsx: self-closing element's range ends at its own `/>`, not some other element's");
}

// --- Fragments -----------------------------------------------------------

{
  const src = [
    "function List() {",
    "  return (",
    "    <>",
    "      <div>a</div>",
    "      <div>b</div>",
    "    </>",
    "  );",
    "}",
  ].join("\n");
  const result = await instrumentJsx(src, "a.jsx");
  assertEqual(mmValues(result.code).length, 2, "instrumentJsx: a fragment wrapper gets nothing, but both children inside it do");
}

// --- Nested .map() -------------------------------------------------------

{
  const src = [
    "function Items({items}) {",
    "  return (",
    "    <ul>",
    "      {items.map((item) => (",
    "        <li key={item.id}>{item.name}</li>",
    "      ))}",
    "    </ul>",
    "  );",
    "}",
  ].join("\n");
  const result = await instrumentJsx(src, "a.jsx");
  assert(result.code.includes("<ul data-mm="), "instrumentJsx: the outer <ul> is instrumented");
  assert(result.code.includes("<li data-mm="), "instrumentJsx: an element returned from inside a .map() callback is still found and instrumented");
}

// --- Template literals in an attribute value ------------------------------

{
  // eslint-disable-next-line no-template-curly-in-string
  const src = "const el = <div title={`count: ${n}`}>{n}</div>;";
  const result = await instrumentJsx(src, "a.jsx");
  assert(result.instrumented, "instrumentJsx: a template literal inside a JSX attribute doesn't break parsing");
  assert(result.code.includes("<div data-mm="), "instrumentJsx: the element carrying the template-literal attribute is still instrumented");
  assert(result.code.includes("title={`count: ${n}`}"), "instrumentJsx: the template literal itself is untouched in the output");
}

// --- TSX (typescript plugin engaged) --------------------------------------

{
  const src = [
    "function Badge({count}: {count: number}) {",
    "  const label = count > 9 ? \"9+\" : String(count);",
    '  return <span className="badge">{label}</span>;',
    "}",
  ].join("\n");
  const result = await instrumentJsx(src, "src/Badge.tsx");
  assert(result.instrumented, "instrumentJsx: TSX with real type annotations parses fine (typescript plugin is always on)");
  assert(result.code.includes("<span data-mm=\"src/Badge.tsx:3:"), "instrumentJsx: the host element in a .tsx file is instrumented with the .tsx path");
}

// --- Syntax-error file: never break the preview ---------------------------

{
  const broken = "function f() { return <div><span></div>; }"; // mismatched closing tag
  const result = await instrumentJsx(broken, "broken.jsx");
  assertEqual(result.code, broken, "instrumentJsx: on a syntax error, the ORIGINAL code comes back untouched");
  assertEqual(result.instrumented, false, "instrumentJsx: on a syntax error, instrumented is false");
  assertEqual(result.note, "preview inspector unavailable for this file", "instrumentJsx: on a syntax error, the note explains why (never a thrown error)");
}

// --- instrumentSource dispatcher -----------------------------------------

{
  const htmlResult = await instrumentSource("<body><p>hi</p></body>", "index.html");
  assert(htmlResult.instrumented, "instrumentSource: routes .html to instrumentHtml");

  const jsxResult = await instrumentSource('const el = <div className="x">y</div>;', "src/App.jsx");
  assert(jsxResult.instrumented, "instrumentSource: routes .jsx to instrumentJsx");

  const tsxResult = await instrumentSource('const el = <div className="x">y</div>;', "src/App.tsx");
  assert(tsxResult.instrumented, "instrumentSource: routes .tsx to instrumentJsx too");

  const cssResult = await instrumentSource(".x { color: red; }", "src/styles.css");
  assertEqual(cssResult, { code: ".x { color: red; }", instrumented: false, note: null }, "instrumentSource: an extension it doesn't know is a no-op, not a failure (no note)");
}

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
