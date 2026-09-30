"use client";
// frontend/app/components/workbench/PreviewPane.jsx — W6.1 (Build
// Workbench plan). Mounted as PreviewColumn's `children` (see that
// file's own header) — this is the thing W2.3b left an empty frame
// for.
//
// Four states, one per detectKind() outcome:
//   - "static": the actual meat of this patch — live-bundles
//     index.html + its local <link>/<script> files into one srcDoc,
//     re-reading on every relevant change (debounced ~400ms).
//   - "react": W6.6 — Sandpack (hidden editor, preview only), the same
//     console+inspector bridge as "static" carried in as a project file
//     instead of injected into an HTML document (see
//     lib/preview/mmInspectorFile.js's own header for why).
//   - "python": runs through the SAME usePyodideWorker.js worker
//     ArtifactRenderer.jsx's PythonArtifact already uses — click Run,
//     see stdout/stderr, no live-typing auto-run (Pyodide's own
//     startup is ~10-20s, so re-running on every keystroke would be
//     actively hostile, unlike the static path's cheap string-inlining).
//   - null (detectKind found nothing recognizable, or filesMeta hasn't
//     loaded yet): the plan's own "No preview available for this
//     project type — here's why" text.
//
// Data flow for "static", the part worth being explicit about: this
// reads editor buffers (unsaved edits included, per the plan's own
// "Done when") through useEditorStore() directly — it's mounted well
// inside EditorStoreProvider already, same as CodeEditor and the
// explorer. `provider`/`filesMeta` are NOT in the store (editorStore.js's
// own header lists its exact shape and neither is on it) — they're
// WorkbenchBody's plumbing, passed down as plain props the same way
// CodeEditor and Explorer already receive them.
//
// Security posture: sandbox="allow-scripts" only, matching
// ArtifactRenderer.jsx/WireframePreview.jsx exactly (see either file's
// own header for why "allow-scripts" alone, no allow-same-origin, no
// allow-forms, is enough and deliberately not more).
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Crosshair, ExternalLink, Loader2, Monitor, Play, RefreshCw, Smartphone, Tablet } from "lucide-react";
import { useEditorStore } from "../../lib/workbench/editorStore";
import { detectKind } from "../../lib/preview/detectKind";
import { bundleStatic } from "../../lib/preview/bundleStatic";
import { injectConsoleBridge } from "../../lib/preview/consoleBridge";
import { injectInspectorRuntime } from "../../lib/preview/inspectorRuntime";
import { extractMmRanges, innermostMmAt } from "../../lib/preview/mmRange";
import { elementFromMessage } from "../../lib/workbench/elementRef";
import { instrumentSource } from "../../lib/preview/instrument"; // W6.6
import { shouldBundleFile, pickReactEntry, parseDependencies, withInjectedImport, inspectorPathFor } from "../../lib/preview/reactBundle"; // W6.6
import { buildMmInspectorFileContent } from "../../lib/preview/mmInspectorFile"; // W6.6
import { SandpackProvider, SandpackPreview, useSandpack } from "@codesandbox/sandpack-react"; // W6.6
import { usePyodideWorker } from "../../hooks/usePyodideWorker";

