// frontend/app/lib/ingestDispatch.js — Data Layer §4b.
//
// The "which ingestor does this file belong to" rules used to live only
// inside components/notebooks/IngestionDropzone.jsx. Pulled out here,
// unchanged, so the chat composer's new attach button (WorkspaceChatPanel.jsx,
// embedded in all 7 chat-tab surfaces) can point at the exact same
// process_upload()-backed endpoints (via useSession()'s ingestFile/
// ingestPdfFile/ingestVoiceFile) without re-deriving the extension list a
// second time. IngestionDropzone.jsx now imports from here too — same
// behavior, one source of truth.

export const OFFICE_EXTS = ["docx", "pptx", "xlsx", "xls", "csv", "md", "json"];
export const PDF_EXT = "pdf";
export const AUDIO_EXTS = ["mp3", "wav", "m4a", "ogg", "webm", "flac"];
export const YOUTUBE_RE = /(youtube\.com\/watch|youtu\.be\/)/i;

// NEW — bug audit §3 ("stuck Ingesting…"), theory 1: a plain `await
// fetch(...)` with no timeout of its own means a slow server-side
// pipeline (OCR/parse -> chunk -> embed -> summarize) and a genuinely
// dropped connection look identical to a caller. Bounding the wait
// client-side turns "spins forever, no way to know if it's still
// working or dead" into "surfaces something actionable after a while."
// Generous on purpose: PDF OCR and local Whisper transcription on a big
// file can legitimately take a couple of minutes.
export const INGEST_TIMEOUT_MS = 120_000;

export function extOf(filename) {
  return (filename.split(".").pop() || "").toLowerCase();
}

// Runs `fn(signal)` with an AbortSignal that fires after
// INGEST_TIMEOUT_MS. Throws a distinguishable TimeoutError instead of
// whatever generic abort error the fetch layer would otherwise throw, so
// callers can tell "we gave up waiting" apart from "the server said no."
export async function withTimeout(fn) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), INGEST_TIMEOUT_MS);
  try {
    return await fn(controller.signal);
  } catch (err) {
    if (controller.signal.aborted) {
      const timeoutErr = new Error("Taking longer than expected");
      timeoutErr.isTimeout = true;
      throw timeoutErr;
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// Routes one File to whichever ingest* function (as returned by
// useSession()) handles its extension, or throws a plain Error for an
// unsupported type. Same three-way split IngestionDropzone.jsx's
// handleFiles() always used, now shared.
export async function ingestFileByExtension(file, { ingestFile, ingestPdfFile, ingestVoiceFile }, workspaceId) {
  const ext = extOf(file.name);
  if (AUDIO_EXTS.includes(ext)) {
    return withTimeout((signal) => ingestVoiceFile(workspaceId, file, signal));
  }
  if (ext === PDF_EXT) {
    return withTimeout((signal) => ingestPdfFile(workspaceId, file, signal));
  }
  if (OFFICE_EXTS.includes(ext)) {
    return withTimeout((signal) => ingestFile(workspaceId, file, signal));
  }
  throw new Error(`Unsupported file type .${ext}`);
}
