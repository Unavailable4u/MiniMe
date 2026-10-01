// frontend/app/lib/workbench/deployProposal.js — W8.5b (Build Workbench
// plan). The frontend half of "deploy config through the same review":
// backend W8.5a (eo/code_proposals.py's create_deploy_config_proposal(),
// api/routes/deploy.py's /propose) files deploy_config_writer's plan as
// an ordinary PENDING code proposal; this file is the decision logic the
// UI needs around that — no React, no fetch, no imports, same
// dependency-free shape as instructionStep.js / fileTree.js, so
// __tests__/deployProposal.test.mjs loads the real file under plain
// `node` (an added `import` line would make that test fail loudly).
//
// Three questions it answers:
//
//   1. Is this proposal a deploy one?  (isDeployProposal) — the ONLY
//      thing telling it apart from a chat edit is
//      `model_meta.generator === "deploy_config_writer"`; the backend
//      adds no table, status, route or event for it.
//
//   2. What did /propose just do?  (describeProposeResponse,
//      pendingFromProposeResponse, skippedMessage) — the route returns
//      the plan as before plus `proposal: {id, workspace_id, status,
//      path}` when it was filed for review, or `proposal: null` with a
//      `proposal_skipped` reason when it wasn't (`fallback_plan`,
//      `no_workspace`, or the plan's own problem as free text). A skip
//      is a normal 200, never an error.
//
//   3. What should the Deploy card show, and should "Regenerate" on a
//      deploy proposal re-run the writer?  (deployCardView,
//      regenerateDeployOutcome) — "Regenerate" on a chat edit replays
//      the instruction through the code_editor agent; for a deploy
//      proposal that would have an LLM rewrite the file from a
//      one-line instruction, so it re-runs the deploy writer instead.

/** Mirrors backend eo/code_proposals.py's DEPLOY_CONFIG_GENERATOR. */
export const DEPLOY_CONFIG_GENERATOR = "deploy_config_writer";

/** A stored proposal (any shape that carries `model_meta`) filed by the deploy writer. */
export function isDeployProposal(proposal) {
  return proposal?.model_meta?.generator === DEPLOY_CONFIG_GENERATOR;
}

/**
 * The first PENDING deploy proposal in `proposals`, or null. The list
 * endpoint is most-recent-first and a re-propose rejects the earlier
 * deploy proposal, so there is normally at most one; "first" is the
 * newest if a race ever leaves two.
 */
export function pickPendingDeployProposal(proposals) {
  if (!Array.isArray(proposals)) return null;
  return proposals.find((p) => p?.status === "pending" && isDeployProposal(p)) || null;
}

/**
 * The small shape the Deploy card keeps about a stored proposal:
 * `{id, status, path, platform, reason}`. `path` comes from the file the
 * proposal actually carries (the thing Keep would write), falling back to
 * the tagged metadata; nothing here is trusted to be present.
 */
export function deployProposalSummary(proposal) {
  if (!proposal || typeof proposal !== "object") return null;
  const meta = proposal.model_meta || {};
  const path = proposal.files?.[0]?.path || meta.config_filename || null;
  return {
    id: proposal.id,
    status: proposal.status,
    path,
    platform: meta.platform || null,
    reason: typeof meta.reason === "string" && meta.reason ? meta.reason : null,
  };
}

/**
 * Splits a /propose response into `{proposal, skipped}`:
 *   - `proposal`: `{id, status, path}` when the route filed one, else null;
 *   - `skipped`: the route's `proposal_skipped` reason, else null.
 */
export function describeProposeResponse(res) {
  const p = res?.proposal;
  if (p && typeof p === "object" && p.id) {
    return { proposal: { id: p.id, status: p.status, path: p.path ?? null }, skipped: null };
  }
  const reason = res?.proposal_skipped;
  return { proposal: null, skipped: typeof reason === "string" && reason ? reason : null };
}

/**
 * The Deploy card's `pending` summary straight from a /propose response
 * (so the card can show the Review button the moment Propose returns,
 * before any list round trip). Null unless the route filed a PENDING
 * proposal. platform/reason come from the plan keys the response still
 * carries at the top level.
 */
export function pendingFromProposeResponse(res) {
  const { proposal } = describeProposeResponse(res);
  if (!proposal || proposal.status !== "pending") return null;
  return {
    id: proposal.id,
    status: proposal.status,
    path: proposal.path || res?.config_filename || null,
    platform: typeof res?.platform === "string" && res.platform ? res.platform : null,
    reason: typeof res?.reason === "string" && res.reason ? res.reason : null,
  };
}

