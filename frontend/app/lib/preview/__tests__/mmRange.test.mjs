// W6.5 (Build Workbench plan) — tests for lib/preview/mmRange.js.
// Same dependency-free loadSource() approach as the rest of this
// directory; the fixture is the SAME HTML instrument.test.mjs (W6.3)
// pins its golden data-mm values against, run through the real
// instrumentHtml(), so "the cursor maps to the right mm" is checked
// against ground truth the instrumenter itself produced, not against
// hand-written mm strings that could drift from it.
//
// Run: node frontend/app/lib/preview/__tests__/mmRange.test.mjs
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";
import { instrumentHtml } from "../../preview/instrument.js";

const { parseMm, extractMmRanges, innermostMmAt, offsetOf, rangeFromMm } = loadSource("../../preview/mmRange.js");

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

// --- parseMm ---------------------------------------------------------------

assertEqual(
  parseMm("index.html:4:2:7:8"),
  { mm: "index.html:4:2:7:8", path: "index.html", startLine: 4, startCol: 2, endLine: 7, endCol: 8 },
  "parses path + four numbers"
);
assertEqual(parseMm("C:/proj/a.html:1:0:2:5")?.path, "C:/proj/a.html", "a path containing ':' still splits correctly (numbers are parsed from the right)");
assertEqual(parseMm("nope"), null, "garbage is null, not a throw");
assertEqual(parseMm(":1:2:3:4"), null, "an empty path is rejected");
assertEqual(parseMm(42), null, "a non-string is null");

// --- fixture from W6.3's own golden test ------------------------------------

const fixture =
  "<!DOCTYPE html>\n" +
  '<html><head><title>x</title><meta charset="utf-8"><style>.a{color:red}</style></head>\n' +
  "<body>\n" +
  '  <div class="a">\n' +
  "    <p>hi</p>\n" +
  '    <img src="x.png">\n' +
  "  </div>\n" +
  "  <script>console.log(1)</script>\n" +
  "</body></html>";

const instrumented = instrumentHtml(fixture, "index.html").code;
const ranges = extractMmRanges(instrumented);
const mms = ranges.map((r) => r.mm);

assertEqual(mms.includes("index.html:4:2:7:8"), true, "extractMmRanges finds the div's W6.3 golden value");
assertEqual(ranges.every((r) => r.path === "index.html"), true, "every extracted range carries the entry path");
assertEqual(extractMmRanges(undefined), [], "non-string html -> no ranges");
assertEqual(extractMmRanges('<p data-mm="a&amp;b.html:1:0:1:9">x</p>')[0]?.path, "a&b.html", "an escaped & in the path is decoded back");

const pRange = ranges.find((r) => r.startLine === 5);
assertEqual(!!pRange, true, "the <p> on line 5 was instrumented");

// --- innermostMmAt ----------------------------------------------------------
// Cursor columns here are 0-based, matching the contract; EditorWorkbench
// converts CodeEditor's 1-based cursor col before calling.

assertEqual(innermostMmAt(ranges, "index.html", 5, 6), pRange.mm, "a cursor inside <p>hi</p> picks the <p> (innermost), not the enclosing div");
assertEqual(innermostMmAt(ranges, "index.html", 5, 1), "index.html:4:2:7:8", "a cursor in the indentation before <p> is still inside the div only");
assertEqual(innermostMmAt(ranges, "index.html", 4, 5), "index.html:4:2:7:8", "a cursor inside the div's own opening tag picks the div");
assertEqual(innermostMmAt(ranges, "index.html", 7, 4), "index.html:4:2:7:8", "a cursor inside the closing </div> is still the div");
// <body> (3:0 -> 9:7) encloses the whole fixture, so "outside the div"
// resolves to the body — the next range out — rather than to nothing.
assertEqual(innermostMmAt(ranges, "index.html", 7, 8), "index.html:3:0:9:7", "the END is exclusive: right after </div> is outside it (falls back to <body>)");
assertEqual(innermostMmAt(ranges, "index.html", 4, 1), "index.html:3:0:9:7", "left of the div's start is outside it (falls back to <body>)");
assertEqual(innermostMmAt(ranges, "index.html", 1, 3), null, "before <body> even starts (the doctype line) matches nothing");
assertEqual(innermostMmAt(ranges, "other.html", 5, 6), null, "a cursor in a DIFFERENT file never matches this file's ranges");
assertEqual(innermostMmAt(ranges, null, 5, 6), null, "no active path -> null");
assertEqual(innermostMmAt(ranges, "index.html", NaN, 0), null, "a non-finite position -> null, not a throw");

// Nesting where a later-starting range must beat an earlier one that
// also contains the cursor, whatever order they appear in the list.
const nested = [
  { mm: "n.html:1:0:9:0", path: "n.html", startLine: 1, startCol: 0, endLine: 9, endCol: 0 },
  { mm: "n.html:3:2:5:2", path: "n.html", startLine: 3, startCol: 2, endLine: 5, endCol: 2 },
  { mm: "n.html:4:4:4:10", path: "n.html", startLine: 4, startCol: 4, endLine: 4, endCol: 10 },
];
assertEqual(innermostMmAt(nested, "n.html", 4, 6), "n.html:4:4:4:10", "three levels deep -> the deepest");
assertEqual(innermostMmAt([...nested].reverse(), "n.html", 4, 6), "n.html:4:4:4:10", "list order doesn't matter");
assertEqual(innermostMmAt(nested, "n.html", 3, 3), "n.html:3:2:5:2", "middle level when the cursor is outside the inner one");

// --- offsetOf / rangeFromMm ---------------------------------------------------

assertEqual(offsetOf("ab\ncd\nef", 1, 0), 0, "line 1 col 0 is offset 0");
assertEqual(offsetOf("ab\ncd\nef", 2, 1), 4, "line 2 col 1");
assertEqual(offsetOf("ab\ncd\nef", 2, 99), 5, "a column past the line's end clamps to the line end (before the \\n)");
assertEqual(offsetOf("ab\ncd\nef", 99, 0), 8, "a line past the document clamps to the doc end");

const divParsed = parseMm("index.html:4:2:7:8");
const div = rangeFromMm(fixture, divParsed);
assertEqual(div.snippet.startsWith('<div class="a">'), true, "the snippet for the div range starts at its opening tag");
assertEqual(div.snippet.endsWith("</div>"), true, "...and ends exactly after its closing tag");
assertEqual(fixture.slice(div.from, div.to), div.snippet, "from/to offsets index the same text the snippet holds");

const pParsed = parseMm(pRange.mm);
assertEqual(rangeFromMm(fixture, pParsed).snippet, "<p>hi</p>", "the <p> range is exactly <p>hi</p>");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll mmRange.js tests passed.");
}
