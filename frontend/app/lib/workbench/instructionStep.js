// frontend/app/lib/workbench/instructionStep.js — W8.4 (Build Workbench
// plan). "Instructions ↔ editor": an InstructionChecklist step gets a
// "Work on this" button that opens the relevant file in the Editor and
// pre-fills the chat with a chip + a starter message.
//
// This file is the deterministic half of that: given a step and the
// project's file paths, decide WHICH file is relevant, WHAT to put in the
// composer, and WHICH chat mode to leave it in. No React, no fetch, no
// imports — same dependency-free shape as fileTree.js / tabUtils.js, so
// __tests__/instructionStep.test.mjs can load the real file under plain
// `node`. BuildTab.jsx's InstructionsView does the I/O around it (list
// the files, read the one picked, add the chip, open the Editor).
//
// What a step looks like (agents/hardware_speccer.py's JSON shape):
//   { id, title, tool_ids: [...], part_ids: [...], done }
// There is NO file field on it — these are assembly instructions
// ("3D print the enclosure halves", "Flash the firmware to the ESP32"),
// written before any code necessarily exists. So "the relevant file" is
// a best guess, and this module is deliberately honest about how good a
// guess it is:
//   - reason "name"  — the step's own words (title, part ids, tool ids)
//                      hit the file's NAME (`sensor_reader.py` for "Wire
//                      up the sensor reader"). A confident match.
//   - reason "entry" — nothing matched by name, but the step is plainly
//                      about the software ("Flash the firmware"), so the
//                      project's entry file (main.*, index.*, *.ino …) is
//                      offered. A guess.
//   - null           — a purely physical step ("Sand the lid") with no
//                      file in sight. We don't invent one.
// The reason decides the chat mode (planWorkOnStep below): a confident
// name match opens in Edit, a guess opens in Ask, so a wrong guess can't
// turn into an edit proposal against the wrong file.

// Words that carry no signal for matching a step to a file name.
const STOPWORDS = new Set([
  "the", "and", "for", "with", "into", "onto", "from", "this", "that", "then", "your",
  "all", "any", "use", "using", "make", "sure", "step", "each", "both", "also",
  "put", "place", "set", "get", "attach", "insert", "connect", "mount", "screw",
]);

// A step whose own words say "this is about the software", which is when
// falling back to the project's entry file is a reasonable guess.
const CODE_WORDS = new Set([
  "firmware", "code", "flash", "upload", "program", "programming", "sketch",
  "script", "software", "library", "config", "configure", "configuration",
  "compile", "calibrate", "calibration", "debug",
]);

// Entry-file basenames (extension stripped), best first.
const ENTRY_NAMES = ["main", "index", "app", "firmware", "sketch", "server"];

// Never offered: not something a build step is about, or not text.
const SKIP_DIRS = /(^|\/)(node_modules|dist|build|\.git|\.next|__pycache__)\//;
const SKIP_FILES = /(^|\/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock)$/;
const SKIP_EXT = /\.(png|jpe?g|gif|webp|ico|svg|pdf|zip|gz|woff2?|ttf|otf|bin|stl|3mf|step|map|min\.js|min\.css)$/i;

const MIN_NAME_SCORE = 3; // one hit on the file's own name; a directory hit alone (1) isn't enough
const NAME_WEIGHT = 3;
const DIR_WEIGHT = 1;

/**
 * Lower-case word tokens of `text`: split on anything that isn't a
 * letter/digit and on camelCase seams, drop pure numbers ("mcu_1" →
 * "mcu"), stopwords and anything under 3 characters, and fold a plain
 * trailing "s" ("sensors" → "sensor") so a plural step matches a
 * singular file name.
 */
export function tokenize(text) {
  if (typeof text !== "string" || !text) return [];
  const spaced = text.replace(/([a-z0-9])([A-Z])/g, "$1 $2");
  const out = [];
  for (const raw of spaced.toLowerCase().split(/[^a-z0-9]+/)) {
    if (raw.length < 3 || /^\d+$/.test(raw)) continue;
    const word = raw.length > 3 && raw.endsWith("s") && !raw.endsWith("ss") ? raw.slice(0, -1) : raw;
    if (STOPWORDS.has(word) || STOPWORDS.has(raw)) continue;
    if (!out.includes(word)) out.push(word);
  }
  return out;
}

function splitPath(path) {
  const slash = path.lastIndexOf("/");
  const dir = slash === -1 ? "" : path.slice(0, slash);
  const base = slash === -1 ? path : path.slice(slash + 1);
  const dot = base.lastIndexOf(".");
  const stem = dot > 0 ? base.slice(0, dot) : base;
  return { dir, base, stem };
}

