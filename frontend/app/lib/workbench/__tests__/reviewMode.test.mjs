// W5.3 (Build Workbench plan) — tests for reviewMode.js.
//
// Loads the REAL lib/workbench/reviewMode.js through loadSource.mjs (no
// pasted copy). No `imports` are passed on purpose: reviewMode.js must
// stay dependency-free (same rule as fileTree.js/tabUtils.js — see that
// file's own header), and an added `import` line would make this load
// fail loudly.
//
// Run: node frontend/app/lib/workbench/__tests__/reviewMode.test.mjs
import { loadSource } from "./loadSource.mjs";

const {
  chunkLineStats,
  reviewFilesFromProposal,
  decisionForFile,
  buildDecisions,
  keepAllDecisions,
  rejectAllDecisions,
  fileDiffStats,
  reviewProgress,
  dirtyOverlap,
  unreviewableReason,
  resolvedMessage,
} = loadSource("../reviewMode.js");

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

// A minimal stand-in for CM6's `Text` — exactly the subset chunkLineStats()
// uses: `.lineAt(pos).number`, 1-based, computed from `\n` offsets.
function fakeDoc(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text[i] === "\n") starts.push(i + 1);
  return {
    lineAt(pos) {
      let number = 1;
      for (let i = 1; i < starts.length; i++) {
        if (starts[i] > pos) break;
        number++;
      }
      return { number };
    },
  };
}

// --- chunkLineStats -----------------------------------------------------

{
  // "line1\nline2\nline3\n" -> "line1\nCHANGED\nline3\n": one line swapped
  // for one line, at line 2 on both sides.
  const original = fakeDoc("line1\nline2\nline3\n");
  const doc = fakeDoc("line1\nCHANGED\nline3\n");
  const chunks = [{ fromA: 6, toA: 12, endA: 11, fromB: 6, toB: 14, endB: 13 }];
  assertEqual(chunkLineStats(chunks, original, doc), { added: 1, removed: 1 }, "a one-line swap is +1/-1");
}
{
  // A pure insertion: nothing removed (fromA === toA).
  const original = fakeDoc("a\nb\n");
  const doc = fakeDoc("a\nNEW\nb\n");
  const chunks = [{ fromA: 2, toA: 2, endA: 1, fromB: 2, toB: 6, endB: 5 }];
  assertEqual(chunkLineStats(chunks, original, doc), { added: 1, removed: 0 }, "a pure insertion has removed: 0");
}
{
  // A pure deletion: nothing added (fromB === toB).
  const original = fakeDoc("a\nGONE\nb\n");
  const doc = fakeDoc("a\nb\n");
  const chunks = [{ fromA: 2, toA: 7, endA: 6, fromB: 2, toB: 2, endB: 1 }];
  assertEqual(chunkLineStats(chunks, original, doc), { added: 0, removed: 1 }, "a pure deletion has added: 0");
}
assertEqual(chunkLineStats([], fakeDoc(""), fakeDoc("")), { added: 0, removed: 0 }, "no chunks -> no lines");
assertEqual(chunkLineStats(undefined, fakeDoc(""), fakeDoc("")), { added: 0, removed: 0 }, "chunkLineStats(undefined) doesn't throw");
{
  // Two separate chunks in one file both count toward the total. Each
  // chunk here covers exactly one line, so its own `endA`/`endB` is the
  // same offset as its `fromA`/`fromB` — trivially "the same line" —
  // rather than needing to land on some other in-line offset.
  const original = fakeDoc("a\nb\nc\nd\n");
  const doc = fakeDoc("A\nb\nC\nd\n");
  const chunks = [
    { fromA: 0, toA: 2, endA: 0, fromB: 0, toB: 2, endB: 0 },
    { fromA: 4, toA: 6, endA: 4, fromB: 4, toB: 6, endB: 4 },
  ];
  assertEqual(chunkLineStats(chunks, original, doc), { added: 2, removed: 2 }, "two separate chunks both count toward the total");
}

// --- reviewFilesFromProposal ---------------------------------------------

{
  const built = reviewFilesFromProposal({
    files: [
      { path: "a.py", op: "replace", base_version: 3, original: "old\n", proposed: "new\n" },
      { path: "new.py", op: "create", base_version: 0, original: "", proposed: "x = 1\n" },
      { path: "gone.py", op: "delete", base_version: 2, original: "bye\n", proposed: "" },
    ],
  });
  assertEqual(built.order, ["a.py", "new.py", "gone.py"], "order matches the proposal's files, in order");
  assertEqual(built.files["a.py"], {
    op: "replace",
    baseVersion: 3,
    original: "old\n",
    proposed: "new\n",
    current: "new\n",
    remaining: null,
    added: 0,
    removed: 0,
  }, "a file's initial state: current === proposed, remaining null until an editor reports");
  assertEqual(built.files["new.py"].op, "create", "op is carried through");
  assertEqual(built.files["gone.py"].op, "delete", "...for every op");
}
assertEqual(reviewFilesFromProposal({}), { order: [], files: {} }, "a proposal with no files array -> empty review");
assertEqual(reviewFilesFromProposal(null), { order: [], files: {} }, "reviewFilesFromProposal(null) doesn't throw");
{
  const built = reviewFilesFromProposal({ files: [{ path: "a.py", proposed: "x" }, { path: "a.py", proposed: "y" }] });
  assertEqual(built.order, ["a.py"], "a duplicate path in `files` isn't added to order twice");
}
{
  const built = reviewFilesFromProposal({ files: [{ path: "a.py", original: "x\n", proposed: "y\n" }] });
  assertEqual(built.files["a.py"].op, "replace", "a file with no `op` defaults to replace");
  assertEqual(built.files["a.py"].baseVersion, 0, "a file with no base_version defaults to 0");
}

