"use client";
import { createContext, useContext, useState, useRef, useEffect, useCallback, useMemo } from "react";
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

  // NEW — 3e usage-event ownership (architecture doc §2.3's three
  // options; going with option 1): extracted out of the sessionId-keyed
  // bind_global handler below so WorkspaceDockContext's per-dock
  // handleDockEvent can invoke this exact same logic via a threaded-in
  // callback (`onUsageEvent`, passed through WorkspaceDockBridge in
  // AppShell.jsx same as refreshChatList/getWorkspaceIdForChat/
  // fetchWorkspaces already are), rather than duplicating it or leaving
  // usage_update/quota_alert permanently unhandled once this
  // subscription is deleted (see WorkspaceDockContext.jsx's file-header
  // comment on the open question this resolves). Pure extraction — the
  // branch bodies are unchanged, only the call site moved.
  const handleUsageEvent = useCallback((eventType, payload) => {
    if (eventType === "usage_update") {
      const statKey = `${payload?.provider}:${payload?.key_id}`;
      setUsageStats((prev) => ({ ...prev, [statKey]: payload }));

      // Part 17: append to this key's own history (capped so a very
      // long session doesn't grow this unbounded).
      setUsageHistory((prev) => {
        const series = prev[statKey] || [];
        const next = [...series, { t: Date.now(), tokens: payload?.tokens_used_today ?? 0 }];
        return { ...prev, [statKey]: next.length > 300 ? next.slice(-300) : next };
      });

      // Part 17: maintain a per-provider running total (summed across
      // every key seen so far for that provider) and append one row to
      // a combined, time-aligned series every update, forward-filling
      // every OTHER provider's last known value so the combined chart
      // has a real value for every provider at every timestamp, not
      // just the one that happened to fire this particular event.
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
    if (eventType === "quota_alert") {
      console.warn("quota_alert:", payload);
      return;
    }
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
      // NEW — Phase 8 (Working Panel transparency) step 8.1: log the
      // RAW envelope (relay/emitter.py's Part 6.3 shape: {type,
      // session_id, agent, path, timestamp, payload}) for the four
      // event types AgentStepList.jsx will need to render richer step
      // rows from, ahead of writing any of 8.2-8.5. `data` here, not
      // the destructured `agent`/`payload` below, so nothing this repo
      // already throws away (notably `path`) is hidden from the log.
      //
      // FINDINGS (confirmed by reading the emitting code directly,
      // eo/executor.py + relay/emitter.py + eo/dispatcher.py +
      // eo/loop_v4.py, not by guessing from the frontend side — this
      // console.log is here to CONFIRM those reads against real traffic,
      // not to discover the shape from scratch):
      //   - agent_start payload today is ONLY {label: role}
      //     (eo/executor.py's `payload={"label": role}`). No source/
      //     secondary-data scope, no key_overrides, nothing about what
      //     the agent was actually given -- 8.3 ("what it was given")
      //     has NO existing field to read yet; that step needs a real
      //     eo/executor.py payload addition, not just a frontend change.
      //   - Every event's top-level `path` (e.g. "adaptive"/"direct"/
      //     "fixed"/"instant") is the SAME for every step in one run --
      //     it's the run's pipeline path, set once by eo/loop_v4.py's
      //     routing_decision, not decided per-step. eo/structure.py's
      //     PATH_TO_TIER maps it to the numeric tier (0-3). This IS
      //     already on every agent_start/agent_done envelope today --
      //     SessionContext.jsx's handler below just doesn't read `path`
      //     off `data` yet, only `agent`/`payload`. 8.2 ("tier per
      //     step") can get tier for free from this, no backend change
      //     needed, by threading `data.path` into the step object here.
      //   - agent_done payload is {summary, duration_ms, image?}
      //     (eo/executor.py). No chained-call info on it.
      //   - dispatch_event (agent: "dispatcher") fires payload
      //     {destination, reason} AFTER a role finishes and the
      //     Dispatcher picks the next hop (eo/dispatcher.py's
      //     _log_route()) -- this is the closest existing thing to
      //     "what it called out to" (8.4), but `reason` is a short
      //     internal code today ("escalate"/"recheck"/etc, see
      //     eo/dispatcher.py's next_step()), not a human sentence, and
      //     it isn't role-scoped the way agent_start/agent_done are --
      //     it has to be correlated to the preceding step by arrival
      //     order. WorkspaceChatPanel/SessionContext already collects
      //     these into `routeTrace`, just not merged onto `liveSteps`
      //     entries yet.
      //   - routing_decision's payload IS the full Inspector/Panel
      //     `decision` dict (eo/loop_v4.py's `_get_decision()`) --
      //     `tier`, `domain`, `confidence`, `suggested_agents`, and a
      //     `reasoning` string ("classification failed, defaulting to
      //     tier 3 (safest)" being the one confirmed literal value in
      //     this codebase; the Panel/Inspector supply their own text
      //     the rest of the time). This is RUN-level, fired once before
      //     any agent_start, already captured as `liveDecision`/
      //     `m.data.decision` and rendered by RoutingTraceCard.jsx --
      //     8.5's "one-line routing why" per STEP most likely means
      //     reusing this same `reasoning` string on every step's row
      //     (it doesn't vary per step either), not a new per-step why.
      // No behavior change in this step — every existing branch below
      // is untouched; this only adds visibility for 8.2-8.5 to build on.
      if (["routing_decision", "agent_start", "agent_done", "dispatch_event"].includes(eventType)) {
        console.log(`[Phase 8 / 8.1 payload check] ${eventType}`, data);
      }
      const { agent, payload } = data;
      if (eventType === "routing_decision") {
        setLiveDecision(payload);
        return;
      }
      if (eventType === "usage_update") {
        handleUsageEvent(eventType, payload);
        return;
      }
      if (eventType === "dispatch_event") {
        const nextRouteTrace = [...routeTraceRef.current, { destination: payload?.destination, reason: payload?.reason }];
        routeTraceRef.current = nextRouteTrace;
        setRouteTrace(nextRouteTrace);
        // NEW — Phase 8 step 8.4 ("chained calls"). eo/dispatcher.py's
        // _log_route() (called from next_step()) always fires this
        // AFTER the step (or, for a concurrent group, every member of
        // the group) it's deciding about has already emitted its own
        // agent_done, and BEFORE the next step's agent_start -- see
        // eo/executor.py's main loop and _run_concurrent_group(), both
        // of which call next_step() only once results[role] is written.
        // That ordering means the last entry currently in
        // stepsRef.current is always the step this exact decision was
        // made about, so it's safe to just tag the array's last item —
        // no id/role matching needed, same "trust arrival order" shape
        // the openStepStack LIFO logic above already relies on for
        // nested chained calls within a single step. destination is
        // null on a normal run's final step (dispatcher.py's
        // _log_route() no-ops rather than emitting when destination is
        // None) so most runs' last row simply never gets tagged --
        // expected, not a bug.
        if (payload?.destination && stepsRef.current.length > 0) {
          const lastIdx = stepsRef.current.length - 1;
          const updated = stepsRef.current.map((s, i) =>
            i === lastIdx ? { ...s, calledOutTo: { destination: payload.destination, reason: payload.reason } } : s
          );
          stepsRef.current = updated;
          setLiveSteps(updated);
        }
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
        handleUsageEvent(eventType, payload);
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
          // NEW — Phase 8 step 8.2: `path` is the run's pipeline path
          // ("instant"/"direct"/"fixed"/"adaptive"), sitting on every
          // event envelope already (relay/emitter.py's Part 6.3 shape)
          // but never read off `data` here before 8.1's payload-check
          // pass confirmed it. Same value for every step in a run (set
          // once, at routing_decision time) — stored per-step anyway so
          // AgentStepList.jsx doesn't need to reach outside `steps` to
          // label each row, and so a finished message's persisted
          // snapshot (this same object, see `steps: stepsRef.current`
          // below) carries its own tier without depending on that run's
          // liveDecision still being in memory.
          path: data.path || null,
          // NEW — Phase 8 step 8.3: eo/executor.py now reports which
          // earlier roles' results were already on the memory bus for
          // this step to draw on (see that file's agent_start comment
          // for why this, not a source/secondary-data scope, is the
          // honest equivalent inside the staffed-task pipeline —
          // Notebooks generate's actual source scoping is a separate,
          // step-less code path this panel never renders anyway).
          // Defaults to [] for any event from a not-yet-updated backend
          // (older payload shape, or agent_start events this build
          // doesn't handle specially, e.g. instant/direct's own
          // entrypoints which never had role_names[:idx] to report).
          givenRoles: payload?.given_roles || [],
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

  // NEW — Data Layer §9c: per-workspace "an upload's Source Manager +
  // Backlink Detector pass is still running server-side" flag.
  // NotebooksGeneratePicker's Generate button reads this (via
  // processingWorkspaces below) so it doesn't dispatch a generate call
  // against a packet (eo/source_index.py:get_packet()) that's mid-write
  // right after a fresh upload.
  //
  // A Set of workspace ids, not a boolean, since more than one
  // workspace's uploads can be in flight in the same tab (e.g. two
  // Notebooks open in two WorkspaceDock panes), and a per-workspace
  // count (processingCountsRef, the actual source of truth — the Set
  // in state is just its rendered projection) since a single workspace
  // can itself have more than one upload in flight at once (a
  // multi-file drop in IngestionDropzone.jsx) — only clear a workspace
  // out of the Set once every one of its in-flight uploads has
  // settled.
  //
  // Entered in markSourceProcessingStarted() at ingest call-start —
  // there's no separate "upload accepted" server event, just the
  // eventual notify() completion, so call-start is the only "something
  // is now in flight" signal the client has. Cleared two ways, both
  // wired below: (1) this call's own settle, in *_wrapped's finally
  // block just past this effect — a guarantee independent of whether
  // any WebSocket message ever arrives; (2) this session's
  // "upload_processed"/"backlinks_updated" push below, which is what
  // lets §2d's future parallel-fan-out uploads (server processing that
  // outlives this request) and cross-tab updates (the same workspace
  // open in a second tab) clear the flag too, not just this exact
  // call's own request/response. Both paths funnel through the same
  // decrement-and-floor-at-zero helper, so whichever fires first wins
  // and the other is a safe no-op.
  const [processingWorkspaces, setProcessingWorkspaces] = useState(() => new Set());
  const processingCountsRef = useRef({});   // wsId -> in-flight count

  // NEW — Overlap/Live-Viz guide §6/§8: gold-highlight for whichever
  // topic-tree node a topic_added/topic_merged/connection_added event
  // just touched. One flat Set shared across every open workspace --
  // node ids already embed their own workspace_id (`node:{ws}:{id}`,
  // same convention KnowledgeGraphView.jsx's graphNodes build), so
  // there's no cross-workspace collision to worry about. Consumed by
  // NotebooksTab.jsx's BacklinksView -> KnowledgeGraphView's
  // `pulsingIds` prop.
  const [topicPulsingIds, setTopicPulsingIds] = useState(() => new Set());
  // Step 2.3a (perf audit item #2, first useCallback batch): wrapped so
  // this stops being a new function reference on every render, which is
  // required for the useMemo'd `value` (step 2.2) to actually start
  // holding a stable reference for consumers that only use this field.
  // No deps needed -- setTopicPulsingIds (useState setter) is stable
  // across renders by React's own contract, and nothing else outside
  // this function's own scope is referenced.
  const pulseTopicNode = useCallback((id) => {
    if (!id) return;
    setTopicPulsingIds((prev) => new Set(prev).add(id));
    setTimeout(() => {
      setTopicPulsingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }, 1800);
  }, []);

  // _syncProcessingWorkspaces reads/writes processingCountsRef.current
  // (a ref, not state) and calls the stable setProcessingWorkspaces
  // setter -- refs are stable by identity across renders too, so this
  // needs no deps either.
  const _syncProcessingWorkspaces = useCallback(() => {
    setProcessingWorkspaces(
      new Set(Object.keys(processingCountsRef.current).filter((k) => processingCountsRef.current[k] > 0))
    );
  }, []);

  // Depend on _syncProcessingWorkspaces itself, now that it's a stable
  // useCallback reference rather than a fresh function every render --
  // if it weren't wrapped above, listing it here would defeat the
  // point (the dep would change every render, invalidating this memo
  // right along with it).
  const markSourceProcessingStarted = useCallback((wsId) => {
    if (!wsId) return;
    processingCountsRef.current[wsId] = (processingCountsRef.current[wsId] || 0) + 1;
    _syncProcessingWorkspaces();
  }, [_syncProcessingWorkspaces]);

  const markSourceProcessingSettled = useCallback((wsId) => {
    if (!wsId || !processingCountsRef.current[wsId]) return;   // already at 0 (or never started) — the other clear path already handled it
    processingCountsRef.current[wsId] = Math.max(0, processingCountsRef.current[wsId] - 1);
    _syncProcessingWorkspaces();
  }, [_syncProcessingWorkspaces]);

  // NEW — Data Layer §9c: the frontend's first consumer of §9b's real
  // /ws/{session_id} push transport. Deliberately a plain WebSocket,
  // not routed through getPusherClient() above — see eo/notify.py's
  // own "ASSUMPTION FLAGGED" docstring note on why this channel is its
  // own thing rather than a wrapper around the existing Pusher
  // transport that effect already uses.
  //
  // Kept intentionally simpler than that Pusher effect: no
  // reconnect-with-backoff here, since the one thing riding this
  // socket today (clearing processingWorkspaces on
  // upload_processed/backlinks_updated) already degrades safely if a
  // message is missed — markSourceProcessingSettled() also fires from
  // *_wrapped's own finally block below regardless of whether this
  // socket ever delivers. A real reconnect strategy is the natural
  // next piece once something riding this channel needs a delivery
  // guarantee this tab's own request/response can't already provide
  // (§9d's chat proactive suggestions will likely be the first).
  useEffect(() => {
    if (!sessionId) return;
    let socket = null;
    let cancelled = false;
    (async () => {
      const { data: { session } } = await supabase.auth.getSession();
      const token = session?.access_token;
      if (!token || cancelled) return;   // no token yet, or effect already torn down while we were awaiting getSession()
      const wsUrl = `${API_URL.replace(/^http/, "ws")}/ws/${encodeURIComponent(sessionId)}?token=${encodeURIComponent(token)}`;
      const ws = new WebSocket(wsUrl);
      ws.onmessage = (evt) => {
        let event;
        try {
          event = JSON.parse(evt.data);
        } catch {
          return;   // not JSON — not one of eo/notify.py's events, ignore rather than throw
        }
        if (event.kind === "upload_processed" || event.kind === "backlinks_updated") {
          markSourceProcessingSettled(event.payload?.workspace_id);
        }
        // NEW — Overlap/Live-Viz guide §6/§8: same socket, same
        // event.kind dispatch, three additional kinds. Node id built
        // the same `node:{workspace_id}:{topic_id}` way the new
        // /topics/graph endpoint (api/server.py) already builds edge
        // endpoints, so it lines up with what KnowledgeGraphView's
        // graphNodes map keys its own ids by.
        const wsId = event.payload?.workspace_id;
        if (wsId && event.kind === "topic_added") {
          pulseTopicNode(`node:${wsId}:${event.payload.topic_id}`);
        } else if (wsId && event.kind === "topic_merged") {
          // The merged-away topic never gets its own node (see
          // source_manager.py's comment: a "duplicate" fold is an
          // instances-append on the TARGET, not a new /topics/<id>) --
          // pulse target_topic_id, not topic_id.
          pulseTopicNode(`node:${wsId}:${event.payload.target_topic_id}`);
        } else if (wsId && event.kind === "connection_added") {
          // Both endpoints of a new connection are worth calling out,
          // not just one.
          pulseTopicNode(`node:${wsId}:${event.payload.from_topic}`);
          pulseTopicNode(`node:${wsId}:${event.payload.to_topic}`);
        }
      };
      if (cancelled) {
        ws.close();   // getSession() resolved after unmount/sessionId-change beat us here
        return;
      }
      socket = ws;
    })();
    return () => {
      cancelled = true;
      socket?.close();
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

  const markNotificationsRead = useCallback(() => {
    setUnreadCount(0);
    setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
  }, []);

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

  const refreshChatList = useCallback(async () => {
    const res = await fetch(`${API_URL}/api/chats`, {
      headers: await authHeaders(),
    });
    setChats(await res.json());
  }, []);

  // Step 2.3i (perf audit item #2, ninth useCallback batch, continued):
  // switchChat/createNewChat land HERE, right after refreshChatList,
  // rather than at their original textual position further down the
  // file. Reason: removeWorkspaceChat (2.3c, below) already has both of
  // these in its dependency array, and that array is evaluated
  // synchronously during render as part of the useCallback(...) call --
  // not deferred the way a useEffect body is. Leaving switchChat/
  // createNewChat declared after removeWorkspaceChat would mean
  // referencing a `const` before its initializer has run in the same
  // render pass: a genuine temporal-dead-zone ReferenceError, not the
  // "it's fine, it's only called later from an event handler" pattern
  // every ordering check up through 2.3h has been able to rely on. Every
  // other call site of switchChat/createNewChat (the mount effect near
  // the top of the file) is inside useEffect(..., []), so it still runs
  // after this render completes either way -- moving the declaration up
  // doesn't change anything for those callers, it only fixes the one
  // that actually mattered.
  //
  // Both close over a pile of setState setters and refs (setSessionId,
  // setMessages, stepsRef, setLiveSteps, routeTraceRef, setRouteTrace,
  // dependencyMapRef, setDependencyMap, structurePlanRef,
  // setStructurePlan, roleRequestsRef, setRoleRequests,
  // setMacroLoopDecisions, setLiveDecision) -- all stable by React's own
  // guarantee, so none of those belong in the dep array (same omission
  // rule established for _syncProcessingWorkspaces in the very first
  // useCallback batch). The only real dependency either one has is
  // refreshChatList, already stable directly above.
  const switchChat = useCallback(async (chatId, { skipListReload = false } = {}) => {
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
  }, [refreshChatList]);

  const createNewChat = useCallback(async () => {
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
  }, [refreshChatList]);

  // NEW — step 3e prereq: pure lookup, no state mutation. Same check
  // ChatTab.jsx already did inline to find its activeWorkspace. Exposed
  // here (rather than left duplicated) so WorkspaceDockContext's
  // switchChat/createNewChat/etc. can resolve "which dock key does this
  // chatId belong to" without importing this file or duplicating the
  // `workspaces` state itself — it's passed down as a callback prop
  // instead (see AppShell.jsx's WorkspaceDockBridge).
  const getWorkspaceIdForChat = useCallback((chatId) => {
    const ws = (workspaces || []).find(
      (w) => Array.isArray(w.chat_ids) && w.chat_ids.includes(chatId)
    );
    return ws?.id ?? null;
  }, [workspaces]);

  // NEW — §4: loads memory_batch groups so the sidebar can render batch
  // sections and the Working Panel can show "sharing memory with..."
  // for the active chat. §5 adds create/rename/unlink/delete on top of
  // this same `batches` state.
  // NEW — §6: repurposes the old LinkChatsModal save flow. Creates a
  // real batch (mutual membership) instead of the old one-directional
  // linkChats() call — see ChatSidebar.jsx's LinkChatsModal.
  // Step 2.3b: fetchBatches moved ABOVE createBatch (previously
  // createBatch, at the top of this file's source order, called
  // fetchBatches, which was declared further down -- fine when both
  // were hoisted `function` declarations, but a real bug waiting to
  // happen once both become `const ... = useCallback(...)`: unlike
  // functions, `const` bindings are NOT hoisted, so createBatch's
  // dependency array would try to read `fetchBatches` before its own
  // const initializer has run and throw a ReferenceError (temporal
  // dead zone) the first time this component renders. Every batch
  // from here on checks this same thing before converting: does this
  // function call another function converted in this same batch (or
  // an earlier one), and if so, is that dependency's const
  // declaration actually above this one in source order?
  const fetchBatches = useCallback(async () => {
    const res = await fetch(`${API_URL}/api/batches`, {
      headers: await authHeaders(),
    });
    setBatches(await res.json());
  }, []);

  const createBatch = useCallback(async (name, memberChatIds) => {
    await fetch(`${API_URL}/api/batches`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ name, member_chat_ids: memberChatIds }),
    });
    await fetchBatches();
    await refreshChatList();
  }, [fetchBatches, refreshChatList]);
  // NEW — §9.2: live estimate for the create-batch modal. Not stored in
// context state — it's ephemeral per-modal-open, computed fresh each
// time the checkbox selection changes.
  const estimateBatch = useCallback(async (chatIds) => {
    const res = await fetch(`${API_URL}/api/batches/estimate`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ chat_ids: chatIds }),
    });
    return res.json();
  }, []);
  // NEW — §7: workspaces ("Projects" in the UI). Mirrors the batch functions
// above 1:1, with one thing to keep straight: workspaces store members as
// `chat_ids` (see eo/chat_workspace.py), batches use `member_chat_ids` —
// don't cross the two up when reading a response.

  const fetchWorkspaces = useCallback(async () => {
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
  }, []);

  const createWorkspace = useCallback(async (name, stage) => {
  // NEW — item #10 / B0: optional stage lets a caller (e.g. a stage
  // tab's own "New project" button) create a workspace that natively
  // belongs to that tab. Omitted = old behavior (backend defaults to
  // "note"), so existing Chat/Notebooks callers are unaffected.
  const res = await fetch(`${API_URL}/api/workspaces`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify(stage ? { name, stage } : { name }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const workspace = await res.json();
  await fetchWorkspaces();
  return workspace;
  }, [fetchWorkspaces]);

async function createWorkspaceWithChats(name, chatIds = [], stage) {
  const workspace = await createWorkspace(name, stage);
  for (const chatId of chatIds) {
    await addWorkspaceChat(workspace.id, chatId);
  }
  return workspace;
}

  const renameWorkspace = useCallback(async (wsId, name) => {
  await fetch(`${API_URL}/api/workspaces/${wsId}/rename`, {
    method: "PATCH",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ name }),
  });
  await fetchWorkspaces();
  }, [fetchWorkspaces]);

// Step 2.3c (perf audit item #2, third useCallback batch): continuing
// the same "workspaces" bucket started in 2.3b. Same ordering check as
// before -- fetchWorkspaces/refreshChatList are already `const ...
// useCallback` and declared above (lines 769/842), so referencing them
// here is safe with no hoisting concern.
//
// removeWorkspaceChat is the one that needs care: it closes over
// `chats`/`sessionId` (state, must be in the dep array) *and* calls
// switchChat()/createNewChat(). Originally noted here that those two
// were "still plain function declarations further down the file" and
// that this wouldn't actually stabilize until a later batch converted
// them -- UPDATE as of 2.3i: they're now useCallback-wrapped too, and
// specifically moved to right after refreshChatList (near line 769),
// ahead of this function, for a reason beyond simple hoisting -- see
// the comment there. With that move, this dependency array is both
// correct and fully stable.
const addWorkspaceChat = useCallback(async (wsId, chatId) => {
  await fetch(`${API_URL}/api/workspaces/${wsId}/chats`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ chat_id: chatId }),
  });
  await fetchWorkspaces();
  await refreshChatList(); // membership changes linked_chat_ids server-side (chat_workspace.py's _sync)
}, [fetchWorkspaces, refreshChatList]);

