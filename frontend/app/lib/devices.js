// frontend/app/lib/devices.js
// Patch X01 (M2C build plan, WP X01 -- Devices page in the web app).
//
// The pure, dependency-free logic behind components/DevicesPanel.jsx, kept
// out of the component so plain `node` can test it (the component is JSX and
// pulls in React and the Supabase client, which `npm test` cannot load; see
// lib/__tests__/devices.test.mjs). No `import` lines on purpose: that test
// loads this file through lib/workbench/__tests__/loadSource.mjs with no
// `imports` map, so an added import fails loudly.
//
// WHAT THE REAL API LOOKS LIKE (backend/api/routes/devices.py, patch S03 --
// verified in the file, not taken from the plan):
//
//   GET  /api/devices[?include_revoked=true]
//        -> {devices: [{id, device_name, os, client_version, created_at,
//                       last_seen_at, revoked_at}]}   (active only by default)
//   POST /api/devices/approve  {user_code} -> {status, device_name, os, client_version}
//   POST /api/devices/deny     {user_code} -> {status: "denied"}
//   POST /api/devices/{id}/revoke          -> {id, revoked_at}
//
//   Errors (all `{detail: "<fixed string>"}`):
//     404  approve/deny: the code is wrong, expired or already used. ONE
//          message for all three, on purpose (the server will not say which),
//          so this page cannot either. revoke: not your device / no such device.
//     429  approve/deny: the account is locked after 5 wrong codes for 15
//          minutes (a right code is refused too). `Retry-After` carries the
//          seconds -- but a browser on another origin can only read it if the
//          server exposes it via CORS, which api/middleware.py does not (no
//          `expose_headers`), so the page treats it as optional.
//     503  pairing not configured, or its rate-limit store is down. The
//          server fails closed: nothing was approved.
//     422  request body failed validation (e.g. the code was missing).
//     401  the Supabase session is not accepted.
//
//   There is NO online/offline field. The only liveness signal is
//   `last_seen_at`, which the server stamps whenever a device exchanges its
//   credential for a ~15 minute token (so it moves about every 15 minutes
//   while a client is running, and is null until a paired device first does
//   so). This page therefore shows "Last seen" and never invents a status.
//
// Nothing here talks to the network or touches a code in any way other than
// reshaping what the person typed; the secrets stay in the request body (the
// component never puts a code in a URL, a log line or the DOM after submit).

// eo/devices.py USER_CODE_LENGTH. The alphabet is deliberately NOT mirrored:
// the server owns it, and answers a code of the wrong shape with the same 404
// as a wrong one (without counting it against the lockout), so a client that
// guessed the alphabet wrong would lock people out of a working flow.
export const USER_CODE_LENGTH = 8;

// Build plan section O1, verbatim. Do not reword; do not replace with a
// "your data never leaves your device" claim (the model runs remotely, so
// that would be false). Pinned character for character by the test.
export const TRUST_STATEMENT =
  "Your files, history and settings are stored on your device. When you ask M2C to work, the relevant code is sent to our servers and our model providers to generate the answer; we don't keep it after the run ends.";

// Build plan section 11.3 ("Trust copy ... the web Devices page"), the two
// sentences that add to O1 without contradicting it.
export const TRUST_STATEMENT_EXTRA =
  "We keep only usage numbers (which model, how many tokens, when) so we can apply your plan's limit across the web app and M2C. You can revoke any device at any time.";

// ---------------------------------------------------------------------------
// The code field
// ---------------------------------------------------------------------------

// Whitespace plus every dash a pasted code might carry: ASCII hyphen, the
// Unicode hyphens U+2010..U+2015 (hyphen, non-breaking hyphen, figure dash,
// en dash, em dash, horizontal bar) and the minus sign U+2212.
const CODE_SEPARATORS = /[\s\-\u2010-\u2015\u2212]/g;

/** What a person typed or pasted -> the characters to send. Case, spaces and
 *  dashes do not matter (the server does the same: eo/devices.py
 *  normalize_user_code). Never throws; anything that is not a string is "". */
export function normalizeUserCode(text) {
  if (typeof text !== "string") return "";
  return text.replace(CODE_SEPARATORS, "").toUpperCase();
}

/** True when an already-normalized value has the right length and character
 *  class to be worth sending (the Approve button's enable rule). */
export function isCompleteUserCode(normalized) {
  return typeof normalized === "string"
    && normalized.length === USER_CODE_LENGTH
    && /^[A-Z0-9]+$/.test(normalized);
}

// ---------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------

/** Split a GET /api/devices body's `devices` into {active, revoked}. The
 *  server already omits revoked devices unless asked, but the page must not
 *  depend on that: a revoked device never shows in the active list. Order is
 *  kept (the server sends newest first). Entries that are not objects with a
 *  non-empty string `id` are dropped. Never throws. */
export function splitDevices(devices) {
  const active = [];
  const revoked = [];
  if (!Array.isArray(devices)) return { active, revoked };
  for (const d of devices) {
    if (!d || typeof d !== "object" || typeof d.id !== "string" || !d.id) continue;
    (d.revoked_at ? revoked : active).push(d);
  }
  return { active, revoked };
}

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function plural(n, unit) {
  return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
}

/** An ISO timestamp -> "Just now" / "5 minutes ago" / "3 hours ago" /
 *  "2 days ago" / a UTC date (YYYY-MM-DD) once it is a week old.
 *  null, undefined and "" mean the device has not connected since it was
 *  paired -> "Not seen yet". Anything unparseable -> "Unknown" (never "NaN").
 *  A time slightly in the future (clock skew between server and browser)
 *  reads as "Just now". `now` is injectable for tests. */
