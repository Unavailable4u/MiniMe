// W8.4 (Build Workbench plan) — tests for lib/workbench/instructionStep.js.
// Run: node frontend/app/lib/workbench/__tests__/instructionStep.test.mjs
//
// instructionStep.js is import-free on purpose (see its header), so it
// loads with NO `imports` map — an added `import` line would make this
// test fail loudly.
import { loadSource } from "./loadSource.mjs";

const { tokenize, isCandidatePath, scorePath, findEntryFile, pickStepFile, stepDraftText, planWorkOnStep } = loadSource("../instructionStep.js");

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
function assert(cond, msg) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}

// --- tokenize ------------------------------------------------------------------
assertEqual(tokenize("Flash the firmware to the ESP32"), ["flash", "firmware", "esp32"], "stopwords dropped, digits inside a token kept");
assertEqual(tokenize("mcu_1"), ["mcu"], "a trailing numeric id part is dropped");
assertEqual(tokenize("3d_printer"), ["printer"], "tokens under 3 chars are dropped");
assertEqual(tokenize("sensorReader"), ["sensor", "reader"], "camelCase is split");
assertEqual(tokenize("sensors Sensor"), ["sensor"], "plural folds to singular and the result is deduped");
assertEqual(tokenize("address"), ["address"], "a double-s word is not de-pluralised");
assertEqual(tokenize(""), [], "empty string -> no tokens");
assertEqual([tokenize(null), tokenize(undefined), tokenize(5)], [[], [], []], "non-strings -> no tokens");

// --- isCandidatePath -----------------------------------------------------------
assertEqual(
  ["src/main.py", "firmware/main.ino", "README.md", "a/b/.gitkeep", "node_modules/x/index.js", "package-lock.json", "img/logo.png", "dist/app.js", "static/app.min.js", "parts/case.stl", "", null].map(isCandidatePath),
  [true, true, true, false, false, false, false, false, false, false, false, false],
  "source/text files are candidates; placeholders, deps, lockfiles, build output, binaries and minified files are not"
);

// --- scorePath -----------------------------------------------------------------
assertEqual(scorePath("src/sensor_reader.py", ["sensor"]), 3, "a hit on the file name scores 3");
assertEqual(scorePath("sensor/main.py", ["sensor"]), 1, "a hit only on a directory scores 1");
assertEqual(scorePath("src/sensor_reader.py", ["sensor", "reader"]), 6, "every matching word adds up");
assertEqual(scorePath("src/main.py", ["sensor"]), 0, "no overlap scores 0");
assertEqual(scorePath("src/sensor/sensor.py", ["sensor"]), 3, "a word counts once, as a name hit (not name + dir)");

// --- findEntryFile -------------------------------------------------------------
assertEqual(findEntryFile(["util.py", "src/main.py", "main.py"]), "main.py", "main.* wins, shallowest copy first");
assertEqual(findEntryFile(["app.js", "index.html"]), "index.html", "index outranks app");
assertEqual(findEntryFile(["lib.cpp", "blink/blink.ino"]), "blink/blink.ino", "a .ino file counts as the sketch entry");
assertEqual(findEntryFile(["util.py", "notes.md"]), null, "no entry-like file -> null");
assertEqual(findEntryFile([]), null, "empty project -> null");
assertEqual(findEntryFile(["node_modules/x/index.js"]), null, "an excluded path is never an entry file");

// --- pickStepFile --------------------------------------------------------------
const files = ["src/main.py", "src/sensor_reader.py", "src/display.py", "README.md", "firmware/main.ino"];

