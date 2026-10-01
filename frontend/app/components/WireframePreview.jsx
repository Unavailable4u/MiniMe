"use client";
import { useEffect, useRef, useState } from "react";
import { Code, Crosshair, RefreshCw, Send, X } from "lucide-react";
import { buildWireframeEditInstruction, buildWireframeFrame, selectionFromMessage } from "../lib/preview/wireframeInspect";
import { elementLabel } from "../lib/workbench/elementRef";

// New in Part 5 §5.5 -- the one genuinely new frontend component this
// part introduces (see Part 5 §5.7). Renders wireframe_sketcher's raw
// self-contained HTML block (a single ```html fenced code block, per
// that role's own brief) inside a sandboxed iframe, plus a small
// "request an edit" bar underneath.
//
// Edit round-trip: deliberately NOT wired to any dedicated backend
// endpoint or input_keys/stage_output mechanism -- there isn't one for
// cross-turn edits (see api/task_runner.py: a follow-up is just a
// normal POST /api/task reusing the same session_id, and generic_
// worker's ordinary conversation-memory prepend is what actually
// carries this component's own prior HTML forward to wireframe_
// sketcher's next hire -- see that role's brief). So `onRequestEdit`
// here is expected to be wired to the SAME chat-send function the main
// chat input already uses, just pre-seeded with an edit-shaped prompt
// -- not a new API call.
//
// sandbox="allow-scripts" only -- no allow-same-origin, no allow-forms,
// no allow-popups. wireframe_sketcher's own brief already forbids
// external scripts/stylesheets/CDN links (no network access in this
// sandbox anyway), so allow-scripts alone is enough for any inline
// interactivity a wireframe might sketch (e.g. a toggle) without
// granting the iframe access to this app's own origin, cookies, or
// parent DOM.
//
// W8.6 (Build Workbench plan) adds two things on top of that:
//
//  1. The workbench preview's point-and-select inspector. The crosshair in
//     the header turns on inspect mode; a click selects an element, and
//     the "Send edit" bar then says "make THIS button bigger" with that
//     element's description and exact source (wireframeInspect.js owns
//     all of that logic and is tested; this file only wires it to React).
//     The iframe's srcDoc is an instrumented COPY (data-mm stamped, runtime
//     injected) — the `html` prop, what the panel saves, and what "Turn
//     into code" sends are always the original text. The message listener
//     checks the same two things PreviewPane's does: the message came from
//     OUR iframe's window (a sandboxed frame has an opaque origin, so
//     event.origin is useless), and it carries THIS build's nonce.
//
//  2. "Turn into code": the code row under the preview. It is driven
//     entirely by props from the owner (BuildTab's WireframesPanel keeps
//     the pending-proposal state, since the proposal outlives this
//     component) — `codeView` is wireframeCode.js's wireframeCodeView().
//     Omit `onTurnIntoCode` and the row isn't rendered.

const REBUILD_DEBOUNCE_MS = 300; // the paste box above feeds `html` on every keystroke

