// frontend/app/lib/workbench/problems.js — W8.3b (Build Workbench plan).
// Pure logic behind the Problems panel: turning what
// `GET /api/workspaces/{id}/code/findings` returns (W8.3a,
// eo/code_findings.py) into the three things the workbench shows —
//   1. a bottom-panel list (ProblemsPanel.jsx),
//   2. a count on the Problems tab and in the status bar,
//   3. @codemirror/lint diagnostics drawn in the editor.
// No imports — see fileTree.js's header; the test loader in
// __tests__/problems.test.mjs enforces it. (That is also why the
// diagnostics here are plain objects and `toLintDiagnostics()` takes
// the document as a duck-typed `{lines, length, line(n)}`: CodeEditor.jsx
// hands over CM6's real Text, a test hands over a stand-in.)
//
// THE SERVER SHAPE (see code_findings.py's header for how it is made):
//   {path, line, severity, message, source, file_version}
//   - `severity` is already @codemirror/lint's own vocabulary
//     ("error" | "warning" | "info") — nothing is translated here.
//   - `line` is 1-based, or null for a problem with the file as a whole.
//     The backend only stores a line that exists in the version it
//     checked, so a non-null line is trustworthy FOR THAT VERSION.
//   - `file_version` is the file's version when the finding was made.
//
// WHY VERSIONS MATTER. A finding describes one version of one file. The
// pipeline replaces a file's findings every time it rewrites the file,
// but nothing re-checks them when the PERSON saves (or a kept proposal
// lands): the file moves to a newer version and the old findings, line
// numbers included, may no longer be true. So findings are compared
// with versions in two places and treated differently on purpose:
//   - the LIST (annotateFindings): still shown, marked `stale`, and
//     left out of the counts. Hiding them would make the list lie by
//     omission; counting them would nag about things that may be fixed.
//   - the EDITOR (editorDiagnostics): drawn only when the finding's
//     version is exactly the version of the text in the buffer, because
//     an underline on the wrong line is worse than none.

/** @codemirror/lint's severity names, most serious first. */
export const SEVERITIES = ["error", "warning", "info"];

const SEVERITY_RANK = { error: 0, warning: 1, info: 2 };

const SOURCE_LABELS = {
  sandbox_test: "Test run",
  security_scan: "Security scan",
  semgrep: "Semgrep",
  gitleaks: "Gitleaks",
};

/**
 * Human label for a finding's `source`. Known ones get proper wording;
 * a tool name the backend adds later is shown as it came rather than
 * dropped; null/empty gives "".
 *
 * @param {string|null|undefined} source
 * @returns {string}
 */
export function sourceLabel(source) {
  if (typeof source !== "string" || !source) return "";
  return SOURCE_LABELS[source] || source;
}

function compareFindings(a, b) {
  if (a.path !== b.path) return a.path < b.path ? -1 : 1;
  // A file-level problem (no line) sorts ahead of that file's lines.
  const la = a.line ?? 0;
  const lb = b.line ?? 0;
  if (la !== lb) return la - lb;
  const sa = SEVERITY_RANK[a.severity];
  const sb = SEVERITY_RANK[b.severity];
  if (sa !== sb) return sa - sb;
  if (a.message !== b.message) return a.message < b.message ? -1 : 1;
  return 0;
}

/**
 * Clean, sort and key the server's findings.
 *
 * Accepts the response body (`{findings: [...]}`) or the bare array.
 * Anything that isn't a usable finding (no path, no message, not an
 * object) is dropped instead of throwing — one malformed row must not
 * blank the whole panel. An unknown severity becomes "info" (shown,
 * never an error the server didn't claim); a line that isn't a positive
 * integer becomes null (file-level).
 *
 * The camelCase `fileVersion` is this module's spelling of the
 * server's `file_version`. Every result also gets a unique `key`
 * (React list key) — two identical findings on one line are legal, so
 * a repeat gets a `#2`, `#3` suffix.
 *
 * Sorted by path, then line (file-level first), then severity, then
 * message, so the list reads top to bottom the way the files do.
 *
 * @param {unknown} raw
 * @returns {{key: string, path: string, line: number|null, severity: "error"|"warning"|"info", message: string, source: string|null, fileVersion: number|null}[]}
 */
