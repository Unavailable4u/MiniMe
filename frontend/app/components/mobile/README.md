# components/mobile/

Structural forks for `data-viewport="mobile"`, cloned from the pattern in
`../../hooks/useViewport.js`. Two kinds of viewport difference exist in this
app, and only one of them belongs in this folder:

- **Cosmetic** — spacing, font size, hiding a column. Handled with Tailwind's
  `sm:`/`md:` prefixes or a `--viewport-*` CSS variable, in the *same* file as
  the desktop version. No component in this folder.
- **Structural** — a genuinely different navigation model or component tree
  (different props, different children, not just different classes). That's
  what lives here.

## Convention

Mirror the real tree under `app/components/`: `mobile/AppShell.jsx` is the
mobile counterpart of `../AppShell.jsx`, `mobile/ChatSidebar.jsx` of
`../ChatSidebar.jsx`, and so on. Same filename, same relative path — makes the
desktop/mobile counterpart of anything trivial to find.

**Business logic (contexts, hooks, data fetching) stays shared. Only
presentation forks.** A file in here should be a thin wrapper around the
desktop component (or its own small, presentation-only component) — never a
copy-pasted duplicate of the desktop component's actual logic. If a mobile
fork starts accumulating its own state/fetching that isn't just "where do I
put this on screen," that's a sign the shared logic needs to move up a level
(a hook, a context) rather than being forked twice.

## What's here so far (Phase 1 — the shell)

- `MobileDrawer.jsx` — generic slide-in overlay (backdrop, escape-to-close,
  scroll-lock). Shared by the two drawers below rather than each rolling
  their own.
- `ChatSidebar.jsx` — wraps the desktop `ChatSidebar` in a left-side
  `MobileDrawer`. All list/search/batch/project logic still lives in the
  desktop file, untouched.
- `WorkingPanelDrawer.jsx` — wraps whatever `WorkingPanel` content it's given
  in a right-side `MobileDrawer`. Only used by the standalone Chat tab's dock
  so far (`WorkspaceChatPanel.jsx`'s non-`stacked` instance) — the six
  `stacked` domain-tab docks are Phase 5/6 work.
- `AppShell.jsx` — the Grok-style header (hamburger, scrollable tab strip,
  panel toggle) that replaces `../AppShell.jsx`'s desktop `<header>`.
- `events.js` — the one cross-component signal (`OPEN_WORKING_PANEL_EVENT`)
  needed so the header button and `WorkspaceChatPanel.jsx` can talk without
  prop-drilling through every tab in between.

**Current status of everything past Phase 1 — including what's done out of
order, what's inconsistent with the rule above, and known gaps — is tracked
in `/MOBILE_PLAN.md` at the repo root, not here.** Update that file, not
this list, when a phase's status changes; this section only describes
Phase 1's own contents and shouldn't be re-purposed as a running tracker
again (it already went stale once).
