// W2.4 (Build Workbench plan) — tests for explorerOps.js.
//
// Loads the REAL lib/workbench/explorerOps.js through loadSource.mjs (no
// pasted copy). Its one import, ./fileTree, is satisfied with the real
// fileTree.js — so these tests also fail if the two drift apart.
//
// Run: node frontend/app/lib/workbench/__tests__/explorerOps.test.mjs
import { loadSource } from "./loadSource.mjs";

const fileTree = loadSource("../fileTree.js");
const {
  MAX_PATH_LENGTH,
  joinPath,
  entryPaths,
  checkEntryName,
  collapseNested,
  pathsUnder,
  uniqueCopyPath,
  planDrop,
  deleteSummary,
} = loadSource("../explorerOps.js", { imports: { "./fileTree": fileTree } });

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

const FILES = ["README.md", "src/App.jsx", "src/lib/util.js", "src/empty/.gitkeep", "tests/a.test.js", "notes"];
const taken = entryPaths(FILES);

// --- joinPath / entryPaths ------------------------------------------------

assertEqual(joinPath("", "a.js"), "a.js", "joinPath: root");
assertEqual(joinPath("src/lib", "a.js"), "src/lib/a.js", "joinPath: nested folder");
assertEqual(
  [...taken].sort(),
  ["README.md", "notes", "src", "src/App.jsx", "src/empty", "src/empty/.gitkeep", "src/lib", "src/lib/util.js", "tests", "tests/a.test.js"],
  "entryPaths: every file plus every folder above it, each once"
);
assertEqual([...entryPaths([])], [], "entryPaths: empty project");

// --- checkEntryName -------------------------------------------------------

const ok = (name, ctx = {}) => checkEntryName(name, { dir: "src", taken, ...ctx });

assertEqual(ok("New.jsx"), { name: "New.jsx", error: null }, "a plain new name is fine");
assertEqual(ok("  New.jsx  "), { name: "New.jsx", error: null }, "surrounding spaces are trimmed, not rejected");
assertEqual(ok("").error, "Enter a name.", "empty is refused");
assertEqual(ok("   ").error, "Enter a name.", "whitespace-only is refused");
assertEqual(ok("a/b.js").error, "A name can't contain / or \\.", "a slash is refused (a name is one path segment)");
assertEqual(ok("a\\b.js").error, "A name can't contain / or \\.", "so is a backslash");
assertEqual(ok(".").error, "\".\" isn't allowed as a name.", '"." is refused');
assertEqual(ok("..").error, "\"..\" isn't allowed as a name.", '".." is refused');
assertEqual(ok("bad*name").error, "Use letters, numbers, spaces and . _ - [ ] ( ) @ + ~ = , only.", "a character the server would refuse is refused up front");
assertEqual(ok("tab\there").error !== null, true, "a tab is refused");
assertEqual(ok("nul\u0000").error !== null, true, "a NUL is refused");
assertEqual(ok("émoji.js").error !== null, true, "non-ASCII letters are refused (the server's charset is ASCII)");

for (const good of ["[id].js", "(auth)", "@modal", "+page.svelte", "my file.js", "a~b=c,d.js", ".env", "a-b_c.d"]) {
  assertEqual(ok(good).error, null, `framework-style / odd-but-legal name "${good}" is accepted`);
}

assertEqual(ok("App.jsx").error, "\"App.jsx\" already exists here.", "a name already used by a file in that folder is refused");
assertEqual(ok("lib").error, "\"lib\" already exists here.", "so is one used by a folder");
assertEqual(checkEntryName("src", { dir: "", taken }).error, "\"src\" already exists here.", "at the root too");
assertEqual(checkEntryName("App.jsx", { dir: "tests", taken }).error, null, "the same name in a different folder is fine");
assertEqual(ok("App.jsx", { selfPath: "src/App.jsx" }).error, null, "renaming an entry to its own name isn't a clash");
assertEqual(ok("app.jsx", { selfPath: "src/App.jsx" }).error, null, "changing only the letter case is allowed");
assertEqual(ok("lib", { selfPath: "src/App.jsx" }).error, "\"lib\" already exists here.", "renaming onto a DIFFERENT existing entry is still a clash");

