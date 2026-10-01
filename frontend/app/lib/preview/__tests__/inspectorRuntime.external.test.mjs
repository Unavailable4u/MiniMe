// W7.3 (Build Workbench plan) — tests for inspectorRuntime.js's EXTERNAL
// mode (`buildInspectorScript(nonce, {external: true})`): the variant a
// person's own dev app loads from /mm-inspector.js.
//
// Same technique as inspectorRuntime.test.mjs (W6.4): REAL execution of
// the REAL generated script inside Node's `vm`, against a hand-rolled
// fake DOM that implements only what the script touches. That file also
// covers the default (non-external) mode; this one is about what is NEW:
// origin pinning, the nonce handshake, and the three source-attribute
// formats. A last block pins that the default output is untouched.
//
// Run: node frontend/app/lib/preview/__tests__/inspectorRuntime.external.test.mjs
import vm from "node:vm";
import * as parse5 from "parse5";
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

const { buildInspectorScript } = loadSource("../../preview/inspectorRuntime.js", { imports: { parse5 } });

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

const ATTRS = ["data-mm", "data-locatorjs", "data-locatorjs-id"];
const RECT = { x: 0, y: 0, top: 0, left: 0, right: 10, bottom: 10, width: 10, height: 10 };

class FakeElement {
  constructor(tag, attrs = {}, children = [], text = "") {
    this.tagName = tag.toUpperCase();
    this._attrs = attrs;
    this.className = attrs.class || "";
    this.children = children;
    this._text = text;
    this.parentElement = null;
    for (const c of children) c.parentElement = this;
  }
  getAttribute(n) {
    return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null;
  }
  // Supports `[a]` and a comma list `[a],[b],[c]` — the only selector shapes the script uses.
  closest(selector) {
    const names = selector.split(",").map((part) => {
      const m = /^\[([a-zA-Z-]+)\]$/.exec(part.trim());
      if (!m) throw new Error("fake closest() got an unsupported selector: " + selector);
      return m[1];
    });
    for (let node = this; node; node = node.parentElement) if (names.some((n) => node.getAttribute(n) != null)) return node;
    return null;
  }
  getBoundingClientRect() {
    return RECT;
  }
  get textContent() {
    return this._text || this.children.map((c) => c.textContent).join("");
  }
  get outerHTML() {
    return `<${this.tagName.toLowerCase()}>${this.textContent}</${this.tagName.toLowerCase()}>`;
  }
  *walk() {
    yield this;
    for (const c of this.children) yield* c.walk();
  }
}

/**
 * @param {FakeElement} root
 * @param {{framed?: boolean, scriptAttrs?: object, scriptSrc?: string, locatorData?: object}} [opts]
 */
function makeSandbox(root, opts = {}) {
  const { framed = true, scriptAttrs = { "data-mm-origin": "http://localhost:3000" }, scriptSrc = "", locatorData } = opts;
  const posted = []; // {msg, target}
  const handlers = {};
  const timers = [];
  const parent = { postMessage: (msg, target) => posted.push({ msg, target }) };
  const win = {
    addEventListener: (type, fn) => {
      (handlers[type] ||= []).push(fn);
    },
    setTimeout: (fn, ms) => timers.push({ fn, ms }),
    getComputedStyle: () => ({ color: "c", backgroundColor: "b", fontSize: "16px", padding: "0", margin: "0", display: "block" }),
  };
  win.parent = framed ? parent : win;
  if (locatorData) win.__LOCATOR_DATA__ = locatorData;
  const script = { getAttribute: (n) => (n in scriptAttrs ? scriptAttrs[n] : null), src: scriptSrc };
  const document = {
    body: {},
    currentScript: script,
    documentElement: { appendChild: () => {} },
    createElement: () => ({ style: {} }),
    contains: (el) => [...root.walk()].includes(el),
    querySelector: (selector) => {
      const names = selector.split(",").map((p) => /^\[([a-zA-Z-]+)\]$/.exec(p.trim())[1]);
      for (const el of root.walk()) if (names.some((n) => el.getAttribute(n) != null)) return el;
      return null;
    },
    querySelectorAll: (selector) => {
      const m = /^\[([a-zA-Z-]+)=("(?:[^"\\]|\\.)*")\]$/.exec(selector);
      if (!m) throw new Error("fake querySelectorAll got an unsupported selector: " + selector);
      const wanted = JSON.parse(m[2]);
      return [...root.walk()].filter((el) => el.getAttribute(m[1]) === wanted);
    },
  };
  const sandbox = { window: win, document, URL };
  // `window` must also be reachable as a bare global the way a browser's is
  sandbox.window.window = win;
  const context = vm.createContext(sandbox);
  new vm.Script(buildInspectorScript("ignored-in-external-mode", { external: true })).runInContext(context);
  const fire = (type, event) => (handlers[type] || []).forEach((fn) => fn(event));
  return { win, parent, posted, handlers, timers, fire };
}

