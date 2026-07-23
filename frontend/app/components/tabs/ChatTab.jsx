"use client";
import { useEffect, useMemo } from "react";
import { useSession } from "../../context/SessionContext";
import { useLastActiveChatId } from "../../context/WorkspaceDockContext";
import WorkspaceChatPanel from "../WorkspaceChatPanel";

// CHANGED — §6 sub-step 1: this used to contain the entire chat box +
// WorkingPanel composition directly. That composition now lives in
// WorkspaceChatPanel.jsx so it can also be docked inside Notebooks/
// Research/etc (next sub-step). ChatTab is left as a thin wrapper —
// unchanged behavior for the standalone "Chat" tab in top nav — so
// nothing else in AppShell.jsx needs to change yet.
//
// CHANGED — step 3e regression fix: ChatTab was the one remaining call
// site deriving its chat from SessionContext's `sessionId`. Now that
// ChatSidebar (the normal way anyone switches chats) calls the dock's
// switchChat instead of SessionContext's, `sessionId` stopped updating
// on chat clicks — this tab's embedded panel and data bubble went stale.
// Fixed by reading `useLastActiveChatId()` instead (tracks whichever
// chat was most recently the target of switchChat/createNewChat, from
// any tab, dock-side) and resolving its workspace via
// `getWorkspaceIdForChat`, then handing both ids down to
// WorkspaceChatPanel so it renders in dock mode instead of legacy mode.
export default function ChatTab({ onActiveWorkspaceChange }) {
  const { workspaces, getWorkspaceIdForChat } = useSession();
  const lastActiveChatId = useLastActiveChatId();
  const activeWorkspaceId = lastActiveChatId ? getWorkspaceIdForChat(lastActiveChatId) : null;
  const activeWorkspace = useMemo(
    () => (workspaces || []).find((workspace) => workspace.id === activeWorkspaceId),
    [workspaces, activeWorkspaceId]
  );

  // NEW — item #1: the Data bubble now lives in AppShell's top nav, not
  // floating over this tab's own content, so this just reports which
  // workspace (if any) is active instead of rendering the bubble itself.
  useEffect(() => {
    onActiveWorkspaceChange?.(activeWorkspace?.id || null, activeWorkspace?.name);
  }, [activeWorkspace?.id, activeWorkspace?.name, onActiveWorkspaceChange]);

  return (
    <div className="relative h-full min-h-0">
      <WorkspaceChatPanel workspaceId={activeWorkspaceId} chatId={lastActiveChatId} />
    </div>
  );
}
