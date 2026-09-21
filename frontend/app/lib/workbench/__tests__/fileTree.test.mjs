// W2.3a (Build Workbench plan) — tests for fileTree.js; extended in W2.4
// for the flat-row / filter / path-arithmetic helpers.
//
// Loads the REAL lib/workbench/fileTree.js through loadSource.mjs. No
// `imports` are passed on purpose: fileTree.js must stay dependency-free,
// and this load fails if someone adds an `import` to it.
//
// Run: node frontend/app/lib/workbench/__tests__/fileTree.test.mjs
import { loadSource } from "./loadSource.mjs";

const {
  buildFileTree,
  sortedChildren,
  basename,
  dirname,
  ancestorDirs,
  topLevelDirs,
  PLACEHOLDER_NAME,
  isPlaceholderPath,
  realFilePaths,
  isSameOrDescendant,
  remapPath,
  flattenVisible,
  filterPaths,
  rangeBetween,
} = loadSource("../fileTree.js");

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


// --- W2.4: placeholders ------------------------------------------------

assertEqual(PLACEHOLDER_NAME, ".gitkeep", "the empty-folder placeholder is .gitkeep");
assertEqual(isPlaceholderPath("src/empty/.gitkeep"), true, "a .gitkeep in a folder is a placeholder");
assertEqual(isPlaceholderPath(".gitkeep"), true, "...and one at the root");
assertEqual(isPlaceholderPath("src/.gitkeep.bak"), false, "a longer name isn't");
assertEqual(isPlaceholderPath("src/gitkeep"), false, "...nor one without the dot");
assertEqual(
  realFilePaths({ "src/a.js": {}, "src/empty/.gitkeep": {}, ".gitkeep": {} }),
  ["src/a.js"],
  "realFilePaths drops the placeholders"
);
assertEqual(realFilePaths(null), [], "realFilePaths(null) (first load) is empty");

// --- W2.4: path arithmetic ----------------------------------------------

assertEqual(isSameOrDescendant("src", "src"), true, "a path is 'same or under' itself");
assertEqual(isSameOrDescendant("src/a/b.js", "src"), true, "a file under a folder is under it");
assertEqual(isSameOrDescendant("srcx/a.js", "src"), false, "a sibling that merely shares a prefix is not under it");
assertEqual(isSameOrDescendant("src", "src/a"), false, "a folder isn't under its own child");

assertEqual(remapPath("src", "src", "lib"), "lib", "remapPath: the moved path itself");
assertEqual(remapPath("src/a/b.js", "src", "lib"), "lib/a/b.js", "remapPath: a file inside the moved folder");
assertEqual(remapPath("src/a.js", "src/a.js", "src/b.js"), "src/b.js", "remapPath: a renamed file");
assertEqual(remapPath("srcx/a.js", "src", "lib"), "srcx/a.js", "remapPath: a shared-prefix sibling is untouched");
assertEqual(remapPath("other/a.js", "src", "lib"), "other/a.js", "remapPath: an unrelated path is untouched");
assertEqual(remapPath("a/b.js", "a", "x/y"), "x/y/b.js", "remapPath: moving into a nested destination");
assertEqual(remapPath("a/b.js", "a", "a2"), "a2/b.js", "remapPath: destination that extends the old name");

// --- W2.4: flattenVisible ------------------------------------------------

const w24rows = (rows) => rows.map((r) => `${"  ".repeat(r.depth)}${r.name}${r.type === "dir" ? "/" : ""}`);
const w24tree = buildFileTree(meta("README.md", "src/App.jsx", "src/lib/util.js", "src/empty/.gitkeep", "tests/a.test.js"));

assertEqual(
  w24rows(flattenVisible(w24tree, new Set())),
  ["src/", "tests/", "README.md"],
  "nothing expanded: just the top level"
);
assertEqual(
  w24rows(flattenVisible(w24tree, new Set(["src"]))),
  ["src/", "  empty/", "  lib/", "  App.jsx", "tests/", "README.md"],
  "an expanded folder lists its children (folders first) right under it; the empty folder shows, its .gitkeep doesn't"
);
assertEqual(
  w24rows(flattenVisible(w24tree, new Set(["src", "src/lib", "src/empty"]))),
  ["src/", "  empty/", "  lib/", "    util.js", "  App.jsx", "tests/", "README.md"],
  "nested expansion nests depth"
);
assertEqual(
  w24rows(flattenVisible(w24tree, new Set(["src/lib"]))),
  ["src/", "tests/", "README.md"],
  "an expanded folder under a collapsed one stays hidden"
);
const someRows = flattenVisible(w24tree, new Set(["src"]));
assertEqual(
  someRows.map((r) => [r.path, r.parent, r.open]),
  [
    ["src", "", true],
    ["src/empty", "src", false],
    ["src/lib", "src", false],
    ["src/App.jsx", "src", undefined],
    ["tests", "", false],
    ["README.md", "", undefined],
  ],
  "rows carry their full path, parent folder and (for folders) open state"
);
assertEqual(flattenVisible(buildFileTree({}), new Set()), [], "an empty w24tree flattens to no rows");

// --- W2.4: filterPaths -----------------------------------------------------

const allPaths = ["README.md", "src/App.jsx", "src/lib/util.js", "src/empty/.gitkeep", "tests/a.test.js"];
assertEqual(filterPaths(allPaths, ""), null, "a blank query means no filter");
assertEqual(filterPaths(allPaths, "   "), null, "so does whitespace");
assertEqual(filterPaths(allPaths, null), null, "...and null");

let f = filterPaths(allPaths, "app");
assertEqual([[...f.files], [...f.dirs]], [["src/App.jsx"], ["src"]], "matching is case-insensitive and keeps the folders above a match");
f = filterPaths(allPaths, "src util");
assertEqual([...f.files], ["src/lib/util.js"], "every term must match somewhere in the path");
assertEqual([...f.dirs].sort(), ["src", "src/lib"], "...and all the folders above it come along");
f = filterPaths(allPaths, "test");
assertEqual([...f.files], ["tests/a.test.js"], "a term can match a folder name");
f = filterPaths(allPaths, "gitkeep");
assertEqual([...f.files], [], "placeholders never match");
f = filterPaths(allPaths, "zzz");
assertEqual([[...f.files], [...f.dirs]], [[], []], "no match: an empty (not null) result, so the UI can say 'nothing matches'");

const filtered = filterPaths(allPaths, "util");
assertEqual(
  w24rows(flattenVisible(w24tree, new Set(), filtered)),
  ["src/", "  lib/", "    util.js"],
  "filtering shows only matches and their folders, opened up regardless of `expanded`"
);
assertEqual(
  w24rows(flattenVisible(w24tree, new Set(), filterPaths(allPaths, "zzz"))),
  [],
  "a filter with no matches flattens to no rows"
);

// --- W2.4: rangeBetween ----------------------------------------------------

const rr = flattenVisible(w24tree, new Set(["src"]));
assertEqual(rangeBetween(rr, "src/lib", "src/App.jsx"), ["src/lib", "src/App.jsx"], "range, top to bottom");
assertEqual(rangeBetween(rr, "src/App.jsx", "src"), ["src", "src/empty", "src/lib", "src/App.jsx"], "range, bottom to top comes back in list order");
assertEqual(rangeBetween(rr, "src", "src"), ["src"], "a range of one");
assertEqual(rangeBetween(rr, "gone/x.js", "tests"), ["tests"], "an anchor that's no longer listed: just the target");
assertEqual(rangeBetween(rr, "src", "gone/x.js"), [], "a target that isn't listed: nothing");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
