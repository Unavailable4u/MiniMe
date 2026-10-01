// W7.3 (Build Workbench plan) — tests for lib/preview/urlBridge.js, the
// parent-side trust decisions for a user's own dev app in the URL
// preview's iframe. No imports in the source, so none supplied here.
//
// Run: node frontend/app/lib/preview/__tests__/urlBridge.test.mjs
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

const { acceptFrameMessage, buildParentMessage, makeBridgeNonce } = loadSource("../../preview/urlBridge.js");

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

// key order is not part of the contract
const sorted = (o) => Object.fromEntries(Object.entries(o).sort(([a], [b]) => a.localeCompare(b)));

const FRAME = { id: "our-iframe-window" };
const OTHER = { id: "some-other-window" };
const ORIGIN = "http://localhost:5173";
const NONCE = "n-123";
const expected = { frameWindow: FRAME, origin: ORIGIN, nonce: NONCE };
const ev = (over = {}) => ({ source: FRAME, origin: ORIGIN, data: { source: "minime-preview", nonce: NONCE, type: "minime:select", mm: "a:1:0:1:5" }, ...over });

// --- the happy path ---------------------------------------------------------

assertEqual(acceptFrameMessage(ev(), expected)?.type, "minime:select", "a message from our frame, our origin, our nonce is accepted");
assertEqual(acceptFrameMessage(ev(), expected)?.data.mm, "a:1:0:1:5", "the payload comes back for the caller to sanitize");

// --- every one of the three checks must hold --------------------------------

assertEqual(acceptFrameMessage(ev({ source: OTHER }), expected), null, "wrong source window → rejected, even with the right origin and nonce");
assertEqual(acceptFrameMessage(ev({ origin: "http://localhost:9999" }), expected), null, "wrong origin (another localhost port) → rejected");
assertEqual(acceptFrameMessage(ev({ origin: "null" }), expected), null, "an opaque 'null' origin is not the preview's origin");
assertEqual(acceptFrameMessage(ev({ data: { source: "minime-preview", nonce: "stale", type: "minime:select" } }), expected), null, "wrong nonce → rejected");
assertEqual(acceptFrameMessage(ev({ data: { source: "minime-preview", type: "minime:select" } }), expected), null, "missing nonce → rejected");
assertEqual(acceptFrameMessage(ev({ data: { source: "minime-preview", nonce: null, type: "minime:select" } }), expected), null, "a null nonce → rejected");
assertEqual(acceptFrameMessage(ev({ data: { source: "someone-else", nonce: NONCE, type: "minime:select" } }), expected), null, "wrong envelope source → rejected");
assertEqual(acceptFrameMessage(ev({ data: { source: "minime-preview", nonce: NONCE } }), expected), null, "no type → rejected");
assertEqual(acceptFrameMessage(ev({ data: { source: "minime-preview", nonce: NONCE, type: 7 } }), expected), null, "non-string type → rejected");
for (const junk of [null, undefined, "string", 42, []]) {
  assertEqual(acceptFrameMessage(ev({ data: junk }), expected), null, `non-object data ${JSON.stringify(junk)} → rejected`);
}
assertEqual(acceptFrameMessage(null, expected), null, "a null event doesn't throw");
assertEqual(acceptFrameMessage(ev(), { frameWindow: null, origin: ORIGIN, nonce: NONCE }), null, "no iframe mounted → everything rejected (an unmounted frame must not match event.source === undefined)");
assertEqual(acceptFrameMessage(ev({ source: undefined }), { frameWindow: undefined, origin: ORIGIN, nonce: NONCE }), null, "undefined === undefined must not pass");
assertEqual(acceptFrameMessage(ev(), { frameWindow: FRAME, origin: "", nonce: NONCE }), null, "no expected origin → rejected");
assertEqual(acceptFrameMessage(ev(), { frameWindow: FRAME, origin: ORIGIN, nonce: "" }), null, "an empty expected nonce can't be matched by anything");

// --- the handshake: the ONLY nonce-less message -----------------------------

const ready = { source: "minime-preview", type: "minime:ready", tagged: true };
assertEqual(acceptFrameMessage(ev({ data: ready }), expected)?.type, "minime:ready", "minime:ready is accepted without a nonce (the frame doesn't have one yet)");
assertEqual(acceptFrameMessage(ev({ data: ready, source: OTHER }), expected), null, "…but only from our frame");
assertEqual(acceptFrameMessage(ev({ data: ready, origin: "http://localhost:9999" }), expected), null, "…and only from the preview's origin");
assertEqual(acceptFrameMessage(ev({ data: { ...ready, type: "minime:select" } }), expected), null, "dropping the nonce from any other type is not accepted");
assertEqual(acceptFrameMessage(ev({ data: { ...ready, type: "minime:inspectExited" } }), expected), null, "inspectExited needs the nonce too");

// --- outgoing messages --------------------------------------------------------

assertEqual(sorted(buildParentMessage("n1", { type: "minime:inspect", on: true })), sorted({ source: "minime-parent", nonce: "n1", type: "minime:inspect", on: true }), "parent messages carry the envelope source + nonce");
assertEqual(sorted(buildParentMessage("n1", { type: "minime:hello" })), sorted({ source: "minime-parent", nonce: "n1", type: "minime:hello" }), "the hello message carries the nonce for the frame to adopt");
assertEqual(buildParentMessage("n1", { source: "forged", nonce: "forged", type: "x" }).source, "minime-parent", "a caller-supplied field can't override the envelope source");
assertEqual(buildParentMessage("n1", { source: "forged", nonce: "forged", type: "x" }).nonce, "n1", "…nor the nonce");

// --- nonce generation ---------------------------------------------------------

const a = makeBridgeNonce();
const b = makeBridgeNonce();
assert(typeof a === "string" && a.length >= 8, "a nonce is a non-trivial string");
assert(a !== b, "two nonces differ");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll urlBridge.js tests passed.");
}
