// M2C B10 (Remove Local folder mode from the browser editor) — guard test.
//
// The browser editor works on cloud project files only. The Explorer's
// Project files / Local folder switch, createLocalFileProvider() and every
// branch that only a local provider could take are gone. This test keeps it
// that way. It reads the REAL source files — no pasted copies — and fails if
// a local-provider code path comes back.
//
// What it checks:
//   - fileProviders.js no longer exports createLocalFileProvider, and the
//     cloud provider carries no `terminal` / `writeNeedsConfirm` capability
//     and no propose / confirm / deny / status methods (those existed for
//     the local provider and the removed Terminal panel only);
//   - no code under components/workbench/ or lib/workbench/ names a "local"
//     source / provider id, createLocalFileProvider, PendingActionBar, the
//     source switch (onChangeSource), the daemon-offline empty state, the
//     "No history for local files" branch or the "Local folder" label;
//   - BuildTab.jsx no longer arms a "local" source override;
//   - what B10 deliberately KEEPS is still there: hooks/useDaemonStatus.js
//     (untouched until M2C Phase 5) and the Dev-server URL preview
//     (UrlPreview.jsx, the /mm-inspector.js route, the preview pane's
//     "Dev server URL" mode). The Dev-server URL preview never went through
//     the file provider, so removing Local mode cannot affect it — this test
//     pins that the pieces it does use are still present.
//
// Plain `node` only (no bundler, no JSX parsing): it works on text, with
// comments stripped first so a comment can't trip or hide a match.
//
// Run: node frontend/app/lib/workbench/__tests__/localFolderRemoved.test.mjs
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadSource } from "./loadSource.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const appRoot = resolve(here, "..", "..", ".."); // frontend/app

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

// Source text with block comments (JSX comment braces included) and `//`
// line comments removed.
function stripComments(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
}

/** Every .js / .jsx / .mjs file under `dir`, skipping node_modules, .next and test folders. */
function sourceFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name === ".next" || name === "__tests__") continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
    else if (/\.(jsx?|mjs)$/.test(name)) out.push(full);
  }
  return out;
}

function code(...parts) {
  return stripComments(readFileSync(join(appRoot, ...parts), "utf8"));
}

// --- fileProviders.js: no local provider, no leftover capabilities --------

const fileProviders = loadSource("../fileProviders.js", {
  imports: {
    "../../context/SessionContext": { authHeaders: async () => ({ Authorization: "Bearer t" }) },
    "../pusherClient": { getPusherClient: () => null },
    "./aiUpdates": loadSource("../aiUpdates.js"),
  },
});

assertEqual(typeof fileProviders.createLocalFileProvider, "undefined", "fileProviders.js no longer exports createLocalFileProvider");
assertEqual(
  Object.keys(fileProviders).sort(),
  ["FileConflictError", "createCloudFileProvider", "parseErrorDetail"],
  "fileProviders.js exports exactly the cloud provider, FileConflictError and parseErrorDetail"
);

const cloud = fileProviders.createCloudFileProvider({ workspaceId: "ws-1", apiUrl: "http://api.test" });
assertEqual(cloud.id, "cloud", "the cloud provider is still id \"cloud\"");
assertEqual(
  { write: cloud.capabilities.write, history: cloud.capabilities.history, findings: cloud.capabilities.findings },
  { write: true, history: true, findings: true },
  "the cloud provider keeps write, history and findings"
);
assertEqual("terminal" in cloud.capabilities, false, "no `terminal` capability flag any more (nothing reads it)");
assertEqual("writeNeedsConfirm" in cloud.capabilities, false, "no `writeNeedsConfirm` capability (only the local provider's propose/confirm save used it)");
for (const method of ["propose", "confirm", "deny", "status"]) {
  assertEqual(method in cloud, false, `the cloud provider has no ${method}() (a local-daemon method)`);
}
for (const method of ["list", "read", "write", "remove", "move", "mkdir", "history", "restore", "findings", "subscribe"]) {
  assertEqual(typeof cloud[method], "function", `the cloud provider still has ${method}()`);
}

// --- no local-provider code under the editor's own folders -----------------

const editorDirs = [join(appRoot, "components", "workbench"), join(appRoot, "lib", "workbench")];
const editorFiles = editorDirs.flatMap((dir) => sourceFiles(dir));

