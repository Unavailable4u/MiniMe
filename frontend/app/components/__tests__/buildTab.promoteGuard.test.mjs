// Patch B04 (M2C Phase 0) — "Promote" must go through BuildTab's unsaved-edits
// guard, exactly like switching project or leaving the Editor sub-view.
//
// Why this test looks the way it does
// -----------------------------------
// BuildTab.jsx is a ~2000-line React component: JSX, next/dynamic imports,
// a dozen context hooks. Plain `node` (which is all `npm test` runs — see the
// "test" script in package.json) can neither import nor render it, and the
// repo has no React testing library or DOM environment; adding one would be a
// new dependency, which this patch must not do. So, like
// hooks/__tests__/pyodideWorker.test.mjs, this reads the REAL source text of
// the component and exercises the real code pulled out of it — nothing is
// pasted into the test as a copy that could drift from the component:
//
//   1. The real `guardUnsaved` and `confirmPendingNav` function sources are
//      extracted and evaluated against a fake `editorDirtyRef` /
//      `setPendingNav`.
//   2. The real `onClick` expression of the Promote button (found as the one
//      `onClick` that mentions `handlePromote(`) is extracted and evaluated
//      with a fake `handlePromote`, then "clicked" with a dirty and a clean
//      editor.
//   3. Source-level assertions pin down what cannot be run: that the
//      confirmation wording is unchanged, that there is still a single
//      confirmation dialog, and that the other navigation actions still go
//      through guardUnsaved().
//
// What this does NOT prove: that React actually renders the ConfirmDialog
// when `pendingNav` is set. That wiring is pre-existing (the dialog is shared
// with project switching) and unchanged by this patch; the manual check steps
// in the patch description cover it.
//
// Run: node frontend/app/components/__tests__/buildTab.promoteGuard.test.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const buildTabPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../tabs/BuildTab.jsx",
);
const src = fs.readFileSync(buildTabPath, "utf8");

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

// --- tiny source scanner ----------------------------------------------------
// Given the index of an opening "{", returns the index just past its matching
// "}". Skips //-comments, /* */ comments and '…' "…" `…` literals so a brace
// inside a comment or string doesn't throw the count off. (Template-literal
// `${…}` nesting is not tracked; none of the regions extracted below use it.)
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

// Full text of `function name(...) { ... }` (or `async function`).
function extractFunction(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src);
  if (!m) return null;
  const open = src.indexOf("{", src.indexOf(")", m.index));
  const end = matchBrace(src, open);
  return end === -1 ? null : src.slice(m.index, end);
}

