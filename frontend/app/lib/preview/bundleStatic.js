// frontend/app/lib/preview/bundleStatic.js — W6.1 (Build Workbench plan).
//
// Turns an entry HTML file plus "however you get me the content of a
// path" into one self-contained HTML string an iframe's `srcDoc` can
// render directly — no bundler, no module resolution, just inlining
// exactly what the plan scopes: `<link href>` (stylesheets) and
// `<script src>` (scripts) that point at files IN THIS PROJECT.
//
// Deliberately NOT recursive/transitive: a `<link>`/`<script>` found
// INSIDE an inlined file (there usually isn't one — CSS doesn't have
// script tags, and a plain <script> file wouldn't either) is not
// walked into, and neither is a CSS `@import`. This is "index.html +
// style.css + app.js", not a bundler — see detectKind.js's header for
// why a real bundled project (Vite/CRA's module-graph HTML) is routed
// to the react provider (W6.6) instead of here in the first place.
//
// Same parse5 tree-mutate-then-serialize approach W6.3's
// instrumentHtml() already uses, for the same reason: parse5's real
// HTML5 parsing gets void elements/implied tags/quoting right for
// free, where hand-splicing the raw text would have to reimplement
// chunks of the HTML5 spec to do the same. See that file for the
// caveat that comes with it (not a byte-for-byte round-trip of the
// original source, which is fine here — an iframe about to render the
// output doesn't care about the developer's original formatting).
//
// No React/provider/fetch imports here on purpose: `resolveFile` is
// the one seam to the outside world, passed in by the caller
// (PreviewPane.jsx, which knows how to pull live-buffer vs.
// provider.read() content) — this file stays plain async functions
// operating on strings, testable with plain node exactly like
// instrument.test.mjs tests instrument.js, using a stub resolveFile
// instead of a real FileProvider.

import * as parse5 from "parse5";

const EXTERNAL_RE = /^([a-z]+:)?\/\//i; // http:, https:, //cdn... (protocol-relative), or any other-scheme URL
const DATA_URI_RE = /^data:/i;

function isExternal(url) {
  return EXTERNAL_RE.test(url) || DATA_URI_RE.test(url) || url.startsWith("#");
}

// Resolves an href/src found in `entryPath` against the project's own
// path space (NOT a real URL/filesystem resolver -- workspace paths are
// plain "a/b/c" strings with "/" separators, so this only needs to
// handle "./", "../", a bare relative segment, and a root-absolute "/"
// treated as relative to entryPath's own directory, which is what a
// simple static file server would do when index.html itself IS the
// site root).
function resolveRelative(entryPath, url) {
  const clean = url.split("#")[0].split("?")[0]; // strip a fragment/query before resolving
  if (!clean) return null;

  const entryDir = entryPath.includes("/") ? entryPath.slice(0, entryPath.lastIndexOf("/")) : "";
  const base = clean.startsWith("/") ? clean.slice(1) : entryDir ? `${entryDir}/${clean}` : clean;

  const parts = [];
  for (const seg of base.split("/")) {
    if (seg === "." || seg === "") continue;
    if (seg === "..") parts.pop();
    else parts.push(seg);
  }
  return parts.join("/");
}

/**
 * @param {object} args
 * @param {string} args.entryPath - the workspace path of the HTML file to bundle (detectKind's `entryPath`)
 * @param {string} args.entryContent - that file's current content (buffer-preferred, unsaved edits included — the caller's job, not this function's)
 * @param {(resolvedPath: string) => Promise<string|null|undefined>} args.resolveFile
 *   called once per local (non-external) href/src this function finds,
 *   with the path already resolved relative to entryPath. Return the
 *   file's content, or null/undefined if there's nothing at that path —
 *   this function never throws on a missing file, it just leaves that
 *   one tag un-inlined and adds a warning.
 * @returns {Promise<{html: string, warnings: string[]}>}
 */
export async function bundleStatic({ entryPath, entryContent, resolveFile }) {
  const warnings = [];
  let document;
  try {
    document = parse5.parse(entryContent, { sourceCodeLocationInfo: false });
  } catch (err) {
    // parse5 essentially never throws (see instrument.js's own note on
    // this) but the contract here is the same "never break the
    // preview" one instrument.js follows: fall back to the untouched
    // source rather than propagate.
    return { html: entryContent, warnings: [`Couldn't parse ${entryPath}: ${err.message}`] };
  }

  // Collected as {node, kind} pairs during the walk, then resolved and
  // applied afterward -- keeps the tree-walk itself synchronous and
  // simple, with all the `await resolveFile(...)` calls (which may
  // genuinely hit the network, via provider.read()) grouped together
  // and run concurrently rather than serialized one-at-a-time down the
  // tree.
  const candidates = [];
  function walk(node) {
    if (node.tagName === "link") {
      const rel = (node.attrs || []).find((a) => a.name === "rel")?.value;
      const href = (node.attrs || []).find((a) => a.name === "href")?.value;
      if (rel === "stylesheet" && href && !isExternal(href)) candidates.push({ node, kind: "style", url: href });
    } else if (node.tagName === "script") {
      const src = (node.attrs || []).find((a) => a.name === "src")?.value;
      if (src && !isExternal(src)) candidates.push({ node, kind: "script", url: src });
    }
    for (const child of node.childNodes || []) walk(child);
  }
  walk(document);

  await Promise.all(
    candidates.map(async (c) => {
      const resolvedPath = resolveRelative(entryPath, c.url);
      if (!resolvedPath) return;
      let content;
      try {
        content = await resolveFile(resolvedPath);
      } catch (err) {
        warnings.push(`Couldn't load ${resolvedPath}: ${err.message}`);
        return;
      }
      if (content == null) {
        warnings.push(`${resolvedPath} (linked from ${c.url}) wasn't found in this project`);
        return;
      }

      if (c.kind === "style") {
        // Swap the <link> node itself for a <style> node in its exact
        // place in the parent's children — a straight attrs mutation
        // won't do here, "stylesheet link" and "style block" are
        // different tag names.
        const styleNode = {
          nodeName: "style",
          tagName: "style",
          attrs: [],
          namespaceURI: c.node.namespaceURI,
          childNodes: [{ nodeName: "#text", value: content, parentNode: null }],
          parentNode: c.node.parentNode,
        };
        styleNode.childNodes[0].parentNode = styleNode;
        const siblings = c.node.parentNode.childNodes;
        siblings[siblings.indexOf(c.node)] = styleNode;
      } else {
        // A script element just loses its `src` and gains the file's
        // text as a child text node -- still a <script>, so no swap
        // needed the way the stylesheet case does.
        c.node.attrs = c.node.attrs.filter((a) => a.name !== "src");
        c.node.childNodes = [{ nodeName: "#text", value: content, parentNode: c.node }];
      }
    })
  );

  return { html: parse5.serialize(document), warnings };
}
