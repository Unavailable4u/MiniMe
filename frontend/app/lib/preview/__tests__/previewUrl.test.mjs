// W7.3 (Build Workbench plan) — tests for lib/preview/previewUrl.js.
// previewUrl.js has no imports, so it is loaded with no `imports` map:
// an added `import` line makes this test fail loudly (loadSource.mjs's
// own contract).
//
// Run: node frontend/app/lib/preview/__tests__/previewUrl.test.mjs
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

const {
  normalizePreviewUrl,
  buildInspectorSnippet,
  previewPrefsKey,
  normalizePreviewPrefs,
  loadPreviewPrefs,
  savePreviewPrefs,
  INSPECTOR_SCRIPT_PATH,
  DEFAULT_PREVIEW_PREFS,
} = loadSource("../../preview/previewUrl.js");

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

const OWN = "http://localhost:3000"; // a dev build of MiniMe itself

// --- accepted addresses -----------------------------------------------------

{
  const r = normalizePreviewUrl("localhost:5173", OWN);
  assertEqual(r, { ok: true, url: "http://localhost:5173/", origin: "http://localhost:5173" }, "a bare host:port gets http:// (new URL('localhost:5173') alone would parse 'localhost:' as a scheme)");
}
assertEqual(normalizePreviewUrl("  http://localhost:5173  ", OWN).url, "http://localhost:5173/", "surrounding whitespace is trimmed");
assertEqual(normalizePreviewUrl("http://127.0.0.1:8080/app?x=1#top", OWN).url, "http://127.0.0.1:8080/app?x=1#top", "127.0.0.1 with path, query and hash is kept as typed");
assertEqual(normalizePreviewUrl("http://127.5.6.7:3001", OWN).ok, true, "the whole 127.0.0.0/8 block is loopback");
assertEqual(normalizePreviewUrl("http://[::1]:5173/", OWN).ok, true, "IPv6 loopback [::1] is accepted");
assertEqual(normalizePreviewUrl("http://myapp.localhost:5173", OWN).ok, true, "*.localhost is loopback");
assertEqual(normalizePreviewUrl("https://localhost:5173", OWN).origin, "https://localhost:5173", "https on localhost is accepted and its origin reported");
assertEqual(normalizePreviewUrl("LOCALHOST:5173", OWN).ok, true, "host case doesn't matter");

// --- refused addresses ------------------------------------------------------

for (const bad of ["", "   ", "http://", "not a url", "http://exa mple.com"]) {
  assertEqual(normalizePreviewUrl(bad, OWN).ok, false, `refuses ${JSON.stringify(bad)}`);
}
assertEqual(normalizePreviewUrl("https://example.com", OWN).ok, false, "refuses a non-loopback host (scripts + same-origin must never meet an arbitrary remote site)");
assertEqual(normalizePreviewUrl("http://192.168.1.20:5173", OWN).ok, false, "refuses a LAN address — loopback only in v1");
assertEqual(normalizePreviewUrl("http://localhost.evil.com:5173", OWN).ok, false, "refuses a look-alike host that merely starts with localhost");
assertEqual(normalizePreviewUrl("http://evil.com/localhost", OWN).ok, false, "refuses a host whose PATH says localhost");
assertEqual(normalizePreviewUrl("http://128.0.0.1", OWN).ok, false, "128.x is not loopback");
assertEqual(normalizePreviewUrl("javascript:alert(1)", OWN).ok, false, "refuses javascript:");
assertEqual(normalizePreviewUrl("file:///etc/passwd", OWN).ok, false, "refuses file:");
assertEqual(normalizePreviewUrl("data:text/html,<script>1</script>", OWN).ok, false, "refuses data:");
assertEqual(normalizePreviewUrl("ftp://localhost/", OWN).ok, false, "refuses ftp:");
assertEqual(normalizePreviewUrl("http://user:pw@localhost:5173", OWN).ok, false, "refuses embedded credentials");
assertEqual(normalizePreviewUrl("http://localhost:5173/" + "a".repeat(3000), OWN).ok, false, "refuses an absurdly long address");
assert(typeof normalizePreviewUrl("https://example.com", OWN).error === "string" && normalizePreviewUrl("https://example.com", OWN).error.length > 0, "a refusal carries a human-readable reason");

// --- MiniMe must never frame itself (scripts + same-origin would let the copy drop its own sandbox) ---

