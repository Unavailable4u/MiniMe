"use client";
// frontend/app/context/ChatListContext.jsx
//
// Audit §2.1 / Item 2 (remaining work), slice 4 of the "split
// SessionContext into concern-based contexts" fix (slice 1:
// NotificationsContext.jsx, slice 2: UsageStatsContext.jsx, slice 3:
// WorkspacesContext.jsx). Same "state-only" shape as slice 3
// (WorkspacesContext.jsx) — see that file's header comment, which flagged
// this as the next planned slice.
//
// SCOPE NOTE — read before extending this file: the audit's remaining
// Item 2 work is described as two things, "chat/messages and live-run
// state." This slice takes only the sidebar chat-list half of
// "chat/messages": the `chats` array (used by ChatSidebar.jsx,
// GrowthTab.jsx, BuildTab.jsx, ResearchTab.jsx, TestTab.jsx,
// NotebooksTab.jsx, PlanTab.jsx, and WorkspaceDockContext.jsx's
// getChats() bridge) and `refreshChatList()`, the function that
// refetches it. That's the part of "chat/messages" that behaves exactly
// like the workspaces slice: pure fetched-list state with a single
// setter function, no entanglement with anything else.
//
// DELIBERATELY NOT moved here yet: `messages` (the active chat's message
// array) and `sessionId` (which chat is active), plus switchChat/
// createNewChat/renameChat/deleteChat/linkChats/persistMessage/sendTask/
// resumeRun/confirmHireReview. Reason: every one of those closes over
// liveSteps/liveDecision/routeTrace/dependencyMap/structurePlan/
// roleRequests/macroLoopDecisions state (see switchChat's own comment in
// SessionContext.jsx, "Both close over a pile of setState setters and
// refs") — i.e. they're the "live-run state" half of Item 2's remaining
// work, not this slice's "chat/messages" half. Splitting `messages` out
// on its own, without also splitting liveSteps/liveDecision/etc., would
// just move the entanglement into a new file instead of removing it.
// That's the next slice, and it's a bigger one — this one is deliberately
// scoped to land safely first.
//
// `chats` is still read (not mutated) inside a few functions that stay in
// SessionContext.jsx (e.g. removeWorkspaceChat's "don't strand the user on
// a deleted chat" fallback) — those pull it from useChatList() now instead
// of local state, same as SessionContext already does for `workspaces`
// (useWorkspaces()) and `handleUsageEvent` (useUsageStats()).
//
// Has to sit ABOVE SessionProvider (like UsageStatsProvider/
// WorkspacesProvider, unlike NotificationsProvider), because
// SessionContext.jsx's mount effect and several of its still-resident
// functions (switchChat, createNewChat, removeWorkspaceChat, deleteChat,
// linkChats, createWorkspaceChat, persistMessageTo, ...) all call
// refreshChatList()/read `chats` directly.
import { createContext, useContext, useState, useCallback, useMemo } from "react";
import { supabase } from "../lib/supabaseClient";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Duplicated from SessionContext.jsx rather than imported from it, same
// reasoning WorkspacesContext.jsx/WorkspaceDockContext.jsx give for their
// own copies: both read the same env vars via the same shared
// supabaseClient.js singleton, so they can't drift in practice.
async function authHeaders(opts = {}) {
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token;
  const headers = {};
  if (opts.json) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

const ChatListContext = createContext(null);

export function useChatList() {
  const ctx = useContext(ChatListContext);
  if (!ctx) throw new Error("useChatList() must be used inside <ChatListProvider>");
  return ctx;
}

export function ChatListProvider({ children }) {
  const [chats, setChats] = useState([]); // NEW — sidebar list, MOVED from SessionContext.jsx

  // Pure extraction of the fetch/set that used to be inlined at the top
  // of SessionContext.jsx's mount effect PLUS the separately-defined
  // refreshChatList() further down that file — those were two copies of
  // the same GET /api/chats call (one used only once, on mount; the
  // other used by every chat-lifecycle function afterward). Unified into
  // one function here since there's no reason to keep both bodies once
  // both live in the same file.
  //
  // Returns the fetched array on success, or `null` on failure — the
  // `null` case matters to SessionContext.jsx's mount effect, which needs
  // to tell "fetch failed, don't try to restore/create a chat" apart from
  // "fetch succeeded, there just aren't any chats yet" (the latter is the
  // legitimate `list.length === 0` -> createNewChat() case). A callback
  // that only ever set state and returned nothing (the old shape) can't
  // make that distinction for its caller.
  const refreshChatList = useCallback(async () => {
    const res = await fetch(`${API_URL}/api/chats`, {
      headers: await authHeaders(),
    });
    const body = await res.json();
    // Same guard as SessionContext.jsx's old inline version and
    // WorkspacesContext.jsx's fetchWorkspaces() — never let a non-array
    // response (e.g. {"detail": "..."} from an auth/server error) reach
    // ChatSidebar.jsx's chats.filter() and crash the app.
    if (!res.ok || !Array.isArray(body)) {
      console.error("Failed to load chats:", res.status, body);
      setChats([]);
      return null;
    }
    setChats(body);
    return body;
  }, []);

  const value = useMemo(
    () => ({ chats, refreshChatList }),
    [chats, refreshChatList]
  );

  return (
    <ChatListContext.Provider value={value}>
      {children}
    </ChatListContext.Provider>
  );
}
