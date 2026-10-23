// M2C B09 (Remove the browser Terminal panel) — static guard test.
//
// The browser editor no longer has a Terminal panel: the tab, the
// TerminalPanel component and its wiring in EditorWorkbench are gone
// (the backend command routes stay; M2C replaces them later). This
// test keeps it that way. It reads the REAL source files — no pasted
// copies — and fails if a code reference to the old panel comes back.
//
// What it checks:
//   - components/TerminalPanel.jsx does not exist;
//   - no file under app/ imports, renders or otherwise names
//     `TerminalPanel` in CODE (comment-only mentions are allowed — a few
//     files, e.g. lib/workbench/aiUpdates.js, still name it in comments);
//   - BottomPanel.jsx has no terminal empty-state entry, and still has
//     the Problems / Console / History entries with their original copy;
//   - EditorWorkbench.jsx no longer builds a terminal panel node or
//     hands one to BottomPanel.
//
// Plain `node` only (no bundler, no JSX parsing): it works on text, with
// comments stripped first so a comment can't trip or hide a match.
//
// Run: node frontend/app/lib/workbench/__tests__/terminalRemoved.test.mjs
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadSource } from "./loadSource.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const appRoot = resolve(here, "..", "..", ".."); // frontend/app

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

// Source text with block comments (JSX comment braces included) and `//`
// line comments removed.
function stripComments(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
}

/** Every .js / .jsx / .mjs file under `dir`, skipping node_modules, .next and test folders. */
function sourceFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name === ".next" || name === "__tests__") continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
    else if (/\.(jsx?|mjs)$/.test(name)) out.push(full);
  }
  return out;
}

// --- the component is gone -------------------------------------------------

assertEqual(existsSync(join(appRoot, "components", "TerminalPanel.jsx")), false, "components/TerminalPanel.jsx is deleted");

// --- no code reference anywhere under app/ ------------------------------

const offenders = sourceFiles(appRoot)
  .filter((file) => /\bTerminalPanel\b/.test(stripComments(readFileSync(file, "utf8"))))
  .map((file) => relative(appRoot, file));
assertEqual(offenders, [], "no file under app/ imports, renders or names TerminalPanel in code (comments excluded)");

// --- BottomPanel: Terminal entry removed, the others untouched -------------

const bottomPanelSrc = readFileSync(join(appRoot, "components", "workbench", "BottomPanel.jsx"), "utf8");
const bottomPanelCode = stripComments(bottomPanelSrc);
assertEqual(/terminal/i.test(bottomPanelCode), false, "BottomPanel.jsx has no Terminal entry or copy in code");

const { BOTTOM_TABS } = loadSource("../layoutPrefs.js");
for (const { id } of BOTTOM_TABS) {
  assertEqual(
    new RegExp(`^\\s*${id}:\\s*\\{`, "m").test(bottomPanelCode),
    true,
    `BottomPanel.jsx still has an empty-state entry for the "${id}" tab`
  );
}
assertEqual(bottomPanelCode.includes('title: "No problems"'), true, "Problems empty-state copy is unchanged");
assertEqual(bottomPanelCode.includes('title: "Console is empty"'), true, "Console empty-state copy is unchanged");
assertEqual(bottomPanelCode.includes('title: "File history"'), true, "History empty-state copy is unchanged");

// --- EditorWorkbench: no terminal panel node, nothing handed to BottomPanel --

const workbenchCode = stripComments(
  readFileSync(join(appRoot, "components", "workbench", "EditorWorkbench.jsx"), "utf8")
);
assertEqual(/terminalPanelNode/.test(workbenchCode), false, "EditorWorkbench.jsx no longer builds a terminal panel node");
assertEqual(/\bterminal\s*:/.test(workbenchCode), false, "EditorWorkbench.jsx passes no `terminal` entry in bottomPanels");
assertEqual(/["']terminal["']/.test(workbenchCode), false, "EditorWorkbench.jsx never selects a \"terminal\" bottom tab");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
