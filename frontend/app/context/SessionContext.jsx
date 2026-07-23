"use client";
import { createContext, useContext, useState, useRef, useEffect } from "react";
import { getPusherClient, onPusherConnectionChange } from "../lib/pusherClient";
import { supabase } from "../lib/supabaseClient";
import { useAuth } from "./AuthContext";   // NEW — Part 8.9: notification bell's per-user Pusher channel

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const ACTIVE_CHAT_KEY = "minime_active_chat_id";   // NEW — persists which chat to reopen on refresh

// Part 8.2/8.9: replaces the old static `x-api-key` header everywhere in
// this file. The backend's require_auth() (api/server.py) now verifies a
// real per-user Supabase JWT via `Authorization: Bearer <token>`, not a
// shared secret — every fetch() call below was updated to call this
// instead of sending process.env.NEXT_PUBLIC_API_KEY. Pulls the current
// access_token fresh on every call rather than caching it, since
// supabase-js's client already keeps the in-memory session current
// (including silent refresh) — reading it live here means a call made
// right after a token refresh never races against a stale cached value.
export async function authHeaders(opts = {}) {
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token;
  const headers = {};
  if (opts.json) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

const SessionContext = createContext(null);

export function useSession() {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession() must be used inside <SessionProvider>");
  return ctx;
}

export function SessionProvider({ children }) {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [chats, setChats] = useState([]);                 // NEW — sidebar list
  const [batches, setBatches] = useState([]);              // NEW — §4/§5: memory_batch groups, parallel to `chats`
  const [workspaces, setWorkspaces] = useState([]); // NEW — §7: named containers, function like an always-on batch           
  const [sessionId, setSessionId] = useState(null);        // CHANGED — no longer random-on-mount; this IS chat_id
  const [chatsLoading, setChatsLoading] = useState(true);  // NEW
  const [liveDecision, setLiveDecision] = useState(null);
  // CHANGE — Part 18: liveLanes (object keyed by module name) replaced
  // with liveSteps (an ordered array). Two reasons, both found reading
  // eo/executor.py directly:
  //   1. agent_start/agent_done are keyed by the RESOLVED MODULE name
  //      (e.g. "generic_worker"), not the role. Multiple hired roles
  //      routinely share the same module, so an object keyed by module
  //      name silently overwrote one role's lane with another's.
  //   2. Object state also meant a finished message could never show
  //      its own trace afterward — liveLanes just got reset to {} on
  //      the next sendTask() and was never attached to the message.
  // Array order is safe to rely on here because eo/executor.py's
  // execute_graph() is a strictly sequential `while` loop (confirmed
  // reading it directly) — no two agent_start events are ever "in
  // flight" at once, so "operate on the most recently pushed entry" is
  // always correct and never ambiguous.
  const [liveSteps, setLiveSteps] = useState([]);
  const stepsRef = useRef([]);           // NEW — Part 18: mirrors liveSteps synchronously
  const stepSeq = useRef(0);             // NEW — Part 18: unique id per step, for React keys
  // Bug fix: agent_done/agent_token_chunk/error used to always target
  // "whichever step is structurally last in stepsRef.current" — correct
  // ONLY when steps never nest. That broke the moment a role that spawns
  // its OWN sub-agent events (agents/code_writers.py's per-worker "Code
  // Writer N — module" events, agents/reviewer.py's per-worker "Reviewer
  // N" events) ran: eo/executor.py's outer agent_start for "implementer"
  // pushes a step, then code_writers.py pushes+closes 3 more nested steps
  // WHILE "implementer" is still open, then executor.py's own agent_done
  // for "implementer" arrives last — but by then "array's last element"
  // is the 3rd nested worker's (already-closed) step, so agent_done
  // overwrites THAT instead of the "implementer" step it actually
  // belongs to, leaving "implementer" stuck on status "running" forever
  // (same for "verifier" wrapping reviewer.py's nested Reviewer N steps).
  // Fix: nesting is a call stack, not a flat sequence — track which step
  // ids are still open (LIFO), and match every agent_done/token_chunk/
  // error to the MOST RECENTLY OPENED step that hasn't closed yet (the
  // top of the stack), by id, not by array position.
  const openStepStack = useRef([]);      // NEW — bug fix
  // NEW — captures the new "agent_requested_role" event (an agent asked
  // eo/executor.py to insert a missing prerequisite role and retry — see
  // eo/errors.py). Kept separate from routeTrace since it's a distinct
  // kind of edge (a runtime request, not a dispatcher routing decision),
  // but RoutingTraceGraph.jsx merges both into one picture.
  const [roleRequests, setRoleRequests] = useState([]);
  const roleRequestsRef = useRef([]);
  // NEW — Part 21: mirror routeTrace/dependencyMap/structurePlan the
  // same way stepsRef mirrors liveSteps, so they survive into the
  // per-message snapshot instead of being wiped by the next run's
  // setRouteTrace([]) / setDependencyMap({}) / setStructurePlan(null).
  const routeTraceRef = useRef([]);
  const dependencyMapRef = useRef({});
  const structurePlanRef = useRef(null);
  const [usageStats, setUsageStats] = useState({});
  const [usageHistory, setUsageHistory] = useState({});       // { [statKey]: [{t, tokens}, ...] } — Part 17
  const [combinedUsageHistory, setCombinedUsageHistory] = useState([]); // [{t, [provider]: tokens}, ...] — Part 17
  const latestByProviderRef = useRef({});                       // provider -> summed tokens across its keys, for the combined chart — Part 17
  const [routeTrace, setRouteTrace] = useState([]);
  const [macroLoopDecisions, setMacroLoopDecisions] = useState([]);
  const [dependencyMap, setDependencyMap] = useState({});
  const [structurePlan, setStructurePlan] = useState(null);
  const [mode, setMode] = useState("auto");
  const [pusherConnected, setPusherConnected] = useState(false); // NEW — Settings tab diagnostic, §6
  const [notifications, setNotifications] = useState([]);   // NEW — Part 8.9: newest first
  const [unreadCount, setUnreadCount] = useState(0);          // NEW — Part 8.9
  const [activeMessageIndex, setActiveMessageIndex] = useState(null); // NEW — Part 21: shared scroll-sync index between Chat and Working panels
  // NEW — Part 2 §2.5: gates whether sendTask() calls /api/task directly
  // (today's exact one-click behavior, default) or /api/task/preview
  // first so a human can review/edit the staffed hires before anything
  // dispatches. Per-session, not persisted — a deliberate minority-case
  // toggle per the design doc, not a global setting.
  const [reviewBeforeDispatch, setReviewBeforeDispatch] = useState(false);
  // NEW — Part 2 §2.5: non-null exactly when a preview_task() call
  // returned "preview_ready" and is awaiting HireReviewScreen's
  // confirm/cancel. Holds everything confirmHireReview()/
  // cancelHireReview() need without re-deriving them: the original
  // task text, the decision object (handed back to /api/task/confirm
  // unmodified), and the hires list to render.
  const [pendingHireReview, setPendingHireReview] = useState(null);
  // NEW — Part 2 §2.4/§2.7: non-null exactly when the live run is
  // currently paused at a human-in-the-loop checkpoint. Holds the role
  // name so AgentStepList.jsx/RoutingTraceGraph.jsx know which step to
  // decorate with the "awaiting_approval" status/actions — the actual
  // full output is already sitting on that step from its own agent_done
  // event, this is just the "and now it's paused" flag layered on top.
  const [pausedApproval, setPausedApproval] = useState(null);
  // NEW — Part 2 §2.4/§2.7: {taskText, sessionId} for the run currently
  // paused, so resumeRun() can finalize the assistant message once the
  // human's decision lets the run actually finish. Distinct from
  // pausedApproval (which role is paused) since this survives across
  // possibly several consecutive pauses in the same run.
  const [pausedRun, setPausedRun] = useState(null);

  // NEW — Workflow Templates fix: a template run's {running, result,
  // chatId} keyed by template_id, kept HERE rather than as local state
  // inside WorkflowTemplatesTab/TemplateCard. AppShell fully unmounts
  // the inactive tab's component tree on every tab switch (`<Active />`
  // swaps component identity), so any state that needs to survive a
  // tab switch — same requirement `loading`/`messages` already have for
  // the Chat tab — has to live in SessionProvider, above that boundary,
  // not in the tab component itself. Deliberately does NOT touch
  // `sessionId`/`messages` — a template run happens in its own
  // background chat and must not hijack whatever chat is currently open
  // in the Chat tab.
  const [templateRuns, setTemplateRuns] = useState({});

  // --- NEW: on mount, load the chat list, then restore the last active
  // chat (or create the very first one). This replaces the old
  // `useState(() => "sess_" + ...)` initializer — sessionId is no longer
  // minted randomly on every page load, it's loaded from localStorage /
  // the persisted chat store, which is the actual fix for "everything
  // disappears on refresh" (see guide §0).
  useEffect(() => {
    (async () => {
      const res = await fetch(`${API_URL}/api/chats`, {
        headers: await authHeaders(),
      });
      const body = await res.json();
      // Guard against a non-array response (e.g. an error body like
      // {"detail": "..."} from require_auth()/a 500) ever reaching
      // ChatSidebar.jsx's chats.filter() — fail visibly in the console
      // instead of crashing the whole app on a backend error.
      if (!res.ok || !Array.isArray(body)) {
        console.error("Failed to load chats:", res.status, body);
        setChats([]);
        setChatsLoading(false);
        return;
      }
      const list = body;
      setChats(list);
      fetchBatches();   // NEW — §4: don't block chat restore on this, batches are additive UI
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
      setChatsLoading(false);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Pusher subscription: identical to today's page.js effect, just
  // living up here instead of inside the page that used to render
  // everything. This is the fix described in §1 — this effect now only
  // ever mounts/unmounts with the whole app, never with a tab switch.
  // Note: sessionId now changes value on switchChat()/createNewChat()
  // (it used to only be set once), so this effect — keyed on
  // [sessionId] — correctly unsubscribes from the old chat's channel
  // and subscribes to the new one automatically whenever you switch.
  useEffect(() => {
    if (!sessionId) return;   // NEW — nothing to subscribe to until the first chat is loaded/created
    const pusher = getPusherClient();
    if (!pusher) {
      console.warn("Pusher env vars not set — live agent events disabled.");
      return;
    }
    const channelName = `session-${sessionId.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
    const channel = pusher.subscribe(channelName);
    channel.bind_global((eventType, data) => {
      const { agent, payload } = data;
      if (eventType === "routing_decision") {
        setLiveDecision(payload);
        return;
      }
      if (eventType === "usage_update") {
        const statKey = `${payload?.provider}:${payload?.key_id}`;
        setUsageStats((prev) => ({ ...prev, [statKey]: payload }));

        // NEW — Part 17: append to this key's own history (capped so a
        // very long session doesn't grow this unbounded).
        setUsageHistory((prev) => {
          const series = prev[statKey] || [];
          const next = [...series, { t: Date.now(), tokens: payload?.tokens_used_today ?? 0 }];
          return { ...prev, [statKey]: next.length > 300 ? next.slice(-300) : next };
        });

        // NEW — Part 17: maintain a per-provider running total (summed
        // across every key seen so far for that provider) and append one
        // row to a combined, time-aligned series every update, forward-
        // filling every OTHER provider's last known value so the combined
        // chart has a real value for every provider at every timestamp,
        // not just the one that happened to fire this particular event.
        const provider = payload?.provider;
        if (provider) {
          // Recompute this provider's total from every key of theirs
          // we've seen so far, rather than a running += — a += would
          // double count if this same key's usage_update fires again
          // with a lower number for any reason (shouldn't happen, but
          // recomputing from source is one fewer thing to trust blindly).
          setUsageStats((prevStats) => {
            const total = Object.entries(prevStats)
              .filter(([k]) => k.startsWith(`${provider}:`))
              .reduce((sum, [, v]) => sum + (v.tokens_used_today || 0), 0)
              + (payload?.tokens_used_today || 0); // this event's own key may not be in prevStats yet
            latestByProviderRef.current = { ...latestByProviderRef.current, [provider]: total };
            return prevStats; // this call is read-only against usageStats — the actual write already happened above
          });
          setCombinedUsageHistory((prev) => {
            const row = { t: Date.now(), ...latestByProviderRef.current };
            const next = [...prev, row];
            return next.length > 300 ? next.slice(-300) : next;
          });
        }
        return;
      }
      if (eventType === "dispatch_event") {
        const nextRouteTrace = [...routeTraceRef.current, { destination: payload?.destination, reason: payload?.reason }];
        routeTraceRef.current = nextRouteTrace;
        setRouteTrace(nextRouteTrace);
        return;
      }
      if (eventType === "macro_loop_decision") {
        setMacroLoopDecisions((prev) => [
          ...prev,
          { action: payload?.decision, loop: payload?.loop, cause: payload?.cause },
        ]);
        return;
      }
      if (eventType === "dependency_map") {
        dependencyMapRef.current = payload?.map || {};
        setDependencyMap(dependencyMapRef.current);
        return;
      }
      if (eventType === "structure_plan") {
        structurePlanRef.current = payload?.mermaid || null;
        setStructurePlan(structurePlanRef.current);
        return;
      }
      if (eventType === "quota_alert") {
        console.warn("quota_alert:", payload);
        return;
      }
      if (eventType === "agent_requested_role") {
        // NEW — see eo/executor.py's MissingDependencyError handling.
        const next = [...roleRequestsRef.current, {
          requestingAgent: agent, requestedRole: payload?.requested_role, label: payload?.label,
        }];
        roleRequestsRef.current = next;
        setRoleRequests(next);
        return;
      }
      // CHANGE — Part 18: agent_start/agent_token_chunk/agent_done/error
      // now push/update against liveSteps (array), not a lanes object.
      if (eventType === "agent_start") {
        const step = {
          id: stepSeq.current++,
          agent,                                   // resolved module name (executor.py's current_name)
          role: payload?.label || agent,            // actual role — payload.label per executor.py's emit_event() call
          text: "",
          summary: null,
          image: null,
          durationMs: null,
          status: "running",
        };
        stepsRef.current = [...stepsRef.current, step];
        openStepStack.current = [...openStepStack.current, step.id];   // NEW — bug fix
        setLiveSteps(stepsRef.current);
        return;
      }
      if (eventType === "agent_token_chunk") {
        // §1: not every agent is confirmed to emit this. Steps that
        // never receive a chunk simply fall back to agent_done's
        // summary below.
        if (openStepStack.current.length === 0) return;
        const targetId = openStepStack.current[openStepStack.current.length - 1];   // NEW — bug fix
        const updated = stepsRef.current.map((s) =>
          s.id === targetId ? { ...s, text: s.text + (payload?.text || "") } : s
        );
        stepsRef.current = updated;
        setLiveSteps(updated);
        return;
      }
      if (eventType === "agent_done") {
        if (openStepStack.current.length === 0) return;
        const targetId = openStepStack.current[openStepStack.current.length - 1];   // NEW — bug fix
        openStepStack.current = openStepStack.current.slice(0, -1);                 // NEW — bug fix: pop
        const updated = stepsRef.current.map((s) =>
          s.id === targetId
            ? { ...s, status: "done", summary: payload?.summary, durationMs: payload?.duration_ms, image: payload?.image || null }
            : s
        );
        stepsRef.current = updated;
        setLiveSteps(updated);
        return;
      }
      if (eventType === "error") {
        if (openStepStack.current.length === 0) return;
        const targetId = openStepStack.current[openStepStack.current.length - 1];   // NEW — bug fix
        openStepStack.current = openStepStack.current.slice(0, -1);                 // NEW — bug fix: pop
        const updated = stepsRef.current.map((s) =>
          s.id === targetId ? { ...s, status: "error", summary: payload?.message } : s
        );
        stepsRef.current = updated;
        setLiveSteps(updated);
        return;
      }
      // NEW — Part 2 §2.4/§2.7: eo/executor.py emits this AFTER the
      // role's own normal agent_done (which already closed its step with
      // status "done" and the full output). This just overlays the
      // paused flag on that same step — found by role name, most recent
      // match, since a role can in principle run more than once in a
      // session (recheck/escalate) and it's the LATEST run of it that's
      // actually paused.
      if (eventType === "awaiting_approval") {
        const roleName = payload?.role || payload?.label || agent;
        const idx = [...stepsRef.current].map((s) => s.role).lastIndexOf(roleName);
        if (idx !== -1) {
          const updated = stepsRef.current.map((s, i) => (i === idx ? { ...s, status: "awaiting_approval" } : s));
          stepsRef.current = updated;
          setLiveSteps(updated);
        }
        setPausedApproval({ role: roleName });
        return;
      }
    });
    return () => {
      pusher.unsubscribe(channelName);
    };
  }, [sessionId]);

  // NEW — §2.5: pusherConnected now reflects the shared client's actual
  // connection state (bound once, independent of sessionId/user), rather
  // than each channel effect optimistically flipping it on construction
  // and tearing it down again on every switchChat(). Runs once for the
  // life of the app.
  useEffect(() => {
    const unsubscribe = onPusherConnectionChange((state) => {
      setPusherConnected(state === "connected");
    });
    return unsubscribe;
  }, []);

  // NEW — Part 8.4/8.9: second Pusher subscription, on the user's own
  // channel rather than the current chat's. Deliberately a SEPARATE
  // effect/subscription from the session one above — different channel
  // scheme, different lifecycle (this one only remounts when the signed-
  // in user changes, not on every switchChat()), same "add a scheme
  // alongside, don't touch the existing one" instruction from §8.4.
  const { user } = useAuth();
  useEffect(() => {
    if (!user?.id) return;
    const pusher = getPusherClient();
    if (!pusher) return; // SettingsTab's pusherConnected diagnostic already covers the "not configured" case

    const channelName = `user-${user.id.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
    const channel = pusher.subscribe(channelName);
    channel.bind("notification", (data) => {
      const note = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        kind: data?.payload?.kind,
        payload: data?.payload,
        timestamp: data?.timestamp || new Date().toISOString(),
        read: false,
      };
      setNotifications((prev) => [note, ...prev].slice(0, 50)); // cap, same reasoning usageHistory's 300-cap follows
      setUnreadCount((prev) => prev + 1);
    });
    return () => {
      pusher.unsubscribe(channelName);
    };
  }, [user?.id]);

  function markNotificationsRead() {
    setUnreadCount(0);
    setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
  }

  // --- NEW: chat list + switching / creating / renaming / deleting /
  // linking chats. sessionId and chat_id are the same string everywhere
  // (see eo/chat_store.py's docstring), so these just move sessionId
  // around and keep the persisted chat store + local state in sync.
  // NOTE: SessionContext.jsx itself was not present in the uploaded repo
// (both repomix dumps are backend-only — eo/, api/, utils/). This is the
// runTemplate() function as specified, to paste into your actual
// SessionContext.jsx in place of the current implementation.

async function runTemplate(templateId, taskText) {
  setTemplateRuns((prev) => ({
    ...prev,
    [templateId]: { running: true, result: null, chatId: prev[templateId]?.chatId ?? null },
  }));

  let chatId;
  try {
    const existing = await fetch(`${API_URL}/api/workflow-templates/${templateId}/chat`, {
      headers: await authHeaders(),
    }).then((r) => r.json());
    chatId = existing?.id;
    if (!chatId) {
      const res = await fetch(`${API_URL}/api/chats`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ title: taskText.trim().slice(0, 60) || "Template run", template_id: templateId }),
      });
      chatId = (await res.json()).id;
    }
  } catch (err) {
    setTemplateRuns((prev) => ({
      ...prev,
      [templateId]: { running: false, result: { status: "error", message: `Couldn't create chat: ${err.message || err}` }, chatId: null },
    }));
    return;
  }

  // NEW — show "Open chat" right away, not just once the run finishes.
  setTemplateRuns((prev) => ({ ...prev, [templateId]: { running: true, result: null, chatId } }));

  await persistMessageTo(chatId, { role: "user", text: taskText });
  await refreshChatList();

  try {
    const res = await fetch(`${API_URL}/api/task/from-template`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ template_id: templateId, task_text: taskText, session_id: chatId }),
    });
    const data = await res.json();
    await persistMessageTo(chatId, { role: "assistant", data, task: taskText });
    setTemplateRuns((prev) => ({ ...prev, [templateId]: { running: false, result: data, chatId } }));
  } catch (err) {
    const errData = { status: "error", message: String(err) };
    await persistMessageTo(chatId, { role: "assistant", data: errData, task: taskText });
    setTemplateRuns((prev) => ({ ...prev, [templateId]: { running: false, result: errData, chatId } }));
  }
  await refreshChatList();
}

  async function refreshChatList() {
    const res = await fetch(`${API_URL}/api/chats`, {
      headers: await authHeaders(),
    });
    setChats(await res.json());
  }

  // NEW — step 3e prereq: pure lookup, no state mutation. Same check
  // ChatTab.jsx already did inline to find its activeWorkspace. Exposed
  // here (rather than left duplicated) so WorkspaceDockContext's
  // switchChat/createNewChat/etc. can resolve "which dock key does this
  // chatId belong to" without importing this file or duplicating the
  // `workspaces` state itself — it's passed down as a callback prop
  // instead (see AppShell.jsx's WorkspaceDockBridge).
  function getWorkspaceIdForChat(chatId) {
    const ws = (workspaces || []).find(
      (w) => Array.isArray(w.chat_ids) && w.chat_ids.includes(chatId)
    );
    return ws?.id ?? null;
  }

  // NEW — §4: loads memory_batch groups so the sidebar can render batch
  // sections and the Working Panel can show "sharing memory with..."
  // for the active chat. §5 adds create/rename/unlink/delete on top of
  // this same `batches` state.
  // NEW — §6: repurposes the old LinkChatsModal save flow. Creates a
  // real batch (mutual membership) instead of the old one-directional
  // linkChats() call — see ChatSidebar.jsx's LinkChatsModal.
  async function createBatch(name, memberChatIds) {
    await fetch(`${API_URL}/api/batches`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ name, member_chat_ids: memberChatIds }),
    });
    await fetchBatches();
    await refreshChatList();
  }
  // NEW — §9.2: live estimate for the create-batch modal. Not stored in
