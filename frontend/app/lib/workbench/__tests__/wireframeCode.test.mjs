// W8.6 (Build Workbench plan) — tests for lib/workbench/wireframeCode.js.
// Run: node frontend/app/lib/workbench/__tests__/wireframeCode.test.mjs
//
// wireframeCode.js is import-free on purpose (see its header), so it
// loads with NO `imports` map — an added `import` line would make this
// test fail loudly.
import { loadSource } from "./loadSource.mjs";

const {
  WIREFRAME_CODE_GENERATOR,
  WIREFRAME_TARGET_PATH,
  WIREFRAME_MAX_CHARS,
  isWireframeProposal,
  pickPendingWireframeProposal,
  wireframeProposalSummary,
  wireframeCodeBlocker,
  describeWireframeResponse,
  wireframeCodeView,
} = loadSource("../wireframeCode.js");

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

const wf = (over = {}) => ({
  id: "prop_1",
  status: "pending",
  model_meta: { generator: "wireframe_to_code", screen_label: "Login", target_path: "index.html", replaces_existing: false },
  files: [{ path: "index.html", op: "create" }],
  summary: "Add index.html from wireframe: Login",
  ...over,
});
const chatEdit = (over = {}) => ({ id: "prop_2", status: "pending", model_meta: { generator: "code_editor" }, files: [], ...over });

// --- constants mirror the backend ---------------------------------------
assertEqual(WIREFRAME_CODE_GENERATOR, "wireframe_to_code", "generator tag matches backend WIREFRAME_CODE_GENERATOR");
assertEqual(WIREFRAME_TARGET_PATH, "index.html", "target path matches backend WIREFRAME_TARGET_PATH");
assertEqual(WIREFRAME_MAX_CHARS, 400000, "size cap matches backend _WIREFRAME_MAX_CHARS");

// --- isWireframeProposal -------------------------------------------------
assertEqual(isWireframeProposal(wf()), true, "tagged proposal is a wireframe one");
assertEqual(isWireframeProposal(chatEdit()), false, "chat edit is not");
assertEqual(isWireframeProposal({ model_meta: { generator: "deploy_config_writer" } }), false, "deploy proposal is not");
assertEqual(isWireframeProposal({}), false, "no model_meta -> not");
assertEqual(isWireframeProposal(null), false, "null -> not");
assertEqual(isWireframeProposal(undefined), false, "undefined -> not");

// --- pickPendingWireframeProposal ---------------------------------------
assertEqual(pickPendingWireframeProposal([chatEdit(), wf({ id: "w" })])?.id, "w", "picks the wireframe one among others");
assertEqual(pickPendingWireframeProposal([wf({ id: "new" }), wf({ id: "old" })])?.id, "new", "first (newest) wins");
assertEqual(pickPendingWireframeProposal([wf({ status: "accepted" }), wf({ status: "rejected" })]), null, "resolved ones are not pending");
assertEqual(pickPendingWireframeProposal([chatEdit()]), null, "no wireframe proposal -> null");
assertEqual(pickPendingWireframeProposal([null, undefined, wf({ id: "w" })])?.id, "w", "tolerates junk entries");
assertEqual(pickPendingWireframeProposal(undefined), null, "non-array -> null");
assertEqual(pickPendingWireframeProposal({ not: "an array" }), null, "object -> null");

// --- wireframeProposalSummary -------------------------------------------
assertEqual(
  wireframeProposalSummary(wf()),
  { id: "prop_1", status: "pending", path: "index.html", label: "Login", replacesExisting: false },
  "summary of a create"
);
assertEqual(wireframeProposalSummary(wf({ files: [{ path: "index.html", op: "replace" }] }))?.replacesExisting, true, "a replace op is flagged");
assertEqual(
  wireframeProposalSummary(wf({ files: [], model_meta: { generator: "wireframe_to_code", replaces_existing: true } }))?.replacesExisting,
  true,
  "no file entry -> falls back to the tagged flag"
);
assertEqual(wireframeProposalSummary(wf({ files: [], model_meta: { generator: "wireframe_to_code" } }))?.path, "index.html", "no file, no meta path -> default target");
assertEqual(wireframeProposalSummary(wf({ files: [{ path: "public/index.html", op: "create" }] }))?.path, "public/index.html", "path comes from the file the proposal carries");
assertEqual(wireframeProposalSummary(wf({ model_meta: { generator: "wireframe_to_code", screen_label: 7 } }))?.label, "", "non-string label -> empty");
assertEqual(wireframeProposalSummary(wf({ status: undefined }))?.status, "pending", "missing status defaults to pending");
assertEqual(wireframeProposalSummary(null), null, "null -> null");
assertEqual(wireframeProposalSummary({}), null, "no id -> null");
assertEqual(wireframeProposalSummary({ id: "" }), null, "empty id -> null");
assertEqual(wireframeProposalSummary("prop_1"), null, "non-object -> null");

