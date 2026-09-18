"use client";
import { Plus, X, Loader2, ArrowUpRight } from "lucide-react";
import MobileDrawer from "./MobileDrawer";
import { STAGE_THEME } from "../WorkspaceStageIcons";
import { SUB_TABS, PROMOTE_LABELS } from "../tabs/PlanTab";

// Mobile counterpart of ../tabs/PlanTab.jsx — built the way the
// README's cosmetic/structural rule says (Phase 5), same split as
// mobile/NotebooksTab.jsx and mobile/ResearchTab.jsx before it. Per that
// rule: business logic stays shared — the controller hook
// (`usePlanTabController`, exported from the desktop file) owns every
// bit of state, every effect, every handler, and even
// `subTabContent`/`dockAndModals`/`renderRoot` (the parts that were
// already identical on every viewport, including the rename/delete
// kebab and nested-chat rows inside `projectRows` — those are plain
// markup, not desktop-only, so they carry over unchanged). This file
// only supplies the four things that genuinely differ from the desktop
// shell, same split as Research/Notebooks:
//   - the project list as a left-side drawer instead of a persistent
//     column (opened by the header hamburger — OPEN_TAB_SIDEBAR_EVENT,
//     handled inside the controller — or the "New plan project" button
//     below)
//   - a vertical icon-only sub-tab rail instead of the desktop pill row
//   - a one-line "which sub-tab is active" header, replacing the
//     desktop pill row's own nav — Plan has no generate-picker widget
//     the way Notebooks' navChrome does
//   - a single combo <select> for promote-target+mode instead of three
//     separate desktop controls
//
// Takes `controller` (the already-built return value of
// usePlanTabController) as a prop rather than calling the hook itself —
// see the router at the bottom of ../tabs/PlanTab.jsx for why: the hook
// owns effects (data fetching, localStorage sync, the Pusher
// subscription, the OPEN_TAB_SIDEBAR_EVENT listener) that must only
// ever run once per render, so only the router calls it.
export default function PlanTab({ controller: c }) {
  const projectPicker = (
    <MobileDrawer side="left" open={c.mobilePlanDrawerOpen} onClose={() => c.setMobilePlanDrawerOpen(false)}>
      <div className="w-72 max-w-[80vw] flex flex-col h-full">
        <div className="h-12 px-3 flex items-center justify-between border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <STAGE_THEME.plan.Icon size={13} className={STAGE_THEME.plan.color} /> Plan projects
          </span>
          <div className="flex items-center gap-3">
            <button onClick={() => c.setShowCreateModal(true)} title="New plan project" className="text-[var(--neutral-400)] hover:text-[var(--neutral-100)] p-1">
              <Plus size={16} />
            </button>
            <button onClick={() => c.setMobilePlanDrawerOpen(false)} title="Close" className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] p-1">
              <X size={16} />
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
  // alongside it. Same positioning as mobile/ResearchTab.jsx's/
  // mobile/NotebooksTab.jsx's own iconRail.
  // NEW — Plan has SEVEN sub-tabs (Research/Notebooks both have five),
  // so this rail is the one place that count actually matters: 7 * 36px
  // buttons plus gaps can run past a short phone's available height
  // (e.g. landscape, or a tall on-screen keyboard/notch eating into the
  // viewport). `overflow-y-auto` lets the rail itself scroll instead of
  // assuming every icon always fits, rather than silently clipping the
  // last tab (Blueprint) off the bottom on the wrong device — Research's
  // rail didn't need this at five tabs, so it doesn't have it.
  const iconRail = c.activeWs && (
    <div className="w-12 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-2 gap-1 overflow-y-auto">
      {SUB_TABS.map((t) => (
        <button
          key={t.id}
          onClick={() => c.setSubTab(t.id)}
          title={t.label}
          className={`shrink-0 flex items-center justify-center w-9 h-9 rounded-lg ${
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
  // encodes both, e.g. "→ Build"/"→ Build, keep here too") and reduces
  // the action itself to a small icon-only button. Same collapsing as
  // mobile/ResearchTab.jsx's/mobile/NotebooksTab.jsx's own promoteControl.
  const promoteControl = c.promoteTargets && (
    <div className="flex items-center gap-1.5">
      <label className="sr-only" htmlFor="plan-promote-combo">Promote to</label>
      <select
        id="plan-promote-combo"
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
  // No generate-picker slot here — Plan has nothing equivalent to
  // NotebooksGeneratePicker for mobile's own header to carry over, same
  // as Research's subTabNav.
  const subTabNav = c.activeWs && (
    <div className="h-10 px-3 flex items-center border-b border-[var(--neutral-800)]">
      <span className="text-sm font-semibold text-[var(--accent)]">
        {SUB_TABS.find((t) => t.id === c.subTab)?.label}
      </span>
    </div>
  );

  return c.renderRoot({ projectPicker, iconRail, subTabNav, promoteControl });
}
