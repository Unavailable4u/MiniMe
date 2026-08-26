// J.3 regression test — dangling wiring edges.
//
// No JS test runner (jest/vitest) exists in this repo yet (frontend/package.json
// has zero test deps, no jest.config/vitest.config anywhere). Rather than
// silently bootstrapping a whole framework as a side effect of a one-line
// filter fix, this is a dependency-free assertion script that exercises the
// *actual* link-filter logic from WiringGraph.jsx (kept byte-for-byte in
// sync below) so it can run with plain `node` in CI today. If/when a real
// runner is added for the frontend, this should be ported into it verbatim
// and this file deleted.
//
// Run: node frontend/app/components/__tests__/wiringGraph.linkFilter.test.mjs

function buildLinks(wiring) {
  const nodes = (wiring?.nodes || []).map((n) => ({
    id: n.id,
    label: n.label || n.id,
    type: n.type,
  }));
  const nodeIds = new Set(nodes.map((n) => n.id));
  return (wiring?.edges || [])
    .filter((e) => e.from !== e.to && nodeIds.has(e.from) && nodeIds.has(e.to))
    .map((e) => ({ source: e.from, target: e.to, kind: e.kind }));
}

// Pre-fix behavior, for comparison — the exact old filter (self-loop only).
function buildLinksOld(wiring) {
  return (wiring?.edges || [])
    .filter((e) => e.from !== e.to)
    .map((e) => ({ source: e.from, target: e.to, kind: e.kind }));
}

const wiring = {
  nodes: [
    { id: "mcu1", label: "ESP32", type: "mcu" },
    { id: "sensor1", label: "BME280", type: "sensor" },
  ],
  edges: [
    { from: "mcu1", to: "sensor1", kind: "data" },   // valid
    { from: "mcu1", to: "mcu1", kind: "power" },      // self-loop (already filtered pre-fix)
    { from: "mcu1", to: "sensor2", kind: "data" },    // dangling target — sensor2 doesn't exist
    { from: "power1", to: "mcu1", kind: "power" },    // dangling source — power1 doesn't exist
  ],
};

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

// Prove the bug existed: old filter lets dangling edges through.
const oldLinks = buildLinksOld(wiring);
assertEqual(
  oldLinks.length,
  3,
  "pre-fix filter leaves 2 dangling edges + 1 valid edge (self-loop only removed)"
);

// Prove the fix: new filter drops both dangling edges, keeps the one valid edge.
const newLinks = buildLinks(wiring);
assertEqual(
  newLinks,
  [{ source: "mcu1", target: "sensor1", kind: "data" }],
  "post-fix filter keeps only the edge whose endpoints both exist"
);

// Edge case: empty nodes list should drop every edge, not throw.
assertEqual(
  buildLinks({ nodes: [], edges: [{ from: "a", to: "b", kind: "data" }] }),
  [],
  "empty node list drops all edges without throwing"
);

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
