// frontend/app/lib/preview/detectKind.js — W6.1 (Build Workbench plan).
//
// Picks which preview provider a project's file list calls for. No
// imports on purpose — same reasoning as lib/workbench/fileTree.js and
// tabUtils.js: plain data in, plain data out, so __tests__/detectKind.test.mjs
// can load THIS file's real source and run it with plain `node`.
//
// Scope decision, since the plan's own wording ("package.json/JSX entry
// with react") leaves room to read package.json's actual dependency
// list: this stays METADATA-ONLY (file paths + the small metadata
// FileProvider.list() already returns — no content, no network, no
// provider passed in), so detectKind can run synchronously and be
// tested the same dependency-free way as every other lib/workbench/
// file. "React project" here means "has a package.json AND at least one
// .jsx/.tsx file" — a structural signal, not "package.json declares
// react as a dependency". That's good enough to route to a provider;
// W6.6 (the actual React/Sandpack provider) is the piece that needs
// package.json's real content, and it already has to read files anyway
// to hand Sandpack a dependency list.
//
// Priority matters here, and isn't just the order the plan happens to
// list the three kinds in: a Vite or Create-React-App project commonly
// ships its own root or public/index.html (usually just a `<div
// id="root">` and a `<script type="module" src="/src/main.jsx">`) —
// the static provider has no bundler and can't resolve that script's
// module imports, so serving that file as-is would render a blank
// page. Checking react/python (the more specific, structural signals)
// BEFORE falling back to the general "any index.html" static check
// avoids that misclassification.

const JSX_RE = /\.[jt]sx$/i;
const PY_RE = /\.py$/i;
const INDEX_HTML_RE = /(^|\/)index\.html?$/i;

/**
 * @param {{[path: string]: object}} filesMeta - FileProvider.list()'s result (flat, no directory rows)
 * @returns {{kind: "react"|"python"|"static"|null, entryPath: string|null, reason: string|null}}
 *   `entryPath` is only set for "static" (the file to bundle from);
 *   react/python don't have one yet — W6.6 and the python provider each
 *   pick their own entry point when they render. `reason` is a short,
 *   human-readable explanation for the `null` case, meant to be shown
 *   directly (the plan's own "No preview available for this project
 *   type — here's why").
 */
export function detectKind(filesMeta) {
  const paths = Object.keys(filesMeta || {});

  if (paths.includes("package.json") && paths.some((p) => JSX_RE.test(p))) {
    return { kind: "react", entryPath: null, reason: null };
  }

  if (paths.some((p) => PY_RE.test(p))) {
    return { kind: "python", entryPath: null, reason: null };
  }

  // Shortest path wins so a root index.html is preferred over a nested
  // one (e.g. a docs/index.html buried in an otherwise non-static
  // project) — depth is measured in path segments, not string length,
  // so "public/index.html" (2 segments) beats "a/b/index.html" (3)
  // regardless of which literal string happens to be longer.
  const htmlCandidates = paths.filter((p) => INDEX_HTML_RE.test(p));
  if (htmlCandidates.length) {
    htmlCandidates.sort((a, b) => a.split("/").length - b.split("/").length);
    return { kind: "static", entryPath: htmlCandidates[0], reason: null };
  }

  return {
    kind: null,
    entryPath: null,
    reason: paths.length
      ? "No index.html, React entry, or Python file found — nothing here matches a preview provider yet."
      : "This project has no files yet.",
  };
}
