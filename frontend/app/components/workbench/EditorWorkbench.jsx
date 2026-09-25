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
// W2.3b completes the shell: a preview column at the right of the
// editor (empty until W6.1 mounts a PreviewPane in it), a bottom panel
// (Problems / Console / Terminal / History — containers only, each
// filled by its own later step), a drag splitter for each, and the
// persisted layout. Top to bottom the shell is now: a "main row"
// (explorer | editor | preview), the bottom panel, the status bar.
//
// Layout persistence is split in two (layoutPrefs.js has the why):
// pixel sizes go through useSplitter's own `storageKey`, exactly like
// the explorer width; the on/off/which-tab flags live in the editor
// store's `layout` and are written to one json key on change.
//
// W2.4: the explorer's file operations (new / rename / delete /
// duplicate / drag-move). The pane only reports intent; the async work
// is hooks/useExplorerOps.js, wired in below, with the two pieces that
// need this component's own state — the delete confirmation and
// re-keying per-path bookkeeping on a rename — kept here. `reserveCorner`
// is BuildTab telling us its floating "open chat" bubble is showing, so
// the bottom edge leaves room for it.
//
// W2.5: the save flow. Save now sends the buffer's `version` as
// base_version, so a save that lost a race comes back as a
// FileConflictError (409) carrying the server's current file. That
// surfaces as a three-choice bar — Reload theirs / Keep mine / Compare
// (ConflictCompareView) — instead of the blind overwrite the Code view
// always did; both resolutions reuse existing store actions (FILE_LOADED,
// SAVE_SUCCESS+keepEdited), so the reducer didn't change. Also here:
// optional autosave (one debounce timer per dirty file; the "which
// files" rule is tabUtils.planAutosave), optional Prettier
// format-on-save (formatOnSave.js, lazy-loaded; manual saves only), and
// the unsaved-edits guards — a `beforeunload` prompt, plus an
// `onDirtyChange` report that BuildTab uses to confirm before a project
// or sub-tab switch unmounts this component. That's a callback prop
// rather than a `ref` handle on purpose: BuildTab mounts this through
// next/dynamic, and next@14's dynamic() wrapper is a plain function
// component that never forwards `ref` to what it loads.
//
// W3.1 part 2: closes out Local's write path (part 1 shipped browsing/
// opening only — see fileProviders.js's own header). saveFile() below
// now branches on `provider.capabilities.writeNeedsConfirm`: Cloud (and
// any future provider with plain `write`) keeps the W2.5 path above
// unchanged; Local instead calls provider.propose("write_file", ...)
// and waits for a human to hit Confirm on the PendingActionBar already
// rendered above the main row (W3.1 part 1) — handleActionConfirmed()
// is what turns that confirm into a store update. Autosave is skipped
// entirely for a writeNeedsConfirm provider (see the autosave effect's
// own comment on why re-arming it would spam proposals), and the Save
// button/status bar read differently too (EditorTabs' saveLabel prop,
// StatusBar's "awaiting-confirmation" state).
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import { authHeaders } from "../../context/SessionContext";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { useExplorerOps } from "../../hooks/useExplorerOps";
import { useSplitter } from "../../hooks/useSplitter";
import { useViewport } from "../../hooks/useViewport";
// W4.1: the shared code-context store — mounted by BuildTab.jsx ABOVE
// this component (a sibling ancestor of EditorWorkbench and
// WorkspaceChatPanel, see codeContext.js's own header), so this is
// just an ordinary descendant read, the same relationship
// useEditorStore() below has to EditorStoreProvider.
import { useCodeContext } from "../../lib/workbench/codeContext";
// W5.3: the review round trip — getProposal() to fetch what
// pendingReview only carries the id of, resolveProposal() for Done.
import { ProposalStaleError, getProposal, resolveProposal } from "../../lib/workbench/codeProposals";
import { createCloudFileProvider, createLocalFileProvider, FileConflictError } from "../../lib/workbench/fileProviders";
import { EditorStoreProvider, useEditorStore } from "../../lib/workbench/editorStore";
import { basename, isSameOrDescendant, realFilePaths, remapPath } from "../../lib/workbench/fileTree";
import { deleteSummary } from "../../lib/workbench/explorerOps";
import { formatContent, isFormattable } from "../../lib/workbench/formatOnSave";
import { jumpToPosition } from "../../lib/workbench/gotoPosition";
import { buildDecisions, dirtyOverlap, unreviewableReason } from "../../lib/workbench/reviewMode";
import { useProjectSearch } from "../../hooks/useProjectSearch";
import {
  BOTTOM_PANEL_DEFAULT_HEIGHT,
  BOTTOM_PANEL_MIN_HEIGHT,
  EDITOR_MIN_WIDTH,
  LOCAL_SOURCE_IDS,
  MAIN_ROW_MIN_HEIGHT,
  PREVIEW_DEFAULT_WIDTH,
  PREVIEW_MIN_WIDTH,
  bottomPanelMaxHeight,
  browserStorage,
  loadLayout,
  previewMaxWidth,
  saveLayout,
} from "../../lib/workbench/layoutPrefs";
import { loadSavePrefs, saveSavePrefs } from "../../lib/workbench/savePrefs";
import { encodeTabFlags, planAutosave, planBufferSync } from "../../lib/workbench/tabUtils";
import ConfirmDialog from "../ConfirmDialog";
import PendingActionBar from "../PendingActionBar";
import TerminalPanel from "../TerminalPanel";
import BottomPanel from "./BottomPanel";
import CodeEditor from "./CodeEditor";
import ConflictCompareView from "./ConflictCompareView";
import EditorTabs from "./EditorTabs";
import Explorer from "./Explorer";
import HistoryPanel from "./HistoryPanel";
import PreviewColumn from "./PreviewColumn";
import PreviewPane from "./PreviewPane"; // NEW — W6.1: mounted below, replacing the placeholder PreviewColumn shows when its children prop is omitted
import ProjectSearchPanel from "./ProjectSearchPanel";
import QuickOpen from "./QuickOpen";
import ReviewPanel from "./ReviewPanel";
import StatusBar from "./StatusBar";

// W2.5: how long a dirty file has to sit untouched before autosave (when
// the toggle in savePrefs.js is on) saves it.
const AUTOSAVE_DELAY_MS = 1500;

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
 *
 * `registerPane` (W2.6) hands the parent the *ref object* itself, once,
 * rather than the view — the ref object is stable for this pane's whole
 * lifetime (a plain useRef(null)), so the parent can always read
 * `.current?.getView()` at the moment it actually needs the view (a
 * search result click, say) instead of the parent needing to know when
 * CodeEditor's own internal CM6 setup finishes.
 */
const EditorPane = memo(function EditorPane({
  path,
  active,
  visible,
  value,
  readOnly,
  onEdit,
  onSave,
  onCursor,
  registerPane,
  // W4.1: same "(path, ...)" shape as onEdit/onSave/onCursor above —
  // CodeEditor itself doesn't know its own path (see that file's own
  // onRangeChange doc comment), so this pane is what closes over it
  // before handing WorkbenchBody's codeContext handlers a path-aware
  // callback.
  onAddToChat,
  onRangeChange,
}) {
  const editorRef = useRef(null);
  const shown = active && visible;

  useEffect(() => {
    if (shown) editorRef.current?.getView()?.requestMeasure();
  }, [shown]);

  useEffect(() => {
    registerPane?.(path, editorRef);
    return () => registerPane?.(path, null);
  }, [path, registerPane]);

  return (
    <div className={active ? "absolute inset-0" : "hidden"}>
      <CodeEditor
        ref={editorRef}
        filePath={path}
        value={value}
        readOnly={readOnly}
        onChange={(text) => onEdit(path, text)}
        onSave={() => onSave(path)}
        onCursorChange={(pos) => onCursor(path, pos)}
        onAddToChat={onAddToChat ? (sel) => onAddToChat(path, sel) : undefined}
        onRangeChange={onRangeChange ? (mapRange) => onRangeChange(path, mapRange) : undefined}
      />
    </div>
  );
});

