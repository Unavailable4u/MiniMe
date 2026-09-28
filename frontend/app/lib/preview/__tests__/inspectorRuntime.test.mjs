// W6.4 (Build Workbench plan) — tests for lib/preview/inspectorRuntime.js.
//
// Two tiers, same split consoleBridge.test.mjs already established (see
// that file's own header for why): structural/string checks on
// buildInspectorScript()'s OUTPUT (no DOM needed), plus — new here,
// because "click reports the right mm" is this step's own literal
// "Done when" and deserves more than a string match — REAL EXECUTION of
// that same script against a minimal, purpose-built fake DOM, using
// Node's built-in `vm` module (no new dependency: vm ships with Node).
//
// The fake DOM below is NOT a general-purpose jsdom replacement — it
// implements exactly the handful of methods inspectorRuntime.js's own
// script actually calls (closest() for '[data-mm]' only,
// querySelectorAll('[data-mm="..."]') for the exact-match case,
// getAttribute, getBoundingClientRect, a stub getComputedStyle,
// addEventListener that just RECORDS handlers so this file can invoke
// them directly instead of needing a real event loop). Anything the
// real script called that this fake DOM didn't implement would throw
// during the vm.Script.runInContext() calls below, so a passing run
// here is real evidence the script only ever touches the DOM surface
// this test accounts for.
//
// Run: node frontend/app/lib/preview/__tests__/inspectorRuntime.test.mjs
import vm from "node:vm";
import * as parse5 from "parse5";
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";
import { instrumentHtml } from "../../preview/instrument.js";

const { buildInspectorScript, injectInspectorRuntime, MAX_TEXT_PREVIEW_LENGTH, MAX_HTML_PREVIEW_LENGTH, MAX_ANCESTORS } = loadSource(
  "../../preview/inspectorRuntime.js",
  { imports: { parse5 } }
);

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

// ===========================================================================
// Tier 1 — structural checks on the generated script string
// ===========================================================================

const script = buildInspectorScript("abc123");

assert(script.includes('"abc123"') || script.includes("'abc123'"), "the nonce is embedded, quoted as a string literal");
assert(!script.includes("eval("), "never eval (plan's own security note, same as consoleBridge.js)");
assert(!script.includes("new Function("), "never the Function constructor either");
assert(!/<\/script/i.test(script), "never contains a literal '</script' — would truncate the injected tag");
assert(script.includes("'minime:select'"), "posts the plan's own minime:select message type");
assert(script.includes("'minime:inspectExited'"), "notifies the parent when Esc exits inspect mode locally");
assert(script.includes("'minime:inspect'"), "listens for the parent's minime:inspect toggle");
assert(script.includes("'minime:highlight'"), "listens for the parent's minime:highlight");
assert(script.includes("addEventListener('click', mmOnClick, true)"), "the click listener is registered on the CAPTURE phase, per the plan's own line");
assert(script.includes("event.preventDefault()") && script.includes("event.stopPropagation()"), "a click while inspecting is prevented/stopped so the page's own handler never fires");
assert(script.includes("'Escape'"), "Esc is wired to exit inspect mode");
assert(script.includes("__mmInspectorInstalled__"), "guards against double-installation if injected twice");
assert(script.includes("closest('[data-mm]')"), "resolution walks up to the nearest element carrying data-mm — the 'nearest static ancestor' rule from the plan");

const hostileNonce = `x"; window.__pwned__=1; //`;
const hostileScript = buildInspectorScript(hostileNonce);
const encoded = JSON.stringify(hostileNonce);
assert(hostileScript.includes(encoded), "a nonce with a quote is safely JSON-encoded");
assertEqual(
  hostileScript.split(encoded).join("").includes("__pwned__"),
  false,
  "outside its one safely-quoted occurrence, a hostile nonce's payload never appears in the output"
);

assert(typeof MAX_TEXT_PREVIEW_LENGTH === "number" && MAX_TEXT_PREVIEW_LENGTH > 0, "MAX_TEXT_PREVIEW_LENGTH is a real positive cap");
assertEqual(MAX_HTML_PREVIEW_LENGTH, 1024, "MAX_HTML_PREVIEW_LENGTH matches the plan's own explicit htmlPreview(≤1KB)");
assert(typeof MAX_ANCESTORS === "number" && MAX_ANCESTORS > 0, "MAX_ANCESTORS is a real positive cap");

// ===========================================================================
// Tier 2 — real execution against a minimal fake DOM (Node's `vm` module)
// ===========================================================================

