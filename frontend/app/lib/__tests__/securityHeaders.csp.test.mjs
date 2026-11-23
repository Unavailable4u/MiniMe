// Patch SX-29 -- tests for the report-only Content-Security-Policy built by
// frontend/securityHeaders.js (buildHeaderRules and friends).
//
// The REAL module is loaded (it has no dependencies), so these tests check the
// exact header values a deployment would send. They do NOT prove that a
// browser accepts the policy or that the app runs without violations; that is
// what the report-only period is for (see the comment at the top of
// securityHeaders.js). They also do not start a server; the real response
// headers were checked by hand with `next dev` and curl -I (see the patch
// description).
//
// Run: node frontend/app/lib/__tests__/securityHeaders.csp.test.mjs   (or `npm test`)
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(here, "../../..");
const require = createRequire(import.meta.url);
const { buildHeaderRules } = require(path.join(frontendDir, "securityHeaders.js"));

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
  assertEqual(Boolean(cond), true, msg);
}

// ---- helpers ------------------------------------------------------------------

const PROD = { NODE_ENV: "production" };
const FULL_ENV = {
  NODE_ENV: "production",
  NEXT_PUBLIC_API_URL: "https://api.example.com/v1",
  NEXT_PUBLIC_SUPABASE_URL: "https://abc.supabase.co",
  NEXT_PUBLIC_PUSHER_CLUSTER: "eu",
  NEXT_PUBLIC_SENTRY_DSN: "https://SECRETKEY123@o1.ingest.us.sentry.io/42",
  CSP_REPORT_URI: "https://csp.example.com/report",
};

function rulesFor(env) {
  const rules = buildHeaderRules(env);
  const page = rules.find((r) => r.source === "/:path*");
  const worker = rules.find((r) => r.source === "/workers/:path*");
  const get = (rule, key) => (rule ? rule.headers.filter((h) => h.key === key).map((h) => h.value) : []);
  return {
    rules,
    page,
    worker,
    enforced: get(page, "Content-Security-Policy"),
    pageReportOnly: get(page, "Content-Security-Policy-Report-Only"),
    workerReportOnly: get(worker, "Content-Security-Policy-Report-Only"),
  };
}

/** "a 'self'; b x y" -> Map { a => ["'self'"], b => ["x","y"] } (and counts directives). */
function parsePolicy(value) {
  const map = new Map();
  let count = 0;
  for (const part of value.split(";")) {
    const tokens = part.trim().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) continue;
    count++;
    map.set(tokens[0], tokens.slice(1));
  }
  return { map, count };
}

// ---- 1. what is enforced and what is only reported -------------------------------

{
  const r = rulesFor(FULL_ENV);
  assertEqual(r.enforced, ["frame-ancestors 'none'"],
    "the only ENFORCED CSP is frame-ancestors 'none'");
  assertEqual(r.pageReportOnly.length, 1, "the page has exactly one report-only policy");
  assertEqual(r.workerReportOnly.length, 1, "the worker rule has exactly one report-only policy");
  assertTrue(
    !r.page.headers.some((h) => h.key === "Content-Security-Policy" && h.value !== "frame-ancestors 'none'"),
    "the real policy is never sent under the enforcing header name",
  );
  assertTrue(
    r.page.headers.some((h) => h.key === "X-Frame-Options" && h.value === "DENY"),
    "the four SX-28 headers are still on the page rule",
  );
  assertTrue(!r.pageReportOnly[0].includes("frame-ancestors"),
    "frame-ancestors is not put in the report-only policy (browsers ignore it there)");
  assertEqual(r.rules.length, 2, "exactly two rules: every path, and /workers/*");
}

// ---- 2. the page policy, directive by directive ------------------------------------

