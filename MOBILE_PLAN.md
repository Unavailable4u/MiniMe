# Mobile/tablet rollout — status

Running log of where the mobile/tablet pass actually stands, kept in one
place so "is X done" is one glance instead of grepping the whole
codebase or asking whoever last touched it. See
`frontend/app/components/mobile/README.md` for the fork *convention*
(cosmetic vs. structural, file-naming); this doc is the phase-by-phase
*status* against that convention.

Convention: update this file in the same commit/branch that changes a
phase's status — before moving on to the next phase, not at the end of
the whole rollout.

## Phase 1 — Shell
**Done.** `mobile/AppShell.jsx`, `mobile/ChatSidebar.jsx`,
`mobile/MobileDrawer.jsx`, `mobile/WorkingPanelDrawer.jsx`,
`mobile/events.js`. See that README for detail.

## Phase 2 — Chat
**Done, tested on real iOS/Android.** `WorkspaceChatPanel.jsx` and
`MessageBubble.jsx` handle the mobile composer/keyboard-inset/
scroll-to-bottom quirks inline (2 `viewport === "mobile"` branches —
small enough that a dedicated `mobile/WorkspaceChatPanel.jsx` fork
wasn't justified; revisit if that branch count grows). The drawer
piece is properly forked into `mobile/WorkingPanelDrawer.jsx`.

## Phase 3 — Cheap wins
**Done.** SettingsTab, AuditLogTab, LocalWorkspaceTab. Applied the
Phase 5 fork rule (structural differences get a JS branch; cosmetic
ones don't) and confirmed it scales down, not just up:

- `SettingsTab.jsx`, `AuditLogTab.jsx` — both already a single
  vertical column with no sidebar/columns to hide, so per
  `useViewport.js`'s own cosmetic-vs-structural split these needed no
  branch and no `components/mobile/` fork. Cosmetic-only: swapped
  hard-coded padding for `--viewport-content-padding` and gave every
  tappable control a `--viewport-touch-target` min-height. First real
  consumers of those tokens — Phase 0 defined them in `globals.css`
  but nothing used them until now, so this also validates the token
  plumbing itself, not just the fork rule.
- `LocalWorkspaceTab.jsx` — the one genuine structural case in this
  batch: its file-tree/preview pane are side-by-side on desktop, which
  doesn't fit a phone width. Didn't get a `components/mobile/` fork
  though — one `isMobile` branch (from `useViewport()`) toggling
  master/detail (tree full-width, tap a file to swap to a full-width
  preview with a Back button) in the same file, same "small enough to
  stay inline, revisit if it grows" call already made for
  `WorkspaceChatPanel.jsx`'s composer in Phase 2. No controller-hook
  split — at one branch there's nothing shared to extract yet.

Net: the rule holds at small scale without forcing every tab into
`components/mobile/` just because the folder exists. Same three
checks (real structural difference? branch count? shared-logic
extraction actually needed?) should gate Research/Plan/Test/Growth/
Build in Phase 5 too, not just "big tab -> automatic fork."

## Phase 4 — Modals (`ResponsiveSheet`)
**Primitive built; 2 of ~8 modals retrofitted.**
`components/mobile/ResponsiveSheet.jsx` exists: centered dialog on
desktop (unchanged visual box, now via `className`/`maxWidth`/`style`
props instead of each caller's own wrapper divs), full-screen bottom
sheet on mobile, viewport-aware internally (calls `useViewport()`
itself — callers don't branch). Reuses the same escape/scroll-lock/
backdrop-click handling as `mobile/MobileDrawer.jsx`.

Retrofitted (the two identified as the known gap below):
- `ConfirmDialog.jsx` — fully retrofitted, same visual box.
- `ManageWorkspaceModal.jsx` — both its main dialog and its inline
  "Leave project" sub-dialog retrofitted. Its four nested
  `ConfirmDialog` uses got mobile behavior automatically, no changes
  needed there.

**Not yet retrofitted:** `CreateWorkspaceModal`, `ManageBatchModal`,
`AttachChatToWorkspaceModal`/`AddChatToWorkspaceModal`,
`RolePickerOverlay`. Route each through `ResponsiveSheet` the same way
as it comes up in Phase 3/5 — see `ResponsiveSheet.jsx`'s own header
comment for the `className`/`maxWidth`/`style` contract.

**Resolved:** the "Notebooks already renders 2 un-adapted modals"
known gap from before this phase — both are retrofitted now. Still
worth a real-phone check once you're testing this phase's build, same
as any UI change.

## Phase 5 — Big tabs
Planned order: Research → Plan → Test → Growth → Build → Notebooks.

- **NotebooksTab: done, tested on real iOS/Android — but built out of
  order and out of convention.** Currently ~10 `isMobile` branches
  inlined directly in `components/tabs/NotebooksTab.jsx` (grew the file
  from 3,479 to 3,782 lines), instead of a dedicated
  `components/mobile/NotebooksTab.jsx` per the README's own
  cosmetic/structural rule.
  **Decision (locked in):** retrofit this into a real
  `mobile/NotebooksTab.jsx` fork rather than keep the inline branches.
  **Correction: this line was stale — the retrofit is actually done.**
  `components/tabs/NotebooksTab.jsx` is now a `useNotebooksTabController`
  hook plus a router (`NotebooksTab` calls the hook once and renders
  either `NotebooksTabDesktop` or `components/mobile/NotebooksTab.jsx`
  off `controller.viewport`); the mobile file takes the built
  `controller` as a prop rather than calling the hook itself, since the
  hook owns effects that must only run once per render. Found this
  while working Phase 3 (step 5) below, since it's the reference
  pattern that phase's fork-rule decisions lean on — fixing the status
  here rather than leaving it to whoever reads this next. This is also
  the reference pattern for Research/Plan/Test/Growth/Build, since
  Notebooks' layout is close to identical to the rest.
- **Research: done, following the Notebooks reference pattern — plus one
  cross-cutting bug found and fixed while finishing it.**
  `components/tabs/ResearchTab.jsx` is now a `useResearchTabController`
  hook plus a thin router (`ResearchTab` calls the hook once, renders
  either `ResearchTabDesktop` or `components/mobile/ResearchTab.jsx` off
  `controller.viewport`), same split as Notebooks. The mobile file
  supplies the four things that actually differ by viewport — project
  list as a `MobileDrawer` instead of a persistent column, a vertical
  icon-only sub-tab rail (Research has the same five-tab count Notebooks
  does), a one-line active-sub-tab header replacing the desktop pill
  row, and a single combo `<select>` collapsing promote-target+mode into
  one control — everything else (`projectRows`, `subTabContent`,
  `dockAndModals`, `renderRoot`) stays shared, unforked, in the
  controller.
  - **Bug found and fixed: the drawer was built but unreachable.**
    Research's project-picker drawer, its `mobileProjectsDrawerOpen`
    state, and its `OPEN_TAB_SIDEBAR_EVENT` listener were all wired up
    correctly — but `../AppShell.jsx`'s own `TABS_WITH_OWN_MOBILE_SIDEBAR`
    set (which decides whether the mobile header even shows a hamburger
    for the active tab) only ever listed `["chat", "notebooks"]`.
    Research was never added, so the hamburger silently didn't render
    on this tab at all — the drawer itself was never broken, just
    unopenable. Fixed by adding `"research"` to that set and extending
    `sidebarLabel` to show "Research projects" (matching the drawer's
    own header text, same convention `"Notebooks"` already follows).
    **Worth checking the same set for Build's project list once that
    gets its own mobile pass** — it's the other tab called out in that
    set's own header comment as "the obvious next one," so it's worth
    confirming as a deliberate addition rather than the same oversight
    repeating.
  - **Sources sub-tab toolbar merged for mobile, same idea as the chat
    composer's own compact mode.** The desktop row (a labeled scope
    `<select>`, a free-text query input, a worded "Search" button, and a
    separate Refresh icon) is four separately-bordered controls
    competing for a phone's width — the same problem
    `WorkspaceChatPanel`'s composer had with attach/mode/textarea/Send,
    solved the same way: every control now lives inside ONE bordered box
    as icon-only buttons around a borderless input (scope becomes an
    icon trigger + dropdown, same pattern as the composer's mode picker;
    Search and Refresh both stay as icon buttons rather than one folding
    away, since both get tapped often here — see the dockLoading
    auto-refresh effect in `SourcesPanel`). Desktop's row is untouched.
    Gated on the controller's own `isMobile`, not a new viewport check.
  - **Two small touch-usability fixes made in the same pass, since they
    sit in the same section:** each source card's delete button used
    `opacity-0 group-hover:opacity-100`, which never reveals on a touch
    screen (there's no hover state to trigger it) — always visible on
    mobile now, desktop keeps the original hover-to-reveal. Source
    titles now wrap (`flex-1 min-w-0 break-words`) instead of risking
    overflow next to a card's tags/delete button on a narrow screen.
    **Same `opacity-0 group-hover:opacity-100` pattern exists elsewhere**
    (GrowthTab, TestTab, BuildTab, PlanTab, NotebooksTab, ChatSidebar) —
    left alone here since those tabs haven't had their own mobile pass
    yet; worth applying the same fix when each of them does, not before.
  - Not tested on a real phone yet — same "confirm on real hardware"
    caveat as everything else in this pass; the drawer fix in particular
    is worth a tap-through since it was invisible on mobile emulation
    too (a missing hamburger button doesn't throw, it just isn't there).
- **Plan: done, following the Notebooks/Research reference pattern.**
  `components/tabs/PlanTab.jsx` is now a `usePlanTabController` hook plus
  a thin router (`PlanTab` calls the hook once, renders either
  `PlanTabDesktop` or `components/mobile/PlanTab.jsx` off
  `controller.viewport`), same split as Notebooks/Research. The mobile
  file supplies the four things that actually differ by viewport —
  project list as a `MobileDrawer` instead of a persistent column
  (rename/delete kebab and nested-chat rows inside `projectRows` carry
  over unchanged, since that markup was never desktop-only to begin
  with), a vertical icon-only sub-tab rail, a one-line active-sub-tab
  header replacing the desktop pill row, and a single combo `<select>`
  collapsing promote-target+mode into one control — everything else
  (`projectRows`, `subTabContent`, `dockAndModals`, `renderRoot`) stays
  shared, unforked, in the controller.
  - **Seven sub-tabs, not five.** Plan's `SUB_TABS` (PRD, Architecture,
    Schema, API Contract, Devil's Advocate, Feasibility, Blueprint) is
    two tabs longer than Notebooks/Research's own five-tab rail, so
    `iconRail` in `mobile/PlanTab.jsx` adds `overflow-y-auto` that
    Research's rail didn't need — otherwise a short/landscape phone
    viewport can run out of vertical room before the last icon
    (Blueprint) and silently clip it instead of scrolling to it.
  - **`TABS_WITH_OWN_MOBILE_SIDEBAR` fix applied proactively this time.**
    Research's own write-up above flagged a real bug: its drawer was
    fully wired up but unreachable because `AppShell.jsx` never added
    `"research"` to that set. Added `"plan"` (and the matching
    `sidebarLabel` case, "Plan projects") in the same commit as the
    drawer itself, rather than shipping the tab first and discovering
    the same gap later.
  - **Mobile chat-dock auto-open fix shipped as part of this pass, not
    bolted on after.** Notebooks and Research each had (or picked up)
    the same bug: selecting a chat from the project drawer called
    `openInDock`, which unconditionally expanded the chat dock if it
    was collapsed — on mobile that dock is a full-screen overlay
    (`lg:hidden fixed inset-0` in `dockAndModals`), so picking a chat
    slammed that overlay over the whole screen instantly, with no way
    to just browse the drawer without being dropped into a chat. Plan's
    `openInDock` was written with the `!isMobile &&` guard from the
    start (`if (!isMobile && chatDockCollapsed) toggleChatDock()`) —
    desktop keeps the original auto-expand behavior; mobile leaves the
    decision to the person, who can still reach the chat via the
    floating "Open chat" bubble.
  - **Touch-usability fix (opacity-0-until-hover) also built in from the
    start, not deferred.** The Phase 5 entry above for Research flagged
    this same `opacity-0 group-hover:opacity-100` pattern as present in
    `PlanTab` (among others) and left alone until each tab got its own
    mobile pass. Plan's pass is that pass: every hover-only affordance
    in `projectRows`/chat rows (the "+" new-chat button, the rename/
    delete kebab, per-chat rename/delete) is now `isMobile ? "opacity-100"
    : "opacity-0 group-hover:opacity-100"`, same fix Research already
    applied to its own Sources cards. Still present, un-fixed, in
    GrowthTab/TestTab/BuildTab/NotebooksTab/ChatSidebar — apply the same
    fix when each of those gets its own mobile pass, not before.
  - Not tested on a real phone yet — same caveat as Research's own
    entry above.
- Test, Growth, Build: **not started.**

## Phase 6 — Graphs/canvases
**Ahead of schedule for the two tabs converted so far.**
`TouchCollapsibleGraph.jsx` (generic tap-to-expand wrapper for touch
devices, pass-through on mouse/trackpad) is built and wired into
`WorkingPanel.jsx` around `RoutingTraceGraph` and `DependencyGraph`,
and into `NotebooksTab.jsx`'s `BacklinksView` around
`KnowledgeGraphView` — all three are react-force-graph-2d/ReactFlow
canvases that bind their own pointer events for pan/drag, the actual
conflict this wrapper exists to solve. Since `WorkingPanel` is the
shared dock embedded via `WorkspaceChatPanel`, its wrapping already
benefits every tab that docks it, not just Chat.

**Bug fix (post-Phase-4-write-up):** the wrapper's collapse gate was
`isTouch` alone (`pointer: coarse`), so it never collapsed when
"mobile" was being checked the way this app's own tooling checks
it — `useViewport.js`'s `?forceViewport=mobile` override, or a
desktop browser window narrowed/put into devtools responsive mode
without a touch-emulating device preset. Both keep the pointer "fine"
while `data-viewport` flips to "mobile", so the graphs stayed fully
expanded and looked like the wrap silently hadn't applied. Gate is
now `isTouch || viewport === "mobile"` — a real touch device still
collapses regardless of width (the wide-landscape-tablet case
`isTouch` alone existed for), and so does anything the app itself
calls "mobile", which is the more relevant bar day to day than a
physical-input check a resized window will never satisfy.

**Correction, not a gap:** the previous version of this file listed
`MermaidDiagram` (both in `WorkingPanel.jsx` and `NotebooksTab.jsx`)
as unwrapped and assumed it had the same conflict — that assumption
was wrong. `MermaidDiagram.jsx`'s own header comment says panning is
the browser's native scroll-container scrollbars, not a pointer-bound
canvas drag (zoom is separate tap buttons); a native scroll container
doesn't fight touch-scrolling the way a canvas library's own pointer
handling does. Left unwrapped on purpose — wrapping it would have been
a UX regression (forcing tap-to-expand on something that already
worked inline). Worth confirming on a real phone all the same, same as
anything else in this pass.

**Still not touched:** `ForceGraphBase`, `WiringGraph`, `MechView` —
none of these are used by Chat or Notebooks (the two tabs converted so
far), so they're correctly out of scope until Research/Plan/Test/
Growth/Build come up in Phase 5.

**Now in scope, not yet done:** Plan's Phase 5 pass landed above, and
its Blueprint sub-tab (`BlueprintView` in `tabs/PlanTab.jsx`) renders
`WiringGraph` (a `ForceGraphBase` wrapper, same pan/zoom pointer-binding
conflict `KnowledgeGraphView` has) and `MechView` (not yet inspected for
its own pointer/touch conflict — likely a `@react-three/fiber` scene,
which has the analogous problem with orbit-drag). Follow the same
pattern as `NotebooksTab.jsx`'s own Phase-6 gap close once this is
picked up: wrap the `WiringGraph`/`MechView` render sites in
`BlueprintView` with `TouchCollapsibleGraph` (`label="wiring graph"` /
`label="mechanical view"`), gated the same `isTouch || viewport ===
"mobile"` way. Deliberately not bundled into Plan's own Phase 5 pass
above — that pass was scoped to layout/structure parity (drawer, sub-tab
rail, promote control, the chat-dock-auto-open fix), not the
graph-canvas touch conflict, which is Phase 6's own concern.

## Phase 7 — Tablet
**Not started.**

## Phase 8 — Regression + real devices
**Not started as a formal pass.** Chat and Notebooks have each been
manually phone-tested once, but both depend on `WorkingPanel` and will
depend on `ResponsiveSheet` once Phase 4 lands — re-test both after
those land, not just newly-converted tabs.
