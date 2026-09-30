// frontend/app/lib/preview/reactBundle.js — W6.6 (Build Workbench plan).
// The "react" provider's own assembly step, parallel to what
// bundleStatic.js does for "static": turn a project's real files into
// the shape Sandpack's `files`/`customSetup` props want. Pure functions
// only (no React, no SandpackProvider import) — the actual component
// (PreviewPane.jsx's ReactPreview) calls these and does nothing else
// with the data before handing it to Sandpack, so this stays testable
// with plain `node` the same way bundleStatic.js's own helpers are.
//
// detectKind.js's own header already scoped this out on purpose
// ("W6.6... is the piece that needs package.json's real content") —
// this module is that piece.

// Same "just what this codebase's own file types need" scope
// eo/code_proposals.py's _BINARY_EXTENSIONS keeps for itself (W5.5) —
// not a general binary-sniffing library, just enough to keep an image
// or a lockfile out of a payload handed to Sandpack, which can only
// usefully bundle text source anyway.
const SKIP_EXTENSIONS = new Set([
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
  ".woff", ".woff2", ".ttf", ".otf", ".eot",
  ".zip", ".tar", ".gz", ".map", ".lock",
]);
// Lockfiles that don't carry a ".lock" extension of their own (a JSON
// or YAML file by name) — same exact-basename approach
// eo/code_proposals.py's own _LOCKFILE_BASENAMES (W5.5) takes, for the
// same reason: "ends in .json" isn't a signal, the specific FILENAME is.
const SKIP_BASENAMES = new Set(["package-lock.json", "pnpm-lock.yaml", "npm-shrinkwrap.json"]);
const SKIP_DIR_SEGMENTS = new Set(["node_modules", ".git", "dist", "build", ".next"]);

/**
 * @param {string} path
 * @returns {boolean}
 */
export function shouldBundleFile(path) {
  if (typeof path !== "string" || !path) return false;
  const segments = path.split("/");
  if (segments.slice(0, -1).some((seg) => SKIP_DIR_SEGMENTS.has(seg))) return false;
  const name = segments[segments.length - 1];
  if (SKIP_BASENAMES.has(name)) return false;
  const dot = name.lastIndexOf(".");
  const ext = dot === -1 ? "" : name.slice(dot).toLowerCase();
  return !SKIP_EXTENSIONS.has(ext);
}

// Checked in this order against the actual file set — the first path
// PRESENT wins. Vite's own scaffolding (`npm create vite -- --template
// react`) produces `src/main.jsx`, which is why it's first: the plan's
// own Done-when is specifically "a Vite-style React project", and a
// plain package.json + JSX-file heuristic (detectKind.js's own
// structural check) says nothing about which convention a given
// project follows.
const ENTRY_CANDIDATES = [
  "src/main.jsx", "src/main.tsx", "src/main.js", "src/main.ts",
  "src/index.jsx", "src/index.tsx", "src/index.js", "src/index.ts",
  "index.jsx", "index.tsx", "index.js", "index.ts",
];

/**
 * @param {string[]} paths - every path in the project (filtering for
 *   bundle-worthiness is the CALLER's job — an entry candidate is still
 *   a candidate even if it happened to be in SKIP_DIR_SEGMENTS, though
 *   that combination shouldn't occur for a real project)
 * @returns {string|null}
 */
export function pickReactEntry(paths) {
  const present = new Set(paths || []);
  for (const candidate of ENTRY_CANDIDATES) {
    if (present.has(candidate)) return candidate;
  }
  return null;
}

/**
 * `package.json`'s `dependencies`/`devDependencies`, tolerant of
 * anything short of a well-formed object — a project's package.json is
 * exactly the kind of file that's often mid-edit (a trailing comma
 * while typing a new entry), and Sandpack getting NO dependencies is a
 * far better failure mode than this function throwing and taking the
 * whole preview down with it.
 *
 * @param {string|null|undefined} packageJsonContent
 * @returns {{dependencies: Record<string,string>, devDependencies: Record<string,string>}}
 */
export function parseDependencies(packageJsonContent) {
  const empty = { dependencies: {}, devDependencies: {} };
  if (typeof packageJsonContent !== "string" || !packageJsonContent.trim()) return empty;
  let parsed;
  try {
    parsed = JSON.parse(packageJsonContent);
  } catch {
    return empty;
  }
  const pick = (value) =>
    value && typeof value === "object" && !Array.isArray(value)
      ? Object.fromEntries(Object.entries(value).filter(([k, v]) => typeof k === "string" && typeof v === "string"))
      : {};
  return { dependencies: pick(parsed?.dependencies), devDependencies: pick(parsed?.devDependencies) };
}

/**
 * Where `mm-inspector.js` (mmInspectorFile.js) should live: right next
 * to the entry file, so the entry can always reach it with a plain
 * relative `"./mm-inspector.js"` import — deliberately NOT a project-
 * root-absolute `/mm-inspector.js` path (the plan's own shorthand for
 * "the inspector file", not a literal resolution requirement): whether
 * a bare absolute specifier like that resolves to the project root
 * under Sandpack's own bundler is exactly the kind of thing this step's
 * own "prototype first" note flags as unverified, and getting it wrong
 * would silently break the ENTIRE preview (a failed import fails the
 * whole bundle). A same-directory relative import has no such
 * ambiguity in any bundler.
 *
 * @param {string} entryPath
 * @returns {string}
 */
export function inspectorPathFor(entryPath) {
  const slash = entryPath.lastIndexOf("/");
  return slash === -1 ? "mm-inspector.js" : `${entryPath.slice(0, slash)}/mm-inspector.js`;
}

/**
 * Prepends `import "<importSpecifier>";` to the entry file's own code,
 * in a COPY of `files` — the plan's own "an `import` line in the entry
 * *of the copy*" line: the person's real buffer is never touched, only
 * what gets handed to Sandpack this render. A missing/absent entry
 * (pickReactEntry() found nothing) leaves `files` completely unchanged
 * — better to preview without click-to-code than to silently invent an
 * entry point Sandpack didn't ask for.
 *
 * @param {Record<string,string>} files - path -> source, NOT mutated
 * @param {string|null} entryPath
 * @param {string} importSpecifier
 * @returns {Record<string,string>}
 */
export function withInjectedImport(files, entryPath, importSpecifier) {
  if (!entryPath || !(entryPath in files)) return files;
  return { ...files, [entryPath]: `import ${JSON.stringify(importSpecifier)};\n${files[entryPath]}` };
}
