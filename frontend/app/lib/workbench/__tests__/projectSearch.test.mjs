// frontend/app/lib/workbench/__tests__/projectSearch.test.mjs — W2.6
// (Build Workbench plan) — tests for projectSearch.js.
//
// Loads the REAL lib/workbench/projectSearch.js through loadSource.mjs
// (no pasted copy). Its one import, ./fileTree, is satisfied with the
// real fileTree.js — so these tests also fail if the two drift apart,
// same convention as explorerOps.test.mjs.
//
// Run: node frontend/app/lib/workbench/__tests__/projectSearch.test.mjs
import { loadSource } from "./loadSource.mjs";

const fileTree = loadSource("../fileTree.js");
const {
  isSearchableFilePath,
  searchableFilePaths,
  matchesInLine,
  trimLineForDisplay,
  searchFileContent,
  searchProject,
} = loadSource("../projectSearch.js", { imports: { "./fileTree": fileTree } });

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

// --- isSearchableFilePath / searchableFilePaths --------------------------

assertTrue(isSearchableFilePath("src/App.jsx"), "an ordinary code file is searchable");
assertTrue(isSearchableFilePath("README.md"), "a doc file is searchable");
assertTrue(isSearchableFilePath("assets/logo.svg"), "SVG is text (XML) and stays searchable, unlike fileIcons' 'image' category");
assertEqual(isSearchableFilePath("assets/photo.png"), false, "a binary image extension is not searchable");
assertEqual(isSearchableFilePath("vendor/lib.wasm"), false, "a wasm binary is not searchable");
assertEqual(isSearchableFilePath("src/.gitkeep"), false, "a folder placeholder is never searchable — it isn't a real file");

{
  const filesMeta = {
    "src/App.jsx": { size: 10 },
    "assets/photo.png": { size: 20 },
    "src/lib/.gitkeep": { size: 0 },
    "README.md": { size: 5 },
  };
  assertEqual(
    searchableFilePaths(filesMeta).sort(),
    ["README.md", "src/App.jsx"],
    "searchableFilePaths drops both placeholders and binary extensions"
  );
}
assertEqual(searchableFilePaths({}), [], "no files at all is an empty list, not an error");
assertEqual(searchableFilePaths(null), [], "a null filesMeta doesn't throw");

// --- matchesInLine ---------------------------------------------------------

assertEqual(matchesInLine("hello world", "world"), [[6, 11]], "a single match returns its [start, end) range");
assertEqual(matchesInLine("Hello World", "world"), [[6, 11]], "matching is case-insensitive");
assertEqual(matchesInLine("no match here", "xyz"), [], "no occurrence is an empty list, not null");
assertEqual(
  matchesInLine("foo foo foo", "foo"),
  [[0, 3], [4, 7], [8, 11]],
  "every non-overlapping occurrence is returned, in order"
);
assertEqual(matchesInLine("aaaa", "aa"), [[0, 2], [2, 4]], "matches don't overlap — the scan resumes AFTER each one");
assertEqual(matchesInLine("", "x"), [], "an empty line never matches");

// --- trimLineForDisplay -----------------------------------------------------

{
  const short = "const x = 1;";
  const ranges = matchesInLine(short, "x");
  assertEqual(trimLineForDisplay(short, ranges), { text: short, ranges }, "a short line passes through untouched");
}

{
  // A line far longer than the display cap, with plenty of text on
  // BOTH sides of the match so the window is cut short of the real
  // line end in both directions.
  const long = "a".repeat(1000) + "NEEDLE" + "b".repeat(1000);
  const ranges = matchesInLine(long, "needle");
  const { text, ranges: trimmed } = trimLineForDisplay(long, ranges);
  assertTrue(text.length <= 302, "a long line is windowed down near MAX_LINE_DISPLAY (plus ellipses)");
  assertTrue(text.startsWith("…"), "cutting the front of the line leaves a leading ellipsis");
  assertTrue(text.endsWith("…"), "cutting the tail of the line leaves a trailing ellipsis");
  assertEqual(trimmed.length, 1, "the match itself survives the window");
  const [s, e] = trimmed[0];
  assertEqual(text.slice(s, e).toLowerCase(), "needle", "the re-mapped range still points at the actual match text");
}

{
  // A match near the very START of a long line — the window shouldn't
  // try to center itself past the line's own start.
  const long = "NEEDLE" + "b".repeat(1000);
  const ranges = matchesInLine(long, "needle");
  const { text, ranges: trimmed } = trimLineForDisplay(long, ranges);
  assertTrue(!text.startsWith("…"), "a match at the very start needs no leading ellipsis");
  assertTrue(text.endsWith("…"), "the tail is still cut");
  const [s, e] = trimmed[0];
  assertEqual(text.slice(s, e).toLowerCase(), "needle", "the range still points at the match after a start-anchored window");
}

// --- searchFileContent -----------------------------------------------------

assertEqual(searchFileContent("no matches at all", "zzz"), [], "no matches in the whole file is an empty list");

{
  const content = "line one\nline TWO has a match\nline three";
  const results = searchFileContent(content, "match");
  assertEqual(results.length, 1, "only the matching line comes back");
  assertEqual(results[0].line, 2, "line numbers are 1-based");
  assertEqual(results[0].column, 16, "column is the 1-based start of the first match");
  assertEqual(results[0].text, "line TWO has a match", "the full (short) line text is kept");
}