// W6.2: a fresh nonce per successful build, NOT per component mount —
// each rebuild re-injects the bridge into a brand-new srcDoc string, so
// a stale nonce from a PREVIOUS build could otherwise still validate
// against a message that (implausibly, but not impossibly, given the
// iframe itself is recreated via `key={reloadNonce}` on every rebuild
// too) arrives from an old, not-yet-torn-down frame. crypto.randomUUID()
// is used when available (every modern browser); the fallback covers
// only a non-secure-context edge case (a plain http:// dev server) where
// it's undefined — still random enough that a page running INSIDE the
// sandboxed iframe (the only thing that could possibly guess it) has
// nothing to gain from trying.
function makeNonce() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `mm-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const DEBOUNCE_MS = 400;

const DEVICE_PRESETS = [
  { id: "mobile", label: "Mobile", Icon: Smartphone, width: 390 },
  { id: "tablet", label: "Tablet", Icon: Tablet, width: 768 },
  { id: "desktop", label: "Desktop", Icon: Monitor, width: null }, // null = fill available width
];

/**
 * Builds the one function bundleStatic() needs — "give me this path's
 * current content" — preferring a live, possibly-unsaved editor buffer
 * over a server read, and caching server reads by the version
 * filesMeta already carries so an unopened, unchanged file (style.css,
 * say, never opened as a tab) isn't re-fetched on every single
 * debounced rebuild while you type in a DIFFERENT open file.
 *
 * A file with no `version` in its metadata (LocalFileProvider's
 * listing doesn't carry one the way the cloud provider's does) always
 * bypasses the cache and re-fetches — correctness over a cache hit for
 * a case this patch's own "Done when" doesn't specifically target.
 */
function makeResolver({ provider, filesMeta, buffers, cacheRef }) {
  return async function resolveFile(path) {
    const buf = buffers[path];
    if (buf) return buf.edited; // open tab -- always the freshest possible content, unsaved edits included

    const version = filesMeta?.[path]?.version;
    const cached = cacheRef.current.get(path);
    if (cached && version != null && cached.version === version) return cached.content;

    const file = await provider.read(path);
    cacheRef.current.set(path, { version, content: file.content || "" });
    return file.content || "";
  };
}

// W6.4/W6.5: parent -> frame. The runtime ignores anything without this
// build's own nonce (inspectorRuntime.js's message listener), so a stale
// message aimed at a previous build's iframe can't act on a newer one.
// Module-level (reads only the two refs it's handed) so the message
// listener effect below can call it without listing a per-render
// function in its dependency array.
function postToFrame(iframeRef, nonce, message) {
  iframeRef.current?.contentWindow?.postMessage({ source: "minime-parent", nonce, ...message }, "*");
}

function DeviceToolbar({ device, onDeviceChange, onReload, onOpenNewTab, warnings, inspecting, onToggleInspect }) {
  return (
    <div className="shrink-0 flex items-center justify-between gap-2 px-2 h-8 border-b border-[var(--neutral-800)]">
      <div className="flex items-center gap-0.5">
        {DEVICE_PRESETS.map(({ id, label, Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => onDeviceChange(id)}
            title={label}
            aria-label={label}
            aria-pressed={device === id}
            className={`touch-target p-1 rounded ${
              device === id ? "bg-white/10 text-[var(--neutral-100)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            <Icon size={13} />
          </button>
        ))}
        {onToggleInspect && (
          <>
            <span className="mx-1 h-4 w-px bg-[var(--neutral-800)]" />
            <button
              type="button"
              onClick={onToggleInspect}
              title={inspecting ? "Stop inspecting (Esc)" : "Inspect element"}
              aria-label="Inspect element"
              aria-pressed={inspecting}
              className={`touch-target p-1 rounded ${
                inspecting ? "bg-[var(--accent)] text-[var(--accent-text)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
              }`}
            >
              <Crosshair size={13} />
            </button>
          </>
        )}
      </div>
      <div className="flex items-center gap-1">
        {warnings?.length > 0 && (
          <span title={warnings.join("\n")} className="flex items-center gap-1 text-[10px] text-amber-400">
            <AlertTriangle size={11} />
            {warnings.length}
          </span>
        )}
        <button type="button" onClick={onReload} title="Reload" aria-label="Reload" className="touch-target p-1 rounded text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
          <RefreshCw size={13} />
        </button>
        <button
          type="button"
          onClick={onOpenNewTab}
          title="Open in new tab"
          aria-label="Open in new tab"
          className="touch-target p-1 rounded text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
        >
          <ExternalLink size={13} />
        </button>
      </div>
    </div>
  );
}