const fromParent = (s, data, over = {}) => s.fire("message", { source: s.parent, origin: "http://localhost:3000", data: { source: "minime-parent", ...data }, ...over });
const click = (s, target) => s.fire("click", { target, preventDefault() {}, stopPropagation() {} });
const selects = (s) => s.posted.filter((p) => p.msg.type === "minime:select");

// ===========================================================================
// Install conditions — inert unless framed AND given a parent origin
// ===========================================================================

{
  const root = new FakeElement("div", { "data-mm": "a:1:0:1:5" });
  const s = makeSandbox(root, { framed: false });
  assertEqual(s.posted, [], "not inside an iframe → posts nothing");
  assertEqual(Object.keys(s.handlers), [], "…and installs no listeners (loading the script in a normal tab is inert)");
}
{
  const s = makeSandbox(new FakeElement("div"), { scriptAttrs: {}, scriptSrc: "" });
  assertEqual(s.posted, [], "framed but no data-mm-origin and no usable src → posts nothing");
  assertEqual(Object.keys(s.handlers), [], "…and installs nothing");
}
{
  const s = makeSandbox(new FakeElement("div"), { scriptAttrs: { "data-mm-origin": "javascript:alert(1)" } });
  assertEqual(s.posted, [], "a non-http(s) data-mm-origin is not an origin → inert");
}
{
  const s = makeSandbox(new FakeElement("div"), { scriptAttrs: { "data-mm-origin": "not a url" } });
  assertEqual(s.posted, [], "an unparseable data-mm-origin → inert, no throw");
}
{
  const s = makeSandbox(new FakeElement("div"), { scriptAttrs: {}, scriptSrc: "https://minime.example.com/mm-inspector.js" });
  assertEqual(s.posted[0]?.target, "https://minime.example.com", "with no data-mm-origin the script's own src origin is used");
}
{
  const s = makeSandbox(new FakeElement("div"), { scriptAttrs: { "data-mm-origin": "http://localhost:3000/some/path?x=1" } });
  assertEqual(s.posted[0]?.target, "http://localhost:3000", "a data-mm-origin with a path is reduced to its origin");
}

// ===========================================================================
// Announce — `minime:ready`, addressed to the pinned origin only
// ===========================================================================

{
  const s = makeSandbox(new FakeElement("div", { "data-locatorjs": "/p/a.jsx:1:0" }));
  assertEqual(s.posted.length, 1, "one minime:ready at install");
  assertEqual(s.posted[0].msg.type, "minime:ready", "it is a minime:ready");
  assertEqual(s.posted[0].msg.source, "minime-preview", "…in the preview envelope");
  assertEqual(s.posted[0].msg.nonce, null, "…carrying no nonce (it doesn't have one yet)");
  assertEqual(s.posted[0].msg.tagged, true, "tagged:true when the page carries a source attribute");
  assertEqual(s.posted[0].target, "http://localhost:3000", "posted to the pinned origin");
  s.fire("load");
  assertEqual(s.posted.length, 2, "re-announced on window load");
  assertEqual(s.timers.length, 1, "and once more on a timer (a client-rendered app may not have mounted yet)");
  s.timers[0].fn();
  assertEqual(s.posted.length, 3, "the timer re-announces too");
  assert(s.posted.every((p) => p.target !== "*"), "NO message in external mode is ever posted with targetOrigin '*'");
}
{
  const s = makeSandbox(new FakeElement("div", {}));
  assertEqual(s.posted[0].msg.tagged, false, "tagged:false when no element carries any source attribute");
}
for (const attr of ATTRS) {
  const s = makeSandbox(new FakeElement("div", { [attr]: "x" }));
  assertEqual(s.posted[0].msg.tagged, true, `${attr} alone counts as tagged`);
}

// ===========================================================================
// Handshake and message gating
// ===========================================================================

