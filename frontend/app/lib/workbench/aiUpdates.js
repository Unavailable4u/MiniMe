// frontend/app/lib/workbench/aiUpdates.js — W8.2 (Build Workbench plan).
// Pure decision logic for "AI updated 4 files": what the toast says
// when a chat code-generation run (the pipeline's write-back) rewrites
// files that are open in the editor. No imports — see fileTree.js's
// header; the test loader in __tests__/aiUpdates.test.mjs enforces it.
//
// WHAT THIS ADDS, AND WHAT IT DOESN'T. The W0.1 policy already holds
// and is not changed here: tabUtils.js's planBufferSync() reloads a
// CLEAN open buffer when the server's version moves and flags a DIRTY
// one `stale` instead of replacing it. What was missing is that none of
// it is announced. A clean file just changes under you, and a dirty
// background tab gets a `stale` flag whose only visible sign is a
// banner on that tab — which you only see if you click it. This module
// turns one pipeline write into a short list the person can act on:
// which open files changed, whether their edits were kept, and what
// the change is.
//
// WHICH WRITES COUNT. Only the pipeline's. The backend tags
// code_file_updated with the emitting agent (see relay/emitter.py's
// emit_workspace_event(): the envelope carries `agent`):
//   "code_writers" — api/task_runner.py's _write_code_files(), the
//                    chat run's write-back. This is the one that
//                    arrives unannounced, so it is the one announced.
//   "code_editor"  — eo/code_proposals.py's resolve_proposal(). The
//                    person just pressed Keep on that; a toast
//                    saying "AI updated your file" would be noise.
// A person's own Save doesn't emit the event at all.
//
// THE EVENT SHAPE (easy to get wrong): the Pusher payload is the whole
// envelope `{type, workspace_id, agent, timestamp, payload: {file_path,
// file_paths, workspace_id}}`, so the paths are at `data.payload`, not
// `data` — PendingActionBar.jsx and TerminalPanel.jsx already read
// `data.payload`. extractFileUpdate() reads there and falls back to
// the root so a flattened shape would still work.

/** `agent` on code_file_updated for a chat run's write-back. */
export const PIPELINE_AGENT = "code_writers";

/**
 * Most files one toast will fetch and list. A run that rewrites more
 * open files than this is rare, and every listed file costs one read
 * to build its diff, so the rest are left to the normal per-tab flow
 * (reload when clean, stale banner when dirty) without a toast row.
 */
export const MAX_AI_UPDATE_FILES = 20;

/**
 * @param {string|null|undefined} agent - the envelope's `agent`
 * @returns {boolean}
 */
export function isPipelineWrite(agent) {
  return agent === PIPELINE_AGENT;
}

/**
 * Pulls the changed paths and the emitting agent out of a
 * code_file_updated Pusher message.
 *
 * @param {any} data - what bind_global's handler receives as `data`
 * @returns {{filePaths: string[], agent: string|null}}
 */
export function extractFileUpdate(data) {
  const body = data && typeof data === "object" ? data : {};
  const payload = body.payload && typeof body.payload === "object" ? body.payload : body;
  const listed = Array.isArray(payload.file_paths) ? payload.file_paths : [];
  const raw = listed.length ? listed : payload.file_path ? [payload.file_path] : [];
  const filePaths = [];
  for (const p of raw) {
    if (typeof p === "string" && p && !filePaths.includes(p)) filePaths.push(p);
  }
  return { filePaths, agent: typeof body.agent === "string" ? body.agent : null };
}

/**
 * What the open buffers held at the moment the event arrived — taken
 * BEFORE the refresh that reloads them, because afterwards the old
 * text is gone. Only paths that are open in a tab with a loaded buffer
 * are included; an unopened file has nothing to compare against.
 *
 * @param {object} args
 * @param {string[]} args.paths - the event's changed paths
 * @param {string[]} args.tabs
 * @param {{[path: string]: {edited?: string, saved?: string}}} args.buffers
 * @returns {{path: string, before: string, saved: string}[]}
 *   `before` is the text in the editor (your unsaved edits, if any);
 *   `saved` is the text those edits were based on.
 */
export function planAiUpdateBaselines({ paths, tabs, buffers }) {
  const open = new Set(tabs);
  const out = [];
  for (const path of paths) {
    if (out.length >= MAX_AI_UPDATE_FILES) break;
    if (!open.has(path) || out.some((b) => b.path === path)) continue;
    const buffer = buffers[path];
    if (!buffer || typeof buffer.edited !== "string") continue;
    out.push({
      path,
      before: buffer.edited,
      saved: typeof buffer.saved === "string" ? buffer.saved : buffer.edited,
    });
  }
  return out;
}

