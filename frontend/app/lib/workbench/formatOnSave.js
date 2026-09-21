// frontend/app/lib/workbench/formatOnSave.js — W2.5 (Build Workbench
// plan). Format-on-save: runs a buffer's content through
// `prettier/standalone` right before it's sent to the server, gated
// behind the "Format on save" toggle in savePrefs.js.
//
// Everything prettier-related is dynamically imported — the same
// "bundle size: lazy-import per-language chunks" rule this repo's own
// checklist already applies to CM6's language-data (CodeEditor.jsx's
// loadLanguageExtension) and W6.3's @babel/parser. A person who never
// turns this toggle on never pays for prettier's plugin bundles, and a
// person who does only pays for the ONE parser's plugins the active
// file actually needs, not all of them.
//
// Parser selection is by FILE EXTENSION, matching every other
// filename-driven choice in this module family (CodeEditor.jsx's
// LanguageDescription.matchFilename, fileIcons.js) rather than the
// server-reported `language` string — that's a display label, not a
// prettier parser id. workspace_code_files.py's own
// _EXTENSION_LANGUAGE_MAP maps BOTH .js and .jsx to "javascript", but
// prettier's babel parser handles JSX directly, so one parser id
// covers both here too; TypeScript's .ts/.tsx get their own parser
// because prettier's babel parser doesn't understand TS type syntax.
//
// Verified against prettier@3.9.8's real package.json `exports` map
// and a live `prettier.format()` call per parser (js/ts/css/html/md/
// json) — the plugin lists below aren't guessed.

const PARSER_BY_EXTENSION = {
  js: "babel",
  jsx: "babel",
  mjs: "babel",
  cjs: "babel",
  ts: "typescript",
  tsx: "typescript",
  json: "json",
  jsonc: "json",
  css: "css",
  scss: "scss",
  less: "less",
  html: "html",
  htm: "html",
  md: "markdown",
  markdown: "markdown",
};

// Which plugin modules (under prettier/plugins/*) a parser needs.
// babel/typescript both produce an ESTree-shaped AST that the
// `estree` plugin is what actually PRINTS — prettier.format() throws
// "Couldn't find plugin for the printer" without it, even though
// nothing above ever names "estree" as a parser in its own right.
const PLUGINS_FOR_PARSER = {
  babel: ["babel", "estree"],
  typescript: ["typescript", "estree"],
  json: ["babel", "estree"],
  css: ["postcss"],
  scss: ["postcss"],
  less: ["postcss"],
  html: ["html"],
  markdown: ["markdown"],
};

function extensionOf(path) {
  const name = (path || "").split(/[\\/]/).pop() || "";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/**
 * The prettier parser id for `path`, or null if format-on-save has
 * nothing to do with this file. Exported (rather than folded into
 * formatContent()) so a caller can decide whether to even OFFER
 * formatting — grey out a menu item, skip attempting it — without
 * pulling in prettier at all.
 */
export function parserFor(path) {
  return PARSER_BY_EXTENSION[extensionOf(path)] || null;
}

export function isFormattable(path) {
  return parserFor(path) != null;
}

// One dynamic import() per plugin module / per prettier itself, cached
// across calls for the lifetime of the page: every save while the
// toggle is on needs prettier, so re-fetching an already-loaded chunk
// each time would be wasteful, but a plugin this session never
// actually used (nobody opened a CSS file) is still never imported.
//
// The specifiers are spelled out literally, NOT built as
// import(`prettier/plugins/${name}`). A template import of a bare
// specifier makes webpack (next build) create a "context module" over
// the WHOLE prettier/plugins directory — every file in it, .d.ts
// declarations included — and the build fails parsing them. Literal
// specifiers are also what give each plugin its own lazy chunk, which is
// the entire point of this file.
const PLUGIN_LOADERS = {
  babel: () => import("prettier/plugins/babel"),
  estree: () => import("prettier/plugins/estree"),
  typescript: () => import("prettier/plugins/typescript"),
  postcss: () => import("prettier/plugins/postcss"),
  html: () => import("prettier/plugins/html"),
  markdown: () => import("prettier/plugins/markdown"),
};

// A failed load (a network hiccup on a lazy chunk) is dropped from the
// cache instead of remembered: otherwise one blip would leave
// format-on-save silently dead until the page is reloaded, with every
// later save quietly falling back to unformatted text.
const pluginCache = new Map();
function loadPlugin(name) {
  if (!pluginCache.has(name)) {
    pluginCache.set(
      name,
      PLUGIN_LOADERS[name]()
        .then((mod) => mod.default)
        .catch((err) => {
          pluginCache.delete(name);
          throw err;
        })
    );
  }
  return pluginCache.get(name);
}

let prettierPromise = null;
function loadPrettier() {
  if (!prettierPromise) {
    prettierPromise = import("prettier/standalone")
      .then((mod) => mod.default)
      .catch((err) => {
        prettierPromise = null;
        throw err;
      });
  }
  return prettierPromise;
}

/**
 * Formats `content` for `path` and returns the result, or returns
 * `content` UNCHANGED if the extension has no parser mapped above.
 * Throws if prettier fails to load or the content doesn't parse (a
 * syntax error mid-edit is normal) — EditorWorkbench.jsx's saveFile()
 * catches that and falls back to saving what's actually in the buffer
 * rather than blocking the save on a formatter error; this function
 * itself stays a plain "format or throw" so it's just as usable from
 * a future caller that DOES want to surface the failure (a "Format
 * document" command, say) without EditorWorkbench's own fallback
 * policy baked in here.
 *
 * @param {string} path
 * @param {string} content
 * @returns {Promise<string>}
 */
export async function formatContent(path, content) {
  const parser = parserFor(path);
  if (!parser) return content;
  const pluginNames = PLUGINS_FOR_PARSER[parser];
  const [prettier, plugins] = await Promise.all([
    loadPrettier(),
    Promise.all(pluginNames.map(loadPlugin)),
  ]);
  return prettier.format(content, { parser, plugins });
}
