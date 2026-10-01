// W8.6 (Build Workbench plan) — tests for lib/preview/wireframeInspect.js.
// Run: node frontend/app/lib/preview/__tests__/wireframeInspect.test.mjs
//
// Same approach as inspectorRuntime.test.mjs: the REAL source files are
// loaded through loadSource.mjs (no pasted copies), and the two
// collaborators that need parse5 are the real ones too — instrument.js's
// instrumentSource() and inspectorRuntime.js's injectInspectorRuntime() —
// so "click a button in the wireframe, get that button's source" is
// proven end to end rather than against stand-ins. The stand-ins below
// (throwing / non-injecting fakes) exist only to reach buildWireframeFrame's
// failure branches, which the real ones almost never take.
//
// wireframeInspect.js's own imports (mmRange.js, elementRef.js) are
// dependency-free modules, so they're loaded for real and handed over
// through `imports` under the exact specifiers the file uses.
import * as parse5 from "parse5";
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

const mmRange = loadSource("../../preview/mmRange.js");
const elementRef = loadSource("../../workbench/elementRef.js");
const { instrumentSource } = loadSource("../../preview/instrument.js", { imports: { parse5 } });
const { injectInspectorRuntime } = loadSource("../../preview/inspectorRuntime.js", { imports: { parse5 } });

const {
  WIREFRAME_MM_PATH,
  MAX_SNIPPET_CHARS,
  buildWireframeFrame,
  selectionFromMessage,
  buildWireframeEditInstruction,
} = loadSource("../../preview/wireframeInspect.js", {
  imports: { "./mmRange": mmRange, "../workbench/elementRef": elementRef },
});

let failures = 0;
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}
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

const NONCE = "test-nonce-123";
const real = { instrument: instrumentSource, injectRuntime: injectInspectorRuntime };

const LOGIN = [
  "<!doctype html>",
  "<html>",
  "<head><title>Login</title></head>",
  "<body>",
  '  <h1 class="title">Sign in</h1>',
  '  <button class="btn primary" id="go">Log in</button>',
  "</body>",
  "</html>",
].join("\n");
const BUTTON_SRC = '<button class="btn primary" id="go">Log in</button>';

// ---------------------------------------------------------------------------
// constants
// ---------------------------------------------------------------------------
assertEqual(WIREFRAME_MM_PATH, "wireframe.html", "the virtual path every wireframe data-mm carries");
assertEqual(MAX_SNIPPET_CHARS, 1500, "snippet cap");

// ---------------------------------------------------------------------------
// buildWireframeFrame — with the real instrumenter and the real injector
// ---------------------------------------------------------------------------
{
  const frame = await buildWireframeFrame({ html: LOGIN, nonce: NONCE, ...real });
  assertEqual(frame.inspectable, true, "a normal wireframe is inspectable");
  assertEqual(frame.note, null, "no note on success");
  assert(frame.srcDoc.includes("__mmInspectorInstalled__"), "the runtime is in the srcDoc");
  assert(frame.srcDoc.includes(JSON.stringify(NONCE)), "this build's nonce is baked into the runtime");
  assert(frame.srcDoc.includes('data-mm="wireframe.html:'), "elements are stamped with the virtual path");
  assert(!LOGIN.includes("data-mm"), "(the original text itself never carries data-mm)");

  // The ranges in the srcDoc point back into the ORIGINAL text exactly.
  const ranges = mmRange.extractMmRanges(frame.srcDoc);
  const slices = ranges.map((r) => mmRange.rangeFromMm(LOGIN, r).snippet);
  assert(slices.includes(BUTTON_SRC), "the button's data-mm range slices exactly the button's source out of the original");
  assert(
    slices.includes('<h1 class="title">Sign in</h1>'),
    "so does the heading's"
  );
  assert(ranges.every((r) => r.path === WIREFRAME_MM_PATH), "every range carries the wireframe path and no other");
}

// Empty / unusable input: nothing to show, never throws.
for (const bad of ["", "   \n\t ", undefined, null, 42, {}]) {
  const frame = await buildWireframeFrame({ html: bad, nonce: NONCE, ...real });
  assertEqual(frame, { srcDoc: "", inspectable: false, note: null }, `unusable html ${JSON.stringify(bad)} -> empty frame`);
}