// A fresh nonce per build — same reasoning as PreviewPane.jsx's makeNonce().
function makeNonce() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `mm-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function postToFrame(iframeRef, nonce, message) {
  iframeRef.current?.contentWindow?.postMessage({ source: "minime-parent", nonce, ...message }, "*");
}

export default function WireframePreview({
  html,
  screenLabel,
  onRequestEdit,
  onTurnIntoCode,
  onReviewCode,
  codeView,
  codeBusy,
  codeError,
}) {
  const [editText, setEditText] = useState("");
  const [sending, setSending] = useState(false);
  const [iframeKey, setIframeKey] = useState(0);
  const [frame, setFrame] = useState(null); // {srcDoc, inspectable, note} | null while building
  const [inspecting, setInspecting] = useState(false);
  const [selection, setSelection] = useState(null); // selectionFromMessage()'s result

  // What the message listener reads at message time: the iframe, the nonce
  // of the build now on screen, and the exact text that build was made from
  // (data-mm ranges are positions in THAT text, not in whatever `html` is
  // by the time a click arrives).
  const iframeRef = useRef(null);
  const nonceRef = useRef(null);
  const sourceRef = useRef("");
  const hasFrameRef = useRef(false);

  const hasContent = Boolean(html && html.trim());

  // Build the preview copy whenever the wireframe changes. The first build
  // is immediate; later ones are debounced. parse5 and the runtime are
  // loaded on first use so they stay out of BuildTab's own chunk.
  useEffect(() => {
    let cancelled = false;
    setSelection(null); // a selection points into text that no longer exists
    if (!hasContent) {
      nonceRef.current = null;
      sourceRef.current = "";
      hasFrameRef.current = false;
      setFrame(null);
      return undefined;
    }
    const timer = setTimeout(
      async () => {
        const nonce = makeNonce();
        let built;
        try {
          const [{ instrumentSource }, { injectInspectorRuntime }] = await Promise.all([
            import("../lib/preview/instrument"),
            import("../lib/preview/inspectorRuntime"),
          ]);
          built = await buildWireframeFrame({ html, nonce, instrument: instrumentSource, injectRuntime: injectInspectorRuntime });
        } catch {
          built = { srcDoc: html, inspectable: false, note: "Element inspector unavailable for this wireframe." };
        }
        if (cancelled) return;
        nonceRef.current = nonce;
        sourceRef.current = html;
        hasFrameRef.current = true;
        setFrame(built);
      },
      hasFrameRef.current ? REBUILD_DEBOUNCE_MS : 0
    );
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [html, hasContent]);

  // One listener for this component's lifetime, reading the CURRENT
  // iframe/nonce/source through the refs above.
  useEffect(() => {
    function onMessage(event) {
      if (!nonceRef.current) return;
      if (event.source !== iframeRef.current?.contentWindow) return;
      const data = event.data;
      if (!data || data.source !== "minime-preview" || data.nonce !== nonceRef.current) return;
      if (data.type === "minime:select") {
        // Untrusted data from inside the sandbox: selectionFromMessage()
        // type-checks and caps every field and ignores a range that isn't
        // in this wireframe.
        const picked = selectionFromMessage(data, sourceRef.current);
        if (!picked) return;
        setSelection(picked);
        // A pick ends inspect mode, same as the workbench preview.
        setInspecting(false);
        postToFrame(iframeRef, nonceRef.current, { type: "minime:inspect", on: false });
        return;
      }
      if (data.type === "minime:inspectExited") setInspecting(false);
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  function handleToggleInspect() {
    const next = !inspecting;
    setInspecting(next);
    postToFrame(iframeRef, nonceRef.current, { type: "minime:inspect", on: next });
  }

  // A rebuild (or the reload button) swaps in a fresh runtime whose own
  // inspect mode starts off — re-send ours so the crosshair and the frame
  // agree.
  function handleIframeLoad() {
    postToFrame(iframeRef, nonceRef.current, { type: "minime:inspect", on: inspecting });
  }

  async function submitEdit() {
    const instruction = editText.trim();
    if (!instruction || !onRequestEdit) return;
    setSending(true);
    try {
      await onRequestEdit(buildWireframeEditInstruction({ screenLabel, instruction, selection }));
      setEditText("");
      setSelection(null);
    } finally {
      setSending(false);
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitEdit();
    }
  }

  const inspectable = Boolean(frame?.inspectable);
  const selectionName = selection ? elementLabel(selection.element) : "";

  return (
    <div className="rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-950-a50)] overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--neutral-800)]">
        <span className="text-xs font-medium text-[var(--neutral-400)]">
          {screenLabel || "Wireframe preview"}
        </span>
        <div className="flex items-center gap-0.5">
          {inspectable && (
            <button
              type="button"
              onClick={handleToggleInspect}
              title={inspecting ? "Stop inspecting (Esc)" : "Inspect element"}
              aria-label="Inspect element"
              aria-pressed={inspecting}
              className={`touch-target p-1 rounded-md ${
                inspecting ? "bg-[var(--accent)] text-[var(--accent-text)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
              }`}
            >
              <Crosshair size={12} />
            </button>
          )}
          {hasContent && (
            <button
              type="button"
              onClick={() => setIframeKey((k) => k + 1)}
              title="Reload preview"
              className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] p-1 rounded-md"
            >
              <RefreshCw size={12} />
            </button>
          )}
        </div>
      </div>

      {hasContent && frame ? (
        <iframe
          key={iframeKey}
          ref={iframeRef}
          onLoad={handleIframeLoad}
          srcDoc={frame.srcDoc}
          sandbox="allow-scripts"
          title={screenLabel || "Wireframe preview"}
          className="w-full bg-white"
          style={{ height: "420px", border: "none" }}
        />
      ) : (
        <div className="flex items-center justify-center h-[420px] text-xs text-[var(--neutral-600)]">
          {hasContent ? "Loading preview…" : "No wireframe generated yet."}
        </div>
      )}

      {frame?.note && (
        <p className="border-t border-[var(--neutral-800)] px-3 py-1.5 text-[10px] text-amber-400">{frame.note}</p>
      )}

      {inspecting && !selection && (
        <p className="border-t border-[var(--neutral-800)] px-3 py-1.5 text-[11px] text-[var(--neutral-500)]">
          Click an element in the wireframe to edit just that element. Esc cancels.
        </p>
      )}

      {selection && (
        <div
          className="flex items-center gap-2 border-t border-[var(--neutral-800)] px-3 py-1.5 font-mono text-[10px] text-[var(--neutral-400)]"
          title={selection.mm}
        >
          <Crosshair size={11} className="shrink-0 text-[var(--accent)]" />
          <span className="truncate">{selectionName}</span>
          <span className="shrink-0 text-[var(--neutral-600)]">line {selection.line}</span>
          {selection.element.dynamic && <span className="shrink-0 text-amber-400">dynamic — nearest source element</span>}
          {selection.element.instanceCount > 1 && (
            <span className="shrink-0 text-[var(--neutral-500)]">×{selection.element.instanceCount}</span>
          )}
          <button
            type="button"
            onClick={() => setSelection(null)}
            title="Clear selection"
            aria-label="Clear selection"
            className="ml-auto shrink-0 text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
          >
            <X size={11} />
          </button>
        </div>
      )}

      {onTurnIntoCode && (
        <div className="border-t border-[var(--neutral-800)] px-3 py-2 space-y-1.5 text-[11px]">
          {codeView?.message && <p className="text-[var(--neutral-500)]">{codeView.message}</p>}
          {codeView?.notice && <p className="text-amber-400">{codeView.notice}</p>}
          {codeError && <p className="text-rose-400">{codeError}</p>}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={codeBusy || codeView?.kind === "blocked"}
              onClick={onTurnIntoCode}
              title={codeView?.kind === "review" ? "Files this wireframe again and replaces the one waiting for review" : undefined}
              className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50 ${
                codeView?.kind === "review"
                  ? "border border-[var(--neutral-700)] text-[var(--neutral-300)] hover:bg-[var(--neutral-800)]"
                  : "bg-[var(--accent)] text-[var(--accent-text)]"
              }`}
            >
              <Code size={12} />
              {codeBusy ? "Sending…" : codeView?.kind === "review" ? "Send again" : "Turn into code"}
            </button>
            {codeView?.kind === "review" && onReviewCode && (
              <button
                type="button"
                disabled={codeBusy}
                onClick={onReviewCode}
                className="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-[var(--accent-text)] disabled:opacity-50"
              >
                Review code
              </button>
            )}
          </div>
        </div>
      )}

      {onRequestEdit && (
        <div className="border-t border-[var(--neutral-800)] px-3 py-2 flex items-end gap-2">
          <textarea
            id="wireframe-edit-text"
            name="wireframeEditText"
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={sending}
            placeholder={
              selection
                ? `Describe an edit to the selected ${selectionName}`
                : "Describe an edit, e.g. 'make the primary button bigger'"
            }
            rows={1}
            className="flex-1 resize-none bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)] leading-relaxed"
          />
          <button
            type="button"
            disabled={sending || !editText.trim()}
            onClick={submitEdit}
            className="flex items-center gap-1.5 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50"
          >
            <Send size={12} />
            {sending ? "Sending…" : "Send edit"}
          </button>
        </div>
      )}
    </div>
  );
}