function StaticPreview({ provider, filesMeta, entryPath, cursor, onSelectElement }) {
  const { state, consoleMessage } = useEditorStore();
  const [device, setDevice] = useState("desktop");
  const [built, setBuilt] = useState(null); // {html, warnings} | null while building the first time
  const [buildError, setBuildError] = useState(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const cacheRef = useRef(new Map());
  const debounceRef = useRef(null);
  // W6.2: the iframe this render's build is destined for, and the nonce
  // baked into that SAME build's injected bridge — both refs (not
  // state) since nothing here needs to re-render when either changes,
  // only the message listener below needs to read the current value at
  // the moment a postMessage actually arrives.
  const iframeRef = useRef(null);
  const nonceRef = useRef(null);
  // Monotonically increasing id for the console feed's own React keys
  // (ConsolePanel.jsx renders one row per entry) — a bridge message
  // carries no id of its own (see consoleBridge.js's own header on why
  // that's the parent's job, not the sandboxed page's).
  const consoleIdRef = useRef(0);
  // W6.4: `inspecting` is the PARENT's source of truth for the
  // crosshair toggle (the frame's own copy is just a mirror of the last
  // minime:inspect it was sent — see inspectorRuntime.js's header for
  // the Esc case that has to flow back the other way). `lastSelection`
  // is the most recent minime:select payload, shown as a one-line
  // readout below the toolbar. W6.5: acting on a selection (open the
  // file, select the range, add the chip) is EditorWorkbench.jsx's job,
  // reached through `onSelectElement` — this component only knows how
  // to turn the frame's message into that call.
  const [inspecting, setInspecting] = useState(false);
  const [lastSelection, setLastSelection] = useState(null);
  // The latest callback, read through a ref so the message listener
  // (subscribed once, not per render) never calls a stale closure.
  const onSelectElementRef = useRef(onSelectElement);
  onSelectElementRef.current = onSelectElement;
  // W6.5: what the frame is currently told to outline, so a reloaded
  // frame (fresh runtime, nothing highlighted) can be re-sent it.
  const highlightMmRef = useRef(null);
  const buffers = state.buffers;

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      try {
        const entryContent = buffers[entryPath]?.edited ?? (await provider.read(entryPath)).content ?? "";
        const resolveFile = makeResolver({ provider, filesMeta, buffers, cacheRef });
        const result = await bundleStatic({ entryPath, entryContent, resolveFile });
        // W6.2: inject the console/error bridge into every build, not
        // only on reload — the plan's own "Done when" is "a throw...
        // shows in Console within a second" while typing, so a rebuild
        // triggered by a keystroke needs the bridge just as much as the
        // first render does. A fresh nonce per build (not per mount) —
        // see makeNonce()'s own comment above for why.
        const nonce = makeNonce();
        nonceRef.current = nonce;
        // W6.5: read the data-mm ranges off bundleStatic's output BEFORE
        // the two runtimes are injected — they're pure scripts, but this
        // way the reverse-highlight lookup below sees exactly the
        // instrumented document and nothing else.
        const ranges = extractMmRanges(result.html);
        const html = injectInspectorRuntime(injectConsoleBridge(result.html, nonce), nonce);
        setBuilt({ html, warnings: result.warnings, ranges });
        setBuildError(null);
      } catch (err) {
        setBuildError(err?.message || String(err));
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(debounceRef.current);
    // buffers is state.buffers's own object reference, which the
    // reducer replaces on every dispatch (including every keystroke's
    // EDIT_BUFFER) -- that's the "updates as you type" behavior the
    // plan's own "Done when" asks for, debounced by the timer above
    // rather than by narrowing this dependency list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entryPath, filesMeta, buffers, reloadNonce]);

  // W6.2: the parent half of the bridge — one listener for this pane's
  // whole lifetime (not re-subscribed per build), reading the CURRENT
  // iframe/nonce through the refs above at message time. Two checks,
  // both required (plan §5, step W6.2's own "Security" line):
  //   - `event.source === iframeRef.current?.contentWindow` — a sandboxed
  //     iframe has an opaque origin (event.origin is always the string
  //     "null"), so origin checks are useless; comparing the WINDOW
  //     object itself is what actually proves this message came from
  //     OUR iframe and not some other frame/tab that happens to be
  //     sending postMessage's around.
  //   - `event.data.nonce === nonceRef.current` — the source check alone
  //     isn't enough once the SAME iframe has been reused across
  //     rebuilds within one `key={reloadNonce}` lifetime... it hasn't
  //     (a new build swaps `built.html` but the iframe itself is only
  //     recreated on `reloadNonce` changing, per its own `key` prop
  //     below) — so without the nonce check, a bridge instance installed
  //     by an EARLIER build (still running, since a rebuild doesn't
  //     tear down the previous iframe's JS execution until React
  //     actually replaces the srcDoc) could keep posting messages that
  //     look valid. Comparing against the CURRENT nonce is what makes a
  //     stale bridge instance's messages get silently ignored instead.
  // Every payload is otherwise treated as untrusted data (this module
  // never eval()s or otherwise executes anything from `event.data`) —
  // it's read into plain strings/numbers only, same posture
  // consoleBridge.js's own header describes for the bridge's own
  // handling of console arguments.
  useEffect(() => {
    function onMessage(event) {
      if (event.source !== iframeRef.current?.contentWindow) return;
      const data = event.data;
      if (!data || data.source !== "minime-preview" || data.nonce !== nonceRef.current) return;
      // W6.4: the inspector runtime posts through the same channel with
      // the same source/nonce as the console bridge, so its messages
      // must be peeled off BEFORE the console handling below — otherwise
      // a minime:select would land in the Console tab as an empty row.
      if (data.type === "minime:select") {
        // Untrusted data from inside the sandbox: elementFromMessage()
        // type-checks and length-caps every field, and returns null when
        // there's no usable mm to resolve to a source range.
        const selection = elementFromMessage(data);
        if (!selection) return;
        setLastSelection({ mm: selection.mm, ...selection.element });
        onSelectElementRef.current?.(selection);
        // A pick ends inspect mode (same as a browser's own element
        // picker): keyboard focus has just moved to the editor, where
        // Esc can no longer reach the frame's own handler to turn it
        // off, so leaving it on would strand the person in a mode with
        // no keyboard exit.
        setInspecting(false);
        postToFrame(iframeRef, nonceRef.current, { type: "minime:inspect", on: false });
        return;
      }
      if (data.type === "minime:inspectExited") {
        setInspecting(false);
        return;
      }
      consoleIdRef.current += 1;
      consoleMessage({
        id: consoleIdRef.current,
        type: typeof data.type === "string" ? data.type : "console",
        level: typeof data.level === "string" ? data.level : "log",
        text: typeof data.text === "string" ? data.text : "",
        stack: typeof data.stack === "string" ? data.stack : null,
        sourceLine: typeof data.sourceLine === "number" ? data.sourceLine : null,
        sourceColumn: typeof data.sourceColumn === "number" ? data.sourceColumn : null,
        timestamp: typeof data.timestamp === "number" ? data.timestamp : Date.now(),
      });
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [consoleMessage]);

  function handleToggleInspect() {
    const next = !inspecting;
    setInspecting(next);
    postToFrame(iframeRef, nonceRef.current, { type: "minime:inspect", on: next });
  }

  // A rebuild swaps srcDoc, which reloads the frame into a FRESH
  // runtime whose own `inspecting` starts false and which has nothing
  // highlighted — without this, turning inspect mode on and then typing
  // a character (debounced rebuild) would silently drop it, and the
  // reverse highlight would vanish on every keystroke. Re-sent on every
  // load, not just when set, so a runtime that somehow kept state can't
  // disagree with the toolbar / editor either.
  function handleIframeLoad() {
    postToFrame(iframeRef, nonceRef.current, { type: "minime:inspect", on: inspecting });
    postToFrame(iframeRef, nonceRef.current, { type: "minime:highlight", mm: highlightMmRef.current });
  }

  // W6.5, reverse direction: the innermost element containing the
  // editor's cursor is outlined in the preview — "the element you're
  // editing". CodeEditor reports a 1-based column; data-mm columns are
  // 0-based. Only the file the cursor is actually in can match (ranges
  // carry their own path), so moving to a different file clears it.
  const highlightMm = useMemo(
    () => (built?.ranges && cursor ? innermostMmAt(built.ranges, state.activePath, cursor.line, cursor.col - 1) : null),
    [built, cursor, state.activePath]
  );
  useEffect(() => {
    highlightMmRef.current = highlightMm;
    postToFrame(iframeRef, nonceRef.current, { type: "minime:highlight", mm: highlightMm });
  }, [highlightMm]);

  function handleReload() {
    cacheRef.current.clear(); // a manual reload means "don't trust anything cached", unlike the live-typing path which is fine reusing unopened files' last-known content
    setReloadNonce((n) => n + 1);
  }

  function handleOpenNewTab() {
    if (!built?.html) return;
    // Same blob-URL-then-delayed-revoke pattern as
    // ArtifactRenderer.jsx's openInNewTab() -- see that file for why
    // 10s (long enough for the new tab to finish loading the blob
    // before it's revoked, short enough not to leak the object URL
    // forever).
    const blob = new Blob([built.html], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank", "noopener,noreferrer");
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  const preset = DEVICE_PRESETS.find((d) => d.id === device);

  return (
    <div className="h-full min-h-0 flex flex-col">
      <DeviceToolbar
        device={device}
        onDeviceChange={setDevice}
        onReload={handleReload}
        onOpenNewTab={handleOpenNewTab}
        warnings={built?.warnings}
        inspecting={inspecting}
        onToggleInspect={handleToggleInspect}
      />
      {lastSelection && (
        <div
          className="shrink-0 flex items-center gap-2 px-2 py-1 border-b border-[var(--neutral-800)] font-mono text-[10px] text-[var(--neutral-400)]"
          title={lastSelection.mm}
        >
          <span className="truncate">
            {lastSelection.tag}
            {lastSelection.classes.length > 0 ? `.${lastSelection.classes.join(".")}` : ""}
          </span>
          <span className="shrink-0 text-[var(--neutral-600)]">{lastSelection.mm}</span>
          {lastSelection.dynamic && <span className="shrink-0 text-amber-400">dynamic — nearest source element</span>}
          {lastSelection.instanceCount > 1 && <span className="shrink-0 text-[var(--neutral-500)]">×{lastSelection.instanceCount}</span>}
        </div>
      )}
      <div className="flex-1 min-h-0 overflow-auto bg-[var(--neutral-900)] flex justify-center">
        {buildError ? (
          <div className="p-4 text-xs text-red-400 whitespace-pre-wrap">{buildError}</div>
        ) : !built ? (
          <div className="flex items-center gap-1.5 text-xs text-[var(--neutral-500)] m-auto">
            <Loader2 size={12} className="animate-spin" /> Building preview…
          </div>
        ) : (
          <iframe
            key={reloadNonce}
            ref={iframeRef}
            onLoad={handleIframeLoad}
            title="Preview"
            srcDoc={built.html}
            sandbox="allow-scripts"
            className="bg-white h-full shrink-0"
            style={{ width: preset.width ?? "100%", border: "none" }}
          />
        )}
      </div>
    </div>
  );
}

// W6.6 (Build Workbench plan) — the "react" provider: Sandpack, hidden
// editor (only <SandpackPreview> is mounted — same "no code tab, just
// the rendered output" choice ArtifactRenderer.jsx already made for its
// own model-generated React artifacts), the project's REAL files
// (buffers preferred, matching StaticPreview's own makeResolver), and
// the same console+inspector bridge as the static preview — just
// carried in as a project file instead of injected into an HTML
// document. See lib/preview/mmInspectorFile.js's own header for why
// that split exists (Sandpack's bundler iframe is cross-origin; there
// is no HTML this app can parse5-inject a <script> into the way
// bundleStatic.js's output allows).
//
// Two components, not one, because useSandpack() only works inside
// <SandpackProvider>: ReactPreview assembles the files/dependencies and
// renders the provider; SandpackReactBridge is the actual consumer,
// mounted as its child, doing everything that needs the live Sandpack
// client (finding the iframe, wiring the message channel, the reverse
// highlight). This mirrors BuildTab.jsx's own CodeAwareChatPanel split
// (a provider a component can't read from itself, and a child that
// can) for the identical reason.
//
// Message handling here is intentionally NOT shared with StaticPreview's
// own onMessage effect via a common hook, even though the logic is very
// similar — extracting one would mean touching StaticPreview's already
// -working, already-tested implementation as part of THIS step, for a
// feature (React/Sandpack) whose own underlying platform behavior
// (does postMessage really reach us the way §5's own "prototype first"
// note wonders) this patch cannot verify in a real browser. Keeping the
// two independent means a surprise here can't regress the static path.
function ReactPreview({ provider, filesMeta, cursor, onSelectElement }) {
  const { state, consoleMessage } = useEditorStore();
  const [device, setDevice] = useState("desktop");
  const [assembled, setAssembled] = useState(null); // {files, dependencies, entry, nonce, ranges, warnings} | null
  const [assembleError, setAssembleError] = useState(null);
  const [inspecting, setInspecting] = useState(false);
  const [lastSelection, setLastSelection] = useState(null);
  const cacheRef = useRef(new Map());
  const debounceRef = useRef(null);
  const consoleIdRef = useRef(0);

  const buffers = state.buffers;

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      try {
        const resolveFile = makeResolver({ provider, filesMeta, buffers, cacheRef });
        const paths = Object.keys(filesMeta || {}).filter(shouldBundleFile);
        const warnings = [];

        // Every file instrumented (W6.3's JSX half, finally wired in —
        // instrumentSource() itself already no-ops for anything that
        // isn't .js/.jsx/.ts/.tsx/.html, so this doesn't need its own
        // per-extension branch the way bundleStatic.js's entry-only
        // instrumentation does; a React project's clickable surface IS
        // its JSX, unlike a static project's separate script files —
        // see bundleStatic.js's own header for that comparison).
        const entries = await Promise.all(
          paths.map(async (path) => {
            const content = await resolveFile(path);
            const result = await instrumentSource(content, path);
            if (result.note) warnings.push(`${path}: ${result.note}`);
            return [path, result.code];
          })
        );
        const files = Object.fromEntries(entries);
        const ranges = extractMmRanges(Object.values(files).join("\n"));
        const entry = pickReactEntry(paths);
        const { dependencies } = parseDependencies(files["package.json"] ?? null);
        const nonce = makeNonce();

        let finalFiles = files;
        if (entry) {
          const inspectorPath = inspectorPathFor(entry);
          finalFiles = {
            ...withInjectedImport(files, entry, "./mm-inspector.js"),
            [inspectorPath]: buildMmInspectorFileContent(nonce),
          };
        } else {
          warnings.push("No recognized entry file (looked for src/main.jsx, src/index.js, etc.) — the preview will still try to run, but click-to-code and the console won't be available.");
        }

        // Sandpack's own path convention: every files/entry key carries
        // a leading "/" (see ArtifactRenderer.jsx's own "/App.js").
        const sandpackFiles = Object.fromEntries(Object.entries(finalFiles).map(([path, code]) => [`/${path}`, code]));
        setAssembled({ files: sandpackFiles, dependencies, entry: entry ? `/${entry}` : undefined, nonce, ranges, warnings });
        setAssembleError(null);
      } catch (err) {
        setAssembleError(err?.message || String(err));
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(debounceRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filesMeta, buffers]);

  return (
    <div className="h-full min-h-0 flex flex-col">
      <DeviceToolbar
        device={device}
        onDeviceChange={setDevice}
        onReload={() => {}}
        onOpenNewTab={() => {}}
        warnings={assembled?.warnings}
        inspecting={inspecting}
        onToggleInspect={() => setInspecting((v) => !v)}
      />
      {lastSelection && (
        <div className="shrink-0 flex items-center gap-2 px-2 py-1 border-b border-[var(--neutral-800)] font-mono text-[10px] text-[var(--neutral-400)]" title={lastSelection.mm}>
          <span className="truncate">
            {lastSelection.tag}
            {lastSelection.classes.length > 0 ? `.${lastSelection.classes.join(".")}` : ""}
          </span>
          <span className="shrink-0 text-[var(--neutral-600)]">{lastSelection.mm}</span>
          {lastSelection.dynamic && <span className="shrink-0 text-amber-400">dynamic — nearest source element</span>}
          {lastSelection.instanceCount > 1 && <span className="shrink-0 text-[var(--neutral-500)]">×{lastSelection.instanceCount}</span>}
        </div>
      )}
      <div className="flex-1 min-h-0 overflow-auto bg-[var(--neutral-900)] flex justify-center">
        {assembleError ? (
          <div className="p-4 text-xs text-red-400 whitespace-pre-wrap">{assembleError}</div>
        ) : !assembled ? (
          <div className="flex items-center gap-1.5 text-xs text-[var(--neutral-500)] m-auto">
            <Loader2 size={12} className="animate-spin" /> Building preview…
          </div>
        ) : (
          <div className="h-full" style={{ width: DEVICE_PRESETS.find((p) => p.id === device)?.width ?? "100%" }}>
            {/* key={assembled.entry+deps} would force a full remount on
                every keystroke (Sandpack re-bundles happily on a files
                PROP change already); keying on nothing lets Sandpack do
                its own incremental rebuild, the same "typing recompiles,
                doesn't restart the whole client" behavior its HMR is
                for. */}
            <SandpackProvider
              template="react"
              theme="dark"
              files={assembled.files}
              customSetup={{ entry: assembled.entry, dependencies: assembled.dependencies }}
              style={{ height: "100%", width: "100%" }}
            >
              <SandpackReactBridge
                nonce={assembled.nonce}
                ranges={assembled.ranges}
                cursor={cursor}
                activePath={state.activePath}
                onSelectElement={onSelectElement}
                consoleMessage={consoleMessage}
                inspecting={inspecting}
                setInspecting={setInspecting}
                setLastSelection={setLastSelection}
                consoleIdRef={consoleIdRef}
              />
            </SandpackProvider>
          </div>
        )}
      </div>
    </div>
  );
}

// The actual Sandpack consumer — see ReactPreview's own header for why
// this is split out as a child rather than folded into it.
function SandpackReactBridge({ nonce, ranges, cursor, activePath, onSelectElement, consoleMessage, inspecting, setInspecting, setLastSelection, consoleIdRef }) {
  const previewRef = useRef(null);
  const iframeRef = useRef(null);
  const { sandpack } = useSandpack();

  // getClient() is null until Sandpack has actually created its runtime
  // client — re-checked on every status change rather than assumed
  // ready after some fixed delay or a single mount-time read.
  useEffect(() => {
    iframeRef.current = previewRef.current?.getClient?.()?.iframe || null;
  }, [sandpack.status]);

  useEffect(() => {
    function onMessage(event) {
      if (!iframeRef.current || event.source !== iframeRef.current.contentWindow) return;
      const data = event.data;
      if (!data || data.source !== "minime-preview" || data.nonce !== nonce) return;
      if (data.type === "minime:select") {
        const selection = elementFromMessage(data);
        if (!selection) return;
        setLastSelection({ mm: selection.mm, ...selection.element });
        onSelectElement?.(selection);
        setInspecting(false);
        postToFrame(iframeRef, nonce, { type: "minime:inspect", on: false });
        return;
      }
      if (data.type === "minime:inspectExited") {
        setInspecting(false);
        return;
      }
      consoleIdRef.current += 1;
      consoleMessage({
        id: consoleIdRef.current,
        type: typeof data.type === "string" ? data.type : "console",
        level: typeof data.level === "string" ? data.level : "log",
        text: typeof data.text === "string" ? data.text : "",
        stack: typeof data.stack === "string" ? data.stack : null,
        sourceLine: typeof data.sourceLine === "number" ? data.sourceLine : null,
        sourceColumn: typeof data.sourceColumn === "number" ? data.sourceColumn : null,
        timestamp: typeof data.timestamp === "number" ? data.timestamp : Date.now(),
      });
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [nonce, consoleMessage, onSelectElement, setInspecting, setLastSelection, consoleIdRef]);

  // Sandpack's OWN bundler/compile errors (a syntax error, say) happen
  // BEFORE any of the project's own code — including the imported
  // inspector file — ever runs, so the custom bridge above can
  // structurally never see them. Sandpack's own error channel is the
  // only thing that can; shown via its built-in overlay
  // (showSandpackErrorOverlay, default true on SandpackPreview) AND
  // echoed into the same Console feed the runtime bridge's own messages
  // land in, so a build failure isn't a DIFFERENT kind of silence.
  useEffect(() => {
    return sandpack.listen((message) => {
      if (message.type !== "action" || message.action !== "show-error") return;
      consoleIdRef.current += 1;
      const where = message.path ? `${message.path}${message.line ? `:${message.line}` : ""} — ` : "";
      consoleMessage({
        id: consoleIdRef.current,
        type: "error",
        level: "error",
        text: `${message.title ? message.title + ": " : ""}${where}${message.message || "Build error"}`,
        stack: null,
        timestamp: Date.now(),
      });
    });
  }, [sandpack, consoleMessage, consoleIdRef]);

  // Reverse highlight — same idea as StaticPreview's own, over `ranges`
  // extracted from every instrumented project file instead of one HTML
  // document.
  const highlightMm = useMemo(
    () => (ranges && cursor ? innermostMmAt(ranges, activePath, cursor.line, cursor.col - 1) : null),
    [ranges, cursor, activePath]
  );
  useEffect(() => {
    postToFrame(iframeRef, nonce, { type: "minime:highlight", mm: highlightMm });
  }, [highlightMm, nonce]);

  // Re-sent on every status change, same "a fresh runtime starts with
  // nothing toggled on" reasoning as StaticPreview's own onLoad handler
  // — Sandpack's own HMR can swap the running code without this
  // component ever re-rendering for an unrelated reason otherwise.
  useEffect(() => {
    postToFrame(iframeRef, nonce, { type: "minime:inspect", on: inspecting });
  }, [inspecting, nonce, sandpack.status]);

  return (
    <SandpackPreview
      ref={previewRef}
      showOpenInCodeSandbox={false}
      showRefreshButton={false}
      showOpenNewtab={false}
      style={{ height: "100%", width: "100%" }}
    />
  );
}

function PythonPreview({ filesMeta }) {
  const { run } = usePyodideWorker();
  const [status, setStatus] = useState("idle"); // idle | loading | ok | error
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  // Same entry-picking spirit as detectKind's index.html shortest-path
  // rule: prefer a file literally named main.py (the obvious
  // convention), else the shallowest .py file in the project.
  const entryPath = useMemo(() => {
    const pyFiles = Object.keys(filesMeta || {}).filter((p) => /\.py$/i.test(p));
    const main = pyFiles.find((p) => p === "main.py" || p.endsWith("/main.py"));
    if (main) return main;
    return pyFiles.sort((a, b) => a.split("/").length - b.split("/").length)[0] || null;
  }, [filesMeta]);

  const { state } = useEditorStore();

  async function handleRun() {
    setStatus("loading");
    setError(null);
    try {
      // Live buffer if the entry file happens to be open (unsaved edits
      // included, same as the static path), otherwise this component
      // doesn't have a provider prop to fall back to a server read with
      // -- PreviewPane passes `provider` down to StaticPreview only
      // today; wiring it here too is a reasonable, small follow-up once
      // "run the file as currently saved" turns out not to be enough.
      const code = state.buffers[entryPath]?.edited;
      const payload = await run(code ?? "");
      setResult(payload);
      setStatus("ok");
    } catch (err) {
      setError(err?.message || String(err));
      setStatus("error");
    }
  }

  if (!entryPath) {
    return <EmptyState title="No Python entry file found" detail="Expected at least one .py file in this project." />;
  }

  const hasOutput = result && (result.stdout || result.stderr || (result.images && result.images.length > 0));

  return (
    <div className="h-full min-h-0 overflow-auto p-3 space-y-2">
      <p className="text-[11px] text-[var(--neutral-500)]">
        Running <span className="text-[var(--neutral-300)] font-mono">{entryPath}</span>
      </p>
      <button
        type="button"
        onClick={handleRun}
        disabled={status === "loading"}
        className="flex items-center gap-1.5 text-[11px] px-2 py-1 rounded border border-[var(--neutral-800)] text-[var(--neutral-300)] hover:bg-white/5 disabled:opacity-60 transition-colors"
      >
        {status === "loading" ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
        {status === "loading" ? "Starting Python — first run loads the runtime (~10–20s)…" : status === "ok" ? "Run again" : "Run"}
      </button>
      {status === "error" && <div className="text-[11px] text-red-400 whitespace-pre-wrap">{error}</div>}
      {status === "ok" && hasOutput && (
        <div className="space-y-2">
          {result.stdout && (
            <pre className="overflow-x-auto p-2 text-xs text-[var(--neutral-300)] bg-black/60 rounded max-h-[240px] whitespace-pre-wrap">{result.stdout}</pre>
          )}
          {result.stderr && (
            <pre className="overflow-x-auto p-2 text-xs text-amber-400 bg-black/60 rounded max-h-[160px] whitespace-pre-wrap">{result.stderr}</pre>
          )}
        </div>
      )}
      {status === "ok" && !hasOutput && <p className="text-[11px] text-[var(--neutral-600)]">Ran with no output.</p>}
    </div>
  );
}

function EmptyState({ title, detail }) {
  return (
    <div className="h-full flex flex-col items-center justify-center gap-1 px-6 text-center">
      <p className="text-xs text-[var(--neutral-400)]">{title}</p>
      {detail && <p className="max-w-xs text-[11px] leading-relaxed text-[var(--neutral-600)]">{detail}</p>}
    </div>
  );
}

/**
 * @param {object} props
 * @param {object} props.provider - the active FileProvider (WorkbenchBody's own, same one CodeEditor/Explorer use)
 * @param {{[path: string]: object}|null} props.filesMeta - FileProvider.list()'s result; null until the first load lands
 * @param {{line: number, col: number}|null} [props.cursor] - W6.5: the ACTIVE editor file's caret (CodeEditor's own 1-based report); drives the reverse highlight
 * @param {(selection: {mm: string, element: object}) => void} [props.onSelectElement] - W6.5: a click in the preview's inspect mode, already sanitized by elementRef.js's elementFromMessage()
 */
function PreviewPane({ provider, filesMeta, cursor, onSelectElement }) {
  const detected = useMemo(() => detectKind(filesMeta || {}), [filesMeta]);

  if (!filesMeta) {
    return (
      <div className="h-full flex items-center gap-1.5 text-xs text-[var(--neutral-500)] justify-center">
        <Loader2 size={12} className="animate-spin" /> Loading…
      </div>
    );
  }

  if (detected.kind === "static") {
    return (
      <StaticPreview
        provider={provider}
        filesMeta={filesMeta}
        entryPath={detected.entryPath}
        cursor={cursor}
        onSelectElement={onSelectElement}
      />
    );
  }
  if (detected.kind === "python") {
    return <PythonPreview filesMeta={filesMeta} />;
  }
  if (detected.kind === "react") {
    return <ReactPreview provider={provider} filesMeta={filesMeta} cursor={cursor} onSelectElement={onSelectElement} />;
  }
  return <EmptyState title="No preview available for this project type" detail={detected.reason} />;
}

export default PreviewPane;
