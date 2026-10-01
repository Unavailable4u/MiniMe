// frontend/app/lib/preview/previewUrl.js — W7.3 (Build Workbench plan).
// "Localhost preview URL": the rules for which URLs the preview pane
// will put in an iframe, the copy-paste <script> snippet a person adds
// to their own dev app, and the small per-workspace preference (which
// source the preview is showing + the last URL typed).
//
// No imports, same "dependency-free family" as layoutPrefs.js /
// fileTree.js — everything here is string/URL math, so plain `node` can
// test the real file (see __tests__/previewUrl.test.mjs).
//
// WHY THE URL RULES ARE STRICT. Every other preview in this directory
// runs under `sandbox="allow-scripts"` with NO allow-same-origin
// (plan §7 item 4) — that only works because the page is generated
// text MiniMe itself hands to the iframe. A real dev server is
// different: a sandboxed, opaque-origin frame can't use localStorage or
// cookies and gets its module scripts rejected by any dev server whose
// CORS policy isn't "*" (Vite restricts to localhost origins by
// default, and "null" isn't one). So the URL preview needs
// `allow-same-origin` — the combination the HTML spec warns about, and
// only safe when the framed page is on a DIFFERENT origin from the
// embedder (it then keeps its own origin and can't reach MiniMe's DOM,
// storage or cookies). That is exactly what these rules guarantee:
//   - loopback hosts only (localhost, *.localhost, 127.0.0.0/8, [::1])
//     — never an arbitrary remote site with scripts + same-origin;
//   - never MiniMe's own origin. A dev build of MiniMe is itself
//     http://localhost:3000, so "localhost:3000" is an easy typo for
//     someone whose app runs elsewhere — framing it with
//     allow-scripts + allow-same-origin would let the framed copy
//     remove its own sandbox. Blocked outright, including the
//     127.0.0.1 / localhost alias on the same port, which is a
//     different origin to the browser but still MiniMe;
//   - no embedded credentials (user:pass@host).

const SCHEME_RE = /^[a-z][a-z0-9+.-]*:\/\//i;
const IPV4_LOOPBACK_RE = /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/;

/** Path of the script a person's dev app loads; served by app/mm-inspector.js/route.js. */
export const INSPECTOR_SCRIPT_PATH = "/mm-inspector.js";

const MAX_URL_LENGTH = 2048;

function isLoopbackHost(hostname) {
  const h = String(hostname || "").toLowerCase();
  if (h === "localhost" || h.endsWith(".localhost")) return true;
  if (h === "[::1]") return true;
  return IPV4_LOOPBACK_RE.test(h);
}

function effectivePort(u) {
  if (u.port) return u.port;
  return u.protocol === "https:" ? "443" : "80";
}

/**
 * @param {string} input - what the person typed ("localhost:5173", "http://127.0.0.1:3000/app", …)
 * @param {string} [ownOrigin] - window.location.origin of the MiniMe page itself (the embedder)
 * @returns {{ok: true, url: string, origin: string} | {ok: false, error: string}}
 *   `url` is the normalized href to load; `origin` is what messages from
 *   the frame must carry as event.origin.
 */