assertEqual(
  pickStepFile({ title: "Wire up the sensor reader", part_ids: [], tool_ids: [] }, files),
  { path: "src/sensor_reader.py", reason: "name" },
  "title words matching a file name -> that file, reason 'name'"
);
assertEqual(
  pickStepFile({ title: "Mount the module", part_ids: ["display_1"], tool_ids: [] }, files),
  { path: "src/display.py", reason: "name" },
  "a part id can match a file name too"
);
assertEqual(
  pickStepFile({ title: "Flash the firmware", part_ids: ["mcu_1"], tool_ids: [] }, files),
  { path: "firmware/main.ino", reason: "entry" },
  "a software step with no name match falls back to the entry file (two main.* tie on rank; alphabetical wins, so firmware/ before src/)"
);
assertEqual(
  pickStepFile({ title: "Sand the lid smooth", part_ids: ["lid_1"], tool_ids: ["sandpaper"] }, files),
  null,
  "a purely physical step -> no file (we don't invent one)"
);
assertEqual(
  pickStepFile({ title: "Assemble the case", part_ids: [], tool_ids: [] }, ["src/main.py", "src/display.py"], { phaseName: "Program" }),
  { path: "src/main.py", reason: "entry" },
  "the phase name makes a step a software step for the fallback"
);
assertEqual(
  pickStepFile({ title: "Assemble the case", part_ids: [], tool_ids: [] }, ["src/main.py", "src/display.py"]),
  null,
  "…without it the same step has no file"
);
assertEqual(
  pickStepFile({ title: "Sand the lid", part_ids: [], tool_ids: [] }, files, { phaseName: "Fabricate" }),
  null,
  "…but a physical phase name doesn't"
);
assertEqual(
  pickStepFile({ title: "Wire the sensor", part_ids: [], tool_ids: [] }, ["sensor/readme.md", "src/main.py"]),
  null,
  "a directory-only hit (score 1) is not confident enough to pick a file"
);
assertEqual(pickStepFile({ title: "Flash the firmware" }, []), null, "empty project -> null");
assertEqual(pickStepFile({ title: "Flash the firmware" }, ["a/.gitkeep"]), null, "only placeholders -> null");
assertEqual(pickStepFile({}, files), null, "a step with no title/ids -> null");
assertEqual(pickStepFile(null, files), null, "a missing step -> null");
assertEqual(
  pickStepFile({ title: "Update the display" }, ["b/display.py", "a/display.py"]),
  { path: "a/display.py", reason: "name" },
  "equal scores resolve deterministically (shallower, then alphabetical)"
);
assertEqual(
  pickStepFile({ title: "Update the display" }, ["x/y/display.py", "display.py"]),
  { path: "display.py", reason: "name" },
  "…a shallower path beats a deeper one"
);

// --- stepDraftText -------------------------------------------------------------
assertEqual(
  stepDraftText({ title: " Flash the firmware ", part_ids: ["mcu_1"], tool_ids: ["usb_cable"] }, "Program"),
  'Work on this build step (Program phase): "Flash the firmware". Parts: mcu_1. Tools: usb_cable.',
  "draft names the phase, step, parts and tools"
);
assertEqual(stepDraftText({ title: "Sand the lid", part_ids: [], tool_ids: [] }, ""), 'Work on this build step: "Sand the lid".', "no phase / parts / tools -> just the step");
assertEqual(stepDraftText({ title: "X", part_ids: [null, "", 5, "a"] }, null), 'Work on this build step: "X". Parts: a.', "junk ids are ignored");

// --- planWorkOnStep ------------------------------------------------------------
const confident = planWorkOnStep({ step: { id: "s1", title: "Wire up the sensor reader", part_ids: [], tool_ids: [] }, phaseName: "Wiring", paths: files });
assertEqual([confident.path, confident.reason, confident.mode], ["src/sensor_reader.py", "name", "edit"], "a name match opens in Edit mode");
assert(confident.draft.includes("Wire up the sensor reader") && confident.draft.includes("Wiring phase"), "…with the step and phase in the draft");

const guess = planWorkOnStep({ step: { id: "s2", title: "Flash the firmware", part_ids: [], tool_ids: [] }, phaseName: "Program", paths: ["main.py"] });
assertEqual([guess.path, guess.reason, guess.mode], ["main.py", "entry", "ask"], "an entry-file guess opens in Ask mode (a wrong guess can't become an edit proposal)");

const none = planWorkOnStep({ step: { id: "s3", title: "Sand the lid", part_ids: [], tool_ids: [] }, phaseName: "Fabricate", paths: files });
assertEqual([none.path, none.reason, none.mode], [null, null, "ask"], "no file -> Ask mode, no path (Edit mode needs one)");
assert(none.draft.length > 0, "…but the draft is still produced");

const empty = planWorkOnStep({ step: { id: "s4", title: "Flash the firmware" }, phaseName: "Program", paths: [] });
assertEqual([empty.path, empty.mode], [null, "ask"], "an empty project -> no path, Ask mode");

// -------------------------------------------------------------------------------

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll instructionStep.js tests passed.");
}