// context state — it's ephemeral per-modal-open, computed fresh each
// time the checkbox selection changes.
  async function estimateBatch(chatIds) {
    const res = await fetch(`${API_URL}/api/batches/estimate`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ chat_ids: chatIds }),
    });
    return res.json();
  }
  async function fetchBatches() {
    const res = await fetch(`${API_URL}/api/batches`, {
      headers: await authHeaders(),
    });
    setBatches(await res.json());
  }
  // NEW — §7: workspaces ("Projects" in the UI). Mirrors the batch functions
// above 1:1, with one thing to keep straight: workspaces store members as
// `chat_ids` (see eo/chat_workspace.py), batches use `member_chat_ids` —
// don't cross the two up when reading a response.

async function fetchWorkspaces() {
  const res = await fetch(`${API_URL}/api/workspaces`, {
    headers: await authHeaders(),
  });
  const body = await res.json();
  // Same guard as fetchChats() above — never let a non-array response
  // (e.g. {"detail": "..."} from an auth/server error) reach
  // GrowthTab.jsx's workspaces.filter() and crash the app.
  if (!res.ok || !Array.isArray(body)) {
    console.error("Failed to load workspaces:", res.status, body);
    setWorkspaces([]);
    return;
  }
  setWorkspaces(body);
}

async function createWorkspace(name) {
  const res = await fetch(`${API_URL}/api/workspaces`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const workspace = await res.json();
  await fetchWorkspaces();
  return workspace;
}

async function createWorkspaceWithChats(name, chatIds = []) {
  const workspace = await createWorkspace(name);
  for (const chatId of chatIds) {
    await addWorkspaceChat(workspace.id, chatId);
  }
  return workspace;
}

async function renameWorkspace(wsId, name) {
  await fetch(`${API_URL}/api/workspaces/${wsId}/rename`, {
    method: "PATCH",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ name }),
  });
  await fetchWorkspaces();
}

