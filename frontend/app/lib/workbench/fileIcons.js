// frontend/app/lib/workbench/fileIcons.js — W2.4 (Build Workbench plan).
// Which icon a file gets in the explorer, decided by its name. Returns
// a KEY, not a component: this file stays plain data-in/data-out (no
// imports, so its test can load the real source under plain `node`),
// and Explorer's own key -> lucide icon + color table does the
// rendering. Adding a file type is one line in a list here plus, if it
// needs a new look, one entry there.
//
// Keys: code · markup · style · json · data · config · doc · image ·
// shell · lock · package · file (the fallback).

// Whole-name matches win over the extension (`package.json` is a
// package, not just json).
const BY_NAME = {
  "package.json": "package",
  "package-lock.json": "lock",
  "yarn.lock": "lock",
  "pnpm-lock.yaml": "lock",
  "poetry.lock": "lock",
  "cargo.lock": "lock",
  dockerfile: "config",
  makefile: "config",
  license: "doc",
  "license.md": "doc",
};

const BY_EXTENSION = {
  // code
  js: "code", jsx: "code", mjs: "code", cjs: "code", ts: "code", tsx: "code",
  py: "code", rb: "code", go: "code", rs: "code", java: "code", kt: "code",
  swift: "code", c: "code", h: "code", cpp: "code", hpp: "code", cs: "code",
  php: "code", lua: "code", dart: "code", vue: "code", svelte: "code",
  sql: "code", r: "code", scala: "code",
  // markup / style
  html: "markup", htm: "markup", xml: "markup",
  css: "style", scss: "style", sass: "style", less: "style",
  // data / config
  json: "json", jsonc: "json",
  csv: "data", tsv: "data",
  yaml: "config", yml: "config", toml: "config", ini: "config", cfg: "config", conf: "config",
  lock: "lock",
  // docs / media / scripts
  md: "doc", mdx: "doc", txt: "doc", rst: "doc",
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image",
  ico: "image", bmp: "image", avif: "image", svg: "image",
  sh: "shell", bash: "shell", zsh: "shell", ps1: "shell", bat: "shell", cmd: "shell",
};

/**
 * @param {string} name - a file NAME (last path segment), not a path
 * @returns {string} one of the keys listed at the top of this file
 */
export function fileIconKey(name) {
  const lower = String(name || "").toLowerCase();
  if (BY_NAME[lower]) return BY_NAME[lower];
  if (lower.startsWith("readme")) return "doc";
  // .gitignore, .env, .eslintrc, .prettierrc… — dotfiles are configuration.
  if (lower.startsWith(".")) return "config";
  const dot = lower.lastIndexOf(".");
  const ext = dot > 0 ? lower.slice(dot + 1) : "";
  return BY_EXTENSION[ext] || "file";
}