// A fragment (no <html>/<head>) still gets the runtime: parse5 supplies the implied head.
{
  const frame = await buildWireframeFrame({ html: '<p id="x">hi</p>', nonce: NONCE, ...real });
  assertEqual(frame.inspectable, true, "a bare fragment is inspectable");
  assert(frame.srcDoc.includes("__mmInspectorInstalled__"), "runtime injected into a fragment's implied head");
}

// Plain text has nothing to instrument: shown as it was, no note (not a failure).
{
  const frame = await buildWireframeFrame({ html: "just some words", nonce: NONCE, ...real });
  assertEqual(frame, { srcDoc: "just some words", inspectable: false, note: null }, "text with no elements is returned byte for byte, silently");
}

// Failure branches: the preview must never get WORSE than the original text.
{
  const boom = async () => {
    throw new Error("parse exploded");
  };
  const frame = await buildWireframeFrame({ html: LOGIN, nonce: NONCE, instrument: boom, injectRuntime: injectInspectorRuntime });
  assertEqual(frame.srcDoc, LOGIN, "instrument throws -> the original, untouched");
  assertEqual(frame.inspectable, false, "instrument throws -> not inspectable");
  assert(typeof frame.note === "string" && frame.note.length > 0, "instrument throws -> says so");
}
{
  const frame = await buildWireframeFrame({
    html: LOGIN,
    nonce: NONCE,
    instrument: async (code) => ({ code, instrumented: false, note: "preview inspector unavailable for this file" }),
    injectRuntime: injectInspectorRuntime,
  });
  assertEqual(
    frame,
    { srcDoc: LOGIN, inspectable: false, note: "preview inspector unavailable for this file" },
    "instrument reports it could not -> original, and its own note passes through"
  );
}
{
  const frame = await buildWireframeFrame({ html: LOGIN, nonce: NONCE, instrument: async () => null, injectRuntime: injectInspectorRuntime });
  assertEqual([frame.srcDoc, frame.inspectable, frame.note], [LOGIN, false, null], "instrument returns nothing -> original, no note");
}
{
  const frame = await buildWireframeFrame({
    html: LOGIN,
    nonce: NONCE,
    instrument: async () => ({ code: 7, instrumented: true, note: null }),
    injectRuntime: injectInspectorRuntime,
  });
  assertEqual([frame.srcDoc, frame.inspectable], [LOGIN, false], "instrument claims success with non-string code -> original");
}
{
  const frame = await buildWireframeFrame({
    html: LOGIN,
    nonce: NONCE,
    instrument: instrumentSource,
    injectRuntime: () => {
      throw new Error("inject exploded");
    },
  });
  assertEqual([frame.srcDoc, frame.inspectable], [LOGIN, false], "injector throws -> the ORIGINAL, not the instrumented copy");
  assert(!frame.srcDoc.includes("data-mm"), "...and nothing stamped leaks into the fallback");
}
{
  // injectInspectorRuntime() hands its input back untouched when it can't inject.
  const frame = await buildWireframeFrame({ html: LOGIN, nonce: NONCE, instrument: instrumentSource, injectRuntime: (code) => code });
  assertEqual([frame.srcDoc, frame.inspectable], [LOGIN, false], "injector returns its input without the runtime -> not inspectable, original shown");
  assert(typeof frame.note === "string", "...with a note");
}
{
  const calls = [];
  await buildWireframeFrame({
    html: LOGIN,
    nonce: NONCE,
    instrument: async (code, path) => {
      calls.push(["instrument", path]);
      return instrumentSource(code, path);
    },
    injectRuntime: (code, nonce) => {
      calls.push(["inject", nonce]);
      return injectInspectorRuntime(code, nonce);
    },
  });
  assertEqual(calls, [["instrument", "wireframe.html"], ["inject", NONCE]], "collaborators get the virtual path and the nonce, in that order");
}

// Positions survive the things that move columns/lines around.
{
  const html = '<p>caf\u00e9 \ud83d\ude00 <b id="b">bold</b></p>\n\t<i>tabbed</i>';
  const frame = await buildWireframeFrame({ html, nonce: NONCE, ...real });
  const slices = mmRange.extractMmRanges(frame.srcDoc).map((r) => mmRange.rangeFromMm(html, r).snippet);
  assert(slices.includes('<b id="b">bold</b>'), "an element after an accented letter and an emoji on the same line maps exactly");
  assert(slices.includes("<i>tabbed</i>"), "an element after a tab maps exactly");
}
{
  const html = LOGIN.replace(/\n/g, "\r\n");
  const frame = await buildWireframeFrame({ html, nonce: NONCE, ...real });
  const slices = mmRange.extractMmRanges(frame.srcDoc).map((r) => mmRange.rangeFromMm(html, r).snippet);
  assert(slices.includes(BUTTON_SRC), "CRLF line endings: the button still maps exactly");
}

