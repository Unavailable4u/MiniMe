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
- Research, Plan, Test, Growth, Build: **not started.**

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

## Phase 7 — Tablet
**Not started.**

## Phase 8 — Regression + real devices
**Not started as a formal pass.** Chat and Notebooks have each been
manually phone-tested once, but both depend on `WorkingPanel` and will
depend on `ResponsiveSheet` once Phase 4 lands — re-test both after
those land, not just newly-converted tabs.
