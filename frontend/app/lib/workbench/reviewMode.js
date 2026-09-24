// frontend/app/lib/workbench/reviewMode.js — W5.3 (Build Workbench plan).
// The pure decision logic behind the Copilot-style review UI: given a
// stored proposal and what the person did with each hunk, work out what
// to send to POST .../code/proposals/{id}/resolve.
//
// Split out of the components for the same reason tabUtils.js is: the
// interesting parts here are "given this state, what should happen"
// rules — is this file kept or undone, is the review finished, which
// open buffers would a Keep collide with — and a rule is far easier to
// get right, and keep right, as a function with a test than tangled
// into an effect. No imports — see fileTree.js's header; the test
// loader in __tests__/reviewMode.test.mjs enforces it.
//
// The one CodeMirror-shaped thing in here, chunkLineStats(), takes the
// two documents as plain objects with a `lineAt(pos).number` method —
// which is exactly the subset of CM6's `Text` it uses — so it can be
// tested against a small stand-in instead of needing CodeMirror
// installed.

// CM6 always splits on \r\n, \r and \n alike and reports "\n" back, so
// whatever text comes out of a review editor is LF-only even for a
// CRLF file. Comparing it with the server's text has to ignore that, or
// an untouched CRLF file would look "changed" at every line end. Same
// rule as editorUtils.normalizeLineBreaks (duplicated: this module
// stays import-free).
function eol(text) {
  return (text ?? "").replace(/\r\n|\r/g, "\n");
}

/**
 * Lines added / removed by a set of merge chunks — what the file
 * list's `+12 −3` badge shows. `chunks` are @codemirror/merge `Chunk`s
 * (fromA/toA/endA against the ORIGINAL text, fromB/toB/endB against the
 * text being edited); a side with `from === to` covers no lines. Counts
 * whole lines on each side, not characters: a one-word edit inside a
 * line is one line added and one removed, the same as `git diff --stat`.
 *
 * @param {{fromA:number,toA:number,endA:number,fromB:number,toB:number,endB:number}[]} chunks
 * @param {{lineAt:(pos:number)=>{number:number}}} originalDoc
 * @param {{lineAt:(pos:number)=>{number:number}}} doc
 * @returns {{added:number, removed:number}}
 */
export function chunkLineStats(chunks, originalDoc, doc) {
  let added = 0;
  let removed = 0;
  for (const ch of chunks || []) {
    if (ch.fromA < ch.toA) removed += originalDoc.lineAt(ch.endA).number - originalDoc.lineAt(ch.fromA).number + 1;
    if (ch.fromB < ch.toB) added += doc.lineAt(ch.endB).number - doc.lineAt(ch.fromB).number + 1;
  }
  return { added, removed };
}

/**
 * The review slice's `files` map + display order, built from a proposal
 * as GET .../code/proposals/{id} returns it. `remaining` starts null
 * — "the editor hasn't reported yet" — so a review can't look finished
 * before its merge views have even mounted (see reviewProgress()).
 * `current` starts as the proposed text, which is what an editor shows
 * before any hunk is decided.
 *
 * @param {{files?: {path:string, op?:string, base_version?:number, original?:string, proposed?:string}[]}} proposal
 * @returns {{order: string[], files: Record<string, object>}}
 */
export function reviewFilesFromProposal(proposal) {
  const order = [];
  const files = {};
  for (const f of proposal?.files || []) {
    if (!f || !f.path || f.path in files) continue;
    order.push(f.path);
    files[f.path] = {
      op: f.op || "replace",
      baseVersion: f.base_version ?? 0,
      original: f.original ?? "",
      proposed: f.proposed ?? "",
      current: f.proposed ?? "",
      remaining: null,
      added: 0,
      removed: 0,
    };
  }
  return { order, files };
}

/**
 * One file's resolve() decision, from what's in its review editor now.
 *
 * The server's vocabulary is only keep | undo (see
 * eo/code_proposals.py's _VALID_DECISIONS) — a mix of kept and undone
 * hunks inside one file is expressed as "keep" with the merged text as
 * `finalContent`. So:
 *
 *   - Nothing of the proposal survived (the final text is the original
 *     text again) → "undo": there is nothing to write.
 *   - The whole proposal survived → "keep" with NO finalContent, so the
 *     server writes the proposal's own stored text byte for byte (which
 *     matters for a CRLF file: the editor's copy is LF-only).
 *   - Anything in between → "keep" with the merged text.
 *
 * `delete` is special: the server's delete ignores finalContent
 * entirely, so a delete where some lines were brought back by undoing
 * hunks must NOT be sent as "keep" — that would delete the whole file
 * regardless. It is kept only when nothing was brought back.
 *
 * @param {{op:string, original:string, proposed:string, final:string}} f
 * @returns {{decision:"keep"|"undo", finalContent:string|null}}
 */
