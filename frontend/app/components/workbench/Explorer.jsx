"use client";
// frontend/app/components/workbench/Explorer.jsx — W2.3a (Build
// Workbench plan), grown in W2.4 into the explorer with operations.
//
// W2.4 adds, on top of the W2.3a tree: multi-select (Ctrl/Cmd-click
// toggles, Shift-click ranges), keyboard navigation (arrows, Home/End,
// Enter, F2 rename, Delete), a right-click menu that a long-press opens
// on touch screens, inline rename and "New file / New folder", drag and
// drop to move, a filter box, and per-type file icons.
//
// Still presentational in the sense that matters (decision D6): it
// knows nothing about providers, tabs or the editor store. It is given
// the file list, which file is open, and a set of callbacks —
// onCreateFile / onCreateFolder / onRename (each resolves to an error
// message or null, shown inline), onMove (resolves to the moves done),
// onDuplicate, onRequestDelete (the workbench owns the confirmation),
// onOpenFile, onRefresh, onDownloadZip — and reports intent through
// them. What those DO is the workbench's business
// (hooks/useExplorerOps.js), which is why this pane looks the same
// over Cloud files today and Local files after W3.1.
//
// The tree is rendered as ONE flat list of rows (flattenVisible() in
// fileTree.js) with aria-level, not as nested components. That is what
// makes "next row", shift-click ranges and drop targets a matter of
// array indices.
//
// `flagsKey` is the same per-tab flag string EditorTabs takes (see
// encodeTabFlags()), used here only to put an "unsaved" dot beside
// files with edits; being a string it lets memo() skip re-rendering
// this whole tree on every keystroke.
import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Cloud,
  Copy,
  CopyPlus,
  Download,
  FilePlus,
  FolderPlus,
  HardDrive,
  Loader2,
  MessageSquarePlus,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  WifiOff,
  X,
} from "lucide-react";
import {
  ancestorDirs,
  buildFileTree,
  dirname,
  filterPaths,
  flattenVisible,
  isPlaceholderPath,
  isSameOrDescendant,
  rangeBetween,
  remapPath,
  topLevelDirs,
} from "../../lib/workbench/fileTree";
import { checkEntryName, collapseNested, entryPaths, joinPath, planDrop } from "../../lib/workbench/explorerOps";
import { decodeTabFlags } from "../../lib/workbench/tabUtils";
import { useIsTouchDevice } from "../../hooks/useIsTouchDevice";
import { useLongPress } from "../../hooks/useLongPress";
import ContextMenu from "./ContextMenu";
import { NewEntryRow, TreeRow } from "./ExplorerRow";

// What a drag from this tree carries. Checked on dragover so an
// unrelated drag (a file from the desktop, selected text) is ignored.
const DRAG_MIME = "application/x-minime-paths";

// Only drags that started in this tree count; anything else (a file
// dragged in from the desktop, selected text) is left to the browser.
function isOurDrag(e) {
  return Array.from(e.dataTransfer?.types || []).includes(DRAG_MIME);
}

function legacyCopy(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
  } catch {
    // nothing left to try — the paste will just be empty
  }
  document.body.removeChild(area);
}

// navigator.clipboard needs a secure context and a user gesture; the
// textarea route covers plain-http dev setups and older browsers.
function copyText(text) {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => legacyCopy(text));
  } else {
    legacyCopy(text);
  }
}

