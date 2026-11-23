// frontend/securityHeaders.js
// Patch SX-28 -- basic security headers for every page of the frontend.
//
// WHY THIS IS A SEPARATE FILE: next.config.js pulls in @sentry/nextjs and
// cannot be loaded by a plain `node --test` run. This module has no
// dependencies, so next.config.js `require`s it and
// app/lib/__tests__/securityHeaders.test.mjs checks the exact values.
//
// WHAT EACH HEADER DOES
//   X-Content-Type-Options: nosniff
//       The browser must trust the Content-Type we send and not guess a
//       "better" type (for example run a .txt file as script).
//   Referrer-Policy: strict-origin-when-cross-origin
//       Other sites get only our origin (no path or query) and only over
//       https; same-site requests still get the full URL.
//   X-Frame-Options: DENY
//       No other site can put this app in an iframe (clickjacking). This does
//       NOT affect the iframes the app itself creates (srcDoc previews, the
//       URL preview): the header is about who may frame *our* pages.
//       SX-29 adds the modern equivalent, CSP frame-ancestors 'none'.
//   Permissions-Policy: camera=(), geolocation=(), microphone=()
//       Turns off these three browser features for the app and for any
//       iframe inside it. At the time of this patch nothing in app/ calls
//       getUserMedia, MediaRecorder or geolocation (voice notes are uploaded
//       as audio FILES through /api/notes/voice), so nothing breaks. If a
//       recording feature is added later, change microphone=() to
//       microphone=(self) here and update the test.
//
// NOT HERE: Strict-Transport-Security (HSTS). That is set at the reverse
// proxy, so a local `next dev` over http is never told to force https.
//
// SX-29 -- CONTENT SECURITY POLICY (REPORT-ONLY)
//   buildHeaderRules(env) is what next.config.js returns from headers(). On top
//   of the four headers above it adds:
//   * Content-Security-Policy: frame-ancestors 'none'   (ENFORCED)
//       The only enforced directive. Same effect as X-Frame-Options: DENY. It is
//       a separate header because browsers ignore frame-ancestors inside a
//       report-only policy.
//   * Content-Security-Policy-Report-Only: <the real policy>   (NOT enforced)
//       The browser blocks nothing; it only reports what WOULD have been
//       blocked. Enforcing comes in a later patch, after a week of reports.
//   * a second report-only policy for /workers/* (the Pyodide Python runner),
//       which needs cdn.jsdelivr.net and WebAssembly. Keeping that out of the
//       page policy matters: jsdelivr serves any npm package, so allowing it as
//       a page script source is a known way around a CSP.
//
//   Every directive was checked against the code when this was written (see the
//   comment on each). Origins that differ per deployment (API, Supabase, Pusher,
//   Sentry) are read from the same NEXT_PUBLIC_* variables the app itself uses.
//   next.config.js evaluates headers() when `next build` runs, so set these (and
//   CSP_REPORT_URI) at BUILD time, like every NEXT_PUBLIC_* value.
//
//   NO NONCES, on purpose: a Next.js advisory concerns nonce-based CSP. Wait for
//   the Next upgrade before using them. script-src keeps 'unsafe-inline' for now.
//
//   WHY ENFORCING LATER WILL NEED CARE: iframes built with srcDoc (the HTML,
//   wireframe and React previews) inherit the embedding page's CSP, and those
//   previews run user-built apps that load their own scripts and styles. Their
//   violations will show up in the reports (document-uri about:srcdoc) and an
//   enforced policy would break them. Decide how to handle previews first.

const DEFAULT_API_URL = "http://localhost:8000"; // same fallback the app code uses
const GOOGLE_FONTS_CSS = "https://fonts.googleapis.com";
const GOOGLE_FONTS_FILES = "https://fonts.gstatic.com";
const JSDELIVR = "https://cdn.jsdelivr.net";

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  {
    key: "Permissions-Policy",
    value: "camera=(), geolocation=(), microphone=()",
  },
];

// ---- helpers: turn environment values into safe CSP source expressions ----
// A bad or hostile value must never be able to add a directive or a source of
// its own, so every value is parsed and then re-checked against a strict
// pattern. An invalid value is dropped (the origin is simply not allowed).

const ORIGIN_RE = /^https?:\/\/[A-Za-z0-9.-]+(:\d{1,5})?$/;

