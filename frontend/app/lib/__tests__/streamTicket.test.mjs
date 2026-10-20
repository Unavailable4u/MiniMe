// Tests for lib/streamTicket.js -- the helper that replaces
// `?token=<jwt>` with a single-use `?ticket=` on the WebSocket and SSE URLs.
//
// Loads the REAL streamTicket.js through lib/workbench's loadSource.mjs
// (same reuse the preview tests do). No `imports` are passed on purpose:
// the helper must stay dependency-free -- it takes fetch/header builders
// as arguments precisely so it never pulls in the Supabase client -- and
// this load fails loudly if someone adds an `import`.
//
// Run: node frontend/app/lib/__tests__/streamTicket.test.mjs
import { loadSource } from "../workbench/__tests__/loadSource.mjs";

const { requestStreamTicket, buildWsUrl, buildSseUrl } = loadSource("../../streamTicket.js");

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

const API = "https://api.example.com";
const okResponse = (body) => ({ ok: true, status: 200, json: async () => body });
const badResponse = (status) => ({ ok: false, status, json: async () => ({ detail: "nope" }) });
const getHeaders = async (opts) => ({
  ...(opts?.json ? { "Content-Type": "application/json" } : {}),
  Authorization: "Bearer real-access-token",
});

// --- requestStreamTicket: the request it makes -----------------------------

{
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, init });
    return okResponse({ ticket: "tkt_abc123", expires_in: 30 });
  };
  const ticket = await requestStreamTicket({
    apiUrl: API, kind: "ws", sessionId: "chat-1", fetchFn, getHeaders,
  });

  assertEqual(ticket, "tkt_abc123", "returns the ticket from the response body");
  assertEqual(calls.length, 1, "makes exactly one request");
  assertEqual(calls[0].url, `${API}/api/stream-ticket`, "POSTs to /api/stream-ticket on the API origin");
  assertEqual(calls[0].init.method, "POST", "uses POST");
  assertEqual(
    JSON.parse(calls[0].init.body),
    { kind: "ws", session_id: "chat-1" },
    "body names the transport and the session"
  );
  assertEqual(
    calls[0].init.headers.Authorization,
    "Bearer real-access-token",
    "the real credential travels in the Authorization header (the one place it should)"
  );
  assertEqual(calls[0].init.headers["Content-Type"], "application/json", "asks authHeaders for a JSON content type");
  assertEqual(/token/i.test(calls[0].url), false, "the access token is never put in the URL");
}

{
  let n = 0;
  const fetchFn = async () => okResponse({ ticket: `t${++n}` });
  const a = await requestStreamTicket({ apiUrl: API, kind: "sse", sessionId: "s", fetchFn, getHeaders });
  const b = await requestStreamTicket({ apiUrl: API, kind: "sse", sessionId: "s", fetchFn, getHeaders });
  assertEqual([a, b], ["t1", "t2"], "every call mints a fresh ticket (a ticket is single-use; never cached)");
}

// --- requestStreamTicket: every failure becomes null -----------------------
// Callers treat null as "don't open the stream", exactly as they did when
// there was no token. None of these may throw.

const failureCases = [
  ["401 (signed out)", async () => badResponse(401)],
  ["404 (no access to this chat)", async () => badResponse(404)],
  ["503 (ticket store down)", async () => badResponse(503)],
  ["network error (fetch rejects)", async () => { throw new TypeError("Failed to fetch"); }],
  ["undefined response", async () => undefined],
  ["body is not JSON", async () => ({ ok: true, json: async () => { throw new SyntaxError("bad json"); } })],
  ["body has no ticket", async () => okResponse({})],
  ["ticket is empty", async () => okResponse({ ticket: "" })],
  ["ticket is not a string", async () => okResponse({ ticket: 12345 })],
  ["body is null", async () => okResponse(null)],
];
for (const [label, fetchFn] of failureCases) {
  let result, threw = false;
  try {
    result = await requestStreamTicket({ apiUrl: API, kind: "ws", sessionId: "s", fetchFn, getHeaders });
  } catch {
    threw = true;
  }
  assertEqual({ threw, result }, { threw: false, result: null }, `returns null, does not throw: ${label}`);
}

{
  const getHeadersThatThrows = async () => { throw new Error("no session"); };
  let result, threw = false;
  try {
    result = await requestStreamTicket({
      apiUrl: API, kind: "ws", sessionId: "s",
      fetchFn: async () => okResponse({ ticket: "x" }), getHeaders: getHeadersThatThrows,
    });
  } catch {
    threw = true;
  }
  assertEqual({ threw, result }, { threw: false, result: null }, "returns null, does not throw: header builder fails");
}

// --- URL builders -----------------------------------------------------------

assertEqual(
  buildWsUrl("http://localhost:8000", "chat-1", "tkt"),
  "ws://localhost:8000/ws/chat-1?ticket=tkt",
  "ws URL: http -> ws, ticket in the query"
);
assertEqual(
  buildWsUrl("https://api.example.com", "chat-1", "tkt"),
  "wss://api.example.com/ws/chat-1?ticket=tkt",
  "ws URL: https -> wss"
);
assertEqual(
  buildWsUrl(API, "a/b c", "t&k=t"),
  "wss://api.example.com/ws/a%2Fb%20c?ticket=t%26k%3Dt",
  "ws URL: session id and ticket are URL-encoded"
);
assertEqual(
  buildSseUrl(API, "chat-1", "tkt"),
  "https://api.example.com/api/task/chat-1/stream?ticket=tkt",
  "sse URL: on the API origin (not a bare relative path), ticket in the query"
);
assertEqual(
  buildSseUrl(API, "a/b c", "t&k=t"),
  "https://api.example.com/api/task/a%2Fb%20c/stream?ticket=t%26k%3Dt",
  "sse URL: session id and ticket are URL-encoded"
);
for (const url of [buildWsUrl(API, "s", "t"), buildSseUrl(API, "s", "t")]) {
  assertEqual(/[?&]token=/.test(url), false, `no ?token= in ${url}`);
}

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll streamTicket tests passed.");
