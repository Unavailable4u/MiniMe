// W6.1 (Build Workbench plan) — tests for lib/preview/detectKind.js.
//
// Loads the REAL detectKind.js through loadSource.mjs (reused from
// lib/workbench/'s __tests__ — see instrument.test.mjs's header for why
// this is a reuse, not a copy). No `imports` are passed on purpose:
// detectKind.js must stay dependency-free, and this load fails if
// someone adds an `import` to it.
//
// Run: node frontend/app/lib/preview/__tests__/detectKind.test.mjs
import { loadSource } from "../../workbench/__tests__/loadSource.mjs";

const { detectKind } = loadSource("../../preview/detectKind.js", {});

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

function meta(paths) {
  return Object.fromEntries(paths.map((p) => [p, {}]));
}

assertEqual(
  detectKind(meta(["index.html", "style.css", "app.js"])),
  { kind: "static", entryPath: "index.html", reason: null },
  "plain static project: root index.html wins"
);

assertEqual(
  detectKind(meta(["package.json", "src/App.jsx", "src/main.jsx"])),
  { kind: "react", entryPath: null, reason: null },
  "package.json + a .jsx file anywhere -> react"
);

assertEqual(
  detectKind(meta(["package.json", "src/App.tsx"])),
  { kind: "react", entryPath: null, reason: null },
  ".tsx counts the same as .jsx"
);

assertEqual(
  detectKind(meta(["main.py", "requirements.txt"])),
  { kind: "python", entryPath: null, reason: null },
  "a .py file anywhere -> python"
);

// The Vite/CRA misclassification this priority order exists to avoid —
// see detectKind.js's own header for the reasoning.
assertEqual(
  detectKind(meta(["package.json", "index.html", "src/main.jsx"])).kind,
  "react",
  "a React project's own index.html doesn't get misread as a plain static site"
);

assertEqual(
  detectKind(meta(["docs/nested/index.html", "public/index.html", "readme.md"])).entryPath,
  "public/index.html",
  "shortest path wins when more than one index.html exists (2 segments beats 3)"
);

assertEqual(
  detectKind(meta(["README.md", "notes.txt"])),
  {
    kind: null,
    entryPath: null,
    reason: "No index.html, React entry, or Python file found — nothing here matches a preview provider yet.",
  },
  "nothing recognizable -> null kind with a reason"
);

assertEqual(
  detectKind({}),
  { kind: null, entryPath: null, reason: "This project has no files yet." },
  "an empty project gets its own reason, not the generic one"
);

assertEqual(
  detectKind(meta(["package.json", "notes.txt"])).kind,
  null,
  "package.json alone, with no .jsx/.tsx anywhere, is NOT enough to call it a react project"
);

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
