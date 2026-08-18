"use client";
// frontend/app/context/WorkspaceDockContext.jsx
//
// Step 3a/3b/3c of the §2.6 build order (architecture doc §2.3/§2.4/§2.5),
// PLUS a step identified while starting 3e (not separately lettered in the
// original doc): 3a built the state shape + keying, 3b added the
// session-Pusher-channel subscription, 3c ported the run functions
// (sendTask/resumeRun/confirmHireReview/persistMessage/etc). This pass
// additionally ports switchChat/createNewChat/renameChat/deleteChat/
// linkChats — needed before ANY consumer can safely be rewired to pass a
// real workspaceId in (see WorkspaceChatPanel's 3d comment): those five
// are what actually populate a dock's sessionId/messages in the first
// place, and until they existed here, flipping a consumer to dock mode
// would just show it empty. See WorkspaceDockProvider's own comment for
// why they needed refreshChatList/getWorkspaceIdForChat/getChats threaded
// in as props rather than an import from SessionContext.
//
// ALSO in this pass: a small `lastActiveChatId` primitive (see its own
// comment further down), added because ChatSidebar's row highlight has
// no single dock to read a "the active chat" sessionId from once real
// consumers start passing distinct workspaceIds into switchChat's per-
// chat key resolution.
//
// Still NOT done: 3d (rewire WorkspaceChatPanel — landed separately,
// dual-mode) and 3e (rewire the remaining 8 consumers, one at a time).
// SessionContext.jsx is UNTOUCHED by this patch — it keeps its own,
// independent subscription to the same channel name for now, AND its own
// (now-duplicate) copies of switchChat/createNewChat/etc. That means, for
// any dock actually wired up post-3e, a chat's events get processed twice
// (once by SessionContext's copy, once by this one) until 3e removes
// SessionContext's copies. That's intentional: nothing calls
// useWorkspaceDock()/useWorkspaceDockActions() from real UI yet (3e hasn't
// landed for any of the 8 remaining consumers), so today this is dormant —
// the double-processing scenario doesn't exist in the running app yet,
// only once wiring starts. Flagging so it isn't a surprise when 3e lands.
//
// RESOLVED (was an open question for 3e): two branches of the original
// handler — usage_update and quota_alert — feed usageStats/usageHistory/
// combinedUsageHistory, which §2.4 explicitly keeps app-wide on
// SessionContext, not per-dock. Went with option 1 of the three raised:
// this file's handleDockEvent still does NOT touch any dock's own state
// for those two event types, but it does forward them, verbatim, to
// SessionContext via a threaded-in `onUsageEvent` callback (same
// round-trip shape as refreshChatList/getWorkspaceIdForChat/
// fetchWorkspaces above — see AppShell.jsx's WorkspaceDockBridge, which
// passes SessionContext's `handleUsageEvent` in as this prop).
// SessionContext's own copy of that logic was extracted into
// `handleUsageEvent` for exactly this reuse — no duplicated branch
// bodies, one implementation, two call sites (its own subscription,
// still live today, and this one). This is what actually clears
// SessionContext's session-${sessionId} subscription for deletion once
// 3e's remaining consumer parity (ResearchTab/PlanTab) lands — nothing
// upstream of it would be orphaned anymore.
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
import { getSuggestedFollowUp } from "../lib/notebookAffinities";   // NEW — Phase 3 step 3.2
import { TARGETS } from "../lib/notebookCapabilities";   // NEW — Phase 3 step 3.2
import { readProactiveSuggestionsEnabled } from "../hooks/useProactiveSuggestions";   // NEW — Phase 3 step 3.7

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

// NEW — Phase 3 step 3.2. Same reduction NotebooksGeneratePicker.jsx and
// WorkspaceChatPanel.jsx each already build locally (see
// notebookCapabilities.js's own step-1.7 comment on that duplication) —
// added here too rather than importing a shared export, to keep this
// step's diff to "one more local copy," not a refactor of the existing
// ones.
const TARGETS_BY_KEY = Object.fromEntries(TARGETS.map((t) => [t.key, t]));

// Duplicated from SessionContext.jsx for the same reason as API_URL above.
const ACTIVE_CHAT_KEY = "minime_active_chat_id";

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
// `notebooksGenerateRun` -- NEW, Notebooks integration guide §5. Mirrors
// NotebooksGeneratePicker.jsx's own local run state (targets/branches)
// into the dock so WorkingPanel.jsx -- which has no other connection to
// the picker, and no `messages` entry to hang a snapshot off since a
// Generate command isn't a chat turn -- can render the same run as its
// own multi-branch RoutingTraceGraph section. Shape: `{ targets:
// string[], branches: [{panel_key, label, status, error?, result?}] }`,
// or `null` when no Generate command has run yet this session. Picker
// and WorkingPanel resolve to the SAME dock key off the same
// workspaceId (see normalizeDockKey below), so writing here from one
// and reading in the other needs no extra plumbing.
function makeInitialDockState() {
  return {
    messages: [],
    sessionId: null,
    loading: false,
    // Perf audit item #3: paired with the messages page fetched by
    // switchChat/loadOlderMessages below — hasMoreOlder is whether
    // chat_store.get_chat() reported more messages older than what's
    // currently loaded; loadingOlder guards loadOlderMessages against
    // being fired again (e.g. from a second scroll event) while a page
    // is already in flight.
    hasMoreOlder: false,
    loadingOlder: false,
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
    notebooksGenerateRun: null, // NEW — see note above
    // NEW — Phase 4 step 4.5: one entry per (workspace, panel_key) live
    // generation, transitioning "started" -> "done"/"error" in place as
    // the matching Pusher events arrive (see handleDockEvent's new
    // generation_started/generation_done/generation_error branch below).
    // Deliberately separate from notebooksGenerateRun above rather than
    // reusing its `branches` shape: that field is the Picker's own local
    // echo of ITS OWN run (targets/branches, written straight from
    // runGenerate's fetch response, no Pusher involved) and step 2.9
    // removes the Picker UI that owns it. This one is the session-wide,
    // chat-native notification feed Phase 4 is building instead — it has
    // to keep working for a chat-triggered generation that never went
    // through the Picker at all.
    generationNotifications: [],
    // NEW — Phase 3 step 3.8: "don't repeat the same nudge/pairing
    // twice in a session." Tracks which `${sourceKey}->${suggestedKey}`
    // pairs (notebookAffinities.js's NOTEBOOK_AFFINITIES keys/values)
    // have already been appended as a suggestion message in THIS dock
    // — i.e. this workspace's currently-open chat session, the same
    // scope every other per-dock field above already lives at. A plain
    // array (not a Set) since dock state is a plain object read via
    // useSyncExternalStore's snapshot equality — matches
    // generationNotifications/liveSteps' own "array of plain values"
    // shape rather than introducing a non-serializable field type this
    // file doesn't otherwise use anywhere in dock state.
    shownSuggestionPairs: [],
    // NEW — CO4 patch 3: raw feed of the new "cache_hit"/
    // "worker_pool_selection" events (see handleDockEvent's new branch
    // below and relay/emitter.py's own comment on both event types).
    // Plain array of {type, agent, payload, timestamp}, same "array of
    // plain values" shape as liveSteps/routeTrace above — no rendering
    // yet (CO4 patch 4 is what turns this into timeline nodes); this
    // patch is only responsible for the data existing and reaching the
    // frontend.
    decisionEvents: [],
  };
}

