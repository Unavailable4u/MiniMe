"use client";
import { useEffect, useRef, useState } from "react";
import { useSession } from "../../context/SessionContext";
import { UploadCloud, Link2, CheckCircle2, XCircle, Loader2, Mic, AlertTriangle } from "lucide-react";
import { OFFICE_EXTS, PDF_EXT, AUDIO_EXTS, YOUTUBE_RE, ingestFileByExtension, withTimeout } from "../../lib/ingestDispatch";
// CHANGED — Data Layer §4b: the extension groups, YOUTUBE_RE, withTimeout,
// and the per-file ingestor dispatch used to be defined locally in this
// file. They now live in lib/ingestDispatch.js so the chat composer's new
// attach button can reuse the exact same rules — no behavior change here.

// How long a "done" row stays visible before it auto-clears from the
// progress list.
const DONE_AUTOCLEAR_MS = 3000;

// One row per in-flight or completed ingestion — the "progress list"
// half of §4.7's "one drop-target and progress list shared across
// formats."
function ProgressRow({ item }) {
  return (
    <div className="flex items-center gap-2 px-3 py-2 text-xs border-b border-[var(--neutral-900)] last:border-b-0">
      {item.status === "pending" && <Loader2 size={13} className="animate-spin text-[var(--neutral-500)] shrink-0" />}
      {item.status === "done" && <CheckCircle2 size={13} className="text-green-400 shrink-0" />}
      {item.status === "error" && <XCircle size={13} className="text-red-400 shrink-0" />}
      {item.status === "timeout" && <AlertTriangle size={13} className="text-amber-400 shrink-0" />}
      <span className="truncate text-[var(--neutral-300)] flex-1">{item.name}</span>
      <span
        className={`shrink-0 ${
          item.status === "error" ? "text-red-400" : item.status === "timeout" ? "text-amber-400" : "text-[var(--neutral-500)]"
        }`}
      >
        {item.status === "pending" ? "Ingesting…" : item.message}
      </span>
    </div>
  );
}

