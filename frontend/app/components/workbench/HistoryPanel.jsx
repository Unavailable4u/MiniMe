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
//
// W8.1: the one-click "Restore to before AI edit" bar at the top, and
// badges that say what REPLACED each row. A row's `source` is what
// overwrote that content, not who wrote it (aiUndo.js's header has the
// full story) — so a row badged "Before AI edit" is the file as it
// stood before an AI edit landed on it, and that bar is just a
// shortcut to restoring exactly that row. Unlike picking a row by hand
// it skips the confirmation when nothing would be lost beyond what
// the server keeps anyway (see undoNeedsConfirm()). The list also
// reloads when the open file's version changes, so an AI edit that
// lands while this panel is open shows up — and becomes undoable —
// without closing and reopening it.
import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2, RotateCcw } from "lucide-react";
import { basename } from "../../lib/workbench/fileTree";
import {
  describeLaterChanges,
  findUndoTarget,
  undoLabel,
  undoNeedsConfirm,
  versionBadge,
} from "../../lib/workbench/aiUndo";
import HistoryCompareView from "./HistoryCompareView";
import ConfirmDialog from "../ConfirmDialog";

function SourceBadge({ source }) {
  const badge = versionBadge(source);
  return (
    <span
      title={badge.title}
      className={`shrink-0 rounded border px-1 text-[10px] leading-4 ${
        badge.ai
          ? "border-[var(--accent)] text-[var(--accent)]"
          : "border-[var(--neutral-800)] text-[var(--neutral-500)]"
      }`}
    >
      {badge.label}
    </span>
  );
}

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
  const [pendingUndo, setPendingUndo] = useState(null); // a findUndoTarget() result awaiting confirmation
  const [notice, setNotice] = useState(null); // one-line "that worked" message after an undo
  const seqRef = useRef(0);

  // Switching file (or provider) starts from a clean slate. Bumping
  // seqRef also orphans any request still in flight for the previous
  // file, including when the new one has no history to load at all.
  useEffect(() => {
    seqRef.current++;
    setVersions(null);
    setError(null);
    setCompareVersion(null);
    setRestoreError(null);
    setPendingUndo(null);
    setNotice(null);
    setLoading(false);
  }, [path, provider]);

  // (Re)load the list when the file or its version changes. The version
  // is a dependency so the list follows the file: a Restore (see
  // doRestore) or an AI edit being applied bumps it, and the new row
  // appears without this panel having to be reopened. This does NOT
  // clear `versions` first — the old list stays on screen while the
  // new one loads, instead of flashing to empty on every save.
  useEffect(() => {
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
        if (seq === seqRef.current) {
          setVersions(list);
          setError(null);
        }
      })
      .catch((err) => {
        if (seq === seqRef.current) setError(err.message || "Couldn't load history.");
      })
      .finally(() => {
        if (seq === seqRef.current) setLoading(false);
      });
  }, [path, provider, currentVersion]);

  // The newest AI edit still worth undoing, or null. currentContent is
  // the SAVED text — see findUndoTarget().
  const undoTarget = useMemo(
    () => findUndoTarget(versions, { currentVersion, currentContent }),
    [versions, currentVersion, currentContent]
  );

  // The notice is a "that worked" acknowledgement, not a status — it
  // would only go stale (the next save makes "Restored v3" a lie about
  // what's open), so it clears itself.
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 8000);
    return () => clearTimeout(t);
  }, [notice]);

  async function doRestore(entry, { undoAi = false } = {}) {
    if (!path || !provider) return;
    setRestoring(true);
    setRestoreError(null);
    setNotice(null);
    try {
      const file = await provider.restore(path, entry.version);
      // onRestored lands the new version in the editor store, which
      // changes `currentVersion` and so reloads the list above — the
      // restore itself is a new row (see restore_version()'s own
      // docstring: it's an append, not a rewrite).
      onRestored(path, file);
      setPendingRestore(null);
      setPendingUndo(null);
      setCompareVersion(null);
      if (undoAi) {
        setNotice(
          `Restored v${entry.version} — the file as it was before the ${
            entry.source === "pipeline" ? "AI run" : "AI edit"
          }. The AI's version is still in History.`
        );
      }
    } catch (err) {
      setRestoreError(err.message || "Restore failed.");
    } finally {
      setRestoring(false);
    }
  }

  // The one-click path. Goes straight through unless it would reach
  // further than the click suggests (unsaved edits, or later saves) —
  // then it asks, in words that name what else is affected.
  function handleUndoClick() {
    if (!undoTarget || restoring) return;
    if (undoNeedsConfirm({ dirty, laterChanges: undoTarget.laterChanges })) {
      setPendingUndo(undoTarget);
    } else {
      doRestore(undoTarget.entry, { undoAi: true });
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
      {notice && (
        <p role="status" className="px-1 pb-2 text-[11px] text-emerald-400">
          {notice}
        </p>
      )}

      {undoTarget && (
        <div className="shrink-0 flex items-center gap-2 px-1 pb-2">
          <button
            type="button"
            onClick={handleUndoClick}
            disabled={restoring}
            title={`Bring back v${undoTarget.entry.version}, the file as it was before the ${
              undoTarget.source === "pipeline" ? "AI run" : "AI edit"
            }. Nothing is deleted — the current version stays in History.`}
            className="touch-target inline-flex shrink-0 items-center gap-1.5 rounded border border-[var(--accent)] px-2 py-1 text-[11px] font-medium text-[var(--accent)] hover:bg-[var(--neutral-900)] disabled:opacity-50"
          >
            <RotateCcw size={11} />
            {undoLabel(undoTarget.source)}
          </button>
          <span className="min-w-0 truncate text-[11px] text-[var(--neutral-600)]">
            v{undoTarget.entry.version}
            {undoTarget.laterChanges > 0 ? ` · ${describeLaterChanges(undoTarget.laterChanges)} since` : ""}
          </span>
        </div>
      )}

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
            <SourceBadge source={entry.source} />
            <button
              type="button"
              onClick={() => setCompareVersion(entry)}
              title="Compare with the current version"
              className="min-w-0 flex-1 truncate text-left text-[var(--neutral-300)] hover:text-[var(--neutral-100)]"
            >
              {formatWhen(entry.updated_at)}
              {entry.updated_by ? (
                <span className="text-[var(--neutral-600)]">{` · ${entry.updated_by}`}</span>
              ) : null}
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

      <ConfirmDialog
        open={!!pendingUndo}
        title={pendingUndo ? `${undoLabel(pendingUndo.source)}?` : ""}
        tone="info"
        message={
          pendingUndo
            ? `Bring back v${pendingUndo.entry.version} of ${basename(path)}, the file as it was before the ${
                pendingUndo.source === "pipeline" ? "AI run" : "AI edit"
              }, as a new version.${
                describeLaterChanges(pendingUndo.laterChanges)
                  ? ` That also rolls back ${describeLaterChanges(pendingUndo.laterChanges)} made after it.`
                  : pendingUndo.laterChanges === null
                    ? " That may also roll back saves made after it."
                    : ""
              }${
                dirty ? " This also discards your unsaved edits in the editor." : ""
              } Nothing is deleted — the current version stays in History too.`
            : ""
        }
        confirmLabel={restoring ? "Restoring…" : "Restore"}
        onConfirm={() => pendingUndo && doRestore(pendingUndo.entry, { undoAi: true })}
        onCancel={() => setPendingUndo(null)}
      />
    </div>
  );
}