// A plain, hand-rolled element — NOT jsdom. Implements exactly what
// inspectorRuntime.js's script calls on an element: getAttribute,
// closest('[data-mm]'), getBoundingClientRect, className/tagName,
// textContent, outerHTML. See this file's own header for why a fake
// this narrow is the right amount of test infrastructure here.
class FakeElement {
  constructor(tag, attrs = {}, children = [], rect = { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }) {
    this.tagName = tag.toUpperCase();
    this._attrs = attrs;
    this.className = attrs.class || "";
    this.children = children;
    this._rect = rect;
    for (const c of children) c.parentElement = this;
    this.parentElement = null;
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
  }
  closest(selector) {
    const m = /^\[([a-zA-Z-]+)\]$/.exec(selector);
    if (!m) throw new Error("fake closest() only supports '[attr]' selectors, got: " + selector);
    let node = this;
    while (node) {
      if (node.getAttribute(m[1]) != null) return node;
      node = node.parentElement;
    }
    return null;
  }
  getBoundingClientRect() {
    return this._rect;
  }
  get textContent() {
    return this._text || this.children.map((c) => c.textContent).join("");
  }
  get outerHTML() {
    return `<${this.tagName.toLowerCase()}>${this.textContent}</${this.tagName.toLowerCase()}>`;
  }
  // Flattens this element and every descendant, depth-first — backs the
  // fake document's querySelectorAll/querySelector below.
  *walk() {
    yield this;
    for (const c of this.children) yield* c.walk();
  }
}

/**
 * Builds a fresh sandbox (fake window/document + a `posted` array the
 * test reads afterward) and runs buildInspectorScript(nonce) in it via
 * vm.createContext/vm.Script — REAL execution of the REAL generated
 * source, not a reimplementation of its logic. `handlers` captures
 * every window.addEventListener call by event name so the test can
 * invoke them directly (there is no real browser event loop to dispatch
 * through in a vm context).
 */
