"use client";
import { useState, useEffect } from "react";
import { SessionProvider } from "../context/SessionContext";
import ChatSidebar from "./ChatSidebar";
import ChatTab from "./tabs/ChatTab";
import TokenUsageTab from "./tabs/TokenUsageTab";
import SettingsTab from "./tabs/SettingsTab";
import RoleLibraryTab from "./tabs/RoleLibraryTab";
import WorkflowTemplatesTab from "./tabs/WorkflowTemplatesTab";

const TABS = [
  { id: "chat", label: "Chat", render: ChatTab },
  { id: "roles", label: "Role Library", render: RoleLibraryTab },
  { id: "templates", label: "Workflow Templates", render: WorkflowTemplatesTab },
  { id: "usage", label: "Token Usage", render: TokenUsageTab },
  { id: "settings", label: "Settings", render: SettingsTab },
];

const SIDEBAR_KEY = "minime_sidebar_collapsed";

export default function AppShell() {
  const [activeTab, setActiveTab] = useState("chat");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  useEffect(() => {
    setSidebarCollapsed(localStorage.getItem(SIDEBAR_KEY) === "1");
  }, []);

  function toggleSidebar() {
    setSidebarCollapsed((prev) => {
      localStorage.setItem(SIDEBAR_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }

  const Active = TABS.find((t) => t.id === activeTab)?.render ?? ChatTab;
  return (
    <SessionProvider>
      <div className="flex flex-col h-screen">
        <header className="border-b border-neutral-800 px-4 py-3 flex items-center gap-6">
          <h1 className="text-sm font-medium text-neutral-400">MiniMe</h1>
          <nav className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setActiveTab(t.id)}
                className={`text-xs rounded-lg px-3 py-1.5 transition-colors ${
                  activeTab === t.id ? "bg-neutral-100 text-neutral-900 font-medium" : "text-neutral-500 hover:text-neutral-300"
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>
        </header>
        <div className="flex flex-1 min-h-0">
          {activeTab === "chat" && <ChatSidebar collapsed={sidebarCollapsed} onToggle={toggleSidebar} />}
          <div className="flex-1 min-h-0">
            <Active />
          </div>
        </div>
      </div>
    </SessionProvider>
  );
}