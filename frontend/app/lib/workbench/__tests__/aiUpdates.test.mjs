// W8.2 (Build Workbench plan) — tests for aiUpdates.js.
//
// Loads the REAL lib/workbench/aiUpdates.js through loadSource.mjs (no
// pasted copy). No `imports` are passed on purpose: aiUpdates.js must
// stay dependency-free, and this load fails if someone adds an `import`.
//
// The event fixtures use the backend's real envelope (relay/emitter.py's
// emit_workspace_event()): the paths live under `payload`, and `agent`
// sits on the envelope itself.
//
// Run: node frontend/app/lib/workbench/__tests__/aiUpdates.test.mjs
import { loadSource } from "./loadSource.mjs";

const {
  PIPELINE_AGENT,
  MAX_AI_UPDATE_FILES,
  isPipelineWrite,
  extractFileUpdate,
  planAiUpdateBaselines,
  buildAiUpdateEntries,
  mergeAiUpdates,
  describeAiUpdate,
  visibleAiUpdates,
  aiUpdateTitle,
  aiUpdateSubtitle,
} = loadSource("../aiUpdates.js");

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

// --- isPipelineWrite ----------------------------------------------------

assertEqual(PIPELINE_AGENT, "code_writers", "pipeline agent matches task_runner.py's emit");
assertEqual(
  ["code_writers", "code_editor", null, undefined, "", "CODE_WRITERS"].map(isPipelineWrite),
  [true, false, false, false, false, false],
  "only the chat run's write-back counts; a kept proposal (code_editor) does not"
);

// --- extractFileUpdate --------------------------------------------------

assertEqual(
  extractFileUpdate({
    type: "code_file_updated",
    workspace_id: "ws1",
    agent: "code_writers",
    payload: { file_path: "b.js", file_paths: ["a.js", "b.js"], workspace_id: "ws1" },
  }),
  { filePaths: ["a.js", "b.js"], agent: "code_writers" },
  "reads file_paths and agent from the real envelope"
);
assertEqual(
  extractFileUpdate({ agent: "code_writers", payload: { file_path: "only.js" } }),
  { filePaths: ["only.js"], agent: "code_writers" },
  "falls back to the singular file_path"
);
assertEqual(
  extractFileUpdate({ agent: "code_writers", payload: { file_path: "last.js", file_paths: [] } }),
  { filePaths: ["last.js"], agent: "code_writers" },
  "an empty file_paths list falls back to file_path"
);
assertEqual(
  extractFileUpdate({ agent: "code_editor", file_paths: ["flat.js"] }),
  { filePaths: ["flat.js"], agent: "code_editor" },
  "a flattened message (no payload key) still works"
);
assertEqual(
  extractFileUpdate({ payload: { file_paths: ["a.js", "a.js", "", 7, null, "b.js"] } }),
  { filePaths: ["a.js", "b.js"], agent: null },
  "drops duplicates and non-string/empty entries; missing agent is null"
);
assertEqual(
  [extractFileUpdate(null), extractFileUpdate(undefined), extractFileUpdate("x"), extractFileUpdate({})],
  Array(4).fill({ filePaths: [], agent: null }),
  "junk input yields an empty update, never throws"
);
assertEqual(
  extractFileUpdate({ truncated: true, agent: "code_writers", payload: {} }),
  { filePaths: [], agent: "code_writers" },
  "a shrunk placeholder payload yields no paths"
);

// --- planAiUpdateBaselines ----------------------------------------------

const buffers = {
  "a.js": { saved: "A0", edited: "A0" }, // clean
  "b.js": { saved: "B0", edited: "B-mine" }, // dirty
  "c.js": { saved: "C0", edited: "C0" }, // loaded but not in tabs below
};
assertEqual(
  planAiUpdateBaselines({ paths: ["a.js", "b.js", "c.js", "gone.js"], tabs: ["a.js", "b.js", "gone.js"], buffers }),
  [
    { path: "a.js", before: "A0", saved: "A0" },
    { path: "b.js", before: "B-mine", saved: "B0" },
  ],
  "keeps only paths open in a tab with a loaded buffer; captures your text and its base"
);
assertEqual(
  planAiUpdateBaselines({ paths: ["a.js", "a.js"], tabs: ["a.js"], buffers }),
  [{ path: "a.js", before: "A0", saved: "A0" }],
  "a path named twice is captured once"
);
assertEqual(
  planAiUpdateBaselines({ paths: ["x.js"], tabs: ["x.js"], buffers: { "x.js": { saved: "s" } } }),
  [],
  "a buffer with no edited text yet is skipped"
);
{
  const many = Array.from({ length: MAX_AI_UPDATE_FILES + 5 }, (_, i) => `f${i}.js`);
  const manyBuffers = Object.fromEntries(many.map((p) => [p, { saved: "s", edited: "s" }]));
  assertEqual(
    planAiUpdateBaselines({ paths: many, tabs: many, buffers: manyBuffers }).length,
    MAX_AI_UPDATE_FILES,
    "capped so one run can't trigger an unbounded number of reads"
  );
}

// --- buildAiUpdateEntries ------------------------------------------------

