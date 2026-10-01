// frontend/app/lib/workbench/aiUndo.js — W8.1 (Build Workbench plan).
// Pure decision logic for "Restore to before AI edit" and for the
// source badges in the History panel. No imports — see fileTree.js's
// header; the test loader in __tests__/aiUndo.test.mjs enforces it.
//
// THE ONE THING TO KNOW (it is easy to get backwards): in
// workspace_code_file_versions, a row's `source` is who/what REPLACED
// that content — not who wrote it. See write_file()'s docstring in
// backend/eo/workspace_code_files.py ("for the row this write
// replaces (not for the new row itself)"). So a history entry with
// source "proposal" is not "an AI edit"; it is the file exactly as it
// was BEFORE an AI edit landed on top of it. That is precisely what
// "restore to before the AI edit" wants, so no new backend route or
// column is needed: the target is already sitting in the history list.
//
//   v1 (you saved it)   ── an accepted proposal replaces it ──▶ v2
//   history row: {version: 1, source: "proposal"}   ← restore THIS
//
// (The live version has no history row until something replaces it, so
// "was the current content written by AI?" is answered by the row for
// version current-1, not by anything on the live row.)

/** `source` values that mean "an AI write replaced this content". */
export const AI_SOURCES = ["proposal", "pipeline"];

/**
 * @param {string|null|undefined} source - a history entry's `source`
 * @returns {boolean}
 */
export function isAiSource(source) {
  return AI_SOURCES.includes(source);
}

const VERSION_BADGES = {
  user: {
    label: "Before your save",
    ai: false,
    title: "This is the file as it was before you saved over it.",
  },
  proposal: {
    label: "Before AI edit",
    ai: true,
    title: "This is the file as it was before an AI edit you kept was applied.",
  },
  pipeline: {
    label: "Before AI run",
    ai: true,
    title: "This is the file as it was before an AI code-generation run rewrote it.",
  },
  restore: {
    label: "Before restore",
    ai: false,
    title: "This is the file as it was before an older version was restored over it.",
  },
};

/**
 * Badge for one history row. Worded as "Before …" on purpose — see the
 * header: the row's `source` names what replaced it, and "AI edit" on
 * its own would read as "this version was written by AI", which is the
 * opposite of what the row holds.
 *
 * @param {string|null|undefined} source
 * @returns {{label: string, ai: boolean, title: string}}
 */
export function versionBadge(source) {
  return (
    VERSION_BADGES[source] || {
      label: "Earlier version",
      ai: false,
      title: "An earlier saved version of this file.",
    }
  );
}

/**
 * Which snapshot "Restore to before AI edit" should bring back, or
 * null when there is nothing to undo.
 *
 * Rule: the NEWEST history entry replaced by an AI write
 * (`isAiSource`) that has not already been undone. "Already undone"
 * is judged by content, because the history rows don't record which
 * version a restore came from: an AI edit counts as undone when any
 * later version of the file — including the live one — holds exactly
 * the text from before it. That one test covers three cases:
 *   - the button disappears right after it's used (the restore
 *     re-creates that text as the live version);
 *   - a second click steps back to the AI edit before it, instead of
 *     offering to jump FORWARD to a text that re-applies the first edit
 *     (v1 -AI-> v2 -AI-> v3, undo twice: v2's pre-AI text is still
 *     different from the live v1 text, but the file has been there
 *     since, so it isn't offered again);
 *   - an AI write that changed nothing, or one you reverted by hand,
 *     isn't offered as something to undo.
 * It can only see the versions History still keeps (the newest 30 per
 * file); rows pruned out of that window are simply not considered.
 *
 * `currentContent` must be the SAVED content (what the server has), not
 * the editor's unsaved text — same reason HistoryPanel's own compare
 * view uses it. Without a string to compare against we can't tell a
 * real undo from a no-op, so there is no target.
 *
 * @param {{version: number, source?: string|null, content: string}[]} history - any order
 * @param {{currentVersion?: number|null, currentContent?: string|null}} current
 * @returns {{entry: object, source: string, laterChanges: number|null}|null}
 *   `laterChanges` = how many versions were saved AFTER the AI edit
 *   (so restoring also drops those from the live file); null when the
 *   current version isn't known.
 */
export function findUndoTarget(history, { currentVersion, currentContent } = {}) {
  if (!Array.isArray(history) || typeof currentContent !== "string") return null;

  const known = history.filter((e) => e && Number.isInteger(e.version) && typeof e.content === "string");
  // Has the file held `text` at any version after `version`?
  const returnedToSince = (version, text) =>
    currentContent === text || known.some((e) => e.version > version && e.content === text);

  let best = null;
  for (const entry of known) {
    if (!isAiSource(entry.source)) continue;
    if (returnedToSince(entry.version, entry.content)) continue;
    if (best === null || entry.version > best.version) best = entry;
  }
  if (best === null) return null;

  // Snapshot N was replaced by the AI write that produced N+1, so
  // anything from N+2 up to the live version came after it.
  const laterChanges = Number.isInteger(currentVersion) ? Math.max(0, currentVersion - best.version - 1) : null;
  return { entry: best, source: best.source, laterChanges };
}

/**
 * Whether the one-click undo should stop and ask first. The restore
 * itself loses nothing on the server (it's an append — the current
 * version stays in History), so the common case — nothing unsaved,
 * the AI edit is the latest change — goes straight through. Asking is
 * for the two cases where the click reaches further than the person
 * may expect: unsaved editor text is discarded, or later saves are
 * rolled back along with the AI edit. Unknown (null) counts as "ask".
 *
 * @param {{dirty?: boolean, laterChanges: number|null}} args
 * @returns {boolean}
 */
export function undoNeedsConfirm({ dirty, laterChanges }) {
  return !!dirty || laterChanges !== 0;
}

/**
 * @param {string} source - "proposal" | "pipeline"
 * @returns {string} button text
 */
export function undoLabel(source) {
  return source === "pipeline" ? "Restore to before AI run" : "Restore to before AI edit";
}

/**
 * "" for none/unknown, else "1 later save" / "3 later saves".
 *
 * @param {number|null} n
 * @returns {string}
 */
export function describeLaterChanges(n) {
  if (!Number.isInteger(n) || n <= 0) return "";
  return n === 1 ? "1 later save" : `${n} later saves`;
}
