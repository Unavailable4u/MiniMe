// frontend/app/lib/preview/consoleBridge.js — W6.2 (Build Workbench
// plan). "A small runtime script injected into the preview forwards
// console.*, window.onerror, unhandledrejection via postMessage(...)"
// (plan §5, step W6.2's own "Build" line).
//
// Two halves, mirroring instrument.js's own "one file, two concerns"
// shape (see that file's header):
//   - buildBridgeScript(nonce): the runtime itself, as a plain JS
//     STRING. Deliberately ES5-flavored (var, function expressions, no
//     arrow functions/template literals/optional chaining) — this runs
//     INSIDE the preview iframe, which is whatever HTML/JS a person
//     typed into their own project; it must not assume any particular
//     JS version the preview's own <html> doctype/browser combination
//     supports. No eval/new Function anywhere in it (plan's own
//     security note) — it only wraps console methods and adds two
//     event listeners.
//   - injectConsoleBridge(html, nonce): the parse5 tree-mutate-then-
//     serialize step that actually gets the runtime INTO a bundled
//     document — same approach and same "never break the preview"
//     contract bundleStatic.js/instrument.js already use, for the same
//     reason (parse5's HTML5 parsing handles a missing <head>, a
//     fragment-only input, implied tags, etc. correctly for free).
//
// Injected as the FIRST child of <head> (falling back to <html>, then
// giving up and returning the input untouched if neither exists) so it
// installs before any of the page's own inline/inlined scripts run —
// wrapping `console.log` etc. after the page has already logged
// something would miss that first call.
//
// Security posture (plan §5, step W6.2's own "Security" line): the
// PARENT side (PreviewPane.jsx) is what actually has to verify a
// message is genuine — `sandbox="allow-scripts"` gives the iframe an
// opaque origin, so `event.origin` is always the string "null" and
// useless as a check. This module's only job on that front is making
// sure every message this script sends carries the caller's `nonce`
// verbatim, so the parent can compare it against the one embedded here
// (see PreviewPane.jsx's message listener for the other half: verifying
// `event.source === iframe.contentWindow` too, and that the nonce
// wasn't guessed by treating it as long/random rather than a small
// integer).
import * as parse5 from "parse5";

// Caps applied INSIDE the bridge, before anything is even posted — the
// plan's own "cap sizes" line. A single huge console.log(giantObject)
// or a deep recursive error shouldn't be able to balloon postMessage
// payloads (or, downstream, editorStore.js's console feed — see that
// file's own CONSOLE_MAX_MESSAGES for the separate ROW-COUNT cap).
export const MAX_ARG_LENGTH = 2000;
export const MAX_STACK_LENGTH = 4000;
export const MAX_ARGS_PER_CALL = 20;

/**
 * The runtime script, as a string, with `nonce` baked in via
 * JSON.stringify (safe against a nonce containing a quote or backslash
 * — not that PreviewPane.jsx's own generator would ever produce one,
 * but this function doesn't get to assume that about every future
 * caller). Exported mainly so __tests__/consoleBridge.test.mjs can
 * assert on its shape without going through a real iframe (there's no
 * DOM in this repo's plain-`node` test runner — see that file's own
 * header for what it actually checks instead).
 *
 * @param {string} nonce
 * @returns {string}
 */
