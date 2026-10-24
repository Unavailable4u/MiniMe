// Patch X01 (M2C build plan, WP X01 — Devices page) — tests for lib/devices.js,
// the pure helper behind components/DevicesPanel.jsx.
// Run: node frontend/app/lib/__tests__/devices.test.mjs   (or `npm test`)
//
// What is pinned here, and why:
//   1. normalizeUserCode: the same normalisation eo/devices.py applies
//      (case, whitespace and hyphens do not matter), so what the page sends
//      is what the server would have accepted from a perfect typist. Pasted
//      Unicode dashes are dropped too (the server rejects non-ASCII input
//      outright, and a code copied from a rendered terminal can carry one).
//   2. isCompleteUserCode: the button-enable rule (8 letters/digits).
//   3. splitDevices: revoked devices never appear in the active list, even
//      if a response includes them; junk entries are ignored; order is kept.
//   4. formatLastSeen: relative wording, plural handling, "never seen",
//      clock-skew and garbage input.
//   5. describeDevice: the row model the component renders, with fallbacks
//      for a field the API did not send.
//   6. deviceErrorMessage / parseRetryAfter / formatRetryAfter: the mapping of
//      the REAL S03 error shapes (api/routes/devices.py: 404 for a wrong,
//      expired or used code with ONE message, 429 + Retry-After for the
//      lockout, 503 fail-closed, 422 validation, 401 session) to text.
//   7. The trust statement is the build plan's section O1 wording, verbatim,
//      and does not contain the false "never leaves your device" claim.
//
// Loads the REAL lib/devices.js through loadSource.mjs (no pasted copy). No
// `imports` are passed on purpose: the helper must stay dependency-free, and
// the load fails loudly if someone adds an `import`.
import { loadSource } from "../workbench/__tests__/loadSource.mjs";

const {
  USER_CODE_LENGTH,
  TRUST_STATEMENT,
  TRUST_STATEMENT_EXTRA,
  normalizeUserCode,
  isCompleteUserCode,
  splitDevices,
  formatLastSeen,
  describeDevice,
  lastSeenLabel,
  describeApproval,
  parseRetryAfter,
  formatRetryAfter,
  deviceErrorMessage,
} = loadSource("../../devices.js");

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
function assertTrue(cond, msg) {
  assertEqual(!!cond, true, msg);
}

// ---------------------------------------------------------------- normalize
assertEqual(USER_CODE_LENGTH, 8, "user codes are 8 characters (eo/devices.py USER_CODE_LENGTH)");
assertEqual(normalizeUserCode("BCDF-GHJK"), "BCDFGHJK", "normalize: the displayed form loses its hyphen");
assertEqual(normalizeUserCode("bcdf-ghjk"), "BCDFGHJK", "normalize: lower case is upper-cased");
assertEqual(normalizeUserCode("  bcdf ghjk \n"), "BCDFGHJK", "normalize: spaces, tabs and newlines are removed");
assertEqual(normalizeUserCode("b-c-d-f---g-h-j-k"), "BCDFGHJK", "normalize: any number of hyphens is removed");
assertEqual(normalizeUserCode("BCDF\u2013GHJK"), "BCDFGHJK", "normalize: a pasted en dash is removed");
assertEqual(normalizeUserCode("BCDF\u2014GHJK"), "BCDFGHJK", "normalize: a pasted em dash is removed");
assertEqual(normalizeUserCode("BCDF\u2011GHJK"), "BCDFGHJK", "normalize: a pasted non-breaking hyphen is removed");
assertEqual(normalizeUserCode("BCDF\u00a0GHJK"), "BCDFGHJK", "normalize: a non-breaking space is removed");
assertEqual(normalizeUserCode(""), "", "normalize: empty stays empty");
assertEqual(normalizeUserCode(null), "", "normalize: null becomes empty");
assertEqual(normalizeUserCode(undefined), "", "normalize: undefined becomes empty");
assertEqual(normalizeUserCode(12345678), "", "normalize: a non-string becomes empty");
assertEqual(normalizeUserCode("bcdf-ghjk-extra"), "BCDFGHJKEXTRA", "normalize: does not truncate (the length check is separate)");

// ----------------------------------------------------------------- complete
assertTrue(isCompleteUserCode("BCDFGHJK"), "complete: 8 letters");
assertTrue(isCompleteUserCode("ABCD1234"), "complete: digits are not rejected here (the server owns the alphabet)");
assertEqual(isCompleteUserCode("BCDFGHJ"), false, "complete: 7 characters is not enough");
assertEqual(isCompleteUserCode("BCDFGHJKL"), false, "complete: 9 characters is too many");
assertEqual(isCompleteUserCode(""), false, "complete: empty");
assertEqual(isCompleteUserCode("BCDF-GHJ"), false, "complete: expects an already-normalized value");
assertEqual(isCompleteUserCode(null), false, "complete: null");
assertEqual(isCompleteUserCode("BCDFGHJ\u00c9"), false, "complete: non-ASCII letters are rejected");

