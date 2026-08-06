"use client";
import { useRef, useEffect, useState } from "react";
import { Pause, Loader2 } from "lucide-react";
import { useSession } from "../context/SessionContext";
import { useWorkspaceDock } from "../context/WorkspaceDockContext";
import RoutingTraceCard from "./RoutingTraceCard";
import AgentStepList from "./AgentStepList";
import RoutingTraceGraph from "./RoutingTraceGraph";
import DependencyGraph from "./DependencyGraph";
import MermaidDiagram from "./MermaidDiagram";
import SaveRunAsTemplate from "./SaveRunAsTemplate";

// One section per assistant message that carries a snapshot (steps /
// routeTrace / dependencyMap / structurePlan — all attached by
// SessionContext.jsx's sendTask(), success and error paths alike),
// plus a trailing "live" section for whatever run is currently in
// flight (no message snapshot exists for that one yet).
//
// `isSyncingRef` is the same lock ChatTab.jsx uses on its own scroll
// handler — set true right before a programmatic sync-scroll here so
// this panel's own onScroll doesn't immediately fire, recompute a
// (possibly different) closest index, and bounce activeMessageIndex
// back, which would fight ChatTab's handler forever.
//
// `workspaceId`/`chatId` — required in practice. WorkspaceChatPanel.jsx
// (Item 2 step 2) never mounts this component unless
// `useWorkspaceDock(workspaceId, chatId)` already resolved a real key —
// it early-returns "Select a project to chat" / "Loading your chat…"
// otherwise — so this component is dock-only; the old `usingDock ? dock
// : legacy` ternaries were dead code and were removed (Item 2 step 3).
// `batches` and `API_URL` still come off `useSession()` — they're
// app-wide (§2.4 "mother" state), not per-dock, and have no dock
// equivalent.
// `onNavigateSubTab` — NEW, guide §5. Optional; only NotebooksTab.jsx
// passes it (through WorkspaceChatPanel.jsx), same as
// NotebooksGeneratePicker.jsx's own prop of the same name/shape
// (subTab key -> void). Every other caller of WorkingPanel leaves it
// undefined, which is a safe no-op below — a branch node click just
// does nothing for a dock that isn't inside Notebooks.
// CO3: small local component rather than inlining the busy-state
// handling into WorkingPanel's render — mirrors ArtifactRenderer.jsx's
// PythonArtifact "Run" button pattern (local status state, spinner while
// in flight, re-enables after). Once clicked, stays disabled/"Pausing…"
// until the live "awaiting_approval" event actually arrives and
// liveSteps flips (handled by the parent's conditional render, not this
// component) — a pause is fire-and-forget server-side, so this button
// can't itself know the moment the run really stopped, only that it
// asked.
function PauseButton({ onRequestPause }) {
  const [pending, setPending] = useState(false);

  async function handleClick() {
    setPending(true);
    try {
      await onRequestPause();
    } finally {
      // Left true briefly even after the request resolves — the actual
      // pause hasn't landed yet at that point, only the request has.
      // The button disappears on its own once liveSteps shows
      // awaiting_approval (parent's gate above), so there's no risk of
      // this staying stuck in a "Pausing…" state forever if that never
      // arrives except the genuinely-rare case the run finishes on its
      // own first, which is also a fine outcome to just let happen.
      setPending(false);
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={pending}
      title="Pause after the current step finishes"
      className="flex items-center gap-1 text-[10px] px-2 py-1 rounded border border-[var(--neutral-800)] text-[var(--neutral-400)] hover:bg-white/5 disabled:opacity-60 transition-colors"
    >
      {pending ? <Loader2 size={11} className="animate-spin" /> : <Pause size={11} />}
      {pending ? "Pausing…" : "Pause"}
    </button>
  );
}

export default function WorkingPanel({ isSyncingRef, workspaceId = null, chatId = null, onNavigateSubTab = null }) {
  const { batches, API_URL } = useSession(); // §4 / Part 2 §2.7: app-wide, no dock equivalent
  const dock = useWorkspaceDock(workspaceId, chatId);

  const messages = dock.state.messages;
  const activeMessageIndex = dock.state.activeMessageIndex;
  const setActiveMessageIndex = (i) => dock.setDockState({ activeMessageIndex: i });
  const loading = dock.state.loading;
  const liveDecision = dock.state.liveDecision;
  const liveSteps = dock.state.liveSteps;
  const routeTrace = dock.state.routeTrace;
  const roleRequests = dock.state.roleRequests;
  const dependencyMap = dock.state.dependencyMap;
  const structurePlan = dock.state.structurePlan;
  const sessionId = dock.state.sessionId; // §4
  const resumeRun = dock.resumeRun; // Part 2 §2.4/§2.7
  const requestPause = dock.requestPause; // NEW — CO3
  const pausedRun = dock.state.pausedRun; // NEW — CO3 patch 4
  // guide §5 — a Notebooks Generate command can only originate from a
  // workspace-keyed dock (NotebooksGeneratePicker.jsx always has a
  // workspaceId), so this is simply undefined for a WorkingPanel not
  // embedded in a workspace tab.
  const notebooksGenerateRun = dock.state.notebooksGenerateRun;

  // NEW — §4: answers "which chats is *this* chat currently pulling
  // context from" right where the user is already looking, without
  // opening the (§5) manage-batch modal. A chat is in at most one batch
  // (see eo/memory_batch.py's "one batch at a time" note), so `.find`
  // is safe here.
  const activeBatch = batches.find((b) => b.member_chat_ids.includes(sessionId)) || null;

  const sectionRefs = useRef([]);
  const containerRef = useRef(null);

  // Scroll to the active index when it changed from the OTHER panel
  // (i.e. from ChatTab's handler, via setActiveMessageIndex).
  useEffect(() => {
    if (activeMessageIndex == null) return;
    const el = sectionRefs.current[activeMessageIndex];
    if (!el || !containerRef.current) return;
    isSyncingRef.current = true;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    // Release the lock after the smooth scroll has had time to settle,
    // rather than on scroll-end (no reliable cross-browser event for
    // that on a plain div), so this panel's own onScroll below stays
    // suppressed for the duration of the programmatic scroll.
    const t = setTimeout(() => {
      isSyncingRef.current = false;
    }, 500);
    return () => clearTimeout(t);
  }, [activeMessageIndex, isSyncingRef]);

  // This panel's own scroll -> figure out which section is closest to
  // the top and publish it as activeMessageIndex, same "closest
  // distance from container top" approach as ChatTab.handleChatScroll.
  function handleScroll() {
    if (isSyncingRef.current) return;
    const containerTop = containerRef.current?.getBoundingClientRect().top ?? 0;
    let closestIndex = null;
    let closestDist = Infinity;
    sectionRefs.current.forEach((el, i) => {
      if (!el) return;
      const dist = Math.abs(el.getBoundingClientRect().top - containerTop);
      if (dist < closestDist) {
        closestDist = dist;
        closestIndex = i;
      }
    });
    if (closestIndex != null) setActiveMessageIndex(closestIndex);
  }

  const snapshotMessages = messages
    .map((m, i) => ({ ...m, index: i }))
    .filter(
      (m) =>
        m.role === "assistant" &&
        (m.steps?.length > 0 ||
          m.routeTrace?.length > 0 ||
          (m.dependencyMap && Object.keys(m.dependencyMap).length > 0) ||
          m.structurePlan)
    );

  return (
    <div className="h-full flex flex-col">
      {/* NEW — §4B: small strip above the panel, only when the active
          chat is currently a batch member. */}
      {activeBatch && (
        <div
          className="text-[10px] px-3 py-1.5 border-b shrink-0"
          style={{ borderColor: "var(--cyber-border)", color: "var(--cyber-dim)" }}
        >
          Sharing memory with {activeBatch.member_chat_ids.length - 1} other chat(s) in &quot;{activeBatch.name}&quot;
        </div>
      )}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto p-3 space-y-6"
      >
      {snapshotMessages.length === 0 && !loading && !notebooksGenerateRun && (
        <p className="text-[var(--neutral-600)] text-xs p-4">
          Routing and structure info will appear here once a task runs.
        </p>
      )}

      {/* NEW — guide §5: a Notebooks "Generate" command isn't a chat
          turn, so it has no snapshotMessages entry to hang a section
          off — this reads NotebooksGeneratePicker.jsx's mirrored run
          state straight off the dock instead (same key, see
          WorkspaceDockContext.jsx's notebooksGenerateRun note) and
          renders it as its own always-current section, above the chat
          history, since a Generate run isn't tied to any one message's
          position in that history the way a chat task's routing trace
          is. */}
      {notebooksGenerateRun && (
        <div className="space-y-2 border-b border-[var(--neutral-800)] pb-4">
          <p className="text-xs text-[var(--neutral-500)]">Generate</p>
          <RoutingTraceGraph
            branches={notebooksGenerateRun.branches}
            onBranchClick={(panelKey) => {
              const subTab = notebooksGenerateRun.branches.find((b) => b.panel_key === panelKey)?.subTab;
              if (subTab) onNavigateSubTab?.(subTab);
            }}
          />
        </div>
      )}

      {snapshotMessages.map((m) => (
        <div
          key={m.index}
          ref={(el) => (sectionRefs.current[m.index] = el)}
          className="space-y-2 border-b border-[var(--neutral-800)] pb-4"
        >
          <p className="text-xs text-[var(--neutral-500)] truncate">{m.task}</p>
          {m.data?.decision && <RoutingTraceCard decision={m.data.decision} />}
          {/* Part 2 §2.3/§2.7 — "save from a finished run" write path.
              execution_order is the Panel/Inspector's own already-decided
              role order for this run, identical in shape to a workflow
              template's `roles` — only shown once a run actually has one
              (tier 0/1/2 runs, or a run with a single/empty pipeline,
              have nothing meaningful to save here). */}
          {m.data?.decision?.execution_order?.length > 0 && (
            <SaveRunAsTemplate
              apiUrl={API_URL}
              roles={m.data.decision.execution_order}
              domainHint={m.data.decision.domain}
            />
          )}
          {/* NEW — CO4 patch 2: only ever populated on a finished
              snapshot (m.data.result.dedup_notes) — the live `steps`
              call further down never passes this, since the organizer
              synthesis pass that produces it only runs once a whole
              run has finished (see task_runner.py's own comment at
              that call site). */}
          {m.steps?.length > 0 && (
            <AgentStepList steps={m.steps} dedupNotes={m.data?.result?.dedup_notes} />
          )}
          {/*
            FIX: this used to require `m.routeTrace?.length > 1` before
            the graph would even mount -- a dispatch_event only fires
            AFTER the first role finishes and the dispatcher computes the
            next hop, so for most of a run's early life (SGA, Inspector,
            role-brief writing, the first role itself) routeTrace has 0
            or 1 entries and the graph never appeared until the run was
            nearly over. RoutingTraceGraph now builds its own backbone
            from `steps` (every real agent_start/agent_done, in order),
            so it no longer needs routeTrace to have anything in it at
            all -- render it whenever there's ANY real activity to show.
          */}
          {(m.steps?.length > 0 || m.routeTrace?.length > 0) && (
            <RoutingTraceGraph
              trace={m.routeTrace}
              suggestedAgents={m.data?.decision?.suggested_agents}
              steps={m.steps}
              roleRequests={m.roleRequests}
              runStatus={m.data?.status === "error" ? "error" : "done"}
            />
          )}
          {m.dependencyMap && Object.keys(m.dependencyMap).length > 0 && (
            <DependencyGraph map={m.dependencyMap} />
          )}
          {m.structurePlan && <MermaidDiagram mermaidText={m.structurePlan} />}
        </div>
      ))}

      {/* Live section for the in-progress run — same shape LiveActivity.jsx
          used to render standalone; absorbed here per Part 21 Step 5. */}
      {loading && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <p className="text-xs text-[var(--neutral-500)]">Running…</p>
            {/* CO3: always available while a run is live — not gated on
                liveDecision existing, since a pause request just sets a
                flag eo/executor.py's loop picks up at its own next
                checkpoint (after whichever role finishes next), so it's
                fine to request one even during the pre-decision
                "Classifying and routing…" moment. Hidden once a step is
                already awaiting_approval — pausing an already-paused
                run has nothing left to do.
                CHANGED — CO3 patch 4: also hidden once pausedRun is set.
                Previously only the awaiting_approval case was checked,
                so a manual pause (which never sets any step's status —
                see AgentStepList's own comment on `manualPause`) left
                this button visible and clickable on an already-paused
                run. */}
            {!liveSteps.some((s) => s.status === "awaiting_approval") && !pausedRun && (
              <PauseButton onRequestPause={requestPause} />
            )}
          </div>
          {!liveDecision ? (
            <div className="text-[var(--neutral-500)] text-sm animate-pulse">
              Classifying and routing...
            </div>
          ) : (
            <>
              <RoutingTraceCard decision={liveDecision} />
              {/*
                FIX: same gate removed as above, PLUS this now renders as
                soon as liveDecision exists (classification just landed) —
                RoutingTraceGraph draws the planned pipeline as a
                placeholder chain immediately from suggestedAgents, then
                fills it in live as liveSteps arrives, instead of waiting
                for two dispatch_events to accumulate first.
              */}
              <RoutingTraceGraph
                trace={routeTrace}
                suggestedAgents={liveDecision?.suggested_agents}
                steps={liveSteps}
                roleRequests={roleRequests}
                runStatus="running"
              />
              {Object.keys(dependencyMap).length > 0 && (
                <DependencyGraph map={dependencyMap} />
              )}
              {structurePlan && <MermaidDiagram mermaidText={structurePlan} />}
              <AgentStepList
                steps={liveSteps}
                onResume={resumeRun}
                manualPause={!liveSteps.some((s) => s.status === "awaiting_approval") && !!pausedRun}
              />
            </>
          )}
        </div>
      )}
      </div>
    </div>
  );
}