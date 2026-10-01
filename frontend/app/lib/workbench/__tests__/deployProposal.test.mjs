// W8.5b (Build Workbench plan) — tests for lib/workbench/deployProposal.js.
// Run: node frontend/app/lib/workbench/__tests__/deployProposal.test.mjs
//
// deployProposal.js is import-free on purpose (see its header), so it
// loads with NO `imports` map — an added `import` line would make this
// test fail loudly.
import { loadSource } from "./loadSource.mjs";

const {
  DEPLOY_CONFIG_GENERATOR,
  isDeployProposal,
  pickPendingDeployProposal,
  deployProposalSummary,
  describeProposeResponse,
  pendingFromProposeResponse,
  skippedMessage,
  regenerateDeployOutcome,
  deployCardView,
} = loadSource("../deployProposal.js");

let failures = 0;
function assertEqual(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures++;
    console.error(`FAIL: ${msg}\n  expected: ${e}\n  actual:   ${a}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

const deployProposal = (over = {}) => ({
  id: "prop_d",
  status: "pending",
  files: [{ path: "render.yaml", op: "create", original: "", proposed: "services:\n" }],
  model_meta: { generator: "deploy_config_writer", platform: "render", config_filename: "render.yaml", reason: "backend detected" },
  ...over,
});
const chatProposal = (over = {}) => ({ id: "prop_c", status: "pending", files: [{ path: "render.yaml" }], model_meta: { generator: "code_editor" }, ...over });

// --- isDeployProposal ----------------------------------------------------------
assertEqual(DEPLOY_CONFIG_GENERATOR, "deploy_config_writer", "the generator tag matches the backend's DEPLOY_CONFIG_GENERATOR");
assert(isDeployProposal(deployProposal()), "a proposal tagged deploy_config_writer is a deploy proposal");
assert(!isDeployProposal(chatProposal()), "a chat-edit proposal is not, even when it touches the same file");
assert(!isDeployProposal({ id: "x" }) && !isDeployProposal({ model_meta: null }) && !isDeployProposal(null) && !isDeployProposal(undefined), "missing model_meta / null / undefined -> not a deploy proposal");

// --- pickPendingDeployProposal ---------------------------------------------------
assertEqual(
  pickPendingDeployProposal([chatProposal(), deployProposal({ id: "d1" }), deployProposal({ id: "d2" })])?.id,
  "d1",
  "the first (newest) pending deploy proposal wins; chat edits are skipped"
);
assertEqual(pickPendingDeployProposal([deployProposal({ status: "accepted" }), chatProposal()]), null, "no PENDING deploy proposal -> null");
assertEqual([pickPendingDeployProposal([]), pickPendingDeployProposal(null), pickPendingDeployProposal(undefined)], [null, null, null], "empty / non-array -> null");

// --- deployProposalSummary -------------------------------------------------------
assertEqual(
  deployProposalSummary(deployProposal()),
  { id: "prop_d", status: "pending", path: "render.yaml", platform: "render", reason: "backend detected" },
  "summary: id/status/path/platform/reason"
);
assertEqual(deployProposalSummary(deployProposal({ files: [], model_meta: { generator: "deploy_config_writer", config_filename: "fly.toml" } })).path, "fly.toml", "no files -> path from the tagged metadata");
assertEqual(deployProposalSummary(deployProposal({ model_meta: { generator: "deploy_config_writer", reason: "" } })).reason, null, "an empty reason -> null");
assertEqual(deployProposalSummary(null), null, "null proposal -> null");

// --- describeProposeResponse / pendingFromProposeResponse -------------------------
const filedRes = {
  platform: "render", config_filename: "render.yaml", config_content: "x", reason: "why",
  proposal: { id: "prop_1", workspace_id: "ws_1", status: "pending", path: "render.yaml" },
};
assertEqual(describeProposeResponse(filedRes), { proposal: { id: "prop_1", status: "pending", path: "render.yaml" }, skipped: null }, "filed: proposal summary, no skip");
assertEqual(
  describeProposeResponse({ proposal: null, proposal_skipped: "no_workspace" }),
  { proposal: null, skipped: "no_workspace" },
  "skipped: no proposal, the reason"
);
assertEqual(describeProposeResponse({ platform: "render" }), { proposal: null, skipped: null }, "an old-shape response (no proposal keys) -> neither");
assertEqual(describeProposeResponse(null), { proposal: null, skipped: null }, "null response -> neither");
assertEqual(
  pendingFromProposeResponse(filedRes),
  { id: "prop_1", status: "pending", path: "render.yaml", platform: "render", reason: "why" },
  "pendingFromProposeResponse: built from the response's own plan keys"
);
assertEqual(pendingFromProposeResponse({ ...filedRes, proposal: { ...filedRes.proposal, status: "failed" } }), null, "a failed proposal is not a pending one");
assertEqual(pendingFromProposeResponse({ proposal: null, proposal_skipped: "fallback_plan" }), null, "a skip -> no pending");

// --- skippedMessage ------------------------------------------------------------
assert(/couldn't be read/.test(skippedMessage("fallback_plan")), "fallback_plan has fixed wording");
assert(/isn't part of a project you own/.test(skippedMessage("no_workspace")), "no_workspace has fixed wording");
assertEqual(
  skippedMessage("deploy plan has no config_filename"),
  "The proposed config couldn't be filed for review: deploy plan has no config_filename.",
  "an unknown reason is shown verbatim after a fixed lead-in"
);
assertEqual([skippedMessage(""), skippedMessage(null), skippedMessage(undefined), skippedMessage(5)], [null, null, null, null], "empty / non-string -> null");

// --- regenerateDeployOutcome -----------------------------------------------------
assertEqual(regenerateDeployOutcome(filedRes), { ok: true, proposalId: "prop_1" }, "regenerate: a new pending proposal -> ok + its id");
{
  const r = regenerateDeployOutcome({ proposal: null, proposal_skipped: "fallback_plan" });
  assert(r.ok === false && /couldn't be read/.test(r.message) && /earlier proposal is still waiting/.test(r.message), "regenerate: a placeholder plan -> not ok, and says the earlier one is still there");
}
{
  const r = regenerateDeployOutcome({ proposal: { id: "p", status: "failed", path: "render.yaml" } });
  assert(r.ok === false && /status: failed/.test(r.message) && /still waiting/.test(r.message), "regenerate: a non-pending new proposal -> not ok");
}
{
  const r = regenerateDeployOutcome({});
  assert(r.ok === false && /didn't produce a new proposal/.test(r.message), "regenerate: an empty response -> not ok with a generic message");
}

// --- deployCardView --------------------------------------------------------------
const plan = { platform: "render", config_filename: "render.yaml", config_content: "x", reason: "backend detected" };
const fallbackPlan = { ...plan, config_content: "# fallback", reason: "fallback: could not parse a real proposal", fallback: true };
const pending = { id: "prop_d", status: "pending", path: "render.yaml", platform: "render", reason: "backend detected" };

assertEqual(deployCardView({ plan: null, pending: null }).kind, "no_plan", "no plan, nothing pending -> no_plan");
assertEqual(deployCardView({ plan: null, pending: null }).message, "No deploy plan proposed yet for this project.", "no_plan keeps the card's original empty-state wording");
assertEqual(deployCardView({}).kind, "no_plan", "no arguments -> no_plan");

{
  const v = deployCardView({ plan, pending });
  assertEqual([v.kind, v.platform, v.path, v.reason, v.notice], ["review", "render", "render.yaml", "backend detected", null], "pending -> review, details from the proposal");
}
{
  const v = deployCardView({ plan: { platform: "fly", config_filename: "fly.toml" }, pending });
  assertEqual([v.platform, v.path], ["render", "render.yaml"], "review shows the PENDING proposal's host, not the bus plan's (a newer propose may have replaced the plan)");
}
{
  const v = deployCardView({ plan: fallbackPlan, pending });
  assertEqual([v.kind, v.platform, v.path], ["review", "render", "render.yaml"], "a failed re-propose (placeholder plan on the bus) doesn't hide the pending proposal");
  assert(/couldn't be read/.test(v.notice), "...and warns that the re-propose didn't replace it");
}
{
  const v = deployCardView({ plan, pending, skipped: "no_workspace" });
  assert(/isn't part of a project you own/.test(v.notice), "this session's skip reason becomes the notice on a review card");
}
{
  const v = deployCardView({ plan: fallbackPlan, pending: null });
  assertEqual([v.kind, v.platform, v.path, v.reason], ["fallback", null, null, null], "placeholder plan, nothing pending -> fallback, its stand-in render.yaml is not shown as a recommendation");
  assert(/couldn't be read/.test(v.message), "fallback explains itself");
}
{
  const v = deployCardView({ plan, pending: null, skipped: "deploy plan has no config_filename" });
  assertEqual([v.kind, v.platform, v.path], ["skipped", "render", "render.yaml"], "a plan that couldn't be filed -> skipped, plan details still shown");
  assert(/no config_filename/.test(v.message), "...with the reason");
}
{
  const v = deployCardView({ plan, pending: null, fileExists: true });
  assertEqual(v.kind, "idle", "real plan, nothing pending -> idle");
  assert(/render\.yaml is in this project's files/.test(v.message), "idle + file present says so");
}
{
  const v = deployCardView({ plan, pending: null, fileExists: false });
  assert(/isn't in this project's files/.test(v.message) && /Re-propose/.test(v.message), "idle + file absent says so and points at Re-propose");
}
{
  const v = deployCardView({ plan, pending: null, fileExists: null });
  assertEqual([v.kind, v.message], ["idle", null], "idle + unknown file state -> no claim either way");
}
assertEqual(deployCardView({ plan, pending: null, skipped: "fallback_plan" }).kind, "skipped", "skip reason takes precedence over the file line");

if (failures > 0) {
  console.error(`\n${failures} test(s) failed`);
  process.exit(1);
}
console.log("\nAll deployProposal tests passed");