{
  const r = rulesFor(FULL_ENV);
  const { map, count } = parsePolicy(r.pageReportOnly[0]);
  assertEqual(map.get("default-src"), ["'self'"], "default-src is 'self'");
  assertEqual(map.get("script-src"), ["'self'", "'unsafe-inline'"],
    "script-src (production) is 'self' + 'unsafe-inline' only");
  assertEqual(map.get("worker-src"), ["'self'"], "worker-src is 'self' (the only worker is /workers/pyodideWorker.js)");
  assertEqual(map.get("object-src"), ["'none'"], "object-src is 'none'");
  assertEqual(map.get("base-uri"), ["'self'"], "base-uri is 'self'");
  assertTrue(map.get("style-src").includes("https://fonts.googleapis.com"), "style-src allows Google Fonts css");
  assertTrue(map.get("font-src").includes("https://fonts.gstatic.com"), "font-src allows Google Fonts files");
  assertTrue(
    ["'self'", "data:", "blob:", "https:"].every((s) => map.get("img-src").includes(s)),
    "img-src allows self, data:, blob: and https:",
  );
  assertTrue(
    ["'self'", "blob:"].every((s) => map.get("media-src").includes(s)),
    "media-src allows self and blob: (audio/video object URLs)",
  );
  assertTrue(map.get("frame-src").includes("https://*.codesandbox.io"), "frame-src allows the Sandpack bundler");
  assertTrue(
    ["http://localhost:*", "http://127.0.0.1:*"].every((s) => map.get("frame-src").includes(s)),
    "frame-src allows the loopback URL preview",
  );
  assertTrue(
    !["blob:", "data:", "about:"].some((s) => map.get("frame-src").includes(s)),
    "frame-src does not allow blob:, data: or about: (nothing in the code needs them)",
  );
  assertEqual(count, 12, "12 directives with a report URI configured (no accidental extras)");
}

// ---- 3. no wildcards that defeat the policy, no nonces ---------------------------------

{
  const { pageReportOnly, workerReportOnly } = rulesFor(FULL_ENV);
  for (const [label, policy] of [["page", pageReportOnly[0]], ["worker", workerReportOnly[0]]]) {
    assertTrue(!/nonce-/.test(policy) && !/strict-dynamic/.test(policy), `${label} policy uses no nonces or strict-dynamic`);
    const tokens = policy.split(/[\s;]+/);
    assertTrue(!tokens.includes("*"), `${label} policy has no bare * source`);
    assertTrue(!tokens.includes("http:"), `${label} policy has no bare http: source`);
  }
  const { map } = parsePolicy(pageReportOnly[0]);
  assertTrue(!map.get("script-src").some((s) => /jsdelivr|^https:/.test(s)),
    "page script-src does not allow cdn.jsdelivr.net or any https host");
}

// ---- 4. development adds 'unsafe-eval'; production never does ------------------------------

{
  for (const [label, env, expected] of [
    ["production", { NODE_ENV: "production" }, false],
    ["development", { NODE_ENV: "development" }, true],
    ["NODE_ENV unset", {}, true],
  ]) {
    const { pageReportOnly } = rulesFor(env);
    const hasEval = parsePolicy(pageReportOnly[0]).map.get("script-src").includes("'unsafe-eval'");
    assertEqual(hasEval, expected, `'unsafe-eval' in script-src: ${label} -> ${expected}`);
  }
  const { workerReportOnly } = rulesFor({ NODE_ENV: "development" });
  assertTrue(!workerReportOnly[0].includes("'unsafe-eval'"), "the worker policy never has 'unsafe-eval'");
}

// ---- 5. connect-src follows the deployment's own environment ----------------------------------

{
  const { pageReportOnly } = rulesFor(FULL_ENV);
  const connect = parsePolicy(pageReportOnly[0]).map.get("connect-src");
  assertEqual(
    connect,
    [
      "'self'",
      "https://api.example.com",
      "https://abc.supabase.co",
      "wss://ws-eu.pusher.com",
      "https://sockjs-eu.pusher.com",
      "https://o1.ingest.us.sentry.io",
    ],
    "connect-src lists self, the API, Supabase, Pusher (ws + fallback) and the Sentry host (origins only, no paths)",
  );
  assertTrue(!pageReportOnly[0].includes("SECRETKEY123"), "the Sentry DSN key never appears in the header");

  // Nothing set: only 'self' plus the same localhost:8000 fallback the app code uses.
  const bare = parsePolicy(rulesFor(PROD).pageReportOnly[0]).map.get("connect-src");
  assertEqual(bare, ["'self'", "http://localhost:8000"], "with no env set: 'self' + the http://localhost:8000 API fallback");
}

// ---- 6. hostile or broken env values cannot change the policy ----------------------------------

