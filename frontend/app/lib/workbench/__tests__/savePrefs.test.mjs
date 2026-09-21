// W2.5 (Build Workbench plan) — tests for savePrefs.js.
//
// Loads the REAL lib/workbench/savePrefs.js through loadSource.mjs (no
// pasted copy). No `imports` are passed on purpose: savePrefs.js takes its
// storage object as an argument and must stay dependency-free, and this
// load fails if someone adds an `import` to it.
//
// Run: node frontend/app/lib/workbench/__tests__/savePrefs.test.mjs
import { loadSource } from "./loadSource.mjs";

const { DEFAULT_SAVE_PREFS, normalizeSavePrefs, sameSavePrefs, loadSavePrefs, saveSavePrefs } =
  loadSource("../savePrefs.js");

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

// A minimal in-memory localStorage.
function fakeStorage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
    removeItem: (k) => {
      delete data[k];
    },
  };
}

// --- defaults -----------------------------------------------------------

assertEqual(DEFAULT_SAVE_PREFS, { autosave: false, formatOnSave: false }, "both toggles default to OFF");
assertEqual(Object.isFrozen(DEFAULT_SAVE_PREFS), true, "DEFAULT_SAVE_PREFS is frozen");

// --- normalizeSavePrefs -------------------------------------------------

assertEqual(normalizeSavePrefs(undefined), DEFAULT_SAVE_PREFS, "undefined normalizes to the defaults");
assertEqual(normalizeSavePrefs(null), DEFAULT_SAVE_PREFS, "null normalizes to the defaults");
assertEqual(normalizeSavePrefs("on"), DEFAULT_SAVE_PREFS, "a string normalizes to the defaults");
assertEqual(normalizeSavePrefs([true, true]), DEFAULT_SAVE_PREFS, "an array normalizes to the defaults");
assertEqual(normalizeSavePrefs({}), DEFAULT_SAVE_PREFS, "an empty object normalizes to the defaults");
assertEqual(
  normalizeSavePrefs({ autosave: true, formatOnSave: true }),
  { autosave: true, formatOnSave: true },
  "valid prefs pass through unchanged"
);
assertEqual(
  normalizeSavePrefs({ autosave: "yes", formatOnSave: 1 }),
  DEFAULT_SAVE_PREFS,
  "wrong-typed fields each fall back to their default (truthy strings/numbers are NOT accepted as true)"
);
assertEqual(
  normalizeSavePrefs({ autosave: true, formatOnSave: "yes" }),
  { autosave: true, formatOnSave: false },
  "one bad field doesn't discard the good one"
);
assertEqual(
  Object.keys(normalizeSavePrefs({ autosave: true, extra: 1 })).sort(),
  ["autosave", "formatOnSave"],
  "unknown keys are dropped"
);
assertEqual(
  normalizeSavePrefs({ formatOnSave: "yes" }, { autosave: true, formatOnSave: true }),
  { autosave: true, formatOnSave: true },
  "with a fallback, missing/invalid fields take the fallback's values instead of the defaults"
);
assertEqual(
  normalizeSavePrefs({ autosave: false }, { autosave: true, formatOnSave: true }),
  { autosave: false, formatOnSave: true },
  "...and valid fields still win over the fallback"
);
assertEqual(
  Object.isFrozen(normalizeSavePrefs(null)),
  false,
  "normalizeSavePrefs returns a fresh object callers may own, not the frozen defaults"
);

assertEqual(sameSavePrefs(normalizeSavePrefs(null), DEFAULT_SAVE_PREFS), true, "sameSavePrefs: equal");
assertEqual(sameSavePrefs({ autosave: true, formatOnSave: false }, DEFAULT_SAVE_PREFS), false, "sameSavePrefs: autosave differs");
assertEqual(sameSavePrefs({ autosave: false, formatOnSave: true }, DEFAULT_SAVE_PREFS), false, "sameSavePrefs: formatOnSave differs");

// --- storage ------------------------------------------------------------

let store = fakeStorage();
assertEqual(loadSavePrefs(store), DEFAULT_SAVE_PREFS, "nothing stored loads as the defaults");
assertEqual(loadSavePrefs(null), DEFAULT_SAVE_PREFS, "no storage at all (SSR / blocked) loads as the defaults");

saveSavePrefs(store, { autosave: true, formatOnSave: false });
assertEqual(Object.keys(store.data).length, 1, "a non-default value writes exactly one key");
assertEqual(
  loadSavePrefs(store),
  { autosave: true, formatOnSave: false },
  "saved prefs load back identically"
);

const keyUsed = Object.keys(store.data)[0];
saveSavePrefs(store, { autosave: true, formatOnSave: true });
assertEqual(Object.keys(store.data), [keyUsed], "the same single key is reused — prefs are workbench-wide, not per workspace");

saveSavePrefs(store, DEFAULT_SAVE_PREFS);
assertEqual(Object.keys(store.data), [], "saving the defaults removes the key instead of writing it");

store = fakeStorage();
saveSavePrefs(store, { autosave: true, junk: "x" });
assertEqual(
  JSON.parse(store.data[Object.keys(store.data)[0]]),
  { autosave: true, formatOnSave: false },
  "what's written is the normalized prefs, never the caller's raw object"
);

store = fakeStorage({ minime_build_editor_save_prefs: "{not json" });
assertEqual(loadSavePrefs(store), DEFAULT_SAVE_PREFS, "an unparseable saved value loads as the defaults");
store = fakeStorage({ minime_build_editor_save_prefs: JSON.stringify({ autosave: "true", formatOnSave: true }) });
assertEqual(
  loadSavePrefs(store),
  { autosave: false, formatOnSave: true },
  "a saved value with one wrong-typed field keeps the good field"
);

// Storage that throws (quota exceeded, Safari private mode) is swallowed, never surfaced.
const throwing = {
  getItem: () => {
    throw new Error("blocked");
  },
  setItem: () => {
    throw new Error("quota");
  },
  removeItem: () => {
    throw new Error("blocked");
  },
};
assertEqual(loadSavePrefs(throwing), DEFAULT_SAVE_PREFS, "a throwing getItem loads as the defaults");
let threw = false;
try {
  saveSavePrefs(throwing, { autosave: true, formatOnSave: true });
  saveSavePrefs(throwing, DEFAULT_SAVE_PREFS);
  saveSavePrefs(null, { autosave: true, formatOnSave: true });
} catch {
  threw = true;
}
assertEqual(threw, false, "saving never throws — a failed write isn't worth an error");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
