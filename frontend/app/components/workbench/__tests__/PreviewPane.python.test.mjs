// Patch B03 (M2C build plan, decision O6) — tests for removing the Python
// Run from the preview panel (components/workbench/PreviewPane.jsx).
//
// WHAT KIND OF TEST THIS IS — read this before trusting it. PreviewPane.jsx
// cannot be loaded under plain `node`: it is JSX (no transform in the test
// path) with `export default`, and it imports React, lucide-react and
// Sandpack, none of which loadSource.mjs supports. So this is a SOURCE-LEVEL
// test: it reads the REAL PreviewPane.jsx text, parses it with
// @babel/parser (already a dependency of this package; no new one is added)
// and asserts on the syntax tree. It does NOT render the component. The one
// piece that IS run for real is detectKind(), loaded through loadSource.mjs,
// because it decides which branch of PreviewPane a project lands in. The
// manual check steps for the rendered behaviour are in the patch
// description.
//
// What it pins down:
//   1. PreviewPane.jsx no longer imports the Pyodide worker hook, and the
//      names PythonPreview / usePyodideWorker / Play (the Run button's
//      icon) are gone from it. Walking the tree rather than grepping the
//      text means a comment that still mentions the old design cannot
//      cause a false failure, and an import cannot hide in one.
//   2. The `detected.kind === "python"` branch renders <EmptyState> with
//      the neutral title "Python files aren't run in the browser".
//   3. The static, react and no-match branches are still there, still
//      render StaticPreview / ReactPreview / EmptyState, and the "No
//      preview available" empty state is unchanged. (HTML and React
//      previews are "unchanged" in the sense of this file's routing; their
//      components are not edited by this patch.)
//   4. Pyodide stays for chat artifacts: ArtifactRenderer.jsx still imports
//      the hook.
//   5. detectKind() still routes a .py-only project to "python" (so the
//      empty state is what a .py project shows) and a package.json + .jsx
//      project to "react".
//
// Run: node frontend/app/components/workbench/__tests__/PreviewPane.python.test.mjs
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "@babel/parser";
import { loadSource } from "../../../lib/workbench/__tests__/loadSource.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel) => readFileSync(resolve(here, rel), "utf8");

let failures = 0;
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

function parseJsx(src) {
  return parse(src, { sourceType: "module", plugins: ["jsx"] });
}

// Depth-first walk over every node of a Babel AST.
function walk(node, visit) {
  if (!node || typeof node.type !== "string") return;
  visit(node);
  for (const key of Object.keys(node)) {
    if (key === "loc" || key === "leadingComments" || key === "trailingComments" || key === "innerComments") continue;
    const child = node[key];
    if (Array.isArray(child)) child.forEach((c) => walk(c, visit));
    else if (child && typeof child.type === "string") walk(child, visit);
  }
}

// `root` is either a parsed file (has .program) or any single AST node.
function collect(root, predicate) {
  const out = [];
  walk(root.program ?? root, (n) => {
    if (predicate(n)) out.push(n);
  });
  return out;
}

const previewSrc = read("../PreviewPane.jsx");
const ast = parseJsx(previewSrc);

// ---- 1. the worker hook and the Run UI are gone ------------------------
const importSources = collect(ast, (n) => n.type === "ImportDeclaration").map((n) => n.source.value);
assert(
  !importSources.some((s) => /usePyodideWorker|pyodide/i.test(s)),
  "PreviewPane.jsx has no import of the Pyodide worker hook"
);

const identifierNames = new Set(collect(ast, (n) => n.type === "Identifier" || n.type === "JSXIdentifier").map((n) => n.name));
assert(!identifierNames.has("usePyodideWorker"), "no identifier usePyodideWorker remains in PreviewPane.jsx");
assert(!identifierNames.has("PythonPreview"), "no identifier PythonPreview remains in PreviewPane.jsx");

const lucideImport = collect(ast, (n) => n.type === "ImportDeclaration" && n.source.value === "lucide-react")[0];
const lucideNames = (lucideImport?.specifiers || []).map((s) => s.local.name);
assert(lucideNames.includes("Loader2"), "Loader2 is still imported (the 'Loading…' state and other previews use it)");
assert(!lucideNames.includes("Play"), "Play (only the removed Run button used it) is no longer imported");

// ---- 2/3. the branch that picks the preview ----------------------------
// Finds `detected.kind === "<kind>"` tests and returns the JSX element name
// that the matching branch assigns to `body`.
function isKindTest(test, kind) {
  return (
    test &&
    test.type === "BinaryExpression" &&
    test.operator === "===" &&
    test.left.type === "MemberExpression" &&
    test.left.object.name === "detected" &&
    test.left.property.name === "kind" &&
    test.right.type === "StringLiteral" &&
    test.right.value === kind
  );
}

function branchJsx(kind) {
  const ifs = collect(ast, (n) => n.type === "IfStatement" && isKindTest(n.test, kind));
  if (ifs.length !== 1) return { count: ifs.length, element: null };
  const jsx = collect(ifs[0].consequent, (n) => n.type === "JSXElement")[0] || null;
  return { count: 1, element: jsx };
}

const elementName = (el) => el?.openingElement?.name?.name ?? null;
const attrString = (el, name) => {
  const attr = el?.openingElement?.attributes?.find((a) => a.type === "JSXAttribute" && a.name.name === name);
  return attr && attr.value && attr.value.type === "StringLiteral" ? attr.value.value : null;
};

const py = branchJsx("python");
assert(py.count === 1, 'exactly one `detected.kind === "python"` branch is left');
assert(elementName(py.element) === "EmptyState", "the python branch renders <EmptyState>");
assert(
  attrString(py.element, "title") === "Python files aren't run in the browser",
  'the python empty state says "Python files aren\'t run in the browser"'
);

const st = branchJsx("static");
assert(st.count === 1 && elementName(st.element) === "StaticPreview", "the static branch still renders <StaticPreview>");
const rc = branchJsx("react");
assert(rc.count === 1 && elementName(rc.element) === "ReactPreview", "the react branch still renders <ReactPreview>");

const emptyStates = collect(ast, (n) => n.type === "JSXElement" && elementName(n) === "EmptyState");
assert(
  emptyStates.some((el) => attrString(el, "title") === "No preview available for this project type"),
  'the "No preview available for this project type" empty state is unchanged'
);
const emptyStateDefs = collect(ast, (n) => n.type === "FunctionDeclaration" && n.id?.name === "EmptyState");
assert(emptyStateDefs.length === 1, "EmptyState is still defined once and shared by every empty state");

// ---- 4. Pyodide stays for chat artifacts --------------------------------
const artifactAst = parseJsx(read("../../ArtifactRenderer.jsx"));
const artifactImports = collect(artifactAst, (n) => n.type === "ImportDeclaration").map((n) => n.source.value);
assert(
  artifactImports.some((s) => /usePyodideWorker/.test(s)),
  "ArtifactRenderer.jsx still imports usePyodideWorker (Pyodide stays for chat artifacts)"
);

// ---- 5. routing, run for real -------------------------------------------
const { detectKind } = loadSource("../../../lib/preview/detectKind.js", {});
const meta = (paths) => Object.fromEntries(paths.map((p) => [p, {}]));
assert(detectKind(meta(["main.py", "notes.txt"])).kind === "python", "a project with a .py file is still classified 'python' (-> the empty state)");
assert(detectKind(meta(["package.json", "src/App.jsx"])).kind === "react", "package.json + .jsx is still classified 'react'");
assert(detectKind(meta(["index.html", "style.css"])).kind === "static", "an index.html project is still classified 'static'");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
