// Patch B05 (M2C Phase 0) -- the Build board must not show a slow response
// from the PREVIOUS project after the user has switched to another one.
//
// Why this test looks the way it does
// -----------------------------------
// Same constraint as buildTab.promoteGuard.test.mjs (Patch B04): BuildTab.jsx
// is a ~2000-line React component that plain `node` (all `npm test` runs)
// cannot import or render, and adding a React test library would be a new
// dependency. So this reads the REAL source of BuildTab.jsx, pulls out the
// real `refresh()` function and evaluates it against a fake fetch and a fake
// setData; the real guard comes from lib/latestRequest.js. Nothing under test
// is copied into this file.
//
// refresh() closes over `selectedWsId`, which React re-binds on every
// render. The test mimics that by building a fresh refresh() per "render"
// (render("A"), render("B")) that all share ONE guard ref, exactly
// like the real component's useRef.
//
// What this does NOT prove: that React re-renders with the right data after
// setData, or that the effect's cleanup runs in the browser. The effect's
// wiring is pinned at source level below, and the manual check steps in the
// patch description cover the rest.
//
// Run: node frontend/app/components/__tests__/buildTab.boardRace.test.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadSource } from "../../lib/workbench/__tests__/loadSource.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.resolve(here, "../tabs/BuildTab.jsx"), "utf8");

let failures = 0;
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}
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

// --- tiny source scanner (same approach as the B04 test) ---------------------
function matchBrace(text, open) {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    const c = text[i];
    const n = text[i + 1];
    if (c === "/" && n === "/") {
      i = text.indexOf("\n", i);
      if (i === -1) break;
    } else if (c === "/" && n === "*") {
      i = text.indexOf("*/", i + 2) + 1;
      if (i === 0) break;
    } else if (c === "'" || c === '"' || c === "`") {
      for (i++; i < text.length && text[i] !== c; i++) {
        if (text[i] === "\\") i++;
      }
    } else if (c === "{") {
      depth++;
    } else if (c === "}") {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  return -1;
}
function extractFunction(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src);
  if (!m) return null;
  const open = src.indexOf("{", src.indexOf(")", m.index));
  const end = matchBrace(src, open);
  return end === -1 ? null : src.slice(m.index, end);
}

// --- fakes -------------------------------------------------------------------
// A fetch whose responses are released by hand, so the test controls the
// order in which they "arrive".
function makeHarness() {
  const pending = {}; // wsId -> list of { resolve, reject }
  const calls = [];
  const fetchFn = (url) =>
    new Promise((resolve, reject) => {
      const wsId = url.split("/").pop();
      calls.push(url);
      (pending[wsId] ||= []).push({ resolve, reject });
    });
  const respond = (wsId, payload, { ok = true } = {}) => {
    const entry = pending[wsId].shift();
    entry.resolve({
      ok,
      status: ok ? 200 : 500,
      statusText: ok ? "OK" : "Server Error",
      json: async () => payload,
    });
  };
  const sets = [];
  const setData = (d) => sets.push(d);
  return { fetchFn, respond, calls, sets, setData };
}

const refreshSrc = extractFunction("refresh");
assert(!!refreshSrc, "refresh() was found in BuildTab.jsx");

const { createLatestGuard } = loadSource("../../latestRequest.js");

// A "render" of BuildTab: refresh() bound to one selectedWsId, all sharing the
// same guard ref and fakes.
function makeRenderer(h) {
  const boardGuardRef = { current: createLatestGuard() };
  const factory = new Function(
    "selectedWsId", "API_URL", "authHeaders", "fetch", "setData", "boardGuardRef",
    `return (${refreshSrc});`,
  );
  const render = (selectedWsId) =>
    factory(selectedWsId, "http://api", async () => ({}), h.fetchFn, h.setData, boardGuardRef);
  return { render, boardGuardRef };
}

const tick = () => new Promise((r) => setImmediate(r));

