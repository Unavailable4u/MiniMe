"use client";
import { createContext, useContext, useState, useRef, useEffect } from "react";
import Pusher from "pusher-js";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const ACTIVE_CHAT_KEY = "minime_active_chat_id";   // NEW — persists which chat to reopen on refresh

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

  // --- NEW: on mount, load the chat list, then restore the last active
  // chat (or create the very first one). This replaces the old
  // `useState(() => "sess_" + ...)` initializer — sessionId is no longer
  // minted randomly on every page load, it's loaded from localStorage /
  // the persisted chat store, which is the actual fix for "everything
  // disappears on refresh" (see guide §0).
  useEffect(() => {
    (async () => {
      const res = await fetch(`${API_URL}/api/chats`, {
        headers: { "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
      });
      const list = await res.json();
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
    const key = process.env.NEXT_PUBLIC_PUSHER_KEY;
    const cluster = process.env.NEXT_PUBLIC_PUSHER_CLUSTER;
    if (!key || !cluster) {
      console.warn("Pusher env vars not set — live agent events disabled.");
      return;
    }
    const pusher = new Pusher(key, { cluster });
    setPusherConnected(true);
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
            ? { ...s, status: "done", summary: payload?.summary, durationMs: payload?.duration_ms }
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
      pusher.disconnect();
      setPusherConnected(false);
    };
  }, [sessionId]);

  // --- NEW: chat list + switching / creating / renaming / deleting /
  // linking chats. sessionId and chat_id are the same string everywhere
  // (see eo/chat_store.py's docstring), so these just move sessionId
  // around and keep the persisted chat store + local state in sync.

  async function refreshChatList() {
    const res = await fetch(`${API_URL}/api/chats`, {
      headers: { "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
    });
    setChats(await res.json());
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
      headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
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
      headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
      body: JSON.stringify({ chat_ids: chatIds }),
    });
    return res.json();
  }
  async function fetchBatches() {
    const res = await fetch(`${API_URL}/api/batches`, {
      headers: { "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
    });
    setBatches(await res.json());
  }
  // NEW — §7: workspaces ("Projects" in the UI). Mirrors the batch functions
// above 1:1, with one thing to keep straight: workspaces store members as
// `chat_ids` (see eo/chat_workspace.py), batches use `member_chat_ids` —
// don't cross the two up when reading a response.

async function fetchWorkspaces() {
  const res = await fetch(`${API_URL}/api/workspaces`, {
    headers: { "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
  });
  setWorkspaces(await res.json());
}

async function createWorkspace(name) {
  await fetch(`${API_URL}/api/workspaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
    body: JSON.stringify({ name }),
  });
  await fetchWorkspaces();
}

async function renameWorkspace(wsId, name) {
  await fetch(`${API_URL}/api/workspaces/${wsId}/rename`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
    body: JSON.stringify({ name }),
  });
  await fetchWorkspaces();
}

async function addWorkspaceChat(wsId, chatId) {
  await fetch(`${API_URL}/api/workspaces/${wsId}/chats`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
    body: JSON.stringify({ chat_id: chatId }),
  });
  await fetchWorkspaces();
  await refreshChatList(); // membership changes linked_chat_ids server-side (chat_workspace.py's _sync)
}

async function removeWorkspaceChat(wsId, chatId, deleteChat = false) {
  await fetch(
    `${API_URL}/api/workspaces/${wsId}/chats/${chatId}?delete_chat=${deleteChat}`,
    { method: "DELETE", headers: { "x-api-key": process.env.NEXT_PUBLIC_API_KEY } }
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
    headers: { "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
  });
  await fetchWorkspaces();
  await refreshChatList();
}

  // NEW — §5: manage-batch modal actions (rename / unlink members /
  // delete the whole batch). All three touch batch membership, which
  // also changes linked_chat_ids server-side (see eo/memory_batch.py),
  // so each refreshes both `batches` and `chats` the same way §3/§4's
  // create flow already does.

  async function renameBatch(batchId, name) {
    await fetch(`${API_URL}/api/batches/${batchId}/rename`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
      body: JSON.stringify({ name }),
    });
    await fetchBatches();
  }

  async function unlinkBatchMembers(batchId, chatIds) {
    await fetch(`${API_URL}/api/batches/${batchId}/unlink`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
      body: JSON.stringify({ chat_ids: chatIds }),
    });
    await fetchBatches();
    await refreshChatList();
  }

  async function deleteBatch(batchId) {
    await fetch(`${API_URL}/api/batches/${batchId}`, {
      method: "DELETE",
      headers: { "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
    });
    await fetchBatches();
    await refreshChatList();
  }

  async function switchChat(chatId, { skipListReload = false } = {}) {
    const res = await fetch(`${API_URL}/api/chats/${chatId}`, {
      headers: { "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
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
      headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
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
      headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
      body: JSON.stringify({ title }),
    });
    await refreshChatList();
  }

  async function deleteChat(chatId) {
    await fetch(`${API_URL}/api/chats/${chatId}`, {
      method: "DELETE",
      headers: { "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
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
      headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
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
        headers: { "Content-Type": "application/json", "x-api-key": process.env.NEXT_PUBLIC_API_KEY },
        body: JSON.stringify({ message }),
      });
    } catch (err) {
      console.error("Failed to persist message:", err);
    }
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
          headers: {
            "Content-Type": "application/json",
            "x-api-key": process.env.NEXT_PUBLIC_API_KEY,
          },
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
        headers: {
          "Content-Type": "application/json",
          "x-api-key": process.env.NEXT_PUBLIC_API_KEY,
        },
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
        headers: {
          "Content-Type": "application/json",
          "x-api-key": process.env.NEXT_PUBLIC_API_KEY,
        },
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
        headers: {
          "Content-Type": "application/json",
          "x-api-key": process.env.NEXT_PUBLIC_API_KEY,
        },
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
        headers: {
          "Content-Type": "application/json",
          "x-api-key": process.env.NEXT_PUBLIC_API_KEY,
        },
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
  batches, fetchBatches,
  createBatch, estimateBatch,
  renameBatch, unlinkBatchMembers, deleteBatch,
  workspaces, fetchWorkspaces, createWorkspace, renameWorkspace,
  addWorkspaceChat, removeWorkspaceChat, deleteWorkspace,   // NEW — §7
  liveDecision, liveSteps, usageStats, usageHistory, combinedUsageHistory, routeTrace, dependencyMap, structurePlan,
  macroLoopDecisions,
  roleRequests,
  mode, setMode,
  pusherConnected,
  activeMessageIndex, setActiveMessageIndex,
  sendTask, registerProject,
  // NEW — Part 2 §2.5: manual role editing before dispatch
  reviewBeforeDispatch, setReviewBeforeDispatch,
  pendingHireReview, confirmHireReview, cancelHireReview,
  // NEW — Part 2 §2.4/§2.7: human-in-the-loop checkpoints
  pausedApproval, resumeRun,
  };
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}