export default function IngestionDropzone({ workspaceId, onIngested }) {
  const { ingestClip, ingestVideoUrl, ingestFile, ingestPdfFile, ingestVoiceFile } = useSession();
  const [items, setItems] = useState([]);
  const [dragOver, setDragOver] = useState(false);
  const [urlDraft, setUrlDraft] = useState("");
  const inputRef = useRef(null);
  // FIX — this component unmounts whenever the sub-tab or notebook
  // selection changes away from Sources while an upload is still in
  // flight. Without this guard, the awaited ingest call still resolves
  // and calls setItems on a component that's no longer mounted (a no-op
  // at best, a console warning at worst). The *data* race — a slow
  // upload's onIngested firing after the user has moved to a different
  // notebook — is guarded separately in NotebooksTab.loadNotebookData,
  // since that's the state that actually gets displayed.
  //
  // FIX — the ref must be reset to true on every mount, not just set
  // once at useRef(true). In dev, React StrictMode deliberately mounts
  // every component twice (mount -> unmount -> remount) to surface
  // exactly this class of bug: the first phantom unmount's cleanup set
  // mountedRef.current = false, and with only a bare useEffect(() => ()
  // => {...}, []) — no reset in the effect body itself — it was never
  // flipped back to true on the real mount that followed. Every
  // settleItem() call after that silently no-op'd, so a finished
  // upload's row stayed stuck on "pending"/"Ingesting…" forever; only a
  // full page refresh (which wipes this component's state entirely)
  // made the stuck row go away.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  function pushItem(name) {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setItems((prev) => [{ id, name, status: "pending", message: "" }, ...prev]);
    return id;
  }

  function settleItem(id, status, message) {
    if (!mountedRef.current) return;
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, status, message } : it)));
    // Auto-clear completed rows after a few seconds so the progress
    // list doesn't just accumulate every past upload for the session —
    // errors (and timeouts) stay put so they're not missed.
    if (status === "done") {
      setTimeout(() => {
        if (!mountedRef.current) return;
        setItems((prev) => prev.filter((it) => it.id !== id));
      }, DONE_AUTOCLEAR_MS);
    }
  }

  // CHANGED — bug audit §3, theory 2: this used to be a `for...of` loop
  // that `await`ed each file in turn, so file #2 didn't even get its
  // pending row pushed until file #1 fully resolved. One slow/stuck
  // file made an entire batch look frozen even though only one of them
  // actually was. Each file now runs independently (own pushItem, own
  // try/catch, own settle) and they all kick off together — a hang in
  // one no longer blocks the others from starting or finishing.
  async function handleFiles(fileList) {
    await Promise.allSettled(
      Array.from(fileList).map(async (file) => {
        const id = pushItem(file.name);
        try {
          const result = await ingestFileByExtension(
            file, { ingestFile, ingestPdfFile, ingestVoiceFile }, workspaceId
          );
          settleItem(id, "done", `${result.node_ids?.length || 0} node(s)`);
          onIngested?.(result);
        } catch (err) {
          if (err?.isTimeout) {
            // Per the audit guide: a client-side timeout doesn't mean
            // ingestion failed -- the server may well have finished
            // writing the node right after we gave up waiting on it.
            // Refresh the sources list so a successful-but-slow
            // ingestion still shows up even though this row can't
            // truthfully claim "done." No auto-clear on this one — the
            // person should notice and check for themselves.
            settleItem(id, "timeout", "Still working — check Sources in a bit");
            onIngested?.();
          } else {
            settleItem(id, "error", String(err.message || err));
          }
        }
      })
    );
  }

  async function handleUrlSubmit(e) {
    e.preventDefault();
    const url = urlDraft.trim();
    if (!url) return;
    setUrlDraft("");
    const id = pushItem(url);
    try {
      const result = YOUTUBE_RE.test(url)
        ? await withTimeout((signal) => ingestVideoUrl(workspaceId, url, signal))
        : await withTimeout((signal) => ingestClip(workspaceId, url, signal));
      settleItem(id, "done", `${result.node_ids?.length || 0} node(s)`);
      onIngested?.(result);
    } catch (err) {
      if (err?.isTimeout) {
        settleItem(id, "timeout", "Still working — check Sources in a bit");
        onIngested?.();
      } else {
        settleItem(id, "error", String(err.message || err));
      }
    }
  }

  return (
    <div className="space-y-3">
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (e.dataTransfer.files?.length) handleFiles(e.dataTransfer.files);
        }}
        onClick={() => inputRef.current?.click()}
        className={`flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-6 py-8 text-center cursor-pointer transition-colors ${
          dragOver ? "border-[var(--cyber-cyan)] bg-[var(--cyber-cyan)]/5" : "border-[var(--neutral-800)] hover:border-[var(--neutral-700)]"
        }`}
      >
        <UploadCloud size={22} className="text-[var(--neutral-500)]" />
        <div className="text-xs text-[var(--neutral-300)]">
          Drop PDFs, docs, slides, sheets, or audio here — or click to browse
        </div>
        <div className="text-[10px] text-[var(--neutral-600)]">
          {[PDF_EXT, ...OFFICE_EXTS].join(", ")} · {AUDIO_EXTS.join(", ")}
        </div>
        <input
          ref={inputRef}
          type="file"
          multiple
          aria-label="Upload files"
          className="hidden"
          onChange={(e) => e.target.files?.length && handleFiles(e.target.files)}
        />
      </div>

      <form onSubmit={handleUrlSubmit} className="flex items-center gap-2">
        <Link2 size={14} className="text-[var(--neutral-500)] shrink-0" />
        <input
          value={urlDraft}
          onChange={(e) => setUrlDraft(e.target.value)}
          aria-label="Paste a web page or YouTube URL"
          placeholder="Paste a web page or YouTube URL…"
          className="flex-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
        />
        <button type="submit" className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium shrink-0">
          Add
        </button>
      </form>
      <p className="flex items-center gap-1.5 text-[10px] text-[var(--neutral-600)]">
        <Mic size={11} /> Voice notes and meeting recordings ingest the same way — drop the audio file above.
      </p>

      {items.length > 0 && (
        <div className="rounded-lg border border-[var(--neutral-800)] bg-black/20 overflow-hidden max-h-48 overflow-y-auto">
          {items.map((it) => <ProgressRow key={it.id} item={it} />)}
        </div>
      )}
    </div>
  );
}