async function addWorkspaceChat(wsId, chatId) {
  await fetch(`${API_URL}/api/workspaces/${wsId}/chats`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ chat_id: chatId }),
  });
  await fetchWorkspaces();
  await refreshChatList(); // membership changes linked_chat_ids server-side (chat_workspace.py's _sync)
}

async function removeWorkspaceChat(wsId, chatId, deleteChat = false) {
  await fetch(
    `${API_URL}/api/workspaces/${wsId}/chats/${chatId}?delete_chat=${deleteChat}`,
    { method: "DELETE", headers: await authHeaders() }
  );
  await fetchWorkspaces();
  if (deleteChat && chatId === sessionId) {
    // Same "don't strand the user on a chat that no longer exists" logic
    // as deleteChat() below — switchChat()/createNewChat() already
    // refresh the chat list internally.
    const remaining = chats.filter((c) => c.id !== chatId);
    if (remaining.length > 0) await switchChat(remaining[0].id);
    else await createNewChat();
  } else {
    await refreshChatList();
  }
}

async function deleteWorkspace(wsId) {
  await fetch(`${API_URL}/api/workspaces/${wsId}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
  await fetchWorkspaces();
  await refreshChatList();
}

// NEW — §8: advances a workspace along the fixed stage sequence
// (note -> research -> plan -> build -> test -> growth). Defaults to
// the next stage when toStage is omitted, but callers can explicitly
// choose a later stage in the same sequence. Throws on failure (unlike
// the other CRUD functions above, which silently no-op) because a
// rejected promote -- wrong stage order, no edit access -- is
// something the calling button needs to surface, same reasoning already
// used for the Part 8.9 membership functions below.
async function promoteWorkspace(wsId, toStage = null) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/promote`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ to_stage: toStage }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  await fetchWorkspaces();
  return res.json();
}

