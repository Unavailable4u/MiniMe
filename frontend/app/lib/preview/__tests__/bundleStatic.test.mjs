// W6.1 (Build Workbench plan) — tests for lib/preview/bundleStatic.js.
//
// Loads the REAL bundleStatic.js through loadSource.mjs (see
// instrument.test.mjs's header for why this pattern, and why it's a
// reuse of lib/workbench/'s loadSource.mjs rather than a copy).
// bundleStatic.js's one static import is `parse5` — same real,
// actually-installed package instrument.test.mjs already proved this
// pattern works with (npm install parse5 in frontend/ first).
//
// Run: node frontend/app/lib/preview/__tests__/bundleStatic.test.mjs
import * as parse5 from "parse5";
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

const { bundleStatic } = loadSource("../../preview/bundleStatic.js", { imports: { parse5 } });

let failures = 0;
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}
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

// A stub resolveFile backed by a plain {path: content} map, standing in
// for PreviewPane.jsx's real one (buffers-then-provider.read()) — this
// file has no business knowing about either.
function fileMap(map) {
  return async (path) => (Object.prototype.hasOwnProperty.call(map, path) ? map[path] : null);
}

// --- basic inline: stylesheet + script, both at the project root -----

{
  const entryContent = [
    "<!DOCTYPE html>",
    "<html><head><title>x</title>",
    '<link rel="stylesheet" href="style.css">',
    "</head><body>",
    "<p>hi</p>",
    '<script src="app.js"></script>',
    "</body></html>",
  ].join("\n");

  const result = await bundleStatic({
    entryPath: "index.html",
    entryContent,
    resolveFile: fileMap({ "style.css": "body { color: red; }", "app.js": 'console.log("hi");' }),
  });

  assertEqual(result.warnings, [], "clean bundle: no warnings when every referenced file resolves");
  assert(result.html.includes("<style>body { color: red; }</style>"), "the <link> becomes an inline <style> with the CSS content");
  assert(result.html.includes('<script>console.log("hi");</script>'), "the <script src> loses its src and gains the JS as inline content");
  assert(!result.html.includes("style.css"), "the original href is gone once inlined");
  assert(!result.html.includes("app.js"), "the original src is gone once inlined");
}

// --- external URLs pass through untouched -----------------------------

{
  const entryContent = [
    '<link rel="stylesheet" href="https://cdn.example.com/x.css">',
    '<script src="//other-cdn.example.com/lib.js"></script>',
    '<script src="app.js"></script>',
  ].join("\n");

  const resolveFile = async (path) => {
    throw new Error(`resolveFile should never be called for an external URL, got: ${path}`);
  };

  // app.js still needs to resolve, so use a map for it and let the
  // external ones prove they're never even asked about.
  const result = await bundleStatic({
    entryPath: "index.html",
    entryContent,
    resolveFile: async (path) => (path === "app.js" ? "1;" : resolveFile(path)),
  });

  assert(result.html.includes('href="https://cdn.example.com/x.css"'), "an absolute https href is left as a real <link>, not inlined");
  assert(result.html.includes('src="//other-cdn.example.com/lib.js"'), "a protocol-relative script src is left alone too");
  assert(result.html.includes("<script>1;</script>"), "the one genuinely local script still gets inlined");
}

// --- missing local file: warning, tag left alone -----------------------

{
  const entryContent = '<link rel="stylesheet" href="missing.css"><p>hi</p>';
  const result = await bundleStatic({
    entryPath: "index.html",
    entryContent,
    resolveFile: fileMap({}),
  });
  assert(result.warnings.length === 1 && result.warnings[0].includes("missing.css"), "a local file that isn't found gets exactly one warning naming it");
  assert(result.html.includes('href="missing.css"'), "the tag is left as-is (not silently dropped) when the file can't be found");
}

// --- relative path resolution: subdirectory entry, ../, ./ -------------

{
  const entryContent = [
    '<link rel="stylesheet" href="./style.css">',
    '<script src="../shared/util.js"></script>',
  ].join("\n");

  const seen = [];
  const result = await bundleStatic({
    entryPath: "public/index.html",
    entryContent,
    resolveFile: async (path) => {
      seen.push(path);
      return path === "public/style.css" ? "a{}" : path === "shared/util.js" ? "1;" : null;
    },
  });

  assert(seen.includes("public/style.css"), "./style.css from public/index.html resolves to public/style.css");
  assert(seen.includes("shared/util.js"), "../shared/util.js from public/index.html resolves to shared/util.js (one level up, out of public/)");
  assert(result.warnings.length === 0, "both resolve cleanly, no warnings");
}

// --- root-absolute path treated as relative to the entry's own dir -----

{
  const entryContent = '<script src="/app.js"></script>';
  const result = await bundleStatic({
    entryPath: "index.html",
    entryContent,
    resolveFile: fileMap({ "app.js": "1;" }),
  });
  assert(result.html.includes("<script>1;</script>"), "a root-absolute /app.js resolves relative to the project root (index.html's own directory), same as a real static file server would serve it");
}

// --- a script tag with inline content (no src) is left completely alone

{
  const entryContent = "<script>console.log(1);</script>";
  const result = await bundleStatic({ entryPath: "index.html", entryContent, resolveFile: fileMap({}) });
  assert(result.html.includes("<script>console.log(1);</script>"), "an inline script with no src is untouched (nothing to inline)");
  assertEqual(result.warnings, [], "no warning for a script that was never meant to be inlined");
}

// --- malformed entry HTML never throws ---------------------------------

{
  // parse5 essentially never throws (see the module's own comment), but
  // the contract must hold even if it somehow did -- pass a genuinely
  // non-string to prove the catch path returns the input untouched
  // rather than propagating.
  const badInput = null;
  let result;
  try {
    result = await bundleStatic({ entryPath: "index.html", entryContent: badInput, resolveFile: fileMap({}) });
  } catch (err) {
    result = { threw: true };
  }
  assert(!result.threw, "bundleStatic never throws outward, even on bad input — it returns a result with a warning instead");
}

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