{
  // CRLF content shouldn't shift line numbers vs. what CodeEditor shows.
  const content = "one\r\ntwo needle\r\nthree";
  const results = searchFileContent(content, "needle");
  assertEqual(results.length, 1, "CRLF line endings are normalised before splitting");
  assertEqual(results[0].line, 2, "line numbers match what the LF-normalised editor buffer would show");
}

{
  const content = ["match"].concat(Array.from({ length: 10 }, () => "match")).join("\n");
  const results = searchFileContent(content, "match", { maxMatches: 3 });
  assertEqual(results.length, 3, "maxMatches caps the number of matching LINES returned for one file");
}

{
  // A line with several occurrences is still one result row, with the
  // total count carried separately from the display ranges.
  const results = searchFileContent("foo foo foo", "foo");
  assertEqual(results.length, 1, "multiple matches on one line are still a single result row");
  assertEqual(results[0].matchCount, 3, "matchCount reports every occurrence on that line");
  assertEqual(results[0].ranges.length, 3, "a short line keeps every match range for highlighting");
}

// --- searchProject -----------------------------------------------------------

assertEqual(searchProject({ "a.js": "hello" }, ""), { results: [], truncated: false, matchCount: 0 }, "an empty query returns nothing");
assertEqual(searchProject({ "a.js": "hello" }, "   "), { results: [], truncated: false, matchCount: 0 }, "a whitespace-only query is treated as empty");
assertEqual(searchProject({}, "x"), { results: [], truncated: false, matchCount: 0 }, "no files at all is an empty (not truncated) result");
assertEqual(searchProject(null, "x"), { results: [], truncated: false, matchCount: 0 }, "a null fileTexts map doesn't throw");

{
  const fileTexts = {
    "z.js": "no match",
    "a.js": "has a needle here",
    "m.js": "another needle over here",
  };
  const { results, truncated, matchCount } = searchProject(fileTexts, "needle");
  assertEqual(truncated, false, "well under either cap isn't truncated");
  assertEqual(matchCount, 2, "matchCount sums matches across every file");
  assertEqual(results.map((r) => r.path), ["a.js", "m.js"], "results are sorted by path, not by fileTexts' own key order");
  assertTrue(results.every((r) => r.truncated === false), "no single file hit its own cap either");
}

{
  // Project-wide cap: stop collecting once the total is reached, and
  // say so. matchCount/maxTotalMatches budget result ROWS (matching
  // lines) — five lines with one "x" each is 5 rows, unlike the
  // "multiple matches on one line" case above, which is still 1 row.
  const fiveLines = "x\nx\nx\nx\nx";
  const fileTexts = { "a.js": fiveLines, "b.js": fiveLines };
  const { results, truncated, matchCount } = searchProject(fileTexts, "x", { maxTotalMatches: 6 });
  assertEqual(matchCount, 6, "the project-wide row cap is respected across files, not just within one");
  assertTrue(truncated, "hitting the project-wide cap is reported as truncated");
  assertEqual(results.length, 2, "a.js still contributes its full share before the budget runs out mid-b.js");
  const a = results.find((r) => r.path === "a.js");
  const b = results.find((r) => r.path === "b.js");
  assertEqual(a.matches.length, 5, "a.js (sorted first) gets its full 5 rows before the budget is touched");
  assertEqual(b.matches.length, 1, "b.js only gets whatever's left of the budget (1 row)");
  assertTrue(!a.truncated, "a.js wasn't itself cut short — the project-wide cut happened in b.js");
  assertTrue(b.truncated, "b.js is flagged as the file that got cut short by the remaining budget");
}

// --- caseSensitive option ---------------------------------------------------

assertEqual(matchesInLine("Hello World", "world"), [[6, 11]], "default matchesInLine is case-insensitive");
assertEqual(matchesInLine("Hello World", "world", { caseSensitive: true }), [], "caseSensitive: true respects exact case");
assertEqual(matchesInLine("Hello World", "World", { caseSensitive: true }), [[6, 11]], "caseSensitive: true still matches exact case");

{
  const fileTexts = { "a.js": "Needle\nneedle" };
  const insensitive = searchProject(fileTexts, "needle");
  const sensitive = searchProject(fileTexts, "needle", { caseSensitive: true });
  assertEqual(insensitive.matchCount, 2, "case-insensitive (the default) matches both lines");
  assertEqual(sensitive.matchCount, 1, "caseSensitive: true only matches the exact-case line");
}

{
  // Per-file cap: one very matchy file doesn't crowd out a smaller
  // signal in another file that comes later alphabetically.
  const fileTexts = {
    "a-noisy.js": Array.from({ length: 500 }, () => "x").join("\n"),
    "b-quiet.js": "x",
  };
  const { results } = searchProject(fileTexts, "x", { maxMatchesPerFile: 5 });
  const noisy = results.find((r) => r.path === "a-noisy.js");
  const quiet = results.find((r) => r.path === "b-quiet.js");
  assertEqual(noisy.matches.length, 5, "the per-file cap limits one noisy file's own contribution");
  assertTrue(noisy.truncated, "a file cut off by its own per-file cap is flagged truncated");
  assertTrue(quiet && quiet.matches.length === 1, "a per-file cap on one file doesn't starve a later file's own budget");
}

// -------------------------------------------------------------------------

if (failures > 0) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
} else {
  console.log("\nAll projectSearch.js tests passed.");
}
