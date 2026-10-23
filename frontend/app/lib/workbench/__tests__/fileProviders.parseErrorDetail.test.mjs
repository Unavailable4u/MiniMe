// Patch B06 (M2C build plan, WP B06 — readable validation errors) — tests
// for fileProviders.js's parseErrorDetail(): how an HTTP error response is
// turned into the one-line message a caller puts in `new Error(...)`.
// Run: node frontend/app/lib/workbench/__tests__/fileProviders.parseErrorDetail.test.mjs
//
// Why this exists: FastAPI's 422 responses carry `detail` as an ARRAY of
// objects ({loc, msg, type}). parseErrorDetail() used to return that array
// as-is, `new Error(array)` stringified it, and the Build tab showed
// "[object Object]" for e.g. an invalid file path. These tests pin the
// readable format:
//   array   -> "file_path: message; other: message" (a leading "body" or
//              "query" is dropped from `loc`, the rest is joined with ".")
//   object  -> its `message` or `msg`, else compact JSON
//   string  -> unchanged
//   missing / empty -> the old "<status> <statusText>" fallback
// and that the function NEVER throws, whatever the body looks like.
//
// Loads the REAL lib/workbench/fileProviders.js through loadSource.mjs (no
// pasted copy). Its three imports are not what is under test: SessionContext
// is a React module plain `node` can't load, so authHeaders is stubbed;
// the Pusher client is stubbed; aiUpdates.js is dependency-free and is loaded
// for real. `fetch` is a fake for the provider-level case at the bottom,
// which proves the message really reaches the Error a caller catches.
import { loadSource } from "./loadSource.mjs";

const fileProviders = loadSource("../fileProviders.js", {
  imports: {
    "../../context/SessionContext": { authHeaders: async () => ({ Authorization: "Bearer t" }) },
    "../pusherClient": { getPusherClient: () => null },
    "./aiUpdates": loadSource("../aiUpdates.js"),
  },
});
const { parseErrorDetail, createCloudFileProvider } = fileProviders;

let failures = 0;

// Every case goes through check() so one broken case (including a thrown
// TypeError, e.g. "parseErrorDetail is not a function" before the export
// exists) is reported individually instead of aborting the whole file.
async function check(msg, fn) {
  try {
    const { actual, expected } = await fn();
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    if (a !== e) {
      failures++;
      console.error(`FAIL: ${msg}\n  expected: ${e}\n  actual:   ${a}`);
    } else {
      console.log(`PASS: ${msg}`);
    }
  } catch (err) {
    failures++;
    console.error(`FAIL: ${msg}\n  threw: ${err && err.stack ? err.stack.split("\n")[0] : err}`);
  }
}

// A minimal fetch Response stand-in: only what parseErrorDetail touches.
function fakeRes(body, { status = 422, statusText = "Unprocessable Entity" } = {}) {
  return { ok: false, status, statusText, json: async () => body };
}
function nonJsonRes({ status = 502, statusText = "Bad Gateway" } = {}) {
  return {
    ok: false,
    status,
    statusText,
    json: async () => {
      throw new SyntaxError("Unexpected token < in JSON at position 0");
    },
  };
}
async function detailOf(body, opts) {
  return parseErrorDetail(fakeRes(body, opts));
}

const FALLBACK_422 = "422 Unprocessable Entity";

// --- string detail -----------------------------------------------------

await check("string detail stays as is", async () => ({
  actual: await detailOf({ detail: "File changed on the server" }),
  expected: "File changed on the server",
}));

await check("string detail is not trimmed or reworded", async () => ({
  actual: await detailOf({ detail: "  Not found.  " }),
  expected: "  Not found.  ",
}));

// --- array detail (FastAPI / Pydantic 422) -------------------------------

await check("array: one item renders 'field: message', dropping a leading 'body'", async () => ({
  actual: await detailOf({
    detail: [{ loc: ["body", "file_path"], msg: "String should have at most 255 characters", type: "string_too_long" }],
  }),
  expected: "file_path: String should have at most 255 characters",
}));

await check("array: several items are joined with '; ' in order", async () => ({
  actual: await detailOf({
    detail: [
      { loc: ["body", "file_path"], msg: "Field required", type: "missing" },
      { loc: ["body", "content"], msg: "Input should be a valid string", type: "string_type" },
    ],
  }),
  expected: "file_path: Field required; content: Input should be a valid string",
}));

await check("array: a leading 'query' is dropped too", async () => ({
  actual: await detailOf({ detail: [{ loc: ["query", "limit"], msg: "Input should be a valid integer", type: "int_parsing" }] }),
  expected: "limit: Input should be a valid integer",
}));

await check("array: the rest of loc is joined with '.' (nested fields and list indexes)", async () => ({
  actual: await detailOf({ detail: [{ loc: ["body", "files", 0, "path"], msg: "Field required", type: "missing" }] }),
  expected: "files.0.path: Field required",
}));

await check("array: only a LEADING body/query is dropped, not one deeper in loc", async () => ({
  actual: await detailOf({ detail: [{ loc: ["body", "meta", "body"], msg: "Bad", type: "x" }] }),
  expected: "meta.body: Bad",
}));

await check("array: loc of just ['body'] (whole-body error) renders the message alone", async () => ({
  actual: await detailOf({ detail: [{ loc: ["body"], msg: "Input should be a valid dictionary", type: "dict_type" }] }),
  expected: "Input should be a valid dictionary",
}));

await check("array: missing or empty loc renders the message alone", async () => ({
  actual: [await detailOf({ detail: [{ msg: "Something is wrong" }] }), await detailOf({ detail: [{ loc: [], msg: "Something is wrong" }] })],
  expected: ["Something is wrong", "Something is wrong"],
}));

