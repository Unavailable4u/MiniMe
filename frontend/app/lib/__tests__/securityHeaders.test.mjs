// Patch SX-28 -- tests for frontend/securityHeaders.js and its use in
// next.config.js.
//
// securityHeaders.js is a dependency-free CommonJS module, so the REAL file
// is loaded here (no copy). next.config.js cannot be loaded by plain node
// (it needs @sentry/nextjs), so a source-level test checks that it requires
// the module and returns it from headers() for every path.
//
// What this does NOT prove: that a running server sends the headers, or that
// the app still works with them. That is the manual smoke list in the patch
// description (app loads, sign-in works, HTML previews still render).
//
// Run: node frontend/app/lib/__tests__/securityHeaders.test.mjs   (or `npm test`)
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(here, "../../..");
const require = createRequire(import.meta.url);
const { securityHeaders } = require(path.join(frontendDir, "securityHeaders.js"));

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

const byKey = Object.fromEntries(securityHeaders.map((h) => [h.key, h.value]));

// --- the four headers, exact values -----------------------------------------

assertEqual(byKey["X-Content-Type-Options"], "nosniff", "X-Content-Type-Options is nosniff");
assertEqual(
  byKey["Referrer-Policy"],
  "strict-origin-when-cross-origin",
  "Referrer-Policy is strict-origin-when-cross-origin",
);
assertEqual(byKey["X-Frame-Options"], "DENY", "X-Frame-Options is DENY");
assertEqual(
  byKey["Permissions-Policy"],
  "camera=(), geolocation=(), microphone=()",
  "Permissions-Policy denies camera, geolocation and microphone",
);

// --- shape ---------------------------------------------------------------------

assertEqual(securityHeaders.length, 4, "exactly four headers (no accidental extras)");
assertEqual(
  new Set(securityHeaders.map((h) => h.key)).size,
  securityHeaders.length,
  "no header key appears twice",
);
assertTrue(
  securityHeaders.every((h) => typeof h.key === "string" && typeof h.value === "string" && h.value.length > 0),
  "every entry is { key, value } with non-empty strings",
);
assertTrue(
  !("Strict-Transport-Security" in byKey),
  "HSTS is not set here (it belongs to the reverse proxy)",
);
assertTrue(
  !("Content-Security-Policy" in byKey) && !("Content-Security-Policy-Report-Only" in byKey),
  "no CSP here (that is SX-29)",
);

// --- next.config.js wires it in for every path -------------------------------------

const config = fs.readFileSync(path.join(frontendDir, "next.config.js"), "utf8");
assertTrue(
  /require\(\s*["']\.\/securityHeaders["']\s*\)/.test(config),
  "next.config.js requires ./securityHeaders",
);
assertTrue(
  /async\s+headers\s*\(\s*\)/.test(config),
  "next.config.js defines async headers()",
);
assertTrue(
  /source:\s*["']\/:path\*["']\s*,\s*headers:\s*securityHeaders/.test(config),
  "headers() applies securityHeaders to every path (/:path*)",
);

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll securityHeaders tests passed.");
