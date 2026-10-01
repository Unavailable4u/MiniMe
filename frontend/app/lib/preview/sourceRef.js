// frontend/app/lib/preview/sourceRef.js — W7.3 (Build Workbench plan).
// The "attribute-reader adapter": turns what the inspector found on a
// clicked element in a USER'S OWN dev app into the one thing the rest of
// the workbench already understands — a `data-mm` range string
// (`path:startLine:startCol:endLine:endCol`, lines 1-based, columns
// 0-based, end exclusive; see mmRange.js / instrument.js) whose path is a
// file in THIS workspace.
//
// Three attributes can carry a source location, read in the frame by
// inspectorRuntime.js's external mode (which posts either `mm` or
// `locator`, never raw attribute names) and finished here in the parent:
//
//   data-mm="<path>:<sl>:<sc>:<el>:<ec>"
//       MiniMe's own contract. A full range already.
//   data-locatorjs-id="<fullPath>::<n>"            (@locator/babel-jsx's DEFAULT)
//       An index into `window.__LOCATOR_DATA__[fullPath].expressions[n].loc`,
//       which the frame resolves itself (same window as the page) into a
//       full range — so it arrives here as an `mm`, end included.
//   data-locatorjs="<fullPath>:<line>:<column>"
//       Emitted by @locator/webpack-loader always, and by
//       @locator/babel-jsx only with `dataAttribute: "path"`. START ONLY —
//       no end position — so it arrives as `locator` and the element's
//       end is recovered here by parsing the file (below).
//   (All three formats read from @locator/babel-jsx 0.5.1's and
//   @locator/webpack-loader 0.5.1's published source. `fullPath` there is
//   the build's absolute project dir + the file path; line is Babel's
//   1-based line, column Babel's 0-based column — the same bases as
//   data-mm — and the start is the element's opening `<`.)
//
// Two things make a raw path unusable as-is:
//   1. Locator's paths are ABSOLUTE ON THE DEVELOPER'S DISK
//      (`/Users/me/app/src/App.jsx`, `C:\Users\me\app\src\App.jsx`), while
//      a workspace's logical paths are relative (`src/App.jsx`). They are
//      matched by longest path-SUFFIX against the provider's own file
//      list (mapToWorkspacePath) — which is how a paired local folder
//      (LocalFileProvider's list) and a cloud workspace both work with no
//      knowledge of where the project root is on disk.
//   2. Only `data-locatorjs` lacks an end — see resolveSelectRef.
//
// Imports only the sibling pure helpers (`parseMm`/`offsetOf`), so plain
// `node` can test it (the test passes them through loadSource's map).
// @babel/parser is loaded lazily with a dynamic import(), exactly like
// instrument.js, so it never lands in a bundle that doesn't need it.
import { parseMm, offsetOf } from "./mmRange";

const MAX_REF_LENGTH = 1024;
const LOCATOR_RE = /^(.*):(\d+):(\d+)$/;
const JSX_EXTENSION_RE = /\.[jt]sx?$/i;
const MAX_TAG_SCAN = 4000;

/**
 * Parses from the RIGHT (a Windows drive letter's colon must not be
 * mistaken for a field separator) — same approach as parseMm().
 *
 * @param {string} value - a data-locatorjs value
 * @returns {{path: string, line: number, col: number} | null}
 */
export function parseLocator(value) {
  if (typeof value !== "string") return null;
  const m = LOCATOR_RE.exec(value);
  if (!m || !m[1]) return null;
  return { path: m[1], line: Number(m[2]), col: Number(m[3]) };
}

export function buildMm(path, startLine, startCol, endLine, endCol) {
  return `${path}:${startLine}:${startCol}:${endLine}:${endCol}`;
}

/**
 * Maps a path found in a page's attributes onto a file the workspace
 * actually has, or null.
 *
 * Order: (1) the path as given is already a workspace path; (2) the
 * LONGEST suffix of the path, on a segment boundary, that is one; (3) the
 * same two steps ignoring case, accepted only when exactly one workspace
 * file matches (Windows disks are case-insensitive, workspaces aren't).
 * Backslashes, a drive letter and leading slashes are normalized first.
 *
 * Known limit, stated rather than hidden: a workspace that holds only
 * `App.jsx` (the folder was paired one level too deep) still matches
 * `/Users/me/app/src/App.jsx` by its last segment — and so would an
 * unrelated `App.jsx` from a dependency. That is the price of not
 * needing to know where the project root is; a longer suffix always wins
 * when there is one.
 *
 * @param {string} rawPath
 * @param {Iterable<string>} knownPaths - workspace file paths (FileProvider.list() keys)
 * @returns {string|null}
 */
export function mapToWorkspacePath(rawPath, knownPaths) {
  if (typeof rawPath !== "string" || !rawPath) return null;
  const known = knownPaths instanceof Set ? knownPaths : new Set(knownPaths || []);
  const norm = rawPath
    .replace(/\\/g, "/")
    .replace(/^[A-Za-z]:/, "")
    .replace(/^(\.\/)+/, "")
    .replace(/^\/+/, "");
  if (!norm) return null;
  const segments = norm.split("/").filter(Boolean);

  for (let i = 0; i < segments.length; i++) {
    const candidate = segments.slice(i).join("/");
    if (known.has(candidate)) return candidate;
  }

  const lower = new Map();
  for (const p of known) {
    const k = p.toLowerCase();
    lower.set(k, lower.has(k) ? null : p); // null = ambiguous
  }
  for (let i = 0; i < segments.length; i++) {
    const hit = lower.get(segments.slice(i).join("/").toLowerCase());
    if (hit) return hit;
    if (hit === null) return null; // several files differ only by case — don't guess
  }
  return null;
}

