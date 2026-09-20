"use client";
// frontend/app/components/workbench/EditorWorkbench.jsx — W2.3a (Build
// Workbench plan). The Build tab's Editor sub-tab: explorer | tabs +
// CodeMirror editor | status bar. It replaces BuildTab.jsx's old
// CodeView, which is deleted in the same patch — this component takes
// over everything CodeViewBody did (list/open/save, the W0.1 "changed on
// the server" policy, ZIP download) and adds what a workbench needs on
// top: several open files, per-file dirty state, resizable panes.
//
// Mount it through `next/dynamic` with `{ ssr: false }` (BuildTab.jsx
// does) — CodeEditor measures real DOM nodes and has no server
// rendering story, see that file's own header.
//
// Shape of the code, top to bottom:
//   - EditorWorkbench: just wraps the store provider (a provider's own
//     value isn't visible to hooks in the component that mounts it —
//     same split CodeView / CodeViewBody had).
//   - WorkbenchBody: the controller. Talks to the FileProvider, owns
//     the little bit of UI state that isn't buffer state (file list,
//     save-in-flight, notices), and passes plain props + stable
//     callbacks to the presentational panes.
//   - EditorPane: one mounted CodeEditor per open file (below).
//
// Not in this patch (W2.3b): the bottom panel (Problems / Console /
// Terminal / History) and the preview column. The layout below is
// arranged so both slot in without moving anything: a status bar
// under a "main row" that they extend.
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import { authHeaders } from "../../context/SessionContext";
import { useSplitter } from "../../hooks/useSplitter";
import { useViewport } from "../../hooks/useViewport";
import { createCloudFileProvider } from "../../lib/workbench/fileProviders";
import { EditorStoreProvider, useEditorStore } from "../../lib/workbench/editorStore";
import { basename } from "../../lib/workbench/fileTree";
import { encodeTabFlags, planBufferSync } from "../../lib/workbench/tabUtils";
import ConfirmDialog from "../ConfirmDialog";
import CodeEditor from "./CodeEditor";
import EditorTabs from "./EditorTabs";
import Explorer from "./Explorer";
import StatusBar from "./StatusBar";

// Explorer width. Persisted per workspace (the key carries the id) so
// two projects don't fight over one width. Default/min/max in px; the
// max is also capped at 40% of the window so a wide saved value can't
// swallow the editor on a smaller screen.
const EXPLORER_DEFAULT_WIDTH = 240;
const EXPLORER_MIN_WIDTH = 160;
const EXPLORER_MAX_WIDTH = 480;

/**
 * One CodeMirror editor for one open file. Every open tab keeps its own
 * mounted editor and just hides the inactive ones (`hidden` =
 * display:none) instead of swapping a single editor between files. That
 * is deliberate: a CodeMirror EditorView is one document's undo
 * history, cursor, selection and scroll position (see CodeEditor.jsx's
 * header on why a different file needs a different instance), and
 * people expect all of that to still be there when they click back to a
 * tab. Remounting on every tab switch would throw it away each time.
 *
 * A display:none editor can't measure itself, so when this pane becomes
 * visible again it asks CM to re-measure — otherwise the gutter/scroll
 * geometry can be stale until the next interaction.
 *
 * memo()'d: every keystroke replaces the store's `buffers` object, but
 * only the edited file's `value` prop changes, so with stable callbacks
 * from the parent the other open editors skip re-rendering.
 */
const EditorPane = memo(function EditorPane({ path, active, visible, value, onEdit, onSave, onCursor }) {
  const editorRef = useRef(null);
  const shown = active && visible;

  useEffect(() => {
    if (shown) editorRef.current?.getView()?.requestMeasure();
  }, [shown]);

  return (
    <div className={active ? "absolute inset-0" : "hidden"}>
      <CodeEditor
        ref={editorRef}
        filePath={path}
        value={value}
        onChange={(text) => onEdit(path, text)}
        onSave={() => onSave(path)}
        onCursorChange={(pos) => onCursor(path, pos)}
      />
    </div>
  );
});

