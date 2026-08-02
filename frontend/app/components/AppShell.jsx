"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import { SessionProvider, useSession } from "../context/SessionContext";
import { NotificationsProvider } from "../context/NotificationsContext";   // NEW — Item 2 concern split, slice 1: notifications/unreadCount/markNotificationsRead moved out of SessionContext
import { UsageStatsProvider, useUsageStats } from "../context/UsageStatsContext";   // NEW — Item 2 concern split, slice 2: usageStats/usageHistory/combinedUsageHistory/handleUsageEvent moved out of SessionContext
import { WorkspacesProvider, useWorkspaces } from "../context/WorkspacesContext";   // NEW — Item 2 concern split, slice 3: workspaces/fetchWorkspaces moved out of SessionContext
import { ChatListProvider, useChatList } from "../context/ChatListContext";   // NEW — Item 2 concern split, slice 4: chats/refreshChatList moved out of SessionContext
import { WorkspaceDockProvider, useWorkspaceDockActions } from "../context/WorkspaceDockContext";   // NEW — step 3d/3e-prereq: WorkspaceChatPanel calls useWorkspaceDock() unconditionally, and the lifecycle functions (switchChat etc.) now live here too, needing refreshChatList/getWorkspaceIdForChat/getChats threaded in — see WorkspaceDockBridge below. useWorkspaceDockActions is the step 3e cutover for AppShellBody's own openChat below.
import ChatSidebar from "./ChatSidebar";
import ChatTab from "./tabs/ChatTab";
import TokenUsageTab from "./tabs/TokenUsageTab";
import SettingsTab from "./tabs/SettingsTab";
import RoleLibraryTab from "./tabs/RoleLibraryTab";
import WorkflowTemplatesTab from "./tabs/WorkflowTemplatesTab";
import NotebooksTab from "./tabs/NotebooksTab";   // NEW — §4.7: dedicated Notebooks section
import ResearchTab from "./tabs/ResearchTab";     // NEW — Part 3 §3.9: dedicated Research section
import PlanTab from "./tabs/PlanTab";             // FIX — Part 5: was built as a file but never registered here, so it had no top-nav entry and no way to receive a promoted workspace
import BuildTab from "./tabs/BuildTab";           // NEW — Part 7 §7.2: kanban board over feature_status/current_plan
import TestTab from "./tabs/TestTab";             // NEW — Test tab design spec §1: simulate & test
import GrowthTab from "./tabs/GrowthTab";           // NEW — Growth tab design spec §2: growth & marketing
import AccountMenu from "./auth/AccountMenu";      // NEW — Part 8.9: signed-in user email + sign out
import NotificationBell from "./NotificationBell";   // NEW — Part 8.9: cross-chat notification inbox
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

// NEW — item #13: the 7 tabs that resolve a workspaceId and therefore
// have a Data bubble to show in the nav. Role Library, Workflow
// Templates, Token Usage, and Settings never have project data, so the
// nav slot stays empty (not just hidden) on those tabs.
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
    if (savedTab && TABS.some((t) => t.id === savedTab)) {
      setActiveTabState(savedTab);
      setVisitedTabs((prev) => new Set(prev).add(savedTab));
    }
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
  useEffect(() => {
    (async () => {
      const list = await refreshChatList();
      if (list === null) return;
      fetchBatches();     // NEW — §4: don't block chat restore on this, batches are additive UI
      fetchWorkspaces();  // NEW — §7: also additive, don't block chat restore on it
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
    <div className="flex flex-col h-screen">
      <header className="border-b border-[var(--neutral-800)] px-4 py-3 flex items-center gap-6">
        <h1 className="text-sm font-medium text-[var(--neutral-400)]">MiniMe</h1>
        <nav className="flex gap-1">
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
      <div className="flex flex-1 min-h-0">
        {activeTab === "chat" && (
          <ChatSidebar
            collapsed={sidebarCollapsed}
            onToggle={toggleSidebar}
          />
        )}
        <div className="flex-1 min-h-0">
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