await check("array: string items are kept, items with nothing usable are skipped", async () => ({
  actual: await detailOf({ detail: ["first problem", null, { loc: ["body", "a"], msg: "second problem" }, ""] }),
  expected: "first problem; a: second problem",
}));

await check("array: an object item with no msg falls back to compact JSON (not [object Object])", async () => ({
  actual: await detailOf({ detail: [{ loc: ["body", "a"], code: 7 }] }),
  expected: '{"loc":["body","a"],"code":7}',
}));

await check("array: empty array falls back to '<status> <statusText>'", async () => ({
  actual: await detailOf({ detail: [] }),
  expected: FALLBACK_422,
}));

// --- object detail -------------------------------------------------------

await check("object: uses `message`", async () => ({
  actual: await detailOf({ detail: { message: "Quota exceeded", code: "quota" } }),
  expected: "Quota exceeded",
}));

await check("object: uses `msg` when there is no `message`", async () => ({
  actual: await detailOf({ detail: { msg: "Bad path", code: "bad_path" } }),
  expected: "Bad path",
}));

await check("object: `message` wins over `msg`", async () => ({
  actual: await detailOf({ detail: { msg: "second", message: "first" } }),
  expected: "first",
}));

await check("object: otherwise compact JSON on one line", async () => ({
  actual: await detailOf({ detail: { code: "quota", limit: 5 } }),
  expected: '{"code":"quota","limit":5}',
}));

await check("object: a blank `message` is ignored in favour of `msg`, then JSON", async () => ({
  actual: [await detailOf({ detail: { message: "  ", msg: "real" } }), await detailOf({ detail: { message: "", x: 1 } })],
  expected: ["real", '{"message":"","x":1}'],
}));

await check("object: empty object falls back to '<status> <statusText>'", async () => ({
  actual: await detailOf({ detail: {} }),
  expected: FALLBACK_422,
}));

// --- missing detail ------------------------------------------------------

await check("missing detail falls back to '<status> <statusText>'", async () => ({
  actual: await detailOf({ error: "something else" }),
  expected: FALLBACK_422,
}));

await check("null / empty-string / blank detail fall back", async () => ({
  actual: [await detailOf({ detail: null }), await detailOf({ detail: "" }), await detailOf({ detail: "   " })],
  expected: [FALLBACK_422, FALLBACK_422, FALLBACK_422],
}));

await check("fallback uses the response's own status and statusText", async () => ({
  actual: await detailOf({}, { status: 500, statusText: "Internal Server Error" }),
  expected: "500 Internal Server Error",
}));

// --- non-JSON body and other odd bodies ----------------------------------

await check("non-JSON body (res.json() rejects) falls back instead of throwing", async () => ({
  actual: await parseErrorDetail(nonJsonRes()),
  expected: "502 Bad Gateway",
}));

await check("JSON body that is not an object (null, string, number, array) falls back", async () => ({
  actual: [await detailOf(null), await detailOf("oops"), await detailOf(42), await detailOf([1, 2])],
  expected: [FALLBACK_422, FALLBACK_422, FALLBACK_422, FALLBACK_422],
}));

// --- never throws --------------------------------------------------------

await check("never throws: res.json() throwing synchronously", async () => ({
  actual: await parseErrorDetail({
    status: 500,
    statusText: "Oops",
    json: () => {
      throw new Error("boom");
    },
  }),
  expected: "500 Oops",
}));

await check("never throws: a detail JSON.stringify cannot serialise (BigInt)", async () => ({
  actual: await detailOf({ detail: { big: 10n } }),
  expected: FALLBACK_422,
}));

await check("never throws: an array item JSON.stringify cannot serialise", async () => ({
  actual: await detailOf({ detail: [{ big: 10n }, { loc: ["body", "ok"], msg: "fine" }] }),
  expected: "ok: fine",
}));

await check("never throws: a response with no usable status/json at all", async () => {
  const out = [];
  for (const res of [{}, { json: async () => ({ detail: "x" }) }, null, undefined]) {
    out.push(typeof (await parseErrorDetail(res)));
  }
  return { actual: out, expected: ["string", "string", "string", "string"] };
});

await check("never throws: malformed loc and msg types", async () => {
  const out = await detailOf({
    detail: [
      { loc: "body.file_path", msg: "loc is a string" },
      { loc: [null, { a: 1 }], msg: 5 },
      { loc: ["body", "x"], msg: "" },
    ],
  });
  return { actual: typeof out, expected: "string" };
});

// --- through the real provider -------------------------------------------
// The user-visible symptom: createCloudFileProvider().write() rejecting with
// an Error whose message was "[object Object]".

await check("provider: a 422 with an array detail reaches the thrown Error readably", async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    fakeRes({ detail: [{ loc: ["body", "file_path"], msg: "String should match pattern", type: "string_pattern_mismatch" }] });
  try {
    const provider = createCloudFileProvider({ workspaceId: "ws1", apiUrl: "http://api.test" });
    let message = null;
    try {
      await provider.write("bad path", "x");
    } catch (err) {
      message = err.message;
    }
    return { actual: message, expected: "file_path: String should match pattern" };
  } finally {
    globalThis.fetch = realFetch;
  }
});

await check("provider: a 500 with a non-JSON body still gives '<status> <statusText>'", async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => nonJsonRes({ status: 500, statusText: "Internal Server Error" });
  try {
    const provider = createCloudFileProvider({ workspaceId: "ws1", apiUrl: "http://api.test" });
    let message = null;
    try {
      await provider.list();
    } catch (err) {
      message = err.message;
    }
    return { actual: message, expected: "500 Internal Server Error" };
  } finally {
    globalThis.fetch = realFetch;
  }
});

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
