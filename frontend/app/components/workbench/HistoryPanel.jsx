"use client";
// frontend/app/components/workbench/HistoryPanel.jsx — W2.6 (Build
// Workbench plan). The bottom panel's History tab: past versions of the
// ACTIVE file (workspace_code_file_versions, reached through
// fileProviders.js's history()/restore() — both straight wrappers
// around the W1.1 routes already shipped; see that file's own header).
//
// Only ever shows the active tab's history — a "no file open" empty
// state otherwise, same convention StatusBar already uses for its own
// per-buffer fields going blank when nothing is active. The live,
// current version is pinned at the top so there's always something to
// read "compared against", even though it isn't itself clickable —
// there's nothing to compare it to or restore it FROM.
//
// Restore always asks first (ConfirmDialog, same component the
// explorer's own Delete uses): unlike a save conflict's "Keep mine",
// which is choosing between two things already open in front of you,
// picking an old version here replaces the buffer's content outright,
// including any unsaved edits sitting in it right now.
import { useEffect, useRef, useState } from "react";
import { Loader2, RotateCcw } from "lucide-react";
import { basename } from "../../lib/workbench/fileTree";
import HistoryCompareView from "./HistoryCompareView";
import ConfirmDialog from "../ConfirmDialog";

const SOURCE_LABEL = {
  user: "You",
  pipeline: "AI pipeline",
  proposal: "AI edit",
  restore: "Restored",
};

