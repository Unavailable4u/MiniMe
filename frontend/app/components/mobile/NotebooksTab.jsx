"use client";
import { NotebookText, Plus, X, Loader2, ArrowUpRight } from "lucide-react";
import MobileDrawer from "./MobileDrawer";
import NotebooksGeneratePicker from "../notebooks/NotebooksGeneratePicker";
import { STAGE_THEME } from "../WorkspaceStageIcons";
import { SUB_TABS, UNREAD_DOT_TABS, PROMOTE_LABELS } from "../tabs/NotebooksTab";

// Mobile counterpart of ../tabs/NotebooksTab.jsx — retrofitted (mobile UI
// retrofit) from the ~10 `isMobile` branches that used to live inline in
// that file. Per this folder's README: business logic stays shared — the
// controller hook (`useNotebooksTabController`, exported from the desktop
// file) owns every bit of state, every effect, every handler, and even
// `subTabContent`/`dockAndModals`/`renderRoot` (the parts that were
// already identical on every viewport). This file only supplies the four
// things that genuinely differ from the desktop shell:
//   - the notebook list as a left-side drawer instead of a persistent
//     column (opened by the header hamburger — OPEN_TAB_SIDEBAR_EVENT,
//     handled inside the controller — or the notebook-name button
//     elsewhere in this file's caller)
//   - a vertical icon-only sub-tab rail instead of the desktop pill row,
//     since five labeled buttons don't fit a phone width
//   - a one-line "which sub-tab is active" header + generate picker,
//     replacing the desktop pill row's own generate picker slot
//   - a single combo <select> for promote-target+mode instead of three
//     separate desktop controls
//
// Takes `controller` (the already-built return value of
// useNotebooksTabController) as a prop rather than calling the hook
// itself — see the router at the bottom of ../tabs/NotebooksTab.jsx for
// why: the hook owns effects (data fetching, localStorage sync) that
// must only ever run once per render, so only the router calls it.
export default function NotebooksTab({ controller: c }) {
  const notebookPicker = (
    <MobileDrawer side="left" open={c.mobileNotebooksDrawerOpen} onClose={() => c.setMobileNotebooksDrawerOpen(false)}>
      <div className="w-72 max-w-[80vw] flex flex-col h-full">
        <div className="h-12 px-3 flex items-center justify-between border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <NotebookText size={13} className={STAGE_THEME.note.color} /> Notebooks
          </span>
          <div className="flex items-center gap-3">
            <button onClick={() => c.setCreating((v) => !v)} title="New notebook" className="text-[var(--neutral-400)] hover:text-[var(--neutral-100)] p-1">
              <Plus size={16} />
            </button>
            <button onClick={() => c.setMobileNotebooksDrawerOpen(false)} title="Close" className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] p-1">
              <X size={16} />
            </button>
          </div>
        </div>
        {c.notebookCreateForm}
        {c.notebookRows}
      </div>
    </MobileDrawer>
  );

  // Only shown once a notebook is selected and has an active chat —
  // before that there's no sub-tab content to switch between. Reuses
  // the w-10/w-12 slot the desktop-only notebook-picker column
  // occupies on wide screens; on mobile that column never renders (see
  // notebookPicker above, always a drawer), so this is the replacement
  // for it, not an addition alongside it.
  const iconRail = c.selected && c.hasActiveChat && (
    <div className="w-12 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-2 gap-1">
      {SUB_TABS.map((t) => {
        const badgeCount = t.id === "insights" ? c.candidates.length + c.clusterCandidates.length : 0;
        return (
          <button
            key={t.id}
            onClick={() => c.setSubTab(t.id)}
            title={t.label}
            className={`relative flex items-center justify-center w-9 h-9 rounded-lg ${c.subTab === t.id ? "bg-[var(--accent)] text-[var(--accent-text)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"}`}
          >
            <t.icon size={17} />
            {badgeCount > 0 && (
              <span className="absolute -top-1 -right-1 text-[9px] leading-none bg-amber-500/90 text-black rounded-full px-1 py-0.5 font-medium">
                {badgeCount}
              </span>
            )}
            {UNREAD_DOT_TABS.includes(t.id) && c.subTab !== t.id && c.hasUnseenUpdate(t.id) && (
              <span className="absolute top-1 right-1.5 w-1.5 h-1.5 rounded-full bg-amber-400" title="New content since you last viewed this tab" />
            )}
          </button>
        );
      })}
    </div>
  );

  // Desktop's control is three separate widgets (target <select>,
  // complete/partial radiogroup, a fully-worded "Promote to X →"
  // button) — plenty of room on desktop, but three tap targets plus a
  // wide label don't fit next to a notebook name on a phone. This
  // collapses target + mode into ONE <select> (each option already
  // encodes both, e.g. "→ Research"/"→ Research, keep here too") and
  // reduces the action itself to a small icon-only button.
  const promoteControl = c.promoteTargets && (
    <div className="flex items-center gap-1.5">
      <label className="sr-only" htmlFor="notebooks-promote-combo">Promote to</label>
      <select
        id="notebooks-promote-combo"
        value={`${c.promoteTargets.targetStage}|${c.promoteMode}`}
        onChange={(e) => {
          const [stage, mode] = e.target.value.split("|");
          c.setPromoteTargetStage(stage);
          c.setPromoteMode(mode);
        }}
        disabled={c.promoting}
        className="max-w-[132px] bg-[var(--neutral-900)] border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg pl-2 pr-1 py-1.5 text-xs outline-none disabled:opacity-50"
      >
        {c.promoteTargets.availableTargets.map((stage) => (
          <optgroup key={stage} label={PROMOTE_LABELS[stage]}>
            <option value={`${stage}|complete`}>→ {PROMOTE_LABELS[stage]}</option>
            <option value={`${stage}|partial`}>→ {PROMOTE_LABELS[stage]}, keep here too</option>
          </optgroup>
        ))}
      </select>
      <button
        onClick={() => c.handlePromote(c.selected.id, c.promoteTargets.targetStage)}
        disabled={c.promoting}
        title={`${c.promoteMode === "partial" ? "Add to" : "Promote to"} ${PROMOTE_LABELS[c.promoteTargets.targetStage]}`}
        className="shrink-0 flex items-center justify-center text-[var(--neutral-200)] border border-[var(--neutral-700)] rounded-lg p-1.5 disabled:opacity-50"
      >
        {c.promoting ? <Loader2 size={14} className="animate-spin" /> : <ArrowUpRight size={14} />}
      </button>
    </div>
  );

  // Replaces the desktop pill row: a slim header naming just the active
  // sub-tab (in the app's own accent color, so it stays legible at a
  // glance even though only an icon represents it in iconRail above),
  // plus the same generate picker.
  // FIX — was built unconditionally even when c.selected is undefined,
  // throwing on c.selected.id below (same bug/fix as the desktop
  // shell's navChrome — see that file's comment).
  const navChrome = c.selected && (
    <div className="h-10 px-3 flex items-center justify-between border-b border-[var(--neutral-800)]">
      <span className="text-sm font-semibold text-[var(--accent)]">
        {SUB_TABS.find((t) => t.id === c.subTab)?.label}
      </span>
      <NotebooksGeneratePicker workspaceId={c.selected.id} generateNotebooks={c.generateNotebooks} onNavigateSubTab={c.setSubTab} />
    </div>
  );

  return c.renderRoot({ notebookPicker, iconRail, promoteControl, navChrome });
}
