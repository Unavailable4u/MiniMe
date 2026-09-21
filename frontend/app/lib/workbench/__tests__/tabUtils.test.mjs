// W2.3a (Build Workbench plan) — tests for tabUtils.js.
//
// Loads the REAL lib/workbench/tabUtils.js through loadSource.mjs (no
// pasted copy). No `imports` are passed on purpose: tabUtils.js must stay
// dependency-free, and this load fails if someone adds an `import` to it.
//
// Run: node frontend/app/lib/workbench/__tests__/tabUtils.test.mjs
import { loadSource } from "./loadSource.mjs";

const { tabLabels, nextActiveAfterClose, planBufferSync, planAutosave, encodeTabFlags, decodeTabFlags } = loadSource("../tabUtils.js");

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

// --- tabLabels -------------------------------------------------------

assertEqual(
  tabLabels(["src/App.jsx", "README.md"]),
  [
    { path: "src/App.jsx", name: "App.jsx", hint: null },
    { path: "README.md", name: "README.md", hint: null },
  ],
  "unique file names get no folder hint"
);

assertEqual(
  tabLabels(["src/index.js", "public/index.js", "other.js"]),
  [
    { path: "src/index.js", name: "index.js", hint: "src" },
    { path: "public/index.js", name: "index.js", hint: "public" },
    { path: "other.js", name: "other.js", hint: null },
  ],
  "two tabs with the same name each get their folder as a hint; the unique one doesn't"
);

assertEqual(
  tabLabels(["index.js", "src/index.js"]),
  [
    { path: "index.js", name: "index.js", hint: "root" },
    { path: "src/index.js", name: "index.js", hint: "src" },
  ],
  "a root-level file in a name clash is hinted 'root'"
);

assertEqual(tabLabels([]), [], "no tabs, no labels");

// --- nextActiveAfterClose ---------------------------------------------

const strip = ["a", "b", "c", "d"];

assertEqual(nextActiveAfterClose(strip, "b", ["d"]), "b", "closing a non-active tab keeps the active one");
assertEqual(nextActiveAfterClose(strip, null, ["a"]), null, "no active tab stays no active tab");
assertEqual(nextActiveAfterClose(strip, "b", ["b"]), "c", "closing the active tab picks its right neighbour");
assertEqual(nextActiveAfterClose(strip, "d", ["d"]), "c", "closing the last active tab falls back to its left neighbour");
assertEqual(nextActiveAfterClose(strip, "b", ["b", "c"]), "d", "the right neighbour skips other tabs closing in the same batch");
assertEqual(nextActiveAfterClose(strip, "c", ["c", "d"]), "b", "with nothing to the right, falls back left");
assertEqual(nextActiveAfterClose(strip, "b", ["a", "b", "c", "d"]), null, "closing everything leaves no active tab");
assertEqual(nextActiveAfterClose(strip, "b", new Set(["b"])), "c", "accepts any iterable of paths (a Set)");
assertEqual(nextActiveAfterClose(["a", "b"], "ghost", ["ghost"]), "b", "an active path missing from the strip falls back to the last survivor");
assertEqual(nextActiveAfterClose(["a"], "ghost", ["ghost", "a"]), null, "...or null when nothing survives");

// --- planBufferSync ----------------------------------------------------

const clean = (version) => ({ version, dirty: false, stale: false });
const dirty = (version, extra = {}) => ({ version, dirty: true, stale: false, ...extra });

assertEqual(
  planBufferSync({
    tabs: ["a", "b"],
    buffers: { a: clean(2), b: clean(5) },
    meta: { a: { version: 2 }, b: { version: 5 } },
  }),
  { reload: [], stale: [], drop: [] },
  "buffers already at the server's version need nothing"
);

assertEqual(
  planBufferSync({
    tabs: ["a"],
    buffers: { a: clean(2) },
    meta: { a: { version: 3 } },
  }),
  { reload: ["a"], stale: [], drop: [] },
  "a clean buffer behind the server is reloaded"
);