export function decisionForFile({ op, original, proposed, final }) {
  const fin = eol(final);
  const orig = eol(original);
  const prop = eol(proposed);

  if (op === "delete") {
    return fin === prop ? { decision: "keep", finalContent: null } : { decision: "undo", finalContent: null };
  }
  if (fin === orig) return { decision: "undo", finalContent: null };
  if (fin === prop) return { decision: "keep", finalContent: null };
  return { decision: "keep", finalContent: final };
}

/**
 * The `decisions` array resolveProposal() (codeProposals.js) takes,
 * for every file in the review.
 *
 * @param {{order: string[], files: Record<string, object>}} review
 * @returns {{path:string, decision:"keep"|"undo", finalContent:string|null}[]}
 */
export function buildDecisions(review) {
  return (review?.order || []).map((path) => {
    const f = review.files[path];
    const { decision, finalContent } = decisionForFile({
      op: f.op,
      original: f.original,
      proposed: f.proposed,
      final: f.current,
    });
    return { path, decision, finalContent };
  });
}

/**
 * How far along a review is. `ready` is false until every file's editor
 * has reported at least once (`remaining` is null before that);
 * `allResolved` is the "Done" gate — every hunk in every file has been
 * kept or undone. Unresolved hunks are deliberately NOT treated as
 * kept: silently applying changes nobody looked at is the thing a
 * review exists to prevent, so Done waits, and Keep all / Undo all are
 * the one-click way to settle the rest.
 *
 * @param {{order: string[], files: Record<string, {remaining:number|null, added:number, removed:number}>}|null} review
 * @returns {{ready:boolean, remaining:number, filesLeft:number, allResolved:boolean}}
 */
export function reviewProgress(review) {
  if (!review) return { ready: false, remaining: 0, filesLeft: 0, allResolved: false };
  let ready = true;
  let remaining = 0;
  let filesLeft = 0;
  for (const path of review.order) {
    const f = review.files[path];
    if (!f || f.remaining == null) {
      ready = false;
      continue;
    }
    remaining += f.remaining;
    if (f.remaining > 0) filesLeft += 1;
  }
  return { ready, remaining, filesLeft, allResolved: ready && remaining === 0 };
}

/**
 * Files in the review whose open editor buffer has unsaved edits. The
 * proposal was made against the SAVED text on the server, not the
 * buffer, so keeping it moves the server version on and the buffer
 * comes back flagged "changed on the server" — worth saying before it
 * happens.
 *
 * @param {{order: string[]}|null} review
 * @param {Record<string, {dirty?: boolean}>} buffers
 * @returns {string[]}
 */
export function dirtyOverlap(review, buffers) {
  if (!review) return [];
  return review.order.filter((p) => !!buffers?.[p]?.dirty);
}

/**
 * The one-line reason a proposal can't be opened for review, or null
 * when it can. Everything except `pending` has already been decided
 * (or never produced anything to decide on).
 *
 * @param {{status?: string, files?: unknown[]}} proposal
 * @returns {string|null}
 */
export function unreviewableReason(proposal) {
  const status = proposal?.status;
  if (status === "pending") {
    return (proposal.files || []).length === 0 ? "This edit doesn't change any files." : null;
  }
  if (status === "accepted") return "This edit was already applied.";
  if (status === "rejected") return "This edit was already discarded.";
  if (status === "partial") return "This edit was already partly applied.";
  if (status === "stale") return "This edit is out of date — a file changed after it was proposed. Ask for it again.";
  if (status === "failed") return "That edit couldn't be generated. Try asking again.";
  return "This edit can't be opened for review.";
}

/**
 * The confirmation shown after a successful resolve, by final status.
 *
 * @param {string} status - accepted | rejected | partial
 * @returns {string}
 */
export function resolvedMessage(status) {
  if (status === "accepted") return "Applied the AI edit.";
  if (status === "partial") return "Applied part of the AI edit.";
  if (status === "rejected") return "Discarded the AI edit — nothing was changed.";
  return "Finished reviewing the AI edit.";
}
