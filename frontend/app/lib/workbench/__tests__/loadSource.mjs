// W2.3a (Build Workbench plan) — test helper: load a workbench source
// file's REAL text and run it under plain `node`.
//
// Why this exists: the repo has no JS test runner and no bundler in the
// test path (see editorStore.test.mjs's own history), so the source files
// here are `.js` files using ES `import`/`export` with no `"type":
// "module"` in package.json — plain `node` can't import them directly.
// Until W2.2 the answer was to paste a byte-for-byte copy of the code
// under test into each test file, which is exactly how a test ends up
// passing against code that no longer exists. This instead reads the
// actual file, rewrites its `import`/`export` lines into plain
// JavaScript, and evaluates the result.
//
// What it supports (deliberately small — it has to be obviously
// correct):
//   - `export function|async function|const|let|class name` — the names
//     come back on the returned object;
//   - `import {a, b as c} from "spec"`, `import X from "spec"` and
//     `import * as X from "spec"` — satisfied ONLY from the `imports`
//     map you pass in ({ "spec": moduleObject }); any other import throws.
//     That is also how the "no imports" rule for the pure modules
//     (fileTree.js, tabUtils.js) is enforced: load them with no `imports`
//     and an added `import` line makes their test fail loudly.
//   - a leading "use client" directive is dropped.
// Not supported (throws): `export default`, `export { … }` lists,
// side-effect-only imports, dynamic `import()`.
//
// Usage:
//   const tabUtils = loadSource("../tabUtils.js");
//   const store = loadSource("../editorStore.js", {
//     imports: { react: reactStub, "./tabUtils": tabUtils },
//   });
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

const IMPORT_RE = /^import\s+([\s\S]*?)\s+from\s+["']([^"']+)["'];?[ \t]*$/gm;

function bindingsFor(clause, spec) {
  const ref = `__imports[${JSON.stringify(spec)}]`;
  const parts = [];
  const named = clause.match(/\{([\s\S]*?)\}/);
  let rest = clause.replace(/\{[\s\S]*?\}/, "").trim().replace(/,\s*$/, "").trim();
  if (rest.startsWith("* as ")) {
    parts.push(`const ${rest.slice(5).trim()} = ${ref};`);
    rest = "";
  }
  if (rest) parts.push(`const ${rest} = ${ref}.default;`);
  if (named) {
    const names = named[1]
      .split(",")
      .map((n) => n.trim())
      .filter(Boolean)
      .map((n) => n.replace(/\s+as\s+/, ": "));
    if (names.length) parts.push(`const { ${names.join(", ")} } = ${ref};`);
  }
  return parts.join(" ");
}

/**
 * @param {string} relPath - path to the source file, relative to THIS file
 * @param {{imports?: Record<string, object>}} [opts]
 * @returns {Record<string, any>} the file's named exports
 */
export function loadSource(relPath, { imports = {} } = {}) {
  const abs = resolve(here, relPath);
  let src = readFileSync(abs, "utf8");

  src = src.replace(/^\s*["']use client["'];?[ \t]*$/m, "");

  src = src.replace(IMPORT_RE, (_all, clause, spec) => {
    if (!(spec in imports)) {
      throw new Error(
        `${relPath} imports "${spec}", which this test didn't provide. ` +
          `If the file is one of the dependency-free modules (fileTree.js, tabUtils.js) ` +
          `that is the point: they must stay import-free.`
      );
    }
    return bindingsFor(clause, spec);
  });

  if (/^\s*import[\s{*"']/m.test(src.replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, ""))) {
    throw new Error(`${relPath}: unsupported import form (only "import … from '…'" is handled)`);
  }
  if (/^\s*export\s+default\b/m.test(src) || /^\s*export\s*\{/m.test(src)) {
    throw new Error(`${relPath}: loadSource() only supports named declaration exports`);
  }

  const names = [];
  src = src.replace(/^(\s*)export\s+((?:async\s+)?function\s*\*?|const|let|class)\s+([A-Za-z_$][\w$]*)/gm, (_m, indent, kind, name) => {
    names.push(name);
    return `${indent}${kind} ${name}`;
  });

  const body = `"use strict";\n${src}\nreturn { ${names.join(", ")} };`;
  // eslint-disable-next-line no-new-func
  return new Function("__imports", body)(imports);
}
