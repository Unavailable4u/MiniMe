"use client";
import { useState, useEffect } from "react";
import { SessionProvider, useSession } from "../context/SessionContext";
import ChatSidebar from "./ChatSidebar";
import ChatTab from "./tabs/ChatTab";
import TokenUsageTab from "./tabs/TokenUsageTab";
import SettingsTab from "./tabs/SettingsTab";
import RoleLibraryTab from "./tabs/RoleLibraryTab";
import WorkflowTemplatesTab from "./tabs/WorkflowTemplatesTab";
import NotebooksTab from "./tabs/NotebooksTab";   // NEW — §4.7: dedicated Notebooks section
import ResearchTab from "./tabs/ResearchTab";     // NEW — Part 3 §3.9: dedicated Research section

const TABS = [
  { id: "chat", label: "Chat", render: ChatTab },
  { id: "notebooks", label: "Notebooks", render: NotebooksTab },   // NEW — §4.7
  { id: "research", label: "Research", render: ResearchTab },     // NEW — Part 3 §3.9
  { id: "roles", label: "Role Library", render: RoleLibraryTab },
  { id: "templates", label: "Workflow Templates", render: WorkflowTemplatesTab },
  { id: "usage", label: "Token Usage", render: TokenUsageTab },
  { id: "settings", label: "Settings", render: SettingsTab },
];

const SIDEBAR_KEY = "minime_sidebar_collapsed";

export default function AppShell() {
  return (
    <SessionProvider>
      <AppShellBody />
    </SessionProvider>
  );
}

// NEW — split out of AppShell() so this component can sit INSIDE
// SessionProvider and call useSession() directly. Needed so a tab
// (e.g. Workflow Templates' "Open chat" button) can hand this a
// chat_id and actually land on it in the Chat tab, instead of only
// ever printing a session_id with nowhere to go.
function AppShellBody() {
  const { switchChat } = useSession();
  const [activeTab, setActiveTab] = useState("chat");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [pendingTemplateRoles, setPendingTemplateRoles] = useState(null); // NEW — Role Library's sticky multi-select bar hands a role list here, WorkflowTemplatesTab consumes it once

  useEffect(() => {
    setSidebarCollapsed(localStorage.getItem(SIDEBAR_KEY) === "1");
  }, []);

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

  const Active = TABS.find((t) => t.id === activeTab)?.render ?? ChatTab;
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
      </header>
      <div className="flex flex-1 min-h-0">
        {activeTab === "chat" && (
          <ChatSidebar
            collapsed={sidebarCollapsed}
            onToggle={toggleSidebar}
            onOpenNotebooks={() => setActiveTab("notebooks")}
            onOpenResearch={() => setActiveTab("research")}
          />
        )}
        <div className="flex-1 min-h-0">
          {/* onOpenChat: only WorkflowTemplatesTab reads this prop today;
              onStartTemplate: only RoleLibraryTab reads this;
              initialTemplateRoles/onConsumeInitialTemplateRoles: only
              WorkflowTemplatesTab reads these. Every other tab ignores
              props it doesn't use harmlessly. */}
          <Active
            onOpenChat={openChat}
            onStartTemplate={startTemplateWithRoles}
            initialTemplateRoles={pendingTemplateRoles}
            onConsumeInitialTemplateRoles={() => setPendingTemplateRoles(null)}
          />
        </div>
      </div>
    </div>
  );
}
