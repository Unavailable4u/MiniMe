"use client";
import { useMemo, useState, memo } from "react";
import { Maximize2, Minimize2, Code2, ExternalLink, Play, Loader2 } from "lucide-react";
import { usePyodideWorker } from "../hooks/usePyodideWorker";
import { SandpackProvider, SandpackPreview } from "@codesandbox/sandpack-react";

// Phase CO, CO2 (Master Guide v2, §5) — interactive chat output beyond
// markdown/Mermaid. Renders one artifact ({ type, title, code }) from
// result.artifacts as a bordered card, same "model-authored content can
// legitimately fail to render, degrade gracefully" posture
// MermaidDiagram.jsx already applies to its own failure case.
//
// Build order (CO2's own, cheapest setup first): html/svg iframe landed
// first, then python/Pyodide, which runs real CPython-via-WebAssembly in
// a Web Worker (usePyodideWorker.js) so a long-running script never
// freezes the chat UI -- loaded on-demand via Run, not on render, so an
// unused python artifact costs nothing. react/Sandpack (this patch,
// final in the sequence) is the heaviest of the three -- it ships its own
// bundler/transpiler and mounts its own internal iframe, so it's mounted
// directly rather than routed through wrapAsHtmlDoc like html/svg.
// Unknown/unimplemented types still fall back to a read-only source card
// instead of crashing.
//
// Security: html/svg run inside sandbox="allow-scripts" ONLY — no
// allow-same-origin, so model-authored code never shares an origin with
// the app (can't read cookies/localStorage, can't call same-origin APIs)
// and can't navigate the parent window, open new windows, or submit
// forms. Sandpack manages its own internal iframe's sandbox attribute
// (it needs allow-same-origin there so its bundler can write the
// transpiled module graph into that iframe's document) -- that iframe is
// still a distinct, cross-origin context from this app's origin
// (Sandpack's bundler runtime is CDN-hosted by default), so it still
// can't read this app's cookies/localStorage. This is entirely
// client-side rendering — nothing here is ever sent to the backend to
// execute.
const TYPE_LABELS = {
  html: "HTML",
  svg: "SVG",
  python: "Python",
  react: "React",
};

function wrapAsHtmlDoc(type, code) {
  if (type === "svg") {
    // Guide's own suggested shortcut for the svg case: render it through
    // the same iframe path as html rather than a separate
    // dangerouslySetInnerHTML + sanitizer dependency — one code path,
    // one sandbox guarantee, for both shapes.
    return `<!doctype html><html><head><style>html,body{margin:0;padding:0;background:transparent;display:flex;align-items:center;justify-content:center;height:100%;}svg{max-width:100%;max-height:100%;}</style></head><body>${code}</body></html>`;
  }
  return code;
}

