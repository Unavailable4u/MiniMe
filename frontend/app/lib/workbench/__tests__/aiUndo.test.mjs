// W8.1 (Build Workbench plan) — tests for aiUndo.js.
//
// Loads the REAL lib/workbench/aiUndo.js through loadSource.mjs (no
// pasted copy). No `imports` are passed on purpose: aiUndo.js must stay
// dependency-free, and this load fails if someone adds an `import` to it.
//
// The fixtures below follow the backend's real semantics: a history
// row's `source` is what REPLACED that content (see aiUndo.js's header),
// so `{version: 1, source: "proposal"}` means "v1, before an AI edit
// turned it into v2".
//
// Run: node frontend/app/lib/workbench/__tests__/aiUndo.test.mjs
import { loadSource } from "./loadSource.mjs";

const { AI_SOURCES, isAiSource, versionBadge, findUndoTarget, undoNeedsConfirm, undoLabel, describeLaterChanges } =
  loadSource("../aiUndo.js");

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

const row = (version, source, content) => ({ version, source, content, updated_at: null, updated_by: null });

// --- isAiSource / AI_SOURCES -------------------------------------------

assertEqual(AI_SOURCES, ["proposal", "pipeline"], "AI sources are exactly proposal + pipeline");
assertEqual(
  ["user", "proposal", "pipeline", "restore", null, undefined, "other"].map(isAiSource),
  [false, true, true, false, false, false, false],
  "only proposal and pipeline count as AI"
);

// --- versionBadge --------------------------------------------------------

assertEqual(versionBadge("proposal").label, "Before AI edit", "proposal badge reads 'Before AI edit'");
assertEqual(versionBadge("pipeline").label, "Before AI run", "pipeline badge reads 'Before AI run'");
assertEqual(versionBadge("user").label, "Before your save", "user badge reads 'Before your save'");
assertEqual(versionBadge("restore").label, "Before restore", "restore badge reads 'Before restore'");
assertEqual(
  ["proposal", "pipeline", "user", "restore"].map((s) => versionBadge(s).ai),
  [true, true, false, false],
  "only the AI sources are flagged ai"
);
assertEqual(versionBadge(null).label, "Earlier version", "a null source falls back to a neutral badge");
assertEqual(versionBadge("something-new").label, "Earlier version", "an unknown source falls back to a neutral badge");
assertEqual(
  ["user", "proposal", "pipeline", "restore", null].every((s) => versionBadge(s).title.length > 0),
  true,
  "every badge has a tooltip"
);

// --- findUndoTarget: the basic case --------------------------------------

// v1 saved by you; an accepted proposal turned it into v2 (live).
const basic = [row(1, "proposal", "A")];
assertEqual(
  findUndoTarget(basic, { currentVersion: 2, currentContent: "B" }),
  { entry: row(1, "proposal", "A"), source: "proposal", laterChanges: 0 },
  "restores the file as it was before the AI edit; the AI edit is the latest change"
);

assertEqual(findUndoTarget([], { currentVersion: 1, currentContent: "A" }), null, "empty history: nothing to undo");
assertEqual(
  findUndoTarget([row(1, "user", "A")], { currentVersion: 2, currentContent: "B" }),
  null,
  "history with only your own saves: nothing to undo"
);
assertEqual(
  findUndoTarget([row(1, "restore", "A")], { currentVersion: 2, currentContent: "B" }),
  null,
  "a restore is not an AI edit"
);
assertEqual(findUndoTarget(null, { currentVersion: 2, currentContent: "B" }), null, "history not loaded yet: null");
assertEqual(findUndoTarget(undefined, {}), null, "no arguments at all: null");

// --- which AI entry wins ------------------------------------------------

// v1 -[AI]-> v2 -[you]-> v3 -[AI]-> v4 (live). Newest AI edit is the one
// that replaced v3, regardless of the order the rows arrive in.
const chain = [row(3, "proposal", "C"), row(2, "user", "B"), row(1, "proposal", "A")];
assertEqual(
  findUndoTarget(chain, { currentVersion: 4, currentContent: "D" })?.entry.version,
  3,
  "picks the newest AI-replaced snapshot"
);
assertEqual(
  findUndoTarget([...chain].reverse(), { currentVersion: 4, currentContent: "D" })?.entry.version,
  3,
  "doesn't depend on the order the server returned the rows in"
);
assertEqual(
  findUndoTarget(
    [row(5, "user", "E"), row(4, "pipeline", "D"), row(3, "proposal", "C")],
    { currentVersion: 6, currentContent: "F" }
  )?.source,
  "pipeline",
  "a pipeline run counts too, and wins when it is the newest"
);

// --- laterChanges ---------------------------------------------------------

// v1 -[AI]-> v2 -[you]-> v3 -[you]-> v4 (live): two saves after the AI edit.
assertEqual(
  findUndoTarget([row(3, "user", "C"), row(2, "user", "B"), row(1, "proposal", "A")], {
    currentVersion: 4,
    currentContent: "D",
  })?.laterChanges,
  2,
  "counts the saves made after the AI edit"
);
assertEqual(
  findUndoTarget(basic, { currentVersion: undefined, currentContent: "B" })?.laterChanges,
  null,
  "unknown current version: laterChanges is null, not a guess"
);
assertEqual(
  findUndoTarget(basic, { currentVersion: 1, currentContent: "B" })?.laterChanges,
  0,
  "never negative if the current version looks older than the snapshot (stale prop)"
);

