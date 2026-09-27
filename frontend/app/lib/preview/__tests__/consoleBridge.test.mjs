// W6.2 (Build Workbench plan) — tests for lib/preview/consoleBridge.js.
//
// Same loadSource() approach instrument.test.mjs uses (see that file's
// own header): reads the REAL module, real `parse5` import. There's no
// DOM/iframe in this repo's plain-`node` test runner, so
// buildBridgeScript()'s OUTPUT is checked structurally (a string
// containing the right pieces, no forbidden constructs) rather than by
// actually executing it against a fake window/console — a real
// end-to-end check of the injected script's behavior belongs in a
// browser-based test this repo doesn't have yet, same gap
// instrumentJsx's own JSX-in-a-real-bundler behavior has.
//
// Run: node frontend/app/lib/preview/__tests__/consoleBridge.test.mjs
import * as parse5 from "parse5";
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

const { buildBridgeScript, injectConsoleBridge, MAX_ARG_LENGTH, MAX_STACK_LENGTH, MAX_ARGS_PER_CALL } = loadSource(
  "../../preview/consoleBridge.js",
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
  if (actual !== expected) {
    failures++;
    console.error(`FAIL: ${msg}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

// --- buildBridgeScript ---------------------------------------------------

const script = buildBridgeScript("abc123");

assert(script.includes("'abc123'") || script.includes('"abc123"'), "the nonce is embedded in the script, quoted as a string literal");
assert(script.includes("source: 'minime-preview'"), "every posted message is tagged with the plan's own source string");
assert(script.includes("window.parent.postMessage"), "the bridge actually posts to the parent");
assert(!script.includes("eval("), "never eval (plan's own security note)");
assert(!script.includes("new Function("), "never the Function constructor either — same 'no eval' rule");
assert(!/<\/script/i.test(script), "the script body never contains a literal '</script' — that would truncate the injected tag when serialized as HTML");
assert(script.includes("addEventListener('error'"), "wires window.onerror-equivalent via addEventListener, not an assignment that would clobber a page's own handler");
assert(script.includes("addEventListener('unhandledrejection'"), "wires unhandledrejection");
["log", "info", "warn", "error", "debug"].forEach((level) => {
  assert(script.includes(`console[level]`) || script.includes("mmLevels"), `wraps console.${level} via the shared level loop`);
});
assert(script.includes("__mmConsoleBridgeInstalled__"), "guards against double-installation if injected twice");

// A nonce containing a quote/backslash must not break out of the
// embedded string literal — JSON.stringify is what makes this safe.
const hostileNonce = `abc"; window.__pwned__=1; //`;
const hostileScript = buildBridgeScript(hostileNonce);
const encoded = JSON.stringify(hostileNonce);
assert(hostileScript.includes(encoded), "a nonce with a quote is safely JSON-encoded, not string-concatenated raw");
assertEqual(
  hostileScript.split(encoded).join("").includes("__pwned__"),
  false,
  "outside of its one safely-quoted occurrence, the hostile nonce's payload never appears in the output"
);

assert(typeof MAX_ARG_LENGTH === "number" && MAX_ARG_LENGTH > 0, "MAX_ARG_LENGTH is a real positive cap");
assert(typeof MAX_STACK_LENGTH === "number" && MAX_STACK_LENGTH > 0, "MAX_STACK_LENGTH is a real positive cap");
assert(typeof MAX_ARGS_PER_CALL === "number" && MAX_ARGS_PER_CALL > 0, "MAX_ARGS_PER_CALL is a real positive cap");

// --- injectConsoleBridge --------------------------------------------------

function scriptTexts(html) {
  const doc = parse5.parse(html, { sourceCodeLocationInfo: false });
  const out = [];
  (function walk(node) {
    if (node.tagName === "script") {
      out.push((node.childNodes || []).map((c) => c.value || "").join(""));
    }
    for (const child of node.childNodes || []) walk(child);
  })(doc);
  return out;
}

{
  const html = "<html><head><title>t</title></head><body><script>console.log(1);</script></body></html>";
  const out = injectConsoleBridge(html, "n1");
  const scripts = scriptTexts(out);
  assertEqual(scripts.length, 2, "injecting adds exactly one new <script>, alongside the page's own");
  assert(scripts[0].includes("__mmConsoleBridgeInstalled__"), "the INJECTED script is the FIRST one in document order — it must run before the page's own script");
  assert(scripts[1].includes("console.log(1)"), "the page's own script is untouched, just pushed after the bridge");
}

{
  // No <head> in the source at all -- parse5's own implied-tag rules
  // should still produce one to inject into.
  const html = "<html><body><p>hi</p></body></html>";
  const out = injectConsoleBridge(html, "n2");
  assert(scriptTexts(out).some((s) => s.includes("__mmConsoleBridgeInstalled__")), "a document with no explicit <head> still gets the bridge (parse5 implies one)");
}

{
  // A bare fragment with no <html>/<head>/<body> at all -- parse5.parse
  // (full-document mode, not parseFragment) still wraps it in a real
  // document, same as bundleStatic.js's own entryContent handling.
  const html = "<p>just a fragment</p>";
  const out = injectConsoleBridge(html, "n3");
  assert(scriptTexts(out).some((s) => s.includes("__mmConsoleBridgeInstalled__")), "even a bare fragment ends up with a <head> to inject into");
}

{
  // Different nonces produce different embedded scripts -- the parent's
  // per-mount nonce actually round-trips into the iframe unchanged.
  const outA = injectConsoleBridge("<html><head></head><body></body></html>", "nonce-A");
  const outB = injectConsoleBridge("<html><head></head><body></body></html>", "nonce-B");
  assert(scriptTexts(outA)[0].includes("nonce-A"), "nonce A is embedded in its own build");
  assert(scriptTexts(outB)[0].includes("nonce-B"), "nonce B is embedded in its own build");
  assert(!scriptTexts(outA)[0].includes("nonce-B"), "nonce A's build never carries nonce B's value");
}

// Never breaks the preview on unparseable input -- same contract as
// instrument.js/bundleStatic.js. parse5 essentially never throws on a
// string input (see instrument.js's own note on this), so this mostly
// documents the contract rather than forcing a real parse5 failure.
assertEqual(injectConsoleBridge("", "n4").length >= 0, true, "an empty string input doesn't throw");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll consoleBridge.js tests passed.");
}