assertEqual(normalizePreviewUrl("http://localhost:3000", OWN).ok, false, "refuses MiniMe's own origin");
assertEqual(normalizePreviewUrl("localhost:3000/some/page", OWN).ok, false, "refuses MiniMe's own origin with a path, scheme omitted");
assertEqual(normalizePreviewUrl("http://127.0.0.1:3000", OWN).ok, false, "refuses the 127.0.0.1 alias of MiniMe's own port (a different origin to the browser, still MiniMe)");
assertEqual(normalizePreviewUrl("http://[::1]:3000", OWN).ok, false, "refuses the [::1] alias of MiniMe's own port");
assertEqual(normalizePreviewUrl("http://localhost:3001", OWN).ok, true, "a different port on localhost is fine");
assertEqual(normalizePreviewUrl("https://localhost:3000", OWN).ok, true, "same port but a different protocol is a different server");
assertEqual(normalizePreviewUrl("http://localhost:80", "http://localhost").ok, false, "default-port equivalence: :80 is MiniMe when MiniMe is http://localhost");
assertEqual(normalizePreviewUrl("https://minime.example.com", "https://minime.example.com").ok, false, "a deployed MiniMe's own origin is refused (and would be anyway as non-loopback)");
assertEqual(normalizePreviewUrl("http://localhost:5173", "https://minime.example.com").ok, true, "a deployed (https, non-loopback) MiniMe can preview localhost:5173");
assertEqual(normalizePreviewUrl("http://localhost:5173", "").ok, true, "with no ownOrigin (SSR) the loopback rule alone applies");
assertEqual(normalizePreviewUrl("http://localhost:5173", "not a url").ok, true, "an unparseable ownOrigin doesn't throw or block");

// --- snippet ----------------------------------------------------------------

assertEqual(INSPECTOR_SCRIPT_PATH, "/mm-inspector.js", "the script path matches app/mm-inspector.js/route.js");
assertEqual(
  buildInspectorSnippet("http://localhost:3000"),
  '<script src="http://localhost:3000/mm-inspector.js" data-mm-origin="http://localhost:3000" defer></script>',
  "the snippet loads the script from MiniMe's origin and pins it as data-mm-origin"
);
assertEqual(buildInspectorSnippet("https://minime.example.com/"), '<script src="https://minime.example.com/mm-inspector.js" data-mm-origin="https://minime.example.com" defer></script>', "a trailing slash on the origin doesn't double up");
assert(!buildInspectorSnippet('http://x"><script>alert(1)</script>').includes('"><script>'), "an origin with quotes/angle brackets can't break out of the attribute");

// --- prefs ------------------------------------------------------------------

assertEqual(previewPrefsKey("ws-1"), "minime_build_editor_preview_source:ws-1", "per-workspace key in the existing minime_build_editor_* family");
assertEqual(normalizePreviewPrefs(null), { mode: "files", url: "" }, "null normalizes to the defaults");
assertEqual(normalizePreviewPrefs({ mode: "url", url: "http://localhost:5173/" }), { mode: "url", url: "http://localhost:5173/" }, "valid prefs survive");
assertEqual(normalizePreviewPrefs({ mode: "hologram", url: 42 }), { mode: "files", url: "" }, "wrong-typed fields fall back per field");
assertEqual(normalizePreviewPrefs({ mode: "url", url: 42 }), { mode: "url", url: "" }, "one bad field doesn't discard the good one");
assertEqual(normalizePreviewPrefs([]), { mode: "files", url: "" }, "an array is not prefs");
assertEqual(normalizePreviewPrefs({ url: "x".repeat(5000) }).url.length, 2048, "a huge saved url is capped");
assertEqual(DEFAULT_PREVIEW_PREFS, { mode: "files", url: "" }, "defaults are files / no url");

function fakeStorage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = v;
    },
    removeItem: (k) => {
      delete data[k];
    },
  };
}
{
  const st = fakeStorage();
  savePreviewPrefs(st, "w", { mode: "url", url: "http://localhost:5173/" });
  assertEqual(loadPreviewPrefs(st, "w"), { mode: "url", url: "http://localhost:5173/" }, "save → load round-trips");
  assertEqual(loadPreviewPrefs(st, "other"), { mode: "files", url: "" }, "another workspace is unaffected");
  savePreviewPrefs(st, "w", { mode: "files", url: "" });
  assertEqual(Object.keys(st.data), [], "saving the defaults removes the key instead of writing it");
  savePreviewPrefs(st, "w", { mode: "files", url: "http://localhost:5173/" });
  assertEqual(loadPreviewPrefs(st, "w"), { mode: "files", url: "http://localhost:5173/" }, "a remembered URL survives while on the files tab");
  savePreviewPrefs(st, "w", { ws: "w", mode: "url", url: "http://localhost:1/" });
  assertEqual(JSON.parse(st.data[previewPrefsKey("w")]), { mode: "url", url: "http://localhost:1/" }, "extra fields (the pane's own `ws` tag) are not persisted");
}
assertEqual(loadPreviewPrefs(fakeStorage({ [previewPrefsKey("w")]: "{not json" }), "w"), { mode: "files", url: "" }, "unreadable saved JSON falls back to the defaults");
assertEqual(loadPreviewPrefs(null, "w"), { mode: "files", url: "" }, "no storage (blocked) → defaults");
assertEqual(loadPreviewPrefs(fakeStorage(), undefined), { mode: "files", url: "" }, "no workspace id → defaults, no crash");
{
  let threw = false;
  try {
    savePreviewPrefs({ setItem() { throw new Error("quota"); }, removeItem() { throw new Error("quota"); } }, "w", { mode: "url", url: "http://localhost:1/" });
    savePreviewPrefs(null, "w", { mode: "url" });
    savePreviewPrefs(fakeStorage(), undefined, { mode: "url" });
  } catch {
    threw = true;
  }
  assertEqual(threw, false, "a storage failure (quota / blocked) is swallowed, never thrown");
}

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll previewUrl.js tests passed.");
}
