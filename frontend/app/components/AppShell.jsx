"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import dynamic from "next/dynamic";   // NEW — perf audit §2.3 step A: code-split the non-default tab bodies
import { SessionProvider, useSession } from "../context/SessionContext";
import { NotificationsProvider } from "../context/NotificationsContext";   // NEW — Item 2 concern split, slice 1: notifications/unreadCount/markNotificationsRead moved out of SessionContext
import { UsageStatsProvider, useUsageStats } from "../context/UsageStatsContext";   // NEW — Item 2 concern split, slice 2: usageStats/usageHistory/combinedUsageHistory/handleUsageEvent moved out of SessionContext
import { WorkspacesProvider, useWorkspaces } from "../context/WorkspacesContext";   // NEW — Item 2 concern split, slice 3: workspaces/fetchWorkspaces moved out of SessionContext
import { ChatListProvider, useChatList } from "../context/ChatListContext";   // NEW — Item 2 concern split, slice 4: chats/refreshChatList moved out of SessionContext
import { WorkspaceDockProvider, useWorkspaceDockActions } from "../context/WorkspaceDockContext";   // NEW — step 3d/3e-prereq: WorkspaceChatPanel calls useWorkspaceDock() unconditionally, and the lifecycle functions (switchChat etc.) now live here too, needing refreshChatList/getWorkspaceIdForChat/getChats threaded in — see WorkspaceDockBridge below. useWorkspaceDockActions is the step 3e cutover for AppShellBody's own openChat below.
import { useBootProgress } from "../context/BootProgressContext";   // NEW — loading-screen wiring: lets the bootstrap effect below report real completion to Gate.jsx's splash instead of it running on a fixed timer
import { supabase } from "../lib/supabaseClient";   // BUGFIX (401 storm): see the pre-fan-out getUser() call in the bootstrap effect below
import ChatSidebar from "./ChatSidebar";
import ChatTab from "./tabs/ChatTab";   // stays a static import — "chat" is the initial activeTab and always the first (often only) tab visited/mounted on load, so there's nothing to defer here.
import AccountMenu from "./auth/AccountMenu";      // NEW — Part 8.9: signed-in user email + sign out
import NotificationBell from "./NotificationBell";   // NEW — Part 8.9: cross-chat notification inbox
import { useViewport } from "../hooks/useViewport";   // NEW — Phase 1 (mobile shell): structural nav fork below
import MobileHeader from "./mobile/AppShell";           // NEW — Phase 1
import MobileChatSidebar from "./mobile/ChatSidebar";   // NEW — Phase 1
import { OPEN_WORKING_PANEL_EVENT, OPEN_TAB_SIDEBAR_EVENT } from "./mobile/events";   // NEW — Phase 1 / picker-drawer generalization

// NEW — perf audit §2.3 step A: the other ten tab bodies were all static
// imports, so every one of them — including 3,100+-line NotebooksTab —
// landed in the SAME initial JS chunk as ChatTab and everything else
// AppShell needs to boot, even though TABS.filter((t) =>
// visitedTabs.has(t.id)) below already means most of them are never even
// RENDERED until the user actually clicks that tab. Static imports still
// get bundled regardless of whether they're rendered; only next/dynamic
// (or React.lazy) defers the module fetch itself to first use. This
// converts every tab except the always-mounted "chat" one, cutting them
// out of the initial bundle without changing the "stays mounted once
// visited" behavior below at all — dynamic() just defers *when* the
// module is fetched, not whether the mounted-tab-stays-mounted logic
// applies to it once it lands.
//
// No `ssr: false` here: this whole component tree is already
// client-only (page.js's root "use client"), so there's no SSR/CSR
// markup mismatch to guard against the way ForceGraphBase.jsx has to
// (see that file's own comment on why IT avoids next/dynamic — a ref-
// forwarding problem that doesn't apply here, since none of these tabs
// receive a ref from AppShell).
function TabLoadingFallback() {
  // Deliberately minimal — this only shows for the one-time chunk fetch
  // the first time a given tab is opened this session (visitedTabs means
  // it's never re-fetched on subsequent switches), so it's on screen for
  // a beat, not something worth animating or building out further.
  return (
    <div className="h-full flex items-center justify-center text-xs text-[var(--neutral-500)]">
      Loading…
    </div>
  );
}