// --- NEW — Part 8.9: workspace membership, ownership transitions, voting,
// and attribution. Mirrors eo/chat_workspace.py's role model 1:1 (viewer <
// editor < moderator < partner <= owner). Unlike the workspace CRUD
// functions above, these throw on a non-2xx response instead of silently
// no-op'ing: permission edges here are common and expected, and the
// caller (the modal) needs the server's actual detail message to show.
// Members/votes are intentionally NOT stored in `workspaces` state —
// fetched fresh by whichever modal is open, same ephemeral-per-modal
// treatment as estimateBatch() above.

// NEW — Part 8.7: per-workspace backup/restore, using the existing
// GET/POST /api/workspaces/{id}/export|import routes. Same throw-on-
// non-2xx convention as the membership functions below, since
// ManageWorkspaceModal needs a real error message to show on failure.

async function exportWorkspace(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/export`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Export failed (${res.status})`);
  }
  return res.json(); // the manifest itself — caller decides what to do with it (e.g. trigger a download)
}

async function importWorkspace(wsId, manifest) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/import`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ manifest }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Import failed (${res.status})`);
  }
  await fetchWorkspaces();
  await refreshChatList();
  return res.json();
}

async function fetchWorkspaceMembers(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/members`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to load members (${res.status})`);
  }
  return res.json();
}

async function addWorkspaceMember(wsId, email, role = "viewer") {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/members`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ email, role }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to add member (${res.status})`);
  }
  return res.json();
}

async function updateWorkspaceMemberRole(wsId, targetUserId, role) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/members/${targetUserId}`, {
    method: "PATCH",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ role }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to update role (${res.status})`);
  }
  return res.json();
}

async function removeWorkspaceMember(wsId, targetUserId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/members/${targetUserId}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to remove member (${res.status})`);
  }
  return res.json();
}

async function leaveWorkspaceMembership(wsId, successorId = null) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/leave`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ successor_id: successorId }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to leave project (${res.status})`);
  }
  await fetchWorkspaces();
  await refreshChatList();
}

async function forceRemoveOwner(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/owner/remove`, {
    method: "POST",
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to remove owner (${res.status})`);
  }
  const updated = await res.json();
  await fetchWorkspaces();
  return updated;
}

async function fetchWorkspaceVotes(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/votes`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to load vote status (${res.status})`);
  }
  return res.json();
}

async function castWorkspaceVote(wsId, voteTarget = null) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/votes`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ vote_target: voteTarget }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to cast vote (${res.status})`);
  }
  const result = await res.json();
  await fetchWorkspaces(); // a vote may have just resolved ownership — owner_id can change
  return result;
}

async function setWorkspaceAttribution(wsId, show) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/attribution`, {
    method: "PATCH",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ show }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to update attribution setting (${res.status})`);
  }
  const updated = await res.json();
  await fetchWorkspaces();
  return updated;
}

async function setMemberAttributionGrant(wsId, targetUserId, canToggle) {
  const res = await fetch(
    `${API_URL}/api/workspaces/${wsId}/members/${targetUserId}/attribution-grant`,
    {
      method: "PATCH",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ can_toggle: canToggle }),
    }
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to update attribution grant (${res.status})`);
  }
  return res.json();
}

// --- NEW — §4.7: Notebooks tab. A "notebook" is a workspace (§4.3), so
// these all just parameterize the existing /api/workspaces/{ws_id}/...
// surface — no new container concept, matching the domain doc's own
// framing of notebook == workspace_id.

async function fetchWorkspaceNodes(wsId, nodeType) {
  const qs = nodeType ? `?node_type=${encodeURIComponent(nodeType)}` : "";
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/nodes${qs}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return [];
  return res.json();
}

// NEW — §2 fix: deletes a single ingested source/node. Caller is
// responsible for refetching the node list afterward (same pattern
// ingestFile()'s callers already follow via onIngested), since the
// delete endpoint itself only returns {status, id}, not a fresh list.
async function deleteWorkspaceNode(wsId, nodeId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/nodes/${nodeId}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function renameWorkspaceNode(wsId, nodeId, title) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/nodes/${nodeId}/rename`, {
    method: "PATCH",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function fetchGraphEdges(wsId) {
  const res = await fetch(`${API_URL}/api/graph/edges?workspace_id=${encodeURIComponent(wsId)}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return [];
  return res.json();
}

