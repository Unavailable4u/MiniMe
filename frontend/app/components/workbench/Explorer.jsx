"use client";
// frontend/app/components/workbench/Explorer.jsx — W2.3a (Build
// Workbench plan). The file-tree pane. Replaces the old CodeView's
// inline tree (TreeNode + the header with the ZIP/refresh buttons);
// same look and same click-to-open behavior, now a component of its
// own that W2.4 can grow (context menu, rename, drag-move, filter,
// keyboard navigation, multi-select) without touching the shell.
//
// It knows nothing about providers, tabs or the editor store: it gets
// the file list and the active path in and reports "open this file" /
// "refresh" / "download ZIP" out. Which is the whole point of D6 —
// this pane looks the same over Cloud files today and Local files
// after W3.1.
//
// `flagsKey` is the same per-tab flag string EditorTabs takes (see
// encodeTabFlags()), used here only to put an "unsaved" dot beside
// files with edits; being a string it lets memo() skip re-rendering
// this whole tree on every keystroke.
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Download, FileCode, Folder, FolderOpen, Loader2, RefreshCw } from "lucide-react";
import { ancestorDirs, buildFileTree, sortedChildren, topLevelDirs } from "../../lib/workbench/fileTree";
import { decodeTabFlags } from "../../lib/workbench/tabUtils";

function TreeRows({ node, depth, expanded, onToggle, activePath, dirtyPaths, onOpen }) {
  return (
    <>
      {sortedChildren(node).map((entry) => {
        if (entry.type === "dir") {
          const isOpen = expanded.has(entry.path);
          return (
            <div key={`d:${entry.path}`} role="none">
              <button
                type="button"
                role="treeitem"
                aria-expanded={isOpen}
                aria-selected={false}
                onClick={() => onToggle(entry.path)}
                className="w-full flex items-center gap-1 text-xs text-[var(--neutral-300)] hover:text-[var(--neutral-100)] py-0.5 rounded"
                style={{ paddingLeft: `${depth * 14}px` }}
              >
                {isOpen ? <ChevronDown size={12} className="shrink-0" /> : <ChevronRight size={12} className="shrink-0" />}
                {isOpen ? <FolderOpen size={12} className="shrink-0" /> : <Folder size={12} className="shrink-0" />}
                <span className="truncate">{entry.name}</span>
              </button>
              {isOpen && (
                <div role="group">
                  <TreeRows
                    node={entry}
                    depth={depth + 1}
                    expanded={expanded}
                    onToggle={onToggle}
                    activePath={activePath}
                    dirtyPaths={dirtyPaths}
                    onOpen={onOpen}
                  />
                </div>
              )}
            </div>
          );
        }
        const isActive = entry.path === activePath;
        return (
          <button
            key={`f:${entry.path}`}
            type="button"
            role="treeitem"
            aria-selected={isActive}
            onClick={() => onOpen(entry.path)}
            title={entry.path}
            className={`w-full flex items-center gap-1 text-xs py-0.5 rounded ${
              isActive
                ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                : "text-[var(--neutral-400)] hover:text-[var(--neutral-100)]"
            }`}
            style={{ paddingLeft: `${depth * 14 + 16}px` }}
          >
            <FileCode size={12} className="shrink-0" />
            <span className="truncate">{entry.name}</span>
            {dirtyPaths.has(entry.path) && (
              <span
                className="ml-auto mr-1 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400"
                role="img"
                aria-label="Unsaved changes"
              />
            )}
          </button>
        );
      })}
    </>
  );
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
}) {
  const [expanded, setExpanded] = useState(() => new Set());
  const autoExpandedRef = useRef(false);

  const tree = useMemo(() => (filesMeta ? buildFileTree(filesMeta) : null), [filesMeta]);
  const fileCount = filesMeta ? Object.keys(filesMeta).length : 0;
  const dirtyPaths = useMemo(() => {
    const set = new Set();
    const decoded = decodeTabFlags(flagsKey);
    for (const [path, f] of Object.entries(decoded)) if (f.dirty) set.add(path);
    return set;
  }, [flagsKey]);

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
  // chat chip or the preview), open the folders above it. Only ever
  // ADDS to the open set, and only when the active path changes, so
  // collapsing the folder of the file you're editing sticks.
  useEffect(() => {
    if (!activePath) return;
    setExpanded((prev) => {
      const missing = ancestorDirs(activePath).filter((d) => !prev.has(d));
      return missing.length ? new Set([...prev, ...missing]) : prev;
    });
  }, [activePath]);

  function toggleDir(path) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  return (
    <div className="h-full min-h-0 flex flex-col">
      <div className="shrink-0 flex items-center justify-between px-3 h-9 border-b border-[var(--neutral-800)]">
        <span className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">
          Files{fileCount ? ` (${fileCount})` : ""}
        </span>
        <div className="flex items-center gap-2">
          {/* Moved here from the old CodeView's own header (patch 11's
              ZIP download) — the plan puts it in the explorer header.
              Disabled until there's at least one saved file. */}
          <button
            type="button"
            onClick={onDownloadZip}
            disabled={downloading || fileCount === 0}
            aria-label="Download all files as ZIP"
            title="Download as ZIP"
            className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)] disabled:opacity-50"
          >
            {downloading ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
          </button>
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            aria-label="Refresh file list"
            title="Refresh"
            className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)] disabled:opacity-50"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto p-2">
        {downloadError && <p className="text-[10px] text-red-400 px-1 pb-1">{downloadError}</p>}
        {loading && !filesMeta ? (
          <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5 px-1 py-1">
            <Loader2 size={12} className="animate-spin" /> Loading…
          </div>
        ) : error ? (
          <p className="text-xs text-red-400 px-1">{error}</p>
        ) : fileCount === 0 ? (
          <p className="text-xs text-[var(--neutral-600)] px-1">
            No files yet — ask this project&apos;s chat to build something and generated files will show up here.
          </p>
        ) : (
          <div role="tree" aria-label="Project files">
            <TreeRows
              node={tree}
              depth={0}
              expanded={expanded}
              onToggle={toggleDir}
              activePath={activePath}
              dirtyPaths={dirtyPaths}
              onOpen={onOpenFile}
            />
          </div>
        )}
      </div>
    </div>
  );
}

export default memo(Explorer);
