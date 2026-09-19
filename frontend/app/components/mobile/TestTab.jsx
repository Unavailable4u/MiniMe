"use client";
import { Plus, X, Loader2, ArrowUpRight, FolderOpen } from "lucide-react";
import MobileDrawer from "./MobileDrawer";
import { STAGE_THEME } from "../WorkspaceStageIcons";
import { SUB_TABS, PROMOTE_LABELS } from "../tabs/TestTab";

// Mobile counterpart of ../tabs/TestTab.jsx — built the way the README's
// cosmetic/structural rule says (Phase 5), same split as
// mobile/PlanTab.jsx, mobile/ResearchTab.jsx and mobile/NotebooksTab.jsx
// before it. Per that rule: business logic stays shared — the controller
// hook (`useTestTabController`, exported from the desktop file) owns
// every bit of state, every effect, every handler, and even
// `projectRows`/`subTabContent`/`dockAndModals`/`renderRoot` (the parts
// that were already identical on every viewport). This file only
// supplies the five things that genuinely differ from the desktop shell:
//   - the project list as a left-side drawer instead of a persistent
//     column (opened by the header hamburger — OPEN_TAB_SIDEBAR_EVENT,
//     handled inside the controller — or the buttons in `emptyState`)
//   - a vertical icon-only sub-tab rail instead of the desktop pill row
//   - a one-line "which sub-tab is active" header, replacing the desktop
//     pill row's own nav
//   - a single combo <select> for promote-target+mode instead of three
//     separate desktop controls
//   - an actionable empty state (NEW vs. Plan/Research/Notebooks, which
//     fall back to the desktop's one-line "Pick or create a project"
//     sentence). On a phone that sentence gives no hint the project list
//     lives behind the header's hamburger, so this gives the two next
//     steps their own buttons instead.
//
// Where this deliberately differs from mobile/PlanTab.jsx (same chrome,
// same geometry, so switching tabs doesn't shift anything — only the
// bits below):
//   - iconRail gets `overflow-y-auto` like Plan's does. Test only has
//     five sub-tabs, but a landscape phone can still run out of height
//     before the fifth icon (History), and a clipped last tab is worse
//     than a rail that scrolls.
//   - the promote <select> is 16px, not text-xs. iOS Safari zooms the
//     whole page when a form control under 16px takes focus, and doesn't
//     reliably zoom back out — so tapping the select used to leave the
//     header half off-screen. (Plan/Research/Notebooks have the same
//     text-xs select; same one-word fix applies there.)
//   - the drawer pads for the notch/home indicator. layout.js sets
//     viewportFit: "cover", which is what makes env(safe-area-inset-*)
//     meaningful in the first place — in landscape, a left-anchored
//     drawer's header otherwise slides under the notch.
//
// Takes `controller` (the already-built return value of
// useTestTabController) as a prop rather than calling the hook itself —
// see the router at the bottom of ../tabs/TestTab.jsx for why: the hook
// owns effects (localStorage sync, the OPEN_TAB_SIDEBAR_EVENT listener)
// that must only ever run once per render, so only the router calls it.
export default function TestTab({ controller: c }) {
  const projectPicker = (
    <MobileDrawer side="left" open={c.mobileTestDrawerOpen} onClose={() => c.setMobileTestDrawerOpen(false)}>
      <div className="w-72 max-w-[80vw] flex flex-col h-full pt-[env(safe-area-inset-top)] pl-[env(safe-area-inset-left)]">
        <div className="h-12 shrink-0 px-3 flex items-center justify-between border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <STAGE_THEME.test.Icon size={13} className={STAGE_THEME.test.color} /> Test projects
          </span>
          {/* 40px hit areas (Plan's are bare p-1 icons, ~24px) — these two
              sit side by side in a 48px bar, so the close button is the
              one a thumb most easily mistakes for "new project". */}
          <div className="flex items-center">
            <button
              onClick={() => c.setShowCreateModal(true)}
              title="New test project"
              aria-label="New test project"
              className="flex items-center justify-center w-10 h-10 text-[var(--neutral-400)] hover:text-[var(--neutral-100)]"
            >
              <Plus size={16} />
            </button>
            <button
              onClick={() => c.setMobileTestDrawerOpen(false)}
              title="Close"
              aria-label="Close project list"
              className="flex items-center justify-center w-10 h-10 -mr-2 text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            >
              <X size={16} />
            </button>
          </div>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain pb-[env(safe-area-inset-bottom)]">{c.projectRows}</div>
      </div>
    </MobileDrawer>
  );

  // Only shown once a project is selected — before that there's no
  // sub-tab content to switch between. Reuses the w-10/w-12 slot the
  // desktop-only project-picker column occupies on wide screens; on
  // mobile that column never renders (see projectPicker above, always a
  // drawer), so this is the replacement for it, not an addition
  // alongside it. Same positioning and geometry as mobile/PlanTab.jsx's
  // own iconRail.
  const iconRail = c.activeWs && (
    <div className="w-12 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-2 gap-1 overflow-y-auto">
      {SUB_TABS.map((t) => (
        <button
          key={t.id}
          onClick={() => c.setSubTab(t.id)}
          title={t.label}
          aria-label={t.label}
          aria-current={c.subTab === t.id ? "true" : undefined}
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
  // encodes both, e.g. "→ Growth"/"→ Growth, keep here too") and
  // reduces the action itself to a small icon-only button. Same
  // collapsing as mobile/PlanTab.jsx's own promoteControl — Test only
  // ever has one target ("growth"), so in practice the select is really
  // just the complete/partial switch.
  const promoteControl = c.promoteTargets && (
    <div className="flex items-center gap-1.5">
      <label className="sr-only" htmlFor="test-promote-combo">Promote to</label>
      <select
        id="test-promote-combo"
        value={`${c.promoteTargets.targetStage}|${c.promoteMode}`}
        onChange={(e) => {
          const [stage, mode] = e.target.value.split("|");
          c.setPromoteTargetStage(stage);
          c.setPromoteMode(mode);
        }}
        disabled={c.promoting}
        className="max-w-[132px] bg-[var(--neutral-900)] border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg pl-2 pr-1 py-1 text-base outline-none disabled:opacity-50"
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
        aria-label={`${c.promoteMode === "partial" ? "Add to" : "Promote to"} ${PROMOTE_LABELS[c.promoteTargets.targetStage]}`}
        className="shrink-0 flex items-center justify-center text-[var(--neutral-200)] border border-[var(--neutral-700)] rounded-lg p-1.5 disabled:opacity-50"
      >
        {c.promoting ? <Loader2 size={14} className="animate-spin" /> : <ArrowUpRight size={14} />}
      </button>
    </div>
  );

  // Replaces the desktop pill row: a slim header naming just the active
  // sub-tab (in the app's own accent color, so it stays legible at a
  // glance even though only an icon represents it in iconRail above).
  // No generate-picker slot here — Test has nothing equivalent to
  // NotebooksGeneratePicker for mobile's own header to carry over.
  const subTabNav = c.activeWs && (
    <div className="h-10 shrink-0 px-3 flex items-center border-b border-[var(--neutral-800)]">
      <span className="text-sm font-semibold text-[var(--accent)]">
        {SUB_TABS.find((t) => t.id === c.subTab)?.label}
      </span>
    </div>
  );

  // Shown by renderRoot only when there's no active project (i.e. no
  // test-stage project exists yet — the controller auto-selects the
  // first one otherwise). Rendered as its own flex child rather than
  // inside the scroll pane so it can center vertically without leaning
  // on a percentage height inside a flex item.
  const emptyState = !c.activeWs && (
    <div className="flex-1 min-h-0 flex flex-col items-center justify-center text-center gap-4 px-8">
      <STAGE_THEME.test.Icon size={28} className={STAGE_THEME.test.color} />
      <div className="space-y-1">
        <p className="text-sm font-medium text-[var(--neutral-200)]">No test project yet</p>
        <p className="text-xs text-[var(--neutral-500)]">
          Create one to run simulations against a feature, or open the list to pick an existing project.
        </p>
      </div>
      <div className="flex flex-col w-full max-w-[16rem] gap-2">
        <button
          onClick={() => c.setShowCreateModal(true)}
          className="flex items-center justify-center gap-1.5 min-h-[var(--viewport-touch-target)] rounded-lg bg-[var(--accent)] text-[var(--accent-text)] text-sm font-medium"
        >
          <Plus size={15} /> New test project
        </button>
        <button
          onClick={() => c.setMobileTestDrawerOpen(true)}
          className="flex items-center justify-center gap-1.5 min-h-[var(--viewport-touch-target)] rounded-lg border border-[var(--neutral-700)] text-[var(--neutral-300)] text-sm"
        >
          <FolderOpen size={15} /> Open project list
        </button>
      </div>
    </div>
  );

  return c.renderRoot({ projectPicker, iconRail, subTabNav, promoteControl, emptyState });
}