const TokenUsageTab = dynamic(() => import("./tabs/TokenUsageTab"), { loading: TabLoadingFallback });
const SettingsTab = dynamic(() => import("./tabs/SettingsTab"), { loading: TabLoadingFallback });
const RoleLibraryTab = dynamic(() => import("./tabs/RoleLibraryTab"), { loading: TabLoadingFallback });
const WorkflowTemplatesTab = dynamic(() => import("./tabs/WorkflowTemplatesTab"), { loading: TabLoadingFallback });
const NotebooksTab = dynamic(() => import("./tabs/NotebooksTab"), { loading: TabLoadingFallback });   // NEW — §4.7: dedicated Notebooks section
const ResearchTab = dynamic(() => import("./tabs/ResearchTab"), { loading: TabLoadingFallback });     // NEW — Part 3 §3.9: dedicated Research section
const PlanTab = dynamic(() => import("./tabs/PlanTab"), { loading: TabLoadingFallback });             // FIX — Part 5: was built as a file but never registered here, so it had no top-nav entry and no way to receive a promoted workspace
const BuildTab = dynamic(() => import("./tabs/BuildTab"), { loading: TabLoadingFallback });           // NEW — Part 7 §7.2: kanban board over feature_status/current_plan
const TestTab = dynamic(() => import("./tabs/TestTab"), { loading: TabLoadingFallback });             // NEW — Test tab design spec §1: simulate & test
const GrowthTab = dynamic(() => import("./tabs/GrowthTab"), { loading: TabLoadingFallback });           // NEW — Growth tab design spec §2: growth & marketing
import WorkspaceDataBubble from "./WorkspaceDataBubble";   // NEW — items #5/#13: relocated from floating-over-tab-content into the top nav

const TABS = [
  { id: "chat", label: "Chat", render: ChatTab },
  { id: "notebooks", label: "Notebooks", render: NotebooksTab },   // NEW — §4.7
  { id: "research", label: "Research", render: ResearchTab },     // NEW — Part 3 §3.9
  { id: "plan", label: "Plan", render: PlanTab },                 // FIX — Part 5: was missing from this array entirely
  { id: "build", label: "Build", render: BuildTab },               // NEW — Part 7 §7.2; label renamed Tasks→Build, id/component/localStorage keys left as "tasks" intentionally
  { id: "test", label: "Test", render: TestTab },                   // NEW — Test tab design spec §1
  { id: "growth", label: "Growth", render: GrowthTab },               // NEW — Growth tab design spec §2
  { id: "roles", label: "Role Library", render: RoleLibraryTab },
  { id: "templates", label: "Workflow Templates", render: WorkflowTemplatesTab },
  { id: "usage", label: "Token Usage", render: TokenUsageTab },
  { id: "settings", label: "Settings", render: SettingsTab },
];

const SIDEBAR_KEY = "minime_sidebar_collapsed";
const ACTIVE_TAB_KEY = "minime_active_tab";   // NEW — §4 fix: survive refresh, same pattern as SIDEBAR_KEY
const ACTIVE_CHAT_KEY = "minime_active_chat_id";   // NEW — Item 2 remaining piece, live-run-state slice, step 1: same key SessionContext.jsx/WorkspaceDockContext.jsx already read/write, needed here to decide which chat AppShellBody's bootstrap effect restores

// NEW — item #13: the tabs that resolve a workspaceId and therefore
// have a Data bubble to show in the nav. Role Library, Workflow
// Templates, Token Usage, and Settings never have project data, so the
// nav slot stays empty (not just hidden) on those tabs.
// CHANGED — W3.2: "local" (Part 6's standalone tab) removed from this
// set along with the tab itself — Local folder browsing now lives
// inside Build's Editor sub-view (W3.1's Explorer source switch), so it
// reports its workspace through Build's own "build" entry instead of a
// second one.
const WORKSPACE_TAB_IDS = new Set(["chat", "notebooks", "research", "plan", "build", "test", "growth"]);