async function main() {
  // 1. THE BUG: A requested first, B second; B answers, then A answers LAST.
  {
    const h = makeHarness();
    const { render } = makeRenderer(h);
    const pA = render("A")();
    const pB = render("B")();
    await tick();
    h.respond("B", { _session_id: "chat-B", name: "B data" });
    await tick();
    h.respond("A", { _session_id: "chat-A", name: "A data" }); // earlier request resolves last
    const [rA, rB] = await Promise.all([pA, pB]);
    assertEqual(h.sets.map((d) => d.name), ["B data"], "earlier fetch resolving last does not overwrite the latest project's data");
    assertEqual(rB && rB.name, "B data", "the latest request still returns its data");
    assertEqual(rA, null, "a dropped stale response returns null");
  }

  // 2. Same overlap, other arrival order: A answers first, then B.
  {
    const h = makeHarness();
    const { render } = makeRenderer(h);
    const pA = render("A")();
    const pB = render("B")();
    await tick();
    h.respond("A", { name: "A data" });
    await tick();
    h.respond("B", { name: "B data" });
    await Promise.all([pA, pB]);
    assertEqual(h.sets.map((d) => d.name), ["B data"], "stale response arriving first is dropped; only the latest project's data is applied");
  }

  // 3. A non-overlapping refresh still applies and returns its data.
  {
    const h = makeHarness();
    const { render } = makeRenderer(h);
    const p = render("A")();
    await tick();
    h.respond("A", { name: "A data" });
    const r = await p;
    assertEqual(h.sets.map((d) => d.name), ["A data"], "a lone refresh applies its data");
    assertEqual(r && r.name, "A data", "a lone refresh returns its data (existing callers keep working)");
  }

  // 4. Same project, two overlapping refreshes (effect load + Deploy-card
  //    action): the newer one wins whichever order they arrive in.
  {
    const h = makeHarness();
    const { render } = makeRenderer(h);
    const first = render("A")();
    const second = render("A")();
    await tick();
    h.respond("A", { name: "newer" }); // pending[A] is FIFO: this answers `first`
    h.respond("A", { name: "newest" });
    await Promise.all([first, second]);
    assertEqual(h.sets.map((d) => d.name), ["newest"], "for the same project the most recently started refresh wins");
  }

  // 5. Project cleared while a request is in flight: invalidate() drops it.
  {
    const h = makeHarness();
    const { render, boardGuardRef } = makeRenderer(h);
    const p = render("A")();
    await tick();
    boardGuardRef.current.invalidate(); // what the effect does when selectedWsId becomes null
    h.respond("A", { name: "A data" });
    const r = await p;
    assertEqual(h.sets.length, 0, "a response that lands after the project was cleared is dropped");
    assertEqual(r, null, "...and returns null");
  }

  // 6. Errors still propagate to the caller exactly as before.
  {
    const h = makeHarness();
    const { render } = makeRenderer(h);
    const p = render("A")();
    await tick();
    h.respond("A", {}, { ok: false });
    let msg = null;
    try { await p; } catch (e) { msg = e.message; }
    assertEqual(msg, "500 Server Error", "a failed request still throws `<status> <statusText>`");
    assertEqual(h.sets.length, 0, "a failed request never calls setData");
  }

  // 7. No project selected: still a silent no-op, no fetch.
  {
    const h = makeHarness();
    const { render } = makeRenderer(h);
    const r = await render(null)();
    assertEqual(h.calls.length, 0, "refresh() with no project selected does not fetch");
    assertEqual(r === undefined, true, "refresh() with no project selected still returns undefined");
  }

  // --- source-level pins for what cannot be run without React ---------------
  assert(
    /from\s+["']\.\.\/\.\.\/lib\/latestRequest["']/.test(src),
    "BuildTab.jsx imports the guard from lib/latestRequest",
  );
  assert(
    /useRef\(/.test(src.slice(src.indexOf("boardGuardRef") - 80, src.indexOf("boardGuardRef") + 200)) ||
      /boardGuardRef\s*=\s*useRef/.test(src),
    "the guard is held in a ref (survives re-renders)",
  );
  {
    // The effect that loads the board: its "no project" branch must
    // invalidate in-flight requests before clearing data.
    const end = src.indexOf("}, [API_URL, selectedWsId]);");
    const eff = end === -1 ? "" : src.slice(src.lastIndexOf("useEffect(() => {", end), end);
    assert(eff.length > 0 && /invalidate\(\)/.test(eff), "the load effect invalidates in-flight requests when no project is selected");
    assert(
      eff.indexOf("invalidate()") < eff.indexOf("setData(null)"),
      "...and does so before clearing the data",
    );
    assert(/refresh\(\)/.test(eff), "the load effect still loads through refresh()");
  }
  assert(
    (src.match(/\bsetData\(/g) || []).length === 2,
    "setData is called in exactly two places (refresh and the cleared-project branch)",
  );
}

main()
  .then(() => {
    if (failures) {
      console.error(`\n${failures} assertion(s) failed`);
      process.exit(1);
    }
    console.log("\nAll buildTab.boardRace tests passed");
  })
  .catch((e) => {
    console.error(e);
    process.exit(1);
  });
