// frontend/app/lib/workbench/__tests__/quickOpen.test.mjs — W2.6
// (Build Workbench plan). Tests for quickOpen.js.
//
// Loads the REAL lib/workbench/quickOpen.js through loadSource.mjs. No
// `imports` are passed on purpose: quickOpen.js must stay
// dependency-free, and this load fails if someone adds an `import` to
// it — same convention as fileTree.test.mjs / tabUtils.test.mjs.
//
// Run: node frontend/app/lib/workbench/__tests__/quickOpen.test.mjs
import { loadSource } from "./loadSource.mjs";

const { scorePath, rankQuickOpen, defaultQuickOpenList, basenameMatchIndices } = loadSource("../quickOpen.js");

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
function assertTrue(cond, msg) {
  assertEqual(!!cond, true, msg);
}

// --- scorePath --------------------------------------------------------

assertEqual(scorePath("xyz", "app.js"), null, "a character missing from the path entirely is no match");
assertEqual(scorePath("app", "app.js"), scorePath("app", "app.js"), "scorePath is deterministic");

assertTrue(scorePath("app", "app.js").score > 0, "a real match scores above zero");

{
  const m = scorePath("appjs", "app.js");
  assertEqual(m.indices, [0, 1, 2, 4, 5], "indices land on the matched characters, in order, skipping the dot");
}

// A match in the basename outweighs the same characters, at the same
// relative position and boundary, landing only in a folder segment.
// ("wb" starts right after a "/" in both — the comparison isolates the
// basename bonus itself, not the separate boundary bonus below.)
{
  const inName = scorePath("wb", "x/wb.js"); // wb IS the basename
  const inFolder = scorePath("wb", "y/wb/x.js"); // wb is a folder segment, not the basename
  assertTrue(inName.score > 0 && inFolder.score > 0, "both are real matches");
  assertTrue(inName.score > inFolder.score, "a match landing in the basename outscores the same match in a folder segment");
}

// A realistic Quick Open scenario: typing part of a file's own name
// finds it.
{
  const paths = ["frontend/app/lib/workbench/fileTree.js", "frontend/app/lib/workbench/quickOpen.js"];
  const ranked = rankQuickOpen(paths, "quick");
  assertEqual(ranked[0].path, "frontend/app/lib/workbench/quickOpen.js", "typing part of a file's name finds it first");
}

// Boundary bonus: a match starting right after a "/" scores higher than
// the same character matched mid-word (neither at absolute position 0,
// which has its own scoring quirk tested separately by the tie-breaker
// case below).
{
  const atBoundary = scorePath("s", "a/sz.js"); // s right after "/"
  const midWord = scorePath("s", "asz.js"); // s is the 2nd char of "asz", not at a boundary
  assertTrue(atBoundary.score > midWord.score, "a match right at a path/word boundary scores higher");
}

// Consecutive-run bonus: a query matched as one unbroken run scores
// higher than the same characters scattered apart (neither starting at
// absolute position 0).
{
  const run = scorePath("abc", "-abcxyz");
  const scattered = scorePath("abc", "-axbxcx");
  assertTrue(run.score > scattered.score, "an unbroken run of matched characters scores higher than scattered ones");
}

// Tie-breakers: shorter path and earlier first match both read as closer.
{
  const short = scorePath("app", "app.js");
  const long = scorePath("app", "application/legacy/app.js");
  assertTrue(short.score > long.score, "a shorter path with an equally-good match ranks higher");
}

// --- rankQuickOpen ------------------------------------------------------

assertEqual(rankQuickOpen(["a.js", "b.js"], ""), [], "an empty (or whitespace-only) query returns no results");
assertEqual(rankQuickOpen(["a.js", "b.js"], "   "), [], "a whitespace-only query is treated as empty");
assertEqual(rankQuickOpen(null, "a"), [], "a null paths list doesn't throw");
assertEqual(rankQuickOpen(["a.js", "b.js"], "zzz"), [], "no matches at all is an empty list, not an error");

