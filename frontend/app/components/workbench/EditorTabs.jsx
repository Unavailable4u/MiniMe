"use client";
// frontend/app/components/workbench/EditorTabs.jsx — W2.3a (Build
// Workbench plan). The open-file tab strip above the editor.
//
// Purely presentational: it renders what EditorWorkbench hands it and
// reports intent through callbacks. It never touches the editor store
// itself — closing a tab with unsaved edits needs a confirmation
// dialog, and "which tab becomes active next" is the reducer's call
// (see nextActiveAfterClose in lib/workbench/tabUtils.js), both of
// which belong one level up.
//
// Per-tab flags arrive as ONE string (`flagsKey`, see
// encodeTabFlags()) rather than as buffers/Sets, so this component —
// wrapped in memo() — doesn't re-render on every keystroke, only when a
// dirty/stale/loading flag actually flips. That's also why every
// callback prop must be referentially stable (EditorWorkbench passes
// useCallback-wrapped ones); an inline arrow there would defeat the
// memo silently.
//
// W2.4: also hosts the two panel toggles (bottom panel, preview) at the
// strip's right end. They were in the status bar at first, but the
// app's floating "open chat" bubble sits fixed at the bottom-right of
// the screen whenever the chat dock is closed — right on top of them.
// The top-right corner is never under it.
//
// W2.5: the chevron beside Save opens the two save-flow toggles
// (Autosave, Format on save). Their state and the persistence live one
// level up (savePrefs.js / EditorWorkbench.jsx); this only draws the
// menu and reports the click.
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ChevronDown, FileCode, Files, Loader2, PanelBottom, PanelRight, Save, X } from "lucide-react";
import ContextMenu from "./ContextMenu";
import { decodeTabFlags, tabLabels } from "../../lib/workbench/tabUtils";

// An icon button that says whether its panel is showing (lit + aria-pressed).
function PanelToggle({ pressed, onClick, label, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={pressed}
      aria-label={label}
      title={label}
      className={`touch-target flex h-6 w-6 items-center justify-center rounded hover:bg-[var(--neutral-800)] ${
        pressed ? "text-[var(--accent)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
      }`}
    >
      {children}
    </button>
  );
}

