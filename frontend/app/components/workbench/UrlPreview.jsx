"use client";
// frontend/app/components/workbench/UrlPreview.jsx — W7.3 (Build
// Workbench plan). "Localhost preview URL": type your dev server's
// address (http://localhost:5173) and it renders in the preview column,
// no daemon needed. With the one-line inspector snippet added to the
// app, the crosshair works on it too: click an element → the workbench
// opens the matching source range and adds the element chip, exactly as
// it does for the static/React file previews (W6.5).
//
// Mounted by PreviewPane.jsx when its source tab is "Dev server URL".
// Everything that can be decided without a DOM lives in lib/preview/
// (previewUrl.js: which URLs are allowed and the snippet; urlBridge.js:
// which messages to trust; sourceRef.js: turning the page's attribute
// into a workspace range) — this file is wiring and markup.
//
// SECURITY POSTURE, and the one place it deliberately differs from the
// other previews (plan §7 item 4: "sandbox=allow-scripts, no
// allow-same-origin"): that rule assumes the frame holds text MiniMe
// generated. A real dev server needs `allow-same-origin` — an opaque
// origin can't use localStorage/cookies and gets its ES-module requests
// refused by any dev server whose CORS allowlist isn't "*". Scripts +
// same-origin is only safe when the framed page is on a DIFFERENT origin
// from MiniMe (it then keeps its own origin and can't touch ours), which
// previewUrl.js's normalizePreviewUrl() enforces: loopback hosts only,
// never MiniMe's own origin. No allow-top-navigation, so the framed app
// can't redirect the whole workbench away. The frame is also given
// `referrerPolicy="no-referrer"` so the app isn't told where MiniMe
// lives.
//
// Not in this first cut, on purpose: the reverse highlight (editor
// cursor → outline in the preview) — a Locator path attribute has no end
// position, so the frame can't say which element contains a cursor
// without the parent parsing files it hasn't been asked to read — and
// the console bridge (W6.2) for external pages.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, Copy } from "lucide-react";
import { useEditorStore } from "../../lib/workbench/editorStore";
import { elementFromMessage } from "../../lib/workbench/elementRef";
import { buildInspectorSnippet, normalizePreviewUrl } from "../../lib/preview/previewUrl";
import { acceptFrameMessage, buildParentMessage, makeBridgeNonce } from "../../lib/preview/urlBridge";
import { resolveSelectRef } from "../../lib/preview/sourceRef";
import { DEVICE_PRESETS, DeviceToolbar } from "./DeviceToolbar";

function noticeFor(error) {
  if (error.error === "not-in-workspace") {
    return `That element comes from ${error.path}, which isn't a file in this workspace.`;
  }
  if (error.error === "unreadable-file") return `Couldn't read ${error.path} to find the element.`;
  return "That element's source tag couldn't be read.";
}

/**
 * @param {object} props
 * @param {object} props.provider - the active FileProvider (same one the editor uses)
 * @param {{[path: string]: object}} props.filesMeta - FileProvider.list()'s result; its keys are what frame paths are matched against
 * @param {string} props.value - the saved URL ("" when none)
 * @param {(url: string) => void} props.onValueChange - persists a newly loaded URL
 * @param {(selection: {mm: string, element: object}) => void} [props.onSelectElement] - EditorWorkbench's click-to-code handler
 */