// Every `onClick={ <expr> }` in the file, as the text of <expr>.
function onClickExpressions() {
  const out = [];
  const re = /onClick=\{/g;
  let m;
  while ((m = re.exec(src))) {
    const open = m.index + m[0].length - 1;
    const end = matchBrace(src, open);
    if (end === -1) continue;
    out.push(src.slice(open + 1, end - 1).trim());
  }
  return out;
}

// --- extract the real code --------------------------------------------------
const guardSrc = extractFunction("guardUnsaved");
const confirmSrc = extractFunction("confirmPendingNav");
const promoteClicks = onClickExpressions().filter((e) => /\bhandlePromote\s*\(/.test(e));

assert(!!guardSrc, "guardUnsaved() was found in BuildTab.jsx");
assert(!!confirmSrc, "confirmPendingNav() was found in BuildTab.jsx");
assertEqual(promoteClicks.length, 1, "exactly one onClick calls handlePromote (the Promote button)");

// --- 1. source-level: the call site goes through guardUnsaved ---------------
{
  const click = promoteClicks[0] || "";
  assert(
    /\bguardUnsaved\s*\(/.test(click),
    "the Promote button's onClick goes through guardUnsaved()",
  );
  // handlePromote( may only be CALLED from inside a guardUnsaved(...) argument
  // (the one definition line is `async function handlePromote(`).
  const calls = [...src.matchAll(/\bhandlePromote\s*\(/g)].filter(
    (m) => !/function\s+$/.test(src.slice(Math.max(0, m.index - 20), m.index)),
  );
  assertEqual(calls.length, 1, "handlePromote() has exactly one call site");
  assert(
    /guardUnsaved\s*\(\s*\(\s*\)\s*=>\s*handlePromote\s*\(/.test(click),
    "that call is wrapped as guardUnsaved(() => handlePromote(...))",
  );
}

// --- 2. behavior: real guardUnsaved + real onClick, dirty and clean ---------
function makeHarness({ dirty }) {
  const editorDirtyRef = { current: dirty };
  const pendingNavCalls = [];
  const setPendingNav = (v) => pendingNavCalls.push(v);
  const promoteCalls = [];
  const handlePromote = (...args) => promoteCalls.push(args);

  const guardUnsaved = new Function(
    "editorDirtyRef",
    "setPendingNav",
    `${guardSrc}\nreturn guardUnsaved;`,
  )(editorDirtyRef, setPendingNav);

  const onClick = new Function(
    "guardUnsaved",
    "handlePromote",
    "selected",
    "targetStage",
    `return (${promoteClicks[0]});`,
  )(guardUnsaved, handlePromote, { id: "ws-1" }, "test");

  return { editorDirtyRef, pendingNavCalls, promoteCalls, onClick, setPendingNav };
}

if (guardSrc && confirmSrc && promoteClicks.length === 1) {
  // Dirty editor: nothing is promoted yet; the navigation is held.
  {
    const h = makeHarness({ dirty: true });
    h.onClick();
    assertEqual(h.promoteCalls.length, 0, "dirty editor: Promote does NOT run immediately");
    assertEqual(h.pendingNavCalls.length, 1, "dirty editor: the click is held behind the confirmation (setPendingNav called once)");
    assert(
      typeof h.pendingNavCalls[0]?.run === "function",
      "dirty editor: the held navigation is { run } — the same shape project switching uses",
    );

    // Cancel needs no assertion of its own: the dialog's onCancel only calls
    // setPendingNav(null), and nothing has run yet (asserted above).

    // Confirm: run the REAL confirmPendingNav with the held navigation. Only
    // meaningful if a navigation was actually held — otherwise record a
    // failure rather than "passing" vacuously on a no-op.
    const pendingNav = h.pendingNavCalls[0];
    if (!pendingNav) {
      assert(false, "dirty editor, Discard: there is a held navigation to confirm");
    } else {
      const confirmPendingNav = new Function(
        "pendingNav",
        "setPendingNav",
        "editorDirtyRef",
        `${confirmSrc}\nreturn confirmPendingNav;`,
      )(pendingNav, h.setPendingNav, h.editorDirtyRef);
      confirmPendingNav();
      assertEqual(h.promoteCalls, [["ws-1", "test"]], "dirty editor, Discard: Promote then runs once with the clicked project and stage");
      assertEqual(h.editorDirtyRef.current, false, "dirty editor, Discard: the dirty flag is cleared (no second prompt)");
      assertEqual(h.pendingNavCalls[1], null, "dirty editor, Discard: the confirmation is closed");
    }
  }

  // Clean editor: behaves exactly as before — immediate, no confirmation.
  {
    const h = makeHarness({ dirty: false });
    h.onClick();
    assertEqual(h.promoteCalls, [["ws-1", "test"]], "clean editor: Promote runs immediately with the clicked project and stage");
    assertEqual(h.pendingNavCalls.length, 0, "clean editor: no confirmation is shown");
  }
}

// --- 3. source-level: wording and the other navigation actions unchanged ----
{
  const title = 'title="Discard unsaved changes?"';
  const message = 'message="You have unsaved changes in the Editor. Leaving now will discard them."';
  assertEqual(src.split(title).length - 1, 1, "there is still exactly one 'Discard unsaved changes?' dialog (Promote reuses it)");
  assertEqual(src.split(message).length - 1, 1, "the confirmation message text is unchanged");
  assert(src.includes("open={!!pendingNav}"), "that dialog is still driven by pendingNav");

  const selectWs = extractFunction("requestSelectWs") || "";
  const buildView = extractFunction("requestBuildView") || "";
  assert(/guardUnsaved\s*\(\s*\(\s*\)\s*=>\s*setSelectedWsId\(id\)\s*\)/.test(selectWs), "switching project still goes through guardUnsaved()");
  assert(/guardUnsaved\s*\(\s*\(\s*\)\s*=>\s*setBuildView\(id\)\s*\)/.test(buildView), "switching Build sub-view still goes through guardUnsaved()");
}

if (failures > 0) {
  console.error(`\n${failures} test(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll BuildTab Promote-guard tests passed.");
}