// ------------------------------------------------------------------- split
const dev = (id, extra = {}) => ({
  id, device_name: `dev-${id}`, os: "Ubuntu 24.04", client_version: "0.1.0",
  created_at: "2026-10-01T10:00:00Z", last_seen_at: null, revoked_at: null, ...extra,
});
{
  const list = [dev("a"), dev("b", { revoked_at: "2026-10-02T00:00:00Z" }), dev("c")];
  const { active, revoked } = splitDevices(list);
  assertEqual(active.map((d) => d.id), ["a", "c"], "split: active keeps API order and drops revoked");
  assertEqual(revoked.map((d) => d.id), ["b"], "split: revoked devices are separated");
}
assertEqual(splitDevices([]), { active: [], revoked: [] }, "split: empty list");
assertEqual(splitDevices(null), { active: [], revoked: [] }, "split: null does not throw");
assertEqual(splitDevices({ devices: [] }), { active: [], revoked: [] }, "split: a non-array does not throw");
assertEqual(
  splitDevices([null, undefined, 5, "x", {}, { id: "" }, dev("ok")]).active.map((d) => d.id),
  ["ok"],
  "split: entries without a string id are ignored",
);
assertEqual(
  splitDevices([dev("x", { revoked_at: "" })]).active.length,
  1,
  "split: an empty-string revoked_at is not a revocation",
);

// ------------------------------------------------------------ last seen
const NOW = Date.parse("2026-10-24T12:00:00Z");
const ago = (ms) => new Date(NOW - ms).toISOString();
const SEC = 1000, MIN = 60 * SEC, HOUR = 60 * MIN, DAY = 24 * HOUR;
assertEqual(formatLastSeen(null, NOW), "Not seen yet", "lastSeen: null means the device has not connected yet");
assertEqual(formatLastSeen(undefined, NOW), "Not seen yet", "lastSeen: undefined likewise");
assertEqual(formatLastSeen("", NOW), "Not seen yet", "lastSeen: empty string likewise");
assertEqual(formatLastSeen(ago(5 * SEC), NOW), "Just now", "lastSeen: seconds ago");
assertEqual(formatLastSeen(ago(59 * SEC), NOW), "Just now", "lastSeen: still just now at 59 s");
assertEqual(formatLastSeen(new Date(NOW + 30 * SEC).toISOString(), NOW), "Just now", "lastSeen: a slightly future time (clock skew) is just now");
assertEqual(formatLastSeen(ago(1 * MIN), NOW), "1 minute ago", "lastSeen: singular minute");
assertEqual(formatLastSeen(ago(14 * MIN + 30 * SEC), NOW), "14 minutes ago", "lastSeen: minutes are floored");
assertEqual(formatLastSeen(ago(1 * HOUR), NOW), "1 hour ago", "lastSeen: singular hour");
assertEqual(formatLastSeen(ago(5 * HOUR), NOW), "5 hours ago", "lastSeen: plural hours");
assertEqual(formatLastSeen(ago(1 * DAY), NOW), "1 day ago", "lastSeen: singular day");
assertEqual(formatLastSeen(ago(6 * DAY), NOW), "6 days ago", "lastSeen: plural days");
assertEqual(formatLastSeen("2026-09-01T08:00:00Z", NOW), "2026-09-01", "lastSeen: older than a week shows the (UTC) date");
assertEqual(formatLastSeen("not a date", NOW), "Unknown", "lastSeen: garbage is Unknown, not NaN");
assertEqual(formatLastSeen(12345, NOW), "Unknown", "lastSeen: a non-string is Unknown");

// ------------------------------------------------------------ describeDevice
assertEqual(
  describeDevice(dev("a", { device_name: "work-laptop", os: "Ubuntu 24.04", client_version: "0.2.1", last_seen_at: ago(2 * MIN) }), NOW),
  { id: "a", name: "work-laptop", os: "Ubuntu 24.04", version: "0.2.1", lastSeen: "2 minutes ago", lastSeenLabel: "Last seen: 2 minutes ago" },
  "describe: a full row",
);
assertEqual(
  describeDevice({ id: "z" }, NOW),
  { id: "z", name: "Unnamed device", os: "", version: "", lastSeen: "Not seen yet", lastSeenLabel: "Not seen yet" },
  "describe: missing fields fall back instead of printing undefined",
);

assertEqual(lastSeenLabel(null, NOW), "Not seen yet", "label: never seen has no \"Last seen:\" prefix");
assertEqual(lastSeenLabel(ago(3 * HOUR), NOW), "Last seen: 3 hours ago", "label: prefixed relative time");
assertEqual(lastSeenLabel("garbage", NOW), "Last seen: Unknown", "label: unparseable time is still readable");

// ------------------------------------------------------------ describeApproval
assertEqual(
  describeApproval({ status: "approved", device_name: "work-laptop", os: "Ubuntu 24.04", client_version: "0.2.1" }),
  "Approved work-laptop (Ubuntu 24.04, version 0.2.1).",
  "approval: names the device, its OS and its client version (what approve returns)",
);
assertEqual(describeApproval({ device_name: "box", os: "", client_version: "0.1.0" }), "Approved box (version 0.1.0).", "approval: an empty OS is left out");
assertEqual(describeApproval({ device_name: "box" }), "Approved box.", "approval: no details, no empty brackets");
assertEqual(describeApproval({}), "Approved the device.", "approval: no name falls back");
assertEqual(describeApproval(null), "Approved the device.", "approval: null body does not throw");