function WorkbenchBody({ workspaceId, apiUrl }) {
  // One provider per (workspace, api) pair — memoized so re-renders
  // don't create a new one (which would re-subscribe to Pusher).
  const provider = useMemo(
    () => createCloudFileProvider({ workspaceId, apiUrl }),
    [workspaceId, apiUrl]
  );
  const {
    state,
    setActivePath,
    activateTab,
    fileLoaded,
    editBuffer,
    saveSuccess,
    markStale,
    clearStale,
    closeTabs,
  } = useEditorStore();

  const [viewport] = useViewport();
  const isMobile = viewport === "mobile";

  const [filesMeta, setFilesMeta] = useState(null); // {file_path: meta} from provider.list(); null until the first load lands
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState(null);
  const [notice, setNotice] = useState(null); // a dismissible error about opening/refreshing a file
  const [saving, setSaving] = useState({}); // {path: true} while a save is in flight
  const [saveErrors, setSaveErrors] = useState({}); // {path: message}
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState(null);
  const [pendingClose, setPendingClose] = useState(null); // {paths, dirty} awaiting the discard confirmation
  const [cursor, setCursor] = useState(null); // {line, col} of the active file's caret
  const [mobileExplorerOpen, setMobileExplorerOpen] = useState(true); // single-pane layout only

  const explorerSplitter = useSplitter({
    axis: "width",
    defaultSize: EXPLORER_DEFAULT_WIDTH,
    min: EXPLORER_MIN_WIDTH,
    max: () =>
      typeof window !== "undefined"
        ? Math.min(EXPLORER_MAX_WIDTH, window.innerWidth * 0.4)
        : EXPLORER_MAX_WIDTH,
    storageKey: `minime_build_editor_explorer_w:${workspaceId}`,
  });

  // "Latest value" refs. Callbacks below are wrapped in useCallback with
  // short dependency lists so the memoized panes don't re-render on every
  // keystroke; they read anything that changes per keystroke (the store
  // state) through these refs instead of closing over it.
  const stateRef = useRef(state);
  stateRef.current = state;
  const filesMetaRef = useRef(null);
  const listSeqRef = useRef(0); // guards against an older list response landing after a newer one
  const openingRef = useRef(new Set()); // paths whose first read is in flight
  const cancelledOpensRef = useRef(new Set()); // in-flight opens whose tab was closed before the read landed
  const savingRef = useRef(new Set()); // same as `saving`, but readable from planBufferSync callers
  const dismissedRef = useRef({}); // {path: server version the person waved off with "Keep mine"}
  const cursorsRef = useRef({}); // {path: {line, col}} — every editor reports; only the active one is shown
  const isMobileRef = useRef(isMobile);
  isMobileRef.current = isMobile;

  // ---- file list ----------------------------------------------------

  const loadFileList = useCallback(async () => {
    const seq = ++listSeqRef.current;
    setListLoading(true);
    setListError(null);
    try {
      const meta = await provider.list();
      if (seq !== listSeqRef.current) return null; // superseded by a newer refresh
      filesMetaRef.current = meta;
      setFilesMeta(meta);
      return meta;
    } catch (err) {
      if (seq === listSeqRef.current) setListError(err.message);
      return null;
    } finally {
      if (seq === listSeqRef.current) setListLoading(false);
    }
  }, [provider]);

  // Drops tabs and everything keyed to their paths.
  const dropTabs = useCallback(
    (paths) => {
      closeTabs(paths);
      for (const p of paths) {
        delete dismissedRef.current[p];
        delete cursorsRef.current[p];
        // A close that lands while that file's first read is still in
        // flight must win: openFile checks this once the read resolves.
        if (openingRef.current.has(p)) cancelledOpensRef.current.add(p);
      }
      setSaveErrors((prev) => {
        const next = { ...prev };
        let changed = false;
        for (const p of paths) {
          if (p in next) {
            delete next[p];
            changed = true;
          }
        }
        return changed ? next : prev;
      });
    },
    [closeTabs]
  );

  // Re-reads a file that's already open. Not-forced (the automatic
  // refresh): if the person started typing while the read was in
  // flight, don't overwrite their keystrokes — flag it stale instead.
  // Forced ("Reload" on the stale banner) is an explicit "discard mine".
  const reloadFile = useCallback(
    async (path, { force = false } = {}) => {
      try {
        const file = await provider.read(path);
        const latest = stateRef.current.buffers[path];
        if (!latest) return; // tab was closed while this was in flight — don't resurrect it
        if (latest.dirty && !force) {
          markStale(path);
          return;
        }
        fileLoaded(path, file);
        delete dismissedRef.current[path];
      } catch (err) {
        setNotice(`Couldn't refresh ${path}: ${err.message}`);
      }
    },
    [provider, fileLoaded, markStale]
  );

  // The W0.1 policy (never silently clobber unsaved edits), now applied
  // to EVERY open tab and driven by file versions rather than by which
  // path a Pusher event named — see planBufferSync().
  const syncOpenBuffers = useCallback(
    (meta) => {
      const s = stateRef.current;
      const plan = planBufferSync({
        tabs: s.tabs,
        buffers: s.buffers,
        meta,
        dismissed: dismissedRef.current,
        busy: savingRef.current,
      });
      if (plan.drop.length) dropTabs(plan.drop);
      plan.stale.forEach((p) => markStale(p));
      plan.reload.forEach((p) => reloadFile(p));
    },
    [dropTabs, markStale, reloadFile]
  );

  const refreshFromServer = useCallback(async () => {
    const meta = await loadFileList();
    if (meta) syncOpenBuffers(meta);
  }, [loadFileList, syncOpenBuffers]);

  const refreshRef = useRef(refreshFromServer);
  refreshRef.current = refreshFromServer;

  useEffect(() => {
    loadFileList();
  }, [loadFileList]);

  // Live refresh (W0.1 / W2.2): the provider owns the Pusher plumbing;
  // this is only "what to do when told something changed". The payload's
  // file paths aren't even needed — a list refetch + version comparison
  // finds what changed.
  useEffect(() => provider.subscribe(() => refreshRef.current()), [provider]);

  // W0.1: Pusher can drop a connection while a tab is backgrounded
  // (mobile Safari suspends sockets aggressively), so a regen that
  // happened while hidden would otherwise never arrive. Cheap fallback:
  // re-check on visibility regain. Version comparison means this only
  // re-reads files that actually moved.
  useEffect(() => {
    function onVisibility() {
      if (document.visibilityState === "visible") refreshRef.current();
    }
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  // ---- open / close --------------------------------------------------

  const openFile = useCallback(
    async (path) => {
      setNotice(null);
      if (isMobileRef.current) setMobileExplorerOpen(false); // single-pane: jump to the editor
      const s = stateRef.current;
      if (s.buffers[path] || openingRef.current.has(path)) {
        activateTab(path);
        return;
      }
      openingRef.current.add(path);
      cancelledOpensRef.current.delete(path);
      setActivePath(path); // adds the tab and selects it right away; the editor appears once the read lands
      try {
        const file = await provider.read(path);
        // Closed while loading. Tracked with a ref rather than by asking
        // `stateRef.current.tabs`, which only reflects the last RENDER:
        // a read that resolves before React has re-rendered the tab in
        // (a cached or very fast response) would look "closed" and the
        // file would never open.
        if (cancelledOpensRef.current.delete(path)) return;
        fileLoaded(path, file);
      } catch (err) {
        dropTabs([path]);
        setNotice(`Couldn't open ${path}: ${err.message}`);
      } finally {
        openingRef.current.delete(path);
        cancelledOpensRef.current.delete(path);
      }
    },
    [provider, activateTab, setActivePath, fileLoaded, dropTabs]
  );

  // Every close path goes through here so unsaved edits are never
  // discarded silently. (W2.5 adds the same guard for project switches
  // and page unload; this covers what this patch introduces — a close
  // button on a tab.)
  const requestClose = useCallback(
    (paths) => {
      if (paths.length === 0) return;
      const dirty = paths.filter((p) => stateRef.current.buffers[p]?.dirty);
      if (dirty.length === 0) dropTabs(paths);
      else setPendingClose({ paths, dirty });
    },
    [dropTabs]
  );
  const closeOne = useCallback((path) => requestClose([path]), [requestClose]);
  const closeOthers = useCallback(
    (path) => requestClose(stateRef.current.tabs.filter((p) => p !== path)),
    [requestClose]
  );
  const closeAll = useCallback(() => requestClose([...stateRef.current.tabs]), [requestClose]);

  // ---- editing / saving ----------------------------------------------

  const handleEdit = useCallback((path, text) => editBuffer(path, text), [editBuffer]);

  const handleCursor = useCallback((path, pos) => {
    cursorsRef.current[path] = pos;
    if (stateRef.current.activePath === path) setCursor(pos);
  }, []);

  // Switching tabs: show the caret position the newly active editor
  // already has (each editor keeps reporting while hidden).
  useEffect(() => {
    setCursor(state.activePath ? cursorsRef.current[state.activePath] || null : null);
  }, [state.activePath]);

  const saveFile = useCallback(
    async (path) => {
      const buffer = path ? stateRef.current.buffers[path] : null;
      if (!buffer || !buffer.dirty || savingRef.current.has(path)) return;
      const sent = buffer.edited;
      savingRef.current.add(path);
      setSaving((prev) => ({ ...prev, [path]: true }));
      setSaveErrors((prev) => {
        if (!(path in prev)) return prev;
        const { [path]: _cleared, ...rest } = prev;
        return rest;
      });
      try {
        // No baseVersion yet — same blind write the Code view always did;
        // W2.5 is where Save starts sending one and handling the 409.
        const saved = await provider.write(path, sent);
        const latest = stateRef.current.buffers[path];
        // Typing during the round trip is normal (and will be constant
        // once autosave lands): if the buffer no longer matches what was
        // sent, record the save but keep what's in the editor, instead
        // of swapping the server's copy in over the newer keystrokes.
        if (latest) saveSuccess(path, saved, { keepEdited: latest.edited !== sent });
        // Swap just this file's tree entry in place rather than
        // refetching the list. `version` is part of it now: the sync
        // check compares it against the open buffer's.
        const nextMeta = {
          ...(filesMetaRef.current || {}),
          [path]: {
            workspace_id: saved.workspace_id,
            file_path: saved.file_path,
            language: saved.language,
            size: saved.content ? saved.content.length : 0,
            version: saved.version,
            updated_at: saved.updated_at,
            updated_by: saved.updated_by,
          },
        };
        filesMetaRef.current = nextMeta;
        setFilesMeta(nextMeta);
      } catch (err) {
        setSaveErrors((prev) => ({ ...prev, [path]: err.message }));
      } finally {
        savingRef.current.delete(path);
        setSaving((prev) => {
          const { [path]: _done, ...rest } = prev;
          return rest;
        });
      }
    },
    [provider, saveSuccess]
  );

  const saveActive = useCallback(() => saveFile(stateRef.current.activePath), [saveFile]);

  // "Keep mine" on the stale banner: hide it, and remember which server
  // version was waved off so the next refresh doesn't put it straight
  // back (only a NEWER server version will).
  function keepMine(path) {
    dismissedRef.current[path] = filesMetaRef.current?.[path]?.version ?? 0;
    clearStale(path);
  }

  // ---- ZIP (moved here from the old CodeView; shown in the explorer header) ----

  // Not part of the FileProvider contract (a whole-workspace export, not
  // a per-file op — see fileProviders.js), so this still calls the route
  // directly. Needs authHeaders(), so it can't be a plain <a href>:
  // fetch a blob and click a throwaway object URL.
  const downloadZip = useCallback(async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      const res = await fetch(`${apiUrl}/api/workspaces/${workspaceId}/code/zip`, {
        headers: await authHeaders(),
      });
      if (!res.ok) {
        throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${workspaceId}_code.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setDownloadError(err.message);
    } finally {
      setDownloading(false);
    }
  }, [apiUrl, workspaceId]);

  const handleRefresh = useCallback(() => refreshRef.current(), []);

  // ---- derived ---------------------------------------------------------

  const { tabs, buffers, activePath } = state;
  const activeBuffer = activePath ? buffers[activePath] : null;
  const flagsKey = useMemo(() => encodeTabFlags(tabs, buffers), [tabs, buffers]);
  const saveState = !activeBuffer
    ? null
    : saving[activePath]
    ? "saving"
    : saveErrors[activePath]
    ? "error"
    : activeBuffer.dirty
    ? "dirty"
    : "saved";

  // Single-pane layout (phones): explorer and editor take turns filling
  // the width, toggled by the "Files" button in the tab strip. Both stay
  // MOUNTED and are hidden with display:none rather than unmounted, so
  // opening the file list doesn't tear down every editor's undo history.
  // A full mobile pass (bottom tabs etc.) is W8.7; this just keeps the
  // three-pane layout from being crushed on a small screen meanwhile.
  const explorerHidden = isMobile && !mobileExplorerOpen;
  const editorColumnHidden = isMobile && mobileExplorerOpen;
  const toggleExplorer = useCallback(() => setMobileExplorerOpen((v) => !v), []);

  const pendingCloseMessage = pendingClose
    ? pendingClose.dirty.length === 1
      ? `${basename(pendingClose.dirty[0])} has unsaved changes. Closing it will discard them.`
      : `${pendingClose.dirty.length} files have unsaved changes (${pendingClose.dirty
          .map(basename)
          .join(", ")}). Closing them will discard those changes.`
    : "";

  return (
    <div className="flex-1 min-h-0 flex flex-col border border-[var(--neutral-800)] rounded-lg overflow-hidden">
      {/* Main row: explorer | splitter | editor column. W2.3b adds the
          preview column to the right of the editor column and the bottom
          panel between this row and the status bar. */}
      <div className="flex-1 min-h-0 flex">
        <div
          className={explorerHidden ? "hidden" : isMobile ? "flex-1 min-w-0" : "shrink-0"}
          style={isMobile ? undefined : { width: explorerSplitter.size }}
        >
          <Explorer
            filesMeta={filesMeta}
            loading={listLoading}
            error={listError}
            activePath={activePath}
            flagsKey={flagsKey}
            onOpenFile={openFile}
            onRefresh={handleRefresh}
            onDownloadZip={downloadZip}
            downloading={downloading}
            downloadError={downloadError}
          />
        </div>

        {!isMobile && (
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize file explorer"
            onMouseDown={explorerSplitter.onHandleMouseDown}
            className="w-1 shrink-0 cursor-col-resize bg-[var(--neutral-800)] hover:bg-[var(--accent)] transition-colors"
          />
        )}

        <div className={editorColumnHidden ? "hidden" : "flex-1 min-w-0 flex flex-col"}>
          <EditorTabs
            tabs={tabs}
            activePath={activePath}
            flagsKey={flagsKey}
            onActivate={activateTab}
            onClose={closeOne}
            onCloseOthers={closeOthers}
            onCloseAll={closeAll}
            onSave={saveActive}
            canSave={!!activeBuffer?.dirty}
            saving={!!(activePath && saving[activePath])}
            onToggleExplorer={isMobile ? toggleExplorer : undefined}
          />

          {notice && (
            <div className="shrink-0 flex items-center justify-between gap-2 text-[11px] text-red-300 bg-red-500/10 border-b border-red-500/30 px-3 py-1.5">
              <span className="min-w-0 truncate">{notice}</span>
              <button
                type="button"
                onClick={() => setNotice(null)}
                aria-label="Dismiss"
                className="shrink-0 hover:text-red-100"
              >
                <X size={12} />
              </button>
            </div>
          )}

          {activeBuffer?.stale && (
            // W0.1's banner, now per buffer (the store's `stale` flag):
            // this file changed on the server while it had unsaved edits.
            <div className="shrink-0 flex items-center justify-between gap-2 text-[11px] text-amber-300 bg-amber-500/10 border-b border-amber-500/30 px-3 py-1.5">
              <span>Changed on server — this file was updated elsewhere while you had unsaved edits.</span>
              <div className="flex items-center gap-3 shrink-0">
                <button
                  type="button"
                  onClick={() => reloadFile(activePath, { force: true })}
                  className="underline hover:text-amber-200"
                >
                  Reload
                </button>
                <button type="button" onClick={() => keepMine(activePath)} className="underline hover:text-amber-200">
                  Keep mine
                </button>
              </div>
            </div>
          )}

          {activePath && saveErrors[activePath] && (
            <p className="shrink-0 text-xs text-red-400 border-b border-[var(--neutral-800)] px-3 py-1.5">
              {saveErrors[activePath]}
            </p>
          )}

          <div className="relative flex-1 min-h-0">
            {tabs.map((path) => {
              const buffer = buffers[path];
              if (!buffer) return null; // still loading — the placeholder below covers the active one
              return (
                <EditorPane
                  key={path}
                  path={path}
                  active={path === activePath}
                  visible={!editorColumnHidden}
                  value={buffer.edited}
                  onEdit={handleEdit}
                  onSave={saveFile}
                  onCursor={handleCursor}
                />
              );
            })}
            {!activePath ? (
              <div className="absolute inset-0 flex items-center justify-center text-xs text-[var(--neutral-600)]">
                Select a file to view or edit it.
              </div>
            ) : !activeBuffer ? (
              <div className="absolute inset-0 flex items-center justify-center text-xs text-[var(--neutral-600)]">
                Loading…
              </div>
            ) : null}
          </div>
        </div>
      </div>

      <StatusBar
        providerId={provider.id}
        saveState={saveState}
        saveError={activePath ? saveErrors[activePath] : undefined}
        version={activeBuffer?.version}
        language={activeBuffer?.language}
        cursor={cursor}
      />

      <ConfirmDialog
        open={!!pendingClose}
        title="Discard unsaved changes?"
        message={pendingCloseMessage}
        confirmLabel="Discard"
        onConfirm={() => {
          if (pendingClose) dropTabs(pendingClose.paths);
          setPendingClose(null);
        }}
        onCancel={() => setPendingClose(null)}
      />
    </div>
  );
}

export default function EditorWorkbench({ workspaceId, apiUrl }) {
  return (
    <EditorStoreProvider>
      <WorkbenchBody workspaceId={workspaceId} apiUrl={apiUrl} />
    </EditorStoreProvider>
  );
}
