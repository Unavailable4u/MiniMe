// frontend/app/lib/workbench/editorUtils.js — W2.1 (Build Workbench
// plan). Pure functions shared by CodeEditor.jsx and cmTheme.js. Kept
// free of CM6/React/DOM imports on purpose: everything here is a
// plain string/number in, string/number/array out function, which is
// what lets it be covered by a dependency-free `.mjs` test run with
// plain `node` — this repo's own convention for frontend tests (see
// `components/__tests__/wiringGraph.linkFilter.test.mjs`), rather than
// needing a browser or a JS test runner neither of which this repo
// currently has.

/**
 * Collapses "\r\n" and lone "\r" to "\n". CM6's own Text implementation
 * always does this internally and only ever hands back "\n" — see
 * https://codemirror.net/docs/ref/#state.Text (Text.lineBreak) — so a
 * value from outside CodeEditor.jsx (a CRLF file straight off disk,
 * for instance) has to be normalised before it's compared against or
 * diffed with `view.state.doc.toString()`, or every line ending would
 * look like a change. CodeEditor.jsx normalises `value`; it does NOT
 * normalise on the way out through `onChange`, since CM6 already
 * guarantees LF-only text there.
 */
export function normalizeLineBreaks(text) {
  return text.replace(/\r\n|\r/g, "\n");
}

/**
 * The minimal single-range replace that turns `current` into `next`,
 * as a CM6 `{from, to, insert}` change spec. Diffing the FULL document
 * on every external update (the naive `{from: 0, to: current.length,
 * insert: next}`) would map every existing position to the start of
 * the change, which is what actually throws the cursor/selection/folds
 * to line 1 on every server refresh — see CodeEditor.jsx's own header
 * comment on why an external update has to be a normal, minimal
 * transaction rather than a full-doc replace.
 *
 * This is intentionally NOT a general-purpose diff (no LCS, no
 * mid-document hunks) — just the common-prefix / common-suffix trim
 * around the one span that changed. That's sufficient for this
 * component's actual external-update sources (a server reload, a
 * history Restore, a CRLF normalisation pass): they replace "the
 * document" wholesale, but the part that's actually different from
 * what's on screen is usually a single contiguous edit (one line
 * changed, one line appended/prepended). A real multi-hunk diff would
 * serve external updates with several unrelated changes scattered
 * through the file slightly better (each hunk could keep its own
 * nearby selection untouched), but nothing in this component's own
 * call sites produces that shape today, so it isn't worth the extra
 * complexity/dependency (e.g. `diff-match-patch`) until it is.
 */
export function minimalChange(current, next) {
  const minLen = Math.min(current.length, next.length);

  let start = 0;
  while (start < minLen && current[start] === next[start]) start += 1;

  let endCurrent = current.length;
  let endNext = next.length;
  while (
    endCurrent > start &&
    endNext > start &&
    current[endCurrent - 1] === next[endNext - 1]
  ) {
    endCurrent -= 1;
    endNext -= 1;
  }

  return { from: start, to: endCurrent, insert: next.slice(start, endNext) };
}

// Fallback when a file has no indentation evidence at all (an empty
// file, or one with only top-level statements) — 2 spaces, the most
// common default across editors/languages.
const DEFAULT_INDENT_UNIT = "  ";

/**
 * Guesses a file's indent unit (N spaces, or a tab) from its own text,
 * so `indentUnit` (and therefore Tab / auto-indent-on-Enter) matches
 * what's already in the file instead of forcing every file to the
 * same width — see CodeEditor.jsx's header comment ("a fixed unit
 * would make auto-indent insert 2 spaces into a 4-space Python file").
 *
 * Heuristic: a tab anywhere at the start of a line wins outright (a
 * file either uses tabs for indentation or it doesn't — there's no
 * "how many tabs is one level" question the way there is for spaces).
 * Otherwise, look at every place indentation increases from one
 * non-blank line to the next and take the most common step size; most
 * hand-written and formatter-produced code indents by a constant
 * number of spaces per nesting level, so the most frequent increase
 * IS that constant. Ties favour the smaller step, which is the safer
 * side to be wrong on (an 8-space file mis-detected as 4 still nests
 * consistently; a 2-space file mis-detected as 8 puts a whole
 * indent-unit-sized gap under a single level).
 */
