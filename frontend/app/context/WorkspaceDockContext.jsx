"use client";
// frontend/app/context/WorkspaceDockContext.jsx
//
// Step 3a+3b of the §2.6 build order (architecture doc §2.3/§2.4/§2.5).
// 3a built the state shape + keying. 3b (this pass) adds the
// session-Pusher-channel subscription and its bind_global handler,
// owned by the store and parameterized per dock — per the doc: "This
// provider is also what owns the session-${sessionId} Pusher channel
// subscription going forward... same pattern as step 2, just relocated
// and parameterized per dock instead of one global instance."
//
// Still NOT done: 3c (move the run functions: sendTask, resumeRun,
// persistMessage, etc.), 3d (rewire WorkspaceChatPanel), 3e (rewire the
// remaining 8 consumers). SessionContext.jsx is UNTOUCHED by this patch
// — it keeps its own, independent subscription to the same channel name
// for now. That means, for any dock actually wired up post-3d/3e, a
// chat's events get processed twice (once by SessionContext's copy,
// once by this one) until 3e removes SessionContext's copy. That's
// intentional: nothing calls useWorkspaceDock() from real UI yet (3d/3e
// haven't landed), so today this is dormant — the double-processing
// scenario doesn't exist in the running app yet, only once wiring
// starts. Flagging so it isn't a surprise when 3d lands.
//
// KNOWN OPEN QUESTION for 3e: two branches of the original handler —
// usage_update and quota_alert — feed usageStats/usageHistory/
// combinedUsageHistory, which §2.4 explicitly keeps app-wide on
// SessionContext, not per-dock. This file deliberately does NOT handle
// those two event types (see handleDockEvent below) — they're left for
// SessionContext's existing subscription to keep handling. That's fine
// today since SessionContext's subscription is still live. It stops
// being fine once 3e deletes it, at which point something needs to keep
// receiving usage_update/quota_alert. Not resolving that now — flagging
// it as a real decision 3e needs to make (options include: SessionContext
// keeps a slim subscription just for those two events, or the dock
// forwards them up via a callback). Not guessing at the answer here.
//
// KEYING (§2.4): one dock per `workspace_id`, or per standalone `chat_id`
// for a Chat-tab chat that isn't wrapped in a workspace. Confirmed
// against ChatTab.jsx: it resolves `activeWorkspace` by checking whether
// any workspace's `chat_ids` array includes the current `sessionId`, and
// has no fallback identity when none does — so a bare chat's own id is
// the only stable identity available for it. Two tabs that pass the same
// workspaceId into useWorkspaceDock() automatically land on the same
// slot — this is what makes partial promotion (active_stages, already
// shipped) work without special-casing, per the doc's reasoning.
//
// STORE SHAPE: a single external store (useSyncExternalStore, not one
// big useState) keyed by dock key, with per-key subscriber sets. This
// matters once several tabs each hold a dock open at once — updating one
// dock's state should only re-render components subscribed to THAT dock,
// not every mounted dock consumer app-wide. React 18.3.1 (this repo's
// version) ships useSyncExternalStore natively, no new dependency. The
// same per-key listener bookkeeping this requires (3a) turns out to
// double as the natural signal for channel lifecycle in 3b: "first
// subscriber for this key" = bind the channel, "last subscriber for
// this key leaves" = unbind it. No separate ref-counting scheme needed.

import { createContext, useContext, useRef, useCallback, useSyncExternalStore } from "react";
import { getPusherClient } from "../lib/pusherClient";
import { supabase } from "../lib/supabaseClient";