function makeSandbox(root, nonce) {
  const posted = [];
  const handlers = {};
  const sandbox = {
    window: {
      parent: { postMessage: (msg) => posted.push(msg) },
      addEventListener: (type, fn) => {
        handlers[type] = fn;
      },
      getComputedStyle: () => ({
        color: "rgb(0, 0, 0)",
        backgroundColor: "rgb(255, 255, 255)",
        fontSize: "16px",
        padding: "8px",
        margin: "4px",
        display: "block",
      }),
    },
    document: {
      body: {}, // truthy -- just needs to exist for mmEnsureOverlay()'s guard
      documentElement: { appendChild: () => {} },
      createElement: () => ({ style: {} }), // the overlay/label boxes -- never asserted on directly, only that creating them doesn't throw
      querySelector: (selector) => {
        const m = /^\[data-mm="(.*)"\]$/.exec(selector);
        if (!m) throw new Error("fake querySelector only supports the exact-match data-mm form, got: " + selector);
        for (const el of root.walk()) if (el.getAttribute("data-mm") === m[1].replace(/\\"/g, '"')) return el;
        return null;
      },
      querySelectorAll: (selector) => {
        const m = /^\[data-mm="(.*)"\]$/.exec(selector);
        if (!m) throw new Error("fake querySelectorAll only supports the exact-match data-mm form, got: " + selector);
        const wanted = m[1].replace(/\\"/g, '"');
        return [...root.walk()].filter((el) => el.getAttribute("data-mm") === wanted);
      },
    },
  };
  const context = vm.createContext(sandbox);
  new vm.Script(buildInspectorScript(nonce)).runInContext(context);
  return { handlers, posted, window: sandbox.window, document: sandbox.document };
}

function fakeEvent(target, extra = {}) {
  return { target, preventDefault() {}, stopPropagation() {}, ...extra };
}

function postFromParent(sandbox, nonce, msg) {
  sandbox.handlers.message({ source: sandbox.window.parent, data: { source: "minime-parent", nonce, ...msg } });
}

// A small static tree: an instrumented <div> (mm=D) containing a <span>
// with NO data-mm of its own (as if it were inserted at runtime by the
// page's own script, per the plan's "elements created at runtime (no
// data-mm) resolve to the nearest static ancestor" rule) and a second,
// separately-instrumented <button> (mm=B). `walk()`-based lookups make
// this tree usable by the fake document above without any separate
// registration step.
const spanNoMm = new FakeElement("span", {}, [], { x: 0, y: 0, top: 0, left: 0, right: 40, bottom: 20, width: 40, height: 20 });
spanNoMm._text = "hi";
const divMm = new FakeElement(
  "div",
  { "data-mm": "app.html:2:0:5:6", class: "card highlighted" },
  [spanNoMm],
  { x: 10, y: 10, top: 10, left: 10, right: 210, bottom: 110, width: 200, height: 100 }
);
const buttonMm = new FakeElement("button", { "data-mm": "app.html:6:0:6:30", class: "btn-primary" }, [], {
  x: 0,
  y: 200,
  top: 200,
  left: 0,
  right: 120,
  bottom: 240,
  width: 120,
  height: 40,
});
const root = new FakeElement("div", {}, [divMm, buttonMm]);

{
  const s = makeSandbox(root, "n1");
  postFromParent(s, "n1", { type: "minime:inspect", on: true });
  s.handlers.click(fakeEvent(spanNoMm));
  assertEqual(s.posted.length, 1, "a click while inspecting posts exactly one message");
  const msg = s.posted[0];
  assertEqual(msg.type, "minime:select", "the posted message is a minime:select");
  assertEqual(msg.nonce, "n1", "the message carries this build's own nonce");
  assertEqual(msg.mm, "app.html:2:0:5:6", "clicking the un-instrumented span resolves to its instrumented PARENT's mm — the exact plan requirement");
  assertEqual(msg.dynamic, true, "flagged dynamic, since the actual click target had no data-mm of its own");
  assertEqual(msg.tag, "div", "tag reflects the RESOLVED element (div), not the click target (span)");
  assertEqual(msg.classes, ["card", "highlighted"], "classes are split into an array");
  assertEqual(msg.rect, { x: 10, y: 10, top: 10, left: 10, right: 210, bottom: 110, width: 200, height: 100 }, "rect is the resolved element's own bounding box");
  assertEqual(msg.styles.background, "rgb(255, 255, 255)", "styles.background comes from computed backgroundColor");
  assertEqual(msg.instanceCount, 1, "only one element in the document carries this exact mm");
  assertEqual(msg.ancestors, [], "no data-mm'd ANCESTOR above the div itself in this tree (root carries none)");
}

{
  // Clicking an element that already HAS its own data-mm (the button) —
  // dynamic:false, tag/classes/mm all describe the button directly, not
  // some ancestor.
  const s = makeSandbox(root, "n2");
  postFromParent(s, "n2", { type: "minime:inspect", on: true });
  s.handlers.click(fakeEvent(buttonMm));
  const msg = s.posted[0];
  assertEqual(msg.mm, "app.html:6:0:6:30", "a click directly on an instrumented element resolves to itself");
  assertEqual(msg.dynamic, false, "not flagged dynamic — the click target WAS the resolved element");
  assertEqual(msg.tag, "button", "tag is the button's own");
}

{
  // Not inspecting -> a click is a plain no-op, nothing posted, nothing
  // prevented on the page's own behavior.
  const s = makeSandbox(root, "n3");
  s.handlers.click(fakeEvent(buttonMm));
  assertEqual(s.posted, [], "a click while inspect mode is OFF posts nothing at all");
}

{
  // instanceCount > 1: two elements sharing the exact same mm (as a
  // .map() render would produce multiple DOM copies of one template).
  const dupA = new FakeElement("li", { "data-mm": "list.html:3:2:3:20" }, [], { x: 0, y: 0, top: 0, left: 0, right: 10, bottom: 10, width: 10, height: 10 });
  const dupB = new FakeElement("li", { "data-mm": "list.html:3:2:3:20" }, [], { x: 0, y: 20, top: 20, left: 0, right: 10, bottom: 30, width: 10, height: 10 });
  const listRoot = new FakeElement("ul", {}, [dupA, dupB]);
  const s = makeSandbox(listRoot, "n4");
  postFromParent(s, "n4", { type: "minime:inspect", on: true });
  s.handlers.click(fakeEvent(dupA));
  assertEqual(s.posted[0].instanceCount, 2, "instanceCount counts every element sharing this exact data-mm value, not just the one clicked");
}

{
  // Esc: exits inspect mode locally AND tells the parent, so the
  // toolbar's own toggle can resync — a plain "handled internally" exit
  // would leave the parent's crosshair stuck showing 'on'.
  const s = makeSandbox(root, "n5");
  postFromParent(s, "n5", { type: "minime:inspect", on: true });
  s.handlers.keydown({ key: "Escape" });
  assertEqual(s.posted.length, 1, "Esc posts exactly one message");
  assertEqual(s.posted[0].type, "minime:inspectExited", "...and it's the inspectExited notification");
  s.handlers.click(fakeEvent(buttonMm));
  assertEqual(s.posted.length, 1, "after Esc, inspect mode is truly off -- a further click posts nothing new");
}

{
  // Ancestor chain: a deeper tree where BOTH the immediate parent and an
  // outer grandparent carry their own data-mm.
  const inner = new FakeElement("span", { "data-mm": "nested.html:3:4:3:20" }, [], { x: 0, y: 0, top: 0, left: 0, right: 5, bottom: 5, width: 5, height: 5 });
  const mid = new FakeElement("div", { "data-mm": "nested.html:2:2:4:8" }, [inner]);
  const outer = new FakeElement("section", { "data-mm": "nested.html:1:0:5:10" }, [mid]);
  const s = makeSandbox(outer, "n6");
  postFromParent(s, "n6", { type: "minime:inspect", on: true });
  s.handlers.click(fakeEvent(inner));
  assertEqual(s.posted[0].ancestors, ["nested.html:2:2:4:8", "nested.html:1:0:5:10"], "ancestors lists every data-mm'd ANCESTOR, nearest first, not including the clicked element's own mm");
}

{
  // Wire-format check for the message payload the parent -> frame
  // direction uses to verify its own security, unrelated to the mm
  // resolution logic above: an inspect toggle with the WRONG nonce is
  // silently ignored (this is what stops a stale/forged parent message
  // from a previous build from still being able to flip inspect mode on
  // in a fresh iframe instance).
  const s = makeSandbox(root, "n7");
  postFromParent(s, "wrong-nonce", { type: "minime:inspect", on: true });
  s.handlers.click(fakeEvent(buttonMm));
  assertEqual(s.posted, [], "a minime:inspect message with the wrong nonce is ignored -- inspect mode never actually turned on");
}

{
  // minime:highlight with an unknown mm must not throw (there's nothing
  // asserted about the (untested) visual box here -- only that this
  // path never crashes the runtime, since a stale/renamed mm reaching
  // this is entirely plausible once W6.5 starts sending it on ordinary
  // cursor movement).
  const s = makeSandbox(root, "n8");
  let threw = false;
  try {
    postFromParent(s, "n8", { type: "minime:highlight", mm: "does-not-exist" });
  } catch (e) {
    threw = true;
  }
  assert(!threw, "minime:highlight for an mm with no matching element never throws");
}

// ===========================================================================
// Tier 3 — injectInspectorRuntime, and the same golden fixture W6.3's own
// instrument.test.mjs already established (data-mm survives the full
// instrument -> inject -> serialize round trip byte for byte)
// ===========================================================================

{
  const fixture =
    "<!DOCTYPE html>\n" +
    "<html><head><title>x</title><meta charset=\"utf-8\"><style>.a{color:red}</style></head>\n" +
    "<body>\n" +
    '  <div class="a">\n' +
    "    <p>hi</p>\n" +
    '    <img src="x.png">\n' +
    "  </div>\n" +
    "  <script>console.log(1)</script>\n" +
    "</body></html>";

  const instrumented = instrumentHtml(fixture, "index.html");
  const withInspector = injectInspectorRuntime(instrumented.code, "golden-nonce");

  // Same golden value instrument.test.mjs's own test already asserts
  // for this exact fixture -- confirms injectInspectorRuntime's own
  // parse5 round trip never disturbs a data-mm value W6.3 already
  // pinned down.
  assert(withInspector.includes('data-mm="index.html:4:2:7:8"'), "the div's W6.3 golden data-mm value survives injectInspectorRuntime's parse+serialize round trip unchanged");

  const doc = parse5.parse(withInspector, { sourceCodeLocationInfo: false });
  const scripts = [];
  (function walk(node) {
    if (node.tagName === "script") scripts.push((node.childNodes || []).map((c) => c.value || "").join(""));
    for (const child of node.childNodes || []) walk(child);
  })(doc);
  assert(scripts.length >= 1 && scripts[0].includes("__mmInspectorInstalled__"), "the inspector script is injected as the FIRST script in document order");
}

{
  // Composability with the W6.2 console bridge -- PreviewPane.jsx calls
  // both injectors on the same html/nonce, one after the other.
  const html = "<html><head></head><body><script>1;</script></body></html>";
  // A tiny stand-in for injectConsoleBridge's own output shape (a
  // <script> containing a recognizable marker), so this test doesn't
  // need to import consoleBridge.js just to prove ORDER/composability.
  const withConsoleMarker = html.replace("<head>", "<head><script>/* __CONSOLE_MARKER__ */</script>");
  const combined = injectInspectorRuntime(withConsoleMarker, "n9");
  const firstScriptStart = combined.indexOf("<script>");
  assert(combined.includes("__mmInspectorInstalled__"), "the inspector script is present alongside another already-injected script");
  assert(combined.indexOf("__mmInspectorInstalled__") < combined.indexOf("__CONSOLE_MARKER__"), "injectInspectorRuntime inserts itself as the FIRST child of <head> -- ahead of whatever was already injected before it, since PreviewPane.jsx calls the console bridge first and this one second");
}

{
  // Never breaks the preview on unparseable input.
  assertEqual(typeof injectInspectorRuntime("", "n10"), "string", "an empty string input doesn't throw");
}

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll inspectorRuntime.js tests passed.");
}