// Bug fix: shared, stable default snapshot for the "no key resolved yet"
// case in useWorkspaceDock()'s getSnapshot below. A null key means the
// dock is inert (setDockState/sendTask/etc. are already no-ops for it),
// so every consumer without a resolved key can safely share this one
// reference — it's never mutated, only ever read. Calling
// makeInitialDockState() fresh inside getSnapshot instead of reusing this
// was the cause of the "Maximum update depth exceeded" crash: a new
// object every call means useSyncExternalStore's Object.is check never
// sees two equal snapshots in a row, so it re-renders forever. Since
// every current consumer (3e hasn't landed yet for any of the 8 dock
// consumers) resolves to a null key, this loop fired on every one of
// them.
const EMPTY_DOCK_STATE = makeInitialDockState();

const WorkspaceDockStoreContext = createContext(null);

export function WorkspaceDockProvider({ children, refreshChatList, getWorkspaceIdForChat, getChats, fetchWorkspaces, onUsageEvent }) {
  // Option 1 (chosen over letting every UI call site hand-coordinate a
  // SessionContext write + a dock write, or over a thinner wrapper hook
  // with the same fragility): switchChat/createNewChat/renameChat/
  // deleteChat/linkChats move HERE, fully, rather than staying split
  // across two files. They still need three things that only
  // SessionContext actually owns — refreshChatList (app-wide `chats`
  // list), a chatId->workspaceId lookup (also over app-wide `workspaces`),
  // and the current `chats` array itself (for deleteChat's "switch to
  // another chat" fallback). Rather than import SessionContext (the
  // mother/child rule from 3b/3c), these three are passed down as props
  // from a small bridge component that sits inside SessionProvider and
  // calls useSession() itself — see AppShell.jsx's WorkspaceDockBridge.
  //
  // These props are NOT read directly by the store below. The store is
  // built once, lazily, in a ref (storeRef, further down) — capturing
  // these props directly in that one-time closure would freeze them at
  // whatever they were on the provider's first render, which goes stale
  // the moment `chats`/`workspaces` change on SessionContext. Instead
  // they're kept in a ref that's reassigned every render, and the store's
  // action functions read `callbacksRef.current.xxx` at call time. Same
  // "avoid a stale closure via a ref" pattern this file already uses for
  // stepSeqs/openStepStacks/channelBindings.
  const callbacksRef = useRef({});
  callbacksRef.current = { refreshChatList, getWorkspaceIdForChat, getChats, fetchWorkspaces, onUsageEvent };

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

    // NEW — step 3e: a tiny, dock-key-agnostic external store for "which
    // chat did the user most recently switch to, anywhere in the app."
    // Separate from the per-key `states` map above on purpose: once
    // switchChat/createNewChat write into whichever per-workspace dock a
    // chat belongs to (rather than one shared SessionContext sessionId),
    // there's no longer a single dock that's "the" active one — two docks
    // can each correctly be showing their own chat at once. ChatSidebar's
    // row highlight still needs *some* single answer to "which row looks
    // selected", though, and this is the least surprising one: whichever
    // chat most recently was the target of switchChat/createNewChat, full
    // stop, independent of which dock it landed in. Kept as its own
    // useSyncExternalStore-compatible primitive rather than folding it
    // into `states` under a magic key, so it can't collide with a real
    // workspace/chat id.
    let lastActiveChatId = null;
    const lastActiveChatListeners = new Set();
    const getLastActiveChatId = () => lastActiveChatId;
    const setLastActiveChatId = (chatId) => {
      lastActiveChatId = chatId;
      lastActiveChatListeners.forEach((cb) => cb());
    };
    const subscribeLastActiveChatId = (callback) => {
      lastActiveChatListeners.add(callback);
      return () => lastActiveChatListeners.delete(callback);
    };

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

    // NEW — Bug 7 fix (0b), second half. Dock-store port of
    // SessionContext.jsx's own refetchFullStep (see that file's comment
    // for the full reasoning on why `role` is the right re-fetch key and
    // why a 404 is a no-op, not an error) — same shrunk-envelope re-fetch,
    // retargeted from stepsRef/setLiveSteps to this store's
    // states.get(key)/setState(key, ...), same as every other branch in
    // handleDockEvent below already is relative to its SessionContext
    // counterpart.
    const refetchFullStep = async (key, sid, role, stepId) => {
      try {
        const res = await fetch(
          `${API_URL}/api/task/${sid}/step/${encodeURIComponent(role)}/full`,
          { headers: await authHeaders() }
        );
        if (!res.ok) return;
        const { text } = await res.json();
        setState(key, (prev) => ({
          liveSteps: prev.liveSteps.map((s) => (s.id === stepId ? { ...s, summary: text } : s)),
        }));
      } catch (err) {
        console.warn("[Bug 7 / 0b] full-step re-fetch failed, keeping shrunk summary:", err);
      }
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
        // NEW — Phase 8 step 8.4, ported from SessionContext.jsx's own
        // dispatch_event branch (see that file's comment for the full
        // ordering argument). Folded into one setState call rather than
        // two, since this store's setState only shallow-merges one
        // patch per call -- a second call here would just be a second
        // notify() for the same logical update.
        setState(key, (prev) => ({
          routeTrace: [...prev.routeTrace, { destination: payload?.destination, reason: payload?.reason }],
          liveSteps: payload?.destination && prev.liveSteps.length > 0
            ? prev.liveSteps.map((s, i) =>
                i === prev.liveSteps.length - 1
                  ? { ...s, calledOutTo: { destination: payload.destination, reason: payload.reason } }
                  : s
              )
            : prev.liveSteps,
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
      // NEW — CO4 patch 3: both new event types land in the same
      // `decisionEvents` array — they're the same kind of thing from
      // the frontend's point of view (a decision point that happened
      // mid-run, outside the normal agent_start/agent_done sequence),
      // just distinguished by `type` for whatever CO4 patch 4 renders
      // per type. No rendering here — see decisionEvents' own comment
      // in makeInitialDockState above.
      if (
        eventType === "cache_hit" ||
        eventType === "worker_pool_selection" ||
        // NEW — F2 Part 5: relay/emitter.py's LOCAL_TOOL_* events (the
        // daemon tool-call log -- eo/local_workspace_tools.py's
        // propose_action/confirm_action/deny_action/list_workspace_dir/
        // read_workspace_file). Same "decision point outside the normal
        // agent_start/agent_done sequence" bucket cache_hit/worker_pool_
        // selection already land in above -- a local tool call isn't an
        // agent step and has no role to key a StepRow off of, so it goes
        // through this same array rather than liveSteps.
        eventType === "local_tool_proposed" ||
        eventType === "local_tool_confirmed" ||
        eventType === "local_tool_denied" ||
        eventType === "local_tool_executed" ||
        eventType === "local_tool_result"
      ) {
        setState(key, (prev) => ({
          decisionEvents: [
            ...prev.decisionEvents,
            { type: eventType, agent, payload, timestamp: data.timestamp || null },
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
          // NEW — Phase 8 step 8.2, ported from SessionContext.jsx's own
          // agent_start branch (see that file's comment for the full
          // reasoning): `path` rides on every event envelope already,
          // constant for the whole run, and maps to tier via
          // eo/structure.py's PATH_TO_TIER -- stored per-step so
          // AgentStepList.jsx can label a step's tier without reaching
          // outside `steps`, same as the non-dock path.
          path: data.path || null,
          // NEW — Phase 8 step 8.3, ported from SessionContext.jsx's own
          // agent_start branch (see that file's comment for the full
          // reasoning).
          givenRoles: payload?.given_roles || [],
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
        // NEW — Bug 7 fix (0b), second half, ported from
        // SessionContext.jsx's own agent_done branch (see that file's
        // comment for the full reasoning): `data.truncated` is the
        // envelope-level flag relay/emitter.py's emit_event() stamps
        // when this event had to be shrunk to fit Pusher's cap, and
        // `role` is the same string this step was keyed under at
        // agent_start above.
        if (data.truncated && data.session_id) {
          const role = payload?.label || agent;
          refetchFullStep(key, data.session_id, role, targetId);
        }
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
      // NEW — Phase 4 step 4.5: eo/notify.py's generation_started/
      // generation_done/generation_error (step 4.3's VALID_KINDS,
      // step 4.4's notebooks_generate() call sites) all land here, on
      // the same session-${session_id} Pusher channel every other
      // branch above already reads — no new subscription needed (see
      // decisions/step-4.1-notification-transport.md). One entry per
      // (workspace_id, panel_key), updated in place across its
      // started -> done/error lifecycle rather than appended as three
      // separate rows, so step 4.6's notification component renders one
      // pill per generation that transitions state, the same way a
      // BranchRow does. Order in the array is otherwise preserved on
      // update — the row shouldn't jump position just because it
      // finished.
      if (
        eventType === "generation_started" ||
        eventType === "generation_done" ||
        eventType === "generation_error"
      ) {
        const status =
          eventType === "generation_started" ? "started"
          : eventType === "generation_done" ? "done"
          : "error";
        const panelKey = payload?.panel_key;
        const workspaceId = payload?.workspace_id;
        const entry = {
          panelKey,
          workspaceId,
          label: payload?.label,
          status,
          timestamp: data.timestamp || new Date().toISOString(),
        };
        setState(key, (prev) => {
          const idx = prev.generationNotifications.findIndex(
            (n) => n.panelKey === panelKey && n.workspaceId === workspaceId
          );
          const wasAlreadyDone = idx !== -1 && prev.generationNotifications[idx].status === "done";
          const generationNotifications =
            idx === -1
              ? [...prev.generationNotifications, entry]
              : prev.generationNotifications.map((n, i) => (i === idx ? entry : n));

          // NEW — Phase 3 step 3.1/3.2: post-generation cross-sell.
          // Fires once per completed run (guarded by `wasAlreadyDone` so
          // a redelivered/duplicate Pusher event — or an unrelated
          // patch to this same notification, if a later step ever adds
          // one — doesn't append a second suggestion for the same run),
          // AND only when step 3.7's per-browser opt-out
          // (useProactiveSuggestions.js) is still on. Checked here,
          // not just at render time in MessageBubble.jsx, so a person
          // who's opted out never even gets the message written into
          // their thread/persisted history in the first place.
          // Looks up notebookAffinities.js's hardcoded pairing for the
          // panel_key that just finished; if the paired target isn't
          // registered as `enabled` in the Phase 1 manifest (true today
          // of `workflow`, since it's still a Phase 5 stub — see
          // notebookCapabilities.js), there's nothing to suggest, so no
          // message is appended. This is also what quietly defers the
          // mindmap -> workflow pairing until Phase 5 lands workflow as
          // a real, enabled capability: `workflow` is topic-scoped
          // (scopeAllowed: "topic") and a whole-notebook mindmap
          // completion has no single topic to suggest it for, so
          // enabling it blindly here would offer something that can't
          // actually run yet. Re-check that once Phase 5 flips
          // `enabled: true` on it.
          //
          // Message is appended to dock state only (not persisted via
          // persistMessage) — same as the chat-triggered "generation"
          // role message runGenerateTarget() builds in
          // WorkspaceChatPanel.jsx: an ephemeral, client-side thread
          // entry, not a server-recorded chat turn.
          let messagesPatch = {};
          let shownSuggestionPairsPatch = {};
          if (status === "done" && !wasAlreadyDone && readProactiveSuggestionsEnabled()) {
            const suggestedKey = getSuggestedFollowUp(panelKey);
            const suggestedTarget = suggestedKey ? TARGETS_BY_KEY[suggestedKey] : null;
            // NEW — Phase 3 step 3.8: pairing key mirrors
            // notebookAffinities.js's own key->value shape, so it reads
            // the same way as the map itself (`study_flashcards ->
            // study_quiz`) rather than inventing a second encoding.
            const pairingKey = suggestedKey ? `${panelKey}->${suggestedKey}` : null;
            const alreadyShown = pairingKey
              ? prev.shownSuggestionPairs.includes(pairingKey)
              : false;
            if (suggestedTarget && suggestedTarget.enabled !== false && !alreadyShown) {
              messagesPatch = {
                messages: [
                  ...prev.messages,
                  {
                    role: "suggestion",
                    id: `suggest-${panelKey}-${suggestedKey}-${Date.now()}`,
                    sourceKey: panelKey,
                    suggestedKey,
                    workspaceId,
                  },
                ],
              };
              // Recorded whether or not the person ever acts on it —
              // same "counts as shown, not as accepted" posture step
              // 3.8's own wording ("never repeated ... in a session")
              // asks for, and matches eo/prerequisite_suggestions.py's
              // _mark_nudged() marking immediately rather than waiting
              // on an accept/dismiss signal that doesn't exist for
              // this pairing type either.
              shownSuggestionPairsPatch = {
                shownSuggestionPairs: [...prev.shownSuggestionPairs, pairingKey],
              };
            }
          }

          return { generationNotifications, ...messagesPatch, ...shownSuggestionPairsPatch };
        });
        return;
      }
      // usage_update, quota_alert: NOT handled per-dock — these stay
      // app-wide (§2.4), so hand them straight to SessionContext via the
      // threaded-in onUsageEvent callback (3e usage-event ownership,
      // option 1 — see file-header note) instead of updating any dock
      // key's own state.
      if (eventType === "usage_update" || eventType === "quota_alert") {
        callbacksRef.current.onUsageEvent?.(eventType, payload);
        return;
      }
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

    // Drops a dock entirely, including any bound channel. CHANGED — bug
    // #1 fix: this used to have no callers at all, which is exactly why
    // deleting a chat/project left stale Working Panel content behind —
    // deleteChat() below now calls this for the chat's own key, and
    // removeWorkspace() (below) uses it for every key under a deleted
    // workspace.
    const remove = (key) => {
      unbindChannel(key);
      states.delete(key);
      listeners.delete(key);
      stepSeqs.delete(key);
      openStepStacks.delete(key);
    };

    // NEW — bug #1 fix: "delete project" has no equivalent of deleteChat's
    // single-key cleanup, because a workspace can own more than one dock
    // key (today just `ws:{workspaceId}`, but this stays prefix-based
    // rather than a single delete so it keeps working if that ever
    // becomes `ws:{workspaceId}:{chatId}`-style per-chat keys, per the
    // normalizeDockKey comment above). Removes every matching key's
    // state/listeners/channel so nothing under the deleted workspace can
    // bleed into whatever dock key gets reused next.
    const removeWorkspace = (workspaceId) => {
      const prefix = `ws:${workspaceId}`;
      for (const key of states.keys()) {
        if (key === prefix || key.startsWith(`${prefix}:`)) remove(key);
      }
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
        decisionEvents: s.decisionEvents, // NEW — CO4 patch 3
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
        decisionEvents: [], // NEW — CO4 patch 3
      });
      openStepStacks.set(key, []); // stepSeqs is NOT reset — matches original: it only ever counts up, for React key uniqueness across the whole session
    };

    // FIX — "task looks like it stopped when I switch chats" bug:
    // switchChat() (below) reassigns a dock key's `sessionId` to
    // whatever chat the user just clicked into, unconditionally — and
    // setState()'s sessionId-changed branch (above) immediately unbinds
    // that key's Pusher channel and binds a new one for the new chat.
    // If a run dispatched by sendTask()/resumeRun()/confirmHireReview()
    // is still in flight for the OLD chat at that moment, its live
    // events (agent_start/agent_done/decision/etc, still arriving on
    // the old chat's channel) now have nothing listening for them — the
    // run keeps executing normally server-side, but the UI has nothing
    // left tracking it, which is exactly what makes it look stopped.
    // Worse, the run's own completion handler used to look the dock's
    // *current* sessionId up fresh from the store instead of using the
    // one it was actually dispatched for — so when that fetch finally
    // resolved, it would persist its result onto whatever chat the user
    // had switched TO (wrong chat's history gets someone else's
    // message), append that message into the wrong chat's on-screen
    // list, and flip the wrong chat's `loading` flag off even if that
    // chat has its own run going.
    //
    // The fix: every run function captures `dockSessionId` once, at
    // dispatch time, and uses that fixed value for persistence
    // (persistMessageToSession bypasses the key->session store lookup
    // entirely) and for deciding whether it's still safe to touch this
    // dock key's on-screen state (isStaleRun). A run that finishes after
    // its dock slot moved on to a different chat still gets its result
    // correctly saved to ITS chat's history (so switching back to it
    // shows the real outcome, no data lost) — it just no longer
    // clobbers whatever chat is on screen now.
    const persistMessageToSession = async (sessionId, message) => {
      if (!sessionId) return;
      try {
        await fetch(`${API_URL}/api/chats/${sessionId}/messages`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
          body: JSON.stringify({ message }),
        });
      } catch (err) {
        console.error("Failed to persist message:", err);
      }
    };

    const isStaleRun = (key, dockSessionId) => states.get(key)?.sessionId !== dockSessionId;

    // Persists `data` as an assistant message against the session this
    // run was actually dispatched for (dockSessionId), always. Only
    // touches this dock key's live `messages`/extra state (loading,
    // pausedRun, etc.) when that key still belongs to the same session
    // — i.e. the user hasn't switched this dock to a different chat
    // while the run was in flight.
    const finishRun = (key, dockSessionId, taskText, data, extraStateWhenFresh = {}) => {
      const assistantMessage = buildAssistantMessage(key, taskText, data);
      persistMessageToSession(dockSessionId, assistantMessage);
      if (isStaleRun(key, dockSessionId)) {
        console.warn(
          `[dock:${key}] a run dispatched for session ${dockSessionId} finished after this ` +
          `dock switched to a different chat — its result was still saved to the right chat's ` +
          `history, but isn't shown live here.`
        );
        return;
      }
      setState(key, (prev) => ({ messages: [...prev.messages, assistantMessage], ...extraStateWhenFresh }));
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
    // `topicId`/`scope` (FIX — parity with SessionContext.jsx's
    // sendTask): the dock's own sendTask never accepted either, so any
    // caller that needed them (ResearchTab.jsx's Sources scope selector,
    // Notebooks' "Work through" topic hand-off) had no choice but to
    // dispatch through the legacy, non-dock sendTask instead — which is
    // exactly how ResearchTab.jsx ended up bypassing WorkingPanel.jsx's
    // live view entirely (see that file's own fix). Both are optional
    // and default to null/omitted, so every existing caller is unaffected.
    //
    // BUGFIX (bug audit, patch 5) — `key` is "ws:${workspaceId}" for
    // every workspace-scoped dock (normalizeDockKey above), so the real
    // project id is always sitting right here in the closure. Nothing
    // downstream ever forwarded it to the backend: /api/task's
    // TaskRequest.app_slug (backend/api/routes/tasks.py) has stayed
    // unset on every dock-dispatched task, which means
    // task_runner.py's set_app_slug() always fell into its
    // `f"{slugify(task_text)}_{session_id[:8]}"` fallback -- a
    // different, throwaway namespace PER SEARCH, unrelated to this
    // project's real id. agents/academic_search.py's/web_researcher.py's
    // write_node() calls use exactly that namespace
    // (get_current_app_slug()), while ResearchTab.jsx's Sources/Citation
    // Graph tabs read back via GET /api/workspaces/{ws_id}/nodes, keyed
    // by the REAL workspace id. Two different strings that never
    // matched -- nodes got written for real, just into a bucket nothing
    // ever reads from. Passing the real id through as app_slug (a field
    // TaskRequest already accepts) unifies the write and read side.
    const sendTask = async (key, taskText, { mode = "auto", reviewBeforeDispatch = false, topicId = null, scope = null } = {}) => {
      const dockWorkspaceId = key && key.startsWith("ws:") ? key.slice(3) : null;
      const dockSessionId = states.get(key)?.sessionId;
      const userMessage = { role: "user", text: taskText };
      setState(key, (prev) => ({ messages: [...prev.messages, userMessage] }));
      persistMessageToSession(dockSessionId, userMessage);
      setState(key, { loading: true });
      resetLiveRunState(key);

      if (reviewBeforeDispatch) {
        try {
          const res = await fetch(`${API_URL}/api/task/preview`, {
            method: "POST",
            headers: await authHeaders({ json: true }),
            body: JSON.stringify({
              task_text: taskText, session_id: dockSessionId, mode,
              ...(dockWorkspaceId ? { app_slug: dockWorkspaceId } : {}),
            }),
          });
          const data = await res.json();
          if (data.status === "preview_ready") {
            if (isStaleRun(key, dockSessionId)) {
              console.warn(`[dock:${key}] preview for session ${dockSessionId} arrived after this dock switched chats — dropped.`);
              return;
            }
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
          finishRun(key, dockSessionId, taskText, data, { loading: false });
        } catch (err) {
          finishRun(key, dockSessionId, taskText, { status: "error", message: String(err) }, { loading: false });
        }
        return;
      }

      try {
        const res = await fetch(`${API_URL}/api/task`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
          body: JSON.stringify({
            task_text: taskText,
            session_id: dockSessionId,
            mode,
            ...(dockWorkspaceId ? { app_slug: dockWorkspaceId } : {}),
            ...(topicId ? { topic_id: topicId } : {}),
            ...(scope ? { scope } : {}),
          }),
        });
        const data = await res.json();
        if (data.status === "paused") {
          // CO3 patch 3: previously this only set in-memory pausedRun
          // state and returned — nothing was ever appended to
          // `messages` or persisted, so MessageBubble.jsx never saw a
          // status:"paused" message and a reload lost the pause
          // entirely. Now it's appended/persisted like every other
          // result, loading stays true (matching SessionContext.jsx's
          // sendTask()) since the run itself hasn't finished.
          if (!isStaleRun(key, dockSessionId)) {
            setState(key, { pausedRun: { taskText, sessionId: data.session_id || dockSessionId } });
          }
          finishRun(key, dockSessionId, taskText, data);
          return;
        }
        finishRun(key, dockSessionId, taskText, data, { loading: false });
      } catch (err) {
        finishRun(key, dockSessionId, taskText, { status: "error", message: String(err) }, { loading: false });
      }
    };

    const resumeRun = async (key, decision) => {
      const pausedRun = states.get(key)?.pausedRun;
      if (!pausedRun) return;
      const dockSessionId = pausedRun.sessionId;   // FIX — captured once, see sendTask's note above
      try {
        const res = await fetch(`${API_URL}/api/resume`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
          body: JSON.stringify({ session_id: pausedRun.sessionId, ...decision }),
        });
        const data = await res.json();
        if (!isStaleRun(key, dockSessionId)) setState(key, { pausedApproval: null });
        if (data.status === "paused") {
          // CO3 patch 3: same gap as sendTask() above — a resume that
          // immediately hits another approval role re-paused silently,
          // with nothing appended/persisted. Refresh pausedRun to the
          // new session_id (if any) and append/persist the same as a
          // first-time pause.
          if (!isStaleRun(key, dockSessionId)) {
            setState(key, { pausedRun: { taskText: pausedRun.taskText, sessionId: data.session_id || pausedRun.sessionId } });
          }
          finishRun(key, dockSessionId, pausedRun.taskText, data);
          return;
        }
        finishRun(key, dockSessionId, pausedRun.taskText, data, { loading: false, pausedRun: null });
      } catch (err) {
        finishRun(key, dockSessionId, pausedRun.taskText, { status: "error", message: String(err) },
          { loading: false, pausedRun: null, pausedApproval: null });
      }
    };

    const confirmHireReview = async (key, editedHires, { mode = "auto" } = {}) => {
      const pendingHireReview = states.get(key)?.pendingHireReview;
      if (!pendingHireReview) return;
      const { taskText, sessionId: reviewSessionId, decision } = pendingHireReview;
      const dockSessionId = reviewSessionId;   // FIX — captured once, see sendTask's note above
      // BUGFIX (bug audit, patch 5) — same app_slug thread-through as
      // sendTask above; this is the "review hires first" path for the
      // exact same dispatch, so it needs the real workspace id too.
      const dockWorkspaceId = key && key.startsWith("ws:") ? key.slice(3) : null;
      setState(key, { loading: true });
      resetLiveRunState(key);
      try {
        const res = await fetch(`${API_URL}/api/task/confirm`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
          body: JSON.stringify({
            task_text: taskText, decision, hires: editedHires, session_id: reviewSessionId, mode,
            ...(dockWorkspaceId ? { app_slug: dockWorkspaceId } : {}),
          }),
        });
        const data = await res.json();
        finishRun(key, dockSessionId, taskText, data);
      } catch (err) {
        finishRun(key, dockSessionId, taskText, { status: "error", message: String(err) });
      } finally {
        if (!isStaleRun(key, dockSessionId)) setState(key, { loading: false, pendingHireReview: null });
      }
    };

    const cancelHireReview = (key) => {
      setState(key, { pendingHireReview: null });
    };

    // Direct port of SessionContext.jsx's switchChat/createNewChat/
    // renameChat/deleteChat/linkChats (lines ~1382-1447 there), same API
    // calls, same behavior — retargeted from setSessionId/setMessages/etc.
    // to store.setState(key, ...), where `key` is resolved per-call from
    // the chatId involved (via callbacksRef.current.getWorkspaceIdForChat),
    // NOT fixed to one dock instance. That's the actual difference from
    // sendTask/resumeRun/etc. above: those operate on "the dock this
    // component is already showing" (key fixed by the calling component's
    // own workspaceId/chatId props). These operate on "whichever dock a
    // given chatId belongs to," because the caller (e.g. a chat list) is
    // choosing among chats that can belong to different workspaces — the
    // key isn't known until the chatId is. See useWorkspaceDockActions()
    // below, a second, key-agnostic hook for exactly this reason.
    const switchChat = async (chatId, { skipListReload = false } = {}) => {
      // Perf audit item #3: opening a chat now fetches only the most
      // recent page (limit=60) instead of the whole history — see
      // eo/chat_store.py's get_chat() docstring for why the JSONB blob
      // this used to ship was expensive in the first place. hasMoreOlder
      // (from the response's has_more) drives the "Load older messages"
      // affordance in WorkspaceChatPanel.jsx; loadOlderMessages below
      // fetches earlier pages on demand.
      const res = await fetch(`${API_URL}/api/chats/${chatId}?limit=60`, { headers: await authHeaders() });
      if (!res.ok) return null;
      const chat = await res.json();
      const { getWorkspaceIdForChat, refreshChatList } = callbacksRef.current;
      const key = normalizeDockKey(getWorkspaceIdForChat?.(chatId) ?? null, chatId);
      if (key) {
        setState(key, { sessionId: chatId, messages: chat.messages || [], hasMoreOlder: !!chat.has_more, loadingOlder: false });
        resetLiveRunState(key);
      }
      localStorage.setItem(ACTIVE_CHAT_KEY, chatId);
      setLastActiveChatId(chatId);
      if (!skipListReload) await refreshChatList?.();
      return chat;
    };

    // Perf audit item #3: the actual "load older messages on scroll-up"
    // fetch — pages backward from the earliest message currently held in
    // state, using that message's own `seq` (now returned by
    // get_chat()/append_message() — see chat_store.py) as before_seq.
    // Key-agnostic like switchChat above, for the same reason: the chat
    // list can trigger this for a chat that isn't necessarily the
    // caller's own dock instance.
    const loadOlderMessages = async (chatId) => {
      const { getWorkspaceIdForChat } = callbacksRef.current;
      const key = normalizeDockKey(getWorkspaceIdForChat?.(chatId) ?? null, chatId);
      if (!key) return;
      const current = states.get(key);
      if (!current || current.loadingOlder || !current.hasMoreOlder) return;
      const earliest = current.messages[0];
      if (!earliest || earliest.seq == null) return; // nothing loaded yet, or an
      // older message predating this patch with no seq recorded — either
      // way there's no safe before_seq to page from, so stop rather than
      // guess and risk skipping or repeating messages.
      setState(key, { loadingOlder: true });
      try {
        const res = await fetch(
          `${API_URL}/api/chats/${chatId}?limit=60&before_seq=${earliest.seq}`,
          { headers: await authHeaders() },
        );
        if (!res.ok) {
          setState(key, { loadingOlder: false });
          return;
        }
        const chat = await res.json();
        setState(key, (prev) => ({
          messages: [...(chat.messages || []), ...prev.messages],
          hasMoreOlder: !!chat.has_more,
          loadingOlder: false,
        }));
      } catch (err) {
        console.error("Failed to load older messages:", err);
        setState(key, { loadingOlder: false });
      }
    };

    const createNewChat = async () => {
      const res = await fetch(`${API_URL}/api/chats`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ title: "New Chat" }),
      });
      const chat = await res.json();
      const { getWorkspaceIdForChat, refreshChatList } = callbacksRef.current;
      // A brand-new chat has no workspace yet — getWorkspaceIdForChat
      // returns null, normalizeDockKey falls back to chat:${chat.id}.
      const key = normalizeDockKey(getWorkspaceIdForChat?.(chat.id) ?? null, chat.id);
      if (key) setState(key, { sessionId: chat.id, messages: [] });
      localStorage.setItem(ACTIVE_CHAT_KEY, chat.id);
      setLastActiveChatId(chat.id);
      await refreshChatList?.();
      return chat.id;
    };

    const renameChat = async (chatId, title) => {
      await fetch(`${API_URL}/api/chats/${chatId}/rename`, {
        method: "PATCH",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ title }),
      });
      await callbacksRef.current.refreshChatList?.();
    };

    const deleteChat = async (chatId) => {
      await fetch(`${API_URL}/api/chats/${chatId}`, { method: "DELETE", headers: await authHeaders() });
      const { getWorkspaceIdForChat, getChats, refreshChatList } = callbacksRef.current;
      const key = normalizeDockKey(getWorkspaceIdForChat?.(chatId) ?? null, chatId);
      const wasActive = key ? states.get(key)?.sessionId === chatId : false;
      // CHANGED — bug #1 fix: must happen BEFORE switchChat/createNewChat
      // below, not after — setState() shallow-merges into whatever's
      // already at `key`, so if this deleted chat's key gets reused
      // (e.g. a workspace-level key another chat in the same project
      // switches into), the old routing trace / dependency map / plan
      // would otherwise merge into the "new" dock state instead of being
      // replaced by it.
      if (key) remove(key);
      if (wasActive) {
        const remaining = (getChats?.() || []).filter((c) => c.id !== chatId);
        if (remaining.length > 0) await switchChat(remaining[0].id);
        else await createNewChat();
      } else {
        await refreshChatList?.();
      }
    };

    const linkChats = async (chatId, linkedChatIds) => {
      await fetch(`${API_URL}/api/chats/${chatId}/links`, {
        method: "PATCH",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ linked_chat_ids: linkedChatIds }),
      });
      await callbacksRef.current.refreshChatList?.();
    };

    // NEW — GrowthTab Step 3e follow-up (design option 1 from the
    // "investigated and explicitly deferred" note): dock-aware
    // equivalent of SessionContext.jsx's openScopedSubChat(wsId, taskText).
    // The legacy version writes to global setSessionId/setMessages, which
    // is exactly the desync the handoff doc flagged — once a tab's
    // embedded WorkspaceChatPanel is passed workspaceId (dock mode), a
    // quick-action still writing to the global slot would silently stop
    // showing up in that visible dock. This writes into the SAME
    // ws:${workspaceId} slot the dock reads from instead, keyed the
    // identical way useWorkspaceDock(workspaceId) resolves its key, so
    // there is exactly one place a given workspace's chat state lives —
    // no special-casing needed, same reasoning §2.4 gives for why partial
    // promotion falls out of the keying scheme for free.
    //
    // Fixed to a workspaceId (never a bare chatId) on purpose: unlike
    // switchChat/createNewChat, a scoped sub-chat always originates from
    // a specific tab's specific project, so there's no "which dock does
    // this belong to" ambiguity to resolve at call time the way those two
    // have.
    // `topicId`/`scope` (FIX — parity with SessionContext.jsx's
    // openScopedSubChat): previously accepted only (workspaceId,
    // taskText), so ResearchTab.jsx's Sources tab — which needs `scope`
    // to steer web_researcher at a specific domain preset (task 13e) —
    // couldn't use this dock-native version and fell back to the
    // legacy, non-dock openScopedSubChat instead. That legacy path
    // writes into SessionContext's own global sessionId/messages/
    // liveSteps state, which WorkingPanel.jsx never reads (it's dock-
    // only) — so a Sources search dispatched that way ran for real on
    // the backend but never showed up as live progress here. Both
    // params are optional and default to null, so every existing
    // caller (which passes neither) is unaffected.
    const openScopedSubChat = async (workspaceId, taskText, topicId = null, scope = null) => {
      const key = normalizeDockKey(workspaceId, null);
      const res = await fetch(`${API_URL}/api/workspaces/${workspaceId}/chats/create`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ title: "New Chat" }),
      });
      if (!res.ok) throw new Error("Failed to create workspace chat");
      const chat = await res.json();
      setState(key, { sessionId: chat.id, messages: [] });
      resetLiveRunState(key);
      const { fetchWorkspaces, refreshChatList } = callbacksRef.current;
      await fetchWorkspaces?.(); // membership changed server-side — same as SessionContext.jsx's createWorkspaceChat
      await refreshChatList?.();
      await sendTask(key, taskText, { topicId, scope });
      return chat.id;
    };

    // NEW — issue #3 (per-project chat management): plain "create an
    // empty chat nested in this project" version of openScopedSubChat
    // above. Same one-step create-and-attach endpoint
    // (POST /api/workspaces/{id}/chats/create), but — unlike
    // openScopedSubChat — it does NOT call sendTask afterwards, since
    // this is the "+" button beside a project name in a sidebar, not a
    // quick-action that should immediately run something. Mirrors the
    // "+" in ChatSidebar's own createNewChat, just workspace-scoped.
    const createWorkspaceChat = async (workspaceId) => {
      const key = normalizeDockKey(workspaceId, null);
      const res = await fetch(`${API_URL}/api/workspaces/${workspaceId}/chats/create`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ title: "New Chat" }),
      });
      if (!res.ok) throw new Error("Failed to create workspace chat");
      const chat = await res.json();
      setState(key, { sessionId: chat.id, messages: [] });
      resetLiveRunState(key);
      const { fetchWorkspaces, refreshChatList } = callbacksRef.current;
      await fetchWorkspaces?.(); // membership changed server-side — same as openScopedSubChat
      await refreshChatList?.();
      localStorage.setItem(ACTIVE_CHAT_KEY, chat.id);
      setLastActiveChatId(chat.id);
      return chat.id;
    };

    // CO3: on-demand pause for whatever's running right now, as opposed
    // to resumeRun above (which continues a run already paused). Fires
    // the sessionId this dock is actually tracking — same `dockSessionId`
    // sendTask/resumeRun above key off of, so a pause request always
    // targets the run this panel is showing, not a stale id. Genuinely
    // fire-and-forget: the pause_requested:{session_id} flag it sets is
    // picked up by eo/executor.py's loop on its own; there's nothing to
    // await here beyond the request landing. The UI finds out the pause
    // actually took effect the same way it already finds out about an
    // approval_roles pause — the "awaiting_approval" live event flips
    // the paused step's status (see handleDockEvent above).
    const requestPause = async (key) => {
      const dockSessionId = states.get(key)?.sessionId;
      if (!dockSessionId) return;
      try {
        await fetch(`${API_URL}/api/task/${dockSessionId}/pause`, {
          method: "POST",
          headers: await authHeaders({ json: true }),
        });
      } catch (err) {
        // Fire-and-forget by design (matches the backend endpoint's own
        // contract, see tasks.py's request_pause docstring) — a failed
        // pause request just means the run keeps going, which is a safe
        // failure mode, so this only needs to not crash the UI.
        console.error("requestPause failed:", err);
      }
    };

    storeRef.current = {
      getState, subscribe, setState, remove, removeWorkspace,   // CHANGED — bug #1 fix, added removeWorkspace
      persistMessage, sendTask, resumeRun, requestPause, confirmHireReview, cancelHireReview,
      switchChat, createNewChat, renameChat, deleteChat, linkChats, openScopedSubChat, createWorkspaceChat, loadOlderMessages,
      getLastActiveChatId, setLastActiveChatId, subscribeLastActiveChatId,
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
 * Returns { key, state, setDockState, sendTask, resumeRun,
 * confirmHireReview, cancelHireReview } for the dock slot keyed by
 * workspaceId (or chatId when there's no workspace). Used by
 * WorkspaceChatPanel.jsx (3d, dual-mode) when a caller passes it a
 * workspaceId/chatId — every current call site still passes neither, so
 * this resolves to a null key and inert fields for them, same as before.
 * See useWorkspaceDockActions() below for switchChat/createNewChat/etc,
 * which aren't tied to one fixed key.
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
    () => (key ? store.getState(key) : EMPTY_DOCK_STATE),   // FIXED — was makeInitialDockState(), a fresh object every call
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
  const requestPause = useCallback(
    () => (key ? store.requestPause(key) : Promise.resolve()),
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

  // NEW — GrowthTab Step 3e follow-up. Unlike sendTask/resumeRun/etc.
  // above (which operate on whatever key this hook already resolved,
  // from workspaceId OR chatId), this is fixed to workspaceId
  // specifically — a scoped sub-chat always originates from a workspace,
  // never a bare chatId — so it rejects rather than silently resolving
  // to a chat:${chatId} slot if only a chatId was passed to this hook.
  // FIX — topicId/scope now accepted and forwarded (see store-level
  // openScopedSubChat's own comment); every existing caller passes
  // neither and behaves exactly as before.
  const openScopedSubChat = useCallback(
    (taskText, topicId = null, scope = null) =>
      workspaceId
        ? store.openScopedSubChat(workspaceId, taskText, topicId, scope)
        : Promise.reject(new Error("openScopedSubChat requires a workspaceId")),
    [store, workspaceId]
  );

  return { key, state, setDockState, sendTask, resumeRun, requestPause, confirmHireReview, cancelHireReview, openScopedSubChat };
}

/**
 * useWorkspaceDockActions()
 *
 * Step 3e prereq. Separate from useWorkspaceDock(workspaceId) on purpose:
 * switchChat/createNewChat/renameChat/deleteChat/linkChats don't operate
 * on one fixed dock — a chat list (e.g. ChatSidebar) picks among chats
 * that can belong to different workspaces, so the key is resolved from
 * whichever chatId is passed to the call, not from this hook's own props
 * (it takes none). Any component that needs these — without needing a
 * specific dock's live state/sendTask — uses this instead of
 * useWorkspaceDock(someFixedId).
 */
export function useWorkspaceDockActions() {
  const store = useContext(WorkspaceDockStoreContext);
  if (!store) {
    throw new Error("useWorkspaceDockActions must be used within a WorkspaceDockProvider");
  }
  const { switchChat, createNewChat, renameChat, deleteChat, linkChats, createWorkspaceChat, removeWorkspace, loadOlderMessages } = store;   // CHANGED — bug #1 fix, added removeWorkspace; loadOlderMessages NEW — perf audit item #3
  return { switchChat, createNewChat, renameChat, deleteChat, linkChats, createWorkspaceChat, removeWorkspace, loadOlderMessages };
}

/**
 * useLastActiveChatId()
 *
 * Step 3e. Returns the id of whichever chat was most recently the target
 * of switchChat/createNewChat, anywhere in the app — see the store-level
 * comment next to `lastActiveChatId` for why this exists as its own
 * single value instead of reading any one dock's sessionId. Used by
 * ChatSidebar to decide which row looks selected, now that "the" active
 * chat isn't a single per-dock concept once several docks can be showing
 * different chats at once.
 */
export function useLastActiveChatId() {
  const store = useContext(WorkspaceDockStoreContext);
  if (!store) {
    throw new Error("useLastActiveChatId must be used within a WorkspaceDockProvider");
  }
  return useSyncExternalStore(
    store.subscribeLastActiveChatId,
    store.getLastActiveChatId,
    store.getLastActiveChatId
  );
}