/** Whether `path` is a file a step could plausibly be about. */
export function isCandidatePath(path) {
  if (typeof path !== "string" || !path) return false;
  if (SKIP_DIRS.test(path) || SKIP_FILES.test(path) || SKIP_EXT.test(path)) return false;
  return splitPath(path).base !== ".gitkeep";
}

// The words a step is "about": its title plus the ids it references.
function stepWords(step) {
  const parts = [step?.title, ...(Array.isArray(step?.part_ids) ? step.part_ids : []), ...(Array.isArray(step?.tool_ids) ? step.tool_ids : [])];
  return tokenize(parts.filter((p) => typeof p === "string").join(" "));
}

/** How strongly `path` matches the step's words (0 = not at all). */
export function scorePath(path, words) {
  const { dir, stem } = splitPath(path);
  const nameTokens = tokenize(stem);
  const dirTokens = tokenize(dir);
  let score = 0;
  for (const w of words) {
    if (nameTokens.includes(w)) score += NAME_WEIGHT;
    else if (dirTokens.includes(w)) score += DIR_WEIGHT;
  }
  return score;
}

// Shallower paths first, then alphabetical — a deterministic tie-break,
// so the same project and step always open the same file.
function byDepthThenName(a, b) {
  const da = a.split("/").length;
  const db = b.split("/").length;
  return da - db || (a < b ? -1 : a > b ? 1 : 0);
}

/** The project's most entry-like file (main.*, index.*, *.ino …), or null. */
export function findEntryFile(paths) {
  const candidates = (paths || []).filter(isCandidatePath);
  let best = null;
  let bestRank = Infinity;
  for (const path of candidates.slice().sort(byDepthThenName)) {
    const { stem, base } = splitPath(path);
    const lower = stem.toLowerCase();
    let rank = ENTRY_NAMES.indexOf(lower);
    if (rank === -1 && /\.ino$/i.test(base)) rank = ENTRY_NAMES.indexOf("sketch");
    if (rank !== -1 && rank < bestRank) {
      best = path;
      bestRank = rank;
    }
  }
  return best;
}

/**
 * The file a step most likely concerns.
 * @param {{title?: string, part_ids?: string[], tool_ids?: string[]}} step
 * @param {string[]} paths - the project's file paths
 * @param {{phaseName?: string}} [opts] - the phase's name only feeds the "is this a software step?" check for the entry-file fallback; it never scores a file
 * @returns {{path: string, reason: "name" | "entry"} | null}
 */
export function pickStepFile(step, paths, { phaseName } = {}) {
  const candidates = (paths || []).filter(isCandidatePath).sort(byDepthThenName);
  if (candidates.length === 0) return null;

  const words = stepWords(step);
  let best = null;
  let bestScore = 0;
  for (const path of candidates) {
    const score = scorePath(path, words);
    if (score > bestScore) {
      best = path;
      bestScore = score;
    }
  }
  if (best && bestScore >= MIN_NAME_SCORE) return { path: best, reason: "name" };

  const isCodeStep = [...words, ...tokenize(phaseName || "")].some((w) => CODE_WORDS.has(w));
  if (isCodeStep) {
    const entry = findEntryFile(candidates);
    if (entry) return { path: entry, reason: "entry" };
  }
  return null;
}

/** The composer's starter text for a step. The person edits it before sending. */
export function stepDraftText(step, phaseName) {
  const title = typeof step?.title === "string" ? step.title.trim() : "";
  const phase = typeof phaseName === "string" && phaseName.trim() ? ` (${phaseName.trim()} phase)` : "";
  const bits = [`Work on this build step${phase}: "${title}".`];
  const parts = (Array.isArray(step?.part_ids) ? step.part_ids : []).filter((p) => typeof p === "string" && p);
  const tools = (Array.isArray(step?.tool_ids) ? step.tool_ids : []).filter((t) => typeof t === "string" && t);
  if (parts.length) bits.push(`Parts: ${parts.join(", ")}.`);
  if (tools.length) bits.push(`Tools: ${tools.join(", ")}.`);
  return bits.join(" ");
}

/**
 * Everything "Work on this" needs to decide, in one pure call.
 * @returns {{path: string|null, reason: "name"|"entry"|null, draft: string, mode: "ask"|"edit"}}
 *   `mode` is "edit" only for a confident name match (see this file's
 *   header); everything else — an entry-file guess, or no file at all —
 *   is "ask", which also never needs a path (Edit mode's proposals
 *   endpoint 400s on a ref without one).
 */
export function planWorkOnStep({ step, phaseName, paths }) {
  const pick = pickStepFile(step, paths, { phaseName });
  return {
    path: pick ? pick.path : null,
    reason: pick ? pick.reason : null,
    draft: stepDraftText(step, phaseName),
    mode: pick && pick.reason === "name" ? "edit" : "ask",
  };
}