const removeWorkspaceChat = useCallback(async (wsId, chatId, deleteChat = false) => {
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
}, [fetchWorkspaces, refreshChatList, chats, sessionId, switchChat, createNewChat]);

const deleteWorkspace = useCallback(async (wsId) => {
  await fetch(`${API_URL}/api/workspaces/${wsId}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
  await fetchWorkspaces();
  await refreshChatList();
}, [fetchWorkspaces, refreshChatList]);

// NEW — §8: advances a workspace along the fixed stage sequence
// (note -> research -> plan -> build -> test -> growth). Defaults to
// the next stage when toStage is omitted, but callers can explicitly
// choose a later stage in the same sequence. Throws on failure (unlike
// the other CRUD functions above, which silently no-op) because a
// rejected promote -- wrong stage order, no edit access -- is
// something the calling button needs to surface, same reasoning already
// used for the Part 8.9 membership functions below.
//
// NEW — §2.6 step 4: mode is "complete" (default, unchanged behavior —
// workspace leaves the old tab) or "partial" (workspace becomes active
// in to_stage's tab while staying active in every tab it already was).
// Just threads the choice through to the backend, which already
// supports both (see chat_workspace.promote()) — no other client-side
// logic needed here.
const promoteWorkspace = useCallback(async (wsId, toStage = null, mode = "complete") => {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/promote`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ to_stage: toStage, mode }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  await fetchWorkspaces();
  return res.json();
}, [fetchWorkspaces]);

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

