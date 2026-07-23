"use client";
import { useRef, useEffect } from "react";
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
// `workspaceId`/`chatId` — NEW, OPTIONAL. Same dual-mode switch
// WorkspaceChatPanel.jsx got in 3d: `useWorkspaceDock(workspaceId, chatId)`
// is called unconditionally and resolves to a null key when neither prop
// is passed, so an unwired caller (there are none left once
// WorkspaceChatPanel.jsx passes its own workspaceId/chatId straight
// through below) behaves byte-for-byte as before. `batches` and
// `API_URL` stay off `useSession()` unconditionally either way — they're
// app-wide (§2.4 "mother" state), not per-dock.
export default function WorkingPanel({ isSyncingRef, workspaceId = null, chatId = null }) {
  const legacy = useSession();
  const dock = useWorkspaceDock(workspaceId, chatId);
  const usingDock = dock.key != null;

  const { batches, API_URL } = legacy; // NEW — §4 / Part 2 §2.7: app-wide in both modes

  const messages = usingDock ? dock.state.messages : legacy.messages;
  const activeMessageIndex = usingDock ? dock.state.activeMessageIndex : legacy.activeMessageIndex;
  const setActiveMessageIndex = usingDock
    ? (i) => dock.setDockState({ activeMessageIndex: i })
    : legacy.setActiveMessageIndex;
  const loading = usingDock ? dock.state.loading : legacy.loading;
  const liveDecision = usingDock ? dock.state.liveDecision : legacy.liveDecision;
  const liveSteps = usingDock ? dock.state.liveSteps : legacy.liveSteps;
  const routeTrace = usingDock ? dock.state.routeTrace : legacy.routeTrace;
  const roleRequests = usingDock ? dock.state.roleRequests : legacy.roleRequests;
  const dependencyMap = usingDock ? dock.state.dependencyMap : legacy.dependencyMap;
  const structurePlan = usingDock ? dock.state.structurePlan : legacy.structurePlan;
  const sessionId = usingDock ? dock.state.sessionId : legacy.sessionId; // NEW — §4
  const resumeRun = usingDock ? dock.resumeRun : legacy.resumeRun; // NEW — Part 2 §2.4/§2.7

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
      {snapshotMessages.length === 0 && !loading && (
        <p className="text-[var(--neutral-600)] text-xs p-4">
          Routing and structure info will appear here once a task runs.
        </p>
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
          {m.steps?.length > 0 && <AgentStepList steps={m.steps} />}
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
          <p className="text-xs text-[var(--neutral-500)]">Running…</p>
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
              <AgentStepList steps={liveSteps} onResume={resumeRun} />
            </>
          )}
        </div>
      )}
      </div>
    </div>
  );
}