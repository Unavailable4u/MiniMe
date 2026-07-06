"use client";
import { useState } from "react";
import { SessionProvider } from "../context/SessionContext";
import ChatTab from "./tabs/ChatTab";
import TokenUsageTab from "./tabs/TokenUsageTab";
import RoutingStructureTab from "./tabs/RoutingStructureTab";
import SettingsTab from "./tabs/SettingsTab";

const TABS = [
  { id: "chat", label: "Chat", render: ChatTab },
  { id: "usage", label: "Token Usage", render: TokenUsageTab },
  { id: "routing", label: "Routing & Structure", render: RoutingStructureTab },
  { id: "settings", label: "Settings", render: SettingsTab },
];

export default function AppShell() {
  const [activeTab, setActiveTab] = useState("chat");
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
                  activeTab === t.id
                    ? "bg-neutral-100 text-neutral-900 font-medium"
                    : "text-neutral-500 hover:text-neutral-300"
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>
        </header>
        <div className="flex-1 min-h-0">
          <Active />
        </div>
      </div>
    </SessionProvider>
  );
}
