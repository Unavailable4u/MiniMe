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
//   - "react": detected, but W6.6 is the patch that actually renders
//     it (Sandpack) — a clear "coming later" message, not a dead end.
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
import { AlertTriangle, ExternalLink, Loader2, Monitor, Play, RefreshCw, Smartphone, Tablet } from "lucide-react";
import { useEditorStore } from "../../lib/workbench/editorStore";
import { detectKind } from "../../lib/preview/detectKind";
import { bundleStatic } from "../../lib/preview/bundleStatic";
import { usePyodideWorker } from "../../hooks/usePyodideWorker";

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

function DeviceToolbar({ device, onDeviceChange, onReload, onOpenNewTab, warnings }) {
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

function StaticPreview({ provider, filesMeta, entryPath }) {
  const { state } = useEditorStore();
  const [device, setDevice] = useState("desktop");
  const [built, setBuilt] = useState(null); // {html, warnings} | null while building the first time
  const [buildError, setBuildError] = useState(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const cacheRef = useRef(new Map());
  const debounceRef = useRef(null);

  const buffers = state.buffers;

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      try {
        const entryContent = buffers[entryPath]?.edited ?? (await provider.read(entryPath)).content ?? "";
        const resolveFile = makeResolver({ provider, filesMeta, buffers, cacheRef });
        const result = await bundleStatic({ entryPath, entryContent, resolveFile });
        setBuilt(result);
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
      <DeviceToolbar device={device} onDeviceChange={setDevice} onReload={handleReload} onOpenNewTab={handleOpenNewTab} warnings={built?.warnings} />
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
 */
function PreviewPane({ provider, filesMeta }) {
  const detected = useMemo(() => detectKind(filesMeta || {}), [filesMeta]);

  if (!filesMeta) {
    return (
      <div className="h-full flex items-center gap-1.5 text-xs text-[var(--neutral-500)] justify-center">
        <Loader2 size={12} className="animate-spin" /> Loading…
      </div>
    );
  }

  if (detected.kind === "static") {
    return <StaticPreview provider={provider} filesMeta={filesMeta} entryPath={detected.entryPath} />;
  }
  if (detected.kind === "python") {
    return <PythonPreview filesMeta={filesMeta} />;
  }
  if (detected.kind === "react") {
    return <EmptyState title="React preview is coming soon" detail="This looks like a React project (package.json + a .jsx/.tsx file) — a live React preview isn't wired up yet." />;
  }
  return <EmptyState title="No preview available for this project type" detail={detected.reason} />;
}

export default PreviewPane;
