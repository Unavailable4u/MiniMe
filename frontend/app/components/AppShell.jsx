"use client";
import { useState, useEffect } from "react";
import { SessionProvider, useSession } from "../context/SessionContext";
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

// NEW — §8: which tab owns each workspace stage.
// FIX — plan/build were missing here even though Plan/Tasks tabs exist
// (or now exist, in Plan's case): promoting into either stage updated
// the backend correctly but silently failed to navigate anywhere.
// FIX — test was missing the same way until TestTab existed to receive
// a Build→Test promote.
const STAGE_TAB_MAP = { note: "notebooks", research: "research", plan: "plan", build: "tasks", test: "test", growth: "growth" };

export default function AppShell() {
  return (
    <SessionProvider>
      <WorkspaceDockBridge />
    </SessionProvider>
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
function WorkspaceDockBridge() {
  const { refreshChatList, getWorkspaceIdForChat, chats, fetchWorkspaces } = useSession();
  return (
    <WorkspaceDockProvider
      refreshChatList={refreshChatList}
      getWorkspaceIdForChat={getWorkspaceIdForChat}
      getChats={() => chats}
      fetchWorkspaces={fetchWorkspaces}
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
  const { switchChat } = useWorkspaceDockActions();
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
                />
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