// NEW — §8: which tab owns each workspace stage.
// FIX — plan/build were missing here even though Plan/Tasks tabs exist
// (or now exist, in Plan's case): promoting into either stage updated
// the backend correctly but silently failed to navigate anywhere.
// FIX — test was missing the same way until TestTab existed to receive
// a Build→Test promote.
const STAGE_TAB_MAP = { note: "notebooks", research: "research", plan: "plan", build: "tasks", test: "test", growth: "growth" };

export default function AppShell() {
  return (
    // NEW — NotificationsProvider sits alongside SessionProvider (it
    // only needs useAuth() and doesn't read/feed anything on
    // SessionContext — see its own comment). UsageStatsProvider,
    // WorkspacesProvider, and (CHANGED — slice 4) ChatListProvider all
    // have to sit ABOVE SessionProvider instead, because SessionContext.jsx
    // calls useUsageStats()/useWorkspaces()/useChatList() internally (see
    // each context's own header comment for why — SessionContext still
    // owns the chat-lifecycle functions that read/call `chats`/
    // `refreshChatList`, same as it still owns the workspace CRUD
    // functions that read/call `workspaces`/`fetchWorkspaces`).
    <NotificationsProvider>
      <UsageStatsProvider>
        <WorkspacesProvider>
          <ChatListProvider>
            <SessionProvider>
              <WorkspaceDockBridge />
            </SessionProvider>
          </ChatListProvider>
        </WorkspacesProvider>
      </UsageStatsProvider>
    </NotificationsProvider>
  );
}

// NEW — step 3e prereq: WorkspaceDockProvider needs refreshChatList/
// getWorkspaceIdForChat/getChats to run switchChat/createNewChat/etc (see
// WorkspaceDockContext.jsx's own comment on why — mother/child files don't
// import each other). Those three only exist inside SessionProvider, so
// this small bridge — same reasoning as AppShellBody's own split below —
// sits inside SessionProvider, reads them off useSession(), and passes
// them down as plain props. `getChats={() => chats}` is intentionally NOT
// memoized: this component re-renders whenever `chats` changes (it
// consumes the context), so a fresh inline closure each render is exactly
// what keeps deleteChat's "switch to another chat" fallback from reading
// a stale list — see WorkspaceDockProvider's callbacksRef for the other
// half of that (it re-reads these props every render too).
// Also threads handleUsageEvent through as `onUsageEvent` — 3e usage-
// event ownership, option 1 (see WorkspaceDockContext.jsx's file-header
// comment): usage_update/quota_alert are handled by the per-dock
// subscription now, forwarded up through this callback so usageStats/
// usageHistory/combinedUsageHistory can stay app-wide — CHANGED (Item 2
// concern split, slice 2): "app-wide" now means UsageStatsContext, not
// SessionContext, so handleUsageEvent is read from useUsageStats()
// directly instead of being forwarded through useSession().
// fetchWorkspaces similarly CHANGED (Item 2 concern split, slice 3): read
// from useWorkspaces() directly instead of useSession().
// refreshChatList/chats similarly CHANGED (Item 2 concern split, slice 4):
// read from useChatList() directly instead of useSession().
function WorkspaceDockBridge() {
  const { getWorkspaceIdForChat } = useSession();
  const { refreshChatList, chats } = useChatList();
  const { handleUsageEvent } = useUsageStats();
  const { fetchWorkspaces } = useWorkspaces();
  return (
    <WorkspaceDockProvider
      refreshChatList={refreshChatList}
      getWorkspaceIdForChat={getWorkspaceIdForChat}
      getChats={() => chats}
      fetchWorkspaces={fetchWorkspaces}
      onUsageEvent={handleUsageEvent}
    >
      <AppShellBody />
    </WorkspaceDockProvider>
  );
}