const SKIPPED_MESSAGES = {
  fallback_plan:
    "The config writer's output couldn't be read, so there's no real config to review. Try Re-propose.",
  no_workspace:
    "This chat isn't part of a project you own, so the config can't be filed for review here.",
};

/**
 * Plain-language text for a `proposal_skipped` reason. Known codes get
 * fixed wording; anything else is the backend's own description of a
 * problem with the plan ("deploy plan has no config_filename", a path
 * the workspace refuses…), shown as-is after a fixed lead-in. Null for
 * an empty reason.
 */
export function skippedMessage(reason) {
  if (typeof reason !== "string" || !reason) return null;
  if (SKIPPED_MESSAGES[reason]) return SKIPPED_MESSAGES[reason];
  return `The proposed config couldn't be filed for review: ${reason}.`;
}

/**
 * What "Regenerate" on a deploy proposal means for a /propose response.
 *   - `{ok: true, proposalId}` — a new PENDING proposal was filed (the
 *     route has already rejected the one being regenerated, because a
 *     re-propose supersedes earlier deploy proposals);
 *   - `{ok: false, message}` — nothing new was filed (placeholder plan,
 *     no workspace, unusable plan, or a proposal that didn't reach
 *     `pending`). Nothing was rejected in that case, so the original is
 *     still waiting — the message says so.
 */
export function regenerateDeployOutcome(res) {
  const { proposal, skipped } = describeProposeResponse(res);
  if (proposal && proposal.status === "pending") {
    return { ok: true, proposalId: proposal.id };
  }
  const why = proposal
    ? `The new config couldn't be prepared for review (status: ${proposal.status}).`
    : skippedMessage(skipped) || "The config writer didn't produce a new proposal.";
  return { ok: false, message: `${why} The earlier proposal is still waiting for your review.` };
}

/**
 * The Deploy card's view model — what to say and which actions apply —
 * from four independent facts:
 *   plan        the memory-bus plan (`data.deploy_config_plan`) or null
 *   pending     deployProposalSummary()/pendingFromProposeResponse() of the
 *               pending deploy proposal, or null
 *   fileExists  whether the plan's file is already in the project's files
 *               (true/false), or null when unknown / not looked up
 *   skipped     the `proposal_skipped` reason from THIS session's last
 *               /propose, or null
 *
 * `kind`:
 *   "review"   a pending proposal exists -> "Review config" is offered
 *   "no_plan"  nothing proposed yet
 *   "fallback" the writer's placeholder plan (nothing reviewable)
 *   "skipped"  the last propose couldn't be filed (reason in `message`)
 *   "idle"     a real plan, nothing pending (kept, discarded, or never filed)
 * `notice` is a secondary warning shown alongside a "review" card (a
 * re-propose that couldn't replace the pending one).
 * Plan details (`platform`/`path`/`reason`) are null for a placeholder
 * plan: its "render"/"render.yaml" are a stand-in, not a recommendation.
 */
export function deployCardView({ plan, pending, fileExists, skipped } = {}) {
  const skipMsg = skippedMessage(skipped);

  if (pending) {
    return {
      kind: "review",
      platform: pending.platform ?? plan?.platform ?? null,
      path: pending.path ?? plan?.config_filename ?? null,
      reason: pending.reason ?? null,
      message: null,
      notice: skipMsg || (plan?.fallback ? SKIPPED_MESSAGES.fallback_plan : null),
    };
  }

  if (!plan) {
    return {
      kind: "no_plan",
      platform: null,
      path: null,
      reason: null,
      message: "No deploy plan proposed yet for this project.",
      notice: null,
    };
  }

  if (plan.fallback) {
    return {
      kind: "fallback",
      platform: null,
      path: null,
      reason: null,
      message: SKIPPED_MESSAGES.fallback_plan,
      notice: null,
    };
  }

  const path = plan.config_filename ?? null;
  const base = {
    platform: plan.platform ?? null,
    path,
    reason: typeof plan.reason === "string" && plan.reason ? plan.reason : null,
    notice: null,
  };

  if (skipMsg) return { ...base, kind: "skipped", message: skipMsg };

  let message = null;
  if (path && fileExists === true) {
    message = `${path} is in this project's files. Nothing is waiting for review.`;
  } else if (path && fileExists === false) {
    message = `Nothing is waiting for review, and ${path} isn't in this project's files. Re-propose to generate it again.`;
  }
  return { ...base, kind: "idle", message };
}