export function normalizePreviewUrl(input, ownOrigin) {
  const raw = typeof input === "string" ? input.trim() : "";
  if (!raw) return { ok: false, error: "Enter your dev server's address, e.g. http://localhost:5173" };
  if (raw.length > MAX_URL_LENGTH) return { ok: false, error: "That address is too long." };

  // `new URL("localhost:5173")` does NOT fail — it parses "localhost:"
  // as a scheme — so a missing scheme has to be detected BEFORE parsing
  // rather than caught from a thrown error.
  const withScheme = SCHEME_RE.test(raw) ? raw : `http://${raw}`;

  let u;
  try {
    u = new URL(withScheme);
  } catch {
    return { ok: false, error: "That doesn't look like a valid address." };
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") {
    return { ok: false, error: "Only http:// and https:// addresses can be previewed." };
  }
  if (u.username || u.password) {
    return { ok: false, error: "Remove the username/password from the address." };
  }
  if (!isLoopbackHost(u.hostname)) {
    return {
      ok: false,
      error: "Only localhost addresses can be previewed here (localhost, 127.0.0.1 or [::1]). Use “Open in new tab” for anything else.",
    };
  }

  if (ownOrigin) {
    let own = null;
    try {
      own = new URL(ownOrigin);
    } catch {
      own = null;
    }
    if (own) {
      const sameOrigin = u.origin === own.origin;
      const sameLoopbackPort = isLoopbackHost(own.hostname) && u.protocol === own.protocol && effectivePort(u) === effectivePort(own);
      if (sameOrigin || sameLoopbackPort) {
        return { ok: false, error: "That's the address MiniMe itself is running on — enter your own app's dev server instead." };
      }
    }
  }

  return { ok: true, url: u.href, origin: u.origin };
}

/**
 * The <script> tag a person pastes into their app's HTML (index.html
 * for Vite/CRA, the root layout for Next). `origin` is MiniMe's own
 * origin — the script is served from there, and `data-mm-origin` is the
 * ONLY origin the script will ever post to or accept messages from
 * (see inspectorRuntime.js's external mode).
 *
 * @param {string} origin - MiniMe's window.location.origin
 * @returns {string}
 */
export function buildInspectorSnippet(origin) {
  const o = String(origin || "").replace(/\/+$/, "");
  // Attribute-escaped, although a real origin never contains these —
  // this string is shown for copy-paste into someone's HTML.
  const attr = o.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  return `<script src="${attr}${INSPECTOR_SCRIPT_PATH}" data-mm-origin="${attr}" defer></script>`;
}

// ---------------------------------------------------------------------
// Persistence — one JSON key per workspace, same shape/behavior as
// layoutPrefs.js's saveLayout()/loadLayout(): total (never throws), wrong
// types fall back per field, defaults remove the key.
// ---------------------------------------------------------------------

export const PREVIEW_SOURCES = Object.freeze(["files", "url"]);
export const DEFAULT_PREVIEW_PREFS = Object.freeze({ mode: "files", url: "" });

export function previewPrefsKey(workspaceId) {
  return `minime_build_editor_preview_source:${workspaceId}`;
}

/** @returns {{mode: "files"|"url", url: string}} */
export function normalizePreviewPrefs(raw) {
  const src = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  return {
    mode: PREVIEW_SOURCES.includes(src.mode) ? src.mode : DEFAULT_PREVIEW_PREFS.mode,
    url: typeof src.url === "string" ? src.url.slice(0, MAX_URL_LENGTH) : DEFAULT_PREVIEW_PREFS.url,
  };
}

/**
 * @param {{getItem: (k: string) => string|null}|null} storage
 * @param {string} workspaceId
 */
export function loadPreviewPrefs(storage, workspaceId) {
  if (!storage || !workspaceId) return normalizePreviewPrefs(null);
  try {
    const raw = storage.getItem(previewPrefsKey(workspaceId));
    return normalizePreviewPrefs(raw ? JSON.parse(raw) : null);
  } catch {
    return normalizePreviewPrefs(null);
  }
}

/**
 * @param {{setItem: Function, removeItem: Function}|null} storage
 * @param {string} workspaceId
 * @param {{mode?: string, url?: string}} prefs
 */
export function savePreviewPrefs(storage, workspaceId, prefs) {
  if (!storage || !workspaceId) return;
  try {
    const clean = normalizePreviewPrefs(prefs);
    const key = previewPrefsKey(workspaceId);
    if (clean.mode === DEFAULT_PREVIEW_PREFS.mode && clean.url === DEFAULT_PREVIEW_PREFS.url) storage.removeItem(key);
    else storage.setItem(key, JSON.stringify(clean));
  } catch {
    // Quota / blocked storage: the choice just won't survive a reload.
  }
}