// NEW — split out of AppShell() so this component can sit INSIDE
// SessionProvider and call useSession() directly. Needed so a tab
// (e.g. Workflow Templates' "Open chat" button) can hand this a
// chat_id and actually land on it in the Chat tab, instead of only
// ever printing a session_id with nowhere to go.
function AppShellBody() {
  // NEW — step 3e: switchChat now resolves the correct per-workspace (or
  // per-standalone-chat) dock itself from the chatId a notification hands
  // it — it no longer writes into one shared SessionContext sessionId.
  // CHANGED — Item 2 remaining piece, live-run-state slice, step 1: also
  // pulls in createNewChat now, for the bootstrap effect below (same
  // reasoning as switchChat above — that decision needed to move off
  // SessionContext's own copy too, not just openChat()'s).
  const { switchChat, createNewChat } = useWorkspaceDockActions();
  // NEW — step 1: fetchBatches/fetchWorkspaces/refreshChatList, for the
  // same bootstrap effect. fetchBatches has no concern-context of its
  // own yet (Item 1 territory, not this slice), so it still comes off
  // useSession() — everything else here already has its own hook.
  const { fetchBatches } = useSession();
  const { fetchWorkspaces } = useWorkspaces();
  const { refreshChatList } = useChatList();
  const { markTaskDone } = useBootProgress();   // NEW — loading-screen wiring: see the bootstrap effect below
  const [viewport] = useViewport();   // NEW — Phase 1 (mobile shell): "mobile" | "tablet" | "desktop"
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);   // NEW — Phase 1: drives mobile/ChatSidebar.jsx's drawer; desktop's own sidebarCollapsed (below) is untouched
  const [activeTab, setActiveTabState] = useState("chat");
  // NEW — §4 fix: tabs that have been visited at least once stay mounted
  // (hidden via CSS, not unmounted) so their in-memory state — sub-tab,
  // an in-progress Mind Map paste, previewNode, scroll position — survives
  // switching away and back. Starts with "chat" since that's the initial
  // activeTab.
  const [visitedTabs, setVisitedTabs] = useState(() => new Set(["chat"]));
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [pendingTemplateRoles, setPendingTemplateRoles] = useState(null); // NEW — Role Library's sticky multi-select bar hands a role list here, WorkflowTemplatesTab consumes it once
  const [pendingWorkspaceSelection, setPendingWorkspaceSelection] = useState(null); // NEW — §8: { tabId, wsId } handed off by a promote action, consumed once by the destination tab
  const [pendingLocalTabRedirect, setPendingLocalTabRedirect] = useState(false); // NEW — W3.2: true for one render when a saved "local" tab id got remapped to "build" below; consumed once by BuildTab to open straight into Editor/Local

  // NEW — items #5/#13: { [tabId]: { id, name } | null } — each workspace-
  // bearing tab reports its own currently-selected workspace here via
  // onActiveWorkspaceChange, keyed by that tab's own id. Keyed per-tab
  // (not a single shared value) so that switching to a tab that hasn't
  // re-reported since its own last selection never shows another tab's
  // leftover workspace — visited tabs stay mounted but don't re-render
  // on a tab switch, so a single shared value would go stale exactly
  // the way item #8 did for the bubble's own fetch effect.
  const [workspaceContextByTab, setWorkspaceContextByTab] = useState({});

  const setTabWorkspaceContext = useCallback((tabId, ctx) => {
    setWorkspaceContextByTab((prev) => ({ ...prev, [tabId]: ctx }));
  }, []);

  // Stable per-tab callback cache so the function identity passed down as
  // onActiveWorkspaceChange never changes across renders (it used to be a
  // fresh inline arrow function every render, which fed straight into
  // ChatTab's useEffect dependency array and caused an infinite render
  // loop: effect fires -> setTabWorkspaceContext -> AppShell re-renders ->
  // new inline function -> effect deps change -> effect fires again).
  const workspaceChangeHandlersRef = useRef({});
  function getWorkspaceChangeHandler(tabId) {
    if (!workspaceChangeHandlersRef.current[tabId]) {
      workspaceChangeHandlersRef.current[tabId] = (workspaceId, workspaceName) =>
        setTabWorkspaceContext(tabId, workspaceId ? { id: workspaceId, name: workspaceName } : null);
    }
    return workspaceChangeHandlersRef.current[tabId];
  }

  const activeWorkspaceContext = workspaceContextByTab[activeTab] || null;

  useEffect(() => {
    setSidebarCollapsed(localStorage.getItem(SIDEBAR_KEY) === "1");
    // NEW — §4 fix: restore last active tab so a refresh doesn't always
    // land back on Chat.
    const savedTab = localStorage.getItem(ACTIVE_TAB_KEY);
    // CHANGED — W3.2: "local" no longer exists in TABS, so the check
    // below would just silently miss and leave activeTab on its "chat"
    // default — a real dead end for anyone who had Local Files open
    // last. Map it to "build" (where that functionality now lives,
    // W3.1) and flag the redirect so BuildTab can also land on its
    // Editor sub-view with the Local source selected, once.
    if (savedTab === "local") {
      setActiveTabState("build");
      setVisitedTabs((prev) => new Set(prev).add("build"));
      localStorage.setItem(ACTIVE_TAB_KEY, "build");
      setPendingLocalTabRedirect(true);
    } else if (savedTab && TABS.some((t) => t.id === savedTab)) {
      setActiveTabState(savedTab);
      setVisitedTabs((prev) => new Set(prev).add(savedTab));
    }
  }, []);

  // NEW — mobile height fix: `h-screen` (100vh) is sized off the LARGEST
  // possible mobile-browser viewport (address bar collapsed), which is
  // taller than what's actually visible the moment the page loads (bar
  // still showing) — the shell rendered taller than the screen, so
  // reaching the composer or any other bottom-of-page control took an
  // extra scroll. `app-shell-viewport` in globals.css already upgrades
  // the CSS fallback chain to 100dvh (tracks the real visible height as
  // browser chrome shows/hides), which covers that case in every modern
  // engine on its own with zero JS. This effect is the next rung up that
  // same fallback chain: `window.visualViewport` additionally shrinks
  // when the on-screen keyboard opens (100dvh's keyboard behavior is
  // real but has historically been inconsistent across iOS Safari
  // versions), so mirroring its height into `--app-vvh` and having the
  // shell prefer that var when present is what keeps the composer from
  // being covered by the keyboard specifically, on top of the chrome-
  // show/hide case dvh already handles. No-ops (leaves the dvh fallback
  // in charge) on any browser without visualViewport.
  useEffect(() => {
    if (typeof window === "undefined" || !window.visualViewport) return;
    const vv = window.visualViewport;
    function applyVvh() {
      document.documentElement.style.setProperty("--app-vvh", `${vv.height}px`);
    }
    applyVvh();
    vv.addEventListener("resize", applyVvh);
    vv.addEventListener("scroll", applyVvh);
    return () => {
      vv.removeEventListener("resize", applyVvh);
      vv.removeEventListener("scroll", applyVvh);
    };
  }, []);

  // NEW — Item 2 remaining piece, live-run-state slice, step 1: on mount,
  // load the chat list, then restore the last active chat (or create the
  // very first one). MOVED from SessionContext.jsx's own mount effect,
  // same body, same behavior — just retargeted from that component's own
  // switchChat()/createNewChat() (which wrote into a global sessionId/
  // messages pair that step 3e already stopped wiring any real consumer
  // to) onto the dock's copies via useWorkspaceDockActions(). Those write
  // into lastActiveChatId and the correct per-key dock slot directly, the
  // same way every other chat-lifecycle action (ChatSidebar's row click,
  // "+ New chat", etc.) already does — so the default Chat tab now boots
  // straight into dock mode instead of relying on WorkspaceChatPanel's
  // "legacy" fallback (dock.key == null) until the user's first click.
  // SessionContext.jsx's own switchChat/createNewChat are untouched by
  // this — removeWorkspaceChat's fallback (SessionContext.jsx) still
  // calls its own copies, so they can't be deleted yet; this only moves
  // who's responsible for the very first "which chat opens" decision.
  //
  // CHANGED — perf audit §2.3 step B: fetchBatches()/fetchWorkspaces()
  // used to sit AFTER `await refreshChatList()` (and after its `if (list
  // === null) return`), even though neither one reads anything off the
  // chat list — they were waiting on a network round trip they have no
  // actual dependency on, and a chat-list fetch FAILURE silently skipped
  // them entirely (an unrelated failure killing two unrelated,
  // independently-recoverable fetches). Both are now fired in the same
  // tick as refreshChatList(), not chained behind it, so all three go
  // out concurrently; only the chat-restore decision below still awaits
  // refreshChatList() specifically, since it genuinely needs that data.
  useEffect(() => {
    (async () => {
      // BUGFIX (401 storm that survives its own retry): fetchBatches/
      // fetchWorkspaces/refreshChatList each read their auth header via
      // supabase.auth.getSession(), which only trusts whatever's in the
      // cookie -- it does NOT validate against the server. Right after a
      // sign-out -> sign-in transition that can be momentarily wrong, and
      // firing all three in parallel meant three independent callers each
      // discovering that the hard way, each rolling its own
      // refreshSession() recovery. page.js already uses getUser() instead
      // of getSession() for exactly this reason ("validates the token
      // against Supabase rather than trusting whatever's in the cookie");
      // this applies that same discipline once, here, before the fan-out,
      // instead of three times after the fact.
      await supabase.auth.getUser();

      // NEW — loading-screen wiring: each branch marks its own boot task
      // done via .finally()/on every exit path, success or failure alike,
      // so a failed fetch can never leave Gate.jsx's splash waiting
      // forever on a signal that will never arrive — better to reveal the
      // (possibly partially-loaded) app than to strand the visitor behind
      // a permanent splash screen.
      fetchBatches().finally(() => markTaskDone("batches"));     // NEW — §4: independent of the chat list, no reason to wait on it (see comment above)
      fetchWorkspaces().finally(() => markTaskDone("workspaces"));  // NEW — §7: same
      try {
        const list = await refreshChatList();
        if (list === null) return;
        const savedId = typeof window !== "undefined" ? localStorage.getItem(ACTIVE_CHAT_KEY) : null;
        const stillExists = savedId && list.some((c) => c.id === savedId);

        if (stillExists) {
          await switchChat(savedId, { skipListReload: true });
        } else if (list.length > 0) {
          // Don't silently jump to a "new chat" tab on reload — reopen
          // whatever chat is most recently updated instead.
          await switchChat(list[0].id, { skipListReload: true });
        } else {
          await createNewChat();
        }
      } catch (err) {
        // Bug fix: this whole block used to run with no try/catch, so a
        // failed request in here (backend unreachable, CORS, etc.) threw
        // before markTaskDone ever ran below -- leaving Gate.jsx's splash
        // screen stuck forever instead of revealing the (possibly
        // partially-loaded) app, same failure mode the fetchBatches/
        // fetchWorkspaces calls above are already guarded against.
        console.error("Chat bootstrap failed:", err);
      } finally {
        markTaskDone("chatBootstrap");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // NEW — §4 fix: every tab switch both updates the active tab and marks
  // it as visited (so it starts rendering, and then stays mounted), and
  // persists the choice so a page refresh reopens the same tab.
  function setActiveTab(id) {
    setActiveTabState(id);
    setVisitedTabs((prev) => (prev.has(id) ? prev : new Set(prev).add(id)));
    localStorage.setItem(ACTIVE_TAB_KEY, id);
  }

  function toggleSidebar() {
    setSidebarCollapsed((prev) => {
      localStorage.setItem(SIDEBAR_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }

  // NEW — Phase 1 (mobile shell): the mobile header's panel-toggle button
  // has no direct reference to whichever WorkspaceChatPanel instance is
  // currently mounted (that state is local to that component, per-dock —
  // see WorkingPanelDrawer.jsx's own comment on why), so it signals via
  // this module-level event instead of a prop. Same "custom event, no
  // context needed" approach useDensity.js/useViewport.js already use.
  function openMobileWorkingPanel() {
    window.dispatchEvent(new Event(OPEN_WORKING_PANEL_EVENT));
  }

  // NEW — mobile picker-drawer generalization: the hamburger used to be
  // Chat-only (showSidebarButton={activeTab === "chat"} below). Every
  // tab that has its own left-hand picker column on desktop (Notebooks'
  // notebook list, Research's project list) gets the same hamburger
  // affordance on mobile instead of losing that picker entirely below
  // 768px. Chat keeps its own dedicated open path (setMobileSidebarOpen
  // — AppShell already owns that piece of state); every other listed
  // tab doesn't have state AppShell can reach into, so it gets a plain
  // cross-component event (see mobile/events.js's OPEN_TAB_SIDEBAR_EVENT
  // comment) that the tab body itself listens for and answers with its
  // own MobileDrawer, the same way WorkspaceChatPanel already answers
  // OPEN_WORKING_PANEL_EVENT for the panel toggle.
  // BUGFIX — Research's own controller/MobileDrawer/OPEN_TAB_SIDEBAR_EVENT
  // listener (components/mobile/ResearchTab.jsx, ../tabs/ResearchTab.jsx)
  // were all built and wired up, but "research" was never added to this
  // set, so the hamburger button silently didn't render at all on
  // Research's tab body — the drawer wasn't broken, it was just never
  // reachable. Added "plan" up front this time (components/mobile/PlanTab.jsx,
  // ../tabs/PlanTab.jsx) so Plan doesn't repeat the same oversight, and
  // "test" the same way (components/mobile/TestTab.jsx, ../tabs/TestTab.jsx).
  // "growth" the same way (components/mobile/GrowthTab.jsx,
  // ../tabs/GrowthTab.jsx). Build's project list has the same shape but
  // hasn't had its own mobile pass yet (Phase 5 — still "not started" per
  // MOBILE_PLAN.md), so it stays off this list until that lands for real —
  // same "add it deliberately, not as an afterthought" note Research's own
  // fix left behind.
  const TABS_WITH_OWN_MOBILE_SIDEBAR = new Set(["chat", "notebooks", "research", "plan", "test", "growth"]);

  function openMobileTabSidebar() {
    if (activeTab === "chat") {
      setMobileSidebarOpen(true);
    } else {
      window.dispatchEvent(new CustomEvent(OPEN_TAB_SIDEBAR_EVENT, { detail: { tabId: activeTab } }));
    }
  }

  // Loads the given chat (same call ChatSidebar's own chat-switcher
  // uses) and switches the active tab to Chat, in one action — this is
  // what turns "Session: abc123" plain text into a real, working
  // navigation button.
  async function openChat(chatId) {
    await switchChat(chatId);
    setActiveTab("chat");
  }

  // NEW — Role Library's sticky selection bar calls this with the
  // selected role names (in the order they were checked), which lands
  // in WorkflowTemplatesTab as a pre-filled TemplateBuilder, same
  // "hand off a chat_id, land on it" pattern as openChat() above.
  function startTemplateWithRoles(roles) {
    setPendingTemplateRoles(roles);
    setActiveTab("templates");
  }

  // NEW — §8: called by a tab after a successful promoteWorkspace().
  // Navigates to whichever tab owns the new stage (if one exists yet)
  // and pre-selects the workspace there, so it doesn't just disappear
  // from the current tab with no visible destination.
  function handlePromoted(nextStage, wsId) {
    const targetTab = STAGE_TAB_MAP[nextStage];
    if (targetTab) {
      setPendingWorkspaceSelection({ tabId: targetTab, wsId });
      setActiveTab(targetTab);
    }
  }

  return (
    <div className="flex flex-col h-screen app-shell-viewport">
      {/* NEW — Phase 1 (mobile shell): structural fork, not a couple of
          sm:/md: tweaks — see components/mobile/README.md. Below
          "mobile" (<768px) this swaps the persistent nav row + ml-auto
          icon cluster for a hamburger/scrollable-tabs/panel-toggle bar;
          everything else in AppShellBody (state, effects, tab-render
          logic just below) is unchanged and shared by both. Tablet
          still gets the desktop header for now — Phase 7 revisits
          whether it needs its own fork or just md: tweaks. */}
      {viewport === "mobile" ? (
        <MobileHeader
          tabs={TABS}
          activeTab={activeTab}
          onSelectTab={setActiveTab}
          showSidebarButton={TABS_WITH_OWN_MOBILE_SIDEBAR.has(activeTab)}
          onOpenSidebar={openMobileTabSidebar}
          sidebarLabel={
            // NEW — Research fix: matches components/mobile/ResearchTab.jsx's
            // own drawer header text ("Research projects"), same way
            // "Notebooks" already matches mobile/NotebooksTab.jsx's. Plan
            // added the same way — matches mobile/PlanTab.jsx's own drawer
            // header text ("Plan projects"), and Test too — matches
            // mobile/TestTab.jsx's ("Test projects"), and Growth —
            // matches mobile/GrowthTab.jsx's ("Growth workspaces").
            activeTab === "notebooks" ? "Notebooks"
              : activeTab === "research" ? "Research projects"
              : activeTab === "plan" ? "Plan projects"
              : activeTab === "test" ? "Test projects"
              : activeTab === "growth" ? "Growth workspaces"
              : "Chats"
          }
          showWorkingPanelButton={activeTab === "chat"}
          onOpenWorkingPanel={openMobileWorkingPanel}
        />
      ) : (
        <header className="relative border-b border-[var(--neutral-800)] px-4 py-3 flex items-center gap-6">
          <h1 className="flex items-center gap-1.5 text-sm font-bold tracking-tight">
            <img src="/minime-logo.svg" alt="" className="w-5 h-5 object-contain" />
            <span className="text-[var(--neutral-200)]">
              Mini<span className="text-[#ff5168]">Me</span>
            </span>
          </h1>
          <nav className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 flex gap-1 max-w-[calc(100%-320px)] overflow-x-auto">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setActiveTab(t.id)}
                className={`text-xs rounded-lg px-3 py-1.5 transition-colors ${
                  activeTab === t.id ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            {/* NEW — items #5/#13: nav slot for the Data bubble. Only
                mounted on the 7 tabs that resolve a workspaceId, and only
                once that tab has actually reported one (a tab with no
                project selected yet shows nothing here, not an empty
                bubble). Role Library, Workflow Templates, Token Usage,
                and Settings never hit WORKSPACE_TAB_IDS, so the slot is
                simply absent there. */}
            {WORKSPACE_TAB_IDS.has(activeTab) && activeWorkspaceContext?.id && (
              <WorkspaceDataBubble
                workspaceId={activeWorkspaceContext.id}
                workspaceName={activeWorkspaceContext.name}
                storageKey={`minime_databubble_collapsed_${activeWorkspaceContext.id}`}
              />
            )}
            <NotificationBell onOpenChat={openChat} />
            <AccountMenu />
          </div>
        </header>
      )}
      <div className="flex flex-1 min-h-0 min-w-0">
        {/* CHANGED — Phase 1: desktop keeps the persistent flex-column
            sidebar exactly as before; mobile renders the same "only on
            the chat tab" ChatSidebar as a left-side overlay drawer
            instead (mobile/ChatSidebar.jsx — see its own comment for
            why account/notifications live inside it now). */}
        {activeTab === "chat" && viewport !== "mobile" && (
          <ChatSidebar
            collapsed={sidebarCollapsed}
            onToggle={toggleSidebar}
          />
        )}
        {activeTab === "chat" && viewport === "mobile" && (
          <MobileChatSidebar
            open={mobileSidebarOpen}
            onClose={() => setMobileSidebarOpen(false)}
            onOpenChat={openChat}
          />
        )}
        <div className="flex-1 min-h-0 min-w-0 overflow-hidden">
          {/* NEW — §4 fix: every visited tab stays mounted (display: none
              instead of unmounting) so switching tabs doesn't wipe out
              in-component state. Only tabs that have actually been opened
              render at all, so we don't eagerly fetch data for every tab
              on first load.
              onOpenChat: only WorkflowTemplatesTab reads this prop today;
              onStartTemplate: only RoleLibraryTab reads this;
              initialTemplateRoles/onConsumeInitialTemplateRoles: only
              WorkflowTemplatesTab reads these. Every other tab ignores
              props it doesn't use harmlessly. */}
          {TABS.filter((t) => visitedTabs.has(t.id)).map((t) => {
            const TabComponent = t.render;
            return (
              <div
                key={t.id}
                style={{ display: activeTab === t.id ? "contents" : "none" }}
              >
                <TabComponent
                  onOpenChat={openChat}
                  onStartTemplate={startTemplateWithRoles}
                  initialTemplateRoles={pendingTemplateRoles}
                  onConsumeInitialTemplateRoles={() => setPendingTemplateRoles(null)}
                  initialWorkspaceId={pendingWorkspaceSelection?.tabId === t.id ? pendingWorkspaceSelection.wsId : null}
                  onConsumeInitialWorkspaceId={() => setPendingWorkspaceSelection(null)}
                  initialLocalTabRedirect={t.id === "build" ? pendingLocalTabRedirect : false}
                  onConsumeInitialLocalTabRedirect={() => setPendingLocalTabRedirect(false)}
                  onPromoted={handlePromoted}
                  onActiveWorkspaceChange={
                    WORKSPACE_TAB_IDS.has(t.id)
                      ? getWorkspaceChangeHandler(t.id)
                      : undefined
                  }
                />
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}