const exportWorkspace = useCallback(async (wsId) => {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/export`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Export failed (${res.status})`);
  }
  return res.json(); // the manifest itself — caller decides what to do with it (e.g. trigger a download)
}, []);

const importWorkspace = useCallback(async (wsId, manifest) => {
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
}, [fetchWorkspaces, refreshChatList]);

// Step 2.3d (perf audit item #2, fourth useCallback batch): Part 8.9's
// membership functions. All five are call-site-isolated -- none of them
// are called by any other function in this file (confirmed via grep),
// so there's no ordering/hoisting concern to check here, unlike the
// last two batches. Four of the five (fetchWorkspaceMembers,
// addWorkspaceMember, updateWorkspaceMemberRole, removeWorkspaceMember)
// close over nothing but their own arguments, so they get `[]`.
// leaveWorkspaceMembership is the odd one out -- it calls
// fetchWorkspaces/refreshChatList on success, so it depends on those two.
const fetchWorkspaceMembers = useCallback(async (wsId) => {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/members`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to load members (${res.status})`);
  }
  return res.json();
}, []);

const addWorkspaceMember = useCallback(async (wsId, email, role = "viewer") => {
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
}, []);

const updateWorkspaceMemberRole = useCallback(async (wsId, targetUserId, role) => {
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
}, []);

