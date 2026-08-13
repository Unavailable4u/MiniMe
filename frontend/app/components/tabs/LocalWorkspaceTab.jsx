"use client";
import { useState, useEffect, useCallback, useRef, memo } from "react";
import { authHeaders } from "../../context/SessionContext";
import { useWorkspaces } from "../../context/WorkspacesContext";   // same list-of-workspaces store every other project picker in this app reads from
import {
  Folder, FolderOpen, File, Loader2, RefreshCw, WifiOff, ChevronRight,
  HardDrive, AlertTriangle,
} from "lucide-react";

/**
 * F2 Part 6 — new tab shell + read-only file tree.
 *
 * Wired into AppShell.jsx's existing TABS/WORKSPACE_TAB_IDS pattern the
 * same way every other workspace-scoped tab is (see that file's own
 * comment on the standard prop set every tab receives). Deliberately
 * does NOT reuse BuildTab.jsx/TestTab.jsx's stage-filtered project-card
 * picker -- this tab has no pipeline stage of its own (it's not part of
 * the note->research->plan->build->test->growth flow those two filter
 * by), so a plain dropdown over every workspace the person has, same
 * shape AuditLogTab.jsx already uses for its own "which workspace" picker
 * one level up in Settings, is the right amount of picker for what this
 * tab actually needs.
 *
 * Talks only to Part 3's already-shipped, always-runs-free read routes
 * (POST .../local/list_dir, POST .../local/read_file) plus Part 2's
 * GET .../local/status poll. No propose/confirm, no write/delete/
 * execute_command, no terminal -- those are Part 4 (already live on the
 * backend) and Part 7 (not built here). This tab's whole job is: prove
 * the pipe works end-to-end by rendering a real directory tree pulled
 * from a real paired folder, and let someone click into a file to read
 * it -- also read-only, also already-shipped Part 3 surface, so there's
 * no new backend capability being exposed here that isn't already live
 * and safe.
 *
 * Place this file at: frontend/app/components/tabs/LocalWorkspaceTab.jsx
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// How often to poll .../local/status while this tab is open. Cheap on
// purpose (api/routes/local_workspace.py's local_status() docstring:
// "a registry lookup, not a daemon round-trip"), so a short interval
// here doesn't mean driving load through the actual websocket the way
// polling list_dir/read_file on the same interval would.
const STATUS_POLL_MS = 5000;

function formatBytes(size) {
  if (typeof size !== "number") return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

async function apiPost(path, body) {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    // 409 here means "well-formed request, no live daemon (or the
    // daemon rejected it)" -- api/routes/local_workspace.py's own
    // convention (see local_list_dir/local_read_file docstrings).
    // Surfacing `detail` either way means this tab shows the daemon's
    // own error text (e.g. "not a UTF-8 text file") instead of a bare
    // status code.
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

async function fetchStatus(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/local/status`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return { live: false };
  return res.json();
}

function fetchListDir(wsId, path) {
  return apiPost(`/api/workspaces/${wsId}/local/list_dir`, { path });
}

function fetchReadFile(wsId, path) {
  return apiPost(`/api/workspaces/${wsId}/local/read_file`, { path });
}

// One row of the tree. Lazily fetches its own children on first
// expand (and caches them for the life of this node) rather than the
// tab loading the whole tree upfront -- same "don't fetch data nobody's
// asked to see yet" reasoning AppShell.jsx's own dynamic-tab-loading
// comment gives for deferring whole tab bodies.
function FileTreeNode({ wsId, entry, path, depth, onSelectFile, selectedPath }) {
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const isDir = entry.type === "dir";
  const isSelected = !isDir && selectedPath === path;

  const handleClick = useCallback(async () => {
    if (!isDir) {
      onSelectFile(path);
      return;
    }
    if (expanded) {
      setExpanded(false);
      return;
    }
    setExpanded(true);
    if (children !== null) return; // already loaded once -- don't re-fetch on every collapse/expand
    setLoading(true);
    setError(null);
    try {
      const data = await fetchListDir(wsId, path);
      setChildren(data.entries || []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [isDir, expanded, children, wsId, path, onSelectFile]);

  const childIndent = { paddingLeft: `${(depth + 1) * 14 + 6}px` };

  return (
    <div>
      <button
        type="button"
        onClick={handleClick}
        title={path}
        className={`w-full flex items-center gap-1.5 text-xs px-1.5 py-1 rounded text-left hover:bg-[var(--neutral-900)] ${
          isSelected ? "bg-[var(--neutral-900)] text-[var(--neutral-100)]" : "text-[var(--neutral-400)]"
        }`}
        style={{ paddingLeft: `${depth * 14 + 6}px` }}
      >
        {isDir ? (
          <ChevronRight size={11} className={`shrink-0 transition-transform ${expanded ? "rotate-90" : ""}`} />
        ) : (
          <span className="w-[11px] shrink-0" />
        )}
        {isDir ? (
          expanded
            ? <FolderOpen size={12} className="shrink-0 text-[var(--accent)]" />
            : <Folder size={12} className="shrink-0 text-[var(--neutral-500)]" />
        ) : (
          <File size={12} className="shrink-0 text-[var(--neutral-600)]" />
        )}
        <span className="truncate">{entry.name}</span>
        {!isDir && typeof entry.size === "number" && (
          <span className="ml-auto pl-2 text-[10px] text-[var(--neutral-700)] shrink-0">{formatBytes(entry.size)}</span>
        )}
      </button>

      {isDir && expanded && (
        <div>
          {loading && (
            <div className="flex items-center gap-1.5 text-[10px] text-[var(--neutral-600)] py-1" style={childIndent}>
              <Loader2 size={10} className="animate-spin" /> Loading…
            </div>
          )}
          {!loading && error && (
            <div className="flex items-center gap-1.5 text-[10px] text-amber-500 py-1" style={childIndent}>
              <AlertTriangle size={10} className="shrink-0" /> {error}
            </div>
          )}
          {!loading && !error && children && children.length === 0 && (
            <div className="text-[10px] text-[var(--neutral-700)] py-1" style={childIndent}>Empty</div>
          )}
          {!loading && !error && children?.map((child) => (
            <FileTreeNode
              key={child.name}
              wsId={wsId}
              entry={child}
              path={path === "." ? child.name : `${path}/${child.name}`}
              depth={depth + 1}
              onSelectFile={onSelectFile}
              selectedPath={selectedPath}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// Right-hand preview pane for whichever file is currently selected.
// Read-only text dump -- no editing affordance exists yet (that's Part
// 7's job, alongside write_file's already-live propose/confirm surface
// from Part 4), so this deliberately renders as plain, non-editable
// text even though it's sitting right next to a real file on disk.
function FilePreview({ file }) {
  if (!file) {
    return (
      <div className="h-full flex items-center justify-center text-xs text-[var(--neutral-600)]">
        Select a file to preview it
      </div>
    );
  }
  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="shrink-0 px-3 py-2 border-b border-[var(--neutral-800)] text-xs text-[var(--neutral-300)] font-mono truncate" title={file.path}>
        {file.path}
      </div>
      <div className="flex-1 min-h-0 overflow-auto">
        {file.loading && (
          <div className="flex items-center gap-2 text-xs text-[var(--neutral-600)] py-6 justify-center">
            <Loader2 size={14} className="animate-spin" /> Loading…
          </div>
        )}
        {!file.loading && file.error && (
          <div className="flex items-start gap-2 text-xs text-amber-500 p-3">
            <AlertTriangle size={14} className="shrink-0 mt-0.5" />
            <span>{file.error}</span>
          </div>
        )}
        {!file.loading && !file.error && (
          <>
            {file.truncated && (
              <div className="px-3 pt-2 text-[10px] text-amber-500">
                File is larger than the daemon's read limit -- showing the first part only.
              </div>
            )}
            <pre className="text-[11px] text-[var(--neutral-300)] p-3 whitespace-pre-wrap break-all font-mono">
              {file.content}
            </pre>
          </>
        )}
      </div>
    </div>
  );
}

function LocalWorkspaceTab({ initialWorkspaceId, onConsumeInitialWorkspaceId, onActiveWorkspaceChange }) {
  const { workspaces } = useWorkspaces();

  const [selectedId, setSelectedId] = useState(null);
  const [live, setLive] = useState(false);
  const [statusChecked, setStatusChecked] = useState(false); // avoids a "not connected" flash before the first poll resolves
  const [rootEntries, setRootEntries] = useState(null);
  const [rootLoading, setRootLoading] = useState(false);
  const [rootError, setRootError] = useState(null);
  const [selectedFile, setSelectedFile] = useState(null);

  // Consumed once, same "promote/handoff sets it, destination tab reads
  // it once and clears it" contract AppShell.jsx's pendingWorkspaceSelection
  // documents for every other workspace-scoped tab.
  useEffect(() => {
    if (initialWorkspaceId) {
      setSelectedId(initialWorkspaceId);
      onConsumeInitialWorkspaceId?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialWorkspaceId]);

  useEffect(() => {
    if (!selectedId && workspaces.length > 0) setSelectedId(workspaces[0].id);
  }, [workspaces, selectedId]);

  useEffect(() => {
    const selected = workspaces.find((w) => w.id === selectedId);
    onActiveWorkspaceChange?.(selected?.id || null, selected?.name);
  }, [selectedId, workspaces, onActiveWorkspaceChange]);

  const loadRoot = useCallback(async (wsId) => {
    setRootLoading(true);
    setRootError(null);
    try {
      const data = await fetchListDir(wsId, ".");
      setRootEntries(data.entries || []);
    } catch (e) {
      setRootEntries(null);
      setRootError(e.message);
    } finally {
      setRootLoading(false);
    }
  }, []);

  // Reset everything when the selected workspace changes -- a file
  // tree/preview from a different workspace's paired folder has no
  // business staying on screen while the daemon-status poll below
  // catches up to the new selection.
  useEffect(() => {
    setRootEntries(null);
    setRootError(null);
    setSelectedFile(null);
    setLive(false);
    setStatusChecked(false);
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId) return undefined;
    let cancelled = false;

    const check = async () => {
      const status = await fetchStatus(selectedId);
      if (cancelled) return;
      setStatusChecked(true);
      setLive((prevLive) => {
        // Refetch the tree the moment a daemon goes from not-live to
        // live, so pairing the folder locally is enough to populate
        // this tab without a manual refresh click.
        if (!prevLive && status.live) loadRoot(selectedId);
        return status.live;
      });
    };

    check();
    const interval = setInterval(check, STATUS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [selectedId, loadRoot]);

  const onSelectFile = useCallback(async (path) => {
    setSelectedFile({ path, content: "", loading: true, error: null, truncated: false });
    try {
      const data = await fetchReadFile(selectedId, path);
      setSelectedFile({ path, content: data.content, loading: false, error: null, truncated: Boolean(data.truncated) });
    } catch (e) {
      setSelectedFile({ path, content: "", loading: false, error: e.message, truncated: false });
    }
  }, [selectedId]);

  const handleRefresh = useCallback(() => {
    if (selectedId && live) loadRoot(selectedId);
  }, [selectedId, live, loadRoot]);

  return (
    <div className="h-full flex flex-col min-h-0 text-sm">
      <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b border-[var(--neutral-800)]">
        <HardDrive size={13} className="text-[var(--neutral-500)] shrink-0" />
        {workspaces.length > 0 ? (
          <select
            id="local-workspace-picker"
            name="localWorkspacePicker"
            value={selectedId || ""}
            onChange={(e) => setSelectedId(e.target.value)}
            className="text-xs bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1 text-[var(--neutral-300)]"
          >
            {workspaces.map((ws) => (
              <option key={ws.id} value={ws.id}>{ws.name}</option>
            ))}
          </select>
        ) : (
          <span className="text-xs text-[var(--neutral-600)]">No workspaces yet</span>
        )}

        <span className={`flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border ${
          live
            ? "border-emerald-900/50 text-emerald-500"
            : "border-[var(--neutral-800)] text-[var(--neutral-600)]"
        }`}>
          <span className={`w-1.5 h-1.5 rounded-full ${live ? "bg-emerald-500" : "bg-[var(--neutral-700)]"}`} />
          {live ? "Daemon connected" : "No daemon connected"}
        </span>

        <button
          type="button"
          onClick={handleRefresh}
          disabled={!live || rootLoading}
          title="Refresh"
          className="ml-auto text-[var(--neutral-500)] hover:text-[var(--neutral-300)] disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <RefreshCw size={13} className={rootLoading ? "animate-spin" : ""} />
        </button>
      </div>

      <div className="flex-1 min-h-0 flex">
        <div className="w-64 shrink-0 border-r border-[var(--neutral-800)] overflow-auto py-1.5">
          {!selectedId && (
            <p className="text-xs text-[var(--neutral-600)] px-3 py-2">Pick a workspace to get started.</p>
          )}

          {selectedId && statusChecked && !live && (
            <div className="flex flex-col items-center gap-2 text-center text-xs text-[var(--neutral-600)] px-3 py-8">
              <WifiOff size={18} className="text-[var(--neutral-700)]" />
              <span>
                No local daemon is paired with this workspace right now.
                Run the daemon locally (see <code className="text-[10px]">daemon/README.md</code>)
                and this tree fills in automatically once it connects.
              </span>
            </div>
          )}

          {selectedId && live && rootLoading && rootEntries === null && (
            <div className="flex items-center gap-2 text-xs text-[var(--neutral-600)] py-6 justify-center">
              <Loader2 size={14} className="animate-spin" /> Loading…
            </div>
          )}

          {selectedId && live && rootError && (
            <div className="flex items-start gap-2 text-xs text-amber-500 px-3 py-2">
              <AlertTriangle size={14} className="shrink-0 mt-0.5" />
              <span>{rootError}</span>
            </div>
          )}

          {selectedId && live && !rootError && rootEntries && rootEntries.length === 0 && (
            <p className="text-xs text-[var(--neutral-600)] px-3 py-2">This folder is empty.</p>
          )}

          {selectedId && live && !rootError && rootEntries && rootEntries.map((entry) => (
            <FileTreeNode
              key={entry.name}
              wsId={selectedId}
              entry={entry}
              path={entry.name}
              depth={0}
              onSelectFile={onSelectFile}
              selectedPath={selectedFile?.path}
            />
          ))}
        </div>

        <div className="flex-1 min-w-0">
          <FilePreview file={selectedFile} />
        </div>
      </div>
    </div>
  );
}

// Same memo() rationale every other tab body in this file/directory
// uses (see AuditLogTab.jsx/SettingsTab.jsx's own comments): AppShell's
// tab-switch re-render shouldn't force this tab to re-render/re-poll
// when it isn't the active one.
export default memo(LocalWorkspaceTab);