{
  const paths = ["src/App.jsx", "src/lib/app.js", "readme.md"];
  const ranked = rankQuickOpen(paths, "app");
  assertEqual(ranked.map((r) => r.path).sort(), ["src/App.jsx", "src/lib/app.js"].sort(), "case-insensitive match");
}

{
  // Path separators typed into the query line up with real "/"s.
  const paths = ["frontend/app/lib/workbench/quickOpen.js", "frontend/app/lib/workbench/fileTree.js"];
  const ranked = rankQuickOpen(paths, "lib/work");
  assertEqual(ranked.length, 2, "a '/' in the query matches an ordinary path separator character");
}

{
  const paths = Array.from({ length: 5 }, (_, i) => `file${i}.js`);
  const ranked = rankQuickOpen(paths, "file", { limit: 2 });
  assertEqual(ranked.length, 2, "limit caps the number of results returned");
}

{
  // Stable, deterministic ordering for equal scores: shorter path, then
  // alphabetical.
  const paths = ["zzz/a.js", "a.js", "aa.js"];
  const ranked = rankQuickOpen(paths, "a");
  assertEqual(ranked.map((r) => r.path), ["a.js", "aa.js", "zzz/a.js"], "equal-ish scores break ties by length then name");
}

// --- defaultQuickOpenList -----------------------------------------------

assertEqual(defaultQuickOpenList([], []), [], "no files at all is an empty list, not an error");
assertEqual(
  defaultQuickOpenList(null, null),
  [],
  "null paths/recentPaths (first load, nothing opened yet) doesn't throw"
);

{
  const paths = ["a.js", "b.js", "c.js"];
  const recent = ["c.js", "a.js"];
  const list = defaultQuickOpenList(paths, recent);
  assertEqual(
    list.map((r) => r.path),
    ["c.js", "a.js", "b.js"],
    "recent files come first, most-recent first, then the rest in project order"
  );
  assertTrue(
    list.every((r) => r.score === 0 && Array.isArray(r.indices) && r.indices.length === 0),
    "entries carry score 0 and empty indices — nothing typed yet to highlight"
  );
}

{
  // A recent path for a file that's since been deleted/renamed is
  // dropped rather than shown as a dangling entry.
  const paths = ["a.js", "b.js"];
  const recent = ["deleted.js", "a.js"];
  const list = defaultQuickOpenList(paths, recent);
  assertEqual(list.map((r) => r.path), ["a.js", "b.js"], "a recent path no longer in the project is filtered out");
}

{
  // A path appearing twice in recentPaths (opened, closed, reopened)
  // only appears once, at its first (most recent) position.
  const paths = ["a.js", "b.js"];
  const recent = ["a.js", "b.js", "a.js"];
  const list = defaultQuickOpenList(paths, recent);
  assertEqual(list.map((r) => r.path), ["a.js", "b.js"], "a duplicated recent path is de-duplicated at its first position");
}

{
  const paths = Array.from({ length: 5 }, (_, i) => `file${i}.js`);
  const list = defaultQuickOpenList(paths, [], { limit: 2 });
  assertEqual(list.length, 2, "limit caps the default list too");
}

// --- basenameMatchIndices -----------------------------------------------

// "src/lib/App.jsx" — the basename "App.jsx" starts at index 8 (right
// after the second "/").
assertEqual(
  basenameMatchIndices("src/lib/App.jsx", [4, 8, 9]),
  [0, 1],
  "indices inside the basename are remapped relative to its own start"
);
assertEqual(
  basenameMatchIndices("src/lib/App.jsx", [0, 4, 9]),
  [1],
  "indices that fall inside a FOLDER segment are dropped, not remapped"
);
assertEqual(basenameMatchIndices("App.jsx", [0, 1, 2]), [0, 1, 2], "a path with no '/' is its own basename");
assertEqual(basenameMatchIndices("src/App.jsx", []), [], "no indices in is no indices out");
assertEqual(basenameMatchIndices("src/App.jsx", null), [], "a null indices list doesn't throw");

// -------------------------------------------------------------------------

if (failures > 0) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
} else {
  console.log("\nAll quickOpen.js tests passed.");
}
