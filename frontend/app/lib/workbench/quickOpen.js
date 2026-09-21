// frontend/app/lib/workbench/quickOpen.js — W2.6 (Build Workbench
// plan). Pure ranking behind the Quick Open modal
// (components/workbench/QuickOpen.jsx): given the project's file paths
// and whatever's typed so far, which files match and in what order.
//
// No imports on purpose — same reasoning as fileTree.js/tabUtils.js's
// own headers: __tests__/quickOpen.test.mjs loads this file's real
// source with plain `node`, no imports map, so an added `import` fails
// it on purpose.
//
// The scoring is a small, deliberately simple SUBSEQUENCE match (every
// character typed must appear, in order, somewhere in the path) — not
// a fuzzy-search library. It rewards, in rough order of weight: a
// match that lands in the file's own NAME rather than a parent folder,
// a match right at a path-segment/word boundary ("cs" hitting the C
// and S of "components/Sidebar.jsx"), and a run of consecutive
// characters over the same count scattered across the string. That's
// enough to make "eew" find "EditorWorkbench.jsx" ahead of some
// unrelated file that merely contains the same three letters, without
// pulling in a scoring library for a palette of a few hundred paths.

const PATH_SEP = "/";

function isBoundary(ch) {
  return ch === undefined || ch === PATH_SEP || ch === "-" || ch === "_" || ch === "." || ch === " ";
}

/**
 * @param {string} query - already trimmed and non-empty; the caller
 *   (rankQuickOpen) is what handles an empty query
 * @param {string} path
 * @returns {{score: number, indices: number[]}|null} null = no match —
 *   some character in `query`, in order, isn't in `path` at all
 */
export function scorePath(query, path) {
  const q = query.toLowerCase();
  const p = path.toLowerCase();
  const lastSlash = path.lastIndexOf(PATH_SEP);
  const indices = [];
  let score = 0;
  let searchFrom = 0;
  let lastMatch = -1;
  let consecutive = 0;

  for (let qi = 0; qi < q.length; qi += 1) {
    const found = p.indexOf(q[qi], searchFrom);
    if (found === -1) return null;
    indices.push(found);

    if (found === lastMatch + 1) {
      consecutive += 1;
      score += 3 + consecutive; // a longer unbroken run scores progressively better
    } else {
      consecutive = 0;
      score += 1;
    }
    if (isBoundary(p[found - 1])) score += 5;
    if (found > lastSlash) score += 2; // in the basename, not a folder segment

    lastMatch = found;
    searchFrom = found + 1;
  }

  // Tie-breakers once every match above scores identically: a shorter
  // path and an earlier first match both read as "closer" to what was
  // typed ("app.js" over "application/legacy/app.js" for the same
  // query "app").
  score += Math.max(0, 20 - path.length) * 0.05;
  score -= indices[0] * 0.1;

  return { score, indices };
}

/**
 * Ranks every path that matches `query`, best first. A "/" typed into
 * the query ("lib/work" narrowing to workbench files) is matched as an
 * ordinary character by scorePath() above — which already does the
 * right thing, since it can only line up with a real "/" in a
 * matching path.
 *
 * @param {string[]} paths
 * @param {string} query
 * @param {{limit?: number}} [opts]
 * @returns {{path: string, score: number, indices: number[]}[]}
 */
export function rankQuickOpen(paths, query, { limit = 50 } = {}) {
  const q = String(query || "").trim();
  if (!q) return [];
  const ranked = [];
  for (const path of paths || []) {
    const m = scorePath(q, path);
    if (m) ranked.push({ path, score: m.score, indices: m.indices });
  }
  ranked.sort((a, b) => b.score - a.score || a.path.length - b.path.length || a.path.localeCompare(b.path));
  return ranked.slice(0, limit);
}

/**
 * What Quick Open shows before anything is typed: `recentPaths` (most-
 * recently-opened first — see QuickOpen.jsx's own header for where
 * that list comes from) filtered down to files that still exist, then
 * every other file so the palette is never empty on a fresh project.
 * Paths not in `recentPaths` keep the project's own order (already
 * alphabetical — see fileTree.js's buildFileTree()) rather than being
 * re-sorted, so that part of the list reads like a normal file listing.
 * Returned in the same `{path, score, indices}` shape as
 * rankQuickOpen() (score 0, no indices — nothing typed yet to
 * highlight) so the caller can render either list identically.
 *
 * @param {string[]} paths - every real file path in the project
 * @param {string[]} recentPaths - most-recently-opened first
 * @param {{limit?: number}} [opts]
 */
export function defaultQuickOpenList(paths, recentPaths, { limit = 50 } = {}) {
  const known = new Set(paths || []);
  const seen = new Set();
  const ordered = [];
  for (const path of recentPaths || []) {
    if (known.has(path) && !seen.has(path)) {
      seen.add(path);
      ordered.push(path);
    }
  }
  for (const path of paths || []) {
    if (!seen.has(path)) {
      seen.add(path);
      ordered.push(path);
    }
  }
  return ordered.slice(0, limit).map((path) => ({ path, score: 0, indices: [] }));
}

/**
 * `indices` (absolute positions in the full path, as scorePath()
 * returns them) that fall inside `path`'s own basename, remapped to
 * be relative to the basename's own start — what QuickOpen.jsx needs
 * to highlight matched characters in the bold filename it shows
 * separately from the dim folder path underneath it.
 *
 * @param {string} path
 * @param {number[]} indices
 * @returns {number[]}
 */
export function basenameMatchIndices(path, indices) {
  const nameStart = path.lastIndexOf(PATH_SEP) + 1;
  return (indices || []).filter((i) => i >= nameStart).map((i) => i - nameStart);
}