export function buildBridgeScript(nonce) {
  return (
    "(function(){\n" +
    "if (window.__mmConsoleBridgeInstalled__) return;\n" +
    "window.__mmConsoleBridgeInstalled__ = true;\n" +
    "var MM_NONCE = " + JSON.stringify(String(nonce)) + ";\n" +
    "var MAX_ARG = " + MAX_ARG_LENGTH + ";\n" +
    "var MAX_STACK = " + MAX_STACK_LENGTH + ";\n" +
    "var MAX_ARGS = " + MAX_ARGS_PER_CALL + ";\n" +
    "function mmTruncate(str, max) {\n" +
    "  if (typeof str !== 'string') return str;\n" +
    "  return str.length > max ? (str.slice(0, max) + '…(truncated)') : str;\n" +
    "}\n" +
    // Never eval, never assume JSON.stringify succeeds (circular
    // references, BigInt, a DOM node) — falls back to String(value),
    // which itself is wrapped since even THAT can throw for a hostile
    // custom toString().
    "function mmSafeString(value) {\n" +
    "  if (typeof value === 'string') return value;\n" +
    "  if (value === undefined) return 'undefined';\n" +
    "  if (value === null) return 'null';\n" +
    "  if (typeof value === 'function') return '[Function: ' + (value.name || 'anonymous') + ']';\n" +
    "  if (typeof value === 'object' && value && typeof value.message === 'string' && typeof value.stack === 'string') {\n" +
    "    return (value.name || 'Error') + ': ' + value.message;\n" +
    "  }\n" +
    "  try {\n" +
    "    var seen = [];\n" +
    "    return JSON.stringify(value, function (key, v) {\n" +
    "      if (v && typeof v === 'object') {\n" +
    "        if (seen.indexOf(v) !== -1) return '[Circular]';\n" +
    "        seen.push(v);\n" +
    "      }\n" +
    "      if (typeof v === 'function') return '[Function]';\n" +
    "      return v;\n" +
    "    });\n" +
    "  } catch (e) {\n" +
    "    try { return String(value); } catch (e2) { return '[Unserializable value]'; }\n" +
    "  }\n" +
    "}\n" +
    // All untrusted data, by design (this whole module's own header):
    // the parent never eval()s or otherwise trusts anything past the
    // nonce check, so there is nothing sensitive about ALSO tagging
    // every payload with the nonce here — a hostile page could forge
    // this same shape from scratch anyway. The nonce is what lets the
    // PARENT ignore a stale/forged message, not what protects the
    // preview from the page it's running.
    "function mmPost(payload) {\n" +
    "  try {\n" +
    "    var msg = { source: 'minime-preview', nonce: MM_NONCE, timestamp: Date.now() };\n" +
    "    for (var k in payload) if (Object.prototype.hasOwnProperty.call(payload, k)) msg[k] = payload[k];\n" +
    "    window.parent.postMessage(msg, '*');\n" +
    "  } catch (e) { /* a postMessage failure must never break the preview itself */ }\n" +
    "}\n" +
    "var mmLevels = ['log', 'info', 'warn', 'error', 'debug'];\n" +
    "for (var i = 0; i < mmLevels.length; i++) {\n" +
    "  (function (level) {\n" +
    "    var original = (console[level] ? console[level] : function () {}).bind(console);\n" +
    "    console[level] = function () {\n" +
    "      var raw = Array.prototype.slice.call(arguments, 0, MAX_ARGS);\n" +
    "      var parts = [];\n" +
    "      for (var j = 0; j < raw.length; j++) parts.push(mmTruncate(mmSafeString(raw[j]), MAX_ARG));\n" +
    "      mmPost({ type: 'console', level: level, text: parts.join(' ') });\n" +
    "      return original.apply(console, arguments);\n" +
    "    };\n" +
    "  })(mmLevels[i]);\n" +
    "}\n" +
    // A plain (non-capturing) window 'error' listener is the same
    // uncaught-script-error signal window.onerror carries, WITHOUT
    // clobbering a page that already sets window.onerror itself — a
    // resource-load failure (a broken <img>) does not bubble/reach a
    // non-capturing window listener at all, so this never conflates
    // the two the way `window.addEventListener('error', fn, true)`
    // (capture: true) would.
    "window.addEventListener('error', function (event) {\n" +
    "  var err = event && event.error;\n" +
    "  mmPost({\n" +
    "    type: 'error',\n" +
    "    level: 'error',\n" +
    "    text: mmTruncate(String((event && event.message) || 'Script error'), MAX_ARG),\n" +
    "    stack: err && err.stack ? mmTruncate(String(err.stack), MAX_STACK) : null,\n" +
    "    sourceLine: (event && event.lineno) || null,\n" +
    "    sourceColumn: (event && event.colno) || null\n" +
    "  });\n" +
    "});\n" +
    "window.addEventListener('unhandledrejection', function (event) {\n" +
    "  var reason = event && event.reason;\n" +
    "  var message = reason && typeof reason.message === 'string' ? reason.message : mmSafeString(reason);\n" +
    "  mmPost({\n" +
    "    type: 'unhandledrejection',\n" +
    "    level: 'error',\n" +
    "    text: 'Unhandled promise rejection: ' + mmTruncate(String(message), MAX_ARG),\n" +
    "    stack: reason && typeof reason.stack === 'string' ? mmTruncate(reason.stack, MAX_STACK) : null\n" +
    "  });\n" +
    "});\n" +
    "})();"
  );
}

function findFirst(node, tagName) {
  if (node.tagName === tagName) return node;
  for (const child of node.childNodes || []) {
    const found = findFirst(child, tagName);
    if (found) return found;
  }
  return null;
}

/**
 * Inserts buildBridgeScript(nonce) as the first child of `html`'s
 * <head> (falling back to <html> if a document somehow parses out
 * without one — parse5's own implied-tag rules make an absent <head>
 * essentially impossible for real markup, but this stays defensive
 * about it anyway, same posture as the "never break the preview"
 * fallback below it). Returns `html` UNCHANGED if parsing fails or
 * neither element can be found — this must never be the thing that
 * breaks a preview that would otherwise have rendered fine.
 *
 * @param {string} html - bundleStatic()'s own output
 * @param {string} nonce
 * @returns {string}
 */
export function injectConsoleBridge(html, nonce) {
  let document;
  try {
    document = parse5.parse(html, { sourceCodeLocationInfo: false });
  } catch {
    return html;
  }

  const target = findFirst(document, "head") || findFirst(document, "html");
  if (!target) return html;

  const scriptNode = {
    nodeName: "script",
    tagName: "script",
    attrs: [],
    namespaceURI: target.namespaceURI,
    childNodes: [],
    parentNode: target,
  };
  scriptNode.childNodes = [{ nodeName: "#text", value: buildBridgeScript(nonce), parentNode: scriptNode }];

  target.childNodes = target.childNodes || [];
  target.childNodes.unshift(scriptNode);

  try {
    return parse5.serialize(document);
  } catch {
    return html;
  }
}