/** "https://x.example:8443/path?q" -> "https://x.example:8443", else null. */
function originOf(value, { allowUserinfo = false } = {}) {
  if (typeof value !== "string") return null;
  let u;
  try {
    u = new URL(value.trim());
  } catch {
    return null;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return null;
  if (!allowUserinfo && (u.username || u.password)) return null;
  const origin = `${u.protocol}//${u.host}`; // host = name[:port], never the key
  return ORIGIN_RE.test(origin) ? origin : null;
}

/** Pusher cluster name ("eu", "ap1", ...) -> its websocket and fallback hosts. */
function pusherSources(cluster) {
  if (typeof cluster !== "string" || !/^[a-z0-9-]{1,32}$/i.test(cluster)) return [];
  return [`wss://ws-${cluster}.pusher.com`, `https://sockjs-${cluster}.pusher.com`];
}

/** Report endpoint -> its URL, or null if unset or not a plain http(s) URL. */
function reportUriOf(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  let u;
  try {
    u = new URL(value.trim());
  } catch {
    return null;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return null;
  if (u.username || u.password) return null;
  if (/[;,\s'"]/.test(u.href)) return null; // would break out of the directive
  return u.href;
}

function unique(list) {
  return [...new Set(list.filter(Boolean))];
}

function serialize(directives) {
  return directives.map(([name, sources]) => [name, ...sources].join(" ")).join("; ");
}

/** The page policy, as an ordered list of [directive, [sources]] pairs. */
function buildPageDirectives(env = {}) {
  const dev = env.NODE_ENV !== "production";
  const reportUri = reportUriOf(env.CSP_REPORT_URI);

  const connect = unique([
    "'self'",
    // API calls and the SSE stream (EventSource counts as connect-src).
    originOf(env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL),
    // supabase-js auth and data calls.
    originOf(env.NEXT_PUBLIC_SUPABASE_URL),
    // pusher-js: websocket plus its HTTPS fallback host.
    ...pusherSources(env.NEXT_PUBLIC_PUSHER_CLUSTER),
    // Sentry: only the host of the DSN; the key in front of the @ is never copied.
    originOf(env.NEXT_PUBLIC_SENTRY_DSN, { allowUserinfo: true }),
  ]);

  return [
    ["default-src", ["'self'"]],
    // 'unsafe-inline': Next.js inline bootstrap scripts (no nonces, see above).
    // 'unsafe-eval' only outside production: webpack's dev builds need it.
    ["script-src", ["'self'", "'unsafe-inline'", ...(dev ? ["'unsafe-eval'"] : [])]],
    // Google Fonts css is loaded by LandingPage.jsx and globals.css.
    ["style-src", ["'self'", "'unsafe-inline'", GOOGLE_FONTS_CSS]],
    ["font-src", ["'self'", "data:", GOOGLE_FONTS_FILES]],
    // Avatars come from any https host (for example lh3.googleusercontent.com);
    // data: and blob: are used for generated images and previews.
    ["img-src", ["'self'", "data:", "blob:", "https:"]],
    // <audio>/<video> in NotebooksTab play blob: object URLs.
    ["media-src", ["'self'", "blob:", "data:"]],
    ["connect-src", connect],
    // UrlPreview only frames localhost / 127.0.0.1 (http or https); the React
    // preview (Sandpack) frames its bundler on *.codesandbox.io. srcDoc iframes
    // are not governed by frame-src.
    [
      "frame-src",
      [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "https://localhost:*",
        "https://127.0.0.1:*",
        "https://*.codesandbox.io",
      ],
    ],
    // The only worker is /workers/pyodideWorker.js (usePyodideWorker.js).
    ["worker-src", ["'self'"]],
    ["object-src", ["'none'"]],
    ["base-uri", ["'self'"]],
    ...(reportUri ? [["report-uri", [reportUri]]] : []),
  ];
}

/**
 * The policy for /workers/* (public/workers/pyodideWorker.js). The worker
 * imports pyodide.js and downloads Python packages (numpy, matplotlib) from
 * cdn.jsdelivr.net, and Pyodide compiles WebAssembly.
 */
function buildWorkerDirectives(env = {}) {
  const reportUri = reportUriOf(env.CSP_REPORT_URI);
  return [
    ["default-src", ["'self'"]],
    ["script-src", ["'self'", JSDELIVR, "'wasm-unsafe-eval'"]],
    ["connect-src", ["'self'", JSDELIVR]],
    ...(reportUri ? [["report-uri", [reportUri]]] : []),
  ];
}

/** What next.config.js returns from headers(). */
function buildHeaderRules(env = {}) {
  return [
    {
      source: "/:path*",
      headers: [
        ...securityHeaders,
        { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
        {
          key: "Content-Security-Policy-Report-Only",
          value: serialize(buildPageDirectives(env)),
        },
      ],
    },
    {
      // Matches after the rule above, so for the worker script this report-only
      // policy replaces the page one (same header name).
      source: "/workers/:path*",
      headers: [
        {
          key: "Content-Security-Policy-Report-Only",
          value: serialize(buildWorkerDirectives(env)),
        },
      ],
    },
  ];
}

module.exports = {
  securityHeaders,
  buildHeaderRules,
  buildPageDirectives,
  buildWorkerDirectives,
  serialize,
};