// --- decisionForFile ------------------------------------------------------

assertEqual(
  decisionForFile({ op: "replace", original: "old\n", proposed: "new\n", final: "old\n" }),
  { decision: "undo", finalContent: null },
  "final === original -> undo, nothing was kept"
);
assertEqual(
  decisionForFile({ op: "replace", original: "old\n", proposed: "new\n", final: "new\n" }),
  { decision: "keep", finalContent: null },
  "final === proposed -> keep with NO finalContent (server writes its own stored copy byte for byte)"
);
assertEqual(
  decisionForFile({ op: "replace", original: "a\nb\nc\n", proposed: "A\nb\nC\n", final: "A\nb\nc\n" }),
  { decision: "keep", finalContent: "A\nb\nc\n" },
  "a mix of kept and undone hunks -> keep WITH the merged text"
);
assertEqual(
  decisionForFile({ op: "replace", original: "a\r\nb\r\n", proposed: "A\r\nb\r\n", final: "A\nb\n" }),
  { decision: "keep", finalContent: null },
  "CRLF original/proposed vs. CM6's always-LF final text still compares equal (eol() normalizes both sides)"
);
assertEqual(
  decisionForFile({ op: "create", original: "", proposed: "x = 1\n", final: "x = 1\n" }),
  { decision: "keep", finalContent: null },
  "create, fully kept -> keep"
);
assertEqual(
  decisionForFile({ op: "create", original: "", proposed: "x = 1\n", final: "" }),
  { decision: "undo", finalContent: null },
  "create, fully undone -> undo (don't create the file)"
);
assertEqual(
  decisionForFile({ op: "delete", original: "bye\n", proposed: "", final: "" }),
  { decision: "keep", finalContent: null },
  "delete, nothing brought back -> keep (delete the file)"
);
assertEqual(
  decisionForFile({ op: "delete", original: "bye\n", proposed: "", final: "bye\n" }),
  { decision: "undo", finalContent: null },
  "delete with everything undone (brought back) -> undo (keep the file)"
);
assertEqual(
  decisionForFile({ op: "delete", original: "line1\nline2\n", proposed: "", final: "line1\n" }),
  { decision: "undo", finalContent: null },
  "delete with SOME lines brought back is still undo, never keep-with-finalContent — the server's delete ignores finalContent entirely"
);

// --- buildDecisions ---------------------------------------------------

{
  const review = {
    order: ["a.py", "gone.py"],
    files: {
      "a.py": { op: "replace", original: "old\n", proposed: "new\n", current: "new\n" },
      "gone.py": { op: "delete", original: "bye\n", proposed: "", current: "" },
    },
  };
  assertEqual(
    buildDecisions(review),
    [
      { path: "a.py", decision: "keep", finalContent: null },
      { path: "gone.py", decision: "keep", finalContent: null },
    ],
    "buildDecisions maps every file in order through decisionForFile"
  );
}
assertEqual(buildDecisions(null), [], "buildDecisions(null) doesn't throw");

// --- reviewProgress -----------------------------------------------------

assertEqual(reviewProgress(null), { ready: false, remaining: 0, filesLeft: 0, allResolved: false }, "no review -> not ready, nothing resolved");
{
  const loading = {
    order: ["a.py", "b.py"],
    files: { "a.py": { remaining: null }, "b.py": { remaining: null } },
  };
  assertEqual(reviewProgress(loading), { ready: false, remaining: 0, filesLeft: 0, allResolved: false }, "any file still null -> not ready");
}
{
  const partial = {
    order: ["a.py", "b.py"],
    files: { "a.py": { remaining: 2 }, "b.py": { remaining: 0 } },
  };
  assertEqual(reviewProgress(partial), { ready: true, remaining: 2, filesLeft: 1, allResolved: false }, "ready, some hunks left in one file -> not allResolved");
}
{
  const done = {
    order: ["a.py", "b.py"],
    files: { "a.py": { remaining: 0 }, "b.py": { remaining: 0 } },
  };
  assertEqual(reviewProgress(done), { ready: true, remaining: 0, filesLeft: 0, allResolved: true }, "every file at 0 remaining -> allResolved");
}
{
  // A file's editor hasn't mounted at all yet (no entry in `files`) — same as remaining: null.
  const missing = { order: ["a.py"], files: {} };
  assertEqual(reviewProgress(missing), { ready: false, remaining: 0, filesLeft: 0, allResolved: false }, "a listed path missing from files[] -> not ready, doesn't throw");
}

