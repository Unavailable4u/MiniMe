// W2.5 (Build Workbench plan) — tests for formatOnSave.js.
//
// Loads the REAL lib/workbench/formatOnSave.js through loadSource.mjs (no
// pasted copy). No `imports` are passed: the file has no static imports —
// prettier is only ever pulled in by dynamic import() (that's the point of
// it, see the file's header), which is also why loading it here costs
// nothing.
//
// Two halves:
//   1. parserFor()/isFormattable() — pure extension → parser mapping.
//      Always runs.
//   2. formatContent() against the REAL prettier, one snippet per parser.
//      This is what proves the per-parser plugin lists in the file are
//      right — e.g. that babel/typescript really do need `estree` to
//      print, and that json/scss/less ride on babel/postcss. It runs only
//      when `prettier` is installed (the frontend's node_modules); if it
//      isn't, it says SKIP rather than failing, so this file still works
//      in a checkout that hasn't run `npm ci`.
//
// Run: node frontend/app/lib/workbench/__tests__/formatOnSave.test.mjs
import { loadSource } from "./loadSource.mjs";

const { parserFor, isFormattable, formatContent } = loadSource("../formatOnSave.js");

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

// --- parserFor / isFormattable -------------------------------------------

const EXPECTED = {
  "app.js": "babel",
  "App.jsx": "babel", // babel parses JSX itself — no separate parser
  "server.mjs": "babel",
  "legacy.cjs": "babel",
  "index.ts": "typescript",
  "App.tsx": "typescript", // babel can't parse TS type syntax
  "package.json": "json",
  "tsconfig.jsonc": "json",
  "style.css": "css",
  "theme.scss": "scss",
  "theme.less": "less",
  "index.html": "html",
  "page.htm": "html",
  "README.md": "markdown",
  "notes.markdown": "markdown",
};
for (const [path, parser] of Object.entries(EXPECTED)) {
  assertEqual(parserFor(path), parser, `parserFor(${path}) is ${parser}`);
}

assertEqual(parserFor("src/components/deep/App.JSX"), "babel", "the extension is matched case-insensitively, and nested paths work");
assertEqual(parserFor("src\\win\\path\\main.css"), "css", "Windows-style separators work");
assertEqual(parserFor("v1.2/README"), null, "a dot in a FOLDER name isn't an extension");
assertEqual(parserFor("Makefile"), null, "no extension -> no parser");
assertEqual(parserFor(".prettierrc"), null, "a dotfile (dot at position 0) isn't treated as an extension");
assertEqual(parserFor("script.py"), null, "an unsupported language has no parser");
assertEqual(parserFor("archive.tar.gz"), null, "only the last extension counts");
assertEqual(parserFor(""), null, "empty path -> null");
assertEqual(parserFor(undefined), null, "undefined path -> null, no throw");
assertEqual(parserFor(null), null, "null path -> null, no throw");

assertEqual(isFormattable("a.tsx"), true, "isFormattable: a mapped extension");
assertEqual(isFormattable("a.py"), false, "isFormattable: an unmapped extension");

// An unmapped file must come back untouched WITHOUT loading prettier at all.
assertEqual(await formatContent("script.py", "x   =  1"), "x   =  1", "formatContent returns unmapped content unchanged");
assertEqual(await formatContent("Makefile", "all:\n\techo"), "all:\n\techo", "...including files with no extension");

// --- formatContent against the real prettier ------------------------------

let prettierInstalled = true;
try {
  await import("prettier/standalone");
} catch {
  prettierInstalled = false;
}

if (!prettierInstalled) {
  console.log("SKIP: prettier isn't installed (run `npm ci` in frontend/) — the real-formatter half of this file didn't run");
} else {
  const CASES = [
    ["a.js", "const a   =  {b:1}", "const a = { b: 1 };\n"],
    ["a.jsx", "const A = () => <div className='x'>hi</div>", 'const A = () => <div className="x">hi</div>;\n'],
    ["a.mjs", "export const a   =  1", "export const a = 1;\n"],
    ["a.ts", "let x:number   =1", "let x: number = 1;\n"],
    ["a.tsx", "const A = (p:{a:string}) => <b>{p.a}</b>", "const A = (p: { a: string }) => <b>{p.a}</b>;\n"],
    ["a.json", '{"a":1,"b":[1,2]}', '{ "a": 1, "b": [1, 2] }\n'],
    ["a.css", "a{color:red}", "a {\n  color: red;\n}\n"],
    ["a.scss", "a{b{color:red}}", "a {\n  b {\n    color: red;\n  }\n}\n"],
    ["a.less", ".a{.b{color:red}}", ".a {\n  .b {\n    color: red;\n  }\n}\n"],
    ["a.html", "<div><p>hi</p></div>", "<div><p>hi</p></div>\n"],
    ["a.md", "#  Title\n\n*  item", "# Title\n\n- item\n"],
  ];
  for (const [path, input, expected] of CASES) {
    let actual;
    try {
      actual = await formatContent(path, input);
    } catch (err) {
      actual = `THREW: ${err.message.split("\n")[0]}`;
    }
    assertEqual(actual, expected, `formatContent(${path}) formats with the right plugins`);
  }

  // Formatting what's already formatted must be a no-op, or every save would dirty the buffer.
  const once = await formatContent("a.tsx", "const A = (p:{a:string}) => <b>{p.a}</b>");
  assertEqual(await formatContent("a.tsx", once), once, "formatting is idempotent (a second pass changes nothing)");

  // A syntax error mid-edit is normal; saveFile() relies on this REJECTING so it can skip formatting and save the buffer as-is.
  let rejected = null;
  try {
    await formatContent("broken.js", "const = ;");
  } catch (err) {
    rejected = err.name;
  }
  assertEqual(rejected, "SyntaxError", "content that doesn't parse rejects (saveFile falls back to the raw buffer)");

  // Concurrent calls share one load of prettier and its plugins — no double-fetching, no race.
  const results = await Promise.all([
    formatContent("x.js", "a  =  1"),
    formatContent("y.js", "b  =  2"),
    formatContent("z.ts", "let c:number=3"),
  ]);
  assertEqual(results, ["a = 1;\n", "b = 2;\n", "let c: number = 3;\n"], "concurrent formatContent calls all resolve correctly");
}

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