function Explorer({
  filesMeta, // {file_path: meta} | null (null = first load still running)
  loading,
  error,
  activePath,
  flagsKey,
  onOpenFile,
  onRefresh,
  onDownloadZip,
  downloading,
  downloadError,
  canModify = true, // FileProvider.capabilities.write
  busy = false, // a file operation is in flight (useExplorerOps)
  onCreateFile,
  onCreateFolder,
  onRename,
  onRequestDelete,
  onDuplicate,
  onMove,
  // W3.1: all four omitted (the default, from EditorWorkbench not
  // passing them) means "no switcher" — the header row below simply
  // doesn't render, so this stays a no-op change for any caller that
  // predates the source switcher.
  source, // "cloud" | "local" | undefined
  onChangeSource, // (next: "cloud"|"local") => void
  daemonLive = false,
  daemonChecked = false,
}) {
  const [expanded, setExpanded] = useState(() => new Set());
  const [selection, setSelection] = useState(() => new Set()); // paths, files and folders
  const [focusPath, setFocusPath] = useState(null); // the tree's one tab stop
  const [menu, setMenu] = useState(null); // {x, y, paths, root}
  const [editing, setEditing] = useState(null); // {kind: "rename", path} | {kind: "new", dir, isDir}
  const [filter, setFilter] = useState("");
  const [dropTarget, setDropTarget] = useState(null); // folder path, "" = project root, null = none
  const [focusTick, setFocusTick] = useState(0);
  const autoExpandedRef = useRef(false);
  const anchorRef = useRef(null); // where a Shift-range starts
  const shouldFocusRef = useRef(false); // DOM focus follows focusPath only after a keyboard move
  const dragSourcesRef = useRef([]);
  const areaRef = useRef(null);
  const touch = useIsTouchDevice();

  // ---- derived ---------------------------------------------------------

  const filePaths = useMemo(() => (filesMeta ? Object.keys(filesMeta) : []), [filesMeta]);
  const fileSet = useMemo(() => new Set(filePaths), [filePaths]);
  const taken = useMemo(() => entryPaths(filePaths), [filePaths]);
  const fileCount = useMemo(() => filePaths.filter((p) => !isPlaceholderPath(p)).length, [filePaths]);
  const tree = useMemo(() => (filesMeta ? buildFileTree(filesMeta) : null), [filesMeta]);
  const visible = useMemo(() => filterPaths(filePaths, filter), [filePaths, filter]);
  const rows = useMemo(() => (tree ? flattenVisible(tree, expanded, visible) : []), [tree, expanded, visible]);
  const dirtyPaths = useMemo(() => {
    const set = new Set();
    const decoded = decodeTabFlags(flagsKey);
    for (const [path, f] of Object.entries(decoded)) if (f.dirty) set.add(path);
    return set;
  }, [flagsKey]);

  // The latest of these, for the stable callbacks below (so a row's
  // handlers don't change identity — and re-render every row — each
  // time the selection or the list does).
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const selectionRef = useRef(selection);
  selectionRef.current = selection;
  const takenRef = useRef(taken);
  takenRef.current = taken;
  const fileSetRef = useRef(fileSet);
  fileSetRef.current = fileSet;
  const filteringRef = useRef(false);
  filteringRef.current = visible !== null;

  const typeOf = (path) => (fileSetRef.current.has(path) ? "file" : "dir");
  const editable = canModify && !busy;
  // W3.1: Local selected, the status poll has resolved at least once,
  // and it said not live. Distinct from `error` (a real, unexpected
  // failure) on purpose — see this component's own render branch below.
  const daemonOffline = source === "local" && daemonChecked && !daemonLive;

  // ---- effects -----------------------------------------------------------

  // First time files show up: open the top-level folders so the tree
  // isn't a single collapsed root. Once only — after that, what's open
  // is the person's own choice and a refresh must not undo it.
  useEffect(() => {
    if (!filesMeta || autoExpandedRef.current) return;
    const paths = Object.keys(filesMeta);
    if (paths.length === 0) return;
    autoExpandedRef.current = true;
    setExpanded((prev) => new Set([...prev, ...topLevelDirs(paths)]));
  }, [filesMeta]);

  // Keep the active file visible: when it changes (opened from
  // somewhere other than a click on its own row — a tab, and later a
  // chat chip or the preview), open the folders above it, select it and
  // make it the tree's tab stop. Only ever ADDS to the open set, and
  // only when the active path changes, so collapsing the folder of the
  // file you're editing sticks.
  useEffect(() => {
    if (!activePath) return;
    setExpanded((prev) => {
      const missing = ancestorDirs(activePath).filter((d) => !prev.has(d));
      return missing.length ? new Set([...prev, ...missing]) : prev;
    });
    setSelection(new Set([activePath]));
    anchorRef.current = activePath;
    setFocusPath(activePath);
  }, [activePath]);

  // Forget selected paths that no longer exist (deleted, or renamed
  // from elsewhere), so a later Delete/Drag can't act on a ghost.
  useEffect(() => {
    if (!filesMeta) return;
    setSelection((prev) => {
      const next = new Set([...prev].filter((p) => taken.has(p)));
      return next.size === prev.size ? prev : next;
    });
  }, [filesMeta, taken]);

  // DOM focus follows the tab stop only after a keyboard move — never
  // when the active file changes for some other reason, which would
  // steal focus from the editor.
  useEffect(() => {
    if (!shouldFocusRef.current) return;
    shouldFocusRef.current = false;
    const el = [...(areaRef.current?.querySelectorAll("[data-row-path]") || [])].find(
      (node) => node.dataset.rowPath === focusPath
    );
    el?.focus();
  }, [focusTick, focusPath]);

  // ---- selection / expansion helpers ---------------------------------------

  const requestFocus = useCallback((path) => {
    shouldFocusRef.current = true;
    setFocusPath(path);
    setFocusTick((t) => t + 1);
  }, []);

  const selectOnly = useCallback((path) => {
    setSelection(new Set([path]));
    anchorRef.current = path;
    setFocusPath(path);
  }, []);

  const toggleDir = useCallback((path) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const expandDirs = useCallback((dirs) => {
    setExpanded((prev) => {
      const missing = dirs.filter((d) => d && !prev.has(d));
      return missing.length ? new Set([...prev, ...missing]) : prev;
    });
  }, []);

  // After a rename/move succeeded: the same folders stay open, the
  // same things stay selected — under their new paths.
  const followRenames = useCallback((renames) => {
    const remap = (p) => {
      for (const r of renames) if (isSameOrDescendant(p, r.from)) return remapPath(p, r.from, r.to);
      return p;
    };
    setExpanded((prev) => new Set([...prev].map(remap)));
    setSelection((prev) => new Set([...prev].map(remap)));
    if (anchorRef.current) anchorRef.current = remap(anchorRef.current);
    setFocusPath((p) => (p ? remap(p) : p));
  }, []);

  // ---- clicking ---------------------------------------------------------------

  const handleRowClick = useCallback(
    (e, path, type) => {
      setFocusPath(path);
      if (e.metaKey || e.ctrlKey) {
        // Toggle this row in or out of the selection; opens nothing.
        setSelection((prev) => {
          const next = new Set(prev);
          if (next.has(path)) next.delete(path);
          else next.add(path);
          return next;
        });
        anchorRef.current = path;
        return;
      }
      if (e.shiftKey && anchorRef.current) {
        setSelection(new Set(rangeBetween(rowsRef.current, anchorRef.current, path)));
        return;
      }
      selectOnly(path);
      if (type === "dir") toggleDir(path);
      else onOpenFile(path);
    },
    [onOpenFile, selectOnly, toggleDir]
  );

  // A click on the empty space below the rows deselects (and makes
  // "New file" mean "at the top level", like every file explorer).
  const handleAreaClick = useCallback((e) => {
    if (e.target !== e.currentTarget) return;
    setSelection(new Set());
    anchorRef.current = null;
  }, []);

  // ---- the context menu ----------------------------------------------------------

  const openRowMenu = useCallback(
    (path, x, y) => {
      const current = selectionRef.current;
      let paths;
      if (current.has(path)) {
        paths = [...current];
      } else {
        paths = [path];
        selectOnly(path);
      }
      setFocusPath(path);
      setMenu({ x, y, paths, root: false });
    },
    [selectOnly]
  );

  const handleRowContextMenu = useCallback(
    (e, path) => {
      e.preventDefault();
      e.stopPropagation();
      let { clientX: x, clientY: y } = e;
      if (!x && !y) {
        // Fired from the keyboard (the Menu key): no pointer position —
        // open it just under the row.
        const rect = e.currentTarget.getBoundingClientRect();
        x = rect.left + 24;
        y = rect.bottom;
      }
      openRowMenu(path, x, y);
    },
    [openRowMenu]
  );

  const handleAreaContextMenu = useCallback((e) => {
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY, paths: [], root: true });
  }, []);

  const longPress = useLongPress(({ x, y, target }) => {
    if (!(target instanceof Element) || target.closest("input")) return;
    const rowEl = target.closest("[data-row-path]");
    if (rowEl) openRowMenu(rowEl.dataset.rowPath, x, y);
    else setMenu({ x, y, paths: [], root: true });
  });

  const closeMenu = useCallback(() => setMenu(null), []);

  // ---- creating / renaming (inline) ----------------------------------------------------

  // Where "New file" goes when nothing says otherwise: the selected
  // folder, the folder of the selected file, else the top level.
  const defaultNewDir = () => {
    const anchor = anchorRef.current;
    if (!anchor || !takenRef.current.has(anchor)) return "";
    return typeOf(anchor) === "dir" ? anchor : dirname(anchor);
  };

  const startNew = useCallback(
    (isDir, dir) => {
      if (!editable) return;
      setMenu(null);
      setFilter(""); // a filtered-out folder couldn't host the input
      if (dir) expandDirs([...ancestorDirs(`${dir}/x`), dir]);
      setEditing({ kind: "new", dir, isDir });
    },
    [editable, expandDirs]
  );

  const startRename = useCallback(
    (path) => {
      if (!editable) return;
      setMenu(null);
      setEditing({ kind: "rename", path });
    },
    [editable]
  );

  const cancelEdit = useCallback(() => setEditing(null), []);

  const newEntry = editing?.kind === "new" ? editing : null;
  const renamePath = editing?.kind === "rename" ? editing.path : null;

  const validateNew = useCallback(
    (value) => (newEntry ? checkEntryName(value, { dir: newEntry.dir, taken }).error : null),
    [newEntry, taken]
  );
  const submitNew = useCallback(
    async (name, { blurred }) => {
      const dir = newEntry.dir;
      const isDir = newEntry.isDir;
      const clean = checkEntryName(name, { dir, taken: takenRef.current }).name;
      const refusal = await (isDir ? onCreateFolder : onCreateFile)(dir, clean);
      if (refusal) return refusal;
      setEditing(null);
      if (isDir) {
        // A new folder is empty, so it wouldn't open on its own — select
        // it. (A new FILE opens in the editor, and the active-path
        // effect above selects and reveals it.)
        const path = joinPath(dir, clean);
        selectOnly(path);
        if (!blurred) requestFocus(path);
      }
      return null;
    },
    [newEntry, onCreateFile, onCreateFolder, selectOnly, requestFocus]
  );

  const validateRename = useCallback(
    (value) =>
      renamePath ? checkEntryName(value, { dir: dirname(renamePath), taken, selfPath: renamePath }).error : null,
    [renamePath, taken]
  );
  const submitRename = useCallback(
    async (name, { blurred }) => {
      const dir = dirname(renamePath);
      const clean = checkEntryName(name, { dir, taken: takenRef.current, selfPath: renamePath }).name;
      const refusal = await onRename(renamePath, clean);
      if (refusal) return refusal;
      const to = joinPath(dir, clean);
      setEditing(null);
      followRenames([{ from: renamePath, to }]);
      if (!blurred) requestFocus(to);
      return null;
    },
    [renamePath, onRename, followRenames, requestFocus]
  );

  // ---- the keyboard ---------------------------------------------------------------------------

  const handleKeyDown = useCallback(
    (e) => {
      const target = e.target;
      if (!(target instanceof Element) || target.closest("input, textarea")) return;
      const rowEl = target.closest("[data-row-path]");
      if (!rowEl) return;
      const path = rowEl.dataset.rowPath;
      const list = rowsRef.current;
      const index = list.findIndex((r) => r.path === path);
      if (index === -1) return;
      const row = list[index];
      const mod = e.metaKey || e.ctrlKey;

      const focusRow = (nextIndex, extend) => {
        const next = list[Math.max(0, Math.min(list.length - 1, nextIndex))];
        if (!next) return;
        requestFocus(next.path);
        if (extend) {
          anchorRef.current = anchorRef.current ?? path;
          setSelection(new Set(rangeBetween(list, anchorRef.current, next.path)));
        } else {
          setSelection(new Set([next.path]));
          anchorRef.current = next.path;
        }
      };
      const deleteTargets = () => {
        const current = selectionRef.current;
        return collapseNested(current.has(path) ? [...current] : [path]);
      };

      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          focusRow(index + 1, e.shiftKey);
          break;
        case "ArrowUp":
          e.preventDefault();
          focusRow(index - 1, e.shiftKey);
          break;
        case "Home":
          e.preventDefault();
          focusRow(0, e.shiftKey);
          break;
        case "End":
          e.preventDefault();
          focusRow(list.length - 1, e.shiftKey);
          break;
        case "ArrowRight":
          e.preventDefault();
          if (row.type === "dir") {
            if (!row.open) toggleDir(path);
            else if (list[index + 1]?.parent === path) focusRow(index + 1, false); // into its first child
          }
          break;
        case "ArrowLeft":
          e.preventDefault();
          if (row.type === "dir" && row.open && !filteringRef.current) {
            toggleDir(path);
          } else if (row.parent) {
            const parentIndex = list.findIndex((r) => r.type === "dir" && r.path === row.parent);
            if (parentIndex !== -1) focusRow(parentIndex, false);
          }
          break;
        case "Enter":
        case " ":
          e.preventDefault();
          selectOnly(path);
          if (row.type === "dir") toggleDir(path);
          else onOpenFile(path);
          break;
        case "F2":
          e.preventDefault();
          if (selectionRef.current.size <= 1) startRename(path);
          break;
        case "Delete":
        case "Backspace":
          // Plain Backspace is too easy to hit by accident on a row; Cmd/Ctrl+Backspace (the Mac "Delete") is deliberate.
          if (e.key === "Backspace" && !mod) break;
          e.preventDefault();
          if (editable) onRequestDelete(deleteTargets());
          break;
        case "a":
        case "A":
          if (!mod) break;
          e.preventDefault();
          setSelection(new Set(list.map((r) => r.path)));
          break;
        case "Escape":
          setSelection(new Set([path]));
          anchorRef.current = path;
          break;
        case "ContextMenu":
        case "F10": {
          if (e.key === "F10" && !e.shiftKey) break;
          e.preventDefault();
          const rect = rowEl.getBoundingClientRect();
          openRowMenu(path, rect.left + 24, rect.bottom);
          break;
        }
        default:
          break;
      }
    },
    [requestFocus, selectOnly, toggleDir, onOpenFile, startRename, editable, onRequestDelete, openRowMenu]
  );

  // ---- drag and drop -------------------------------------------------------------------------------

  const draggable = editable && !touch && !editing;

  // Would dropping what's being dragged onto `targetDir` move anything
  // legally? Decides the highlight and the cursor. (An illegal drop —
  // into itself, onto a name clash — just isn't offered.)
  const canDropOn = useCallback((targetDir) => {
    const sources = dragSourcesRef.current;
    if (sources.length === 0) return false;
    const { moves, error: planError } = planDrop({ sources, targetDir, taken: takenRef.current });
    return !planError && moves.length > 0;
  }, []);

  const handleRowDragStart = useCallback(
    (e, path) => {
      const current = selectionRef.current;
      if (!current.has(path)) selectOnly(path);
      const sources = collapseNested(current.has(path) ? [...current] : [path]);
      dragSourcesRef.current = sources;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData(DRAG_MIME, JSON.stringify(sources));
      e.dataTransfer.setData("text/plain", sources.join("\n"));
    },
    [selectOnly]
  );

  const clearDrag = useCallback(() => {
    dragSourcesRef.current = [];
    setDropTarget(null);
  }, []);

  const performDrop = useCallback(
    async (targetDir) => {
      const sources = dragSourcesRef.current;
      clearDrag();
      if (sources.length === 0) return;
      const applied = await onMove(sources, targetDir);
      if (applied && applied.length > 0) {
        followRenames(applied);
        if (targetDir) expandDirs([targetDir]);
      }
    },
    [onMove, clearDrag, followRenames, expandDirs]
  );

  // A row is the authority for drags over it (stopPropagation), so the
  // area behind only ever sees drags over the empty space = "the root".
  const handleRowDragOver = useCallback(
    (e, path, type) => {
      if (!isOurDrag(e)) return;
      e.stopPropagation();
      const targetDir = type === "dir" ? path : dirname(path);
      if (canDropOn(targetDir)) {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        setDropTarget(targetDir);
      } else {
        e.dataTransfer.dropEffect = "none";
        setDropTarget(null);
      }
    },
    [canDropOn]
  );

  const handleRowDrop = useCallback(
    (e, path, type) => {
      if (!isOurDrag(e)) return;
      e.preventDefault();
      e.stopPropagation();
      performDrop(type === "dir" ? path : dirname(path));
    },
    [performDrop]
  );

  const handleAreaDragOver = useCallback(
    (e) => {
      if (!isOurDrag(e)) return;
      if (canDropOn("")) {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        setDropTarget("");
      } else {
        e.dataTransfer.dropEffect = "none";
        setDropTarget(null);
      }
    },
    [canDropOn]
  );

  const handleAreaDrop = useCallback(
    (e) => {
      if (!isOurDrag(e)) return;
      e.preventDefault();
      performDrop("");
    },
    [performDrop]
  );

  // Leaving the whole pane (not just moving between two rows inside it).
  const handleAreaDragLeave = useCallback((e) => {
    if (!e.currentTarget.contains(e.relatedTarget)) setDropTarget(null);
  }, []);

  // ---- menu contents ---------------------------------------------------------------------------------

  function buildMenuItems({ paths, root }) {
    const many = paths.length > 1;
    const single = paths[0];
    const newDir = root ? "" : typeOf(single) === "dir" ? single : dirname(single);
    const items = [];

    if (canModify && !many) {
      items.push(
        { key: "new-file", label: "New file", icon: FilePlus, disabled: busy, onSelect: () => startNew(false, newDir) },
        { key: "new-folder", label: "New folder", icon: FolderPlus, disabled: busy, onSelect: () => startNew(true, newDir) }
      );
    }
    if (root) return items;

    if (canModify && !many) {
      items.push(
        { key: "sep-edit", separator: true },
        { key: "rename", label: "Rename", icon: Pencil, hint: "F2", disabled: busy, onSelect: () => startRename(single) },
        { key: "duplicate", label: "Duplicate", icon: CopyPlus, disabled: busy, onSelect: () => onDuplicate(single) }
      );
    }
    items.push(
      { key: "sep-copy", separator: true },
      { key: "copy-path", label: many ? "Copy paths" : "Copy path", icon: Copy, onSelect: () => copyText(paths.join("\n")) },
      // Wired up in W4.1, when code chips exist for it to add to.
      { key: "add-to-chat", label: "Add to chat", icon: MessageSquarePlus, disabled: true, title: "Coming soon" }
    );
    if (canModify) {
      items.push(
        { key: "sep-delete", separator: true },
        {
          key: "delete",
          label: many ? `Delete ${paths.length} items` : "Delete",
          icon: Trash2,
          hint: "Del",
          danger: true,
          disabled: busy,
          onSelect: () => onRequestDelete(collapseNested(paths)),
        }
      );
    }
    return items;
  }

  // ---- render --------------------------------------------------------------------------------------------

  const tabStopPath = rows.some((r) => r.path === focusPath)
    ? focusPath
    : rows.some((r) => r.path === activePath)
    ? activePath
    : rows[0]?.path ?? null;

  // Where the inline "new" row sits: right under its folder (top of
  // that folder's children), or first at the root.
  let newRowIndex = -1;
  let newRowDepth = 0;
  if (newEntry) {
    if (newEntry.dir === "") {
      newRowIndex = 0;
    } else {
      const at = rows.findIndex((r) => r.type === "dir" && r.path === newEntry.dir);
      if (at !== -1) {
        newRowIndex = at + 1;
        newRowDepth = rows[at].depth + 1;
      } else {
        newRowIndex = rows.length; // its folder isn't listed (yet): don't lose the input
      }
    }
  }
  const newEntryRow = newEntry ? (
    <div role="none" key="new-entry">
      <NewEntryRow
        depth={newRowDepth}
        isDir={newEntry.isDir}
        validate={validateNew}
        onSubmit={submitNew}
        onCancel={cancelEdit}
      />
    </div>
  ) : null;

  const showTree = rows.length > 0 || newEntry;

  return (
    <div className="h-full min-h-0 flex flex-col">
      <div className="shrink-0 flex items-center justify-between gap-2 px-3 h-9 border-b border-[var(--neutral-800)]">
        <span className="min-w-0 truncate text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">
          Files{fileCount ? ` (${fileCount})` : ""}
        </span>
        <div className="shrink-0 flex items-center gap-2">
          {canModify && (
            <>
              <button
                type="button"
                onClick={() => startNew(false, defaultNewDir())}
                disabled={busy}
                aria-label="New file"
                title="New file"
                className="touch-target text-[var(--neutral-500)] hover:text-[var(--neutral-200)] disabled:opacity-50"
              >
                <FilePlus size={12} />
              </button>
              <button
                type="button"
                onClick={() => startNew(true, defaultNewDir())}
                disabled={busy}
                aria-label="New folder"
                title="New folder"
                className="touch-target text-[var(--neutral-500)] hover:text-[var(--neutral-200)] disabled:opacity-50"
              >
                <FolderPlus size={12} />
              </button>
            </>
          )}
          {/* Moved here from the old CodeView's own header (patch 11's
              ZIP download) — the plan puts it in the explorer header.
              Disabled until there's at least one saved file. W3.1:
              omitted entirely (not just disabled) when the caller
              doesn't pass a handler — Local has no equivalent
              whole-folder export route, and a visibly-enabled button
              that silently does nothing is worse than no button. */}
          {onDownloadZip && (
            <button
              type="button"
              onClick={onDownloadZip}
              disabled={downloading || fileCount === 0}
              aria-label="Download all files as ZIP"
              title="Download as ZIP"
              className="touch-target text-[var(--neutral-500)] hover:text-[var(--neutral-200)] disabled:opacity-50"
            >
              {downloading ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
            </button>
          )}
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            aria-label="Refresh file list"
            title="Refresh"
            className="touch-target text-[var(--neutral-500)] hover:text-[var(--neutral-200)] disabled:opacity-50"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {/* W3.1: only rendered when a caller actually wires source
          switching in (see this component's own header comment on the
          four new props) — omitting them keeps every OTHER mounting of
          Explorer (there isn't one today, but nothing stops a future
          one) exactly as it was. */}
      {onChangeSource && (
        <div className="shrink-0 flex items-center gap-1 px-2 py-1.5 border-b border-[var(--neutral-800)]">
          <button
            type="button"
            onClick={() => onChangeSource("cloud")}
            aria-pressed={source === "cloud"}
            className={`flex-1 flex items-center justify-center gap-1 rounded-md py-1 text-[10px] font-medium min-h-[var(--viewport-touch-target)] ${
              source === "cloud"
                ? "bg-[var(--neutral-800)] text-[var(--neutral-100)]"
                : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            <Cloud size={11} /> Project files
          </button>
          <button
            type="button"
            onClick={() => onChangeSource("local")}
            aria-pressed={source === "local"}
            title={source === "local" ? (daemonLive ? "Daemon connected" : "No daemon connected") : undefined}
            className={`flex-1 flex items-center justify-center gap-1 rounded-md py-1 text-[10px] font-medium min-h-[var(--viewport-touch-target)] ${
              source === "local"
                ? "bg-[var(--neutral-800)] text-[var(--neutral-100)]"
                : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            <HardDrive size={11} />
            Local folder
            {source === "local" && (
              <span
                aria-hidden="true"
                className={`w-1.5 h-1.5 rounded-full ${daemonLive ? "bg-emerald-500" : "bg-[var(--neutral-700)]"}`}
              />
            )}
          </button>
        </div>
      )}

      {fileCount > 0 && (
        <div className="shrink-0 px-2 py-1.5 border-b border-[var(--neutral-800)]">
          <div className="flex items-center gap-1.5 rounded border border-[var(--neutral-800)] bg-[var(--neutral-950)] px-1.5 focus-within:border-[var(--neutral-600)]">
            <Search size={11} className="shrink-0 text-[var(--neutral-600)]" />
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && filter) {
                  e.preventDefault();
                  setFilter("");
                } else if (e.key === "ArrowDown" && rows.length > 0) {
                  e.preventDefault();
                  requestFocus(rows[0].path);
                }
              }}
              placeholder="Filter files"
              aria-label="Filter files"
              spellCheck={false}
              autoComplete="off"
              className="touch-input min-w-0 flex-1 bg-transparent py-1 text-xs text-[var(--neutral-200)] placeholder:text-[var(--neutral-600)] outline-none"
            />
            {filter && (
              <button
                type="button"
                onClick={() => setFilter("")}
                aria-label="Clear filter"
                title="Clear filter"
                className="touch-target shrink-0 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
              >
                <X size={11} />
              </button>
            )}
          </div>
        </div>
      )}

      <div
        ref={areaRef}
        onClick={handleAreaClick}
        onContextMenu={handleAreaContextMenu}
        onKeyDown={handleKeyDown}
        onDragOver={handleAreaDragOver}
        onDragLeave={handleAreaDragLeave}
        onDrop={handleAreaDrop}
        onDragEnd={clearDrag}
        {...longPress}
        className={`flex-1 min-h-0 overflow-y-auto p-2 ${
          dropTarget === "" ? "ring-1 ring-inset ring-[var(--accent)]/60" : ""
        }`}
      >
        {downloadError && <p className="text-[10px] text-red-400 px-1 pb-1">{downloadError}</p>}
        {daemonOffline ? (
          <div className="flex flex-col items-center gap-2 text-center text-xs text-[var(--neutral-600)] px-3 py-8">
            <WifiOff size={18} className="text-[var(--neutral-700)]" />
            <span>
              No daemon connected — see <code className="text-[10px]">daemon/README.md</code>. Pair a local folder
              and this fills in automatically once it connects.
            </span>
          </div>
        ) : loading && !filesMeta ? (
          <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5 px-1 py-1">
            <Loader2 size={12} className="animate-spin" /> Loading…
          </div>
        ) : error ? (
          <p className="text-xs text-red-400 px-1">{error}</p>
        ) : !showTree ? (
          <p className="text-xs text-[var(--neutral-600)] px-1 pointer-events-none">
            {visible
              ? `No files match "${filter.trim()}".`
              : canModify
              ? "No files yet — create one with the buttons above, or ask this project's chat to build something and generated files will show up here."
              : "No files yet — ask this project's chat to build something and generated files will show up here."}
          </p>
        ) : (
          <div role="tree" aria-label="Project files" aria-multiselectable="true">
            {rows.map((row, i) => (
              <Fragment key={`${row.type}:${row.path}`}>
                {i === newRowIndex && newEntryRow}
                <TreeRow
                  path={row.path}
                  name={row.name}
                  type={row.type}
                  depth={row.depth}
                  open={row.open}
                  selected={selection.has(row.path)}
                  active={row.type === "file" && row.path === activePath}
                  focusable={row.path === tabStopPath}
                  dirty={row.type === "file" && dirtyPaths.has(row.path)}
                  dropTarget={row.type === "dir" && dropTarget === row.path}
                  draggable={draggable}
                  renaming={row.path === renamePath}
                  validateName={row.path === renamePath ? validateRename : undefined}
                  onSubmitName={row.path === renamePath ? submitRename : undefined}
                  onCancelEdit={row.path === renamePath ? cancelEdit : undefined}
                  onRowClick={handleRowClick}
                  onRowContextMenu={handleRowContextMenu}
                  onRowDragStart={handleRowDragStart}
                  onRowDragOver={handleRowDragOver}
                  onRowDrop={handleRowDrop}
                  onRowDragEnd={clearDrag}
                />
              </Fragment>
            ))}
            {newRowIndex >= rows.length && newEntryRow}
          </div>
        )}
      </div>

      {menu && <ContextMenu x={menu.x} y={menu.y} items={buildMenuItems(menu)} onClose={closeMenu} />}
    </div>
  );
}

export default memo(Explorer);
