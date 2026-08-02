"use client";
// frontend/app/context/WorkspacesContext.jsx
//
// Audit §2.1 / Item 2 (remaining work), slice 3 of the "split
// SessionContext into concern-based contexts" fix (slice 1:
// NotificationsContext.jsx, slice 2: UsageStatsContext.jsx).
//
// SCOPE NOTE — read before extending this file: this slice moves the
// `workspaces` array itself and `fetchWorkspaces()` (the only two things
// that actually cause the god-object re-render problem §2.1 describes —
// everything else in the old "§7: workspaces" block of SessionContext.jsx
// is a stateless useCallback wrapping a fetch, not reactive state). The
// ~25 workspace CRUD/membership/voting/attribution functions
// (createWorkspace, renameWorkspace, deleteWorkspace, promoteWorkspace,
// addWorkspaceChat, removeWorkspaceChat, createWorkspaceChat,
// fetchWorkspaceMembers/addWorkspaceMember/etc., fetchWorkspaceVotes/
// castWorkspaceVote, setWorkspaceAttribution/setMemberAttributionGrant,
// exportWorkspace/importWorkspace) DELIBERATELY stayed in
// SessionContext.jsx rather than moving here too. Reason: several of them
// (addWorkspaceChat, removeWorkspaceChat, createWorkspaceChat,
// deleteWorkspace, leaveWorkspaceMembership, importWorkspace) close over
// refreshChatList/chats/sessionId/switchChat/createNewChat — chat/
// messages-concern state and functions that haven't been split out of
// SessionContext.jsx yet (that's the NEXT planned slice). Moving those
// functions here now would mean either (a) this context becoming an
// ancestor of SessionProvider while also needing callbacks threaded UP
// from a descendant, which isn't expressible in React (a provider can't
// consume props from its own descendant), or (b) copying the
// mother/child callback-prop pattern WorkspaceDockContext.jsx already
// uses relative to SessionContext (see that file's header comment) a
// second time, for a context that ALSO needs to sit above SessionProvider
// for a different reason (see next paragraph) — genuinely circular given
// today's boundaries. Revisit this once chat/messages has its own
// context: at that point the coupled functions can move here cleanly,
// taking the chat context's functions as an import instead of a
// same-component closure.
//
// This context has to sit ABOVE SessionProvider (like
// UsageStatsProvider, unlike NotificationsProvider) because
// SessionContext.jsx still reads `workspaces` (getWorkspaceIdForChat)
// and calls `fetchWorkspaces()` (its own mount effect) internally — see
// AppShell.jsx's provider tree.
import { createContext, useContext, useState, useCallback, useMemo } from "react";
import { supabase } from "../lib/supabaseClient";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Duplicated from SessionContext.jsx rather than imported from it, same
// reasoning WorkspaceDockContext.jsx gives for its own copy of this
// function: both read the same env vars via the same shared
// supabaseClient.js singleton, so they can't drift in practice.
async function authHeaders(opts = {}) {
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token;
  const headers = {};
  if (opts.json) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

const WorkspacesContext = createContext(null);

export function useWorkspaces() {
  const ctx = useContext(WorkspacesContext);
  if (!ctx) throw new Error("useWorkspaces() must be used inside <WorkspacesProvider>");
  return ctx;
}

export function WorkspacesProvider({ children }) {
  const [workspaces, setWorkspaces] = useState([]); // NEW — §7: named containers, function like an always-on batch

  // NEW — §7: workspaces ("Projects" in the UI). Mirrors the batch
  // functions' fetchBatches() 1:1. Unchanged from SessionContext.jsx —
  // pure extraction, only the definition site moved.
  const fetchWorkspaces = useCallback(async () => {
    const res = await fetch(`${API_URL}/api/workspaces`, {
      headers: await authHeaders(),
    });
    const body = await res.json();
    // Same guard as fetchChats() in SessionContext.jsx — never let a
    // non-array response (e.g. {"detail": "..."} from an auth/server
    // error) reach GrowthTab.jsx's workspaces.filter() and crash the app.
    if (!res.ok || !Array.isArray(body)) {
      console.error("Failed to load workspaces:", res.status, body);
      setWorkspaces([]);
      return;
    }
    setWorkspaces(body);
  }, []);

  const value = useMemo(
    () => ({ workspaces, fetchWorkspaces }),
    [workspaces, fetchWorkspaces]
  );

  return (
    <WorkspacesContext.Provider value={value}>
      {children}
    </WorkspacesContext.Provider>
  );
}