function positionOf(text, offset) {
  let line = 1;
  let lastNl = -1;
  for (let i = text.indexOf("\n"); i !== -1 && i < offset; i = text.indexOf("\n", i + 1)) {
    line++;
    lastNl = i;
  }
  return { line, col: offset - (lastNl + 1) };
}

// Same metadata-key skip list and walk-every-property approach as
// instrument.js's (non-exported) walkAst — duplicated on purpose so this
// module doesn't depend on parse5 via instrument.js for a 15-line walker.
const WALK_SKIP_KEYS = new Set(["loc", "start", "end", "range", "leadingComments", "trailingComments", "innerComments", "extra"]);
function walkAst(node, visit) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const item of node) walkAst(item, visit);
    return;
  }
  if (typeof node.type === "string") visit(node);
  for (const key of Object.keys(node)) {
    if (WALK_SKIP_KEYS.has(key)) continue;
    const value = node[key];
    if (value && typeof value === "object") walkAst(value, visit);
  }
}

/**
 * Offset just past the `>` that closes the opening tag starting at
 * `offset` (which must point at `<`), skipping `>` inside quotes and
 * inside `{…}` expression containers (`onClick={() => x}` contains one).
 * Returns -1 when `offset` isn't a tag start or no end turns up within a
 * sane distance — the caller then degrades further.
 */
export function openingTagEnd(text, offset) {
  if (text[offset] !== "<") return -1;
  let depth = 0;
  let quote = null;
  const limit = Math.min(text.length, offset + MAX_TAG_SCAN);
  for (let i = offset + 1; i < limit; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") quote = ch;
    else if (ch === "{") depth++;
    else if (ch === "}") depth = Math.max(0, depth - 1);
    else if (ch === ">" && depth === 0) return i + 1;
  }
  return -1;
}

/**
 * Where does the element that opens at (line, col) end?
 *  1. JS/TS files: parse and find the JSXElement starting exactly there —
 *     its `loc.end` is the whole element (children and closing tag
 *     included), the same range instrumentJsx() would have stamped.
 *  2. Otherwise / on any failure: the end of the OPENING TAG only,
 *     flagged `approx` (a Vue/Svelte/HTML file, or a file that no longer
 *     parses mid-edit — it still selects the element's tag, which is
 *     enough to land on the right place).
 *  3. Nothing sensible: the end of that line, also `approx`.
 *
 * @returns {Promise<{line: number, col: number, approx: boolean}>}
 */
async function elementEnd(text, path, line, col) {
  if (JSX_EXTENSION_RE.test(path)) {
    try {
      const parser = await import("@babel/parser");
      const ast = parser.parse(text, { sourceType: "module", plugins: ["jsx", "typescript"] });
      let found = null;
      walkAst(ast, (node) => {
        if (found || node.type !== "JSXElement" || !node.loc) return;
        if (node.loc.start.line === line && node.loc.start.column === col) found = node;
      });
      if (found) return { line: found.loc.end.line, col: found.loc.end.column, approx: false };
    } catch {
      // A file mid-edit doesn't parse — degrade, never fail.
    }
  }
  const start = offsetOf(text, line, col);
  const tagEnd = openingTagEnd(text, start);
  if (tagEnd !== -1) {
    const p = positionOf(text, tagEnd);
    return { line: p.line, col: p.col, approx: true };
  }
  const nl = text.indexOf("\n", start);
  const p = positionOf(text, nl === -1 ? text.length : nl);
  return { line: p.line, col: p.col, approx: true };
}

/**
 * The adapter's one entry point. `data` is a `minime:select` payload from
 * the frame (UNTRUSTED — every field is type-checked and length-capped
 * here, and the result is re-validated by elementFromMessage() by the
 * caller); returns the `mm` to feed the ordinary click-to-code path.
 *
 * @param {{mm?: unknown, locator?: unknown}} data
 * @param {{knownPaths: Iterable<string>, readText: (path: string) => Promise<string>}} deps
 *   `readText` should prefer a live editor buffer over a server read
 *   (the location was computed against the running build, which saved or
 *   not is the closest thing to that text).
 * @returns {Promise<
 *   {mm: string, approx: boolean} |
 *   {error: "no-ref" | "unreadable" | "not-in-workspace" | "unreadable-file", path?: string}
 * >}
 */
export async function resolveSelectRef(data, { knownPaths, readText }) {
  const known = knownPaths instanceof Set ? knownPaths : new Set(knownPaths || []);

  if (typeof data?.mm === "string" && data.mm) {
    const parsed = parseMm(data.mm.slice(0, MAX_REF_LENGTH));
    if (!parsed) return { error: "unreadable" };
    const path = mapToWorkspacePath(parsed.path, known);
    if (!path) return { error: "not-in-workspace", path: parsed.path };
    return { mm: buildMm(path, parsed.startLine, parsed.startCol, parsed.endLine, parsed.endCol), approx: false };
  }

  if (typeof data?.locator === "string" && data.locator) {
    const loc = parseLocator(data.locator.slice(0, MAX_REF_LENGTH));
    if (!loc) return { error: "unreadable" };
    const path = mapToWorkspacePath(loc.path, known);
    if (!path) return { error: "not-in-workspace", path: loc.path };
    let text;
    try {
      text = await readText(path);
    } catch {
      return { error: "unreadable-file", path };
    }
    const end = await elementEnd(String(text ?? ""), path, loc.line, loc.col);
    return { mm: buildMm(path, loc.line, loc.col, end.line, end.col), approx: end.approx };
  }

  return { error: "no-ref" };
}
