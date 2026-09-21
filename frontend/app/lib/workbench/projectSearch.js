// frontend/app/lib/workbench/projectSearch.js — W2.6 (Build Workbench
// plan). Pure line-matching behind the Project Search panel
// (components/workbench/ProjectSearchPanel.jsx): given one file's TEXT
// and a query, which lines match and where.
//
// Unlike quickOpen.js's subsequence match on PATHS, this is a plain
// case-insensitive substring match on file CONTENT — the ordinary
// "find in files" contract a search-across-a-project box always
// delivers, no fuzzy scoring involved.
//
// Fetching content is NOT this file's job. There's no backend search
// route — fileProviders.js's `capabilities.search: false` is a real
// flag the whole way through W2.5, not a stale TODO (see that file's
// own header) — so Project Search is a CLIENT-SIDE content search:
// hooks/useProjectSearch.js is what reads every open buffer's `edited`
// text plus a provider.read() per closed file and hands the results to
// searchProject() below. This module only answers "given text I
// already have, and a query, what matches" — same file/content split
// as fileTree's buildFileTree() (paths only) vs Explorer's own
// provider.read() per open.
//
// Only import: ./fileTree, for realFilePaths()/isPlaceholderPath() —
// searchableFilePaths() reuses those instead of re-deriving the same
// "skip the folder placeholders" rule a second way (same one-import
// shape explorerOps.js already uses for the same reason).
import { isPlaceholderPath, realFilePaths } from "./fileTree";

// Extensions Project Search skips even though they're real, non-
// placeholder files: fetching and line-scanning a PNG's raw bytes
// finds nothing a person is looking for and just spends a request. Not
// the same list as fileIcons.js's "image" icon category on purpose —
// that one is about which PICTURE to show in the tree (an .svg gets
// the image icon there) and this one is about which files are text
// worth searching (an .svg IS text and stays searchable here; a
// genuinely binary format doesn't).
const SKIP_EXTENSIONS = new Set([
  "png", "jpg", "jpeg", "gif", "webp", "ico", "bmp", "avif",
  "woff", "woff2", "ttf", "eot", "otf",
  "zip", "gz", "tar", "rar", "7z",
  "pdf",
  "mp3", "mp4", "mov", "wav", "ogg", "webm",
  "wasm",
]);

