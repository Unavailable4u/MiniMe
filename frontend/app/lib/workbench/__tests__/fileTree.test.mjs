// W2.3a (Build Workbench plan) — tests for fileTree.js.
//
// Loads the REAL lib/workbench/fileTree.js through loadSource.mjs. No
// `imports` are passed on purpose: fileTree.js must stay dependency-free,
// and this load fails if someone adds an `import` to it.
//
// Run: node frontend/app/lib/workbench/__tests__/fileTree.test.mjs
import { loadSource } from "./loadSource.mjs";

const { buildFileTree, sortedChildren, basename, dirname, ancestorDirs, topLevelDirs } = loadSource("../fileTree.js");

let failures = 0;
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

// Compact view of a tree: "dir/" and "file" names, recursively, in display order.
function shape(node) {
  return sortedChildren(node).map((c) => (c.type === "dir" ? { [c.name + "/"]: shape(c) } : c.name));
}

// --- buildFileTree / sortedChildren ---------------------------------------

const meta = (...paths) => Object.fromEntries(paths.map((p) => [p, { size: 1 }]));

assertEqual(shape(buildFileTree({})), [], "an empty file list is an empty tree");
assertEqual(shape(buildFileTree(null)), [], "a null file list (first load) doesn't throw");

assertEqual(
  shape(buildFileTree(meta("README.md", "src/App.jsx", "src/lib/util.js", "package.json", "tests/a.test.js"))),
  [{ "src/": [{ "lib/": ["util.js"] }, "App.jsx"] }, { "tests/": ["a.test.js"] }, "package.json", "README.md"],
  "directories first, then files, each alphabetical (case-insensitive: package.json before README.md), nested folders from path prefixes"
);

// The crash the old tree had: a file `a` and a folder `a/`.
let clash;
let threw = false;
try {
  clash = buildFileTree(meta("a", "a/b.js"));
} catch {
  threw = true;
}
assertEqual(threw, false, "a file and a folder with the same name don't throw (the old white-screen bug)");
assertEqual(shape(clash), [{ "a/": ["b.js"] }, "a"], "...and both are kept: the folder a/ and the file a");

const tree = buildFileTree(meta("src/App.jsx"));
const src = sortedChildren(tree)[0];
assertEqual(
  [src.type, src.name, src.path, sortedChildren(src)[0].path, sortedChildren(src)[0].type],
  ["dir", "src", "src", "src/App.jsx", "file"],
  "nodes carry full paths, so a click can open sortedChildren()'s file by path"
);
assertEqual(sortedChildren(src)[0].meta, { size: 1 }, "file nodes carry their meta entry through");

assertEqual(
  shape(buildFileTree(meta("app/[id]/page.js", "app/(auth)/login.tsx", "docs/my notes.md"))),
  [{ "app/": [{ "(auth)/": ["login.tsx"] }, { "[id]/": ["page.js"] }] }, { "docs/": ["my notes.md"] }],
  "route-folder names with brackets, parens and spaces build normal folders"
);

// --- basename / dirname -----------------------------------------------------

assertEqual(basename("src/a/App.jsx"), "App.jsx", "basename takes the last segment");
assertEqual(basename("README.md"), "README.md", "basename of a root file is itself");
assertEqual(dirname("src/a/App.jsx"), "src/a", "dirname drops the last segment");
assertEqual(dirname("README.md"), "", "dirname of a root file is empty");

// --- ancestorDirs -------------------------------------------------------------

assertEqual(ancestorDirs("src/a/App.jsx"), ["src", "src/a"], "ancestorDirs lists every containing folder, outermost first");
assertEqual(ancestorDirs("src/App.jsx"), ["src"], "one level deep");
assertEqual(ancestorDirs("README.md"), [], "a root file has no ancestors");

// --- topLevelDirs -------------------------------------------------------------

assertEqual(
  [...topLevelDirs(["src/a.js", "src/lib/b.js", "tests/c.js", "README.md"])].sort(),
  ["src", "tests"],
  "topLevelDirs collects each first-level folder once, ignoring root files"
);
assertEqual([...topLevelDirs(["README.md"])], [], "no folders, nothing to expand");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