const longDir = "d".repeat(MAX_PATH_LENGTH - 5);
assertEqual(checkEntryName("abcdef", { dir: longDir, taken: new Set() }).error, "That path is too long.", "a path over the server's limit is refused");
assertEqual(checkEntryName("abcd", { dir: longDir, taken: new Set() }).error, null, "...and exactly at the limit is fine");

// --- collapseNested / pathsUnder ------------------------------------------

assertEqual(collapseNested(["src", "src/App.jsx", "tests/a.test.js"]), ["src", "tests/a.test.js"], "collapseNested: a file inside a selected folder is covered by it");
assertEqual(collapseNested(["src/App.jsx", "src"]), ["src"], "...whatever order they came in");
assertEqual(collapseNested(["src", "srcx"]), ["src", "srcx"], "...but a shared prefix isn't nesting");
assertEqual(collapseNested(["a.js", "a.js", "b.js"]), ["a.js", "b.js"], "collapseNested: duplicates removed, order kept");
assertEqual(collapseNested([]), [], "collapseNested: nothing");
assertEqual(pathsUnder(FILES, "src"), ["src/App.jsx", "src/lib/util.js", "src/empty/.gitkeep"], "pathsUnder: a folder's files (placeholders included — they move/delete with it)");
assertEqual(pathsUnder(FILES, "README.md"), ["README.md"], "pathsUnder: a file is itself");
assertEqual(pathsUnder(FILES, "sr"), [], "pathsUnder: a name prefix isn't a folder");

// --- uniqueCopyPath -------------------------------------------------------

assertEqual(uniqueCopyPath("src/App.jsx", taken), "src/App copy.jsx", "duplicate: 'name copy.ext', extension kept last");
assertEqual(uniqueCopyPath("README.md", taken), "README copy.md", "duplicate: at the root");
assertEqual(
  uniqueCopyPath("src/App.jsx", entryPaths([...FILES, "src/App copy.jsx"])),
  "src/App copy 2.jsx",
  "duplicate: the next free number when 'copy' is taken"
);
assertEqual(
  uniqueCopyPath("src/App.jsx", entryPaths([...FILES, "src/App copy.jsx", "src/App copy 2.jsx"])),
  "src/App copy 3.jsx",
  "duplicate: keeps counting"
);
assertEqual(uniqueCopyPath("notes", taken), "notes copy", "duplicate: a file with no extension");
assertEqual(uniqueCopyPath(".env", entryPaths([".env"])), ".env copy", "duplicate: a dotfile has no extension to keep last");
assertEqual(uniqueCopyPath("a.test.js", entryPaths(["a.test.js"])), "a.test copy.js", "duplicate: only the LAST extension is treated as one");
assertEqual(uniqueCopyPath("src/lib", taken, { isDir: true }), "src/lib copy", "duplicate: a folder");
assertEqual(uniqueCopyPath("v1.2", entryPaths(["v1.2"]), { isDir: true }), "v1.2 copy", "duplicate: a folder name with a dot gets no extension treatment");

// --- planDrop --------------------------------------------------------------

let plan = planDrop({ sources: ["README.md"], targetDir: "src", taken });
assertEqual(plan, { moves: [{ from: "README.md", to: "src/README.md" }], error: null }, "drop a file onto a folder: one move into it");

plan = planDrop({ sources: ["src/App.jsx"], targetDir: "", taken });
assertEqual(plan, { moves: [{ from: "src/App.jsx", to: "App.jsx" }], error: null }, "drop onto the root: moves it out to the top level");

plan = planDrop({ sources: ["src/App.jsx"], targetDir: "src", taken });
assertEqual(plan, { moves: [], error: null }, "drop a file onto the folder it's already in: nothing to do, not an error");

plan = planDrop({ sources: ["tests", "tests/a.test.js"], targetDir: "src", taken });
assertEqual(plan.moves, [{ from: "tests", to: "src/tests" }], "a folder and a file inside it: just the folder moves");

plan = planDrop({ sources: ["src"], targetDir: "src/lib", taken });
assertEqual(plan, { moves: [], error: "Can't move \"src\" into itself." }, "a folder can't go into its own subfolder");
plan = planDrop({ sources: ["src/lib"], targetDir: "src/lib", taken });
assertEqual(plan.error, "Can't move \"lib\" into itself.", "a folder dropped onto itself is refused");