const removeWorkspaceMember = useCallback(async (wsId, targetUserId) => {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/members/${targetUserId}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to remove member (${res.status})`);
  }
  return res.json();
}, []);

const leaveWorkspaceMembership = useCallback(async (wsId, successorId = null) => {
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
}, [fetchWorkspaces, refreshChatList]);

// Step 2.3e (perf audit item #2, fifth useCallback batch): finishing
// off Part 8.9 -- ownership, voting, and attribution settings. Same
// call-site check as 2.3d: none of these five are called by any other
// function in this file (confirmed via grep), only from modal
// components elsewhere in the tree. fetchWorkspaceVotes and
// setMemberAttributionGrant close over nothing, so `[]`; the other
// three call fetchWorkspaces() on success, so they depend on it.
const forceRemoveOwner = useCallback(async (wsId) => {
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
}, [fetchWorkspaces]);

const fetchWorkspaceVotes = useCallback(async (wsId) => {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/votes`, {
    headers: await authHeaders(),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to load vote status (${res.status})`);
  }
  return res.json();
}, []);

const castWorkspaceVote = useCallback(async (wsId, voteTarget = null) => {
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
}, [fetchWorkspaces]);

const setWorkspaceAttribution = useCallback(async (wsId, show) => {
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
}, [fetchWorkspaces]);

const setMemberAttributionGrant = useCallback(async (wsId, targetUserId, canToggle) => {
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
}, []);

// --- NEW — §4.7: Notebooks tab. A "notebook" is a workspace (§4.3), so
// these all just parameterize the existing /api/workspaces/{ws_id}/...
// surface — no new container concept, matching the domain doc's own
// framing of notebook == workspace_id.

// Step 2.3f (perf audit item #2, sixth useCallback batch): the
// Notebooks-tab / graph / extraction-table functions. All six are
// call-site-isolated and close over nothing but their own arguments
// (fetchSimulationResults's `sessionId` param shadows the outer
// session-id state, so it's not a real closure either) -- so every one
// of these gets `[]`.
//
// Side note, not part of this batch's scope: renameWorkspaceNode
// doesn't appear in the `value` object or its dependency array at all
// (confirmed via grep -- fetchWorkspaceNodes/deleteWorkspaceNode/
// fetchGraphEdges/buildExtractionTable/fetchSimulationResults are all
// there, this one isn't). Either it's dead code or something consumes
// it a different way I haven't found -- flagging in case it's meant to
// be exposed and just got missed, not fixing it here since it's outside
// what this useCallback pass is meant to touch.
const fetchWorkspaceNodes = useCallback(async (wsId, nodeType) => {
  const qs = nodeType ? `?node_type=${encodeURIComponent(nodeType)}` : "";
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/nodes${qs}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return [];
  return res.json();
}, []);

// NEW — §2 fix: deletes a single ingested source/node. Caller is
// responsible for refetching the node list afterward (same pattern
// ingestFile()'s callers already follow via onIngested), since the
// delete endpoint itself only returns {status, id}, not a fresh list.
const deleteWorkspaceNode = useCallback(async (wsId, nodeId) => {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/nodes/${nodeId}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}, []);

const renameWorkspaceNode = useCallback(async (wsId, nodeId, title) => {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/nodes/${nodeId}/rename`, {
    method: "PATCH",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}, []);

const fetchGraphEdges = useCallback(async (wsId) => {
  const res = await fetch(`${API_URL}/api/graph/edges?workspace_id=${encodeURIComponent(wsId)}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return [];
  return res.json();
}, []);

// §3.5 — auto-generates a structured table from a workspace's own
// ingested nodes (agents/note_table_builder.py), instead of the user
// manually pasting a chat run's markdown table output. Throws on
// non-2xx so the caller can surface the server's actual error message
// (e.g. "no ingested sources with content found") rather than silently
// returning nothing.
const buildExtractionTable = useCallback(async (wsId, fieldNames, { nodeType, expanded } = {}) => {
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
}, []);

// Test tab / "simulate" domain — reads back whatever persona roles +
// simulation_synthesizer already wrote to the memory bus for a given
// (finished or in-progress) simulate-domain chat run. Read-only despite
// being a POST, same shape as buildExtractionTable above; see
// api/server.py's get_simulation_results() docstring for why this reads
// the bus instead of wrapping review_aggregator.py.
const fetchSimulationResults = useCallback(async (wsId, sessionId) => {
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
}, []);

// Test tab / `personas` sub-tab — thin client for the same Role Library
// store the Role Library panel already reads/writes via GET/PUT/PATCH
// /api/roles (eo/registry.py's list_role_metadata/update_role_prompt/
// set_role_pinned). No new backend surface: `personas` just calls these
// three and filters the result to the "simulate" domain's own role list
// client-side, same "reuse, don't rebuild" reasoning the /simulate
// endpoint itself used for stage_output reads.

// Step 2.3g (perf audit item #2, seventh useCallback batch): the Role
// Library thin-client trio for the Test tab's `personas` sub-tab. All
// three are call-site-isolated, close over nothing but their own
// arguments, so `[]` across the board.
//
// Deliberately NOT touching the five ingest* functions right after this
// (ingestClip, ingestVideoUrl, ingestFile, ingestPdfFile, and a fifth) —
// the comment already on them says they're meant to stay plain,
// unbound functions that IngestionDropzone.jsx/lib/ingestDispatch.js
// call directly by name, with the *_wrapped versions further down being
// the ones that actually go through the context/useCallback treatment.
// Converting the plain ones here would fight that existing design, not
// help it, so this batch stops before them.
const fetchRoles = useCallback(async () => {
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
}, []);

const updateRolePrompt = useCallback(async (roleName, brief) => {
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
}, []);

const setRolePinned = useCallback(async (roleName, pinned) => {
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
}, []);

// Capture — one function per ingestor family (§4.2), all landing through
// write_ingested_source() server-side into the exact same node shape, so
// IngestionDropzone.jsx can treat every one of these identically: call,
// await {node_ids, title}, done.
//
// CHANGED — bug audit §3 ("stuck Ingesting…"): each ingestor now takes an
// optional AbortSignal so a caller can bound how long it's willing to
// wait on a single request (see IngestionDropzone.jsx's withTimeout()).
// A plain `await fetch(...)` has no timeout of its own -- if the backend
// pipeline (OCR/parse -> chunk -> embed -> summarize) runs long, the
// request just sits open indefinitely from the browser's point of view,
// which is what reads as a progress row stuck on "Ingesting…" forever.
//
// CHANGED — Data Layer §9c: each ingestor now also takes an optional
// trailing `sessionId`, threaded straight into the request as
// `session_id` (api/server.py's five upload endpoints forward it to
// process_upload(), which passes it to eo/notify.py's notify() —
// without it, §9b's WebSocket push has no session_id to fire on and
// silently no-ops for every upload). These five functions stay plain,
// unbound module functions -- IngestionDropzone.jsx and
// lib/ingestDispatch.js still call them directly by name with the same
// (wsId, file/url, signal) shape they always have. It's the *_wrapped
// versions below, built inside SessionProvider and exported under
// these same five names in the context value, that supply sessionId —
// see that section's comment for why binding it there instead of here
// keeps every existing call site untouched.

async function ingestClip(wsId, url, signal, sessionId) {
  const res = await fetch(`${API_URL}/api/notes/clip`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ url, workspace_id: wsId, session_id: sessionId || null }),
    signal,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function ingestVideoUrl(wsId, url, signal, sessionId) {
  const res = await fetch(`${API_URL}/api/notes/video`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ url, workspace_id: wsId, session_id: sessionId || null }),
    signal,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function ingestFile(wsId, file, signal, sessionId) {
  const form = new FormData();
  form.append("workspace_id", wsId);
  form.append("file", file);
  if (sessionId) form.append("session_id", sessionId);
  const res = await fetch(`${API_URL}/api/notes/import`, {
    method: "POST",
    headers: await authHeaders(),
    body: form,
    signal,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function ingestPdfFile(wsId, file, signal, sessionId) {
  const form = new FormData();
  form.append("workspace_id", wsId);
  form.append("file", file);
  if (sessionId) form.append("session_id", sessionId);
  const res = await fetch(`${API_URL}/api/notes/pdf`, {
    method: "POST",
    headers: await authHeaders(),
    body: form,
    signal,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

async function ingestVoiceFile(wsId, file, signal, sessionId) {
  const form = new FormData();
  form.append("workspace_id", wsId);
  form.append("file", file);
  if (sessionId) form.append("session_id", sessionId);
  const res = await fetch(`${API_URL}/api/notes/voice`, {
    method: "POST",
    headers: await authHeaders(),
    body: form,
    signal,
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

// NEW — Notebooks integration guide §6.6/§7 (Phase 3): short
// agent-written blurbs written by agents/concept_linker.py, read by
// KnowledgeGraphView.jsx's node-click rationale panel. Read-only on
// the frontend by design -- there's no corresponding save function
// here on purpose, matching api/server.py's GET-only route.
async function fetchNodeSummaries(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/graph/node_summaries`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return {};
  return res.json();
}

// NEW — Backlinks-as-topic-tree: eo/secondary_data.py's {topics,
// connections} document, re-projected server-side into the same
// {nodes, edges} shape fetchWorkspaceNodes()/fetchGraphEdges() already
// return, so KnowledgeGraphView.jsx needs no new fetch-shape handling.
// Read-only, same "no corresponding save function" posture as
// fetchNodeSummaries above -- this store's only write path is
// apply_patch() (source_manager.py / backlink_detector.py), never a
// direct frontend call.
async function fetchTopicsGraph(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/topics/graph`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return { nodes: [], edges: [] };
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

// FIX — bug audit §9 (candidates accept/reject write path): both of
// these used to take a plain list `index`. Switched to `candidate_id`
// to match eo/note_candidates.py's accept_candidate/reject_candidate —
// see that module's docstring for why an index isn't safe once two
// users can be reviewing the same pending list at once.
async function acceptNoteCandidate(wsId, candidateId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/notes/candidates/${candidateId}/accept`, {
    method: "POST",
    headers: await authHeaders(),
  });
  return res.json();
}

async function rejectNoteCandidate(wsId, candidateId) {
  await fetch(`${API_URL}/api/workspaces/${wsId}/notes/candidates/${candidateId}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
}

// Workspace facts (eo/workspace_facts.py, Part 0 §0.3) — durable
// brand_voice/target_user/tech_stack/custom facts for a workspace, plus
// the agent-proposed candidates queue. Same accept/reject shape as the
// note candidates just above (workspace_facts.py's accept_candidate/
// reject_candidate now take a stable candidate_id, not a list index —
// see bug audit §9).

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

async function acceptFactCandidate(wsId, candidateId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/facts/candidates/${candidateId}/accept`, {
    method: "POST",
    headers: await authHeaders(),
  });
  return res.json();
}

async function rejectFactCandidate(wsId, candidateId) {
  await fetch(`${API_URL}/api/workspaces/${wsId}/facts/candidates/${candidateId}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
}

// Corrections + Patch Review (Data Layer architecture §8c) — §8a's
// Corrections tab posts a plain-language correction here; the server
// runs agents/correction_locator.py (§8b) and either queues a
// candidate for Patch Review below or returns a reason there was
// nothing to locate. Same accept/reject shape as every other
// candidate store above — a candidate_id, never a list index.

async function submitCorrection(wsId, { text, scopeNodeId }) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/corrections`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ text, scope_node_id: scopeNodeId ?? null }),
  });
  return res.json();
}