// Duplicated from SessionContext.jsx rather than imported from it. This
// file is meant to be the "child" in the mother/child relationship
// §2.4 describes — SessionContext should not end up depending on this
// file importing back from it (or vice versa) once wiring lands in
// 3d/3e. Both copies read the same env vars via the same shared
// supabaseClient.js singleton, so they can't drift in practice; still,
// once SessionContext's own copies of sendTask/resumeRun/etc. are
// deleted in 3e, it'd be worth pulling both of these into a small
// shared apiClient.js so neither file owns the canonical copy.
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function authHeaders(opts = {}) {
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token;
  const headers = {};
  if (opts.json) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

// Fields per dock — mirrors the per-workspace state SessionContext.jsx
// currently threads through ~20 functions (see handoff notes), PLUS
// `roleRequests`. That field isn't in the doc's §2.4 list, but grepping
// the actual consumers shows it's read by WorkingPanel.jsx (one of the
// 9 consumers slated for dock migration) exactly the same way liveSteps
// is — accumulated per-run, not app-wide — and it's written by the
// agent_requested_role branch of the very same session-channel handler
// this step is moving. Treating its absence from the doc's list as an
// omission rather than leaving agent_requested_role with nowhere to go.
function makeInitialDockState() {
  return {
    messages: [],
    sessionId: null,
    loading: false,
    liveDecision: null,
    liveSteps: [],
    routeTrace: [],
    macroLoopDecisions: [],
    dependencyMap: {},
    structurePlan: null,
    activeMessageIndex: null,
    pendingHireReview: null,
    pausedApproval: null,
    pausedRun: null,
    roleRequests: [], // ADDED in 3b — see note above
  };
}

const WorkspaceDockStoreContext = createContext(null);

export function WorkspaceDockProvider({ children }) {
  // Lazily build one store for the provider's lifetime. Kept in a ref
  // (not state) since the store object itself never needs to trigger a
  // re-render — only per-key notifications do, via useSyncExternalStore
  // in the consumer hook below.
  const storeRef = useRef(null);
  if (storeRef.current === null) {
    const states = new Map(); // dock key -> dock state object
    const listeners = new Map(); // dock key -> Set<() => void>

    // Non-reactive per-key bookkeeping for the agent_start/token_chunk/
    // done/error step lifecycle — a direct port of SessionContext's
    // stepSeq/openStepStack REFS (not state). They're implementation
    // detail for building `liveSteps`, not something any consumer reads
    // directly, so — same as the original — they live outside the
    // reactive store rather than as a public dock field.
    const stepSeqs = new Map(); // dock key -> number
    const openStepStacks = new Map(); // dock key -> array of step ids

    // dock key -> { channel, channelName, handler } for the pusher
    // subscription currently bound for that key, if any.
    const channelBindings = new Map();

    const ensure = (key) => {
      if (!states.has(key)) states.set(key, makeInitialDockState());
      if (!listeners.has(key)) listeners.set(key, new Set());
      if (!stepSeqs.has(key)) stepSeqs.set(key, 0);
      if (!openStepStacks.has(key)) openStepStacks.set(key, []);
    };

    const getState = (key) => {
      ensure(key);
      return states.get(key);
    };

    const notify = (key) => {
      listeners.get(key)?.forEach((cb) => cb());
    };

    // Shallow-merges a patch into one dock's state and notifies only
    // that dock's subscribers. `patch` may be a plain object or an
    // updater function (prevState) => partialPatch — mirroring
    // useState's two call shapes, since 3c needs to move functional
    // updates (e.g. today's setLiveSteps(prev => [...prev, step]))
    // in here with as little rewriting as possible.
    const setState = (key, patch) => {
      ensure(key);
      const prev = states.get(key);
      const partial = typeof patch === "function" ? patch(prev) : patch;
      const next = { ...prev, ...partial };
      states.set(key, next);
      // Mirrors SessionContext's effect being keyed on [sessionId]: a
      // dock switching which chat it's showing (sessionId changes)
      // must resubscribe to the new chat's channel, not keep listening
      // to the old one.
      if (next.sessionId !== prev.sessionId) {
        unbindChannel(key);
        bindChannelIfNeeded(key);
      }
      notify(key);
    };

    // Direct port of the bind_global callback body in SessionContext.jsx
    // (lines ~208-364), retargeted from component-level setXxx calls to
    // store.setState(key, ...), and from stepsRef/openStepStack refs to
    // this store's per-key stepSeqs/openStepStacks maps. Branch-for-
    // branch identical logic; only the write destination changed.
    // usage_update and quota_alert are deliberately NOT handled here —
    // see the file-header note on why, and what 3e still needs to decide.
    const handleDockEvent = (key, eventType, data) => {
      const { agent, payload } = data;

      if (eventType === "routing_decision") {
        setState(key, { liveDecision: payload });
        return;
      }
      if (eventType === "dispatch_event") {
        setState(key, (prev) => ({
          routeTrace: [...prev.routeTrace, { destination: payload?.destination, reason: payload?.reason }],
        }));
        return;
      }
      if (eventType === "macro_loop_decision") {
        setState(key, (prev) => ({
          macroLoopDecisions: [
            ...prev.macroLoopDecisions,
            { action: payload?.decision, loop: payload?.loop, cause: payload?.cause },
          ],
        }));
        return;
      }
      if (eventType === "dependency_map") {
        setState(key, { dependencyMap: payload?.map || {} });
        return;
      }
      if (eventType === "structure_plan") {
        setState(key, { structurePlan: payload?.mermaid || null });
        return;
      }
      if (eventType === "agent_requested_role") {
        setState(key, (prev) => ({
          roleRequests: [
            ...prev.roleRequests,
            { requestingAgent: agent, requestedRole: payload?.requested_role, label: payload?.label },
          ],
        }));
        return;
      }
      if (eventType === "agent_start") {
        const nextSeq = (stepSeqs.get(key) || 0) + 1;
        stepSeqs.set(key, nextSeq);
        const step = {
          id: nextSeq,
          agent,
          role: payload?.label || agent,
          text: "",
          summary: null,
          image: null,
          durationMs: null,
          status: "running",
        };
        openStepStacks.set(key, [...(openStepStacks.get(key) || []), step.id]);
        setState(key, (prev) => ({ liveSteps: [...prev.liveSteps, step] }));
        return;
      }
      if (eventType === "agent_token_chunk") {
        const stack = openStepStacks.get(key) || [];
        if (stack.length === 0) return;
        const targetId = stack[stack.length - 1];
        setState(key, (prev) => ({
          liveSteps: prev.liveSteps.map((s) =>
            s.id === targetId ? { ...s, text: s.text + (payload?.text || "") } : s
          ),
        }));
        return;
      }
      if (eventType === "agent_done") {
        const stack = openStepStacks.get(key) || [];
        if (stack.length === 0) return;
        const targetId = stack[stack.length - 1];
        openStepStacks.set(key, stack.slice(0, -1));
        setState(key, (prev) => ({
          liveSteps: prev.liveSteps.map((s) =>
            s.id === targetId
              ? { ...s, status: "done", summary: payload?.summary, durationMs: payload?.duration_ms, image: payload?.image || null }
              : s
          ),
        }));
        return;
      }
      if (eventType === "error") {
        const stack = openStepStacks.get(key) || [];
        if (stack.length === 0) return;
        const targetId = stack[stack.length - 1];
        openStepStacks.set(key, stack.slice(0, -1));
        setState(key, (prev) => ({
          liveSteps: prev.liveSteps.map((s) =>
            s.id === targetId ? { ...s, status: "error", summary: payload?.message } : s
          ),
        }));
        return;
      }
      if (eventType === "awaiting_approval") {
        const roleName = payload?.role || payload?.label || agent;
        setState(key, (prev) => {
          const idx = [...prev.liveSteps].map((s) => s.role).lastIndexOf(roleName);
          const liveSteps = idx !== -1
            ? prev.liveSteps.map((s, i) => (i === idx ? { ...s, status: "awaiting_approval" } : s))
            : prev.liveSteps;
          return { liveSteps, pausedApproval: { role: roleName } };
        });
        return;
      }
      // usage_update, quota_alert: intentionally not handled — see
      // file-header note.
    };

    // Binds the session-${sessionId} channel for a dock key, if that
    // dock currently has a sessionId and isn't already bound. No-op
    // otherwise (mirrors SessionContext's `if (!sessionId) return;` and
    // `if (!pusher) { console.warn(...); return; }` guards).
    const bindChannelIfNeeded = (key) => {
      if (channelBindings.has(key)) return;
      const dockSessionId = states.get(key)?.sessionId;
      if (!dockSessionId) return;
      const pusher = getPusherClient();
      if (!pusher) {
        console.warn("Pusher env vars not set — live agent events disabled.");
        return;
      }
      const channelName = `session-${dockSessionId.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
      const channel = pusher.subscribe(channelName);
      const handler = (eventType, data) => handleDockEvent(key, eventType, data);
      channel.bind_global(handler);
      channelBindings.set(key, { channel, channelName, handler });
    };

    const unbindChannel = (key) => {
      const entry = channelBindings.get(key);
      if (!entry) return;
      entry.channel.unbind_global(entry.handler);
      getPusherClient()?.unsubscribe(entry.channelName);
      channelBindings.delete(key);
    };

    const subscribe = (key, callback) => {
      ensure(key);
      listeners.get(key).add(callback);
      bindChannelIfNeeded(key); // no-op if already bound, or no sessionId yet
      return () => {
        listeners.get(key)?.delete(callback);
        if (listeners.get(key)?.size === 0) {
          // Last consumer of this dock went away — release its channel
          // rather than leaving a subscription running for a dock no
          // component is showing. Does NOT delete `states.get(key)` —
          // the dock's data survives the tab being hidden/switched away
          // from, same as AppShell's display:none-not-unmount pattern
          // for tabs; only the live channel is released.
          unbindChannel(key);
        }
      };
    };

    // Drops a dock entirely, including any bound channel. Nothing calls
    // this yet — not needed until there's an actual "retract" action
    // (flagged as future/out-of-scope in doc §2.2) or cleanup-on-unmount
    // logic in 3d/3e. Defined now since the store is the natural place
    // for it to live.
    const remove = (key) => {
      unbindChannel(key);
      states.delete(key);
      listeners.delete(key);
      stepSeqs.delete(key);
      openStepStacks.delete(key);
    };

    // ---- 3c: run functions, ported from SessionContext.jsx --------
    //
    // Scope note: this port covers sendTask, resumeRun,
    // confirmHireReview, cancelHireReview, persistMessage,
    // _resetLiveRunState, and _buildAssistantMessage — the functions
    // that actually read/write per-dock state. It deliberately does
    // NOT include switchChat, createNewChat, renameChat, deleteChat, or
    // linkChats, even though the doc's own §2.6 sub-decomposition names
    // them alongside these under 3c's "etc." Those five all primarily
    // mutate the app-wide `chats` list (via refreshChatList()) and only
    // touch per-dock state (sessionId, messages) as a side effect of
    // switching which chat is "in focus" — a materially different, more
    // entangled job than the run functions here. Moving them well
    // deserves its own checkpoint rather than being folded in under
    // "run functions." Deferring them rather than guessing at how they
    // should split between mother/child.
    //
    // Also deliberately NOT ported: runTemplate. Reading its actual
    // body (SessionContext.jsx ~1510-1546) shows it was written to
    // avoid touching per-dock state on purpose — it creates its OWN
    // chat via createChatSilently() and never calls setSessionId/
    // setMessages, specifically so a template run never disturbs
    // whatever chat is currently open. Its state lives in `templateRuns`
    // on SessionContext, not in any dock, and its only caller
    // (WorkflowTemplatesTab.jsx) isn't in the doc's 9-consumer list.
    // Moving it here would contradict its own design rationale — it
    // isn't a "dock" function despite being listed under §2.6's "etc.,"
    // so it's staying put. persistMessageTo/createChatSilently (its two
    // helpers) don't touch dock state either and aren't ported for the
    // same reason.

    const persistMessage = async (key, message) => {
      const dockSessionId = states.get(key)?.sessionId;
      if (!dockSessionId) return;
      try {
        await fetch(`${API_URL}/api/chats/${dockSessionId}/messages`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
          body: JSON.stringify({ message }),
        });
      } catch (err) {
        console.error("Failed to persist message:", err);
      }
    };

    // Reads straight from the store instead of stepsRef/routeTraceRef/
    // etc. — those refs existed in SessionContext only to dodge stale
    // closures over React state; this store's `states` map is already
    // always current, so no parallel ref bookkeeping is needed here.
    const buildAssistantMessage = (key, taskText, data) => {
      const s = getState(key);
      return {
        role: "assistant",
        data,
        task: taskText,
        steps: s.liveSteps,
        routeTrace: s.routeTrace,
        roleRequests: s.roleRequests,
        dependencyMap: s.dependencyMap,
        structurePlan: s.structurePlan,
      };
    };

    const resetLiveRunState = (key) => {
      setState(key, {
        liveDecision: null,
        liveSteps: [],
        roleRequests: [],
        routeTrace: [],
        dependencyMap: {},
        structurePlan: null,
        macroLoopDecisions: [],
      });
      openStepStacks.set(key, []); // stepSeqs is NOT reset — matches original: it only ever counts up, for React key uniqueness across the whole session
    };

    // `mode`/`reviewBeforeDispatch` aren't in the doc's per-dock field
    // list, and aren't obviously app-wide either — they're dispatch
    // preferences read only by the functions being moved here. Rather
    // than guess which side of the mother/child split they belong on,
    // they're accepted as parameters, defaulted to match today's actual
    // defaults (mode: "auto", reviewBeforeDispatch: false per
    // SessionContext's own useState initializers). Whatever wires this
    // up in 3d/3e passes its own current values through unchanged —
    // this keeps that as an explicit decision for whoever does the
    // wiring, not one buried in here.
    const sendTask = async (key, taskText, { mode = "auto", reviewBeforeDispatch = false } = {}) => {
      const dockSessionId = states.get(key)?.sessionId;
      const userMessage = { role: "user", text: taskText };
      setState(key, (prev) => ({ messages: [...prev.messages, userMessage] }));
      persistMessage(key, userMessage);
      setState(key, { loading: true });
      resetLiveRunState(key);

      if (reviewBeforeDispatch) {
        try {
          const res = await fetch(`${API_URL}/api/task/preview`, {
            method: "POST",
            headers: await authHeaders({ json: true }),
            body: JSON.stringify({ task_text: taskText, session_id: dockSessionId, mode }),
          });
          const data = await res.json();
          if (data.status === "preview_ready") {
            setState(key, {
              pendingHireReview: {
                taskText,
                sessionId: data.session_id,
                decision: data.decision,
                hires: data.result?.hires || [],
              },
              loading: false,
            });
            return;
          }
          const assistantMessage = buildAssistantMessage(key, taskText, data);
          setState(key, (prev) => ({ messages: [...prev.messages, assistantMessage], loading: false }));
          persistMessage(key, assistantMessage);
        } catch (err) {
          const assistantMessage = buildAssistantMessage(key, taskText, { status: "error", message: String(err) });
          setState(key, (prev) => ({ messages: [...prev.messages, assistantMessage], loading: false }));
          persistMessage(key, assistantMessage);
        }
        return;
      }

      try {
        const res = await fetch(`${API_URL}/api/task`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
          body: JSON.stringify({ task_text: taskText, session_id: dockSessionId, mode }),
        });
        const data = await res.json();
        if (data.status === "paused") {
          setState(key, { pausedRun: { taskText, sessionId: data.session_id || dockSessionId } });
          return; // loading stays true, matching SessionContext.jsx's sendTask()
        }
        const assistantMessage = buildAssistantMessage(key, taskText, data);
        setState(key, (prev) => ({ messages: [...prev.messages, assistantMessage], loading: false }));
        persistMessage(key, assistantMessage);
      } catch (err) {
        const assistantMessage = buildAssistantMessage(key, taskText, { status: "error", message: String(err) });
        setState(key, (prev) => ({ messages: [...prev.messages, assistantMessage], loading: false }));
        persistMessage(key, assistantMessage);
      }
    };

    const resumeRun = async (key, decision) => {
      const pausedRun = states.get(key)?.pausedRun;
      if (!pausedRun) return;
      try {
        const res = await fetch(`${API_URL}/api/resume`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
          body: JSON.stringify({ session_id: pausedRun.sessionId, ...decision }),
        });
        const data = await res.json();
        setState(key, { pausedApproval: null });
        if (data.status === "paused") return;
        const assistantMessage = buildAssistantMessage(key, pausedRun.taskText, data);
        setState(key, (prev) => ({ messages: [...prev.messages, assistantMessage], loading: false, pausedRun: null }));
        persistMessage(key, assistantMessage);
      } catch (err) {
        const assistantMessage = buildAssistantMessage(key, pausedRun.taskText, { status: "error", message: String(err) });
        setState(key, (prev) => ({
          messages: [...prev.messages, assistantMessage], loading: false, pausedRun: null, pausedApproval: null,
        }));
        persistMessage(key, assistantMessage);
      }
    };

    const confirmHireReview = async (key, editedHires, { mode = "auto" } = {}) => {
      const pendingHireReview = states.get(key)?.pendingHireReview;
      if (!pendingHireReview) return;
      const { taskText, sessionId: reviewSessionId, decision } = pendingHireReview;
      setState(key, { loading: true });
      resetLiveRunState(key);
      try {
        const res = await fetch(`${API_URL}/api/task/confirm`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
          body: JSON.stringify({ task_text: taskText, decision, hires: editedHires, session_id: reviewSessionId, mode }),
        });
        const data = await res.json();
        const assistantMessage = buildAssistantMessage(key, taskText, data);
        setState(key, (prev) => ({ messages: [...prev.messages, assistantMessage] }));
        persistMessage(key, assistantMessage);
      } catch (err) {
        const assistantMessage = buildAssistantMessage(key, taskText, { status: "error", message: String(err) });
        setState(key, (prev) => ({ messages: [...prev.messages, assistantMessage] }));
        persistMessage(key, assistantMessage);
      } finally {
        setState(key, { loading: false, pendingHireReview: null });
      }
    };

    const cancelHireReview = (key) => {
      setState(key, { pendingHireReview: null });
    };

    storeRef.current = {
      getState, subscribe, setState, remove,
      persistMessage, sendTask, resumeRun, confirmHireReview, cancelHireReview,
    };
  }

  return (
    <WorkspaceDockStoreContext.Provider value={storeRef.current}>
      {children}
    </WorkspaceDockStoreContext.Provider>
  );
}

// workspaceId wins when present; chatId is the fallback identity for a
// standalone Chat-tab chat. Returns null when neither is available yet
// (e.g. nothing has loaded), so callers can bail before touching the
// store rather than silently keying on "undefined".
function normalizeDockKey(workspaceId, chatId) {
  if (workspaceId) return `ws:${workspaceId}`;
  if (chatId) return `chat:${chatId}`;
  return null;
}

/**
 * useWorkspaceDock(workspaceId, chatId?)
 *
 * SKELETON ONLY (3a) — returns { key, state, setDockState } for the dock
 * slot keyed by workspaceId (or chatId when there's no workspace).
 * Nothing calls this yet. SessionContext still owns the real, live
 * version of every one of these fields until 3c/3d/3e land.
 */
export function useWorkspaceDock(workspaceId, chatId = null) {
  const store = useContext(WorkspaceDockStoreContext);
  if (!store) {
    throw new Error("useWorkspaceDock must be used within a WorkspaceDockProvider");
  }
  const key = normalizeDockKey(workspaceId, chatId);

  const subscribe = useCallback(
    (callback) => (key ? store.subscribe(key, callback) : () => {}),
    [store, key]
  );
  const getSnapshot = useCallback(
    () => (key ? store.getState(key) : makeInitialDockState()),
    [store, key]
  );

  const state = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const setDockState = useCallback(
    (patch) => {
      if (!key) return;
      store.setState(key, patch);
    },
    [store, key]
  );

  // 3c: bound wrappers so a consumer calls dock.sendTask(taskText) the
  // same way it calls useSession()'s sendTask(taskText) today — no key
  // plumbing at every call site. All are no-ops if key is null (nothing
  // resolved yet), same guard style as setDockState above.
  const sendTask = useCallback(
    (taskText, opts) => (key ? store.sendTask(key, taskText, opts) : Promise.resolve()),
    [store, key]
  );
  const resumeRun = useCallback(
    (decision) => (key ? store.resumeRun(key, decision) : Promise.resolve()),
    [store, key]
  );
  const confirmHireReview = useCallback(
    (editedHires, opts) => (key ? store.confirmHireReview(key, editedHires, opts) : Promise.resolve()),
    [store, key]
  );
  const cancelHireReview = useCallback(
    () => key && store.cancelHireReview(key),
    [store, key]
  );

  return { key, state, setDockState, sendTask, resumeRun, confirmHireReview, cancelHireReview };
}