export function detectIndentUnit(text) {
  if (!text) return DEFAULT_INDENT_UNIT;

  const lines = text.split("\n");
  for (const line of lines) {
    if (line[0] === "\t") return "\t";
  }

  let prevIndent = 0;
  const stepCounts = new Map();
  for (const line of lines) {
    if (line.trim().length === 0) {
      // A blank line carries no indentation evidence and, unlike a
      // dedent, shouldn't reset `prevIndent` — the next real line's
      // indent is still compared against the last non-blank one.
      continue;
    }
    const leading = line.match(/^ */)[0].length;
    if (leading > prevIndent) {
      const step = leading - prevIndent;
      stepCounts.set(step, (stepCounts.get(step) || 0) + 1);
    }
    prevIndent = leading;
  }

  if (stepCounts.size === 0) return DEFAULT_INDENT_UNIT;

  let bestStep = null;
  let bestCount = -1;
  for (const [step, count] of stepCounts) {
    if (count > bestCount || (count === bestCount && step < bestStep)) {
      bestStep = step;
      bestCount = count;
    }
  }
  return " ".repeat(bestStep);
}

/**
 * Character offsets, within one line's text, that should carry an
 * indent-guide mark — one offset per COMPLETED indent level, at the
 * first character of that level's chunk of leading whitespace. Split
 * out from the ViewPlugin in CodeEditor.jsx (see `computeIndentGuideDecorations`
 * there) purely so this — the actual "which columns get a line"
 * decision — can be unit-tested with plain strings, no EditorView
 * required.
 *
 * A line only gets guides if it has content after its indentation:
 * blank/whitespace-only lines are visual noise for this (nothing to
 * the right of a guide on an empty line), and — a level only counts
 * once it's *complete* — a line indented 3 spaces under a 2-space unit
 * gets exactly one guide (at offset 0), not a partial second one at
 * offset 2, since there's no full second unit's worth of whitespace to
 * draw it under.
 */
export function indentGuideOffsets(lineText, unit) {
  const unitLen = unit ? unit.length : 0;
  if (unitLen === 0) return [];

  const leading = lineText.match(/^[ \t]*/)[0].length;
  if (leading < unitLen) return [];
  if (lineText.slice(leading).trim().length === 0) return [];

  const levels = Math.floor(leading / unitLen);
  const offsets = [];
  for (let level = 1; level <= levels; level += 1) {
    offsets.push((level - 1) * unitLen);
  }
  return offsets;
}

function parseColor(color) {
  const value = (color || "").trim();

  const hex = value.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    let h = hex[1];
    if (h.length === 3) h = [...h].map((c) => c + c).join("");
    const num = parseInt(h, 16);
    return { r: (num >> 16) & 255, g: (num >> 8) & 255, b: num & 255 };
  }

  // getComputedStyle() on a *real* CSS property (as opposed to reading
  // a custom property's raw string, which is what cssVar() in
  // cmTheme.js does) would report rgb()/rgba() — not something either
  // cssVar() or FALLBACK ever hands this function today, but handled
  // here rather than silently producing black if that ever changes.
  const rgb = value.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)/i);
  if (rgb) {
    return { r: Number(rgb[1]), g: Number(rgb[2]), b: Number(rgb[3]) };
  }

  return { r: 0, g: 0, b: 0 };
}

/**
 * Returns `color` (a "#rgb"/"#rrggbb" hex string — what every
 * `--neutral-*`/`--accent`/`--cyber-*` token in globals.css is
 * authored as) blended to the given alpha, as an `rgba(...)` string
 * CM6's `EditorView.theme()` can use directly. This is what lets
 * cmTheme.js's translucent overlays (selection, active line, search
 * matches) derive from the SAME tokens every solid color does, instead
 * of a second set of hand-copied `rgba(...)` literals that would
 * silently drift out of sync if a token's hex value ever changed —
 * see cmTheme.js's own header comment.
 */
export function withAlpha(color, alpha) {
  const { r, g, b } = parseColor(color);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
