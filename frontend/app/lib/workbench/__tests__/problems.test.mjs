// W8.3b (Build Workbench plan) — tests for problems.js.
//
// Loads the REAL lib/workbench/problems.js through loadSource.mjs (no
// pasted copy). No `imports` are passed on purpose: problems.js must
// stay dependency-free, and this load fails if someone adds an `import`.
//
// The finding fixtures use the shape GET .../code/findings returns
// (backend/eo/code_findings.py's list_findings()): snake_case
// `file_version`, `line` null for a file-level finding.
//
// Run: node frontend/app/lib/workbench/__tests__/problems.test.mjs
import { loadSource } from "./loadSource.mjs";

const {
  SEVERITIES,
  sourceLabel,
  normalizeFindings,
  sameFindings,
  fileVersions,
  annotateFindings,
  countProblems,
  summarizeProblems,
  tabBadge,
  groupByPath,
  editorDiagnostics,
  diagnosticSpan,
  toLintDiagnostics,
} = loadSource("../problems.js");

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

// A stand-in for CM6's Text: only the three members toLintDiagnostics()
// is documented to use.
function fakeDoc(text) {
  const rows = text.split("\n");
  let pos = 0;
  const lines = rows.map((t) => {
    const info = { from: pos, to: pos + t.length, text: t };
    pos += t.length + 1;
    return info;
  });
  return { lines: lines.length, length: text.length, line: (n) => lines[n - 1] };
}

const f = (over) => ({
  path: "app.py",
  line: 3,
  severity: "error",
  message: "boom",
  source: "sandbox_test",
  file_version: 2,
  ...over,
});

// --- constants / labels -------------------------------------------------

assertEqual(SEVERITIES, ["error", "warning", "info"], "severities are @codemirror/lint's own names, worst first");
assertEqual(
  ["sandbox_test", "security_scan", "semgrep", "gitleaks", "future_tool", null, undefined, ""].map(sourceLabel),
  ["Test run", "Security scan", "Semgrep", "Gitleaks", "future_tool", "", "", ""],
  "source labels: known ones worded, unknown shown as-is, missing is empty"
);

// --- normalizeFindings --------------------------------------------------

assertEqual(normalizeFindings(null), [], "null -> []");
assertEqual(normalizeFindings({ nope: 1 }), [], "an object without findings -> []");
assertEqual(normalizeFindings("x"), [], "a string -> []");
assertEqual(
  normalizeFindings({ findings: [f({})] }),
  [
    {
      key: "app.py:3:sandbox_test:boom",
      path: "app.py",
      line: 3,
      severity: "error",
      message: "boom",
      source: "sandbox_test",
      fileVersion: 2,
    },
  ],
  "reads the response envelope; file_version becomes fileVersion; a key is added"
);
assertEqual(normalizeFindings([f({})]).length, 1, "also accepts the bare array");

assertEqual(
  normalizeFindings([
    null,
    "str",
    42,
    f({ path: "" }),
    f({ path: 7 }),
    f({ message: "" }),
    f({ message: "   " }),
    f({ message: 5 }),
    f({ message: "kept" }),
  ]).map((x) => x.message),
  ["kept"],
  "rows with no usable path or message are dropped, the rest survive"
);

{
  const [x] = normalizeFindings([f({ severity: "critical", line: 0, source: "", file_version: "2" })]);
  assertEqual(
    [x.severity, x.line, x.source, x.fileVersion],
    ["info", null, null, null],
    "unknown severity -> info; line 0 -> null; empty source -> null; non-integer version -> null"
  );
}
assertEqual(normalizeFindings([f({ line: 2.5 })])[0].line, null, "a fractional line -> null (file-level)");
assertEqual(normalizeFindings([f({ line: null })])[0].line, null, "null line stays null");
assertEqual(normalizeFindings([f({ message: "  padded  " })])[0].message, "padded", "message is trimmed");

assertEqual(
  normalizeFindings([
    f({ path: "b.py", line: 1, message: "z" }),
    f({ path: "a.py", line: 9, message: "z" }),
    f({ path: "a.py", line: 2, severity: "info", message: "z" }),
    f({ path: "a.py", line: 2, severity: "error", message: "z" }),
    f({ path: "a.py", line: null, message: "z" }),
  ]).map((x) => `${x.path}:${x.line ?? "file"}:${x.severity}`),
  ["a.py:file:error", "a.py:2:error", "a.py:2:info", "a.py:9:error", "b.py:1:error"],
  "sorted by path, then line (file-level first), then severity"
);