export function formatLastSeen(iso, now = Date.now()) {
  if (iso === null || iso === undefined || iso === "") return "Not seen yet";
  if (typeof iso !== "string") return "Unknown";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "Unknown";
  const seconds = Math.floor((now - then) / 1000);
  if (seconds < MINUTE) return "Just now";
  if (seconds < HOUR) return plural(Math.floor(seconds / MINUTE), "minute");
  if (seconds < DAY) return plural(Math.floor(seconds / HOUR), "hour");
  if (seconds < 7 * DAY) return plural(Math.floor(seconds / DAY), "day");
  return new Date(then).toISOString().slice(0, 10);
}

/** One list row, ready to render, with a fallback for any field the API did
 *  not send (so the page never prints "undefined"). `version` and `os` are ""
 *  when absent and the component then leaves them out. */
export function describeDevice(device, now = Date.now()) {
  const d = device && typeof device === "object" ? device : {};
  const text = (v) => (typeof v === "string" ? v.trim() : "");
  return {
    id: typeof d.id === "string" ? d.id : "",
    name: text(d.device_name) || "Unnamed device",
    os: text(d.os),
    version: text(d.client_version),
    lastSeen: formatLastSeen(d.last_seen_at, now),
    lastSeenLabel: lastSeenLabel(d.last_seen_at, now),
  };
}

/** "Last seen: 5 minutes ago", or plain "Not seen yet" for a device that has
 *  not connected since pairing (so the row never reads "Last seen: Not seen
 *  yet"). */
export function lastSeenLabel(iso, now = Date.now()) {
  const phrase = formatLastSeen(iso, now);
  return phrase === "Not seen yet" ? phrase : `Last seen: ${phrase}`;
}

/** The success line after POST /api/devices/approve, from its
 *  {device_name, os, client_version} body: which device was approved.
 *  "Approved work-laptop (Ubuntu 24.04, version 0.2.1)." Missing fields are
 *  left out rather than printed as "undefined". */
export function describeApproval(body) {
  const b = body && typeof body === "object" ? body : {};
  const text = (v) => (typeof v === "string" ? v.trim() : "");
  const name = text(b.device_name) || "the device";
  const details = [text(b.os), text(b.client_version) && `version ${text(b.client_version)}`].filter(Boolean);
  return `Approved ${name}${details.length ? ` (${details.join(", ")})` : ""}.`;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** Retry-After header value -> whole seconds, or null when absent, empty,
 *  non-numeric, zero or negative. (The server sends seconds; the HTTP-date
 *  form is not used by it and is treated as absent.) */
export function parseRetryAfter(value) {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const text = String(value).trim();
  if (!/^\d+$/.test(text)) return null;
  const n = parseInt(text, 10);
  return n > 0 ? n : null;
}

/** Seconds -> "about 15 minutes" / "about 1 minute" / "less than a minute";
 *  null -> "a while". Rounds up, so the page never promises the wait is over
 *  a little early. */
export function formatRetryAfter(seconds) {
  if (typeof seconds !== "number" || !(seconds > 0)) return "a while";
  if (seconds < MINUTE) return "less than a minute";
  const minutes = Math.ceil(seconds / MINUTE);
  return `about ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

const NETWORK = "Couldn't reach the server. Check your connection and try again.";
const SIGNED_OUT = "Your session has expired. Sign in again, then retry.";
const BAD_CODE =
  "That code isn't valid. It may be mistyped, expired (codes last about 10 minutes) or already used. "
  + "Check the code in your terminal, or run m2c login again to get a new one.";
const FALLBACK = {
  approve: "Couldn't approve the device. Try again.",
  deny: "Couldn't deny the code. Try again.",
  revoke: "Couldn't revoke the device. Try again.",
  list: "Couldn't load your devices. Try again.",
};

/** The text for a failed Devices request, from the real S03 error shapes
 *  (see the header). `action` is "approve" | "deny" | "revoke" | "list";
 *  `status` is the HTTP status (0 / missing when fetch itself threw);
 *  `retryAfter` is the Retry-After header in seconds. Fixed strings only --
 *  nothing from a response body is echoed, so the text can never contain a
 *  code. Always returns a non-empty string. */
export function deviceErrorMessage(action, { status, retryAfter } = {}) {
  if (!status) return NETWORK;
  if (status === 401) return SIGNED_OUT;
  if (status === 429) {
    // Retry-After is only readable by this page when it is same-origin or the
    // server lists it in Access-Control-Expose-Headers (api/middleware.py does
    // not today), so a missing value is the normal cross-origin case, not an
    // error: say "later" rather than guess a duration.
    return typeof retryAfter === "number" && retryAfter > 0
      ? `Too many attempts. Try again in ${formatRetryAfter(retryAfter)}.`
      : "Too many attempts. Try again later.";
  }
  if (status === 503) return "Device pairing is unavailable right now. Try again in a moment.";
  if (status === 404 && (action === "approve" || action === "deny")) return BAD_CODE;
  if (status === 404 && action === "revoke") {
    return "That device no longer exists or was already removed.";
  }
  if (status === 422 && (action === "approve" || action === "deny")) {
    return `Enter the ${USER_CODE_LENGTH}-character code shown in your terminal.`;
  }
  return FALLBACK[action] || "Something went wrong. Try again.";
}
