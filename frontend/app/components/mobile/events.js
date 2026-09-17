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