// --- dirtyOverlap ---------------------------------------------------------

assertEqual(dirtyOverlap(null, {}), [], "no review -> no overlap");
assertEqual(
  dirtyOverlap({ order: ["a.py", "b.py", "c.py"] }, { "a.py": { dirty: true }, "b.py": { dirty: false }, "c.py": {} }),
  ["a.py"],
  "dirtyOverlap returns only the review's files whose buffer is dirty"
);
assertEqual(dirtyOverlap({ order: ["a.py"] }, {}), [], "a file with no open buffer at all isn't dirty");

// --- unreviewableReason ----------------------------------------------------

assertEqual(unreviewableReason({ status: "pending", files: [{ path: "a.py" }] }), null, "a pending proposal with files can be reviewed");
assertEqual(
  unreviewableReason({ status: "pending", files: [] }),
  "This edit doesn't change any files.",
  "a pending proposal with no files can't be reviewed"
);
assertEqual(unreviewableReason({ status: "accepted" }), "This edit was already applied.", "accepted");
assertEqual(unreviewableReason({ status: "rejected" }), "This edit was already discarded.", "rejected");
assertEqual(unreviewableReason({ status: "partial" }), "This edit was already partly applied.", "partial");
assertEqual(
  unreviewableReason({ status: "stale" }),
  "This edit is out of date — a file changed after it was proposed. Ask for it again.",
  "stale"
);
assertEqual(unreviewableReason({ status: "failed" }), "That edit couldn't be generated. Try asking again.", "failed");
assertEqual(unreviewableReason({ status: "something-new" }), "This edit can't be opened for review.", "an unrecognized status gets a generic reason, not a throw");
assertEqual(unreviewableReason({}), "This edit can't be opened for review.", "no status at all -> generic reason");

// --- resolvedMessage -----------------------------------------------------

assertEqual(resolvedMessage("accepted"), "Applied the AI edit.", "accepted");
assertEqual(resolvedMessage("partial"), "Applied part of the AI edit.", "partial");
assertEqual(resolvedMessage("rejected"), "Discarded the AI edit — nothing was changed.", "rejected");
assertEqual(resolvedMessage("something-else"), "Finished reviewing the AI edit.", "an unrecognized status still gets a sensible message");

// --- keepAllDecisions / rejectAllDecisions (W5.4) -------------------------

{
  const proposal = {
    files: [
      { path: "a.py", op: "replace", original: "def greet():\n    pass\n", proposed: "def greet():\n    \"\"\"Says hi.\"\"\"\n    pass\n" },
      { path: "new.py", op: "create", original: "", proposed: "x = 1\n" },
      { path: "gone.py", op: "delete", original: "old\n", proposed: "" },
    ],
  };

  assertEqual(
    keepAllDecisions(proposal),
    [
      { path: "a.py", decision: "keep", finalContent: null },
      { path: "new.py", decision: "keep", finalContent: null },
      { path: "gone.py", decision: "keep", finalContent: null },
    ],
    "keepAllDecisions keeps every file exactly as proposed, with no finalContent"
  );

  assertEqual(
    rejectAllDecisions(proposal),
    [
      { path: "a.py", decision: "undo", finalContent: null },
      { path: "new.py", decision: "undo", finalContent: null },
      { path: "gone.py", decision: "undo", finalContent: null },
    ],
    "rejectAllDecisions undoes every file, including a delete (nothing is removed)"
  );
}

// --- fileDiffStats (W5.4) --------------------------------------------------

assertEqual(fileDiffStats("a\nb\nc\n", "a\nb\nc\n"), { added: 0, removed: 0 }, "identical text -> no changes");
assertEqual(fileDiffStats("", "x = 1\n"), { added: 1, removed: 0 }, "a brand-new file (empty original) is all additions");
assertEqual(fileDiffStats("old\n", ""), { added: 0, removed: 1 }, "a deleted file (empty proposed) is all removals");
assertEqual(
  fileDiffStats("one\ntwo\nthree\nfour\n", "one\nTWO\nTHREE\nfour\n"),
  { added: 2, removed: 2 },
  "a changed middle block, same length -> one-for-one added/removed, common prefix/suffix excluded"
);
assertEqual(
  fileDiffStats("one\ntwo\nthree\n", "one\ntwo\nextra\nthree\n"),
  { added: 1, removed: 0 },
  "a pure insertion in the middle counts only as added"
);
assertEqual(
  fileDiffStats("one\r\ntwo\r\n", "one\r\ntwo\r\n"),
  { added: 0, removed: 0 },
  "CRLF line endings normalize the same way decisionForFile()'s eol() does"
);

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
