"use client";
import { useMemo } from "react";
import { useSession } from "../../context/SessionContext";
import WorkspaceChatPanel from "../WorkspaceChatPanel";
import WorkspaceDataBubble from "../WorkspaceDataBubble";

// CHANGED — §6 sub-step 1: this used to contain the entire chat box +
// WorkingPanel composition directly. That composition now lives in
// WorkspaceChatPanel.jsx so it can also be docked inside Notebooks/
// Research/etc (next sub-step). ChatTab is left as a thin wrapper —
// unchanged behavior for the standalone "Chat" tab in top nav — so
// nothing else in AppShell.jsx needs to change yet.
export default function ChatTab() {
  const { sessionId, workspaces } = useSession();
  const activeWorkspace = useMemo(
    () => (workspaces || []).find((workspace) => Array.isArray(workspace.chat_ids) && workspace.chat_ids.includes(sessionId)),
    [workspaces, sessionId]
  );

  return (
    <div className="relative h-full min-h-0">
      <WorkspaceChatPanel />
      <WorkspaceDataBubble
        workspaceId={activeWorkspace?.id}
        workspaceName={activeWorkspace?.name}
        storageKey="minime_chat_data_bubble_collapsed"
      />
    </div>
  );
}
