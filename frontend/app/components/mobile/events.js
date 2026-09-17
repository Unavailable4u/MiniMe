// Cross-component signal names for mobile/* structural forks that need
// to talk to each other without prop-drilling through every tab file in
// between — same reasoning useDensity.js/useViewport.js already give
// for their own module-level custom events (see either file's header
// comment). Kept in one file so the string literal is never typed twice
// and can't drift between the dispatcher and the listener.
//
// OPEN_WORKING_PANEL_EVENT: dispatched by mobile/AppShell.jsx's header
// button when the person taps the working-panel icon. Listened for by
// WorkspaceChatPanel.jsx, but only its one non-`stacked` instance (the
// standalone Chat tab's dock) — see that file's own comment at the
// listener for why the six `stacked` domain-tab docks intentionally
// ignore this for now.
export const OPEN_WORKING_PANEL_EVENT = "minime-mobile-open-working-panel";

// OPEN_TAB_SIDEBAR_EVENT: generalizes the same "hamburger opens a
// drawer" idea to every tab that has its own picker column (Notebooks'
// notebook list, Research's project list, etc.) instead of just Chat's
// chat list. mobile/AppShell.jsx's hamburger button already has a
// dedicated onOpenSidebar prop for the Chat tab (wired straight to
// MobileChatSidebar's open state in AppShell.jsx, no event needed there
// since AppShell.jsx owns that state itself); for every other tab,
// AppShell.jsx doesn't own — and shouldn't have to duplicate — that
// tab's own picker state, so it dispatches this instead and the tab
// body listens for it and opens its own MobileDrawer. Payload:
// { tabId } so a listener can ignore events meant for a different tab
// if more than one ever mounts at once (NotebooksTab.jsx stays mounted
// once visited, same as every other tab — see AppShell.jsx's
// visitedTabs comment).
export const OPEN_TAB_SIDEBAR_EVENT = "minime-mobile-open-tab-sidebar";