export function normalizeFindings(raw) {
  const list = Array.isArray(raw) ? raw : Array.isArray(raw?.findings) ? raw.findings : [];
  const out = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const path = typeof item.path === "string" ? item.path : "";
    const message = typeof item.message === "string" ? item.message.trim() : "";
    if (!path || !message) continue;
    out.push({
      path,
      line: Number.isInteger(item.line) && item.line >= 1 ? item.line : null,
      severity: SEVERITIES.includes(item.severity) ? item.severity : "info",
      message,
      source: typeof item.source === "string" && item.source ? item.source : null,
      fileVersion: Number.isInteger(item.file_version) ? item.file_version : null,
    });
  }
  out.sort(compareFindings);

  const seen = new Map();
  return out.map((f) => {
    const base = `${f.path}:${f.line ?? "file"}:${f.source ?? ""}:${f.message}`;
    const n = (seen.get(base) || 0) + 1;
    seen.set(base, n);
    return { key: n === 1 ? base : `${base}#${n}`, ...f };
  });
}

/**
 * Whether two normalized lists say the same thing — lets a reload that
 * found nothing new keep the old array (and so skip every re-render and
 * editor update downstream of it).
 *
 * @param {object[]} a
 * @param {object[]} b
 * @returns {boolean}
 */
export function sameFindings(a, b) {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

/**
 * `{path: version}` for the files as they are on the server right now,
 * from the provider's list() result — or null while that hasn't loaded
 * (annotateFindings() then makes no judgement either way).
 *
 * @param {Record<string, {version?: number}>|null|undefined} filesMeta
 * @returns {Record<string, number|null>|null}
 */
export function fileVersions(filesMeta) {
  if (!filesMeta || typeof filesMeta !== "object") return null;
  const out = {};
  for (const [path, meta] of Object.entries(filesMeta)) {
    out[path] = Number.isInteger(meta?.version) ? meta.version : null;
  }
  return out;
}

/**
 * Adds `stale` to each finding, and drops findings for files that no
 * longer exist (deleted or renamed since the check).
 *
 * `stale` = the file has moved to a newer version than the one the
 * finding was made against. A finding with no recorded version, or one
 * whose file version isn't known, is never called stale — "unknown" is
 * not "out of date". `versions` null (file list not loaded yet) keeps
 * everything, unjudged.
 *
 * @param {object[]} findings - normalizeFindings() output
 * @param {Record<string, number|null>|null} versions - fileVersions() output
 * @returns {(object & {stale: boolean})[]}
 */
export function annotateFindings(findings, versions) {
  const out = [];
  for (const f of findings) {
    if (!versions) {
      out.push({ ...f, stale: false });
      continue;
    }
    if (!Object.prototype.hasOwnProperty.call(versions, f.path)) continue;
    const current = versions[f.path];
    const stale = f.fileVersion != null && Number.isInteger(current) && current > f.fileVersion;
    out.push({ ...f, stale });
  }
  return out;
}

/**
 * Counts by severity of the findings that still apply — stale ones are
 * left out (see this file's header).
 *
 * @param {({severity: string, stale?: boolean})[]} annotated
 * @returns {{error: number, warning: number, info: number, total: number}}
 */
export function countProblems(annotated) {
  const counts = { error: 0, warning: 0, info: 0, total: 0 };
  for (const f of annotated) {
    if (f.stale || !Object.prototype.hasOwnProperty.call(SEVERITY_RANK, f.severity)) continue;
    counts[f.severity] += 1;
    counts.total += 1;
  }
  return counts;
}

/**
 * "No problems", or e.g. "1 error, 2 warnings, 3 info".
 *
 * @param {{error: number, warning: number, info: number}} counts
 * @returns {string}
 */
export function summarizeProblems(counts) {
  const parts = [];
  if (counts.error > 0) parts.push(`${counts.error} ${counts.error === 1 ? "error" : "errors"}`);
  if (counts.warning > 0) parts.push(`${counts.warning} ${counts.warning === 1 ? "warning" : "warnings"}`);
  if (counts.info > 0) parts.push(`${counts.info} info`);
  return parts.length ? parts.join(", ") : "No problems";
}

/**
 * What the Problems tab shows beside its label: the total, toned by the
 * worst severity present. null when there is nothing to show.
 *
 * @param {{error: number, warning: number, info: number, total: number}} counts
 * @returns {{count: number, tone: "error"|"warning"|"info", title: string}|null}
 */
export function tabBadge(counts) {
  if (!counts || counts.total <= 0) return null;
  const tone = counts.error > 0 ? "error" : counts.warning > 0 ? "warning" : "info";
  return { count: counts.total, tone, title: summarizeProblems(counts) };
}

/**
 * The list grouped by file, in the order the findings came in (which is
 * normalizeFindings()'s path order).
 *
 * @param {({path: string})[]} annotated
 * @returns {{path: string, items: object[]}[]}
 */
export function groupByPath(annotated) {
  const groups = [];
  const byPath = new Map();
  for (const f of annotated) {
    let group = byPath.get(f.path);
    if (!group) {
      group = { path: f.path, items: [] };
      byPath.set(f.path, group);
      groups.push(group);
    }
    group.items.push(f);
  }
  return groups;
}

/**
 * The findings to draw in ONE editor, as plain `{line, severity,
 * message, source}` — only those made against exactly the version of the
 * text in the buffer (a finding with no recorded version is trusted).
 * A file-level finding is drawn on line 1: there is no better place for
 * it, and the panel still says it is about the file.
 *
 * @param {object[]} findings - normalizeFindings() output
 * @param {string} path
 * @param {number|null|undefined} bufferVersion - the buffer's saved version
 * @returns {{line: number, severity: string, message: string, source?: string}[]}
 */
export function editorDiagnostics(findings, path, bufferVersion) {
  const out = [];
  for (const f of findings) {
    if (f.path !== path) continue;
    if (f.fileVersion != null && f.fileVersion !== bufferVersion) continue;
    const diagnostic = { line: f.line ?? 1, severity: f.severity, message: f.message };
    const label = sourceLabel(f.source);
    if (label) diagnostic.source = label;
    out.push(diagnostic);
  }
  return out;
}

/**
 * The character range a one-line diagnostic underlines: the line's text
 * without its indentation or trailing spaces, so the squiggle sits under
 * the code rather than running out to the margin. A blank line gets one
 * character (the line break) — a zero-width range would draw as a stray
 * widget — unless it is the very end of the document.
 *
 * @param {number} lineFrom - document offset of the line's first character
 * @param {string} lineText
 * @param {number} docLength
 * @returns {{from: number, to: number}}
 */
export function diagnosticSpan(lineFrom, lineText, docLength) {
  const trimmedStart = lineText.length - lineText.trimStart().length;
  const trimmedEnd = lineText.trimEnd().length;
  if (trimmedEnd <= trimmedStart) {
    return { from: lineFrom, to: Math.min(lineFrom + 1, docLength) };
  }
  return { from: lineFrom + trimmedStart, to: lineFrom + trimmedEnd };
}

/**
 * editorDiagnostics()' output -> `Diagnostic[]` for @codemirror/lint's
 * setDiagnostics(), resolved against the document as it is NOW. A line
 * that isn't in the document is skipped, not clamped: this text may have
 * been edited since the finding was made, and "no mark" beats a mark on
 * an unrelated line (the backend makes the same call — code_findings.py).
 *
 * @param {{lines: number, length: number, line: (n: number) => {from: number, text: string}}} doc
 * @param {{line: number, severity: string, message: string, source?: string}[]} diagnostics
 * @returns {{from: number, to: number, severity: string, message: string, source?: string}[]}
 */
export function toLintDiagnostics(doc, diagnostics) {
  const out = [];
  for (const d of diagnostics || []) {
    if (!Number.isInteger(d.line) || d.line < 1 || d.line > doc.lines) continue;
    const line = doc.line(d.line);
    const { from, to } = diagnosticSpan(line.from, line.text, doc.length);
    const lint = { from, to, severity: d.severity, message: d.message };
    if (d.source) lint.source = d.source;
    out.push(lint);
  }
  return out;
}