// ---------------------------------------------------------------------------
// selectionFromMessage — end to end: frame -> message -> selection
// ---------------------------------------------------------------------------
function messageFor(snippetStart, html, extra = {}) {
  // What the injected runtime would post for a click on the element whose
  // source starts with `snippetStart`: the mm it read off the node.
  return (async () => {
    const frame = await buildWireframeFrame({ html, nonce: NONCE, ...real });
    const range = mmRange
      .extractMmRanges(frame.srcDoc)
      .find((r) => mmRange.rangeFromMm(html, r).snippet.startsWith(snippetStart));
    return {
      source: "minime-preview",
      nonce: NONCE,
      type: "minime:select",
      mm: range.mm,
      tag: "button",
      classes: ["btn", "primary"],
      textPreview: "Log in",
      styles: { color: "rgb(0, 0, 0)", fontSize: "14px" },
      instanceCount: 1,
      dynamic: false,
      ...extra,
    };
  })();
}

{
  const msg = await messageFor("<button", LOGIN);
  const sel = selectionFromMessage(msg, LOGIN);
  assertEqual(sel.snippet, BUTTON_SRC, "a click on the button selects exactly the button's source");
  assertEqual(sel.line, 6, "the line is where the element starts (1-based)");
  assertEqual(sel.snippetTruncated, false, "short snippet is not truncated");
  assertEqual(sel.element.tag, "button", "element description comes through");
  assertEqual(sel.element.classes, ["btn", "primary"], "classes come through");
  assertEqual(sel.mm, msg.mm, "the mm is kept");
}

// The path check: a page can't point the edit bar at text it didn't come from.
{
  const msg = await messageFor("<button", LOGIN);
  const forged = { ...msg, mm: msg.mm.replace("wireframe.html", "src/App.jsx") };
  assertEqual(selectionFromMessage(forged, LOGIN), null, "a select carrying another file's path is ignored");
  assertEqual(selectionFromMessage({ ...msg, mm: "wireframe.html:not:numbers" }, LOGIN), null, "a malformed mm is ignored");
  assertEqual(selectionFromMessage({ ...msg, mm: "" }, LOGIN), null, "an empty mm is ignored");
  assertEqual(selectionFromMessage({ ...msg, mm: 42 }, LOGIN), null, "a non-string mm is ignored");
  assertEqual(selectionFromMessage(null, LOGIN), null, "null message is ignored");
  assertEqual(selectionFromMessage(undefined, LOGIN), null, "undefined message is ignored");
  assertEqual(selectionFromMessage({ type: "minime:select" }, LOGIN), null, "no mm at all is ignored");
}

// Untrusted payloads are capped by elementFromMessage before they get here.
{
  const msg = await messageFor("<button", LOGIN, {
    tag: "x".repeat(500),
    textPreview: "y".repeat(5000),
    classes: Array.from({ length: 200 }, (_, i) => `c${i}`),
    styles: { color: 7, background: "z".repeat(1000), evil: "should not survive" },
  });
  const sel = selectionFromMessage(msg, LOGIN);
  assert(sel.element.tag.length <= 40, "tag is length-capped");
  assert(sel.element.textPreview.length <= 200, "text preview is length-capped");
  assert(sel.element.classes.length <= 20, "class list is capped");
  assert(!("evil" in sel.element.styles) && !("color" in sel.element.styles), "unknown / non-string style keys are dropped");
  assert(sel.element.styles.background.length <= 120, "style values are length-capped");
}

// A huge element (the whole <body>, say) is cut at the cap and flagged.
{
  const big = "<body>" + "<p>row</p>".repeat(400) + "</body>";
  const msg = await messageFor("<body", big);
  const sel = selectionFromMessage(msg, big);
  assertEqual(sel.snippet.length, MAX_SNIPPET_CHARS, "snippet is cut at the cap");
  assertEqual(sel.snippetTruncated, true, "and says it was cut");
  assert(big.startsWith(sel.snippet), "the kept part is the START of the element");
}