/**
 * Joins the baselines with what the server holds now. A file drops out
 * when the run left it unchanged from the buffer's point of view:
 *  - the read failed (`fetched[path]` is null/absent) — nothing to
 *    show, and the normal per-tab flow still handled the file;
 *  - the server text equals `saved` — the run rewrote the file with
 *    the text it already had (the version still bumps, which is why
 *    planBufferSync() would reload it), so nothing you had changed;
 *  - the server text equals `before` — your own edits already match.
 *
 * @param {{path: string, before: string, saved: string}[]} baselines
 * @param {{[path: string]: {content?: string, version?: number}|null}} fetched
 * @returns {{path: string, before: string, theirs: string, version: number|null}[]}
 */
export function buildAiUpdateEntries(baselines, fetched) {
  const out = [];
  for (const b of baselines) {
    const file = fetched[b.path];
    if (!file || typeof file.content !== "string") continue;
    if (file.content === b.saved || file.content === b.before) continue;
    out.push({
      path: b.path,
      before: b.before,
      theirs: file.content,
      version: typeof file.version === "number" ? file.version : null,
    });
  }
  return out;
}

/**
 * Folds a new run's entries into what's already showing, so two runs
 * in quick succession read as one list. A path already listed keeps
 * its ORIGINAL `before` and takes the newest `theirs`: the diff then
 * answers "what changed since you last looked", not just "what the
 * latest run did on top of the one before".
 *
 * @param {{path: string, before: string, theirs: string, version: number|null}[]} prev
 * @param {{path: string, before: string, theirs: string, version: number|null}[]} incoming
 */
export function mergeAiUpdates(prev, incoming) {
  const byPath = new Map(prev.map((e) => [e.path, e]));
  for (const entry of incoming) {
    const old = byPath.get(entry.path);
    byPath.set(entry.path, old ? { ...entry, before: old.before } : entry);
  }
  return [...byPath.values()];
}

/**
 * One entry, read against the buffer as it is NOW. State comes from
 * the live buffer rather than being stored, so it can't go out of
 * date: the person typing into a just-reloaded file, or resolving the
 * stale banner by hand, is reflected without any bookkeeping.
 *
 *   "kept-yours" — the buffer is `stale` or `dirty`: its unsaved edits
 *                  are still there and the AI's version was NOT
 *                  applied. `base` is your text.
 *   "reloaded"   — the buffer is clean, so planBufferSync() replaced
 *                  it with the AI's version. `base` is what you had.
 *
 * Returns null when there's nothing left to show: the tab was closed,
 * or the two sides now hold the same text.
 *
 * @param {{path: string, before: string, theirs: string, version: number|null}} entry
 * @param {{stale?: boolean, dirty?: boolean, edited?: string}|undefined} buffer
 * @returns {{path: string, state: "kept-yours"|"reloaded", base: string, theirs: string, version: number|null}|null}
 */
export function describeAiUpdate(entry, buffer) {
  if (!buffer || typeof buffer.edited !== "string") return null;
  const keptYours = !!(buffer.stale || buffer.dirty);
  const base = keptYours ? buffer.edited : entry.before;
  if (base === entry.theirs) return null;
  return {
    path: entry.path,
    state: keptYours ? "kept-yours" : "reloaded",
    base,
    theirs: entry.theirs,
    version: entry.version,
  };
}

/**
 * @param {{path: string, before: string, theirs: string, version: number|null}[]} entries
 * @param {{[path: string]: any}} buffers
 * @returns {NonNullable<ReturnType<typeof describeAiUpdate>>[]}
 */
export function visibleAiUpdates(entries, buffers) {
  const out = [];
  for (const entry of entries) {
    const item = describeAiUpdate(entry, buffers[entry.path]);
    if (item) out.push(item);
  }
  return out;
}

/** @param {number} count */
export function aiUpdateTitle(count) {
  return `AI updated ${count} ${count === 1 ? "file" : "files"}`;
}

/**
 * The toast's second line. Says plainly whether anything of yours was
 * left alone, since "did it overwrite my edits?" is the question a
 * person has the instant a file changes under them.
 *
 * @param {{state: string}[]} items
 */
export function aiUpdateSubtitle(items) {
  const kept = items.filter((i) => i.state === "kept-yours").length;
  if (kept === 0) return "Open files were reloaded with the new version.";
  if (kept === items.length) {
    return kept === 1
      ? "Your unsaved edits were kept — the new version wasn't applied."
      : "Your unsaved edits were kept — the new versions weren't applied.";
  }
  return `${kept} with unsaved edits kept; the rest were reloaded.`;
}