// ------------------------------------------------------------ retry-after
assertEqual(parseRetryAfter("900"), 900, "retry-after: integer seconds");
assertEqual(parseRetryAfter(" 30 "), 30, "retry-after: surrounding spaces");
assertEqual(parseRetryAfter(null), null, "retry-after: absent header");
assertEqual(parseRetryAfter(""), null, "retry-after: empty header");
assertEqual(parseRetryAfter("soon"), null, "retry-after: non-numeric");
assertEqual(parseRetryAfter("-5"), null, "retry-after: negative is ignored");
assertEqual(parseRetryAfter("0"), null, "retry-after: zero is ignored");
assertEqual(formatRetryAfter(900), "about 15 minutes", "retry-after text: the server's 15 minute lock");
assertEqual(formatRetryAfter(60), "about 1 minute", "retry-after text: one minute");
assertEqual(formatRetryAfter(61), "about 2 minutes", "retry-after text: rounds up");
assertEqual(formatRetryAfter(20), "less than a minute", "retry-after text: under a minute");
assertEqual(formatRetryAfter(null), "a while", "retry-after text: unknown");

// ------------------------------------------------------------ error messages
const approve404 = deviceErrorMessage("approve", { status: 404 });
assertTrue(/wrong|valid/i.test(approve404) && /expired/i.test(approve404) && /already used|used/i.test(approve404),
  "approve 404: one message that covers wrong, expired and already-used (the server cannot tell them apart)");
assertTrue(approve404.includes("m2c login"), "approve 404: says how to get a fresh code");
assertEqual(deviceErrorMessage("deny", { status: 404 }), approve404, "deny 404: same wording as approve (same server outcome)");
{
  const locked = deviceErrorMessage("approve", { status: 429, retryAfter: 900 });
  assertTrue(/too many/i.test(locked) && locked.includes("about 15 minutes"), "approve 429: lockout names the wait from Retry-After");
  const noHeader = deviceErrorMessage("approve", { status: 429 });
  assertTrue(/too many/i.test(noHeader) && /later/i.test(noHeader) && !/minute/.test(noHeader),
    "approve 429: without a readable Retry-After (the cross-origin case) it says \"later\" and guesses no duration");
}
assertTrue(/unavailable|try again/i.test(deviceErrorMessage("approve", { status: 503 })), "approve 503: fail-closed server message");
assertTrue(/sign/i.test(deviceErrorMessage("approve", { status: 401 })), "401: tells the user to sign in again");
assertTrue(/8/.test(deviceErrorMessage("approve", { status: 422 })), "approve 422: asks for the 8-character code");
assertTrue(/connect|network|reach/i.test(deviceErrorMessage("approve", { status: 0 })), "status 0 (fetch threw): network message");
assertTrue(/connect|network|reach/i.test(deviceErrorMessage("approve", {})), "no status: network message");
assertTrue(/no longer|not found|already/i.test(deviceErrorMessage("revoke", { status: 404 })), "revoke 404: the device is gone or not yours");
assertTrue(/load/i.test(deviceErrorMessage("list", { status: 500 })), "list 500: says the list could not be loaded");
assertTrue(/revoke/i.test(deviceErrorMessage("revoke", { status: 500 })), "revoke 500: mentions revoking");
assertTrue(/approve/i.test(deviceErrorMessage("approve", { status: 500 })), "approve 500: mentions approving");
assertTrue(/deny|refus/i.test(deviceErrorMessage("deny", { status: 500 })), "deny 500: mentions denying");
assertTrue(typeof deviceErrorMessage("nonsense", { status: 418 }) === "string" && deviceErrorMessage("nonsense", { status: 418 }).length > 0,
  "unknown action: still a non-empty string");
assertTrue(!/undefined|NaN|\[object/.test(["approve", "deny", "revoke", "list"].map((a) =>
  [0, 401, 404, 422, 429, 500, 503].map((s) => deviceErrorMessage(a, { status: s, retryAfter: s === 429 ? 900 : undefined })).join(" ")).join(" ")),
  "no combination prints undefined, NaN or [object Object]");

// ------------------------------------------------------------ trust statement
assertEqual(
  TRUST_STATEMENT,
  "Your files, history and settings are stored on your device. When you ask M2C to work, the relevant code is sent to our servers and our model providers to generate the answer; we don't keep it after the run ends.",
  "trust: the build plan section O1 sentence, character for character",
);
assertTrue(!/never leaves/i.test(TRUST_STATEMENT + TRUST_STATEMENT_EXTRA), "trust: never claims data never leaves the device");
assertTrue(/usage numbers/.test(TRUST_STATEMENT_EXTRA) && /revoke any device at any time/.test(TRUST_STATEMENT_EXTRA),
  "trust: the follow-up sentences cover usage numbers and revocation (section 11.3)");

if (failures) {
  console.error(`\n${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll devices helper assertions passed");