plan = planDrop({ sources: ["README.md"], targetDir: "src", taken: entryPaths([...FILES, "src/README.md"]) });
assertEqual(plan, { moves: [], error: "\"README.md\" already exists in \"src\"." }, "a name clash in the target is refused, naming the folder");
plan = planDrop({ sources: ["src/lib/util.js"], targetDir: "", taken: entryPaths([...FILES, "util.js"]) });
assertEqual(plan.error, "\"util.js\" already exists in the project root.", "...and 'the project root' for the top level");
plan = planDrop({ sources: ["notes"], targetDir: "", taken });
assertEqual(plan.moves, [], "moving a root file to the root is already there");

plan = planDrop({ sources: ["README.md", "src/README.md"], targetDir: "tests", taken: entryPaths([...FILES, "src/README.md"]) });
assertEqual(plan, { moves: [], error: "Two items named \"README.md\" can't go in the same folder." }, "two sources with the same name can't land together");

plan = planDrop({ sources: ["README.md", "src/App.jsx", "src/lib/util.js"], targetDir: "tests", taken });
assertEqual(plan.moves.length, 3, "a multi-selection makes one move per item");
plan = planDrop({ sources: ["README.md", "src/App.jsx"], targetDir: "src", taken: entryPaths([...FILES]) });
assertEqual(plan.moves, [{ from: "README.md", to: "src/README.md" }], "items already in the target are skipped while the rest still move");
plan = planDrop({ sources: ["README.md", "src"], targetDir: "src", taken });
assertEqual(plan.error, "Can't move \"src\" into itself.", "one illegal item aborts the whole drop — nothing half-moves");

const deep = "f".repeat(MAX_PATH_LENGTH - 3);
plan = planDrop({ sources: ["a.js"], targetDir: deep, taken: new Set(["a.js"]) });
assertEqual(plan.error, "The new path for \"a.js\" would be too long.", "a move that would exceed the path limit is refused");

// --- deleteSummary ----------------------------------------------------------

let d = deleteSummary({ paths: ["src/App.jsx"], filePaths: FILES });
assertEqual(d.title, "Delete \"App.jsx\"?", "delete one file: title");
assertEqual(d.message, "This removes the file from the project. You can't undo this.", "delete one file: message");
assertEqual(d.roots, ["src/App.jsx"], "delete one file: roots");

d = deleteSummary({ paths: ["src"], filePaths: FILES });
assertEqual(d.title, "Delete folder \"src\"?", "delete a folder: title");
assertEqual(d.fileCount, 2, "delete a folder: the .gitkeep placeholder isn't counted");
assertEqual(d.message, "This removes the folder and the 2 files in it. You can't undo this.", "delete a folder: message with the count");

d = deleteSummary({ paths: ["src/empty"], filePaths: FILES });
assertEqual(d.message, "This removes the empty folder. You can't undo this.", "delete an empty folder (only its placeholder)");

d = deleteSummary({ paths: ["tests"], filePaths: FILES });
assertEqual(d.message, "This removes the folder and the 1 file in it. You can't undo this.", "delete a folder: singular");

d = deleteSummary({ paths: ["src", "src/App.jsx", "README.md"], filePaths: FILES });
assertEqual(d.roots, ["src", "README.md"], "delete several: nested selections collapse to their folder");
assertEqual(d.title, "Delete 2 items?", "delete several: title counts the collapsed items");
assertEqual(d.fileCount, 3, "delete several: file total counts each file once");
assertEqual(d.message, "This removes 2 items (3 files in total). You can't undo this.", "delete several: message");

d = deleteSummary({ paths: ["src/App.jsx"], filePaths: FILES, dirtyPaths: ["src/App.jsx"] });
assertEqual(d.message, "This removes the file from the project. You can't undo this. Unsaved changes in App.jsx will be lost.", "unsaved edits in one deleted file are named");
d = deleteSummary({ paths: ["src"], filePaths: FILES, dirtyPaths: ["src/App.jsx", "src/lib/util.js", "tests/a.test.js"] });
assertEqual(d.message.endsWith("Unsaved changes in 2 open files will be lost."), true, "unsaved edits: only files under the deleted paths count");
d = deleteSummary({ paths: ["src"], filePaths: FILES, dirtyPaths: ["tests/a.test.js"] });
assertEqual(d.message.includes("Unsaved"), false, "unsaved edits elsewhere aren't mentioned");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