function WorkbenchBody({ workspaceId, apiUrl, reserveCorner, onDirtyChange }) {
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
    setLayout,
    switchSource,
    renamePaths,
    reviewOpen,
    reviewSetActive,
    reviewFileUpdate,
    reviewSubmitting,
    reviewError,
    reviewClose,
  } = useEditorStore();

  // W4.1: see this component's own import comment on why this is
  // available here even though CodeContextProvider is mounted in
  // BuildTab.jsx, above this whole component.
  const { addRef, remapRefs, pendingJump, clearJump, pendingReview, clearReview } = useCodeContext();

  // W3.1: which provider backs the workbench right now — read from
  // `layout.source` (see layoutPrefs.js's own header on why it lives
  // there) rather than component state, so it persists the same way
  // bottomOpen/previewOpen already do. One provider per (source,
  // workspace, api) triple — memoized so re-renders don't create a new
  // one (which would re-subscribe to Pusher for Cloud, or just be
  // wasteful for Local).
  const source = state.layout.source;
  const provider = useMemo(
    () =>
      source === "local"
        ? createLocalFileProvider({ workspaceId, apiUrl })
        : createCloudFileProvider({ workspaceId, apiUrl }),
    [source, workspaceId, apiUrl]
  );

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
  const [pendingDelete, setPendingDelete] = useState(null); // {roots, title, message} awaiting the delete confirmation (W2.4)
  const [cursor, setCursor] = useState(null); // {line, col} of the active file's caret
  const [mobileExplorerOpen, setMobileExplorerOpen] = useState(true); // single-pane layout only
  // W2.5: {path: {current, mine}} — a save 409 for this path, awaiting
  // Reload theirs / Keep mine / Compare. `current` is
  // FileConflictError.current (the server's file shape); `mine` is the
  // text that specific save attempt sent. Kept out of the editor store on
  // purpose — unlike `stale` (which several later steps read from
  // elsewhere), this is UI state about one Save call, the same tier
  // `saving`/`saveErrors` already live at.
  const [conflicts, setConflicts] = useState({});
  const [compareConflict, setCompareConflict] = useState(null); // path shown in ConflictCompareView, or null
  // W3.1 part 2: {path: contentThatWasProposed} — a write_file proposal
  // is outstanding for this path and nothing has been typed since. Kept
  // as content rather than a plain boolean/Set so a further keystroke
  // (buffer.edited no longer equal to the stored text) naturally falls
  // back out of "awaiting confirmation" into ordinary "dirty" without a
  // separate effect to notice the edit — see saveState's derivation and
  // handleActionConfirmed() below, the only two readers/writers besides
  // saveFile() itself.
  const [awaitingConfirmation, setAwaitingConfirmation] = useState({});
  // W2.5: read once per mount — workbench-wide, not per-workspace (see
  // savePrefs.js's own header), so unlike `layout` there's no
  // workspaceId-keyed re-read to do on a project switch.
  const [savePrefs, setSavePrefs] = useState(() => loadSavePrefs(browserStorage()));
  // W2.6: Quick Open (Cmd/Ctrl-P) and Project Search's own "give the
  // query box focus" signal — a counter rather than a boolean so
  // pressing Cmd/Ctrl-Shift-F again while the panel is ALREADY open and
  // focused still does something (re-selects the text) instead of being
  // a no-op change to a flag that's already true.
  const [quickOpenOpen, setQuickOpenOpen] = useState(false);
  const [searchFocusSeq, setSearchFocusSeq] = useState(0);

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

  // The two W2.3b splitters. Their bounds depend on how big the
  // workbench currently is, so the `max` functions read it from the
  // container ref at drag time (useSplitter accepts `() => number` for
  // exactly this) and are wrapped in useCallback: that keeps each
  // splitter's mouse-down handler — and with it the memoized
  // BottomPanel — from being rebuilt on every keystroke.
  //   preview: the handle is on the column's LEFT edge, so dragging left
  //            grows it → `reverse`.
  //   bottom:  the handle is on the panel's TOP edge, so dragging up
  //            grows it → `reverse`.
  const containerRef = useRef(null);
  const explorerWidth = explorerSplitter.size;
  const previewMax = useCallback(
    () => previewMaxWidth(containerRef.current?.clientWidth, explorerWidth),
    [explorerWidth]
  );
  const bottomMax = useCallback(() => bottomPanelMaxHeight(containerRef.current?.clientHeight), []);
  const previewSplitter = useSplitter({
    axis: "width",
    defaultSize: PREVIEW_DEFAULT_WIDTH,
    min: PREVIEW_MIN_WIDTH,
    max: previewMax,
    reverse: true,
    storageKey: `minime_build_editor_preview_w:${workspaceId}`,
  });
  const bottomSplitter = useSplitter({
    axis: "height",
    defaultSize: BOTTOM_PANEL_DEFAULT_HEIGHT,
    min: BOTTOM_PANEL_MIN_HEIGHT,
    max: bottomMax,
    reverse: true,
    storageKey: `minime_build_editor_bottom_h:${workspaceId}`,
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
  // W2.5: saveFile reads the format-on-save flag through this, so flipping
  // the toggle doesn't rebuild saveFile (and re-render every open pane).
  const savePrefsRef = useRef(savePrefs);
  savePrefsRef.current = savePrefs;
  const failedSavesRef = useRef({}); // {path: the exact text whose last save FAILED} — autosave won't retry that same text (planAutosave)
  const autosaveTimersRef = useRef(new Map()); // path -> {edited, timer}
  // W2.6: most-recently-active path first, capped — Quick Open's list
  // before anything is typed (defaultQuickOpenList's own `recentPaths`).
  // A ref, not state: it only needs to be read at the moment the palette
  // opens, not on every change, so there's nothing to gain from a
  // re-render every time a tab switch happens.
  const recentOpenRef = useRef([]);
  // W2.6: path -> the EditorPane's own ref OBJECT (not the view — see
  // EditorPane's header on why). Lets jumpToSearchResult() below reach a
  // specific open pane's live CodeMirror view without CodeEditor itself
  // needing to know Project Search exists.
  const paneRefsRef = useRef(new Map());
  // W3.1: mirrors useDaemonStatus's `live`, read by loadFileList()
  // above. A ref rather than a dependency so loadFileList's identity
  // (and the mount effect that calls it) doesn't churn every 5s poll
  // tick — only a real live/not-live transition should trigger a fetch,
  // and that's handled separately, by useDaemonStatus's own onLiveChange.
  const daemonLiveRef = useRef(false);
  // W3.1 part 2: action_id -> {path, content} for a write_file THIS
  // saveFile() proposed. PendingActionBar's own onConfirmed only hands
  // back {action_id, tool, params} (params for write_file is just
  // {path} — see PendingActionBar.jsx's Pusher handler, the content
  // never round-trips over the wire), so this is where the content we
  // actually sent lives until confirm/deny resolves it. Same "a Map the
  // component owns, not the store" shape as TerminalPanel.jsx's own
  // runsRef for the identical reason: correlating OUR proposals among
  // possibly several pending ones on this workspace (an agent, another
  // tab) is UI bookkeeping, not editor state.
  const pendingLocalWritesRef = useRef(new Map());

  // ---- file list ----------------------------------------------------

  const loadFileList = useCallback(async () => {
    // W3.1: Local with no live daemon isn't an ERROR to surface (see
    // Explorer.jsx's own `daemonOffline` branch) — it's an expected,
    // common state, so this skips the request entirely rather than
    // letting provider.list() throw a 409 that would otherwise show up
    // as a scary red banner where a calm "no daemon connected" already
    // does the job.
    if (provider.id === "local" && !daemonLiveRef.current) {
      filesMetaRef.current = null;
      setFilesMeta(null);
      setListError(null);
      setListLoading(false);
      return null;
    }
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
        delete failedSavesRef.current[p];
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
      // W2.5: a save conflict is about a path that's about to stop
      // being open — closing the tab (with the discard confirmation
      // that already guards a dirty buffer) resolves it as surely as
      // Reload theirs / Keep mine would.
      setConflicts((prev) => {
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
      setCompareConflict((prev) => (prev && paths.includes(prev) ? null : prev));
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
      // W5.3: a review replaces the tabs+editor area outright (see
      // ReviewPanel.jsx's own header) — a normal open would be either
      // invisible (ReviewPanel is what's actually showing) or would
      // abandon in-progress Keep/Undo decisions if it swapped
      // underneath. A file that's PART of the open review switches the
      // review to it, same as clicking its chip in ReviewPanel.jsx's
      // own toolbar; anything else asks for the review to be finished
      // or cancelled first rather than silently doing nothing.
      if (s.review) {
        if (s.review.files[path]) {
          reviewSetActive(path);
        } else {
          setNotice("Finish or cancel the current AI edit review before opening another file.");
        }
        return;
      }
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
    [provider, activateTab, setActivePath, fileLoaded, dropTabs, reviewSetActive]
  );

  // Every close path goes through here so unsaved edits are never
  // discarded silently. (The other ways to lose them — a page unload and
  // a project/sub-tab switch — are guarded by W2.5's beforeunload
  // listener and onDirtyChange report further down.)
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

  // ---- explorer operations (W2.4) -------------------------------------
  // The explorer only asks; hooks/useExplorerOps.js does the provider
  // calls and keeps tabs in step. These two are the bits of that which
  // need this component's own refs and state.

  // A rename/move re-paths the store's tabs (RENAME_PATHS); this does
  // the same for the per-path bookkeeping kept OUTSIDE the store. The
  // caret position follows the file; a "Keep mine" choice, a save error
  // and a save conflict (W2.5) were about the old path's server state, so
  // they're dropped.
  const rekeyPathState = useCallback((renames) => {
    const remap = (p) => {
      for (const r of renames) if (isSameOrDescendant(p, r.from)) return remapPath(p, r.from, r.to);
      return p;
    };
    for (const p of Object.keys(cursorsRef.current)) {
      const q = remap(p);
      if (q !== p) {
        cursorsRef.current[q] = cursorsRef.current[p];
        delete cursorsRef.current[p];
      }
    }
    for (const p of Object.keys(dismissedRef.current)) {
      if (remap(p) !== p) delete dismissedRef.current[p];
    }
    for (const p of Object.keys(failedSavesRef.current)) {
      if (remap(p) !== p) delete failedSavesRef.current[p];
    }
    setSaveErrors((prev) => {
      const stale = Object.keys(prev).filter((p) => remap(p) !== p);
      if (stale.length === 0) return prev;
      const next = { ...prev };
      for (const p of stale) delete next[p];
      return next;
    });
    setConflicts((prev) => {
      const stale = Object.keys(prev).filter((p) => remap(p) !== p);
      if (stale.length === 0) return prev;
      const next = { ...prev };
      for (const p of stale) delete next[p];
      return next;
    });
    setCompareConflict((prev) => (prev && remap(prev) !== prev ? null : prev));
  }, []);

  const explorerOps = useExplorerOps({
    provider,
    filesMetaRef,
    stateRef,
    savingRef,
    refresh: refreshFromServer,
    openFile,
    dropTabs,
    renamePaths,
    onRenamed: rekeyPathState,
    notify: setNotice,
  });

  // Delete asks first; what the dialog says (and how many files it
  // touches) is explorerOps.js's deleteSummary().
  const requestDelete = useCallback((paths) => {
    if (paths.length === 0) return;
    const dirtyPaths = Object.entries(stateRef.current.buffers)
      .filter(([, b]) => b.dirty)
      .map(([p]) => p);
    setPendingDelete(deleteSummary({ paths, filePaths: Object.keys(filesMetaRef.current || {}), dirtyPaths }));
  }, []);

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

  // `auto` = called by the autosave timer rather than by a person pressing
  // Save / Ctrl-S. The only difference is format-on-save: it runs for a
  // deliberate save, never for an autosave — reformatting a file while
  // someone is between thoughts (and any code that happens to parse
  // mid-edit) would shuffle text under their caret, which is why editors
  // that offer both leave formatting out of the timer-driven one.
  const saveFile = useCallback(
    async (path, { auto = false } = {}) => {
      const buffer = path ? stateRef.current.buffers[path] : null;
      if (!buffer || !buffer.dirty || savingRef.current.has(path)) return;
      let sent = buffer.edited;
      const baseVersion = buffer.version;
      savingRef.current.add(path);
      setSaving((prev) => ({ ...prev, [path]: true }));
      setSaveErrors((prev) => {
        if (!(path in prev)) return prev;
        const { [path]: _cleared, ...rest } = prev;
        return rest;
      });
      // A fresh Save attempt supersedes whatever the last one
      // conflicted on — if this one also conflicts, a new entry lands
      // below with the up-to-date `current`/`mine` pair.
      setConflicts((prev) => {
        if (!(path in prev)) return prev;
        const { [path]: _cleared, ...rest } = prev;
        return rest;
      });
      try {
        // W2.5: format-on-save, only for a deliberate save, only when the
        // toggle is on, and only for extensions formatOnSave.js has a
        // parser for. A syntax error mid-edit is normal — skip formatting
        // and save what's actually in the buffer rather than blocking the
        // save on it.
        if (!auto && savePrefsRef.current.formatOnSave && isFormattable(path)) {
          try {
            const formatted = await formatContent(path, sent);
            if (formatted !== sent) {
              sent = formatted;
              // Reflect the formatting in the editor too, so a save never
              // silently rewrites content the buffer doesn't show — but
              // only if nothing was typed while formatting ran (async: it
              // has to load prettier's chunks the first time).
              const preFormat = stateRef.current.buffers[path];
              if (preFormat && preFormat.edited === buffer.edited) editBuffer(path, formatted);
            }
          } catch (err) {
            console.warn(`[EditorWorkbench] format-on-save skipped for ${path}`, err);
          }
        }
        if (provider.capabilities.writeNeedsConfirm) {
          // W3.1 part 2: Local doesn't write — it proposes, and a
          // human confirms on PendingActionBar (rendered above the main
          // row whenever provider.id === "local") before anything
          // touches disk. Nothing here marks the buffer saved: `dirty`
          // stays true (it genuinely is — the content only exists in
          // the buffer and the pending proposal, not on disk yet) and
          // `awaitingConfirmation` is what lets the status bar/Save
          // button show that distinctly from an ordinary unsaved edit.
          // handleActionConfirmed() finishes the job once the person
          // actually clicks Confirm.
          const action = await provider.propose("write_file", { path, content: sent });
          pendingLocalWritesRef.current.set(action.action_id, { path, content: sent });
          delete failedSavesRef.current[path];
          setAwaitingConfirmation((prev) => ({ ...prev, [path]: sent }));
        } else {
          // W1.1/W2.5: base_version turns this into an optimistic-
          // concurrency write — a 409 (FileConflictError) means someone
          // else's save landed first; see the catch below instead of
          // the old blind overwrite.
          const saved = await provider.write(path, sent, { baseVersion });
          delete failedSavesRef.current[path];
          const latest = stateRef.current.buffers[path];
          // Typing during the round trip is normal (and constant once
          // autosave is on): if the buffer no longer matches what was
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
        }
      } catch (err) {
        if (err instanceof FileConflictError && err.current) {
          setConflicts((prev) => ({ ...prev, [path]: { current: err.current, mine: sent } }));
        } else {
          failedSavesRef.current[path] = sent;
          setSaveErrors((prev) => ({ ...prev, [path]: err.message }));
        }
      } finally {
        savingRef.current.delete(path);
        setSaving((prev) => {
          const { [path]: _done, ...rest } = prev;
          return rest;
        });
      }
    },
    [provider, saveSuccess, editBuffer]
  );

  const saveActive = useCallback(() => saveFile(stateRef.current.activePath), [saveFile]);

  // ---- save conflicts (W2.5) ------------------------------------------
  // "Reload theirs": discard the local edits, adopt the server's current
  // content wholesale — the same shape as any other open (fileLoaded),
  // since err.current IS a fresh read of the file.
  const reloadConflictTheirs = useCallback(
    (path) => {
      const conflict = conflicts[path];
      if (!conflict) return;
      fileLoaded(path, conflict.current);
      delete dismissedRef.current[path];
      delete failedSavesRef.current[path];
      setConflicts((prev) => {
        const { [path]: _cleared, ...rest } = prev;
        return rest;
      });
      setCompareConflict((prev) => (prev === path ? null : prev));
    },
    [conflicts, fileLoaded]
  );

  // "Keep mine": adopt the server's VERSION NUMBER (so the next Save's
  // base_version matches and goes through) while leaving the buffer's
  // `edited` text exactly as the person had it. SAVE_SUCCESS with
  // keepEdited does exactly that: `saved` becomes the server's current
  // content, `edited` stays put, and `dirty` is recomputed against it
  // (true, since overwriting what's on the server is the whole point of
  // the next Save). It's an explicit, informed overwrite the person just
  // chose from the conflict bar, unlike the blind PUT W1.1 replaced.
  const keepMineOnConflict = useCallback(
    (path) => {
      const conflict = conflicts[path];
      if (!conflict) return;
      saveSuccess(path, conflict.current, { keepEdited: true });
      dismissedRef.current[path] = conflict.current.version ?? 0;
      delete failedSavesRef.current[path];
      setConflicts((prev) => {
        const { [path]: _cleared, ...rest } = prev;
        return rest;
      });
      setCompareConflict((prev) => (prev === path ? null : prev));
    },
    [conflicts, saveSuccess]
  );

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

  // ---- quick open / project search / history (W2.6) ---------------------

  // Every real file path in the project — Quick Open's full candidate
  // list. The filtering itself (subsequence ranking) happens client-side
  // in quickOpen.js over this same array, so it only needs to change
  // when the file list actually does, not on every keystroke typed into
  // the palette.
  const allFilePaths = useMemo(() => realFilePaths(filesMeta), [filesMeta]);

  // Most-recently-active path first — Quick Open's own list before
  // anything is typed (defaultQuickOpenList's `recentPaths`).
  useEffect(() => {
    if (!state.activePath) return;
    recentOpenRef.current = [state.activePath, ...recentOpenRef.current.filter((p) => p !== state.activePath)].slice(
      0,
      50
    );
  }, [state.activePath]);

  const registerPane = useCallback((path, ref) => {
    if (ref) paneRefsRef.current.set(path, ref);
    else paneRefsRef.current.delete(path);
  }, []);

  // Retries across a few animation frames: a search-result click can
  // call openFile() for a file that isn't open yet, and the new pane's
  // ref won't be registered until AFTER the state update that adds its
  // tab has rendered — the same "wait for the DOM to catch up" shape as
  // CodeEditor's own requestMeasure(), bounded so a path that never
  // mounts (closed again before its read landed) doesn't retry forever.
  const jumpToPositionInPane = useCallback((path, pos, attempt = 0) => {
    const view = paneRefsRef.current.get(path)?.current?.getView();
    if (view) {
      jumpToPosition(view, pos);
      return;
    }
    if (attempt < 8) requestAnimationFrame(() => jumpToPositionInPane(path, pos, attempt + 1));
  }, []);

  // Project Search's own text source: an open tab's LIVE buffer (unsaved
  // edits included — the point is searching what's actually in front of
  // you), undefined for anything closed so useProjectSearch.js knows to
  // fall back to provider.read().
  const getOpenText = useCallback((path) => {
    const b = stateRef.current.buffers[path];
    return b ? b.edited : undefined;
  }, []);

  const search = useProjectSearch({ filesMeta, provider, getOpenText });

  const jumpToSearchResult = useCallback(
    (path, line, column, endColumn) => {
      openFile(path); // the already-open case is handled inside openFile() itself
      jumpToPositionInPane(path, { line, column, endColumn });
    },
    [openFile, jumpToPositionInPane]
  );

  // ---- code context (W4.1) --------------------------------------------
  // "Add to chat" from CodeEditor's floating toolbar / Mod-L / gutter
  // selection. EditorPane (above) has already closed over `path` before
  // this is called, so `sel` is exactly CodeEditor's own onAddToChat
  // shape: {from, to, fromLine, toLine, snippet}.
  const handleAddRange = useCallback(
    (path, sel) => {
      addRef({
        kind: "range",
        path,
        provider: provider.id,
        from: sel.from,
        to: sel.to,
        fromLine: sel.fromLine,
        toLine: sel.toLine,
        snippet: sel.snippet,
      });
    },
    [addRef, provider.id]
  );

  // CodeEditor's onRangeChange fires on every doc change with a plain
  // ChangeSet.mapPos-based mapper (see that file's own buildMapRange());
  // this just forwards it to the reducer's REMAP_REFS, which is a no-op
  // for any path with no "range" chips outstanding.
  const handleRangeChange = useCallback(
    (path, mapRange) => remapRefs(path, mapRange),
    [remapRefs]
  );

  // Explorer's "Add to chat" context-menu item — one or more selected
  // rows, each already classified "file"|"folder" by Explorer's own
  // typeOf(). A folder ref carries no snippet (W5.5 expands it
  // server-side later); a file ref prefers the LIVE buffer (unsaved
  // edits included, same reasoning as Project Search's getOpenText
  // above) and only falls back to a fresh provider.read() for a file
  // that isn't open.
  const handleAddToChat = useCallback(
    async (items) => {
      for (const { path, kind } of items) {
        if (kind === "folder") {
          addRef({ kind: "folder", path, provider: provider.id, snippet: "" });
          continue;
        }
        const openBuffer = stateRef.current.buffers[path];
        let snippet = openBuffer ? openBuffer.edited : null;
        if (snippet == null) {
          try {
            const file = await provider.read(path);
            snippet = file.content ?? "";
          } catch {
            snippet = "";
          }
        }
        addRef({ kind: "file", path, provider: provider.id, snippet });
      }
    },
    [addRef, provider]
  );

  // A chip's click-to-jump (ContextChips.jsx's requestJump →
  // codeContext.js's SET_PENDING_JUMP). Opens the file if it isn't
  // already, same as jumpToSearchResult above, then jumps to the
  // chip's own fromLine..toLine (a "file" ref has neither, so this
  // lands on line 1 — gotoPosition.js's own clamping handles that
  // default). Cleared right away so a second click on the SAME chip
  // still fires (pendingJump going from a ref back to that identical
  // ref wouldn't otherwise be a state change).
  useEffect(() => {
    if (!pendingJump) return;
    const ref = pendingJump;
    clearJump();
    openFile(ref.path);
    jumpToPositionInPane(ref.path, { line: ref.fromLine ?? 1, endLine: ref.toLine ?? undefined });
  }, [pendingJump, clearJump, openFile, jumpToPositionInPane]);

  // W5.3: fetches the proposal `pendingReview` only names by id — see
  // codeContext.js's SET_PENDING_REVIEW comment on why this doesn't
  // trust whatever shape the caller (WorkspaceChatPanel.jsx's "Review"
  // button, by way of BuildTab.jsx's CodeAwareChatPanel) already had.
  // A proposal reviewMode.js's unreviewableReason() rejects (already
  // resolved, gone stale, or a no-op edit with no files) surfaces as
  // the SAME dismissible notice a failed file open uses, rather than
  // opening a review with nothing to show.
  const openProposalForReview = useCallback(
    async (proposalId) => {
      setNotice(null);
      try {
        const proposal = await getProposal(apiUrl, workspaceId, proposalId);
        const reason = unreviewableReason(proposal);
        if (reason) {
          setNotice(reason);
          return;
        }
        reviewOpen(proposal);
      } catch (err) {
        setNotice(`Couldn't open this edit for review: ${err.message}`);
      }
    },
    [apiUrl, workspaceId, reviewOpen]
  );

  // Cleared right away, same as pendingJump above — clicking Review
  // again on the very same proposal (closed without deciding, then
  // reopened) must still fire.
  useEffect(() => {
    if (!pendingReview) return;
    const proposalId = pendingReview;
    clearReview();
    openProposalForReview(proposalId);
  }, [pendingReview, clearReview, openProposalForReview]);

  // ReviewPanel.jsx's "Done" — reviewMode.js's buildDecisions() turns
  // the review's current per-file state into exactly the `decisions`
  // POST .../code/proposals/{id}/resolve wants. A stale 409 (one of the
  // review's files changed on the server since the proposal was made)
  // disables Done rather than leaving it retryable — there is nothing
  // a retry of the SAME decisions would do differently. No success
  // notice on the happy path: `notice` (below) is styled as an error
  // banner everywhere else it's used, and resolveProposal()'s own
  // write already lands through the ordinary code_file_updated ->
  // syncOpenBuffers() channel (this component's `provider.subscribe`
  // effect, unchanged by W5.3) — reviewMode.js's resolvedMessage() is
  // exported for a caller that DOES want the wording (W5.4's tray is
  // the likely one) rather than unused.
  const handleReviewDone = useCallback(async () => {
    const current = stateRef.current.review;
    if (!current) return;
    reviewSubmitting(true);
    try {
      const decisions = buildDecisions(current);
      await resolveProposal(apiUrl, workspaceId, current.proposalId, decisions);
      reviewClose();
    } catch (err) {
      if (err instanceof ProposalStaleError) {
        reviewError(
          "This edit can no longer be applied — one of its files changed on the server since it was proposed. Close this review and ask again.",
          true
        );
      } else {
        reviewError(err.message || "Couldn't apply this edit.", false);
      }
    } finally {
      reviewSubmitting(false);
    }
  }, [apiUrl, workspaceId, reviewSubmitting, reviewClose, reviewError]);

  // History's "Restore": lands the server's response the same way a
  // normal save does — FILE_LOADED for the buffer, plus the file list's
  // own entry patched in place rather than a full refetch (saveFile()
  // above does the identical bookkeeping for a save; kept separate here
  // since Restore's caller, HistoryPanel, isn't the save flow).
  const handleRestored = useCallback(
    (path, file) => {
      fileLoaded(path, file);
      delete dismissedRef.current[path];
      delete failedSavesRef.current[path];
      setSaveErrors((prev) => {
        if (!(path in prev)) return prev;
        const { [path]: _cleared, ...rest } = prev;
        return rest;
      });
      setConflicts((prev) => {
        if (!(path in prev)) return prev;
        const { [path]: _cleared, ...rest } = prev;
        return rest;
      });
      const nextMeta = {
        ...(filesMetaRef.current || {}),
        [path]: {
          workspace_id: file.workspace_id,
          file_path: file.file_path,
          language: file.language,
          size: file.content ? file.content.length : 0,
          version: file.version,
          updated_at: file.updated_at,
          updated_by: file.updated_by,
        },
      };
      filesMetaRef.current = nextMeta;
      setFilesMeta(nextMeta);
    },
    [fileLoaded]
  );

  // ---- local source / daemon / terminal (W3.1) ---------------------------

  // Fires only on a not-live -> live transition (see useDaemonStatus's
  // own header) — the moment a daemon connects, go fetch the tree, the
  // same "don't make the person hit refresh themselves" behavior
  // LocalWorkspaceTab.jsx's own status effect already had.
  const handleDaemonLive = useCallback(() => {
    refreshRef.current();
  }, []);
  const { live: daemonLive, checked: daemonChecked } = useDaemonStatus(
    source === "local" ? workspaceId : null,
    handleDaemonLive
  );
  daemonLiveRef.current = daemonLive;

  // Switching source (the Explorer header's Project files / Local
  // folder toggle): a path under one source means nothing under the
  // other, so this is a hard reset of tabs/buffers (SWITCH_SOURCE
  // itself does that part — see editorStore.js). The only thing this
  // wrapper adds is asking first when there's something to lose,
  // exactly like requestClose() already does for a single dirty tab.
  const [pendingSourceChange, setPendingSourceChange] = useState(null);
  const requestChangeSource = useCallback(
    (next) => {
      if (next === stateRef.current.layout.source) return;
      const dirty = Object.values(stateRef.current.buffers).some((b) => b.dirty);
      if (dirty) setPendingSourceChange(next);
      else switchSource(next);
    },
    [switchSource]
  );

  // Any pending local action (a terminal command, or a proposed write)
  // landing is also the moment the tree might be stale — a command can
  // create/delete/modify files just as easily as a save can. Mirrors
  // LocalWorkspaceTab.jsx's own onConfirmed={() => live && loadRoot(...)}.
  //
  // W3.1 part 2: PendingActionBar calls this for EVERY confirmed action
  // on the workspace, not only ones this tab proposed (an agent, or
  // another browser tab, can confirm one too) — pendingLocalWritesRef is
  // what tells "a write_file THIS saveFile() proposed" apart from any
  // other confirmed action, the same way TerminalPanel.jsx's own runsRef
  // filters the same channel's events down to commands IT started.
  // Someone else's write to a path this tab happens to have open isn't
  // handled here: Local's capabilities.watch is false on purpose (see
  // fileProviders.js) — there's no live-change story for Local yet,
  // only for the specific case of a proposal this saveFile() itself made.
  const handleActionConfirmed = useCallback(
    (action) => {
      if (stateRef.current.layout.source === "local") refreshRef.current();
      const pending = action?.action_id ? pendingLocalWritesRef.current.get(action.action_id) : null;
      if (!pending || action.tool !== "write_file") return;
      pendingLocalWritesRef.current.delete(action.action_id);
      const { path, content } = pending;
      setAwaitingConfirmation((prev) => {
        if (!(path in prev)) return prev;
        const { [path]: _done, ...rest } = prev;
        return rest;
      });
      // Local files carry no version (LocalFileProvider.read()'s own
      // comment) — `{content, version: 0}` is the whole "saved" shape
      // SAVE_SUCCESS needs; `keepEdited` mirrors the Cloud save path
      // above: if the person kept typing while the confirm was pending,
      // the newer text stays in the editor rather than being clobbered
      // by the (now-confirmed, but by then stale) proposed text.
      const latest = stateRef.current.buffers[path];
      if (latest) saveSuccess(path, { content, version: 0 }, { keepEdited: latest.edited !== content });
    },
    [saveSuccess]
  );

  // The Terminal tab's real content once Local is active — reusing
  // components/TerminalPanel.jsx as-is (it already owns its own
  // propose/confirm/deny round trip for execute_command; nothing here
  // duplicates that). Cloud keeps BottomPanel's own EMPTY_STATES.terminal
  // copy, which already explains terminals are a local-folder thing.
  const terminalPanelNode = useMemo(() => {
    if (provider.id !== "local") return null;
    if (daemonChecked && !daemonLive) {
      return (
        <div className="h-full flex flex-col items-center justify-center gap-2 text-center text-xs text-[var(--neutral-600)] px-3">
          <span>
            No daemon connected — see <code className="text-[10px]">daemon/README.md</code>.
          </span>
        </div>
      );
    }
    return <TerminalPanel workspaceId={workspaceId} live={daemonLive} />;
  }, [provider.id, workspaceId, daemonLive, daemonChecked]);

  // ---- panel layout (W2.3b) --------------------------------------------

  const { layout } = state;

  // Persist the flags whenever they change (and once on mount, which
  // writes back what was just loaded — or removes the key when it's the
  // default; see saveLayout). SET_LAYOUT returns the same state for a
  // no-op, so `layout` only changes identity when a flag really flipped.
  useEffect(() => {
    saveLayout(browserStorage(), workspaceId, layout);
  }, [workspaceId, layout]);

  // W2.5: persist the autosave / format-on-save toggles. One shared key
  // (see savePrefs.js), so — unlike the layout effect just above — this
  // doesn't key off `workspaceId` at all.
  useEffect(() => {
    saveSavePrefs(browserStorage(), savePrefs);
  }, [savePrefs]);

  const toggleAutosave = useCallback(
    () => setSavePrefs((prev) => ({ ...prev, autosave: !prev.autosave })),
    []
  );
  const toggleFormatOnSave = useCallback(
    () => setSavePrefs((prev) => ({ ...prev, formatOnSave: !prev.formatOnSave })),
    []
  );

  // Read the flag through stateRef (like the callbacks above) so a
  // toggle's identity doesn't change with the layout it toggles.
  const toggleBottom = useCallback(
    () => setLayout({ bottomOpen: !stateRef.current.layout.bottomOpen }),
    [setLayout]
  );
  const selectBottomTab = useCallback(
    (bottomTab) => setLayout({ bottomTab, bottomOpen: true }),
    [setLayout]
  );
  const togglePreview = useCallback(
    () => setLayout({ previewOpen: !stateRef.current.layout.previewOpen }),
    [setLayout]
  );
  const closePreview = useCallback(() => setLayout({ previewOpen: false }), [setLayout]);

  // "Pending changes (N)": AI-proposed edits waiting for review. The
  // store's `proposals` stays empty until W5.4 loads them, so this is 0
  // for now; only `pending` ones count (resolved ones may live in the
  // list too — plan §5 W5.1's status column).
  const pendingCount = useMemo(
    () => state.proposals.filter((p) => p?.status === "pending").length,
    [state.proposals]
  );

  // ---- derived ---------------------------------------------------------

  const { tabs, buffers, activePath, review } = state;
  const activeBuffer = activePath ? buffers[activePath] : null;
  // W5.3: files in the open review whose NORMAL tab also has unsaved
  // edits — ReviewPanel.jsx's own amber banner (the proposal was made
  // against the saved version, not the buffer).
  const reviewDirtyPaths = useMemo(() => dirtyOverlap(review, buffers), [review, buffers]);
  const flagsKey = useMemo(() => encodeTabFlags(tabs, buffers), [tabs, buffers]);
  // W3.1 part 2: a proposed write is outstanding for the active path
  // and nothing has been typed since (awaitingConfirmation[path] still
  // equals the live buffer text) — checked ahead of "dirty" since a
  // proposal-in-waiting IS dirty (nothing's on disk yet) but reads as
  // something more specific than an ordinary unsaved edit.
  const isAwaitingConfirmation =
    !!activePath && provider.capabilities.writeNeedsConfirm && awaitingConfirmation[activePath] === activeBuffer?.edited;
  const saveState = !activeBuffer
    ? null
    : saving[activePath]
    ? "saving"
    : isAwaitingConfirmation
    ? "awaiting-confirmation"
    : conflicts[activePath]
    ? "conflict"
    : saveErrors[activePath]
    ? "error"
    : activeBuffer.dirty
    ? "dirty"
    : "saved";
  const conflict = activePath ? conflicts[activePath] : null;
  const compare = compareConflict ? conflicts[compareConflict] : null;

  // ---- bottom panel content (W2.6) --------------------------------------
  // Each node is memoized on its OWN inputs so handing them down as a
  // `panels` object doesn't defeat BottomPanel's memo() (see that file's
  // own header) — without this, a fresh JSX element on every render
  // (which WorkbenchBody does on every keystroke, via `buffers`) would
  // give BottomPanel a "new" prop every time regardless of whether
  // anything it actually renders changed.
  const searchPanelNode = useMemo(
    () => (
      <ProjectSearchPanel
        query={search.query}
        onQueryChange={search.setQuery}
        caseSensitive={search.caseSensitive}
        onCaseSensitiveChange={search.setCaseSensitive}
        results={search.results}
        matchCount={search.matchCount}
        truncated={search.truncated}
        loading={search.loading}
        error={search.error}
        filesScanned={search.filesScanned}
        totalFiles={search.totalFiles}
        onJumpToResult={jumpToSearchResult}
        focusSeq={searchFocusSeq}
      />
    ),
    [
      search.query,
      search.setQuery,
      search.caseSensitive,
      search.setCaseSensitive,
      search.results,
      search.matchCount,
      search.truncated,
      search.loading,
      search.error,
      search.filesScanned,
      search.totalFiles,
      jumpToSearchResult,
      searchFocusSeq,
    ]
  );

  const historyPanelNode = useMemo(
    () => (
      <HistoryPanel
        path={activePath}
        provider={provider}
        currentContent={activeBuffer?.saved}
        currentVersion={activeBuffer?.version}
        dirty={!!activeBuffer?.dirty}
        onRestored={handleRestored}
      />
    ),
    [activePath, provider, activeBuffer?.saved, activeBuffer?.version, activeBuffer?.dirty, handleRestored]
  );

  const bottomPanels = useMemo(
    () => ({
      search: searchPanelNode,
      history: historyPanelNode,
      ...(terminalPanelNode ? { terminal: terminalPanelNode } : {}),
    }),
    [searchPanelNode, historyPanelNode, terminalPanelNode]
  );

  // ---- autosave + unsaved-edits guards (W2.5) --------------------------

  // Autosave: one debounce timer per file that planAutosave() says is due.
  // Re-planned after every render (cheap: it walks the open tabs), and a
  // path's timer is only restarted when ITS text changed — so typing in
  // one file never postpones another file's pending save, and switching
  // tabs inside the delay doesn't strand the file you just left.
  // Anything no longer due (saved, conflicted, mid-save, toggle turned
  // off) has its timer cancelled. When an in-flight save finishes, `saving`
  // changes, which re-plans and picks up whatever was typed meanwhile.
  const saveFileRef = useRef(saveFile);
  saveFileRef.current = saveFile;
  useEffect(() => {
    const timers = autosaveTimersRef.current;
    // W3.1 part 2: never for a writeNeedsConfirm provider. A proposed
    // write deliberately leaves the buffer `dirty` (see saveFile()'s own
    // comment — nothing is actually saved until a human confirms), so
    // without this guard the very next debounce tick would find the
    // same file "due" again and propose it a second time, then a third,
    // for as long as it sits unconfirmed — flooding PendingActionBar
    // with duplicates of the same edit instead of the occasional retry
    // planAutosave is meant for.
    const due = savePrefs.autosave && !provider.capabilities.writeNeedsConfirm
      ? planAutosave({ tabs, buffers, busy: Object.keys(saving), conflicts, failed: failedSavesRef.current })
      : [];
    const dueText = new Map(due.map((d) => [d.path, d.edited]));
    for (const [path, t] of timers) {
      if (dueText.get(path) !== t.edited) {
        clearTimeout(t.timer);
        timers.delete(path);
      }
    }
    for (const { path, edited } of due) {
      if (timers.has(path)) continue;
      const timer = setTimeout(() => {
        timers.delete(path);
        saveFileRef.current(path, { auto: true });
      }, AUTOSAVE_DELAY_MS);
      timers.set(path, { edited, timer });
    }
  }, [savePrefs.autosave, tabs, buffers, saving, conflicts, provider]);

  useEffect(() => {
    const timers = autosaveTimersRef.current;
    return () => {
      for (const t of timers.values()) clearTimeout(t.timer);
      timers.clear();
    };
  }, []);

  // Anything unsaved? A boolean, so the two effects below only re-run
  // when it flips (first keystroke / last save), not on every keystroke.
  const anyDirty = useMemo(() => Object.values(buffers).some((b) => b.dirty), [buffers]);

  // Page unload / reload / closing the browser tab: the browser's own
  // "Leave site?" prompt. The listener only exists while something is
  // dirty — a page with an always-on beforeunload handler can be kept out
  // of the back/forward cache by some browsers.
  useEffect(() => {
    if (!anyDirty) return undefined;
    function onBeforeUnload(e) {
      e.preventDefault();
      e.returnValue = ""; // Chrome and Safari only show the prompt when this is set
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [anyDirty]);

  // In-app navigation that unmounts this component (BuildTab switching
  // projects, or leaving the Editor sub-tab) never fires beforeunload, so
  // BuildTab asks for the answer instead and confirms before navigating.
  // It's reported here rather than pulled through a ref — see the header.
  const onDirtyChangeRef = useRef(onDirtyChange);
  onDirtyChangeRef.current = onDirtyChange;
  useEffect(() => {
    onDirtyChangeRef.current?.(anyDirty);
  }, [anyDirty]);
  useEffect(() => {
    // Unmounting means whatever was unsaved is gone (or was just
    // confirmed away); don't leave the parent believing otherwise.
    const report = onDirtyChangeRef;
    return () => report.current?.(false);
  }, []);

  // Single-pane layout (phones): explorer and editor take turns filling
  // the width, toggled by the "Files" button in the tab strip. Both stay
  // MOUNTED and are hidden with display:none rather than unmounted, so
  // opening the file list doesn't tear down every editor's undo history.
  // A full mobile pass (bottom tabs etc.) is W8.7; this just keeps the
  // three-pane layout from being crushed on a small screen meanwhile.
  const explorerHidden = isMobile && !mobileExplorerOpen;
  const editorColumnHidden = isMobile && mobileExplorerOpen;
  const showPreview = !isMobile && layout.previewOpen;
  const toggleExplorer = useCallback(() => setMobileExplorerOpen((v) => !v), []);

  const pendingCloseMessage = pendingClose
    ? pendingClose.dirty.length === 1
      ? `${basename(pendingClose.dirty[0])} has unsaved changes. Closing it will discard them.`
      : `${pendingClose.dirty.length} files have unsaved changes (${pendingClose.dirty
          .map(basename)
          .join(", ")}). Closing them will discard those changes.`
    : "";

  // Cmd/Ctrl-P and Cmd/Ctrl-Shift-F (W2.6). A plain onKeyDown on the
  // workbench's own root div rather than a document-level listener:
  // neither combo is in CodeMirror's own keymaps (see CodeEditor.jsx's
  // own imports — defaultKeymap, searchKeymap, etc.), so the native
  // keydown bubbles up from inside an editor the same as it would from
  // anywhere else in this tree, and scoping the listener to the
  // container means a shortcut typed while focus is elsewhere on the
  // page (the chat dock, say) doesn't fire this handler at all.
  const handleWorkbenchKeyDown = useCallback(
    (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      const key = e.key.toLowerCase();
      if (key === "p" && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        setQuickOpenOpen(true);
      } else if (key === "f" && e.shiftKey && !e.altKey) {
        e.preventDefault();
        selectBottomTab("search");
        setSearchFocusSeq((n) => n + 1);
      }
    },
    [selectBottomTab]
  );

  return (
    <div
      ref={containerRef}
      onKeyDown={handleWorkbenchKeyDown}
      className="flex-1 min-h-0 flex flex-col border border-[var(--neutral-800)] rounded-lg overflow-hidden"
    >
      {/* W3.1: a proposed local action (a terminal command, or — as of
          part 2 — a proposed write from Save) awaiting Confirm/Deny.
          Above the whole main row, not just the terminal tab, the same
          placement LocalWorkspaceTab.jsx already used — a person
          mid-edit in the explorer/editor column should still see it
          land. */}
      {provider.id === "local" && <PendingActionBar workspaceId={workspaceId} onConfirmed={handleActionConfirmed} />}

      {/* Main row: explorer | splitter | editor column | splitter |
          preview column. The bottom panel and the status bar sit below
          it. The row keeps a minimum height so a tall bottom panel
          (which is allowed to shrink, see BottomPanel) can never crush
          the editor to nothing. */}
      <div className="flex-1 flex" style={{ minHeight: MAIN_ROW_MIN_HEIGHT }}>
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
            onDownloadZip={provider.id === "local" ? undefined : downloadZip}
            downloading={downloading}
            downloadError={downloadError}
            // "Can the tree itself change" (new file/folder, rename,
            // delete, duplicate, drag-move) — deliberately NOT the same
            // question as whether a buffer can be typed into (see
            // EditorPane's readOnly below and fileProviders.js's own
            // comment on the split). Local's capabilities.write is
            // false, so these stay hidden there; Save works anyway.
            canModify={provider.capabilities.write}
            busy={explorerOps.busy}
            onCreateFile={explorerOps.createFile}
            onCreateFolder={explorerOps.createFolder}
            onRename={explorerOps.rename}
            onRequestDelete={requestDelete}
            onDuplicate={explorerOps.duplicate}
            onMove={explorerOps.move}
            onAddToChat={handleAddToChat}
            source={source}
            onChangeSource={requestChangeSource}
            daemonLive={daemonLive}
            daemonChecked={daemonChecked}
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

        {/* On desktop the editor column has a real minimum width: with a
            flex-basis of 0 it would otherwise just take whatever is left,
            and an over-wide preview would squeeze it instead of being
            squeezed itself (the preview column is the one that shrinks). */}
        <div
          className={editorColumnHidden ? "hidden" : "flex-1 min-w-0 flex flex-col"}
          style={isMobile ? undefined : { minWidth: EDITOR_MIN_WIDTH }}
        >
          {review ? (
            <ReviewPanel
              review={review}
              dirtyPaths={reviewDirtyPaths}
              visible={!editorColumnHidden}
              onSelectFile={reviewSetActive}
              onFileChange={reviewFileUpdate}
              onDone={handleReviewDone}
              onClose={reviewClose}
            />
          ) : null}
          {/* W5.3: the normal tab strip + editors, kept mounted (not
              unmounted) under `review` so closing a review puts every tab
              back exactly as it was — undo history, cursor, scroll — see
              ReviewPanel.jsx's own header. `display:contents` when shown
              so this wrapper doesn't itself take part in the flex column
              above (its children lay out as if it weren't there). */}
          <div className={review ? "hidden" : "contents"}>
            <EditorTabs
            tabs={tabs}
            activePath={activePath}
            flagsKey={flagsKey}
            onActivate={activateTab}
            onClose={closeOne}
            onCloseOthers={closeOthers}
            onCloseAll={closeAll}
            onSave={saveActive}
            // W3.1 part 2: while a proposal is outstanding for the
            // active path and nothing has changed since (see
            // isAwaitingConfirmation above), Save is disabled rather
            // than left clickable — re-proposing identical content
            // would just pile a second, redundant pending action onto
            // PendingActionBar next to the one already waiting there.
            // Editing further makes it dirty again in the ordinary
            // sense (isAwaitingConfirmation stops matching) and Save
            // re-enables on its own, no extra bookkeeping needed.
            canSave={!!activeBuffer?.dirty && !isAwaitingConfirmation}
            saving={!!(activePath && saving[activePath])}
            saveLabel={provider.capabilities.writeNeedsConfirm ? "Propose write" : "Save"}
            saveLabelBusy={provider.capabilities.writeNeedsConfirm ? "Proposing…" : "Saving…"}
            onToggleExplorer={isMobile ? toggleExplorer : undefined}
            bottomOpen={layout.bottomOpen}
            onToggleBottom={toggleBottom}
            previewOpen={showPreview}
            onTogglePreview={isMobile ? undefined : togglePreview}
            // Autosave doesn't apply to a confirm-gated write (see the
            // autosave effect's own comment) and format-on-save isn't
            // worth splitting the menu out for on its own — the whole
            // Save-options control is hidden for Local rather than
            // offering a toggle that does nothing.
            savePrefs={provider.capabilities.writeNeedsConfirm ? undefined : savePrefs}
            onToggleAutosave={toggleAutosave}
            onToggleFormatOnSave={toggleFormatOnSave}
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

          {conflict && (
            // W2.5: this file's Save was rejected — someone else saved a
            // newer version first. Nothing was written and nothing is
            // lost: the person's text is still in the editor, the
            // server's is in `conflict.current`. Says what happened,
            // then names each way out (the buttons echo the plan's
            // Reload theirs / Keep mine / Compare wording).
            <div
              role="alert"
              className="shrink-0 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px] text-amber-300 bg-amber-500/10 border-b border-amber-500/30 px-3 py-1.5"
            >
              <span className="min-w-0">
                Not saved — this file changed on the server
                {conflict.current.version != null ? ` (now v${conflict.current.version})` : ""} after you opened it.
              </span>
              <div className="flex items-center gap-3 shrink-0">
                <button
                  type="button"
                  onClick={() => setCompareConflict(activePath)}
                  title="See the server's version next to yours"
                  className="underline hover:text-amber-200"
                >
                  Compare
                </button>
                <button
                  type="button"
                  onClick={() => reloadConflictTheirs(activePath)}
                  title="Discard your edits and load the server's version"
                  className="underline hover:text-amber-200"
                >
                  Reload theirs
                </button>
                <button
                  type="button"
                  onClick={() => keepMineOnConflict(activePath)}
                  title="Keep your edits — the next Save replaces the server's version"
                  className="underline hover:text-amber-200"
                >
                  Keep mine
                </button>
              </div>
            </div>
          )}

          {activeBuffer?.stale && !conflict && (
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
                  // W3.1 part 2: a buffer is editable when the provider
                  // can EITHER write directly (Cloud) OR propose a write
                  // (Local) — capabilities.write alone would leave Local
                  // permanently read-only, which was only ever true for
                  // part 1 before Save had anywhere to send a local
                  // edit. `truncated` overrides either way: Local's
                  // read() sets it for a file over the daemon's read
                  // limit, and saving a partial file would silently
                  // chop off the rest of it.
                  readOnly={!(provider.capabilities.write || provider.capabilities.writeNeedsConfirm) || !!buffer.truncated}
                  onEdit={handleEdit}
                  onSave={saveFile}
                  onCursor={handleCursor}
                  registerPane={registerPane}
                  onAddToChat={handleAddRange}
                  onRangeChange={handleRangeChange}
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

        {/* Preview column (W2.3b frame, W6.1 content). Not on the
            single-pane (phone) layout, which has no room for a third
            pane. */}
        {showPreview && (
          <>
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize preview"
              onMouseDown={previewSplitter.onHandleMouseDown}
              className="w-1 shrink-0 cursor-col-resize bg-[var(--neutral-800)] hover:bg-[var(--accent)] transition-colors"
            />
            <div className="shrink min-w-0" style={{ width: previewSplitter.size }}>
              <PreviewColumn onClose={closePreview}>
                <PreviewPane provider={provider} filesMeta={filesMeta} />
              </PreviewColumn>
            </div>
          </>
        )}
      </div>

      <BottomPanel
        open={layout.bottomOpen}
        activeTab={layout.bottomTab}
        onSelectTab={selectBottomTab}
        onToggle={toggleBottom}
        height={isMobile ? BOTTOM_PANEL_DEFAULT_HEIGHT : bottomSplitter.size}
        reserveRight={reserveCorner}
        resizable={!isMobile}
        onResizeStart={bottomSplitter.onHandleMouseDown}
        panels={bottomPanels}
      />

      <StatusBar
        providerId={provider.id}
        saveState={saveState}
        saveError={activePath ? saveErrors[activePath] : undefined}
        version={activeBuffer?.version}
        language={activeBuffer?.language}
        cursor={cursor}
        pendingCount={pendingCount}
        reserveRight={reserveCorner}
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

      <ConfirmDialog
        open={!!pendingDelete}
        title={pendingDelete?.title ?? ""}
        message={pendingDelete?.message ?? ""}
        confirmLabel="Delete"
        onConfirm={() => {
          const roots = pendingDelete?.roots;
          setPendingDelete(null);
          if (roots) explorerOps.remove(roots);
        }}
        onCancel={() => setPendingDelete(null)}
      />

      {/* W3.1: switching source is a hard reset of open tabs (see
          editorStore.js's SWITCH_SOURCE) — same discard-and-confirm
          shape as closing a dirty tab, just for everything open at once. */}
      <ConfirmDialog
        open={!!pendingSourceChange}
        title={pendingSourceChange === "local" ? "Switch to your local folder?" : "Switch to project files?"}
        message="Every open tab here has unsaved changes that don't exist under the other source. Switching will close them and discard those changes."
        confirmLabel="Switch"
        onConfirm={() => {
          if (pendingSourceChange) switchSource(pendingSourceChange);
          setPendingSourceChange(null);
        }}
        onCancel={() => setPendingSourceChange(null)}
      />

      {/* W2.5: "Compare" on the conflict bar. The right-hand side is the
          buffer as it is NOW (what "Keep mine" would keep), falling back
          to what the failed save sent. Both decisions are echoed here so
          the person doesn't have to close it to act. */}
      <ConflictCompareView
        open={!!compare}
        path={compareConflict || undefined}
        theirs={compare?.current?.content}
        theirsVersion={compare?.current?.version}
        mine={(compareConflict && buffers[compareConflict]?.edited) ?? compare?.mine}
        onClose={() => setCompareConflict(null)}
        onReloadTheirs={() => reloadConflictTheirs(compareConflict)}
        onKeepMine={() => keepMineOnConflict(compareConflict)}
      />

      {/* W2.6: Cmd/Ctrl-P. */}
      <QuickOpen
        open={quickOpenOpen}
        onClose={() => setQuickOpenOpen(false)}
        paths={allFilePaths}
        recentPaths={recentOpenRef.current}
        onOpenFile={openFile}
      />
    </div>
  );
}

/**
 * @param {object} props
 * @param {string} props.workspaceId
 * @param {string} props.apiUrl
 * @param {boolean} [props.reserveCorner]
 * @param {(dirty: boolean) => void} [props.onDirtyChange] - W2.5: true while any open file has unsaved edits, false again when saved or unmounted; BuildTab guards project / sub-tab switches with it
 */
export default function EditorWorkbench({
  workspaceId,
  apiUrl,
  reserveCorner = false,
  onDirtyChange,
  initialSourceOverride, // NEW — W3.2: BuildTab's one-time "the old Local Files tab redirected here" signal
  onConsumeInitialSourceOverride, // NEW — W3.2: called once this mount has applied (or ignored) the override above, same consumed-once shape as AppShell's own initialWorkspaceId
}) {
  // The saved panel layout, read once when the workbench mounts — the
  // store's initializer ignores later changes to it. That's fine for the
  // same reason the tabs are: BuildTab remounts this component per
  // project (key={selected.id}), so one workspace's layout never has to
  // be swapped for another's in place. (`workspaceId` is a dependency
  // only so the read is correct if that ever stops being true for the
  // first render.)
  //
  // W3.2: initialSourceOverride, when it validates against
  // LOCAL_SOURCE_IDS, wins over whatever this workspace's own persisted
  // layout says — it's how the retired "local" tab's redirect lands
  // someone on Local instead of whatever they last had open here. Not
  // written back through saveLayout: it's a one-time nudge for this
  // mount, not a new preference: the switch (or lack of one) after this
  // is the user's own choice again.
  const initialLayout = useMemo(() => {
    const loaded = loadLayout(browserStorage(), workspaceId);
    if (initialSourceOverride && LOCAL_SOURCE_IDS.has(initialSourceOverride)) {
      return { ...loaded, source: initialSourceOverride };
    }
    return loaded;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  // Consume the override exactly once per mount, whether or not it
  // validated — an invalid value shouldn't leave BuildTab re-arming it
  // forever either. `initialSourceOverride` itself is intentionally not
  // a dep: this component is remounted (key={selected.id}) for every
  // project switch, so "once per mount" already means "once", the same
  // guarantee LocalWorkspaceTab.jsx's own initialWorkspaceId consumer
  // used to rely on.
  useEffect(() => {
    if (initialSourceOverride) onConsumeInitialSourceOverride?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <EditorStoreProvider initialLayout={initialLayout}>
      <WorkbenchBody
        workspaceId={workspaceId}
        apiUrl={apiUrl}
        reserveCorner={reserveCorner}
        onDirtyChange={onDirtyChange}
      />
    </EditorStoreProvider>
  );
}
