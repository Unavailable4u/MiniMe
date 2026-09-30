// frontend/app/lib/workbench/usageSites.js — W7.1b (Build Workbench
// plan, "Element chip → agent context": "Add 'select usage site' for
// components … otherwise fall back to a text search for
// `<ComponentName`").
//
// The problem: a click in the preview lands on a HOST element (`<button>`),
// and W6.3's instrumenter stamps host tags only — so for
//
//   // Button.jsx                       // App.jsx
//   export function Button(p) {          <Button variant="primary">Buy</Button>
//     return <button className="btn">…
//
// the element chip points inside Button.jsx. "Make THIS button green"
// may well mean the usage (`<Button variant="green">`), not the
// component's own markup, so the person needs a one-click way to get
// the usage site in front of the agent too. This is that lookup.
//
// This is the TEXT-SEARCH fallback only. The plan's first choice —
// walking React's fiber owner chain from the clicked node on a React 18
// dev build — needs the inspector runtime to reach into the preview's
// React internals, which I could not verify from here (the plan itself
// says "prototype first"), so it is deliberately not attempted.
//
// What the text search does and does not know (stated so nobody trusts
// it more than it deserves):
//   - it matches `<Name` at a tag boundary in source files and reports
//     the OPENING tag's range. It does not resolve imports, so a
//     different component that happens to share the name is a false
//     hit; a component re-exported under another name (`import {Button
//     as Btn}`) or used via a variable is a miss. The UI lists the hits
//     and lets the person choose; nothing is added automatically.
//   - `<Name.Sub` and `<NameFoo` are different components and do not
//     match; closing tags (`</Name>`) and TypeScript generics
//     (`Array<Name>`) do not match; a line that is only a comment does
//     not match.
//
// No imports (same dependency-free family as fileTree.js / mmRange.js)
// so plain `node` can test it. Offsets follow mmRange.js's offsetOf():
// lines are split on "\n" only, columns are 0-based, lines 1-based.

// Files that can hold a component usage. `.ts`/`.js` are in: plenty of
// React code is JSX in a .js file.
const USAGE_EXTENSIONS = /\.(jsx|tsx|js|mjs|ts|vue|svelte)$/i;
// Files whose own component definition we can name from the filename
// alone (a single-file component).
const SFC_EXTENSIONS = /\.(vue|svelte)$/i;
const SKIP_DIRS = new Set(["node_modules", "dist", "build", ".next", ".nuxt", ".svelte-kit", "out", "coverage", "vendor", ".git"]);

export const MAX_COMPONENT_NAMES = 6;
export const MAX_USAGE_SITES = 30;
export const MAX_TAG_CHARS = 4000;

/** @param {string} path @returns {boolean} can this file hold a component definition or usage? */
export function isComponentSourcePath(path) {
  return typeof path === "string" && USAGE_EXTENSIONS.test(path);
}

const PASCAL = /^[A-Z][A-Za-z0-9_$]*$/;
function isComponentName(name) {
  // PascalCase with at least one lowercase letter — rules out the
  // SHOUTY_CONSTANTS a `const X = (` pattern would otherwise collect.
  return PASCAL.test(name) && /[a-z]/.test(name);
}