async function fetchPatchCandidates(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/corrections/candidates`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return [];
  return res.json();
}

async function acceptPatchCandidate(wsId, candidateId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/corrections/candidates/${candidateId}/accept`, {
    method: "POST",
    headers: await authHeaders(),
  });
  return res.json();
}

async function rejectPatchCandidate(wsId, candidateId) {
  await fetch(`${API_URL}/api/workspaces/${wsId}/corrections/candidates/${candidateId}`, {
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

// NEW — bug audit §8 ("unread/new content" dots): backs the GET
// /api/workspaces/{ws_id}/panels route (api/server.py's
// list_workspace_panel_content → eo/panel_content.py's list_content),
// which already existed and already returns { panel_key: {..., updated_at} }
// for every saved panel in one round trip — it just had no frontend
// caller yet. Deliberately not merged into fetchPanelContent itself:
// that one's callers (MindMapView, WorkflowsView, StudyView) want a
// single panel's `content` to render; this one's caller
// (NotebooksTab's loadNotebookData) only wants the updated_at map to
// diff against a last-viewed mark, and fetching all of them via N
// single-panel calls just to read a timestamp would be wasteful.
async function fetchPanelContentList(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/panels`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return {};
  return res.json();
}

async function savePanelContent(wsId, panelKey, content) {
  // CHANGED — bug audit §9 trace: this used to call res.json() straight
  // through with no res.ok check, so a failed save (e.g. an unknown
  // panel_key -- see StudyView's study_guide fix, the same class of bug
  // this would have hidden) still resolved normally and let the caller
  // show "Saved." Throwing here means StudyView's handleLoad() (and any
  // other caller) needs a try/catch around this call the same way it
  // already has one around synthesizePodcast/buildVideoOverview -- not
  // silently succeeding is the point.
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/panels/${panelKey}`, {
    method: "PUT",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ content }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data?.detail || `Failed to save panel "${panelKey}"`);
  return data;
}

// Notebooks "Generate" command (Notebooks integration guide §4, §6) —
// the picker/chip-confirmation flow's single dispatch call. `targets` is
// a list of panel_key strings (see api/server.py's
// NOTEBOOKS_GENERATE_TARGETS); `scope` is optional — omitted/empty means
// "whole notebook," same convention as everywhere else this guide
// touches. Returns { branches: [{panel_key, status, result|error}] } —
// a per-target result list, not one shared payload, so a partial
// failure (e.g. Backlinks' concept pass erroring) doesn't take down
// Flashcards' result in the same response.
async function generateNotebooks(wsId, targets, scope) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/notebooks/generate`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ targets, scope: scope || null }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

// Notebooks Chat-First refinement, Phase 2 step 2.5 — thin client over
// POST /api/workspaces/{ws_id}/notebooks/classify-intent. Log-only for
// now: WorkspaceChatPanel.jsx's send path calls this alongside sendTask()
// but doesn't branch on the result yet (that's step 2.6). Returns the
// backend's normalized shape straight through:
//   { tool_calls: [{name, arguments}], ambiguous, content, error }
// The endpoint itself never 500s on a classification failure (see
// utils/llm_client.py's classify_tool_intent() docstring) -- it always
// comes back 200 with an `error` string set instead -- but this wrapper
// still guards the fetch itself (network failure, auth failure, etc.)
// the same way, so a broken classification call can never throw into
// the caller's await and interrupt the real send path.
async function classifyIntent(wsId, message) {
  try {
    const res = await fetch(`${API_URL}/api/workspaces/${wsId}/notebooks/classify-intent`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ message }),
    });
    if (!res.ok) {
      return { tool_calls: [], ambiguous: false, content: null, error: `${res.status} ${res.statusText}` };
    }
    return res.json();
  } catch (err) {
    return { tool_calls: [], ambiguous: false, content: null, error: String(err.message || err) };
  }
}

// NEW — Notebooks Chat-First refinement, Phase 6 step 6.8. Thin client
// over the SAME PUT /api/workspaces/{ws_id}/progress route step 6.5's
// manual-override board already uses (see api/server.py's
// put_workspace_progress()) — "mark X as done" from chat is just
// another caller of that one endpoint, status="done", no notes. No
// new backend route needed: reusing the manual-override path is
// exactly what makes this low-stakes/reversible (guide's step 6.8
// decision — no confirmation needed), since it's the same one-field
// PUT a person could already do by hand from the board.
async function markTopicDone(wsId, topicId) {
  const res = await fetch(
    `${API_URL}/api/workspaces/${wsId}/progress?topic_id=${encodeURIComponent(topicId)}`,
    {
      method: "PUT",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ status: "done" }),
    }
  );
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

// NEW — Notebooks Chat-First refinement, Phase 6 step 6.9. Read side for
// the Not Started/Ongoing/Done board view: thin client over GET
// /api/workspaces/{ws_id}/progress. Omitting topicId returns the whole
// {topic_id: record} map for the workspace in one call — study_progress.
// get_progress()'s "no topic_id" branch, exactly what the board needs to
// render every touched topic at once. Passing topicId narrows to a
// single record (unused by the board itself today, kept for parity with
// the route's other callers). Fails soft to an empty map/`null` rather
// than throwing, since a board render shouldn't hard-fail a whole
// notebook load over one flaky request — same posture fetchTopicsGraph()
// above already takes.
async function fetchWorkspaceProgress(wsId, topicId) {
  const qs = topicId ? `?topic_id=${encodeURIComponent(topicId)}` : "";
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/progress${qs}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return topicId ? null : {};
  return res.json();
}

// NEW — step 6.9. General manual override for the board — same PUT
// markTopicDone() above already calls, just not hardcoded to
// status="done": lets a person drag/click a topic into any column
// (Not Started/Ongoing/Done), or edit its notes, straight from the
// board. `status`/`notes` are both optional and independent, mirroring
// set_progress()'s own merge-update semantics — pass only what changed.
async function setWorkspaceProgress(wsId, topicId, { status, notes } = {}) {
  const res = await fetch(
    `${API_URL}/api/workspaces/${wsId}/progress?topic_id=${encodeURIComponent(topicId)}`,
    {
      method: "PUT",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ status, notes }),
    }
  );
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
  return res.json();
}

// Per-topic workflow, triggered by a Mind Map node click (step 4) — thin
// client over POST /api/workspaces/{ws_id}/topics/workflow (step 2).
// Deliberately separate from generateNotebooks() above: that one dispatches
// against NOTEBOOKS_GENERATE_TARGETS panel_keys and returns a per-target
// branch list persisted server-side; this always addresses exactly one
// topic label, is never persisted, and returns the single
// {title, description, steps, mermaid} dict straight through for the
// caller (DiagramsView, step 8) to hold in its own per-topic state.
// `sourceNodeIds` is optional — omitted/empty means "search the whole
// notebook's topics," same convention generateNotebooks' `scope` uses.
async function generateTopicWorkflow(wsId, topicLabel, sourceNodeIds) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/topics/workflow`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify({ topic_label: topicLabel, source_node_ids: sourceNodeIds || null }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.detail || `${res.status} ${res.statusText}`);
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

// CHANGED — step 6.7 gap fix: threads an optional `topicId` through to
// the request body as `topic_id`, matching RecordQuizAttemptRequest's
// field of the same name (api/server.py) that record_quiz_attempt_endpoint
// already reads to bump study_progress on a passing score (step 6.7).
// Until this patch, nothing in the frontend ever populated that field —
// the backend hook existed but no caller could reach it. `topicId` is
// optional and simply omitted from the body (not sent as null/undefined)
// when the caller doesn't have one, same "only send what you know"
// posture as RecordQuizAttemptRequest.topic_id's own doc comment.
//
// NOTE: today's only call site (NotebooksTab.jsx's whole-notebook Quiz
// tab, `scopeAllowed: "whole"` per notebookCapabilities.js) has no topic
// to pass and deliberately isn't changed to invent one here. This plumbing
// is what a future topic-scoped quiz launch (e.g. Phase 6's Not Started/
// Ongoing/Done board, or promoting `study_quiz` to a topic-scoped
// capability) will call into once it exists.
async function recordQuizAttempt(wsId, quizNodeId, quizText, answers, topicId) {
  const body = { workspace_id: wsId, quiz_node_id: quizNodeId, quiz_text: quizText, answers };
  if (topicId) body.topic_id = topicId;
  const res = await fetch(`${API_URL}/api/notes/study/quiz/attempts`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify(body),
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

// CHANGED — step 6.11.b: topicId is a new optional 3rd param, threaded
// down from NotebooksTab.jsx's WorkflowCard "Work through" button via
// onOpenSubChat -> handleOpenSubChat. It's accepted and logged here only
// — sendTask(taskText) below is untouched, so nothing about today's
// dispatch behavior changes. Step 6.11.c is what actually adds topic_id
// to sendTask's POST body / the TaskRequest model server-side; until
// that lands this value goes nowhere past this console.debug.
async function openScopedSubChat(wsId, taskText, topicId = null) {
  // Step 6.11.c: topicId now actually reaches sendTask() and rides the
  // /api/task POST body as topic_id. The server still only logs it
  // (see api/server.py's post_task()) — 6.11.f is what makes routing
  // consult it — so this is still safe to land ahead of that.
  const chatId = await createWorkspaceChat(wsId);
  await sendTask(taskText, topicId);
  return chatId;
}

  // NEW — §5: manage-batch modal actions (rename / unlink members /
  // delete the whole batch). All three touch batch membership, which
  // also changes linked_chat_ids server-side (see eo/memory_batch.py),
  // so each refreshes both `batches` and `chats` the same way §3/§4's
  // create flow already does.

  // Step 2.3i (perf audit item #2, ninth useCallback batch): batch
  // management (renameBatch/unlinkBatchMembers/deleteBatch) here, plus
  // persistMessage below. switchChat/createNewChat are also part of
  // this batch but got MOVED up near refreshChatList (right after line
  // 774) instead of staying in their original textual position --
  // see the comment there for why. Leaving them declared here would
  // have been a real crash, not just a missed-optimization note like
  // the last few batches flagged: removeWorkspaceChat's dependency
  // array (2.3c, line 919, evaluated synchronously during render, not
  // deferred like a useEffect) already references switchChat and
  // createNewChat. If those stayed `const`-declared down here at their
  // original spot -- after removeWorkspaceChat -- render would hit a
  // genuine temporal-dead-zone ReferenceError the first time through,
  // not the "safe because it's called later, from an event handler"
  // pattern every previous batch's ordering check has been able to
  // rely on. Moving the declaration earlier is the actual fix; a
  // comment explaining why it'd be fine (which is what I almost wrote
  // here) would have been wrong.
  const renameBatch = useCallback(async (batchId, name) => {
    await fetch(`${API_URL}/api/batches/${batchId}/rename`, {
      method: "PATCH",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ name }),
    });
    await fetchBatches();
  }, [fetchBatches]);

  const unlinkBatchMembers = useCallback(async (batchId, chatIds) => {
    await fetch(`${API_URL}/api/batches/${batchId}/unlink`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ chat_ids: chatIds }),
    });
    await fetchBatches();
    await refreshChatList();
  }, [fetchBatches, refreshChatList]);

  const deleteBatch = useCallback(async (batchId) => {
    await fetch(`${API_URL}/api/batches/${batchId}`, {
      method: "DELETE",
      headers: await authHeaders(),
    });
    await fetchBatches();
    await refreshChatList();
  }, [fetchBatches, refreshChatList]);

  // (switchChat and createNewChat used to be declared here -- moved up
  // near refreshChatList; see the note on renameBatch above for why.)

  const persistMessage = useCallback(async (message) => {
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
  }, [sessionId]);

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

  // topicId (NEW — Step 6.11.c) is optional and, for now, purely passed
  // through to the server for logging — see api/server.py's post_task().
  // 6.11.f is what makes the backend actually act on it. Every existing
  // call site (dock.sendTask, PlanTab's WireframesPanel, etc.) keeps
  // working unchanged since this param defaults to null.
  async function sendTask(taskText, topicId = null) {
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
        body: JSON.stringify({
          task_text: taskText,
          session_id: sessionId,
          mode,
          ...(topicId ? { topic_id: topicId } : {}),   // NEW — Step 6.11.c
        }),
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

  // NEW — Data Layer §9c: binds sessionId (this component's own state,
  // not available to the plain module functions above) onto each of
  // the five ingestors, plus the processingWorkspaces start/settle
  // pair from the WS effect above — so IngestionDropzone.jsx and
  // lib/ingestDispatch.js, which both call these by name with the
  // existing (wsId, file/url, signal) shape, get both §9c behaviors
  // "for free" the next time they run, with no call-site changes.
  // Exported below under the SAME five names the module functions
  // have, replacing them in the context value — nothing outside this
  // file should ever import the raw module functions directly.
  // Step 2.3h (perf audit item #2, eighth useCallback batch): the
  // *_wrapped ingest functions -- these are what actually goes into the
  // context value, so getting their deps right matters more than most.
  // Each closes over `sessionId` (state) and calls
  // markSourceProcessingStarted/markSourceProcessingSettled (already
  // useCallback'd, stable references) plus its own plain-function
  // counterpart (ingestClip, ingestVideoUrl, etc. -- deliberately left
  // as plain `function` declarations in 2.3g, so still a new reference
  // every render). Same correctness-first call as removeWorkspaceChat
  // back in 2.3c: the plain ingest function goes in the dep array even
  // though it means these five won't stabilize until that plain
  // function itself is wrapped -- which, per that same 2.3g note, isn't
  // happening, since IngestionDropzone.jsx/lib/ingestDispatch.js need
  // it to stay a plain, directly-importable function. So these five
  // will keep getting new references every render until that tension
  // is resolved by a bigger design change, not a mechanical wrap here.
  const ingestClipWrapped = useCallback(async (wsId, url, signal) => {
    markSourceProcessingStarted(wsId);
    try {
      return await ingestClip(wsId, url, signal, sessionId);
    } finally {
      markSourceProcessingSettled(wsId);
    }
  }, [markSourceProcessingStarted, markSourceProcessingSettled, sessionId, ingestClip]);

  const ingestVideoUrlWrapped = useCallback(async (wsId, url, signal) => {
    markSourceProcessingStarted(wsId);
    try {
      return await ingestVideoUrl(wsId, url, signal, sessionId);
    } finally {
      markSourceProcessingSettled(wsId);
    }
  }, [markSourceProcessingStarted, markSourceProcessingSettled, sessionId, ingestVideoUrl]);

  const ingestFileWrapped = useCallback(async (wsId, file, signal) => {
    markSourceProcessingStarted(wsId);
    try {
      return await ingestFile(wsId, file, signal, sessionId);
    } finally {
      markSourceProcessingSettled(wsId);
    }
  }, [markSourceProcessingStarted, markSourceProcessingSettled, sessionId, ingestFile]);

  const ingestPdfFileWrapped = useCallback(async (wsId, file, signal) => {
    markSourceProcessingStarted(wsId);
    try {
      return await ingestPdfFile(wsId, file, signal, sessionId);
    } finally {
      markSourceProcessingSettled(wsId);
    }
  }, [markSourceProcessingStarted, markSourceProcessingSettled, sessionId, ingestPdfFile]);

  const ingestVoiceFileWrapped = useCallback(async (wsId, file, signal) => {
    markSourceProcessingStarted(wsId);
    try {
      return await ingestVoiceFile(wsId, file, signal, sessionId);
    } finally {
      markSourceProcessingSettled(wsId);
    }
  }, [markSourceProcessingStarted, markSourceProcessingSettled, sessionId, ingestVoiceFile]);

  // Step 2.2 (perf audit item #2): value was previously a plain object
  // literal, rebuilt fresh on every render with a brand-new reference
  // every time -- so every useSession() consumer re-rendered on ANY
  // state change anywhere in this provider, not just the fields it
  // actually uses. useMemo alone does NOT fully fix that yet: most of
  // the ~100 functions below aren't wrapped in useCallback, so their
  // references are still new every render, which still invalidates
  // this memo every render. That's the deliberate next step (2.3,
  // batched) -- this step lands the memo boilerplate and the complete,
  // accurate dependency list first, so each useCallback added in 2.3
  // starts paying off immediately with no further change needed here.
  const value = useMemo(() => ({
  sessionId, API_URL,
  messages, loading,
  chats, chatsLoading,
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
  handleUsageEvent,   // NEW — 3e: threaded into WorkspaceDockProvider as onUsageEvent (option 1, usage-event ownership)
  macroLoopDecisions,
  roleRequests,
  mode, setMode,
  pusherConnected,
  fetchTopicsGraph, topicPulsingIds,   // NEW — Backlinks-as-topic-tree
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
  ingestClip: ingestClipWrapped, ingestVideoUrl: ingestVideoUrlWrapped,
  ingestFile: ingestFileWrapped, ingestPdfFile: ingestPdfFileWrapped, ingestVoiceFile: ingestVoiceFileWrapped,
  processingWorkspaces,   // NEW — §9c: Set of workspace ids with an upload still processing server-side
  detectBacklinks,
  fetchNodeSummaries,   // NEW — Notebooks integration guide §6.6: Backlinks concept-graph node-click rationale
  fetchNoteCandidates, acceptNoteCandidate, rejectNoteCandidate,
  fetchWorkspaceFacts, saveWorkspaceFacts, fetchFactCandidates, acceptFactCandidate, rejectFactCandidate,
  submitCorrection, fetchPatchCandidates, acceptPatchCandidate, rejectPatchCandidate,
  fetchPanelContent, savePanelContent, fetchPanelContentList,   // NEW — generic paste-panel persistence (eo/panel_content.py); fetchPanelContentList added for §8 unread dots
  generateNotebooks,   // NEW — Notebooks integration guide §4/§6: the picker/chip-confirmation "Generate" command
  generateTopicWorkflow,   // NEW — step 4: Mind Map node click -> single per-topic workflow (see step 2's endpoint)
  classifyIntent,   // NEW — Notebooks Chat-First refinement Phase 2 step 2.5: log-only tool-calling classification
  markTopicDone,   // NEW — Notebooks Chat-First refinement Phase 6 step 6.8: "mark X as done" chat tool
  fetchWorkspaceProgress, setWorkspaceProgress,   // NEW — Phase 6 step 6.9: Not Started/Ongoing/Done board view
  fetchDeviceSpec, refreshPartPrices, toggleInstructionStep, // NEW — Blueprint (Plan sub-tab)
  proposeClusters, fetchClusterCandidates, acceptClusterCandidate, rejectClusterCandidate,
  openScopedSubChat,
  gradeQuiz, recordQuizAttempt, fetchMissedQuestions,
  synthesizePodcast,   // NEW — Part 4 §4.4: podcast synthesis
  buildVideoOverview,   // NEW — Part 4 §4.4: video overview (narrated slideshow)
  fetchWorkspaceAudit, fetchMyAudit,   // NEW — Part 8.6: audit log
  }), [
    sessionId, API_URL, messages, loading, chats, chatsLoading,
    refreshChatList, getWorkspaceIdForChat, batches, fetchBatches, createBatch, estimateBatch,
    renameBatch, unlinkBatchMembers, deleteBatch, workspaces, fetchWorkspaces, createWorkspace,
    createWorkspaceWithChats, renameWorkspace, addWorkspaceChat, createWorkspaceChat, removeWorkspaceChat, deleteWorkspace,
    promoteWorkspace, fetchWorkspaceMembers, addWorkspaceMember, updateWorkspaceMemberRole, removeWorkspaceMember, leaveWorkspaceMembership,
    forceRemoveOwner, fetchWorkspaceVotes, castWorkspaceVote, setWorkspaceAttribution, setMemberAttributionGrant, liveDecision,
    liveSteps, usageStats, usageHistory, combinedUsageHistory, routeTrace, dependencyMap,
    structurePlan, handleUsageEvent, macroLoopDecisions, roleRequests, mode, setMode,
    pusherConnected, fetchTopicsGraph, topicPulsingIds, notifications, unreadCount, markNotificationsRead,
    exportWorkspace, importWorkspace, activeMessageIndex, setActiveMessageIndex, sendTask, registerProject,
    reviewBeforeDispatch, setReviewBeforeDispatch, pendingHireReview, confirmHireReview, cancelHireReview, pausedApproval,
    resumeRun, templateRuns, runTemplate, fetchWorkspaceNodes, deleteWorkspaceNode, fetchGraphEdges,
    buildExtractionTable, fetchSimulationResults, fetchRoles, updateRolePrompt, setRolePinned, ingestClipWrapped,
    ingestVideoUrlWrapped, ingestFileWrapped, ingestPdfFileWrapped, ingestVoiceFileWrapped, processingWorkspaces, detectBacklinks,
    fetchNodeSummaries, fetchNoteCandidates, acceptNoteCandidate, rejectNoteCandidate, fetchWorkspaceFacts, saveWorkspaceFacts,
    fetchFactCandidates, acceptFactCandidate, rejectFactCandidate, submitCorrection, fetchPatchCandidates, acceptPatchCandidate,
    rejectPatchCandidate, fetchPanelContent, savePanelContent, fetchPanelContentList, generateNotebooks, generateTopicWorkflow,
    classifyIntent, markTopicDone, fetchWorkspaceProgress, setWorkspaceProgress, fetchDeviceSpec, refreshPartPrices,
    toggleInstructionStep, proposeClusters, fetchClusterCandidates, acceptClusterCandidate, rejectClusterCandidate, openScopedSubChat,
    gradeQuiz, recordQuizAttempt, fetchMissedQuestions, synthesizePodcast, buildVideoOverview, fetchWorkspaceAudit,
    fetchMyAudit,
  ]);
  return (
    <SessionContext.Provider value={value}>
      {children}
    </SessionContext.Provider>
  );
}