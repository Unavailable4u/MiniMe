"use client";
import { Plus, X, Loader2, ArrowUpRight } from "lucide-react";
import MobileDrawer from "./MobileDrawer";
import { STAGE_THEME } from "../WorkspaceStageIcons";
import { SUB_TABS, PROMOTE_LABELS } from "../tabs/ResearchTab";

// Mobile counterpart of ../tabs/ResearchTab.jsx — built the way the
// README's cosmetic/structural rule says (Phase 5 step 6.1), not as
// inline `isMobile` branches the way NotebooksTab.jsx originally shipped
// before its own retrofit. Per that rule: business logic stays shared —
// the controller hook (`useResearchTabController`, exported from the
// desktop file) owns every bit of state, every effect, every handler,
// and even `subTabContent`/`dockAndModals`/`renderRoot` (the parts that
// were already identical on every viewport). This file only supplies
// the four things that genuinely differ from the desktop shell, same
// split as mobile/NotebooksTab.jsx:
//   - the project list as a left-side drawer instead of a persistent
//     column (opened by the header hamburger — OPEN_TAB_SIDEBAR_EVENT,
//     handled inside the controller — or the "New research project"
//     button below)
//   - a vertical icon-only sub-tab rail instead of the desktop pill row,
//     since five labeled buttons don't fit a phone width (Research has
//     the same five-tab count Notebooks did)
//   - a one-line "which sub-tab is active" header, replacing the
//     desktop pill row's own nav — Research has no generate-picker
//     widget to carry over the way Notebooks' navChrome does
//   - a single combo <select> for promote-target+mode instead of three
//     separate desktop controls
//
// Takes `controller` (the already-built return value of
// useResearchTabController) as a prop rather than calling the hook
// itself — see the router at the bottom of ../tabs/ResearchTab.jsx for
// why: the hook owns effects (data fetching, localStorage sync, the
// OPEN_TAB_SIDEBAR_EVENT listener) that must only ever run once per
// render, so only the router calls it.
export default function ResearchTab({ controller: c }) {
  const projectPicker = (
    <MobileDrawer side="left" open={c.mobileProjectsDrawerOpen} onClose={() => c.setMobileProjectsDrawerOpen(false)}>
      <div className="w-72 max-w-[80vw] flex flex-col h-full">
        <div className="h-12 px-3 flex items-center justify-between border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <STAGE_THEME.research.Icon size={13} className={STAGE_THEME.research.color} /> Research projects
          </span>
          <div className="flex items-center">
            <button onClick={() => c.setShowCreateModal(true)} title="New research project" aria-label="New research project" className="flex items-center justify-center w-10 h-10 text-[var(--neutral-400)] hover:text-[var(--neutral-100)]">
              <Plus size={18} />
            </button>
            <button onClick={() => c.setMobileProjectsDrawerOpen(false)} title="Close" aria-label="Close project list" className="flex items-center justify-center w-10 h-10 -mr-2 text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
              <X size={18} />
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto">{c.projectRows}</div>
      </div>
    </MobileDrawer>
  );

  // Only shown once a project is selected — before that there's no
  // sub-tab content to switch between. Reuses the w-10/w-12 slot the
  // desktop-only project-picker column occupies on wide screens; on
  // mobile that column never renders (see projectPicker above, always a
  // drawer), so this is the replacement for it, not an addition
  // alongside it. Same positioning as mobile/NotebooksTab.jsx's own
  // iconRail — no badge counts here since Research's sub-tabs don't
  // carry an unread/pending-count concept the way Notebooks' do.
  const iconRail = c.activeWs && (
    <div className="w-12 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-2 gap-1">
      {SUB_TABS.map((t) => (
        <button
          key={t.id}
          onClick={() => c.setSubTab(t.id)}
          title={t.label}
          className={`flex items-center justify-center w-9 h-9 rounded-lg ${
            c.subTab === t.id ? "bg-[var(--accent)] text-[var(--accent-text)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
          }`}
        >
          <t.icon size={17} />
        </button>
      ))}
    </div>
  );

  // Desktop's control is three separate widgets (target <select>,
  // complete/partial radiogroup, a fully-worded "Promote to X →"
  // button) — plenty of room on desktop, but three tap targets plus a
  // wide label don't fit next to a project name on a phone. This
  // collapses target + mode into ONE <select> (each option already
  // encodes both, e.g. "→ Plan"/"→ Plan, keep here too") and reduces
  // the action itself to a small icon-only button. Same collapsing as
  // mobile/NotebooksTab.jsx's own promoteControl.
  const promoteControl = c.promoteTargets && (
    <div className="flex items-center gap-1.5">
      <label className="sr-only" htmlFor="research-promote-combo">Promote to</label>
      <select
        id="research-promote-combo"
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
        onClick={() => c.handlePromote(c.activeWs.id, c.promoteTargets.targetStage)}
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
  // glance even though only an icon represents it in iconRail above).
  // No generate-picker slot here — Research has nothing equivalent to
  // NotebooksGeneratePicker for mobile's navChrome to carry over.
  const subTabNav = c.activeWs && (
    <div className="h-10 px-3 flex items-center border-b border-[var(--neutral-800)]">
      <span className="text-sm font-semibold text-[var(--accent)]">
        {SUB_TABS.find((t) => t.id === c.subTab)?.label}
      </span>
    </div>
  );

  return c.renderRoot({ projectPicker, iconRail, subTabNav, promoteControl });
}