function baseNameNoExt(path) {
  const base = path.slice(path.lastIndexOf("/") + 1);
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

function kebab(name) {
  return name.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase();
}

const NAME_PATTERNS = [
  // const Name = memo( / forwardRef( / React.memo( …
  /(?:const|let|var)\s+([A-Z][\w$]*)\s*(?::[^=]+)?=\s*(?:React\.)?(?:memo|forwardRef|lazy)\s*[(<]/g,
  // const Name = (props) => …   /   const Name = props => …   /   const Name = async (…) => …
  /(?:const|let|var)\s+([A-Z][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\(|[A-Za-z_$][\w$]*\s*=>)/g,
  // class Name extends Component / React.Component / PureComponent
  /class\s+([A-Z][\w$]*)\s+extends\s+(?:React\.)?(?:Pure)?Component\b/g,
  // Last, so a wrapper's own binding (`const Fancy = memo(function Inner …`)
  // is listed before the inner function's name: Fancy is what JSX uses.
  // export [default] [async] function Name(   /   function Name(   /   memo(function Name(
  /(?:^|[\s;(])(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s+([A-Z][\w$]*)\s*[(<]/g,
];
const DEFAULT_EXPORT_IDENT = /(?:^|\n)\s*export\s+default\s+([A-Z][\w$]*)\s*;?\s*(?:\n|$)/;

/**
 * The component names a source file plausibly defines, most likely
 * first (the default export, when it names one). Falls back to the file
 * name — `Button.jsx` -> `Button` — when nothing in the text matched, and
 * for `.vue`/`.svelte` (one component per file, named by the file) adds
 * the kebab-case form templates also use (`<my-button`).
 *
 * @param {string} path
 * @param {string} text
 * @returns {string[]} at most MAX_COMPONENT_NAMES, deduped; [] when the file can't hold a component
 */
export function componentNamesFromSource(path, text) {
  if (!isComponentSourcePath(path)) return [];
  const src = typeof text === "string" ? text : "";
  const names = [];
  const add = (n) => {
    if (n && !names.includes(n)) names.push(n);
  };

  if (SFC_EXTENSIONS.test(path)) {
    const base = baseNameNoExt(path);
    if (isComponentName(base)) {
      add(base);
      add(kebab(base));
    }
    return names.slice(0, MAX_COMPONENT_NAMES);
  }

  const def = DEFAULT_EXPORT_IDENT.exec(src);
  if (def && isComponentName(def[1])) add(def[1]);
  for (const re of NAME_PATTERNS) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(src)) !== null) {
      if (isComponentName(m[1])) add(m[1]);
      if (names.length >= MAX_COMPONENT_NAMES) return names;
    }
  }
  if (names.length === 0) {
    const base = baseNameNoExt(path);
    if (isComponentName(base)) add(base);
  }
  return names.slice(0, MAX_COMPONENT_NAMES);
}

/**
 * Index just past the `>` that closes the opening tag beginning at
 * `from` (the `<`), skipping `>` inside `{…}` expressions and inside
 * quoted attribute values. -1 when it isn't found within the cap — the
 * caller then settles for the first line.
 */
function openingTagEnd(text, from) {
  let depth = 0;
  let quote = null;
  const stop = Math.min(text.length, from + MAX_TAG_CHARS);
  for (let i = from + 1; i < stop; i++) {
    const ch = text[i];
    if (quote) {
      // A backslash escapes only inside a JS expression; in a JSX
      // attribute string (depth 0) it is an ordinary character.
      if (ch === "\\" && depth > 0) i++;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || (ch === "`" && depth > 0)) {
      quote = ch;
    } else if (ch === "{") {
      depth++;
    } else if (ch === "}") {
      depth = Math.max(0, depth - 1);
    } else if (ch === ">" && depth === 0) {
      return i + 1;
    }
  }
  return -1;
}

function lineStartsOf(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) starts.push(i + 1);
  return starts;
}

// Index of the last line start <= offset (binary search) — that line's
// 0-based index.
function lineIndexAt(starts, offset) {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= offset) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function isCommentLine(text, offset, lineStart) {
  const before = text.slice(lineStart, offset).trim();
  return before.startsWith("//") || before.startsWith("*") || before.startsWith("/*") || before.startsWith("{/*");
}

/**
 * The subset of `paths` worth reading for a usage search: source files
 * that can hold a tag, outside build output / vendored code, minus the
 * component's own file. Callers read these (provider.read is a network
 * call for Cloud) and hand the texts to findUsageSites().
 *
 * @param {string[]} paths
 * @param {string} [excludePath]
 * @returns {string[]} sorted
 */
export function usageCandidatePaths(paths, excludePath) {
  return (paths || [])
    .filter(
      (p) =>
        p !== excludePath && isComponentSourcePath(p) && !p.split("/").slice(0, -1).some((seg) => SKIP_DIRS.has(seg))
    )
    .sort();
}

/**
 * @typedef {object} UsageSite
 * @property {string} path
 * @property {string} name       which component name matched
 * @property {number} fromLine   1-based, the opening tag's first line
 * @property {number} toLine     1-based, its last line
 * @property {number} from       document offset of `<`
 * @property {number} to         document offset just past the opening tag's `>`
 * @property {string} snippet    the opening tag's text
 * @property {string} preview    the first line of it, trimmed, for a list row
 */

/**
 * Where `names` are used, as JSX/template tags, across `fileTexts`.
 *
 * @param {Record<string,string>} fileTexts - path -> current text
 * @param {string[]} names - from componentNamesFromSource()
 * @param {{excludePath?: string, max?: number}} [opts] - `excludePath`: the
 *   component's own file (its own `<Name` — recursion, a doc comment — is
 *   not a usage site)
 * @returns {{sites: UsageSite[], truncated: boolean}} sorted by path then position
 */
export function findUsageSites(fileTexts, names, { excludePath, max = MAX_USAGE_SITES } = {}) {
  const valid = (names || []).filter((n) => typeof n === "string" && /^[A-Za-z][\w$-]*$/.test(n));
  if (valid.length === 0) return { sites: [], truncated: false };
  // `<Name` at a tag boundary: not preceded by a word char (rules out
  // `Array<Name>`), not followed by more of a name, a member access or a
  // namespace (`<Name.Sub`, `<NameFoo`, `<Name:x`). `</Name>` has a `/`
  // after the `<` and never matches.
  const re = new RegExp(`(?<![\\w$])<(${valid.map(escapeRegExp).join("|")})(?![\\w$.:-])`, "g");

  const sites = [];
  let truncated = false;
  const paths = usageCandidatePaths(Object.keys(fileTexts || {}), excludePath);

  outer: for (const path of paths) {
    const text = fileTexts[path];
    if (typeof text !== "string" || !text.includes("<")) continue;
    const starts = lineStartsOf(text);
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      const from = m.index;
      const fromIdx = lineIndexAt(starts, from);
      if (isCommentLine(text, from, starts[fromIdx])) continue;
      let to = openingTagEnd(text, from);
      if (to === -1) {
        const nl = text.indexOf("\n", from);
        to = nl === -1 ? text.length : nl;
      }
      const toIdx = lineIndexAt(starts, Math.max(from, to - 1));
      const snippet = text.slice(from, to);
      sites.push({
        path,
        name: m[1],
        fromLine: fromIdx + 1,
        toLine: toIdx + 1,
        from,
        to,
        snippet,
        preview: snippet.split("\n")[0].trim().slice(0, 120),
      });
      if (sites.length >= max) {
        truncated = true;
        break outer;
      }
    }
  }
  return { sites, truncated };
}
