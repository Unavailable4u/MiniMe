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

**Also retrofitted, as part of Test's Phase 5 pass:** `CreateWorkspaceModal`
(reached from every stage tab's "new project" button, so this one was
always going to come up). Same visual box on desktop (`max-w-xs` is the
same 20rem as the old `w-80`; only the backdrop moved from `black/50` to
`ResponsiveSheet`'s `black/60`, same as `ConfirmDialog`/
`ManageWorkspaceModal` already did), bottom sheet on mobile. One trap
worth knowing about if you retrofit another modal whose `onClose` takes
an argument: `ResponsiveSheet` hands its dismiss handler straight to a
backdrop `onClick`, so passing the caller's `onClose` through directly
leaks the click *event* in as that argument — `if (created)` is then
truthy and `created.id` is `undefined`. `CreateWorkspaceModal` wraps it
(`dismiss`) for that reason.

**Not yet retrofitted:** `ManageBatchModal`,
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
- **Test: done, following the Notebooks/Research/Plan reference pattern —
  plus fixes for things Test specifically needed.**
  `components/tabs/TestTab.jsx` is now a `useTestTabController` hook plus
  a thin router (`TestTab` calls the hook once, renders either
  `TestTabDesktop` or `components/mobile/TestTab.jsx` off
  `controller.viewport`), same split as the other three. The mobile file
  supplies the things that differ by viewport — project list as a
  `MobileDrawer`, vertical icon-only sub-tab rail (five tabs, with
  `overflow-y-auto` like Plan's so a landscape phone can't clip the last
  one), one-line active-sub-tab header, single combo `<select>` for
  promote — everything else (`projectRows`, `subTabContent`,
  `dockAndModals`, `renderRoot`) stays shared in the controller. Chrome
  geometry (rail width, button size, header heights) is deliberately
  identical to Plan's so switching tabs doesn't shift anything.
  - **`TABS_WITH_OWN_MOBILE_SIDEBAR` + `sidebarLabel`: `"test"` added in
    the same change** (`AppShell.jsx`), per Research's write-up above.
  - **The chat dock starts collapsed on mobile — this is the one that
    mattered.** Test defaults its dock to *open* (design spec §1.3: the
    live persona branches are the tab's main value), which is right
    beside the sub-tabs on desktop but is a full-screen
    `lg:hidden fixed inset-0` overlay on a phone — so the first visit to
    the tab showed nothing but a chat panel. Fixes: (1) the initial state
    is read off `<html data-viewport>` in a lazy `useState` initializer
    rather than `useViewport()`, because that hook starts at `"desktop"`
    on every first render for hydration safety, which would flash the
    overlay for a frame (safe here because this tab is never in the
    server-rendered HTML — `AppShell`'s `visitedTabs` starts as
    `{"chat"}`); (2) mobile never restores or writes
    `minime_test_chatdock_collapsed` — that key stays the *desktop*
    preference, so a phone session can't flip what the desktop layout
    restores. Tablet is untouched (still uses the persisted key).
  - **Two "open chat" paths, not one.** `openInDock` keeps the mobile
    guard Plan/Research/Notebooks have (drawer chat rows: don't slam the
    overlay on browse). New `revealChatInDock` always expands, and is
    what `RunSimulationPanel`'s "Run simulation" and `HistoryPanel`'s
    "Open chat" use — with only the guarded one, tapping "Run
    simulation" on a phone would dispatch the run and then show nothing.
    "+ new chat" in the drawer also closes the drawer (it sits at z-50,
    above the z-40 chat overlay it just opened); creating a project from
    the drawer closes it too.
  - **Chat overlay follows the keyboard-aware viewport.** It was
    `fixed inset-0` (layout viewport — doesn't shrink for the iOS
    keyboard, so the composer ended up underneath it); it now uses the
    shell's own `app-shell-viewport` class. Also: the hidden desktop dock
    instance (`hidden lg:flex`, display:none below `lg`) is no longer
    mounted at all on mobile — it was a second full `WorkspaceChatPanel`
    mounted for nothing. Floating chat bubble pads for the home
    indicator; the content pane gets `pb-20` on mobile so the last card
    can scroll clear of it.
  - **Sub-panels.** All five take an `isMobile` prop (Research's
    `SourcesPanel` convention) for the touch-only bits: 16px form
    controls (iOS Safari zooms the page on focus of anything under
    16px — `RunSimulationPanel`'s select/textarea, `PersonasPanel`'s
    brief editor, the promote select, the rename input,
    `CreateWorkspaceModal`'s name field), 40–44px hit areas on
    icon-only buttons, full-width Run button, and mobile-accurate copy
    (the footer said "dock on the right"). The two *layout* bugs —
    `PersonasPanel`'s and `HistoryPanel`'s card headers, which put the
    label and an action cluster in one non-wrapping row and crushed the
    label into a sliver — are fixed with `flex-wrap` rather than an
    `isMobile` branch, so they're container-driven: a desktop with the
    560px dock open has the same ~240px pane, and gets the fix too. Wide
    panes look exactly as before. Rendered markdown gets
    `min-w-0 break-words` so a long URL can't push the pane sideways.
  - **Actionable empty state** (`emptyState` slot, new vs. the other
    three tabs, which reuse desktop's one-line "Pick or create a
    project"): on a phone that sentence doesn't say the list is behind
    the hamburger.
  - **Same touch-usability fix Plan/Research made**
    (`opacity-0 group-hover:opacity-100` / `hidden group-hover:flex`
    never reveal on touch) applied to Test's project/chat rows. Still
    present, un-fixed, in GrowthTab/BuildTab/NotebooksTab/ChatSidebar.
  - **Copy fix:** the empty project list said "promote a built feature
    from the Tasks tab" — that tab's label is "Build" now.
  - **Worth carrying back to Plan/Research/Notebooks:** their
    `promoteControl` `<select>` is still `text-xs`, so tapping it zooms
    the page on iOS; Test's is `text-base`. One-word change in each.
  - **Verified:** `next lint` and `next build` clean (no warnings in
    touched files); every new Tailwind class confirmed present in the
    built CSS; and a jsdom harness mounting the real `TestTab` +
    mobile shell + `MobileDrawer` + `ResponsiveSheet` against mocked
    contexts at 390/900/1280px (first paint, drawer open/close via the
    real event, dock open/close/persistence, Run/History/Personas/
    Reports/promote/create flows, desktop unchanged) — including
    mutation checks that reintroduced three of the bugs above and
    confirmed the harness catches each. **Not verified: pixel layout.**
    jsdom has no layout engine, so nothing above proves the cards
    actually *fit* at 360px, or how the sheet behaves with the iOS
    keyboard up — same "confirm on real hardware" caveat as every other
    entry, and the two things most worth a tap-through are the
    Personas/History cards and the create-project sheet.
- **Sidebar/drawer row actions — cross-cutting pass across all six stage
  tabs + the Chat sidebar (not a per-tab mobile fork).** Closes the
  "`opacity-0 group-hover:opacity-100` … still present, un-fixed, in
  GrowthTab/BuildTab/NotebooksTab/ChatSidebar" items called out in the
  Research, Plan and Test entries above, and adds the one thing three of
  the tabs were missing entirely. Four changes:
  - **Hover-reveal → shared CSS classes** (`globals.css`: `row-reveal`,
    `touch-target`, `touch-row`, `touch-input`). Deliberately CSS keyed to
    touch input (`hover: none` / `pointer: coarse`) *and*
    `data-viewport="mobile"`, not `isMobile` branches: a landscape tablet
    is touch-only at a desktop width (the old `isMobile ? "opacity-100" :
    "opacity-0 group-hover:opacity-100"` never revealed there), and
    Build/Growth have no mobile fork to branch in. Mouse-driven desktop is
    unchanged. `touch-target` = 40px hit area + 18px glyph (TestTab's
    drawer header already used 40px; Plan/Research/Notebooks' were bare
    `p-1`, ~24px, now 40px too). `touch-input` = 16px, for the same iOS
    focus-zoom reason as Test's rename field.
  - **One shared per-row "⋮" menu** (`components/RowMenu.jsx`), replacing
    the Pencil+Trash2 pair in every stage tab's nested chat rows and the
    Chat sidebar's private copy of the menu. Outside-tap closes on
    `pointerdown`, not `mousedown` (iOS Safari doesn't send mouse events
    to `document` for taps on non-interactive elements, so the old menu
    could stay stuck open), and the panel flips upward when opening down
    would clip it inside the scrolling list.
  - **Project management added to Research, Build, Test and Growth**
    (the "⋮" → `ManageWorkspaceModal` entry point Notebooks/Plan/Chat
    already had). Deleting the selected project is safe in Research/
    Build/Test (existing `stillExists` recovery). **Growth needed a real
    fix to make that true:** it rendered off the raw `selectedWsId`, which
    outlives a deleted workspace and 404s every view keyed on it; it now
    renders off `liveWsId` (null once the workspace is gone).
  - **Still not done:** Build and Growth have no mobile fork at all —
    on a phone they still show the fixed-width project column, not a
    drawer, and are not in `TABS_WITH_OWN_MOBILE_SIDEBAR`. The touch fixes
    above make that column usable; they don't replace the Phase 5 work.
    Also unchanged: `SourceRow`/`SourceGroup` in NotebooksTab's main pane
    still use hover-only rename/delete buttons.
  - **Verified:** `next lint` (no new warnings vs. baseline) and `next
    build` clean; the new CSS confirmed present in the built stylesheet;
    `RowMenu` exercised in jsdom (open/close, outside-pointerdown, Escape,
    click isolation from the clickable parent row, single-open, flip-up)
    with mutation checks that reintroduced three of those bugs and
    confirmed each is caught. **Not verified:** any tab's full render
    (the manage modal from a real drawer, Growth's delete-then-empty-state
    path) and pixel layout — jsdom has no layout engine, so nothing above
    proves the 40px rows actually fit at 360px. Same "confirm on real
    hardware" caveat as every other entry; the two worth a tap-through are
    the Chat drawer's last-row menu (flip-up) and Growth's delete flow.
- Growth, Build: **mobile fork not started** (see the row-actions pass
  above for what did ship for them).

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