{
  const btn = new FakeElement("button", { "data-mm": "src/A.jsx:3:2:5:8" });
  const s = makeSandbox(new FakeElement("div", {}, [btn]));
  s.posted.length = 0;

  fromParent(s, { type: "minime:inspect", nonce: "N", on: true });
  click(s, btn);
  assertEqual(selects(s).length, 0, "before the handshake, a minime:inspect (any nonce) is ignored — inspect mode is not on");

  fromParent(s, { type: "minime:inspect", nonce: null, on: true });
  click(s, btn);
  assertEqual(selects(s).length, 0, "a message with nonce:null does NOT match the not-yet-set (null) nonce");

  fromParent(s, { type: "minime:hello", nonce: "N" }, { origin: "http://evil.example" });
  fromParent(s, { type: "minime:inspect", nonce: "N", on: true });
  click(s, btn);
  assertEqual(selects(s).length, 0, "a hello from the WRONG origin is not adopted");

  fromParent(s, { type: "minime:hello", nonce: "N" }, { source: { other: true } });
  fromParent(s, { type: "minime:inspect", nonce: "N", on: true });
  click(s, btn);
  assertEqual(selects(s).length, 0, "a hello from the wrong SOURCE window is not adopted");

  fromParent(s, { type: "minime:hello", nonce: 42 });
  fromParent(s, { type: "minime:hello", nonce: "" });
  fromParent(s, { type: "minime:inspect", nonce: "N", on: true });
  click(s, btn);
  assertEqual(selects(s).length, 0, "a non-string / empty hello nonce is not adopted");

  fromParent(s, { type: "minime:hello", nonce: "N" });
  fromParent(s, { type: "minime:inspect", nonce: "N", on: true });
  click(s, btn);
  assertEqual(selects(s).length, 1, "after a proper hello, an inspect with that nonce turns inspect mode on and a click selects");
  assertEqual(selects(s)[0].msg.nonce, "N", "…and every later frame message carries the adopted nonce");
  assertEqual(selects(s)[0].target, "http://localhost:3000", "…addressed to the pinned origin");

  fromParent(s, { type: "minime:inspect", nonce: "WRONG", on: false });
  click(s, btn);
  assertEqual(selects(s).length, 2, "an inspect with the WRONG nonce (even after hello) is ignored — inspect stays on");

  fromParent(s, { type: "minime:inspect", nonce: "N", on: true }, { origin: "http://evil.example" });
  fromParent(s, { type: "minime:inspect", nonce: "N", on: false }, { origin: "http://evil.example" });
  click(s, btn);
  assertEqual(selects(s).length, 3, "an inspect from the wrong origin can't turn it off either");

  fromParent(s, { type: "minime:inspect", nonce: "N", on: false });
  click(s, btn);
  assertEqual(selects(s).length, 3, "the right message turns it off");

  s.fire("message", { source: s.parent, origin: "http://localhost:3000", data: { source: "someone-else", type: "minime:inspect", nonce: "N", on: true } });
  click(s, btn);
  assertEqual(selects(s).length, 3, "a wrong envelope source is ignored");
}

// Esc exits inspect mode and tells the parent (with the nonce)
{
  const btn = new FakeElement("button", { "data-mm": "src/A.jsx:1:0:1:9" });
  const s = makeSandbox(new FakeElement("div", {}, [btn]));
  fromParent(s, { type: "minime:hello", nonce: "N" });
  fromParent(s, { type: "minime:inspect", nonce: "N", on: true });
  s.posted.length = 0;
  s.fire("keydown", { key: "Escape" });
  assertEqual(s.posted.map((p) => [p.msg.type, p.msg.nonce, p.target]), [["minime:inspectExited", "N", "http://localhost:3000"]], "Esc posts inspectExited with the nonce, to the pinned origin");
}

// ===========================================================================
// The three attribute formats
// ===========================================================================

function inspectAndClick(root, target, opts) {
  const s = makeSandbox(root, opts);
  fromParent(s, { type: "minime:hello", nonce: "N" });
  fromParent(s, { type: "minime:inspect", nonce: "N", on: true });
  click(s, target);
  return selects(s)[0]?.msg;
}

