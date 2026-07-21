"use client";
import WorkspaceChatPanel from "../WorkspaceChatPanel";

// CHANGED — §6 sub-step 1: this used to contain the entire chat box +
// WorkingPanel composition directly. That composition now lives in
// WorkspaceChatPanel.jsx so it can also be docked inside Notebooks/
// Research/etc (next sub-step). ChatTab is left as a thin wrapper —
// unchanged behavior for the standalone "Chat" tab in top nav — so
// nothing else in AppShell.jsx needs to change yet.
export default function ChatTab() {
  return <WorkspaceChatPanel />;
}
