// frontend/app/lib/preview/mmRange.js — W6.5 (Build Workbench plan).
// Pure helpers around the `data-mm` contract instrument.js (W6.3)
// stamps and inspectorRuntime.js (W6.4) reports back:
//
//   data-mm="<path>:<startLine>:<startCol>:<endLine>:<endCol>"
//   lines 1-based, columns 0-based, end EXCLUSIVE (instrument.js's own
//   header is the source of truth for that — nothing here re-derives it).
//
// No imports on purpose (same "dependency-free family" as fileTree.js /
// tabUtils.js): everything W6.5 needs to DECIDE — which range contains
// a cursor, where a range starts and ends in a document — is string and
// integer math, so it stays testable with plain `node`. The parts that
// need a live CodeMirror view (selecting the range, pulsing it) live in
// gotoPosition.js / pulse.js instead.

const MM_RE = /^(.*):(\d+):(\d+):(\d+):(\d+)$/;

/**
 * Parses from the RIGHT, so a path that itself contains ":" (a Windows
 * drive letter from a local folder, say) still splits correctly — the
 * last four colon-separated fields are always the numbers.
 *
 * @param {string} mm
 * @returns {{mm: string, path: string, startLine: number, startCol: number, endLine: number, endCol: number} | null}
 */
export function parseMm(mm) {
  if (typeof mm !== "string") return null;
  const m = MM_RE.exec(mm);
  if (!m || !m[1]) return null;
  return {
    mm,
    path: m[1],
    startLine: Number(m[2]),
    startCol: Number(m[3]),
    endLine: Number(m[4]),
    endCol: Number(m[5]),
  };
}

/**
 * Every data-mm value in an instrumented document, parsed. parse5's
 * serializer escapes `&` and `"` inside an attribute value, so those
 * two are decoded back before parsing (a path containing either is
 * unusual, but decoding is cheap and a wrong path here would make the
 * reverse highlight silently never match).
 *
 * A regex over the serialized HTML rather than a second parse5 pass:
 * bundleStatic() already produced this string, the attribute is
 * machine-written in one fixed shape (` data-mm="..."`), and this runs
 * once per build, not per keystroke.
 *
 * @param {string} html
 * @returns {ReturnType<typeof parseMm>[]}
 */
export function extractMmRanges(html) {
  if (typeof html !== "string") return [];
  const out = [];
  const re = /\sdata-mm="([^"]*)"/g;
  let m;
  while ((m = re.exec(html))) {
    const parsed = parseMm(m[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&"));
    if (parsed) out.push(parsed);
  }
  return out;
}

function cmp(lineA, colA, lineB, colB) {
  return lineA !== lineB ? lineA - lineB : colA - colB;
}

/**
 * The INNERMOST range in `ranges` (same file only) containing the
 * cursor — "the element you're editing", per the plan. Containment is
 * start <= cursor < end (end exclusive, matching the contract).
 * Innermost = the latest start; a tie (same start, e.g. a wrapper
 * whose first child begins at the very same position can't happen for
 * real elements, but the tiebreak keeps this total) goes to the
 * smaller end.
 *
 * @param {ReturnType<typeof parseMm>[]} ranges
 * @param {string|null|undefined} path - the file the cursor is in; ranges from other files never match
 * @param {number} line - 1-based
 * @param {number} col - 0-based (CodeEditor's cursor report is 1-based; the caller subtracts 1)
 * @returns {string|null} the winning range's mm string
 */
export function innermostMmAt(ranges, path, line, col) {
  if (!ranges || !path || !Number.isFinite(line) || !Number.isFinite(col)) return null;
  let best = null;
  for (const r of ranges) {
    if (r.path !== path) continue;
    if (cmp(line, col, r.startLine, r.startCol) < 0) continue;
    if (cmp(line, col, r.endLine, r.endCol) >= 0) continue;
    if (
      !best ||
      cmp(r.startLine, r.startCol, best.startLine, best.startCol) > 0 ||
      (cmp(r.startLine, r.startCol, best.startLine, best.startCol) === 0 &&
        cmp(r.endLine, r.endCol, best.endLine, best.endCol) < 0)
    ) {
      best = r;
    }
  }
  return best ? best.mm : null;
}

/**
 * Document offset of a (1-based line, 0-based col) position, clamped to
 * the line's own length and to the document — a position from a build
 * a keystroke or two stale must degrade to "close enough", never throw
 * or return an offset outside the text.
 *
 * @param {string} text
 * @param {number} line
 * @param {number} col
 * @returns {number}
 */
export function offsetOf(text, line, col) {
  let start = 0;
  for (let l = 1; l < line; l++) {
    const nl = text.indexOf("\n", start);
    if (nl === -1) return text.length;
    start = nl + 1;
  }
  const nl = text.indexOf("\n", start);
  const lineEnd = nl === -1 ? text.length : nl;
  return Math.min(start + Math.max(0, col), lineEnd);
}

/**
 * The exact text (and offsets) an mm range covers in `text`.
 *
 * @param {string} text
 * @param {NonNullable<ReturnType<typeof parseMm>>} parsed
 * @returns {{from: number, to: number, snippet: string}}
 */
export function rangeFromMm(text, parsed) {
  const from = offsetOf(text, parsed.startLine, parsed.startCol);
  const to = Math.max(from, offsetOf(text, parsed.endLine, parsed.endCol));
  return { from, to, snippet: text.slice(from, to) };
}
