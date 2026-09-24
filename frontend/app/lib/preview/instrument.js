// frontend/app/lib/preview/instrument.js — W6.3 (Build Workbench plan).
//
// Source instrumenter for the preview inspector (W6.4/W6.5): stamps every
// element in a file with a `data-mm` attribute so a click inside the
// rendered preview can be traced back to an exact range in the file that
// produced it.
//
// THE CONTRACT — everything downstream (W6.4's inspector runtime, W6.5's
// click-to-code, W7.1's element-scoped edits) depends on this exact shape,
// so it's written down once here rather than re-derived at each call site:
//
//     data-mm="<path>:<startLine>:<startCol>:<endLine>:<endCol>"
//
//   - lines are 1-based, columns are 0-based, end is EXCLUSIVE.
//   - the range covers the WHOLE element: opening tag through closing tag
//     (or through the self-closing `/>` for a void/self-closing element —
//     there is no separate "just the opening tag" range).
//   - `path` is whatever the caller passes as `filePath` — this module
//     doesn't resolve or validate it, callers pass the same workspace-
//     relative path the file tree already uses.
//
// Two independent implementations behind one dispatcher (instrumentSource,
// at the bottom), because HTML and JSX need fundamentally different
// strategies:
//
//   - HTML (instrumentHtml): parse5 gives back a fully mutable tree with
//     exact source locations AND a spec-correct serializer, so this adds
//     the attribute directly to each element's attrs array and lets
//     parse5.serialize() regenerate the markup. This is what a real
//     browser's HTML5 parsing algorithm would produce for the same input
//     (implied tags, void-element normalization, etc. included) — safe
//     for a preview iframe, which is about to re-parse the HTML itself
//     anyway, but NOT a byte-for-byte round-trip of the original source.
//
//   - JSX/TSX (instrumentJsx): there is no equivalent safe round-trip
//     serializer for a Babel AST that preserves the developer's original
//     formatting (re-printing loses comments, quote style, blank lines).
//     So this walks the AST for source *offsets* only and splices
//     ` data-mm="..."` directly into the ORIGINAL source string, back to
//     front (highest offset first) so already-computed offsets for
//     earlier elements stay valid as later ones shift the string.
//
// Rules that apply to both (see each function's own doc comment for how):
//   - operates on copies only — takes a code string, returns a new code
//     string; never touches disk, an editor buffer, or any shared state.
//   - skips an element that already carries a data-mm attribute, so
//     re-running this on already-instrumented output is a safe no-op for
//     that element rather than a duplicate/conflicting attribute.
//   - if parsing fails outright, returns the ORIGINAL code untouched plus
//     a `note` — this must never be the thing that breaks the preview.
//
// Lazy-loading: @babel/parser is a full JS/TS parser and not small (see
// this module's own entry in the build plan's library table) — it's only
// pulled in via a dynamic import() inside instrumentJsx, so a pure-HTML
// preview never loads it. parse5 is comparatively small and needed by
// nearly every preview kind (even a React/Sandpack project typically still
// has an index.html shell), so it's a plain static import here.

import * as parse5 from "parse5";

// Elements no click should ever land on / that don't render visible
// content of their own — instrumenting them would either be inert (head,
// meta, link, title never appear in the rendered box model) or actively
// wrong (script/style content isn't "an element you clicked", and adding
// a data-mm attribute to a <script> tag doesn't do anything harmful, it's
// just noise that will never be hit by the click handler in W6.4).
const HTML_SKIP_TAGS = new Set(["html", "head", "script", "style", "meta", "link", "title"]);

function hasDataMm(attrs) {
  return (attrs || []).some((a) => a.name === "data-mm");
}

function mmValue(filePath, startLine, startCol, endLine, endCol) {
  return `${filePath}:${startLine}:${startCol}:${endLine}:${endCol}`;
}

/**
 * Instruments an HTML document with data-mm attributes.
 *
 * Walks parse5's parsed tree (not the raw text) so HTML5's own error-
 * recovery/implied-tag rules are handled correctly for free — hand-rolled
 * string splicing on HTML specifically would have to reimplement a chunk
 * of the HTML5 parsing algorithm to get void elements, implied </li>,
 * unquoted attributes, etc. right. parse5 essentially never throws (the
 * HTML5 spec defines error recovery for almost any byte sequence), but
 * this still guards the call in case of a genuinely non-string input or
 * an internal parse5 error — same "never break the preview" contract
 * instrumentJsx's real try/catch enforces for real syntax errors.
 *
 * @param {string} code
 * @param {string} filePath
 * @returns {{code: string, instrumented: boolean, note: string|null}}
 */