assertEqual(
  planBufferSync({
    tabs: ["a"],
    buffers: { a: dirty(2) },
    meta: { a: { version: 3 } },
  }),
  { reload: [], stale: ["a"], drop: [] },
  "a dirty buffer behind the server is flagged stale, never reloaded over"
);

assertEqual(
  planBufferSync({
    tabs: ["a"],
    buffers: { a: dirty(2, { stale: true }) },
    meta: { a: { version: 3 } },
  }),
  { reload: [], stale: [], drop: [] },
  "an already-stale buffer isn't flagged again"
);

assertEqual(
  planBufferSync({
    tabs: ["a"],
    buffers: { a: dirty(2) },
    meta: { a: { version: 3 } },
    dismissed: { a: 3 },
  }),
  { reload: [], stale: [], drop: [] },
  "'Keep mine' for server version 3 suppresses the banner for version 3"
);

assertEqual(
  planBufferSync({
    tabs: ["a"],
    buffers: { a: dirty(2) },
    meta: { a: { version: 4 } },
    dismissed: { a: 3 },
  }),
  { reload: [], stale: ["a"], drop: [] },
  "...but a NEWER server version brings the banner back"
);

assertEqual(
  planBufferSync({
    tabs: ["a"],
    buffers: { a: clean(2) },
    meta: {},
  }),
  { reload: [], stale: [], drop: ["a"] },
  "a clean tab whose file is gone from the server is dropped"
);

assertEqual(
  planBufferSync({
    tabs: ["a"],
    buffers: { a: dirty(2) },
    meta: {},
  }),
  { reload: [], stale: [], drop: [] },
  "a dirty tab whose file is gone is kept — unsaved edits are never thrown away"
);

assertEqual(
  planBufferSync({
    tabs: ["a", "b"],
    buffers: { a: clean(1) },
    meta: { a: { version: 1 }, b: { version: 9 } },
  }),
  { reload: [], stale: [], drop: [] },
  "a tab with no buffer yet (still loading) is left alone"
);

assertEqual(
  planBufferSync({
    tabs: ["a", "b"],
    buffers: { a: clean(1), b: clean(1) },
    meta: { a: { version: 2 }, b: { version: 2 } },
    busy: ["a"],
  }),
  { reload: ["b"], stale: [], drop: [] },
  "a path with a save in flight is skipped; others still sync"
);

assertEqual(
  planBufferSync({
    tabs: ["a", "b", "c", "d"],
    buffers: { a: clean(1), b: dirty(1), c: clean(1), d: clean(4) },
    meta: { a: { version: 2 }, b: { version: 2 }, d: { version: 4 } },
  }),
  { reload: ["a"], stale: ["b"], drop: ["c"] },
  "each tab is judged on its own: reload one, flag one, drop one, leave one"
);

assertEqual(
  planBufferSync({
    tabs: ["a"],
    buffers: { a: clean(1) },
    meta: { a: {} },
  }),
  { reload: [], stale: [], drop: [] },
  "a meta entry with no version counts as version 0 (never ahead)"
);

// --- planAutosave (W2.5) -----------------------------------------------