{
  // data-mm: used as-is; wins over the Locator attributes when both exist
  const el = new FakeElement("div", { "data-mm": "src/A.jsx:3:2:5:8", "data-locatorjs": "/p/Other.jsx:9:9", class: "card" });
  const msg = inspectAndClick(new FakeElement("main", {}, [el]), el);
  assertEqual([msg.mm, msg.locator, msg.attr], ["src/A.jsx:3:2:5:8", undefined, "data-mm"], "data-mm → mm, no locator, and it wins over data-locatorjs");
  assertEqual(msg.classes, ["card"], "the rest of the select payload is the ordinary one (classes)");
}
{
  // data-locatorjs (path form): start only → locator, mm null
  const el = new FakeElement("button", { "data-locatorjs": "/Users/me/proj/src/App.jsx:12:4" });
  const msg = inspectAndClick(new FakeElement("main", {}, [el]), el);
  assertEqual([msg.mm, msg.locator, msg.attr], [null, "/Users/me/proj/src/App.jsx:12:4", "data-locatorjs"], "data-locatorjs → locator (start only), mm null for the parent adapter to complete");
}
{
  // data-locatorjs-id (default form): resolved from window.__LOCATOR_DATA__ into a full range
  const el = new FakeElement("button", { "data-locatorjs-id": "/Users/me/proj/src/App.jsx::1" });
  const locatorData = {
    "/Users/me/proj/src/App.jsx": {
      expressions: [
        { name: "main", loc: { start: { line: 5, column: 4 }, end: { line: 12, column: 11 } } },
        { name: "button", loc: { start: { line: 7, column: 6 }, end: { line: 9, column: 15 } } },
      ],
    },
  };
  const msg = inspectAndClick(new FakeElement("main", {}, [el]), el, { locatorData });
  assertEqual([msg.mm, msg.locator, msg.attr], ["/Users/me/proj/src/App.jsx:7:6:9:15", undefined, "data-locatorjs-id"], "data-locatorjs-id → a full range read from window.__LOCATOR_DATA__ (index 1 = the button)");
}
{
  // Windows-style id: the path itself contains ':' and the split is on the LAST '::'
  const el = new FakeElement("i", { "data-locatorjs-id": "C:\\proj\\src\\A.jsx::0" });
  const locatorData = { "C:\\proj\\src\\A.jsx": { expressions: [{ loc: { start: { line: 2, column: 0 }, end: { line: 2, column: 9 } } }] } };
  const msg = inspectAndClick(new FakeElement("main", {}, [el]), el, { locatorData });
  assertEqual(msg.mm, "C:\\proj\\src\\A.jsx:2:0:2:9", "a Windows path with a drive-letter colon resolves (split on the last '::')");
}
{
  // An id with no matching data: the element yields nothing to select (no message), and it doesn't throw
  const el = new FakeElement("i", { "data-locatorjs-id": "/p/A.jsx::7" });
  const msg = inspectAndClick(new FakeElement("main", {}, [el]), el, { locatorData: { "/p/A.jsx": { expressions: [] } } });
  assertEqual(msg, undefined, "an unresolvable data-locatorjs-id posts no select");
}
{
  const el = new FakeElement("i", { "data-locatorjs-id": "/p/A.jsx::7" });
  const msg = inspectAndClick(new FakeElement("main", {}, [el]), el);
  assertEqual(msg, undefined, "with no window.__LOCATOR_DATA__ at all, also no select and no throw");
}
{
  // Clicking an untagged child resolves to the nearest tagged ancestor, exactly as in the default mode
  const inner = new FakeElement("span", {}, [], "hi");
  const outer = new FakeElement("div", { "data-locatorjs": "/p/A.jsx:2:2" }, [inner]);
  const msg = inspectAndClick(new FakeElement("main", {}, [outer]), inner);
  assertEqual([msg.locator, msg.dynamic, msg.tag], ["/p/A.jsx:2:2", true, "div"], "a click on an untagged child resolves to its tagged ancestor, flagged dynamic");
}
{
  // instanceCount counts elements sharing the SAME attribute value, in whichever attribute was used
  const a = new FakeElement("li", { "data-locatorjs": "/p/L.jsx:3:2" });
  const b = new FakeElement("li", { "data-locatorjs": "/p/L.jsx:3:2" });
  const c = new FakeElement("li", { "data-locatorjs": "/p/L.jsx:4:2" });
  const msg = inspectAndClick(new FakeElement("ul", {}, [a, b, c]), a);
  assertEqual(msg.instanceCount, 2, "instanceCount counts both li's carrying the same data-locatorjs value");
}
{
  // A value with a double quote doesn't break the count (JSON.stringify'd selector)
  const a = new FakeElement("li", { "data-mm": 'we"ird.jsx:1:0:1:5' });
  const msg = inspectAndClick(new FakeElement("ul", {}, [a]), a);
  assertEqual(msg.instanceCount, 1, "a quote in the attribute value is escaped, not a selector break (still counts 1, no throw)");
}

// ===========================================================================
// Default mode is unchanged
// ===========================================================================

{
  const def = buildInspectorScript("abc123");
  assert(def.includes('"abc123"'), "default mode still bakes the nonce in");
  assert(def.includes("window.parent.postMessage(msg, '*')"), "default mode still posts with targetOrigin '*' (sandboxed opaque origin — unchanged since W6.4)");
  assert(!def.includes("MM_PARENT_ORIGIN") && !def.includes("data-locatorjs") && !def.includes("minime:ready") && !def.includes("minime:hello"), "no trace of external-mode code in the default script");
  assertEqual(def, buildInspectorScript("abc123", { external: false }), "{external:false} equals no options");
  assertEqual(def, buildInspectorScript("abc123", {}), "{} equals no options");
  assertEqual(def, buildInspectorScript("abc123", { external: "yes" }), "only a literal `true` enables external mode");
  const ext = buildInspectorScript("abc123", { external: true });
  assert(!ext.includes("abc123"), "external mode does not embed the nonce it was handed");
  assert(!ext.includes("postMessage(msg, '*')"), "external mode never contains a '*' postMessage");
  assert(!ext.includes("eval(") && !ext.includes("new Function("), "external mode, like default: no eval / Function constructor");
  assert(!/<\/script/i.test(ext), "external mode never contains a literal '</script'");
}

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll inspectorRuntime external-mode tests passed.");
}