function EditorTabs({
  tabs,
  activePath,
  flagsKey,
  onActivate,
  onClose,
  onCloseOthers,
  onCloseAll,
  onSave,
  canSave,
  saving,
  saveLabel = "Save", // W3.1 part 2: "Propose write" for a writeNeedsConfirm provider (Local)
  saveLabelBusy = "Saving…", // "Proposing…" alongside it
  onToggleExplorer, // only passed on the single-pane (mobile) layout
  bottomOpen = false,
  onToggleBottom,
  previewOpen = false,
  onTogglePreview, // omitted on the single-pane layout: no room for a third pane
  savePrefs, // {autosave, formatOnSave} (savePrefs.js) — the Save options menu is hidden without it
  onToggleAutosave,
  onToggleFormatOnSave,
}) {
  const stripRef = useRef(null);
  const [menu, setMenu] = useState(null); // {x, y, path} | null
  const [optionsMenu, setOptionsMenu] = useState(null); // {x, y} | null — the Save options menu (W2.5)
  const flags = useMemo(() => decodeTabFlags(flagsKey), [flagsKey]);
  const labels = useMemo(() => tabLabels(tabs), [tabs]);

  // Keep the active tab in view: opening a file from the explorer can
  // add a tab past the right edge of a strip that's already scrolling.
  // Looked up by data attribute on the strip's own children instead of
  // a querySelector so odd-but-valid path characters ([id], (group),
  // spaces) never need selector escaping.
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip || !activePath) return;
    const el = Array.from(strip.children).find((c) => c.dataset.tabPath === activePath);
    el?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [activePath, tabs.length]);

  return (
    <div className="shrink-0 flex items-stretch border-b border-[var(--neutral-800)] bg-[var(--neutral-950)]">
      {onToggleExplorer && (
        <button
          type="button"
          onClick={onToggleExplorer}
          aria-label="Show files"
          title="Files"
          className="shrink-0 px-3 border-r border-[var(--neutral-800)] text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
        >
          <Files size={14} />
        </button>
      )}

      <div
        ref={stripRef}
        role="tablist"
        aria-label="Open files"
        // A mouse wheel only scrolls vertically; on a strip that can
        // only scroll sideways that would do nothing, so translate it.
        onWheel={(e) => {
          if (e.deltaY && !e.deltaX && stripRef.current) stripRef.current.scrollLeft += e.deltaY;
        }}
        className="flex-1 min-w-0 flex overflow-x-auto overflow-y-hidden"
      >
        {labels.map(({ path, name, hint }) => {
          const active = path === activePath;
          const flag = flags[path] || {};
          return (
            <div
              key={path}
              role="tab"
              tabIndex={0}
              aria-selected={active}
              data-tab-path={path}
              title={path}
              onClick={() => onActivate(path)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onActivate(path);
                }
              }}
              // Middle-click closes. preventDefault on the mousedown
              // stops the browser's autoscroll cursor from starting
              // (it starts on mousedown, so the auxclick is too late);
              // the actual close is on auxclick, once the button is
              // released — the same moment every other editor does it.
              onMouseDown={(e) => {
                if (e.button === 1) e.preventDefault();
              }}
              onAuxClick={(e) => {
                if (e.button === 1) {
                  e.preventDefault();
                  onClose(path);
                }
              }}
              onContextMenu={(e) => {
                e.preventDefault();
                setMenu({ x: e.clientX, y: e.clientY, path });
              }}
              className={`group shrink-0 flex items-center gap-1.5 h-9 pl-3 pr-1.5 text-xs cursor-pointer select-none border-r border-[var(--neutral-800)] ${
                active
                  ? "bg-[var(--neutral-900)] text-[var(--neutral-100)] shadow-[inset_0_-2px_0_var(--accent)]"
                  : "text-[var(--neutral-500)] hover:text-[var(--neutral-200)] hover:bg-[var(--neutral-900)]"
              }`}
            >
              {flag.loading ? (
                <Loader2 size={12} className="shrink-0 animate-spin" />
              ) : flag.stale ? (
                <AlertTriangle
                  size={12}
                  className="shrink-0 text-amber-400"
                  aria-label="Changed on the server"
                />
              ) : (
                <FileCode size={12} className="shrink-0" />
              )}
              <span className="max-w-[10rem] truncate">{name}</span>
              {hint && <span className="max-w-[6rem] truncate text-[10px] text-[var(--neutral-600)]">{hint}</span>}

              {/* The dot and the × share one slot: a dirty tab shows the
                  dot until hovered, then the ×. Clicking either closes —
                  the parent confirms first if there are unsaved edits,
                  which is exactly the case the dot is warning about. */}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onClose(path);
                }}
                aria-label={flag.dirty ? `Close ${name} (unsaved changes)` : `Close ${name}`}
                title={flag.dirty ? "Unsaved changes — close" : "Close"}
                className={`shrink-0 flex h-5 w-5 items-center justify-center rounded text-[var(--neutral-500)] hover:bg-[var(--neutral-700)] hover:text-[var(--neutral-100)] ${
                  flag.dirty || active ? "" : "row-reveal"
                }`}
              >
                {flag.dirty ? (
                  <>
                    <span className="h-2 w-2 rounded-full bg-amber-400 group-hover:hidden" />
                    <X size={12} className="hidden group-hover:block" />
                  </>
                ) : (
                  <X size={12} />
                )}
              </button>
            </div>
          );
        })}
      </div>

      {activePath && (
        // The old Code view's Save button, kept as a visible control:
        // Cmd/Ctrl-S exists (CodeEditor's onSave) but a phone has no
        // such key, and "where do I save?" shouldn't need a shortcut.
        <div className="shrink-0 flex items-center gap-1 px-2 border-l border-[var(--neutral-800)]">
          <button
            type="button"
            onClick={onSave}
            disabled={!canSave || saving}
            className="flex items-center gap-1.5 rounded-lg border border-[var(--neutral-700)] px-2 py-1 text-xs font-medium text-[var(--neutral-200)] disabled:opacity-50"
          >
            {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
            {saving ? saveLabelBusy : saveLabel}
          </button>
          {savePrefs && (
            <button
              type="button"
              // While the menu is open, its own outside-pointerdown
              // dismissal would close it and then this click would
              // immediately reopen it. Keeping that pointerdown from
              // reaching the document lets the click below be a plain
              // toggle instead.
              onPointerDown={(e) => {
                if (optionsMenu) e.stopPropagation();
              }}
              onClick={(e) => {
                if (optionsMenu) {
                  setOptionsMenu(null);
                  return;
                }
                const rect = e.currentTarget.getBoundingClientRect();
                setOptionsMenu({ x: rect.left, y: rect.bottom + 4 });
              }}
              aria-haspopup="menu"
              aria-expanded={!!optionsMenu}
              aria-label="Save options"
              title={
                savePrefs.autosave || savePrefs.formatOnSave
                  ? `Save options (${[savePrefs.autosave && "autosave", savePrefs.formatOnSave && "format on save"]
                      .filter(Boolean)
                      .join(", ")} on)`
                  : "Save options"
              }
              className={`touch-target flex h-6 w-6 items-center justify-center rounded hover:bg-[var(--neutral-800)] ${
                savePrefs.autosave || savePrefs.formatOnSave
                  ? "text-[var(--accent)]"
                  : "text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
              }`}
            >
              <ChevronDown size={14} />
            </button>
          )}
        </div>
      )}

      {(onToggleBottom || onTogglePreview) && (
        <div className="shrink-0 flex items-center gap-1 px-2 border-l border-[var(--neutral-800)]">
          {onToggleBottom && (
            <PanelToggle pressed={bottomOpen} onClick={onToggleBottom} label="Toggle bottom panel">
              <PanelBottom size={14} />
            </PanelToggle>
          )}
          {onTogglePreview && (
            <PanelToggle pressed={previewOpen} onClick={onTogglePreview} label="Toggle preview">
              <PanelRight size={14} />
            </PanelToggle>
          )}
        </div>
      )}

      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          onClose={() => setMenu(null)}
          items={[
            { key: "close", label: "Close", icon: X, onSelect: () => onClose(menu.path) },
            {
              key: "others",
              label: "Close others",
              disabled: tabs.length < 2,
              onSelect: () => onCloseOthers(menu.path),
            },
            { key: "all", label: "Close all", onSelect: () => onCloseAll() },
            { key: "sep", separator: true },
            {
              key: "copy",
              label: "Copy path",
              onSelect: () => navigator.clipboard?.writeText(menu.path).catch(() => {}),
            },
          ]}
        />
      )}

      {optionsMenu && savePrefs && (
        <ContextMenu
          x={optionsMenu.x}
          y={optionsMenu.y}
          onClose={() => setOptionsMenu(null)}
          items={[
            {
              key: "autosave",
              label: "Autosave",
              checked: savePrefs.autosave,
              title: "Save automatically shortly after you stop typing",
              onSelect: onToggleAutosave,
            },
            {
              key: "format",
              label: "Format on save",
              checked: savePrefs.formatOnSave,
              title: "Run Prettier (JS, TS, JSON, CSS, HTML, Markdown) when you press Save or Ctrl/Cmd+S. Autosave never reformats.",
              onSelect: onToggleFormatOnSave,
            },
          ]}
        />
      )}
    </div>
  );
}

export default memo(EditorTabs);