function UrlPreview({ provider, filesMeta, value, onValueChange, onSelectElement }) {
  const { state } = useEditorStore();
  const [device, setDevice] = useState("desktop");
  const [draft, setDraft] = useState(value || "");
  const [formError, setFormError] = useState(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [inspecting, setInspecting] = useState(false);
  // idle → (iframe load) loading → connected | waiting (no handshake in time)
  const [link, setLink] = useState("idle");
  const [tagged, setTagged] = useState(null); // null until the frame says; false = page has no source attributes
  const [lastSelection, setLastSelection] = useState(null);
  const [notice, setNotice] = useState(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const iframeRef = useRef(null);
  // One nonce per mounted preview; delivered to the frame by handshake
  // (urlBridge.js's header explains why it can't be baked in).
  const nonceRef = useRef(null);
  if (nonceRef.current === null) nonceRef.current = makeBridgeNonce();
  const inspectingRef = useRef(false);
  inspectingRef.current = inspecting;
  // Latest inputs for the message listener (subscribed once per URL, not
  // per render), same ref pattern PreviewPane's own listener uses.
  const latest = useRef({});
  latest.current = { provider, filesMeta, buffers: state.buffers, onSelectElement };

  const ownOrigin = typeof window !== "undefined" ? window.location.origin : "";
  const target = useMemo(() => (value ? normalizePreviewUrl(value, ownOrigin) : null), [value, ownOrigin]);

  // Workspace switch / first prefs load changes `value` from outside.
  useEffect(() => {
    setDraft(value || "");
  }, [value]);

  // A new address or a manual reload is a brand-new frame: nothing from
  // the previous one (link state, inspect mode, selection) carries over.
  const targetUrl = target?.ok ? target.url : null;
  useEffect(() => {
    setLink("idle");
    setTagged(null);
    setInspecting(false);
    setNotice(null);
    setLastSelection(null);
  }, [targetUrl, reloadNonce]);

  // No handshake within a few seconds of the frame loading: either the
  // snippet isn't installed or the server refused to be embedded. The
  // parent cannot tell those apart (the frame is cross-origin), so the
  // hint says both.
  useEffect(() => {
    if (link !== "loading") return undefined;
    const timer = setTimeout(() => setLink((l) => (l === "loading" ? "waiting" : l)), 4000);
    return () => clearTimeout(timer);
  }, [link]);

  const postToFrame = useCallback(
    (message) => {
      if (!target?.ok) return;
      iframeRef.current?.contentWindow?.postMessage(buildParentMessage(nonceRef.current, message), target.origin);
    },
    [target]
  );

  useEffect(() => {
    if (!target?.ok) return undefined;

    async function handleSelect(data) {
      const { provider: prov, filesMeta: meta, buffers, onSelectElement: onSelect } = latest.current;
      const resolved = await resolveSelectRef(data, {
        knownPaths: Object.keys(meta || {}),
        // A live buffer when the file is open (unsaved edits included) —
        // the closest thing to the text the running build was made from.
        readText: async (path) => (buffers[path] ? buffers[path].edited : (await prov.read(path)).content ?? ""),
      });
      if (resolved.error) {
        // Stay in inspect mode: the person can just click something else.
        setNotice(noticeFor(resolved));
        return;
      }
      // Untrusted payload again, now with the resolved workspace mm:
      // elementFromMessage() type-checks and length-caps every field.
      const selection = elementFromMessage({ ...data, mm: resolved.mm });
      if (!selection) return;
      setNotice(resolved.approx ? "Selected the element's opening tag — its full range couldn't be found in the file." : null);
      setLastSelection({ mm: selection.mm, ...selection.element });
      onSelect?.(selection);
      // A pick ends inspect mode (same as the file previews): focus has
      // moved to the editor, where Esc can no longer reach the frame.
      setInspecting(false);
      postToFrame({ type: "minime:inspect", on: false });
    }

    function onMessage(event) {
      const accepted = acceptFrameMessage(event, {
        frameWindow: iframeRef.current?.contentWindow,
        origin: target.origin,
        nonce: nonceRef.current,
      });
      if (!accepted) return;
      const { type, data } = accepted;
      if (type === "minime:ready") {
        setLink("connected");
        setTagged(data.tagged === true);
        // Hand the frame this mount's nonce, then bring it up to date: a
        // reloaded page starts with inspect mode off.
        postToFrame({ type: "minime:hello" });
        postToFrame({ type: "minime:inspect", on: inspectingRef.current });
        return;
      }
      if (type === "minime:inspectExited") {
        setInspecting(false);
        return;
      }
      if (type === "minime:select") handleSelect(data);
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [target, postToFrame]);

  function handleSubmit(event) {
    event.preventDefault();
    const result = normalizePreviewUrl(draft, ownOrigin);
    if (!result.ok) {
      setFormError(result.error);
      return;
    }
    setFormError(null);
    setDraft(result.url);
    if (result.url === value) setReloadNonce((n) => n + 1); // same address again = reload
    else onValueChange(result.url);
  }

  function handleToggleInspect() {
    const next = !inspecting;
    setInspecting(next);
    postToFrame({ type: "minime:inspect", on: next });
  }

  function handleOpenNewTab() {
    if (target?.ok) window.open(target.url, "_blank", "noopener,noreferrer");
  }

  async function handleCopy(text) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard blocked: the snippet is selectable text right there.
    }
  }

  const preset = DEVICE_PRESETS.find((d) => d.id === device);
  const snippet = buildInspectorSnippet(ownOrigin);
  const invalidSaved = target && !target.ok ? target.error : null;

  let statusLine = null;
  if (target?.ok) {
    if (link === "connected") {
      statusLine =
        tagged === false
          ? { tone: "warn", text: "Inspector connected, but this page has no source tags (data-mm or Locator) — elements can't be mapped to code yet." }
          : { tone: "ok", text: "Inspector connected — turn on the crosshair and click an element." };
    } else if (link === "waiting") {
      statusLine = {
        tone: "warn",
        text: "No inspector detected. Add the snippet below to enable click-to-code. A blank or error page usually means the dev server isn't running or forbids embedding (X-Frame-Options / frame-ancestors) — try “Open in new tab”.",
      };
    } else {
      statusLine = { tone: "idle", text: "Loading…" };
    }
  }
  const toneClass = { ok: "text-[var(--neutral-400)]", warn: "text-amber-400", idle: "text-[var(--neutral-500)]" };

  return (
    <div className="h-full min-h-0 flex flex-col">
      <form onSubmit={handleSubmit} className="shrink-0 flex items-center gap-1 px-2 py-1.5 border-b border-[var(--neutral-800)]">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="http://localhost:5173"
          aria-label="Dev server URL"
          spellCheck={false}
          autoComplete="off"
          className="flex-1 min-w-0 px-2 py-1 rounded border border-[var(--neutral-800)] bg-[var(--neutral-900)] text-[11px] font-mono text-[var(--neutral-200)] placeholder:text-[var(--neutral-600)] focus:outline-none focus:border-[var(--neutral-600)]"
        />
        <button
          type="submit"
          className="touch-target shrink-0 px-2 py-1 rounded text-[11px] border border-[var(--neutral-800)] text-[var(--neutral-300)] hover:bg-white/5"
        >
          {target?.ok && draft.trim() === value ? "Reload" : "Go"}
        </button>
      </form>
      {(formError || invalidSaved) && <p className="shrink-0 px-2 py-1 text-[11px] text-red-400 border-b border-[var(--neutral-800)]">{formError || invalidSaved}</p>}

      <DeviceToolbar
        device={device}
        onDeviceChange={setDevice}
        onReload={() => setReloadNonce((n) => n + 1)}
        onOpenNewTab={handleOpenNewTab}
        inspecting={inspecting}
        onToggleInspect={target?.ok ? handleToggleInspect : undefined}
      />

      {statusLine && <p className={`shrink-0 px-2 py-1 text-[10px] leading-snug border-b border-[var(--neutral-800)] ${toneClass[statusLine.tone]}`}>{statusLine.text}</p>}
      {notice && <p className="shrink-0 px-2 py-1 text-[10px] leading-snug text-amber-400 border-b border-[var(--neutral-800)]">{notice}</p>}
      {lastSelection && (
        <div className="shrink-0 flex items-center gap-2 px-2 py-1 border-b border-[var(--neutral-800)] font-mono text-[10px] text-[var(--neutral-400)]" title={lastSelection.mm}>
          <span className="truncate">
            {lastSelection.tag}
            {lastSelection.classes.length > 0 ? `.${lastSelection.classes.join(".")}` : ""}
          </span>
          <span className="shrink-0 text-[var(--neutral-600)]">{lastSelection.mm}</span>
          {lastSelection.instanceCount > 1 && <span className="shrink-0 text-[var(--neutral-500)]">×{lastSelection.instanceCount}</span>}
        </div>
      )}

      <div className="shrink-0 border-b border-[var(--neutral-800)]">
        <button
          type="button"
          onClick={() => setSetupOpen((v) => !v)}
          aria-expanded={setupOpen}
          className="w-full flex items-center gap-1 px-2 py-1 text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
        >
          {setupOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          Set up click-to-code
        </button>
        {setupOpen && (
          <div className="px-2 pb-2 space-y-1.5 text-[11px] leading-relaxed text-[var(--neutral-400)]">
            <p>1. Add this to your app&rsquo;s HTML (dev only) — it does nothing outside this preview:</p>
            <div className="flex items-start gap-1">
              <pre className="flex-1 min-w-0 overflow-x-auto p-1.5 rounded bg-black/60 text-[10px] text-[var(--neutral-300)]">{snippet}</pre>
              <button
                type="button"
                onClick={() => handleCopy(snippet)}
                title="Copy"
                aria-label="Copy snippet"
                className="touch-target shrink-0 p-1 rounded text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
              >
                {copied ? <Check size={12} /> : <Copy size={12} />}
              </button>
            </div>
            <p>
              2. Tag your elements with their source file and line using{" "}
              <span className="font-mono text-[var(--neutral-300)]">@locator/babel-jsx</span> (Vite/Babel) or{" "}
              <span className="font-mono text-[var(--neutral-300)]">@locator/webpack-loader</span> (webpack/Next). Both of its attribute styles are understood.
            </p>
          </div>
        )}
      </div>

      <div className="flex-1 min-h-0 overflow-auto bg-[var(--neutral-900)] flex justify-center">
        {!target?.ok ? (
          <div className="h-full flex flex-col items-center justify-center gap-1 px-6 text-center">
            <p className="text-xs text-[var(--neutral-400)]">Preview your running dev server</p>
            <p className="max-w-xs text-[11px] leading-relaxed text-[var(--neutral-600)]">
              Start it on your machine, then enter its address above (for example http://localhost:5173).
            </p>
          </div>
        ) : (
          <iframe
            key={`${target.url}#${reloadNonce}`}
            ref={iframeRef}
            onLoad={() => setLink((l) => (l === "connected" ? l : "loading"))}
            title="Dev server preview"
            src={target.url}
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals"
            referrerPolicy="no-referrer"
            className="bg-white h-full shrink-0"
            style={{ width: preset.width ?? "100%", border: "none" }}
          />
        )}
      </div>
    </div>
  );
}

export default UrlPreview;