// The text the frame was built from is what positions are read against;
// a different/shorter text must degrade, not throw.
{
  const msg = await messageFor("<button", LOGIN);
  let threw = false;
  let sel;
  try {
    sel = selectionFromMessage(msg, "<p>tiny</p>");
  } catch {
    threw = true;
  }
  assert(!threw, "a stale sourceHtml (shorter than the range) does not throw");
  assert(sel && typeof sel.snippet === "string" && sel.snippet.length <= "<p>tiny</p>".length, "...and gives back at most what is there");
  const none = selectionFromMessage(msg, undefined);
  assertEqual(none.snippet, "", "a non-string sourceHtml gives an empty snippet");
}

// ---------------------------------------------------------------------------
// buildWireframeEditInstruction
// ---------------------------------------------------------------------------
assertEqual(
  buildWireframeEditInstruction({ screenLabel: "Login", instruction: "make the button bigger" }),
  'For the "Login" wireframe: make the button bigger',
  "unscoped, labelled: exactly what Send edit sent before W8.6"
);
assertEqual(buildWireframeEditInstruction({ instruction: "  make it blue  " }), "make it blue", "unscoped, no label: the trimmed instruction");
assertEqual(buildWireframeEditInstruction({ screenLabel: "X", instruction: "go", selection: null }), 'For the "X" wireframe: go', "a null selection is the unscoped message");
assertEqual(buildWireframeEditInstruction({ instruction: undefined }), "", "no instruction -> empty, not 'undefined'");

{
  const msg = await messageFor("<button", LOGIN);
  const selection = selectionFromMessage(msg, LOGIN);
  const text = buildWireframeEditInstruction({ screenLabel: "Login", instruction: "make it bigger", selection });
  const lines = text.split("\n");
  assertEqual(lines[0], 'For the "Login" wireframe: make it bigger', "scoped: starts with the same head line");
  assert(text.includes("Apply this to the selected element only"), "scoped: says to touch only that element");
  assert(text.includes('<button class="btn primary">'), "scoped: describes what it rendered as");
  assert(text.includes("Computed style: color: rgb(0, 0, 0); fontSize: 14px"), "scoped: includes the computed style");
  assert(text.includes("Its current HTML, from line 6:"), "scoped: names where the source starts");
  assert(text.includes("```html\n" + BUTTON_SRC + "\n```"), "scoped: the exact source in an html fence");
  assert(!text.includes("(truncated)"), "scoped: not marked truncated when it isn't");
}

{
  const selection = {
    mm: "wireframe.html:1:0:1:9",
    element: { tag: "pre", classes: [], textPreview: "", styles: {}, dynamic: false, instanceCount: 1 },
    snippet: "<pre>```js\ncode\n```</pre>",
    snippetTruncated: false,
    line: 1,
  };
  const text = buildWireframeEditInstruction({ instruction: "tidy", selection });
  assert(text.includes("````html\n<pre>```js"), "a snippet containing ``` gets a longer fence so it can't close early");
  assert(text.trimEnd().endsWith("````"), "...and the same longer fence closes it");
}
{
  const selection = {
    mm: "wireframe.html:1:0:1:9",
    element: { tag: "div", classes: [], textPreview: "", styles: {}, dynamic: true, instanceCount: 3 },
    snippet: "<div>…</div>",
    snippetTruncated: true,
    line: 12,
  };
  const text = buildWireframeEditInstruction({ screenLabel: "S", instruction: "x", selection });
  assert(text.includes("Its current HTML (truncated), from line 12:"), "a truncated snippet says so");
  assert(text.includes("renders 3 times") && text.includes("created at runtime"), "instance count and dynamic notes are carried over");
}
{
  const selection = {
    mm: "wireframe.html:1:0:1:9",
    element: { tag: "div", classes: [], textPreview: "", styles: {}, dynamic: false, instanceCount: 1 },
    snippet: "",
    snippetTruncated: false,
    line: 1,
  };
  const text = buildWireframeEditInstruction({ instruction: "x", selection });
  assert(!text.includes("```"), "an empty snippet adds no code block");
  assert(text.includes("Apply this to the selected element only"), "...but is still scoped");
}

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll wireframeInspect tests passed.");