// --- the button goes away once used, and steps back after that -----------

// Start: v1 -[AI A1]-> v2 -[AI A2]-> v3 (live, content C).
const twoEdits = [row(2, "proposal", "B"), row(1, "proposal", "A")];
assertEqual(
  findUndoTarget(twoEdits, { currentVersion: 3, currentContent: "C" })?.entry.version,
  2,
  "two AI edits: first offer undoes the latest one"
);
// After undoing A2: restore appends v4 holding B; v3 (C) is snapshotted as "restore".
const afterUndo = [row(3, "restore", "C"), row(2, "proposal", "B"), row(1, "proposal", "A")];
const stepBack = findUndoTarget(afterUndo, { currentVersion: 4, currentContent: "B" });
assertEqual(stepBack?.entry.version, 1, "after using it once, the same click steps back to the AI edit before");
assertEqual(stepBack?.laterChanges, 2, "…and reports the saves since then (the later AI edit and the restore)");

// After undoing A1 too: restore appends v5 holding A.
const afterBoth = [row(4, "restore", "B"), ...afterUndo];
assertEqual(
  findUndoTarget(afterBoth, { currentVersion: 5, currentContent: "A" }),
  null,
  "nothing left to offer once every AI edit has been undone"
);

// Single AI edit, undone: no no-op offer.
assertEqual(
  findUndoTarget([row(2, "restore", "B"), row(1, "proposal", "A")], { currentVersion: 3, currentContent: "A" }),
  null,
  "right after the only AI edit is undone the button is gone (current text equals the snapshot)"
);

// An AI write that changed nothing isn't an edit to undo.
assertEqual(
  findUndoTarget([row(1, "proposal", "same")], { currentVersion: 2, currentContent: "same" }),
  null,
  "an AI edit that left the text unchanged is skipped"
);

// You reverted the AI edit by hand (v2 -> v3 holds the old text again):
// nothing to offer, even after you keep editing past that point.
assertEqual(
  findUndoTarget([row(2, "user", "B"), row(1, "proposal", "A")], { currentVersion: 3, currentContent: "A" }),
  null,
  "reverted by hand: the live text already equals the pre-AI text"
);
assertEqual(
  findUndoTarget([row(3, "user", "A"), row(2, "user", "B"), row(1, "proposal", "A")], {
    currentVersion: 4,
    currentContent: "A2",
  }),
  null,
  "reverted by hand, then edited further: the file was back at the pre-AI text, so the AI edit isn't offered"
);

// The AI edit before a reverted one is still offered.
// v1 -[AI]-> v2 -[you]-> v3 -[AI]-> v4 -[restore v3's text]-> v5 (live "C")
assertEqual(
  findUndoTarget([row(4, "restore", "D"), row(3, "proposal", "C"), row(2, "user", "B"), row(1, "proposal", "A")], {
    currentVersion: 5,
    currentContent: "C",
  })?.entry.version,
  1,
  "undoing the newest of two AI edits leaves the older one available"
);

// --- safety around missing / odd data ---------------------------------------

assertEqual(
  findUndoTarget(basic, { currentVersion: 2, currentContent: undefined }),
  null,
  "no saved content to compare against: no target"
);
assertEqual(findUndoTarget(basic, { currentVersion: 2, currentContent: null }), null, "null saved content: no target");
assertEqual(
  findUndoTarget(basic, { currentVersion: 2, currentContent: "" })?.entry.version,
  1,
  "an empty saved file is still a string, so it still compares"
);
assertEqual(
  findUndoTarget([{ version: 1, source: "proposal" }, null, { version: "2", source: "proposal", content: "x" }], {
    currentVersion: 3,
    currentContent: "B",
  }),
  null,
  "rows with no content, null rows and non-integer versions are ignored rather than crashing"
);

// --- undoNeedsConfirm ---------------------------------------------------------

assertEqual(undoNeedsConfirm({ dirty: false, laterChanges: 0 }), false, "clean buffer + AI edit is latest: one click, no dialog");
assertEqual(undoNeedsConfirm({ dirty: true, laterChanges: 0 }), true, "unsaved edits would be discarded: ask");
assertEqual(undoNeedsConfirm({ dirty: false, laterChanges: 2 }), true, "later saves would be rolled back too: ask");
assertEqual(undoNeedsConfirm({ dirty: false, laterChanges: null }), true, "unknown later saves: ask");
assertEqual(undoNeedsConfirm({ laterChanges: 0 }), false, "dirty omitted counts as clean");

// --- labels -------------------------------------------------------------------

assertEqual(undoLabel("proposal"), "Restore to before AI edit", "button label for a proposal");
assertEqual(undoLabel("pipeline"), "Restore to before AI run", "button label for a pipeline run");
assertEqual(
  [null, 0, 1, 2, 12, -3, undefined].map(describeLaterChanges),
  ["", "", "1 later save", "2 later saves", "12 later saves", "", ""],
  "describeLaterChanges pluralises and stays empty for none/unknown"
);

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