export function instrumentHtml(code, filePath) {
  let document;
  try {
    document = parse5.parse(code, { sourceCodeLocationInfo: true });
  } catch {
    return { code, instrumented: false, note: "preview inspector unavailable for this file" };
  }

  let changed = false;

  function walk(node) {
    if (node.tagName && node.sourceCodeLocation && !HTML_SKIP_TAGS.has(node.tagName)) {
      if (!hasDataMm(node.attrs)) {
        const loc = node.sourceCodeLocation;
        // parse5's startCol/endCol are 1-based; the data-mm contract is
        // 0-based columns (see this module's header) — the -1 below is
        // the only unit conversion this function needs to do.
        node.attrs.push({
          name: "data-mm",
          value: mmValue(filePath, loc.startLine, loc.startCol - 1, loc.endLine, loc.endCol - 1),
        });
        changed = true;
      }
    }
    for (const child of node.childNodes || []) walk(child);
  }
  walk(document);

  if (!changed) return { code, instrumented: false, note: null };
  return { code: parse5.serialize(document), instrumented: true, note: null };
}

// Generic, dependency-free AST walker — deliberately NOT @babel/traverse
// (an extra dependency this module doesn't otherwise need). Visits every
// node reachable through any own enumerable property, which is enough to
// reach JSXElements nested anywhere — inside a .map() callback, inside a
// ternary, inside a template-literal expression — without knowing every
// specific Babel node shape ahead of time. Metadata-only keys are skipped
// both because they're not source nodes and, for `loc`/`extra`, because
// walking into them is pure wasted work on every node in the tree.
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

function isHostElement(name) {
  // Only a plain lowercase JSXIdentifier is a host/intrinsic element in
  // JSX's own convention (the thing that becomes a DOM tag). A capitalized
  // JSXIdentifier (<Button>), a JSXMemberExpression (<Foo.Bar>), or a
  // JSXNamespacedName (<svg:use>) are all component/reference tags with
  // no DOM element of their own to attach an attribute to — v1 gives
  // these nothing, per this module's header; W7.1 is where "select the
  // usage site" for a component click gets solved instead.
  return name.type === "JSXIdentifier" && /^[a-z]/.test(name.name);
}

/**
 * Instruments a JSX/TSX source file with data-mm attributes.
 *
 * @param {string} code
 * @param {string} filePath
 * @returns {Promise<{code: string, instrumented: boolean, note: string|null}>}
 */
export async function instrumentJsx(code, filePath) {
  const parser = await import("@babel/parser");

  let ast;
  try {
    // Both plugins are always on together (matches the build plan's own
    // spec) rather than switched on the .jsx/.tsx extension — a stray
    // `as` cast or `<T>` type param in a .jsx file some projects still
    // ship is far cheaper to tolerate than to special-case here, and
    // enabling the typescript plugin on plain JS/JSX is a no-op when
    // there's no TS syntax to find.
    ast = parser.parse(code, { sourceType: "module", plugins: ["jsx", "typescript"] });
  } catch {
    return { code, instrumented: false, note: "preview inspector unavailable for this file" };
  }

  // Collect every injection point first (as {offset, text}) instead of
  // splicing as we go — walkAst doesn't guarantee traversal order lines
  // up with source order once nested callbacks are involved, and even if
  // it did, splicing earlier in the string would invalidate every offset
  // already computed from the ORIGINAL ast for elements later in the
  // file. Sorting once, descending, and applying back-to-front (below)
  // sidesteps both problems: every splice happens at an offset that is
  // still valid because nothing after it in the string has moved yet.
  const injections = [];
  walkAst(ast, (node) => {
    // JSXFragment (<>...</>) has no tag name at all to attach an
    // attribute to -- walkAst still reaches every JSXElement nested
    // inside it via the normal recursive walk, so fragments need no
    // special-casing here beyond simply not matching this check.
    if (node.type !== "JSXElement") return;
    const name = node.openingElement.name;
    if (!isHostElement(name)) return;
    if (node.openingElement.attributes.some((a) => a.type === "JSXAttribute" && a.name?.name === "data-mm")) return;

    injections.push({
      offset: name.end, // right after the tag name, before any attributes/`>`/`/>` -- see the module header for why here specifically
      text: ` data-mm=${JSON.stringify(mmValue(filePath, node.loc.start.line, node.loc.start.column, node.loc.end.line, node.loc.end.column))}`,
    });
  });

  if (!injections.length) return { code, instrumented: false, note: null };

  injections.sort((a, b) => b.offset - a.offset);
  let out = code;
  for (const { offset, text } of injections) {
    out = out.slice(0, offset) + text + out.slice(offset);
  }
  return { code: out, instrumented: true, note: null };
}

const JSX_EXTENSION_RE = /\.[jt]sx?$/i;
const HTML_EXTENSION_RE = /\.html?$/i;

/**
 * Dispatches to instrumentHtml or instrumentJsx by `filePath`'s
 * extension. Any other extension is not a failure — there's simply
 * nothing this module knows how to instrument for it (a .css or .json
 * file, say) — so it comes back unchanged with `instrumented: false` and
 * no note; `note` is reserved for "this looked instrumentable and
 * parsing it failed", not "this was never in scope".
 *
 * @param {string} code
 * @param {string} filePath
 * @returns {Promise<{code: string, instrumented: boolean, note: string|null}>}
 */
export async function instrumentSource(code, filePath) {
  if (HTML_EXTENSION_RE.test(filePath)) return instrumentHtml(code, filePath);
  if (JSX_EXTENSION_RE.test(filePath)) return instrumentJsx(code, filePath);
  return { code, instrumented: false, note: null };
}
