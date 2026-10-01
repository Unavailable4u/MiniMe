// W8.6 (Build Workbench plan) — tests for codeProposals.js's
// proposeWireframeCode(): the request it sends to the backend's
// POST .../code/proposals/from-wireframe, and how it fails.
// Run: node frontend/app/lib/workbench/__tests__/codeProposals.wireframe.test.mjs
//
// codeProposals.js's three imports (SessionContext's authHeaders, the
// Pusher client, proposalRefs) are stubbed — none of them is what's under
// test, and SessionContext is a React module plain `node` can't load.
// `fetch` is a recording fake, so what is asserted is the actual wire
// request: URL, method, headers, and the JSON body's field names (which
// must match api/routes/code_edit.py's WireframeProposalRequest:
// html / screen_label / session_id).
import { loadSource } from "./loadSource.mjs";

const { proposeWireframeCode } = loadSource("../codeProposals.js", {
  imports: {
    "../../context/SessionContext": { authHeaders: async (opts) => ({ Authorization: "Bearer t", ...(opts?.json ? { "Content-Type": "application/json" } : {}) }) },
    "../pusherClient": { getPusherClient: () => null },
    "./proposalRefs": { toProposalRef: (r) => r },
  },
});

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

let calls = [];
function fakeFetch(response) {
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return response;
  };
}
const ok = (body) => ({ ok: true, status: 200, statusText: "OK", json: async () => body });
const bad = (status, statusText, body) => ({ ok: false, status, statusText, json: async () => (body === undefined ? Promise.reject(new Error("no body")) : body) });

// --- the request ----------------------------------------------------------
{
  calls = [];
  const stored = { id: "prop_1", status: "pending" };
  fakeFetch(ok(stored));
  const out = await proposeWireframeCode("http://api.test", "ws_9", { html: "<p>x</p>", screenLabel: "Login", sessionId: "sess_1" });
  assertEqual(out, stored, "returns the stored proposal body as-is");
  assertEqual(calls.length, 1, "exactly one request");
  assertEqual(calls[0].url, "http://api.test/api/workspaces/ws_9/code/proposals/from-wireframe", "posts to the from-wireframe route of this workspace");
  assertEqual(calls[0].init.method, "POST", "POST");
  assertEqual(calls[0].init.headers, { Authorization: "Bearer t", "Content-Type": "application/json" }, "authenticated, JSON");
  assertEqual(
    JSON.parse(calls[0].init.body),
    { html: "<p>x</p>", screen_label: "Login", session_id: "sess_1" },
    "body fields match the backend's WireframeProposalRequest (snake_case)"
  );
}
{
  calls = [];
  fakeFetch(ok({ id: "p" }));
  await proposeWireframeCode("http://api.test", "ws_9", { html: "<p>x</p>" });
  assertEqual(JSON.parse(calls[0].init.body), { html: "<p>x</p>", screen_label: null, session_id: null }, "missing label / session go as null, not undefined");
}
{
  calls = [];
  fakeFetch(ok({ id: "p" }));
  await proposeWireframeCode("http://api.test", "ws_9", { html: "<p>x</p>", screenLabel: "", sessionId: "" });
  assertEqual(JSON.parse(calls[0].init.body).screen_label, "", "an empty label is sent as the empty string (the backend decides what to do with it)");
}
{
  calls = [];
  fakeFetch(ok({ id: "p" }));
  const html = '<p title="a&b">\u00e9\ud83d\ude00 `` ```</p>\n';
  await proposeWireframeCode("http://api.test", "ws_9", { html });
  assertEqual(JSON.parse(calls[0].init.body).html, html, "the html round-trips through JSON unchanged");
}

// --- failures ----------------------------------------------------------------
async function rejection(promise) {
  try {
    await promise;
    return null;
  } catch (e) {
    return e.message;
  }
}
fakeFetch(bad(400, "Bad Request", { detail: "wireframe html is empty" }));
assertEqual(await rejection(proposeWireframeCode("http://api.test", "ws_9", { html: "" })), "wireframe html is empty", "a 400 throws with the backend's own detail");

fakeFetch(bad(502, "Bad Gateway"));
assertEqual(await rejection(proposeWireframeCode("http://api.test", "ws_9", { html: "<p>x</p>" })), "502 Bad Gateway", "an error with no JSON body falls back to the status line");

fakeFetch(bad(404, "Not Found", { nope: true }));
assertEqual(await rejection(proposeWireframeCode("http://api.test", "ws_9", { html: "<p>x</p>" })), "404 Not Found", "a JSON body without `detail` falls back to the status line");

fakeFetch(ok({ id: "prop_f", status: "failed", summary: "Edit generation failed: boom" }));
assertEqual((await proposeWireframeCode("http://api.test", "ws_9", { html: "<p>x</p>" })).status, "failed", "a 200 carrying a failed row is returned, not thrown (the caller reads it)");

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll proposeWireframeCode tests passed.");
