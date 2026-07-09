"use client";
import { useRef, useEffect } from "react";
import { useSession } from "../context/SessionContext";
import RoutingTraceCard from "./RoutingTraceCard";
import AgentStepList from "./AgentStepList";
import RoutingTraceGraph from "./RoutingTraceGraph";
import DependencyGraph from "./DependencyGraph";
import MermaidDiagram from "./MermaidDiagram";

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
export default function WorkingPanel({ isSyncingRef }) {
  const {
    messages,
    activeMessageIndex,
    setActiveMessageIndex,
    loading,
    liveDecision,
    liveSteps,
    routeTrace,
    roleRequests,
    dependencyMap,
    structurePlan,
    sessionId,   // NEW — §4
    batches,     // NEW — §4
  } = useSession();

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
        <p className="text-neutral-600 text-xs p-4">
          Routing and structure info will appear here once a task runs.
        </p>
      )}

      {snapshotMessages.map((m) => (
        <div
          key={m.index}
          ref={(el) => (sectionRefs.current[m.index] = el)}
          className="space-y-2 border-b border-neutral-800 pb-4"
        >
          <p className="text-xs text-neutral-500 truncate">{m.task}</p>
          {m.data?.decision && <RoutingTraceCard decision={m.data.decision} />}
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
          <p className="text-xs text-neutral-500">Running…</p>
          {!liveDecision ? (
            <div className="text-neutral-500 text-sm animate-pulse">
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
              <AgentStepList steps={liveSteps} />
            </>
          )}
        </div>
      )}
      </div>
    </div>
  );
}
