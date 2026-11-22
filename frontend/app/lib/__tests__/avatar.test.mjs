// Patch SX-03 -- tests for lib/avatar.js (safeAvatarUrl) and a static guard
// on components/LandingPage.jsx.
//
// Why two kinds of test: the repo has no DOM library, and LandingPage.jsx is
// JSX that plain `node` cannot import or render (see the note in
// components/__tests__/buildTab.promoteGuard.test.mjs). So:
//   1. safeAvatarUrl is a pure function and is tested directly. The REAL
//      avatar.js is loaded through lib/workbench's loadSource.mjs with no
//      `imports`, so it must stay dependency-free.
//   2. A source-level test reads the REAL LandingPage.jsx and fails if
//      navAvatar.innerHTML is assigned anywhere (the bug), and checks that
//      the page really uses safeAvatarUrl and builds the <img> as an element.
//
// What this does NOT prove: that a browser shows the initial letter for a
// hostile value. That is the manual check in the patch description.
//
// Run: node frontend/app/lib/__tests__/avatar.test.mjs   (or `npm test`)
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadSource } from "../workbench/__tests__/loadSource.mjs";

const { safeAvatarUrl } = loadSource("../../avatar.js");

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

// --- safeAvatarUrl: accepted -------------------------------------------------

assertEqual(
  safeAvatarUrl("https://lh3.googleusercontent.com/a/x"),
  "https://lh3.googleusercontent.com/a/x",
  "a normal https avatar URL is accepted and returned unchanged",
);

assertEqual(
  safeAvatarUrl("HTTPS://Example.COM/pic.png?s=96"),
  "https://example.com/pic.png?s=96",
  "an https URL is returned in normalised form (lowercase scheme and host)",
);

{
  // A quote inside an otherwise valid https URL is percent-encoded by
  // new URL(), so even a careless caller could not break out of an attribute.
  const out = safeAvatarUrl('https://example.com/a"onerror="alert(1)');
  assertTrue(typeof out === "string" && !out.includes('"'),
    "a quote inside an https URL comes back percent-encoded, never raw");
}

// --- safeAvatarUrl: rejected -------------------------------------------------

const rejected = [
  ["http://example.com/a.png", "plain http"],
  ["javascript:alert(1)", "javascript: URL"],
  ["data:image/svg+xml,<svg onload=alert(1)>", "data: URL"],
  ['x" onerror="alert(1)', "attribute-breaking string with no scheme"],
  ["", "empty string"],
  [null, "null"],
  [undefined, "undefined"],
  [42, "a number"],
  [{ href: "https://example.com/" }, "an object"],
  ["x".repeat(5000) + '" onerror="alert(1)', "5000-character string with an injected quote"],
  ["//example.com/a.png", "protocol-relative URL"],
  ["/avatar.png", "relative path"],
  ["file:///etc/passwd", "file: URL"],
];
for (const [input, label] of rejected) {
  assertEqual(safeAvatarUrl(input), null, `rejected: ${label}`);
}

// --- static guard on LandingPage.jsx ----------------------------------------

const landingPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../components/LandingPage.jsx",
);
const landing = fs.readFileSync(landingPath, "utf8");

assertEqual(
  /navAvatar\s*(\.\s*innerHTML|\[\s*["']innerHTML["']\s*\])\s*\+?=/.test(landing),
  false,
  "LandingPage.jsx never assigns navAvatar.innerHTML",
);
assertTrue(
  /import\s*\{\s*safeAvatarUrl\s*\}\s*from\s*["']\.\.\/lib\/avatar["']/.test(landing),
  "LandingPage.jsx imports safeAvatarUrl from ../lib/avatar",
);
assertTrue(
  /safeAvatarUrl\(\s*avatarUrl\s*\)/.test(landing),
  "LandingPage.jsx passes avatarUrl through safeAvatarUrl",
);
assertTrue(
  /document\.createElement\(\s*['"]img['"]\s*\)/.test(landing),
  "LandingPage.jsx builds the avatar <img> with createElement",
);

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll avatar tests passed.");