const baselines = [
  { path: "a.js", before: "A0", saved: "A0" },
  { path: "b.js", before: "B-mine", saved: "B0" },
  { path: "same.js", before: "S", saved: "S" },
  { path: "mine.js", before: "M-new", saved: "M0" },
  { path: "failed.js", before: "F", saved: "F" },
];
const fetched = {
  "a.js": { content: "A1", version: 3 },
  "b.js": { content: "B1", version: 5 },
  "same.js": { content: "S", version: 9 }, // rewritten with identical text
  "mine.js": { content: "M-new", version: 2 }, // already equals your edit
  "failed.js": null,
};
assertEqual(
  buildAiUpdateEntries(baselines, fetched),
  [
    { path: "a.js", before: "A0", theirs: "A1", version: 3 },
    { path: "b.js", before: "B-mine", theirs: "B1", version: 5 },
  ],
  "keeps real changes; drops identical rewrites, text that already matches yours, and failed reads"
);
assertEqual(
  buildAiUpdateEntries([{ path: "a.js", before: "A0", saved: "A0" }], { "a.js": { content: "A1" } })[0].version,
  null,
  "a missing version is null, not undefined"
);
assertEqual(buildAiUpdateEntries([], {}), [], "no baselines, no entries");

// --- mergeAiUpdates ------------------------------------------------------

{
  const first = [{ path: "a.js", before: "A0", theirs: "A1", version: 2 }];
  const second = [
    { path: "a.js", before: "A1", theirs: "A2", version: 3 },
    { path: "n.js", before: "N0", theirs: "N1", version: 1 },
  ];
  assertEqual(
    mergeAiUpdates(first, second),
    [
      { path: "a.js", before: "A0", theirs: "A2", version: 3 },
      { path: "n.js", before: "N0", theirs: "N1", version: 1 },
    ],
    "a repeat path keeps the original `before` and takes the newest `theirs`; new paths are appended"
  );
  assertEqual(mergeAiUpdates([], second), second, "merging into nothing returns the incoming entries");
}

// --- describeAiUpdate / visibleAiUpdates ----------------------------------

const entry = { path: "a.js", before: "A0", theirs: "A1", version: 3 };
assertEqual(
  describeAiUpdate(entry, { dirty: false, stale: false, edited: "A1" }),
  { path: "a.js", state: "reloaded", base: "A0", theirs: "A1", version: 3 },
  "clean buffer: reloaded, compared against what you had"
);
assertEqual(
  describeAiUpdate(entry, { dirty: true, stale: true, edited: "A0-typing" }),
  { path: "a.js", state: "kept-yours", base: "A0-typing", theirs: "A1", version: 3 },
  "dirty + stale: your edits kept, compared against your CURRENT text"
);
assertEqual(
  describeAiUpdate(entry, { dirty: true, stale: false, edited: "A0-x" }).state,
  "kept-yours",
  "dirty but not (yet) flagged stale still reads as kept — the race with the sync flag can't show 'reloaded'"
);
assertEqual(
  describeAiUpdate(entry, { dirty: false, stale: true, edited: "A0" }).state,
  "kept-yours",
  "stale alone counts as kept"
);
assertEqual(describeAiUpdate(entry, undefined), null, "closed tab: nothing to show");
assertEqual(describeAiUpdate(entry, {}), null, "buffer with no text: nothing to show");
assertEqual(
  describeAiUpdate(entry, { dirty: true, stale: true, edited: "A1" }),
  null,
  "once your text equals the AI's there is no diff to show"
);
assertEqual(
  describeAiUpdate({ path: "a.js", before: "A1", theirs: "A1", version: 4 }, { dirty: false, edited: "A1" }),
  null,
  "a merged entry whose net change is zero is hidden"
);
assertEqual(
  visibleAiUpdates(
    [entry, { path: "b.js", before: "B0", theirs: "B1", version: 2 }, { path: "closed.js", before: "x", theirs: "y", version: 1 }],
    { "a.js": { edited: "A1" }, "b.js": { dirty: true, stale: true, edited: "B-mine" } }
  ).map((i) => [i.path, i.state]),
  [
    ["a.js", "reloaded"],
    ["b.js", "kept-yours"],
  ],
  "visibleAiUpdates preserves order and drops entries whose tab is gone"
);

// --- wording -------------------------------------------------------------

assertEqual([aiUpdateTitle(1), aiUpdateTitle(4)], ["AI updated 1 file", "AI updated 4 files"], "title pluralizes");
assertEqual(
  aiUpdateSubtitle([{ state: "reloaded" }, { state: "reloaded" }]),
  "Open files were reloaded with the new version.",
  "subtitle: all reloaded"
);
assertEqual(
  aiUpdateSubtitle([{ state: "kept-yours" }]),
  "Your unsaved edits were kept — the new version wasn't applied.",
  "subtitle: one kept"
);
assertEqual(
  aiUpdateSubtitle([{ state: "kept-yours" }, { state: "kept-yours" }]),
  "Your unsaved edits were kept — the new versions weren't applied.",
  "subtitle: all kept, plural"
);
assertEqual(
  aiUpdateSubtitle([{ state: "kept-yours" }, { state: "reloaded" }, { state: "reloaded" }]),
  "1 with unsaved edits kept; the rest were reloaded.",
  "subtitle: mixed"
);

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
}
console.log("\nAll aiUpdates tests passed.");