// §3.5 — auto-generates a structured table from a workspace's own
// ingested nodes (agents/note_table_builder.py), instead of the user
// manually pasting a chat run's markdown table output. Throws on
// non-2xx so the caller can surface the server's actual error message
// (e.g. "no ingested sources with content found") rather than silently
// returning nothing.
async function buildExtractionTable(wsId, fieldNames, { nodeType, expanded } = {}) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/table`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({
      field_names: fieldNames,
      node_type: nodeType || null,
      expanded: !!expanded,
    }),
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// Test tab / "simulate" domain — reads back whatever persona roles +
// simulation_synthesizer already wrote to the memory bus for a given
// (finished or in-progress) simulate-domain chat run. Read-only despite
// being a POST, same shape as buildExtractionTable above; see
// api/server.py's get_simulation_results() docstring for why this reads
// the bus instead of wrapping review_aggregator.py.
async function fetchSimulationResults(wsId, sessionId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/simulate`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// Test tab / `personas` sub-tab — thin client for the same Role Library
// store the Role Library panel already reads/writes via GET/PUT/PATCH
// /api/roles (eo/registry.py's list_role_metadata/update_role_prompt/
// set_role_pinned). No new backend surface: `personas` just calls these
// three and filters the result to the "simulate" domain's own role list
// client-side, same "reuse, don't rebuild" reasoning the /simulate
// endpoint itself used for stage_output reads.