// --- wireframeCodeBlocker -----------------------------------------------
assertEqual(wireframeCodeBlocker("<p>x</p>"), null, "usable html has no blocker");
assertEqual(wireframeCodeBlocker(""), "Add a wireframe first.", "empty is blocked");
assertEqual(wireframeCodeBlocker("  \n\t "), "Add a wireframe first.", "whitespace is blocked");
assertEqual(wireframeCodeBlocker(undefined), "Add a wireframe first.", "undefined is blocked");
assertEqual(wireframeCodeBlocker(null), "Add a wireframe first.", "null is blocked");
assertEqual(wireframeCodeBlocker("just words"), "This doesn't look like HTML yet.", "no markup is blocked");
assertEqual(wireframeCodeBlocker("<p>" + "x".repeat(WIREFRAME_MAX_CHARS) + "</p>"), "This wireframe is too large to turn into code.", "over the cap is blocked");
assertEqual(wireframeCodeBlocker("x".repeat(WIREFRAME_MAX_CHARS - 1) + "<"), null, "exactly at the cap is allowed");

// --- describeWireframeResponse ------------------------------------------
{
  const ok = describeWireframeResponse(wf());
  assertEqual(ok.ok, true, "a pending proposal is ok");
  assertEqual(ok.proposal?.id, "prop_1", "ok carries the summary the Review button opens");
}
{
  const failed = describeWireframeResponse(wf({ status: "failed", summary: "Edit generation failed: boom" }));
  assertEqual(failed, { ok: false, message: "Couldn't turn this wireframe into code: Edit generation failed: boom" }, "a failed row says why");
}
assertEqual(
  describeWireframeResponse(wf({ status: "failed", summary: "" })).message,
  "Couldn't turn this wireframe into code: the proposal wasn't created",
  "a failed row with no summary still reads"
);
assertEqual(describeWireframeResponse({}).ok, false, "no id is not ok");
assertEqual(describeWireframeResponse(null).message, "Couldn't file this wireframe for review.", "null body has a message");
assertEqual(describeWireframeResponse(wf({ status: "accepted" })).ok, false, "an already-resolved proposal is not something to review");

// --- wireframeCodeView ---------------------------------------------------
{
  const idle = wireframeCodeView({ pending: null, blocker: null });
  assertEqual(idle.kind, "idle", "nothing pending, nothing blocking -> idle");
  assertEqual(idle.message.includes("press Keep"), true, "idle says nothing is saved before Keep");
  assertEqual(idle.notice, null, "idle has no notice");
}
{
  const blocked = wireframeCodeView({ pending: null, blocker: "Add a wireframe first." });
  assertEqual(blocked, { kind: "blocked", message: "Add a wireframe first.", notice: null }, "a blocker is shown as the message");
}
{
  const review = wireframeCodeView({ pending: wireframeProposalSummary(wf()), blocker: null });
  assertEqual(review.kind, "review", "a pending proposal -> review");
  assertEqual(review.message.startsWith("index.html from “Login” is ready for your review"), true, "review names file and wireframe");
  assertEqual(review.notice, null, "a create has no replace warning");
}
{
  const review = wireframeCodeView({ pending: wireframeProposalSummary(wf({ files: [{ path: "index.html", op: "replace" }] })), blocker: null });
  assertEqual(review.notice, "This replaces the index.html already in this project — check the diff before you Keep.", "a replace warns before opening");
}
{
  const review = wireframeCodeView({ pending: wireframeProposalSummary(wf({ model_meta: { generator: "wireframe_to_code" } })), blocker: null });
  assertEqual(review.message.startsWith("index.html is ready"), true, "no label -> no 'from' clause");
}
assertEqual(
  wireframeCodeView({ pending: wireframeProposalSummary(wf()), blocker: "Add a wireframe first." }).kind,
  "review",
  "a pending proposal wins over a blocker (the pasted text may have changed since)"
);

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll wireframeCode tests passed.");
