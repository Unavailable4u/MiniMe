// Tests for lib/latestRequest.js -- the "latest request wins" guard behind
// Patch B05's board race fix. Loads the REAL source via loadSource.mjs with
// no `imports`, which also enforces that the helper stays dependency-free.
//
// Run: node frontend/app/lib/__tests__/latestRequest.test.mjs
import { loadSource } from "../workbench/__tests__/loadSource.mjs";

const { createLatestGuard } = loadSource("../../latestRequest.js");

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

{
  const g = createLatestGuard();
  const t1 = g.begin();
  assertEqual(g.isLatest(t1), true, "a lone request is the latest");
}
{
  const g = createLatestGuard();
  const t1 = g.begin();
  const t2 = g.begin();
  assertEqual(g.isLatest(t1), false, "an earlier request goes stale once a newer one begins");
  assertEqual(g.isLatest(t2), true, "the newest request is the latest");
}
{
  const g = createLatestGuard();
  const t1 = g.begin();
  g.invalidate();
  assertEqual(g.isLatest(t1), false, "invalidate() makes an in-flight request stale");
  const t2 = g.begin();
  assertEqual(g.isLatest(t2), true, "a request begun after invalidate() is the latest");
}
{
  const a = createLatestGuard();
  const b = createLatestGuard();
  const ta = a.begin();
  b.begin();
  b.begin();
  assertEqual(a.isLatest(ta), true, "guards are independent of each other");
}
{
  const g = createLatestGuard();
  assertEqual(g.isLatest(undefined), false, "an unknown token is never latest");
}

if (failures) {
  console.error(`\n${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll latestRequest tests passed");