async function fetchRoles() {
  const res = await fetch(`${API_URL}/api/roles`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

async function updateRolePrompt(roleName, brief) {
  const res = await fetch(`${API_URL}/api/roles/${encodeURIComponent(roleName)}`, {
    method: "PUT",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ brief }),
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

async function setRolePinned(roleName, pinned) {
  const res = await fetch(`${API_URL}/api/roles/${encodeURIComponent(roleName)}/pin`, {
    method: "PATCH",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ pinned }),
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// Capture — one function per ingestor family (§4.2), all landing through
// write_ingested_source() server-side into the exact same node shape, so
// IngestionDropzone.jsx can treat every one of these identically: call,
// await {node_ids, title}, done.

async function ingestClip(wsId, url) {
  const res = await fetch(`${API_URL}/api/notes/clip`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ url, workspace_id: wsId }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function ingestVideoUrl(wsId, url) {
  const res = await fetch(`${API_URL}/api/notes/video`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ url, workspace_id: wsId }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function ingestFile(wsId, file) {
  const form = new FormData();
  form.append("workspace_id", wsId);
  form.append("file", file);
  const res = await fetch(`${API_URL}/api/notes/import`, {
    method: "POST",
    headers: await authHeaders(),
    body: form,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function ingestPdfFile(wsId, file) {
  const form = new FormData();
  form.append("workspace_id", wsId);
  form.append("file", file);
  const res = await fetch(`${API_URL}/api/notes/pdf`, {
    method: "POST",
    headers: await authHeaders(),
    body: form,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function ingestVoiceFile(wsId, file) {
  const form = new FormData();
  form.append("workspace_id", wsId);
  form.append("file", file);
  const res = await fetch(`${API_URL}/api/notes/voice`, {
    method: "POST",
    headers: await authHeaders(),
    body: form,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

// Organize — on-demand rescans (§4.3), same "candidate, not auto-applied"
// posture as note-candidates below.

async function detectBacklinks(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/backlinks/detect`, {
    method: "POST",
    headers: await authHeaders(),
  });
  return res.json();
}

// Silent note-taking agent candidates (§4.6) — never auto-committed;
// accept/reject here is the review step Definition-of-Done #6 requires.

async function fetchNoteCandidates(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/notes/candidates`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return [];
  return res.json();
}

async function acceptNoteCandidate(wsId, index) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/notes/candidates/${index}/accept`, {
    method: "POST",
    headers: await authHeaders(),
  });
  return res.json();
}

async function rejectNoteCandidate(wsId, index) {
  await fetch(`${API_URL}/api/workspaces/${wsId}/notes/candidates/${index}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
}

// Workspace facts (eo/workspace_facts.py, Part 0 §0.3) — durable
// brand_voice/target_user/tech_stack/custom facts for a workspace, plus
// the agent-proposed candidates queue. Same accept/reject shape as the
// note candidates just above (workspace_facts.py's accept_candidate/
// reject_candidate take a plain list index, not an id).

async function fetchWorkspaceFacts(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/facts`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return { brand_voice: "", target_user: "", tech_stack: [], custom: {} };
  return res.json();
}

async function saveWorkspaceFacts(wsId, facts) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/facts`, {
    method: "PUT",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify(facts),
  });
  return res.json();
}

async function fetchFactCandidates(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/facts/candidates`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return [];
  return res.json();
}

async function acceptFactCandidate(wsId, index) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/facts/candidates/${index}/accept`, {
    method: "POST",
    headers: await authHeaders(),
  });
  return res.json();
}

async function rejectFactCandidate(wsId, index) {
  await fetch(`${API_URL}/api/workspaces/${wsId}/facts/candidates/${index}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
}

// Generic paste-panel content (eo/panel_content.py) — backs Mind Map,
// Study (flashcards/quiz/study guide), and the other "paste the chat's
// output into a box" panels in NotebooksTab.jsx. Same fetch/save shape
// as workspace facts above; panelKey must be one of
// eo/panel_content.py's VALID_PANEL_KEYS (e.g. "mindmap",
// "study_flashcards", "study_quiz", "study_guide", "prd", ...).

async function fetchPanelContent(wsId, panelKey) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/panels/${panelKey}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return { workspace_id: wsId, panel_key: panelKey, content: "", updated_at: null, updated_by: null };
  return res.json();
}

async function savePanelContent(wsId, panelKey, content) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/panels/${panelKey}`, {
    method: "PUT",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ content }),
  });
  return res.json();
}
// Device spec (Blueprint sub-tab: Parts/Wiring/Mech/Instructions) --
// agents/hardware_speccer.py's output, persisted as four keys under
// eo/workspace_facts.py's per-workspace `custom` dict rather than through
// eo/panel_content.py -- panel_content is for opaque pasted text (one
// `content` string), and Blueprint has real structure plus (for
// Instructions) per-step mutation, which that shape doesn't fit. See
// api/server.py's GET/PATCH .../device-spec... routes.

async function fetchDeviceSpec(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/device-spec`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    return {
      parts: [],
      wiring: { nodes: [], edges: [] },
      mech: { enclosure: { w: 0, h: 0, d: 0 }, placements: [] },
      instructions: { phases: [] },
    };
  }
  return res.json();
}

// PartsTable.jsx's "Refresh prices" button. Unlike fetchPanelContent's
// no-args-needed GET, this one has to send the CURRENT parts list in the
// body -- api/server.py's refresh-prices endpoint re-prices exactly the
// parts it's handed rather than re-reading a stored spec, so BlueprintView
// must pass spec.parts through here, not just a workspace id.
async function refreshPartPrices(wsId, parts) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/parts/refresh-prices`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ parts, force_refresh: true }),
  });
  if (!res.ok) return { parts };  // degrade to the unchanged list rather than throwing
  const data = await res.json();
  return data.parts;
}

async function toggleInstructionStep(wsId, stepId, done) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/device-spec/instructions/steps/${stepId}`, {
    method: "PATCH",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ done }),
  });
  return res.json();
}

// Note clustering (agents/note_clusterer.py, Part 4 §4.3) — deterministic
// KMeans over each node's existing embedding, proposed as accept/reject
// candidates (never auto-applied). Unlike facts/notes candidates,
// note_clusterer.py's candidates are keyed by candidate_id, not list
// index, and propose_clusters() is an explicit rescan (like backlink
// detection), not a passive fetch — see NotebooksTab.jsx's "Detect
// clusters" button.

async function proposeClusters(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/clusters/propose`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
  });
  const data = await res.json();
  return data.candidates;
}

async function fetchClusterCandidates(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/clusters/candidates`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return [];
  return res.json();
}

async function acceptClusterCandidate(wsId, candidateId) {
  const res = await fetch(
    `${API_URL}/api/workspaces/${wsId}/clusters/candidates/${encodeURIComponent(candidateId)}/accept`,
    { method: "POST", headers: await authHeaders({ json: true }) }
  );
  return res.json(); // { edges_created: [...] }
}

async function rejectClusterCandidate(wsId, candidateId) {
  await fetch(
    `${API_URL}/api/workspaces/${wsId}/clusters/candidates/${encodeURIComponent(candidateId)}`,
    { method: "DELETE", headers: await authHeaders() }
  );
}

// §4.7 — "click a mind-map node, open a scoped sub-chat": creates a new
// chat, folds it into this notebook's workspace (so it shares memory
// with the rest of the notebook and shows up under it in the sidebar),
// then dispatches taskText as its first message. Returns the new
// chat_id so the caller (NotebooksTab) can hand off to AppShell's
// openChat() to actually land on it.
// NEW — Part 4 §4.4: podcast synthesis (agents/tts_synthesizer.py). Unlike
// every other helper in this file, POST /api/notes/podcast/synthesize
// returns a FileResponse (raw mp3 bytes), not JSON — so this reads the
// response as a blob and hands back an object URL for an <audio> element,
// rather than calling res.json() like gradeQuiz() etc. below. The call is
// synchronous server-side (no job/poll pattern — synthesize_podcast()
// blocks on edge-tts for the whole script), so callers should show a
// loading state for the duration of this await rather than expecting a
// fast round trip.
async function synthesizePodcast(scriptText, title) {
  const res = await fetch(`${API_URL}/api/notes/podcast/synthesize`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ script_text: scriptText, title: title || "podcast" }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Podcast synthesis failed (${res.status})`);
  }
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

// NEW — Part 4 §4.4: Video Overview (agents/video_overview_builder.py).
// Same raw-file-response shape as synthesizePodcast() above (blob → object
// URL, not JSON), and the same synchronous-server-side caveat — moviepy's
// write_videofile() blocks for the whole render, so callers should show a
// loading state for the duration of this await. Requires podcastTitle to
// match a title already used in a prior synthesizePodcast() call for this
// notebook: the backend locates that mp3 on disk by slugified filename
// rather than re-synthesizing it, and 404s with a clear message if it
// isn't there yet.
async function buildVideoOverview(slideText, podcastTitle, title) {
  const res = await fetch(`${API_URL}/api/notes/video-overview/build`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({
      slide_text: slideText,
      podcast_title: podcastTitle || "podcast",
      title: title || "video_overview",
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Video overview build failed (${res.status})`);
  }
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

async function gradeQuiz(quizText, answers) {
  const res = await fetch(`${API_URL}/api/notes/study/quiz/grade`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ quiz_text: quizText, answers }),
  });
  return res.json();
}

async function recordQuizAttempt(wsId, quizNodeId, quizText, answers) {
  const res = await fetch(`${API_URL}/api/notes/study/quiz/attempts`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ workspace_id: wsId, quiz_node_id: quizNodeId, quiz_text: quizText, answers }),
  });
  return res.json();
}

async function fetchMissedQuestions(wsId, quizNodeId) {
  const res = await fetch(
    `${API_URL}/api/notes/study/quiz/missed?workspace_id=${encodeURIComponent(wsId)}&quiz_node_id=${encodeURIComponent(quizNodeId)}`,
    { headers: await authHeaders() }
  );
  if (!res.ok) return [];
  return res.json();
}

// NEW — Part 8.6: audit log reads. Throws (rather than the silent
// empty-array pattern used by e.g. fetchClusterCandidates) because a 403
// here means "you're not owner/partner" — a real, distinct state the UI
// needs to show, not "there's nothing to show yet."
async function fetchWorkspaceAudit(wsId, limit = 100) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/audit?limit=${limit}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to load audit log (${res.status})`);
  }
  return res.json();
}

async function fetchMyAudit(limit = 100) {
  const res = await fetch(`${API_URL}/api/audit/me?limit=${limit}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to load your activity (${res.status})`);
  }
  return res.json();
}

// Was: createNewChat() then addWorkspaceChat() — two round trips where the
// second one always immediately followed the first. Swapped for the
// one-step backend endpoint built for exactly this (api/server.py's
// POST /api/workspaces/{ws_id}/chats/create). Same local-state side effects
// as createNewChat() (sessionId, ACTIVE_CHAT_KEY, messages) plus the
// workspace-list refresh addWorkspaceChat used to do, just in one fetch.
async function createWorkspaceChat(wsId, title = "New Chat") {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/chats/create`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error("Failed to create workspace chat");
  const chat = await res.json();
  setSessionId(chat.id);
  localStorage.setItem(ACTIVE_CHAT_KEY, chat.id);
  setMessages([]);
  await fetchWorkspaces();   // membership changed server-side
  await refreshChatList();
  return chat.id;
}

async function openScopedSubChat(wsId, taskText) {
  const chatId = await createWorkspaceChat(wsId);
  await sendTask(taskText);
  return chatId;
}

  // NEW — §5: manage-batch modal actions (rename / unlink members /
  // delete the whole batch). All three touch batch membership, which
  // also changes linked_chat_ids server-side (see eo/memory_batch.py),
  // so each refreshes both `batches` and `chats` the same way §3/§4's
  // create flow already does.

  async function renameBatch(batchId, name) {
    await fetch(`${API_URL}/api/batches/${batchId}/rename`, {
      method: "PATCH",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ name }),
    });
    await fetchBatches();
  }

  async function unlinkBatchMembers(batchId, chatIds) {
    await fetch(`${API_URL}/api/batches/${batchId}/unlink`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ chat_ids: chatIds }),
    });
    await fetchBatches();
    await refreshChatList();
  }

  async function deleteBatch(batchId) {
    await fetch(`${API_URL}/api/batches/${batchId}`, {
      method: "DELETE",
      headers: await authHeaders(),
    });
    await fetchBatches();
    await refreshChatList();
  }

  async function switchChat(chatId, { skipListReload = false } = {}) {
    const res = await fetch(`${API_URL}/api/chats/${chatId}`, {
      headers: await authHeaders(),
    });
    if (!res.ok) return;
    const chat = await res.json();
    setSessionId(chatId);
    localStorage.setItem(ACTIVE_CHAT_KEY, chatId);
    setMessages(chat.messages || []);
    // Clear transient Working Panel state — it belongs to whatever run is
    // in flight, not to a chat you just reloaded from disk.
    stepsRef.current = []; setLiveSteps([]);
    routeTraceRef.current = []; setRouteTrace([]);
    dependencyMapRef.current = {}; setDependencyMap({});
    structurePlanRef.current = null; setStructurePlan(null);
    roleRequestsRef.current = []; setRoleRequests([]);
    setMacroLoopDecisions([]);
    setLiveDecision(null);
    if (!skipListReload) await refreshChatList();
  }

  async function createNewChat() {
    const res = await fetch(`${API_URL}/api/chats`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ title: "New Chat" }),
    });
    const chat = await res.json();
    setSessionId(chat.id);
    localStorage.setItem(ACTIVE_CHAT_KEY, chat.id);
    setMessages([]);
    await refreshChatList();
    return chat.id;
  }

  async function renameChat(chatId, title) {
    await fetch(`${API_URL}/api/chats/${chatId}/rename`, {
      method: "PATCH",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ title }),
    });
    await refreshChatList();
  }

  async function deleteChat(chatId) {
    await fetch(`${API_URL}/api/chats/${chatId}`, {
      method: "DELETE",
      headers: await authHeaders(),
    });
    if (chatId === sessionId) {
      const remaining = chats.filter((c) => c.id !== chatId);
      if (remaining.length > 0) await switchChat(remaining[0].id);
      else await createNewChat();
    } else {
      await refreshChatList();
    }
  }

  async function linkChats(chatId, linkedChatIds) {
    await fetch(`${API_URL}/api/chats/${chatId}/links`, {
      method: "PATCH",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ linked_chat_ids: linkedChatIds }),
    });
    await refreshChatList();
  }

  async function persistMessage(message) {
    // Fire-and-forget-ish: don't block the UI on this, but don't swallow
    // errors silently either — a failed save here is exactly the "lost
    // my chat" bug again, just moved one layer down.
    try {
      await fetch(`${API_URL}/api/chats/${sessionId}/messages`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ message }),
      });
    } catch (err) {
      console.error("Failed to persist message:", err);
    }
  }

  // NEW — Workflow Templates fix. createNewChat()/persistMessage() above
  // both act on the CURRENTLY ACTIVE chat (they read/write `sessionId`),
  // which is exactly right for the Chat tab's own compose bar but wrong
  // here — running a template must not silently swap out whatever chat
  // the person currently has open. These two are the same two API calls,
  // parameterized by an explicit chatId instead of the active sessionId.
  async function createChatSilently(title) {
    const res = await fetch(`${API_URL}/api/chats`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ title: title || "New Chat" }),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const chat = await res.json();
    return chat.id;
  }

  async function persistMessageTo(chatId, message) {
    try {
      await fetch(`${API_URL}/api/chats/${chatId}/messages`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ message }),
      });
    } catch (err) {
      console.error("Failed to persist message:", err);
    }
  }

  // NEW — Workflow Templates fix. Mirrors sendTask()'s dispatch +
  // persistence shape, but:
  //   1. State lives in `templateRuns` (this provider), not inside the
  //      tab component — see that state's own comment above for why.
  //   2. Creates its OWN chat via createChatSilently() instead of
  //      reusing `sessionId`/persistMessage(), and never calls
  //      setSessionId/setMessages — so a template run always becomes a
  //      real, findable entry in the chat sidebar (fixes "I can't find
  //      it anywhere"), without ever touching whatever chat is
  //      currently open in the Chat tab.
  //   3. Passes that new chat's id as `session_id` on
  //      /api/task/from-template, so the backend's Pusher events and
  //      any approval_roles pause land on the same channel/session the
  //      chat now represents, exactly like a normal /api/task run.
  //   4. Stores the resulting chatId in templateRuns so the UI can
  //      offer a real "Open chat" action instead of an inert session_id
  //      string.
  async function runTemplate(templateId, taskText) {
    setTemplateRuns((prev) => ({
      ...prev,
      [templateId]: { running: true, result: null, chatId: prev[templateId]?.chatId ?? null },
    }));

    let chatId;
    try {
      chatId = await createChatSilently(taskText.trim().slice(0, 60) || "Template run");
    } catch (err) {
      setTemplateRuns((prev) => ({
        ...prev,
        [templateId]: { running: false, result: { status: "error", message: `Couldn't create chat: ${err.message || err}` }, chatId: null },
      }));
      return;
    }

    await persistMessageTo(chatId, { role: "user", text: taskText });
    await refreshChatList();   // shows up in the sidebar right away, not just once the run finishes

    try {
      const res = await fetch(`${API_URL}/api/task/from-template`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ template_id: templateId, task_text: taskText, session_id: chatId }),
      });
      const data = await res.json();
      const assistantMessage = { role: "assistant", data, task: taskText };
      await persistMessageTo(chatId, assistantMessage);
      setTemplateRuns((prev) => ({ ...prev, [templateId]: { running: false, result: data, chatId } }));
    } catch (err) {
      const errData = { status: "error", message: String(err) };
      await persistMessageTo(chatId, { role: "assistant", data: errData, task: taskText });
      setTemplateRuns((prev) => ({ ...prev, [templateId]: { running: false, result: errData, chatId } }));
    }
    await refreshChatList();
  }

  // Part 2 §2.5 — pulled out of sendTask() so confirmHireReview() (below)
  // can reset the exact same live-run state a normal dispatch does; a
  // confirmed hire review is starting a real run just as much as a
  // one-click sendTask() call is.
  function _resetLiveRunState() {
    setLiveDecision(null);
    stepsRef.current = [];
    setLiveSteps([]);
    openStepStack.current = [];
    roleRequestsRef.current = [];
    setRoleRequests([]);
    routeTraceRef.current = [];
    setRouteTrace([]);
    dependencyMapRef.current = {};
    setDependencyMap({});
    structurePlanRef.current = null;
    setStructurePlan(null);
    setMacroLoopDecisions([]);
  }

  // Part 2 §2.5 — same reasoning as the Part 18/21 comments this
  // replaces: snapshot from the refs (not the stale-closure state vars)
  // so the message carries its own self-contained Working Panel section,
  // whether it came from sendTask()'s direct path or confirmHireReview()'s
  // post-review dispatch.
  function _buildAssistantMessage(taskText, data) {
    return {
      role: "assistant",
      data,
      task: taskText,
      steps: stepsRef.current,
      routeTrace: routeTraceRef.current,
      roleRequests: roleRequestsRef.current,
      dependencyMap: dependencyMapRef.current,
      structurePlan: structurePlanRef.current,
    };
  }

  async function sendTask(taskText) {
    const userMessage = { role: "user", text: taskText };   // CHANGED — named so it can be persisted below
    setMessages((prev) => [...prev, userMessage]);
    persistMessage(userMessage);   // NEW
    setLoading(true);
    _resetLiveRunState();

    // Part 2 §2.5: reviewBeforeDispatch is off by default (today's exact
    // one-click behavior, unchanged) — most tasks should stay one-click,
    // this is only for the minority of cases a user has explicitly opted
    // into reviewing hires first.
    if (reviewBeforeDispatch) {
      try {
        const res = await fetch(`${API_URL}/api/task/preview`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
          body: JSON.stringify({ task_text: taskText, session_id: sessionId, mode }),
        });
        const data = await res.json();
        if (data.status === "preview_ready") {
          // Nothing has run yet — stash it and hand off to
          // HireReviewScreen via confirmHireReview()/cancelHireReview().
          // loading stays true: the run genuinely hasn't finished, it's
          // just paused on a human decision instead of agent work.
          setPendingHireReview({
            taskText,
            sessionId: data.session_id,
            decision: data.decision,
            hires: data.result?.hires || [],
          });
          setLoading(false);
          return;
        }
        // Every other status (cache/sga/tier-0/1/needs_*/hires-empty
        // tier-2/3) is a genuinely finished response, identical in shape
        // to what /api/task would have returned — handle it exactly like
        // the non-preview path below.
        const assistantMessage = _buildAssistantMessage(taskText, data);
        setMessages((prev) => [...prev, assistantMessage]);
        persistMessage(assistantMessage);
        setLoading(false);
      } catch (err) {
        const assistantMessage = _buildAssistantMessage(taskText, { status: "error", message: String(err) });
        setMessages((prev) => [...prev, assistantMessage]);
        persistMessage(assistantMessage);
        setLoading(false);
      }
      return;
    }

    try {
      const res = await fetch(`${API_URL}/api/task`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ task_text: taskText, session_id: sessionId, mode }),
      });
      const data = await res.json();
      // Part 2 §2.4/§2.7: post_task() blocks synchronously until either
      // finished or paused at an approval_roles checkpoint, so a
      // "paused" status can come back on this very first response.
      // The Pusher awaiting_approval event has already updated liveSteps
      // by the time this resolves — just remember what's needed to
      // finalize the message once resumeRun() eventually finishes it,
      // and leave `loading` true (the run genuinely isn't done).
      if (data.status === "paused") {
        setPausedRun({ taskText, sessionId: data.session_id || sessionId });
        return;
      }
      const assistantMessage = _buildAssistantMessage(taskText, data);
      setMessages((prev) => [...prev, assistantMessage]);
      persistMessage(assistantMessage);   // NEW
      setLoading(false);
    } catch (err) {
      const assistantMessage = _buildAssistantMessage(taskText, { status: "error", message: String(err) });
      setMessages((prev) => [...prev, assistantMessage]);
      persistMessage(assistantMessage);   // NEW
      setLoading(false);
    }
  }

  // Part 2 §2.4/§2.7 — resolves the checkpoint AgentStepList.jsx's
  // approval actions raised. `decision` is {action: "approve"|"edit"|
  // "reject_redo", text?}, passed straight through to POST /api/resume.
  // A "paused" result means the run hit ANOTHER approval_roles role
  // further down the pipeline — the Pusher awaiting_approval event for
  // that new role has already updated liveSteps/pausedApproval, so this
  // just leaves `loading`/`pausedRun` as they are and returns. Anything
  // else (finished or errored) finalizes the message exactly like
  // sendTask()'s own direct-dispatch path.
  async function resumeRun(decision) {
    if (!pausedRun) return;
    try {
      const res = await fetch(`${API_URL}/api/resume`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ session_id: pausedRun.sessionId, ...decision }),
      });
      const data = await res.json();
      setPausedApproval(null);
      if (data.status === "paused") return;
      const assistantMessage = _buildAssistantMessage(pausedRun.taskText, data);
      setMessages((prev) => [...prev, assistantMessage]);
      persistMessage(assistantMessage);
      setLoading(false);
      setPausedRun(null);
    } catch (err) {
      const assistantMessage = _buildAssistantMessage(pausedRun.taskText, { status: "error", message: String(err) });
      setMessages((prev) => [...prev, assistantMessage]);
      persistMessage(assistantMessage);
      setPausedApproval(null);
      setLoading(false);
      setPausedRun(null);
    }
  }

  // Part 2 §2.5 — HireReviewScreen's "Confirm & Run" calls this with its
  // edited hires array ({role, agent_key, brief, update_library}[]).
  // Dispatches straight through /api/task/confirm — no second
  // staff_task() call — then finishes the run exactly like sendTask()'s
  // direct path (same message shape, same live-state reset).
  async function confirmHireReview(editedHires) {
    if (!pendingHireReview) return;
    const { taskText, sessionId: reviewSessionId, decision } = pendingHireReview;
    setLoading(true);
    _resetLiveRunState();
    try {
      const res = await fetch(`${API_URL}/api/task/confirm`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({
          task_text: taskText,
          decision,
          hires: editedHires,
          session_id: reviewSessionId,
          mode,
        }),
      });
      const data = await res.json();
      const assistantMessage = _buildAssistantMessage(taskText, data);
      setMessages((prev) => [...prev, assistantMessage]);
      persistMessage(assistantMessage);
    } catch (err) {
      const assistantMessage = _buildAssistantMessage(taskText, { status: "error", message: String(err) });
      setMessages((prev) => [...prev, assistantMessage]);
      persistMessage(assistantMessage);
    } finally {
      setLoading(false);
      setPendingHireReview(null);
    }
  }

  // Part 2 §2.5 — HireReviewScreen's "Cancel". Nothing was ever
  // dispatched (preview_task() stopped before execute_graph()/
  // run_with_looping()), so there's no run to tear down — just drop the
  // pending review. The user's message stays in the transcript with no
  // assistant reply, the same way a "needs_app"/"needs_directed_task_type"
  // response leaves an unanswered turn today.
  function cancelHireReview() {
    setPendingHireReview(null);
  }

  async function registerProject() {
    const path = prompt("Full path to the project folder:");
    const name = prompt("Display name for this project:");
    if (!path || !name) return;
    try {
      const res = await fetch(`${API_URL}/api/projects`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ path, display_name: name }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(`Registration failed: ${data.detail || res.status}`);
        return;
      }
      alert(`Registered as '${data.unique_name}' -> ${data.root_path}`);
    } catch (err) {
      alert(`Registration failed: ${String(err)}`);
    }
  }

  const value = {
  sessionId, API_URL,
  messages, loading,
  chats, chatsLoading, switchChat, createNewChat, renameChat, deleteChat, linkChats,
  refreshChatList, getWorkspaceIdForChat,   // NEW — step 3e prereq: threaded into WorkspaceDockProvider as props
  batches, fetchBatches,
  createBatch, estimateBatch,
  renameBatch, unlinkBatchMembers, deleteBatch,
  workspaces, fetchWorkspaces, createWorkspace, createWorkspaceWithChats, renameWorkspace,
  addWorkspaceChat, createWorkspaceChat, removeWorkspaceChat, deleteWorkspace, promoteWorkspace,   // NEW — §7 / §8
  // NEW — Part 8.9: workspace membership, ownership, voting, attribution
  fetchWorkspaceMembers, addWorkspaceMember, updateWorkspaceMemberRole,
  removeWorkspaceMember, leaveWorkspaceMembership, forceRemoveOwner,
  fetchWorkspaceVotes, castWorkspaceVote,
  setWorkspaceAttribution, setMemberAttributionGrant,
  liveDecision, liveSteps, usageStats, usageHistory, combinedUsageHistory, routeTrace, dependencyMap, structurePlan,
  macroLoopDecisions,
  roleRequests,
  mode, setMode,
  pusherConnected,
  notifications, unreadCount, markNotificationsRead,   // NEW — Part 8.9
  exportWorkspace, importWorkspace,                       // NEW — Part 8.7
  activeMessageIndex, setActiveMessageIndex,
  sendTask, registerProject,
  // NEW — Part 2 §2.5: manual role editing before dispatch
  reviewBeforeDispatch, setReviewBeforeDispatch,
  pendingHireReview, confirmHireReview, cancelHireReview,
  // NEW — Part 2 §2.4/§2.7: human-in-the-loop checkpoints
  pausedApproval, resumeRun,
  // NEW — Workflow Templates fix: survives tab switches, see
  // templateRuns' own comment above.
  templateRuns, runTemplate,
  // NEW — §4.7: Notebooks tab
  fetchWorkspaceNodes, deleteWorkspaceNode, fetchGraphEdges, buildExtractionTable,
  fetchSimulationResults,   // NEW — Test tab: reads simulate-domain stage_output back off the bus
  fetchRoles, updateRolePrompt, setRolePinned,   // NEW — Test tab `personas`: thin client over the Role Library store
  ingestClip, ingestVideoUrl, ingestFile, ingestPdfFile, ingestVoiceFile,
  detectBacklinks,
  fetchNoteCandidates, acceptNoteCandidate, rejectNoteCandidate,
  fetchWorkspaceFacts, saveWorkspaceFacts, fetchFactCandidates, acceptFactCandidate, rejectFactCandidate,
  fetchPanelContent, savePanelContent,   // NEW — generic paste-panel persistence (eo/panel_content.py)
  fetchDeviceSpec, refreshPartPrices, toggleInstructionStep, // NEW — Blueprint (Plan sub-tab)
  proposeClusters, fetchClusterCandidates, acceptClusterCandidate, rejectClusterCandidate,
  openScopedSubChat,
  gradeQuiz, recordQuizAttempt, fetchMissedQuestions,
  synthesizePodcast,   // NEW — Part 4 §4.4: podcast synthesis
  buildVideoOverview,   // NEW — Part 4 §4.4: video overview (narrated slideshow)
  fetchWorkspaceAudit, fetchMyAudit,   // NEW — Part 8.6: audit log
  };
  return (
    <SessionContext.Provider value={value}>
      {children}
    </SessionContext.Provider>
  );
}