function PythonArtifact({ code }) {
  const { run } = usePyodideWorker();
  const [status, setStatus] = useState("idle"); // idle | loading | ok | error
  const [result, setResult] = useState(null); // { stdout, stderr, images }
  const [error, setError] = useState(null);

  async function handleRun() {
    setStatus("loading");
    setError(null);
    try {
      const payload = await run(code);
      setResult(payload);
      setStatus("ok");
    } catch (err) {
      setError(err?.message || String(err));
      setStatus("error");
    }
  }

  const hasOutput = result && (result.stdout || result.stderr || (result.images && result.images.length > 0));

  return (
    <div>
      <pre className="overflow-x-auto p-2.5 text-xs text-[var(--neutral-300)] bg-black/50 max-h-[320px]">
        <code>{code}</code>
      </pre>
      <div className="px-2.5 py-2 space-y-2">
        <button
          type="button"
          onClick={handleRun}
          disabled={status === "loading"}
          className="flex items-center gap-1.5 text-[11px] px-2 py-1 rounded border border-[var(--neutral-800)] text-[var(--neutral-300)] hover:bg-white/5 disabled:opacity-60 transition-colors"
        >
          {status === "loading" ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
          {status === "loading"
            ? "Starting Python — first run loads the runtime (~10–20s)…"
            : status === "ok"
            ? "Run again"
            : "Run"}
        </button>

        {status === "error" && (
          <div className="text-[11px] text-red-400 whitespace-pre-wrap">{error}</div>
        )}

        {status === "ok" && result && (
          <div className="space-y-2">
            {result.stdout && (
              <pre className="overflow-x-auto p-2 text-xs text-[var(--neutral-300)] bg-black/60 rounded max-h-[240px] whitespace-pre-wrap">
                {result.stdout}
              </pre>
            )}
            {result.stderr && (
              <pre className="overflow-x-auto p-2 text-xs text-amber-400 bg-black/60 rounded max-h-[160px] whitespace-pre-wrap">
                {result.stderr}
              </pre>
            )}
            {/* BUGFIX (Maximum update depth exceeded / react-window
                useVirtualizer setIndices crash) — same class as
                Markdown.jsx's img renderer fix. This plot output lives
                inside WorkspaceChatPanel's virtualized message list,
                whose row height is measured via react-window v2's
                useDynamicRowHeight (a ResizeObserver under the hood).
                An <img> with only max-width has no height reserved
                before it decodes, so the row's measured height differs
                between the pre-decode and post-decode passes — same
                "measure -> setIndices -> row resizes -> measure again"
                non-convergence Markdown.jsx's own fix comment describes.
                Unlike Markdown's img (which can guess a 16/9
                aspect-ratio default), a matplotlib-style plot's real
                aspect ratio varies too much for one guess to fit well,
                so this reserves a fixed height (not max-height, which
                only caps the POST-decode size and does nothing for the
                PRE-decode 0-height pass) with object-contain so the
                image scales inside that fixed box either way. */}
            {result.images?.map((b64, i) => (
              <img
                key={i}
                src={`data:image/png;base64,${b64}`}
                alt={`Plot ${i + 1}`}
                className="max-w-full rounded border border-[var(--neutral-800)] object-contain"
                style={{ height: 360, width: "100%" }}
              />
            ))}
            {!hasOutput && (
              <div className="text-[11px] text-[var(--neutral-500)]">Ran with no output.</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// Sandpack needs an entry file, not a bare component body — the model's
// `code` is expected to be a full App.jsx that default-exports a
// component (same convention artifact-authoring prompts already use for
// "react" type). If code doesn't include an export default, Sandpack
// will fail to bundle and show its own inline compile error, which is an
// acceptable degrade — it's still contained inside the card, not a page
// crash.
function ReactArtifact({ code, expanded }) {
  const files = useMemo(
    () => ({
      "/App.js": code || "export default function App() { return null; }",
    }),
    [code]
  );

  return (
    <SandpackProvider template="react" files={files} theme="dark">
      {/* Preview-only, no editor pane — this card's existing "Show
          source" toggle (same one html/svg/python use) already covers
          reading the code, so we don't need Sandpack's own editor/tabs
          UI duplicating that inside a chat-width card. */}
      <SandpackPreview
        showOpenInCodeSandbox={false}
        showRefreshButton
        style={{ height: expanded ? "70vh" : "320px" }}
      />
    </SandpackProvider>
  );
}

function ArtifactRenderer({ artifact }) {
  const { type, title, code } = artifact || {};
  const [expanded, setExpanded] = useState(false);
  const [showSource, setShowSource] = useState(false);
  const live = type === "html" || type === "svg";
  const isReact = type === "react";
  // Show source / Expand apply to any type with a real live preview;
  // Open in new tab stays iframe-srcDoc-only below, since Sandpack
  // manages its own separate preview surface rather than a srcDoc we
  // can hand to window.open as a static blob.
  const hasToolbar = live || isReact;

  const srcDoc = useMemo(() => (live ? wrapAsHtmlDoc(type, code || "") : null), [live, type, code]);

  function openInNewTab() {
    // Closest thing to CO4's eventual "expand to full width" for this
    // patch — a real full-page view, just outside the chat layout rather
    // than inline within it. CO4 is what wires an inline full-width mode
    // into the Working Panel; this is a working stand-in until then.
    // React/Sandpack doesn't get this button (see hasToolbar) since
    // there's no static srcDoc to hand off — Sandpack's own preview is
    // itself a live iframe, not a blob URL this app owns.
    if (!live || !srcDoc) return;
    const blob = new Blob([srcDoc], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank", "noopener,noreferrer");
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  if (!artifact || !code) return null;

  return (
    <div className="mt-2 rounded-lg border border-[var(--neutral-800)] bg-black/30 overflow-hidden">
      <div className="flex items-center justify-between gap-2 px-2.5 py-1.5 border-b border-[var(--neutral-800-a70)]">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[10px] font-medium uppercase tracking-wide text-[var(--neutral-500)] shrink-0">
            {TYPE_LABELS[type] || type || "artifact"}
          </span>
          {title && (
            <span className="text-[11px] text-[var(--neutral-300)] truncate">{title}</span>
          )}
        </div>
        {hasToolbar && (
          <div className="flex items-center gap-1 shrink-0">
            <button
              type="button"
              onClick={() => setShowSource((s) => !s)}
              title={showSource ? "Show preview" : "Show source"}
              className="p-1 rounded text-[var(--neutral-500)] hover:text-[var(--neutral-300)] hover:bg-white/5 transition-colors"
            >
              <Code2 size={13} />
            </button>
            {live && (
              <button
                type="button"
                onClick={openInNewTab}
                title="Open in new tab"
                className="p-1 rounded text-[var(--neutral-500)] hover:text-[var(--neutral-300)] hover:bg-white/5 transition-colors"
              >
                <ExternalLink size={13} />
              </button>
            )}
            <button
              type="button"
              onClick={() => setExpanded((e) => !e)}
              title={expanded ? "Collapse" : "Expand"}
              className="p-1 rounded text-[var(--neutral-500)] hover:text-[var(--neutral-300)] hover:bg-white/5 transition-colors"
            >
              {expanded ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
            </button>
          </div>
        )}
      </div>

      {live ? (
        showSource ? (
          <pre className="overflow-x-auto p-3 text-xs text-[var(--neutral-300)] bg-black/50 max-h-[480px]">
            <code>{code}</code>
          </pre>
        ) : (
          <iframe
            title={title || `${type} artifact`}
            srcDoc={srcDoc}
            sandbox="allow-scripts"
            className="w-full bg-white"
            style={{ height: expanded ? "70vh" : "320px", border: "none" }}
          />
        )
      ) : type === "python" ? (
        <PythonArtifact code={code} />
      ) : type === "react" ? (
        showSource ? (
          <pre className="overflow-x-auto p-3 text-xs text-[var(--neutral-300)] bg-black/50 max-h-[480px]">
            <code>{code}</code>
          </pre>
        ) : (
          <ReactArtifact code={code} expanded={expanded} />
        )
      ) : (
        // any future/unimplemented type — readable code, no execution,
        // no crash.
        <div className="p-2.5 space-y-1.5">
          <div className="text-[11px] text-[var(--neutral-500)]">
            Live preview for {TYPE_LABELS[type] || type} artifacts isn&apos;t wired up yet — showing source.
          </div>
          <pre className="overflow-x-auto p-2 text-xs text-[var(--neutral-300)] bg-black/50 rounded max-h-[400px]">
            <code>{code}</code>
          </pre>
        </div>
      )}
    </div>
  );
}

export default memo(ArtifactRenderer);
