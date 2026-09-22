"use client";
// frontend/app/components/workbench/BottomPanel.jsx — W2.3b (Build
// Workbench plan). The panel under the editor row: Problems · Console ·
// Terminal · History. It is the CONTAINER only — every tab shows an
// empty state for now, and each later step fills in its own:
//   Problems  → W8.x (lint / diagnostics)      Console → W6.2 (preview bridge)
//   Terminal  → W3.1 (local daemon)            History → W2.6 (file versions)
//
// The tab strip is always rendered, even collapsed: a closed panel is
// just that 32px strip, which keeps the panel discoverable without
// costing the editor its height until someone asks for it.
//
// Presentational, like the other panes: EditorWorkbench owns the state
// (open / which tab live in the editor store's `layout`, the height in
// a useSplitter) and hands it in. That is also why the resize handle is
// a plain `onResizeStart` prop — this file doesn't know about splitters,
// only where the handle goes (its top edge).
//
// W2.6: `panels[tabId]` — when the caller has real content for a tab
// (ProjectSearchPanel for "search", HistoryPanel for "history"), it's
// rendered in place of that tab's EMPTY_STATES entry. Problems/Console/
// Terminal have no entry yet and keep showing their placeholder until
// their own later step fills them in the same way. EditorWorkbench
// memoizes each node itself (its own header explains why) so passing
// this object doesn't defeat the memo() below.
import { memo, useId } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { BOTTOM_TABS } from "../../lib/workbench/layoutPrefs";

// What each tab says while it has nothing to show. User-facing copy:
// says what the tab is for, not which patch will build it.
const EMPTY_STATES = {
  problems: {
    title: "No problems",
    body: "Errors and warnings found in this project's files will be listed here.",
  },
  console: {
    title: "Console is empty",
    body: "Output and errors from the live preview will appear here.",
  },
  terminal: {
    title: "Terminal is for local folders",
    body: "It opens when a local folder is connected through the local daemon. Project files stored in the cloud have no terminal.",
  },
  history: {
    title: "File history",
    body: "Each save keeps the previous version of a file on the server. Browsing and restoring those versions will happen here.",
  },
};

/**
 * @param {object} props
 * @param {boolean} props.open - false = only the tab strip shows
 * @param {string} props.activeTab - one of BOTTOM_TABS' ids
 * @param {(tabId: string) => void} props.onSelectTab - also opens the panel
 * @param {() => void} props.onToggle - collapse / expand
 * @param {number} props.height - px, the whole panel when open (strip included)
 * @param {boolean} [props.reserveRight] - keep the strip's right end clear for the app's floating "open chat" bubble (matters on narrow screens, where the tabs reach that far)
 * @param {boolean} [props.resizable=true] - false on the single-pane (phone) layout, where the handle is mouse-only
 * @param {(e: import("react").MouseEvent) => void} [props.onResizeStart] - useSplitter's onHandleMouseDown
 * @param {Record<string, import("react").ReactNode>} [props.panels] - real tab content, keyed by tab id (W2.6)
 */
function BottomPanel({
  open,
  activeTab,
  onSelectTab,
  onToggle,
  height,
  reserveRight = false,
  resizable = true,
  onResizeStart,
  panels,
}) {
  const uid = useId();
  const bodyId = `${uid}-body`;
  const empty = EMPTY_STATES[activeTab] || EMPTY_STATES.problems;
  const customPanel = panels?.[activeTab];

  return (
    <div
      // `shrink` (flex-shrink: 1) on purpose: when the window is too
      // short for the saved height AND the editor row's minimum, the
      // panel gives way instead of the editor collapsing to nothing.
      className={`shrink min-h-0 flex flex-col bg-[var(--neutral-950)] ${
        open ? "" : "border-t border-[var(--neutral-800)]"
      }`}
      style={open ? { height } : undefined}
    >
      {open && resizable && (
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize bottom panel"
          onMouseDown={onResizeStart}
          className="h-1 shrink-0 cursor-row-resize bg-[var(--neutral-800)] hover:bg-[var(--accent)] transition-colors"
        />
      )}
      {open && !resizable && <div className="h-px shrink-0 bg-[var(--neutral-800)]" />}

      {/* The collapse control is at the LEFT of the strip, ahead of the
          tabs. At the right it sat under the app's floating "open chat"
          bubble (fixed at the screen's bottom-right corner while the
          chat dock is closed), which made it unclickable. */}
      <div
        className="shrink-0 flex items-stretch h-8 border-b border-[var(--neutral-800)]"
        style={reserveRight ? { paddingRight: 56 } : undefined}
      >
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-label={open ? "Collapse bottom panel" : "Expand bottom panel"}
          title={open ? "Collapse panel" : "Expand panel"}
          className="touch-target shrink-0 px-2.5 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
        >
          {open ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
        </button>

        <div role="tablist" aria-label="Bottom panel" className="flex items-stretch min-w-0 overflow-x-auto">
          {BOTTOM_TABS.map((tab) => {
            // Nothing is "selected" while collapsed — there's no panel
            // showing for the tab to be the label of.
            const selected = open && tab.id === activeTab;
            return (
              <button
                key={tab.id}
                type="button"
                role="tab"
                id={`${uid}-tab-${tab.id}`}
                aria-selected={selected}
                aria-controls={open ? bodyId : undefined}
                onClick={() => onSelectTab(tab.id)}
                className={`shrink-0 px-3 text-[11px] uppercase tracking-wide select-none ${
                  selected
                    ? "text-[var(--neutral-100)] shadow-[inset_0_-2px_0_var(--accent)]"
                    : "text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
                }`}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      </div>

      {open && (
        <div
          id={bodyId}
          role="tabpanel"
          aria-labelledby={`${uid}-tab-${activeTab}`}
          className={`flex-1 min-h-0 overflow-auto ${customPanel ? "px-2 py-2" : "px-4 py-3"}`}
        >
          {customPanel || (
            <div className="h-full flex flex-col items-center justify-center gap-1 text-center">
              <p className="text-xs text-[var(--neutral-400)]">{empty.title}</p>
              <p className="max-w-sm text-[11px] leading-relaxed text-[var(--neutral-600)]">{empty.body}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default memo(BottomPanel);