{
  const keys = normalizeFindings([f({}), f({}), f({})]).map((x) => x.key);
  assertEqual(new Set(keys).size, 3, "identical findings still get unique keys");
  assertEqual(keys[1].endsWith("#2") && keys[2].endsWith("#3"), true, "repeat keys are suffixed #2, #3");
}

// --- sameFindings -------------------------------------------------------

{
  const a = normalizeFindings([f({}), f({ line: 8 })]);
  const b = normalizeFindings([f({}), f({ line: 8 })]);
  assertEqual(sameFindings(a, a), true, "same array is the same");
  assertEqual(sameFindings(a, b), true, "equal content from a fresh fetch is the same");
  assertEqual(sameFindings(a, normalizeFindings([f({})])), false, "different content is different");
  assertEqual(sameFindings([], []), true, "two empty lists are the same");
}

// --- fileVersions / annotateFindings -----------------------------------

assertEqual(fileVersions(null), null, "no file list yet -> null");
assertEqual(fileVersions(undefined), null, "undefined file list -> null");
assertEqual(
  fileVersions({ "a.py": { version: 4, size: 10 }, "b.py": {}, "c.py": { version: "x" }, "d.py": null }),
  { "a.py": 4, "b.py": null, "c.py": null, "d.py": null },
  "versions per path; anything that isn't an integer version is null"
);

{
  const findings = normalizeFindings([
    f({ path: "old.py", file_version: 1 }),
    f({ path: "same.py", file_version: 3 }),
    f({ path: "ahead.py", file_version: 5 }),
    f({ path: "nover.py", file_version: null }),
    f({ path: "unknown.py", file_version: 1 }),
    f({ path: "gone.py", file_version: 1 }),
  ]);
  const versions = { "old.py": 2, "same.py": 3, "ahead.py": 4, "nover.py": 9, "unknown.py": null };
  const out = annotateFindings(findings, versions);
  assertEqual(
    out.map((x) => [x.path, x.stale]),
    [
      ["ahead.py", false],
      ["nover.py", false],
      ["old.py", true],
      ["same.py", false],
      ["unknown.py", false],
    ],
    "stale only when the file moved past the finding's version; a deleted file's finding is dropped"
  );
  assertEqual(
    annotateFindings(findings, null).every((x) => x.stale === false) && annotateFindings(findings, null).length === 6,
    true,
    "file list not loaded: everything kept, nothing called stale"
  );
}

// --- countProblems / summarizeProblems / tabBadge -----------------------

{
  const annotated = [
    { severity: "error", stale: false },
    { severity: "error", stale: true },
    { severity: "warning", stale: false },
    { severity: "warning", stale: false },
    { severity: "info", stale: false },
    { severity: "toString", stale: false },
  ];
  const counts = countProblems(annotated);
  assertEqual(counts, { error: 1, warning: 2, info: 1, total: 4 }, "counts skip stale findings and unknown severities");
  assertEqual(summarizeProblems(counts), "1 error, 2 warnings, 1 info", "summary wording");
  assertEqual(
    tabBadge(counts),
    { count: 4, tone: "error", title: "1 error, 2 warnings, 1 info" },
    "badge: total, worst-severity tone, summary as title"
  );
}
assertEqual(countProblems([]), { error: 0, warning: 0, info: 0, total: 0 }, "no findings -> zero counts");
assertEqual(summarizeProblems({ error: 0, warning: 0, info: 0 }), "No problems", "nothing to say");
assertEqual(summarizeProblems({ error: 2, warning: 0, info: 0 }), "2 errors", "plural errors, others omitted");
assertEqual(summarizeProblems({ error: 0, warning: 1, info: 0 }), "1 warning", "singular warning");
assertEqual(tabBadge({ error: 0, warning: 0, info: 0, total: 0 }), null, "no badge when there are no problems");
assertEqual(tabBadge(null), null, "no badge without counts");
assertEqual(tabBadge({ error: 0, warning: 2, info: 1, total: 3 }).tone, "warning", "tone: warning when no errors");
assertEqual(tabBadge({ error: 0, warning: 0, info: 1, total: 1 }).tone, "info", "tone: info when only info");

// --- groupByPath --------------------------------------------------------

