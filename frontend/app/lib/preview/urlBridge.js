// frontend/app/lib/preview/urlBridge.js — W7.3 (Build Workbench plan).
// The parent half of the message protocol with a USER'S OWN dev app
// running in the URL preview's iframe, as pure functions so the security
// decisions can be tested with plain `node` (no imports, like mmRange.js).
//
// How this differs from the file previews (PreviewPane.jsx's static /
// react providers), and why:
//
//   - Those frames are sandboxed without allow-same-origin, so their
//     origin is the opaque "null" — origin checks are useless there and
//     the plan has them identify messages by `event.source` + a nonce
//     baked into the injected script. Here the page is a real origin
//     (http://localhost:5173) loaded from a URL, and the script inside it
//     is a STATIC file (/mm-inspector.js) — there is no per-build nonce to
//     bake in. So:
//       * both directions pin the ORIGIN as well: this side accepts only
//         event.origin === the preview URL's origin and event.source ===
//         the iframe's window; the frame side (inspectorRuntime.js's
//         external mode) accepts only event.origin === `data-mm-origin`
//         (MiniMe's) and event.source === window.parent, and posts ONLY to
//         that origin — never "*".
//       * the nonce is delivered by handshake instead of baked in: the
//         frame announces `minime:ready` (no nonce — it has none yet),
//         this side answers `minime:hello {nonce}` addressed to the
//         preview origin, and every later frame message must echo it.
//
// `minime:ready` is the ONLY message accepted without the nonce, and all
// it can cause is this side sending the nonce back to the frame that sent
// it (same window, same origin — the two checks above have already
// proven that).

/** A fresh nonce per mounted preview (crypto when present, like PreviewPane's makeNonce). */
export function makeBridgeNonce() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `mm-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Decides whether a window `message` event is a message from THE
 * inspector in THE preview frame. Returns the payload (typed) or null.
 *
 * @param {{source: unknown, origin: string, data: any}} event
 * @param {{frameWindow: unknown, origin: string, nonce: string}} expected
 * @returns {{type: string, data: any} | null}
 */
export function acceptFrameMessage(event, { frameWindow, origin, nonce }) {
  if (!event || !frameWindow || event.source !== frameWindow) return null;
  if (!origin || event.origin !== origin) return null;
  const data = event.data;
  if (!data || typeof data !== "object" || data.source !== "minime-preview") return null;
  if (typeof data.type !== "string") return null;
  if (data.type === "minime:ready") return { type: data.type, data };
  if (!nonce || data.nonce !== nonce) return null;
  return { type: data.type, data };
}

/**
 * Builds a parent -> frame message. Always addressed to the preview's
 * own origin by the caller (`postMessage(msg, origin)`), never "*": the
 * nonce inside must not be readable by a page that navigated elsewhere.
 */
export function buildParentMessage(nonce, message) {
  // Envelope last, so a caller's own field can never override it.
  return { ...message, source: "minime-parent", nonce };
}