{
  const dirty = (edited, extra = {}) => ({ dirty: true, edited, ...extra });
  const clean = { dirty: false, edited: "x" };

  assertEqual(
    planAutosave({ tabs: ["a", "b"], buffers: { a: dirty("A1"), b: clean } }),
    [{ path: "a", edited: "A1" }],
    "only dirty buffers are due, each with the text to debounce on"
  );

  assertEqual(
    planAutosave({ tabs: ["a", "b"], buffers: { a: dirty("A1"), b: dirty("B1") } }),
    [
      { path: "a", edited: "A1" },
      { path: "b", edited: "B1" },
    ],
    "every dirty file is due — not just the active one — so switching tabs inside the delay strands nothing"
  );

  assertEqual(planAutosave({ tabs: [], buffers: {} }), [], "no tabs, nothing due");
  assertEqual(planAutosave({ tabs: ["gone"], buffers: {} }), [], "a tab whose file hasn't loaded yet has no buffer and isn't due");

  assertEqual(
    planAutosave({ tabs: ["a"], buffers: { a: dirty("A1", { stale: true }) } }),
    [],
    "a stale buffer (server moved on) is never autosaved — the banner asks the person first"
  );

  assertEqual(
    planAutosave({ tabs: ["a"], buffers: { a: dirty("A1") }, busy: new Set(["a"]) }),
    [],
    "a file whose save is in flight isn't due (busy accepts a Set)"
  );
  assertEqual(
    planAutosave({ tabs: ["a"], buffers: { a: dirty("A1") }, busy: ["a"] }),
    [],
    "...or an array"
  );
  assertEqual(
    planAutosave({ tabs: ["a", "b"], buffers: { a: dirty("A1"), b: dirty("B1") }, busy: ["a"] }),
    [{ path: "b", edited: "B1" }],
    "busy only holds back its own file"
  );

  assertEqual(
    planAutosave({ tabs: ["a"], buffers: { a: dirty("A1") }, conflicts: { a: { current: {} } } }),
    [],
    "a file with an unresolved save conflict is never autosaved — that's the person's call"
  );

  assertEqual(
    planAutosave({ tabs: ["a"], buffers: { a: dirty("A1") }, failed: { a: "A1" } }),
    [],
    "a file whose last save FAILED for exactly this text isn't retried (no hammering while offline)"
  );
  assertEqual(
    planAutosave({ tabs: ["a"], buffers: { a: dirty("A2") }, failed: { a: "A1" } }),
    [{ path: "a", edited: "A2" }],
    "...but a keystroke since then (different text) earns the next retry"
  );

  // Paths are user-chosen file names, so inherited Object.prototype keys must not read as "has a conflict / failure".
  assertEqual(
    planAutosave({ tabs: ["constructor", "toString"], buffers: { constructor: dirty("c"), toString: dirty("t") } }),
    [
      { path: "constructor", edited: "c" },
      { path: "toString", edited: "t" },
    ],
    "files named like Object.prototype members aren't mistaken for conflicted/failed ones"
  );

  assertEqual(
    planAutosave({ tabs: ["b", "a"], buffers: { a: dirty("A1"), b: dirty("B1") } }).map((d) => d.path),
    ["b", "a"],
    "results follow tab order"
  );
}

// --- encodeTabFlags / decodeTabFlags -----------------------------------

const buffers = { a: { dirty: true }, b: { stale: true }, c: { dirty: true, stale: true }, d: {} };
const key = encodeTabFlags(["a", "b", "c", "d", "e"], buffers);
assertEqual(key, "a\td\nb\ts\nc\tds\nd\t\ne\tl", "flags encode one 'path<TAB>flags' line per tab; a missing buffer is 'l'");

assertEqual(
  decodeTabFlags(key),
  {
    a: { dirty: true, stale: false, loading: false },
    b: { dirty: false, stale: true, loading: false },
    c: { dirty: true, stale: true, loading: false },
    d: { dirty: false, stale: false, loading: false },
    e: { dirty: false, stale: false, loading: true },
  },
  "decode is the inverse of encode"
);

assertEqual(decodeTabFlags(""), {}, "an empty key decodes to no flags");
assertEqual(decodeTabFlags(encodeTabFlags([], {})), {}, "no tabs round-trips to no flags");

assertEqual(
  encodeTabFlags(["a"], { a: { dirty: true } }) === encodeTabFlags(["a"], { a: { dirty: true, edited: "more" } }),
  true,
  "the key doesn't change when only the text changes — so memoized panes skip per-keystroke renders"
);

const oddPath = "app/[id]/(group)/my file.tsx";
assertEqual(
  decodeTabFlags(encodeTabFlags([oddPath], { [oddPath]: { dirty: true } })),
  { [oddPath]: { dirty: true, stale: false, loading: false } },
  "paths with brackets, parens and spaces survive the round trip"
);

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