function formatWhen(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

/**
 * @param {object} props
 * @param {string|null} props.path - the active tab's path, or null
 * @param {object|null} props.provider - needs .history(path)/.restore(path, version)
 * @param {string} props.currentContent - the buffer's SAVED content (not `edited` — see header)
 * @param {number} [props.currentVersion]
 * @param {boolean} [props.dirty] - the open buffer has unsaved edits, named in the restore confirmation
 * @param {(path: string, file: object) => void} props.onRestored - called with the provider's restore() response (the new current file) once a restore lands
 */
export default function HistoryPanel({ path, provider, currentContent, currentVersion, dirty, onRestored }) {
  const [versions, setVersions] = useState(null); // null = not loaded yet for this path
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [compareVersion, setCompareVersion] = useState(null); // one entry from `versions`, or null
  const [pendingRestore, setPendingRestore] = useState(null); // an entry awaiting confirmation
  const [restoring, setRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState(null);
  const seqRef = useRef(0);

  useEffect(() => {
    setVersions(null);
    setError(null);
    setCompareVersion(null);
    setRestoreError(null);
    // W3.1: Local has no version table (capabilities.history: false) —
    // don't call provider.history() at all rather than letting it throw
    // (LocalFileProvider doesn't even implement it) and surface that as
    // a generic error banner.
    if (!path || !provider || !provider.capabilities?.history) return;
    const seq = ++seqRef.current;
    setLoading(true);
    provider
      .history(path)
      .then((list) => {
        if (seq === seqRef.current) setVersions(list);
      })
      .catch((err) => {
        if (seq === seqRef.current) setError(err.message || "Couldn't load history.");
      })
      .finally(() => {
        if (seq === seqRef.current) setLoading(false);
      });
  }, [path, provider]);

  async function doRestore(entry) {
    if (!path || !provider) return;
    setRestoring(true);
    setRestoreError(null);
    try {
      const file = await provider.restore(path, entry.version);
      onRestored(path, file);
      setPendingRestore(null);
      setCompareVersion(null);
      // The restore itself becomes a new version (see restore_version()'s
      // own docstring: it's an append, not a rewrite) — refresh the list
      // so it shows up rather than looking like nothing happened.
      const seq = ++seqRef.current;
      const list = await provider.history(path);
      if (seq === seqRef.current) setVersions(list);
    } catch (err) {
      setRestoreError(err.message || "Restore failed.");
    } finally {
      setRestoring(false);
    }
  }

  if (!path) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-1 text-center">
        <p className="text-xs text-[var(--neutral-400)]">No file open</p>
        <p className="max-w-sm text-[11px] leading-relaxed text-[var(--neutral-600)]">
          Open a file to see its saved history here.
        </p>
      </div>
    );
  }

  if (!provider?.capabilities?.history) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-1 text-center">
        <p className="text-xs text-[var(--neutral-400)]">No history for local files</p>
        <p className="max-w-sm text-[11px] leading-relaxed text-[var(--neutral-600)]">
          Version history is only kept for project files stored in the cloud. Local files are whatever is on disk
          right now.
        </p>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 flex items-center justify-between px-1 pb-2 text-[11px]">
        <span className="truncate text-[var(--neutral-300)]">{basename(path)}</span>
        {loading && <Loader2 size={11} className="animate-spin text-[var(--neutral-600)]" />}
      </div>

      {error && <p className="px-1 pb-2 text-[11px] text-red-400">{error}</p>}
      {restoreError && <p className="px-1 pb-2 text-[11px] text-red-400">{restoreError}</p>}

      <div className="flex-1 min-h-0 overflow-auto">
        <div className="flex items-center gap-2 px-2 py-1 text-[11px] text-[var(--neutral-400)]">
          <span className="w-8 shrink-0 text-right text-[var(--neutral-600)]">
            {currentVersion != null ? `v${currentVersion}` : ""}
          </span>
          <span className="flex-1 truncate">Current</span>
        </div>

        {versions && versions.length === 0 && !loading && (
          <p className="px-2 py-2 text-[11px] text-[var(--neutral-600)]">
            No earlier versions yet — this file has only been saved once.
          </p>
        )}

        {(versions || []).map((entry) => (
          <div
            key={entry.version}
            className="group flex items-center gap-2 rounded px-2 py-1 text-[11px] hover:bg-[var(--neutral-900)]"
          >
            <span className="w-8 shrink-0 text-right text-[var(--neutral-600)]">v{entry.version}</span>
            <button
              type="button"
              onClick={() => setCompareVersion(entry)}
              title="Compare with the current version"
              className="min-w-0 flex-1 truncate text-left text-[var(--neutral-300)] hover:text-[var(--neutral-100)]"
            >
              {formatWhen(entry.updated_at)}
              <span className="text-[var(--neutral-600)]">
                {" · "}
                {SOURCE_LABEL[entry.source] || entry.source || "unknown"}
                {entry.updated_by ? ` · ${entry.updated_by}` : ""}
              </span>
            </button>
            <button
              type="button"
              onClick={() => setPendingRestore(entry)}
              title="Restore this version"
              className="touch-target shrink-0 text-[var(--neutral-500)] opacity-0 hover:text-[var(--neutral-100)] focus:opacity-100 group-hover:opacity-100"
            >
              <RotateCcw size={12} />
            </button>
          </div>
        ))}
      </div>

      <HistoryCompareView
        open={!!compareVersion}
        path={path}
        older={compareVersion?.content}
        olderVersion={compareVersion?.version}
        olderUpdatedBy={compareVersion?.updated_by}
        current={currentContent}
        currentVersion={currentVersion}
        onClose={() => setCompareVersion(null)}
        onRestore={() => compareVersion && setPendingRestore(compareVersion)}
      />

      <ConfirmDialog
        open={!!pendingRestore}
        title="Restore this version?"
        tone="info"
        message={
          pendingRestore
            ? `Bring back v${pendingRestore.version} of ${basename(path)} (${formatWhen(
                pendingRestore.updated_at
              )}) as a new version. Nothing is deleted — the current version stays in history too.${
                dirty ? " This also discards your unsaved edits in the editor." : ""
              }`
            : ""
        }
        confirmLabel={restoring ? "Restoring…" : "Restore"}
        onConfirm={() => pendingRestore && doRestore(pendingRestore)}
        onCancel={() => setPendingRestore(null)}
      />
    </div>
  );
}