assertEqual(
  groupByPath([{ path: "a" , n: 1 }, { path: "a", n: 2 }, { path: "b", n: 3 }]),
  [
    { path: "a", items: [{ path: "a", n: 1 }, { path: "a", n: 2 }] },
    { path: "b", items: [{ path: "b", n: 3 }] },
  ],
  "groups by file, keeping order"
);
assertEqual(groupByPath([]), [], "no findings, no groups");

// --- editorDiagnostics --------------------------------------------------

{
  const findings = normalizeFindings([
    f({ path: "app.py", line: 3, file_version: 2, message: "now" }),
    f({ path: "app.py", line: 7, file_version: 1, message: "old" }),
    f({ path: "app.py", line: null, file_version: 2, severity: "warning", message: "whole file", source: "semgrep" }),
    f({ path: "app.py", line: 5, file_version: null, severity: "info", message: "unversioned", source: null }),
    f({ path: "other.py", line: 1, file_version: 2, message: "elsewhere" }),
  ]);
  assertEqual(
    editorDiagnostics(findings, "app.py", 2),
    [
      { line: 1, severity: "warning", message: "whole file", source: "Semgrep" },
      { line: 3, severity: "error", message: "now", source: "Test run" },
      { line: 5, severity: "info", message: "unversioned" },
    ],
    "only this file, only the buffer's version (or unversioned); file-level goes on line 1; sources are labelled"
  );
  assertEqual(
    editorDiagnostics(findings, "app.py", 3).map((d) => d.message),
    ["unversioned"],
    "a buffer on another version keeps only the unversioned finding"
  );
  assertEqual(
    editorDiagnostics(findings, "app.py", undefined).map((d) => d.message),
    ["unversioned"],
    "a buffer with no known version can't vouch for versioned findings"
  );
  assertEqual(editorDiagnostics(findings, "missing.py", 2), [], "a file with no findings -> []");
}

// --- diagnosticSpan -----------------------------------------------------

assertEqual(diagnosticSpan(10, "    x = 1  ", 100), { from: 14, to: 19 }, "span skips indentation and trailing spaces");
assertEqual(diagnosticSpan(10, "x", 100), { from: 10, to: 11 }, "single character line");
assertEqual(diagnosticSpan(10, "", 100), { from: 10, to: 11 }, "blank line: one character (the line break)");
assertEqual(diagnosticSpan(10, "   ", 100), { from: 10, to: 11 }, "whitespace-only line behaves as blank");
assertEqual(diagnosticSpan(0, "", 0), { from: 0, to: 0 }, "empty document: nothing to extend into");
assertEqual(diagnosticSpan(5, "", 5), { from: 5, to: 5 }, "blank last line: stays inside the document");
assertEqual(diagnosticSpan(0, "\tfoo", 4), { from: 1, to: 4 }, "tab indentation is skipped too");

// --- toLintDiagnostics --------------------------------------------------

{
  const doc = fakeDoc("import os\n\n    print(x)\n");
  // lines: 1 "import os" (0-9), 2 "" (10), 3 "    print(x)" (11-23), 4 "" (24)
  assertEqual(
    toLintDiagnostics(doc, [
      { line: 1, severity: "error", message: "m1", source: "Test run" },
      { line: 3, severity: "warning", message: "m3" },
    ]),
    [
      { from: 0, to: 9, severity: "error", message: "m1", source: "Test run" },
      { from: 15, to: 23, severity: "warning", message: "m3" },
    ],
    "1-based lines become document ranges; source only when present"
  );
  assertEqual(
    toLintDiagnostics(doc, [
      { line: 0, severity: "error", message: "a" },
      { line: 5, severity: "error", message: "b" },
      { line: 2.5, severity: "error", message: "c" },
      { line: "3", severity: "error", message: "d" },
      { line: null, severity: "error", message: "e" },
    ]),
    [],
    "a line that isn't in the document is skipped, never clamped onto another"
  );
  assertEqual(
    toLintDiagnostics(doc, [{ line: 2, severity: "info", message: "blank" }]),
    [{ from: 10, to: 11, severity: "info", message: "blank" }],
    "a blank line is underlined over its line break"
  );
  assertEqual(toLintDiagnostics(doc, null), [], "null diagnostics -> []");
  assertEqual(toLintDiagnostics(doc, []), [], "no diagnostics -> []");
}

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll problems tests passed.");
