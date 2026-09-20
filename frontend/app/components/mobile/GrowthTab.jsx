"use client";
import { Plus, X, FolderOpen } from "lucide-react";
import MobileDrawer from "./MobileDrawer";
import { STAGE_THEME } from "../WorkspaceStageIcons";
import { SUB_TABS } from "../tabs/GrowthTab";

// Mobile counterpart of ../tabs/GrowthTab.jsx — built the way the
// README's cosmetic/structural rule says (Phase 5), same split as
// mobile/TestTab.jsx, mobile/PlanTab.jsx, mobile/ResearchTab.jsx and
// mobile/NotebooksTab.jsx before it. Per that rule: business logic stays
// shared — the controller hook (`useGrowthTabController`, exported from
// the desktop file) owns every bit of state, every effect, every
// handler, and even `projectRows`/`subTabContent`/`dockAndModals`/
// `renderRoot` (the parts that were already identical on every
// viewport). This file only supplies the four things that genuinely
// differ from the desktop shell:
//   - the workspace list as a left-side drawer instead of a persistent
//     column (opened by the header hamburger — OPEN_TAB_SIDEBAR_EVENT,
//     handled inside the controller — or the buttons in `emptyState`)
//   - a vertical icon-only sub-tab rail instead of the desktop pill row
//   - a two-line header: the workspace name (desktop has no equivalent —
//     its picker column is always on screen and already shows which row
//     is selected, but here the list is behind the hamburger, so without
//     this nothing on screen says which workspace you're in), then the
//     active sub-tab's name replacing the desktop pill row's own nav
//   - an actionable empty state (the desktop sentence says "on the left",
//     which a phone doesn't have) — two variants, since Growth, unlike
//     Test/Plan/Research, does NOT auto-select the first workspace:
//     "none exist yet" and "some exist, none picked" need different
//     primary actions.
// Unlike the four before it there is no promote control: Growth is the
// last stage in the pipeline, so there is nothing to promote to.
//
// Chrome geometry (rail width, button size, both header heights, drawer
// width and safe-area padding) is deliberately identical to Test's and
// Plan's so switching tabs doesn't shift anything. Where this matches
// Test rather than Plan, that's because Test is the newer of the two:
//   - iconRail has `overflow-y-auto` so a landscape phone can't clip the
//     last icon (Growth has five sub-tabs; a short viewport can still
//     run out of height before the fifth), `aria-label` and
//     `aria-current`
//   - drawer header buttons are 40px hit areas, and the drawer pads for
//     the notch/home indicator (layout.js sets viewportFit: "cover",
//     which is what makes env(safe-area-inset-*) meaningful — in
//     landscape, a left-anchored drawer's header otherwise slides under
//     the notch)
// One Growth-specific detail: Analytics isn't built yet. Desktop dims its
// pill and appends "(soon)"; an icon-only rail has no room for the word,
// so the icon is dimmed the same way and the "(soon)" moves into the
// header line, where the sub-tab's name is shown anyway.
//
// Takes `controller` (the already-built return value of
// useGrowthTabController) as a prop rather than calling the hook itself —
// see the router at the bottom of ../tabs/GrowthTab.jsx for why: the hook
// owns effects (localStorage sync, the OPEN_TAB_SIDEBAR_EVENT listener)
// that must only ever run once per render, so only the router calls it.
export default function GrowthTab({ controller: c }) {
  const projectPicker = (
    <MobileDrawer side="left" open={c.mobileGrowthDrawerOpen} onClose={() => c.setMobileGrowthDrawerOpen(false)}>
      <div className="w-72 max-w-[80vw] flex flex-col h-full pt-[env(safe-area-inset-top)] pl-[env(safe-area-inset-left)]">
        <div className="h-12 shrink-0 px-3 flex items-center justify-between border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <STAGE_THEME.growth.Icon size={13} className={STAGE_THEME.growth.color} /> Growth workspaces
          </span>
          {/* 40px hit areas — these two sit side by side in a 48px bar,
              so the close button is the one a thumb most easily mistakes
              for "new workspace". */}
          <div className="flex items-center">
            <button
              onClick={() => c.setShowCreateModal(true)}
              title="New growth workspace"
              aria-label="New growth workspace"
              className="flex items-center justify-center w-10 h-10 text-[var(--neutral-400)] hover:text-[var(--neutral-100)]"
            >
              <Plus size={16} />
            </button>
            <button
              onClick={() => c.setMobileGrowthDrawerOpen(false)}
              title="Close"
              aria-label="Close workspace list"
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

  // Only shown once a workspace is selected — before that there's no
  // sub-tab content to switch between. Reuses the w-10/w-12 slot the
  // desktop-only picker column occupies on wide screens; on mobile that
  // column never renders (see projectPicker above, always a drawer), so
  // this is the replacement for it, not an addition alongside it.
  const iconRail = c.liveWsId && (
    <div className="w-12 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-2 gap-1 overflow-y-auto">
      {SUB_TABS.map((t) => (
        <button
          key={t.id}
          onClick={() => c.setSubTab(t.id)}
          title={t.built ? t.label : `${t.label} (soon)`}
          aria-label={t.built ? t.label : `${t.label} (soon)`}
          aria-current={c.subTab === t.id ? "true" : undefined}
          className={`shrink-0 flex items-center justify-center w-9 h-9 rounded-lg ${
            c.subTab === t.id ? "bg-[var(--accent)] text-[var(--accent-text)]" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
          } ${!t.built ? "opacity-60" : ""}`}
        >
          <t.icon size={17} />
        </button>
      ))}
    </div>
  );

  // Same 40px slot Test/Plan/Research use for the project title (there
  // it also carries the promote control; here there's nothing to put on
  // the right, so the name gets the full width to truncate into).
  const workspaceHeader = (
    <div className="h-10 shrink-0 flex items-center px-3 border-b border-[var(--neutral-800)]">
      <h2 className="text-sm font-medium text-[var(--neutral-100)] truncate">{c.selectedGrowthWs?.name}</h2>
    </div>
  );

  // Replaces the desktop pill row: a slim header naming just the active
  // sub-tab (in the app's own accent color, so it stays legible at a
  // glance even though only an icon represents it in iconRail above).
  const activeSubTab = SUB_TABS.find((t) => t.id === c.subTab);
  const subTabNav = c.liveWsId && (
    <div className="h-10 shrink-0 px-3 flex items-center gap-1.5 border-b border-[var(--neutral-800)]">
      <span className="text-sm font-semibold text-[var(--accent)] truncate">{activeSubTab?.label}</span>
      {activeSubTab && !activeSubTab.built && <span className="shrink-0 text-[10px] text-[var(--neutral-500)]">(soon)</span>}
    </div>
  );

  // Shown by renderRoot only when no workspace is selected. Rendered as
  // its own flex child rather than inside the scroll pane so it can
  // center vertically without leaning on a percentage height inside a
  // flex item.
  const hasWorkspaces = c.growthWorkspaces.length > 0;
  const emptyState = !c.liveWsId && (
    <div className="flex-1 min-h-0 flex flex-col items-center justify-center text-center gap-4 px-8">
      <STAGE_THEME.growth.Icon size={28} className={STAGE_THEME.growth.color} />
      <div className="space-y-1">
        <p className="text-sm font-medium text-[var(--neutral-200)]">
          {hasWorkspaces ? "Choose a workspace" : "No growth workspace yet"}
        </p>
        <p className="text-xs text-[var(--neutral-500)]">
          {hasWorkspaces
            ? "Open the list to pick the workspace you want to work in."
            : "Create one to adapt content for each platform, keep your brand voice on file and check page quality. Projects you promote from Test land here too."}
        </p>
      </div>
      <div className="flex flex-col w-full max-w-[16rem] gap-2">
        {hasWorkspaces ? (
          <>
            <button
              onClick={() => c.setMobileGrowthDrawerOpen(true)}
              className="flex items-center justify-center gap-1.5 min-h-[var(--viewport-touch-target)] rounded-lg bg-[var(--accent)] text-[var(--accent-text)] text-sm font-medium"
            >
              <FolderOpen size={15} /> Open workspace list
            </button>
            <button
              onClick={() => c.setShowCreateModal(true)}
              className="flex items-center justify-center gap-1.5 min-h-[var(--viewport-touch-target)] rounded-lg border border-[var(--neutral-700)] text-[var(--neutral-300)] text-sm"
            >
              <Plus size={15} /> New growth workspace
            </button>
          </>
        ) : (
          <button
            onClick={() => c.setShowCreateModal(true)}
            className="flex items-center justify-center gap-1.5 min-h-[var(--viewport-touch-target)] rounded-lg bg-[var(--accent)] text-[var(--accent-text)] text-sm font-medium"
          >
            <Plus size={15} /> New growth workspace
          </button>
        )}
      </div>
    </div>
  );

  return c.renderRoot({ projectPicker, iconRail, workspaceHeader, subTabNav, emptyState });
}