function extensionOf(path) {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** @param {string} path @returns {boolean} */
export function isSearchableFilePath(path) {
  return !isPlaceholderPath(path) && !SKIP_EXTENSIONS.has(extensionOf(path));
}

/**
 * `filesMeta`'s paths, minus the folder placeholders and the
 * extensions above — what useProjectSearch.js loops over to decide
 * which paths need a provider.read() (or an already-open buffer) at
 * all.
 *
 * @param {Record<string, object>} filesMeta - provider.list()'s shape
 * @returns {string[]}
 */
export function searchableFilePaths(filesMeta) {
  return realFilePaths(filesMeta).filter(isSearchableFilePath);
}

// Caps, applied while a single file's content is being scanned and
// again across the whole project — a query like "e" against a large
// minified bundle (one file) or a big project (many files) must still
// return SOMETHING useful and fast rather than either hanging or
// handing the panel thousands of rows to render. Both are generous for
// an editor-adjacent panel: real matches for a real query rarely come
// close.
const DEFAULT_MAX_MATCHES_PER_FILE = 200;
const DEFAULT_MAX_TOTAL_MATCHES = 2000;
// A minified/generated line can run to tens of thousands of columns;
// nothing about "find in files" needs the WHOLE line rendered, only
// enough around the match to read it. Longer lines are windowed around
// their first match (see trimLineForDisplay below) rather than shown
// in full.
const MAX_LINE_DISPLAY = 300;

/**
 * Every non-overlapping occurrence of `needle` in `line`, as
 * `[start, end)` pairs. Case-insensitive by default (the ordinary
 * "find in files" default, same as Explorer's own filename filter);
 * pass `caseSensitive: true` to match exactly as typed. Same
 * indexOf-loop shape as quickOpen.js's scorePath(), for the same
 * reason: obviously correct beats clever for a palette-sized problem.
 *
 * @param {string} line
 * @param {string} needle - non-empty
 * @param {{caseSensitive?: boolean}} [opts]
 * @returns {[number, number][]}
 */
export function matchesInLine(line, needle, { caseSensitive = false } = {}) {
  const hay = caseSensitive ? line : line.toLowerCase();
  const n = caseSensitive ? needle : needle.toLowerCase();
  const ranges = [];
  let from = 0;
  while (from <= hay.length - n.length) {
    const found = hay.indexOf(n, from);
    if (found === -1) break;
    ranges.push([found, found + n.length]);
    from = found + n.length; // non-overlapping: resume AFTER this match
  }
  return ranges;
}

/**
 * Windows a long line down to MAX_LINE_DISPLAY characters centered on
 * its first match, with an ellipsis on whichever side got cut, and
 * re-maps every match range to the trimmed text's own coordinates.
 * Matches that fall entirely outside the window are dropped from the
 * returned list (still counted by the caller's own matchCount, so nothing
 * about "how many matches" is lost — only how many of them can be
 * highlighted in this one display line).
 *
 * @param {string} line
 * @param {[number, number][]} ranges - non-empty
 * @returns {{text: string, ranges: [number, number][]}}
 */
export function trimLineForDisplay(line, ranges) {
  if (line.length <= MAX_LINE_DISPLAY) return { text: line, ranges };

  const [firstStart, firstEnd] = ranges[0];
  const pad = Math.floor((MAX_LINE_DISPLAY - (firstEnd - firstStart)) / 2);
  let start = Math.max(0, firstStart - pad);
  let end = Math.min(line.length, start + MAX_LINE_DISPLAY);
  start = Math.max(0, end - MAX_LINE_DISPLAY); // slide the window back if `end` hit the line's end first

  const prefix = start > 0 ? "…" : "";
  const suffix = end < line.length ? "…" : "";
  const text = prefix + line.slice(start, end) + suffix;
  const shift = start - prefix.length;

  const trimmedRanges = [];
  for (const [s, e] of ranges) {
    if (s < start || e > end) continue; // outside the window — dropped, not mis-highlighted
    trimmedRanges.push([s - shift, e - shift]);
  }
  return { text, ranges: trimmedRanges };
}

/**
 * One file's matching lines, most-matching-first order untouched (file
 * order, top to bottom — that's what "find in files" results read as).
 * `\r\n`/`\r` are normalised to `\n` first (same reasoning as
 * editorUtils.normalizeLineBreaks: a CRLF file shouldn't change the
 * line numbers a person sees here vs. in the editor).
 *
 * @param {string} content
 * @param {string} query - already trimmed and non-empty; the caller
 *   (searchProject) handles an empty query
 * @param {{maxMatches?: number, caseSensitive?: boolean}} [opts]
 * @returns {{line: number, column: number, text: string, ranges: [number, number][], matchCount: number}[]}
 */
export function searchFileContent(
  content,
  query,
  { maxMatches = DEFAULT_MAX_MATCHES_PER_FILE, caseSensitive = false } = {}
) {
  const lines = String(content ?? "").replace(/\r\n?/g, "\n").split("\n");
  const results = [];
  for (let i = 0; i < lines.length && results.length < maxMatches; i += 1) {
    const raw = lines[i];
    const ranges = matchesInLine(raw, query, { caseSensitive });
    if (!ranges.length) continue;
    const { text, ranges: displayRanges } = trimLineForDisplay(raw, ranges);
    results.push({
      line: i + 1, // 1-based, matching CodeEditor/CodeMirror's own line numbering
      column: ranges[0][0] + 1, // 1-based column of the first match, for jump-to-result
      text,
      ranges: displayRanges,
      matchCount: ranges.length,
    });
  }
  return results;
}

/**
 * Every file's matches, across a whole project. `fileTexts` is
 * `{[path]: content}` — already-fetched text, one entry per file
 * useProjectSearch.js decided was worth scanning (an open buffer's
 * live `edited` text, or a provider.read() result for a closed one);
 * this function does no fetching and no filtering of WHICH paths to
 * search, only what each one's text contains.
 *
 * Files are walked in sorted path order so the result list is stable
 * and deterministic regardless of `fileTexts`' own key order (a plain
 * object has none worth relying on). Stops early once
 * `maxTotalMatches` match-lines have been collected across every file
 * — `truncated: true` on the return value says the count undersells
 * what's actually there, same "say so rather than silently drop rows"
 * convention as fileTree.js's own truncation points.
 *
 * `matchCount` (here and on `maxTotalMatches`/`maxMatchesPerFile`)
 * counts result ROWS — matching LINES, the unit the panel actually
 * renders and a person actually clicks to jump to — not raw substring
 * occurrences. A line with five hits on it is one row with
 * `matchCount: 5` in searchFileContent()'s own sense of the word, but
 * contributes exactly 1 toward these budgets, same as a line with one
 * hit; a file with a single very-matchy line can't back into a huge
 * row count in one shot the way an over-matchy LINE already can't
 * (that's searchFileContent's own per-line ranges list, unbounded on
 * purpose — see trimLineForDisplay for how a line's DISPLAY stays
 * bounded regardless).
 *
 * @param {Record<string, string>} fileTexts
 * @param {string} query
 * @param {{caseSensitive?: boolean, maxMatchesPerFile?: number, maxTotalMatches?: number}} [opts]
 * @returns {{results: {path: string, matches: object[], truncated: boolean}[], truncated: boolean, matchCount: number}}
 */
export function searchProject(fileTexts, query, opts = {}) {
  const q = String(query || "").trim();
  const maxMatchesPerFile = opts.maxMatchesPerFile ?? DEFAULT_MAX_MATCHES_PER_FILE;
  const maxTotalMatches = opts.maxTotalMatches ?? DEFAULT_MAX_TOTAL_MATCHES;

  if (!q) return { results: [], truncated: false, matchCount: 0 };

  const paths = Object.keys(fileTexts || {}).sort();
  const results = [];
  let matchCount = 0;
  let truncated = false;

  for (const path of paths) {
    if (matchCount >= maxTotalMatches) {
      truncated = true;
      break;
    }
    const remaining = maxTotalMatches - matchCount;
    const matches = searchFileContent(fileTexts[path], q, {
      maxMatches: Math.min(maxMatchesPerFile, remaining),
      caseSensitive: !!opts.caseSensitive,
    });
    if (!matches.length) continue;
    // This file itself was cut short either by its own per-file cap or
    // by running into what's left of the project-wide budget.
    const fileTruncated = matches.length >= maxMatchesPerFile || matches.length >= remaining;
    if (fileTruncated) truncated = true;
    results.push({ path, matches, truncated: fileTruncated });
    matchCount += matches.length;
  }

  return { results, truncated, matchCount };
}
