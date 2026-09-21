// W2.4 (Build Workbench plan) — tests for fileIcons.js.
//
// Loads the REAL lib/workbench/fileIcons.js through loadSource.mjs. No
// `imports` are passed on purpose: it must stay dependency-free.
//
// Run: node frontend/app/lib/workbench/__tests__/fileIcons.test.mjs
import { loadSource } from "./loadSource.mjs";

const { fileIconKey } = loadSource("../fileIcons.js");

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

const cases = {
  "App.jsx": "code",
  "index.ts": "code",
  "main.py": "code",
  "page.svelte": "code",
  "index.html": "markup",
  "style.css": "style",
  "theme.scss": "style",
  "tsconfig.json": "json",
  "data.csv": "data",
  "config.yaml": "config",
  "pyproject.toml": "config",
  "README.md": "doc",
  "notes.txt": "doc",
  "logo.png": "image",
  "icon.SVG": "image",
  "build.sh": "shell",
  "package.json": "package",
  "package-lock.json": "lock",
  "yarn.lock": "lock",
  Dockerfile: "config",
  ".gitignore": "config",
  ".env": "config",
  ".env.local": "config",
  README: "doc",
  "readme.rst": "doc",
  LICENSE: "doc",
  "archive.zip": "file",
  notes: "file",
};
for (const [name, key] of Object.entries(cases)) {
  assertEqual(fileIconKey(name), key, `${name} → ${key}`);
}

assertEqual(fileIconKey("APP.JS"), "code", "extensions are case-insensitive");
assertEqual(fileIconKey("a.test.js"), "code", "only the last extension counts");
assertEqual(fileIconKey("Makefile"), "config", "whole-name matches are case-insensitive");
assertEqual(fileIconKey(""), "file", "an empty name falls back");
assertEqual(fileIconKey(undefined), "file", "undefined falls back");
assertEqual(fileIconKey("trailingdot."), "file", "a trailing dot has no extension");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