{
  const baseline = parsePolicy(rulesFor(PROD).pageReportOnly[0]);
  const hostile = [
    ["NEXT_PUBLIC_API_URL", "javascript:alert(1)"],
    ["NEXT_PUBLIC_API_URL", "http://a;script-src *"],
    ["NEXT_PUBLIC_API_URL", "https://ok.example.com; script-src https://evil.example"],
    ["NEXT_PUBLIC_SUPABASE_URL", "https://user:pw@abc.supabase.co"],
    ["NEXT_PUBLIC_SUPABASE_URL", "ftp://abc.supabase.co"],
    ["NEXT_PUBLIC_SUPABASE_URL", "not a url"],
    ["NEXT_PUBLIC_SENTRY_DSN", "https://key@evil.example,other.example/1"],
    ["NEXT_PUBLIC_PUSHER_CLUSTER", "eu.evil.example"],
    ["NEXT_PUBLIC_PUSHER_CLUSTER", "eu; script-src *"],
    ["CSP_REPORT_URI", "https://x.example/r;script-src *"],
  ];
  for (const [name, value] of hostile) {
    const { pageReportOnly, workerReportOnly } = rulesFor({ ...PROD, [name]: value });
    const parsed = parsePolicy(pageReportOnly[0]);
    // The hostile value is dropped: same directives, same script-src, no
    // report-uri. connect-src is the baseline too, except that a SET but invalid
    // API URL does not fall back to localhost:8000 (the app would not use that
    // fallback either, because it only applies when the variable is empty).
    const expectedConnect = name === "NEXT_PUBLIC_API_URL" ? ["'self'"] : baseline.map.get("connect-src");
    assertEqual(
      [parsed.count, parsed.map.get("script-src"), parsed.map.get("connect-src"), parsed.map.has("report-uri")],
      [baseline.count, baseline.map.get("script-src"), expectedConnect, false],
      `ignored: ${name}=${value.slice(0, 40)}`,
    );
    assertTrue(!/evil/.test(pageReportOnly[0]) && !/evil/.test(workerReportOnly[0]), `no evil.example in any header for ${name}=${value.slice(0, 30)}`);
  }
}

// ---- 7. the report endpoint (decision D-7: optional) -------------------------------------------------

{
  const unset = rulesFor(PROD);
  assertTrue(!unset.pageReportOnly[0].includes("report-uri") && !unset.workerReportOnly[0].includes("report-uri"),
    "CSP_REPORT_URI unset: no report-uri in either policy (reports are not collected)");

  const set = rulesFor({ ...PROD, CSP_REPORT_URI: "https://csp.example.com/report?k=1" });
  assertTrue(parsePolicy(set.pageReportOnly[0]).map.get("report-uri")?.[0] === "https://csp.example.com/report?k=1",
    "CSP_REPORT_URI set: page policy carries it");
  assertTrue(parsePolicy(set.workerReportOnly[0]).map.get("report-uri")?.[0] === "https://csp.example.com/report?k=1",
    "CSP_REPORT_URI set: worker policy carries it");

  for (const bad of ["", "   ", "ftp://x.example/r", "javascript:alert(1)", "/relative/path", "https://u:p@x.example/r"]) {
    const r = rulesFor({ ...PROD, CSP_REPORT_URI: bad });
    assertTrue(!r.pageReportOnly[0].includes("report-uri"), `CSP_REPORT_URI=${JSON.stringify(bad)} is ignored`);
  }
}

// ---- 8. the worker policy (Pyodide) ------------------------------------------------------------------

{
  const { workerReportOnly } = rulesFor(FULL_ENV);
  const { map } = parsePolicy(workerReportOnly[0]);
  assertEqual(map.get("script-src"), ["'self'", "https://cdn.jsdelivr.net", "'wasm-unsafe-eval'"],
    "worker script-src: self, jsdelivr (importScripts fallback) and wasm-unsafe-eval");
  assertEqual(map.get("connect-src"), ["'self'", "https://cdn.jsdelivr.net"],
    "worker connect-src: self and jsdelivr (Python packages)");
  assertTrue(!map.get("script-src").includes("'unsafe-inline'"), "the worker policy has no 'unsafe-inline'");
  assertTrue(!parsePolicy(rulesFor(FULL_ENV).pageReportOnly[0]).map.get("script-src").includes("'wasm-unsafe-eval'"),
    "the page policy does not get 'wasm-unsafe-eval' (only the worker runs WebAssembly)");
}

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll securityHeaders CSP tests passed.");
