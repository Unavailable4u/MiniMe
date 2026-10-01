// frontend/app/lib/workbench/wireframeCode.js — W8.6 (Build Workbench
// plan). The frontend half of "Turn this wireframe into code":
// backend POST .../code/proposals/from-wireframe (eo/code_proposals.py's
// create_wireframe_proposal()) files the wireframe's HTML as an ordinary
// PENDING proposal for index.html; this file is the decision logic the
// Wireframes sub-tab needs around that — no React, no fetch, no imports,
// same dependency-free shape as deployProposal.js / instructionStep.js,
// so __tests__/wireframeCode.test.mjs loads the real file under plain
// `node` (an added `import` line would make that test fail loudly).
//
// Three questions it answers:
//
//   1. Is this proposal a wireframe one?  (isWireframeProposal) — the ONLY
//      thing telling it apart from a chat edit is
//      `model_meta.generator === "wireframe_to_code"`; the backend adds no
//      table, status or event for it. The tray and the Editor use it to
//      NOT offer "Regenerate" (there is nothing to regenerate: the edit
//      is the wireframe itself, not a model's reading of an instruction).
//
//   2. Can this wireframe be sent at all?  (wireframeCodeBlocker) — the
//      same emptiness / size rules the backend enforces, checked first so
//      the button can say why it's off instead of bouncing a 400.
//
//   3. What should the Wireframes sub-tab show?  (describeWireframeResponse,
//      pickPendingWireframeProposal, wireframeProposalSummary,
//      wireframeCodeView) — the route returns the stored proposal body,
//      the same one POST .../code/proposals returns.

/** Mirrors backend eo/code_proposals.py's WIREFRAME_CODE_GENERATOR. */
export const WIREFRAME_CODE_GENERATOR = "wireframe_to_code";

/** Mirrors backend eo/code_proposals.py's WIREFRAME_TARGET_PATH. */
export const WIREFRAME_TARGET_PATH = "index.html";

/** Mirrors backend eo/code_proposals.py's _WIREFRAME_MAX_CHARS. */
export const WIREFRAME_MAX_CHARS = 400000;

/** A stored proposal (any shape that carries `model_meta`) filed from a wireframe. */
export function isWireframeProposal(proposal) {
  return proposal?.model_meta?.generator === WIREFRAME_CODE_GENERATOR;
}

/**
 * The first PENDING wireframe proposal in `proposals`, or null. The list
 * endpoint is most-recent-first and a re-send rejects the earlier
 * wireframe proposal, so there is normally at most one; "first" is the
 * newest if a race ever leaves two.
 */
export function pickPendingWireframeProposal(proposals) {
  if (!Array.isArray(proposals)) return null;
  return proposals.find((p) => p?.status === "pending" && isWireframeProposal(p)) || null;
}

/**
 * The small shape the Wireframes sub-tab keeps about a stored proposal:
 * `{id, status, path, label, replacesExisting}`. `path` comes from the file
 * the proposal actually carries (the thing Keep would write), falling back
 * to the tagged metadata; nothing here is trusted to be present.
 */
export function wireframeProposalSummary(proposal) {
  if (!proposal || typeof proposal !== "object" || typeof proposal.id !== "string" || !proposal.id) return null;
  const meta = proposal.model_meta || {};
  const file = Array.isArray(proposal.files) ? proposal.files[0] : null;
  const path = (file && typeof file.path === "string" && file.path) || meta.target_path || WIREFRAME_TARGET_PATH;
  const replacesExisting = file ? file.op === "replace" : meta.replaces_existing === true;
  return {
    id: proposal.id,
    status: typeof proposal.status === "string" ? proposal.status : "pending",
    path,
    label: typeof meta.screen_label === "string" ? meta.screen_label : "",
    replacesExisting,
  };
}

/**
 * Why this wireframe can't be turned into code right now, or null when it
 * can. Same rules as the backend's _wireframe_source() (empty / no markup /
 * too long) — the backend stays the authority; this only saves a round trip
 * and gives the button a reason.
 *
 * @param {string} html - the wireframe as shown in the preview
 * @returns {string | null}
 */
export function wireframeCodeBlocker(html) {
  const text = typeof html === "string" ? html.trim() : "";
  if (!text) return "Add a wireframe first.";
  if (!text.includes("<")) return "This doesn't look like HTML yet.";
  if (text.length > WIREFRAME_MAX_CHARS) return "This wireframe is too large to turn into code.";
  return null;
}

/**
 * Reads the from-wireframe route's body (a stored proposal).
 *
 * @param {any} res
 * @returns {{ok: true, proposal: object} | {ok: false, message: string}}
 *   `ok` only for a PENDING proposal with an id — the one thing the
 *   Review button can open. A `failed` row (the store keeps those so a
 *   badge has something to attach to) says why, from its own summary.
 */
export function describeWireframeResponse(res) {
  const summary = wireframeProposalSummary(res);
  if (!summary) return { ok: false, message: "Couldn't file this wireframe for review." };
  if (summary.status !== "pending") {
    const why = typeof res.summary === "string" && res.summary ? res.summary : "the proposal wasn't created";
    return { ok: false, message: `Couldn't turn this wireframe into code: ${why}` };
  }
  return { ok: true, proposal: summary };
}

/**
 * What the Wireframes sub-tab's code row shows.
 *
 * @param {{pending: ReturnType<typeof wireframeProposalSummary>, blocker?: string|null}} input
 * @returns {{kind: "idle"|"review"|"blocked", message: string, notice: string|null}}
 *   `review` is a proposal waiting for Keep/Undo (the row then offers
 *   "Review index.html"); `notice` is the one thing worth warning about
 *   before opening it — that Keep would replace an index.html that
 *   already exists.
 */
export function wireframeCodeView({ pending, blocker }) {
  if (pending) {
    const from = pending.label ? ` from “${pending.label}”` : "";
    return {
      kind: "review",
      message: `${pending.path}${from} is ready for your review — nothing is added to this project until you press Keep.`,
      notice: pending.replacesExisting
        ? `This replaces the ${pending.path} already in this project — check the diff before you Keep.`
        : null,
    };
  }
  if (blocker) return { kind: "blocked", message: blocker, notice: null };
  return {
    kind: "idle",
    message: `Turn this wireframe into the project's ${WIREFRAME_TARGET_PATH}. You'll review it first — nothing is saved until you press Keep.`,
    notice: null,
  };
}