const bannedInEditor = [
  [/createLocalFileProvider/, "createLocalFileProvider"],
  [/["']local["']/, "a \"local\" source / provider id string"],
  [/\bPendingActionBar\b/, "PendingActionBar (the local provider's confirm bar)"],
  [/\bonChangeSource\b|\brequestChangeSource\b|\bswitchSource\b|\bSWITCH_SOURCE\b/, "the source switch"],
  [/\bdaemonOffline\b|\bdaemonLive\b|\bdaemonChecked\b/, "the daemon-offline state"],
  [/\bwriteNeedsConfirm\b|\bawaitingConfirmation\b|\bpendingLocalWritesRef\b/, "the propose/confirm save path"],
  [/No history for local files/, "the \"No history for local files\" empty state"],
  [/Local folder/, "the \"Local folder\" label"],
];
for (const [pattern, label] of bannedInEditor) {
  const offenders = editorFiles
    .filter((file) => pattern.test(stripComments(readFileSync(file, "utf8"))))
    .map((file) => relative(appRoot, file));
  assertEqual(offenders, [], `no code under components/workbench or lib/workbench names ${label}`);
}

// --- the specific files the work package names -----------------------------

const workbench = code("components", "workbench", "EditorWorkbench.jsx");
assertEqual(/createCloudFileProvider\(\{\s*workspaceId,\s*apiUrl\s*\}\)/.test(workbench), true, "EditorWorkbench.jsx always builds the cloud provider");
assertEqual(/\buseDaemonStatus\b/.test(workbench), false, "EditorWorkbench.jsx no longer polls the daemon (that poll only ever served the Local source)");
assertEqual(/onDownloadZip=\{downloadZip\}/.test(workbench), true, "ZIP download is offered unconditionally (it was hidden only for Local)");

const explorer = code("components", "workbench", "Explorer.jsx");
assertEqual(/\bHardDrive\b|\bWifiOff\b/.test(explorer), false, "Explorer.jsx no longer imports the Local folder / daemon-offline icons");
assertEqual(/aria-label="Filter files"/.test(explorer), true, "Explorer.jsx still has its file filter box");

const statusBar = code("components", "workbench", "StatusBar.jsx");
assertEqual(/awaiting-confirmation/.test(statusBar), false, "StatusBar.jsx no longer has the local-only \"awaiting-confirmation\" state");
assertEqual(/Project files/.test(statusBar), true, "StatusBar.jsx still labels the cloud provider \"Project files\"");

const history = code("components", "workbench", "HistoryPanel.jsx");
assertEqual(/capabilities\?*\.history/.test(history), false, "HistoryPanel.jsx no longer branches on a history capability");

// --- BuildTab / AppShell: the redirect no longer asks for Local -------------

const buildTab = code("components", "tabs", "BuildTab.jsx");
assertEqual(/forceLocalSourceOnce|setForceLocalSourceOnce/.test(buildTab), false, "BuildTab.jsx no longer arms a Local source override");
assertEqual(/initialSourceOverride=\{[^}]*["']local["']/.test(buildTab), false, "BuildTab.jsx never passes \"local\" as the editor's source");

const appShell = code("components", "AppShell.jsx");
assertEqual(/savedTab === "local"/.test(appShell), true, "AppShell.jsx still maps a stale saved \"local\" tab id to Build (no dead end for old localStorage)");

// --- what B10 deliberately keeps ------------------------------------------

assertEqual(existsSync(join(appRoot, "hooks", "useDaemonStatus.js")), true, "hooks/useDaemonStatus.js is kept (M2C replaces it in Phase 5)");
const daemonHook = code("hooks", "useDaemonStatus.js");
assertEqual(/export function useDaemonStatus\b/.test(daemonHook), true, "...and still exports useDaemonStatus");
assertEqual(/\/local\/status/.test(daemonHook), true, "...and still polls the backend's local status route (backend local_workspace routes are untouched)");

assertEqual(existsSync(join(appRoot, "components", "workbench", "UrlPreview.jsx")), true, "the Dev-server URL preview component is kept");
assertEqual(existsSync(join(appRoot, "mm-inspector.js", "route.js")), true, "the /mm-inspector.js route the preview proxy injects is kept");
const previewPane = code("components", "workbench", "PreviewPane.jsx");
assertEqual(/id:\s*"url",\s*label:\s*"Dev server URL"/.test(previewPane), true, "the preview pane still offers its \"Dev server URL\" mode");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed.`);
  process.exit(1);
} else {
  console.log("\nAll assertions passed.");
}
