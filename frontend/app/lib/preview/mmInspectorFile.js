// frontend/app/lib/preview/mmInspectorFile.js — W6.6 (Build Workbench
// plan). "add `/mm-inspector.js` + an `import` line in the entry of the
// copy" — this is that file's content.
//
// Sandpack's bundler iframe is cross-origin (its default `bundlerURL`
// is a codesandbox.io-hosted CDN — see ArtifactRenderer.jsx's own note
// on this), so unlike the static preview there is no HTML document this
// app can parse5-inject a <script> into (bundleStatic.js/
// injectConsoleBridge()/injectInspectorRuntime()'s whole approach
// assumes control over the served HTML, which Sandpack's remote bundler
// never gives us). The one thing Sandpack DOES let a caller control is
// which SOURCE FILES the project bundles — so the runtimes ride in as
// an ordinary project file the entry imports for its side effects, and
// run as part of the app's own bundle. Once running, they're plain
// same-window code: `window.parent.postMessage` reaches this app's own
// window exactly the way it does for the static preview's iframe,
// cross-origin or not — postMessage is built for that.
//
// buildBridgeScript()/buildInspectorScript() are otherwise UNCHANGED
// from W6.2/W6.4 — this only concatenates the two IIFEs into one file's
// text. Both are already self-contained `(function(){...})();`
// expressions with their own `__mmConsoleBridgeInstalled__`/
// `__mmInspectorInstalled__` re-entry guards, so simply concatenating
// them (rather than merging their internals) is enough for both to
// install correctly from one file.
import { buildBridgeScript } from "./consoleBridge";
import { buildInspectorScript } from "./inspectorRuntime";

/**
 * @param {string} nonce - the SAME nonce PreviewPane.jsx's React
 *   provider will pass to the parent-side message listener; both
 *   runtimes stamp every message they post with it
 * @returns {string}
 */
export function buildMmInspectorFileContent(nonce) {
  return `${buildBridgeScript(nonce)}\n${buildInspectorScript(nonce)}\n`;
}
