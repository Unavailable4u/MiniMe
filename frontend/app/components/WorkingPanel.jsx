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
    dependencyMap,
    structurePlan,
  } = useSession();

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
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="h-full overflow-y-auto p-3 space-y-6"
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
          {m.routeTrace?.length > 1 && (
            <RoutingTraceGraph
              trace={m.routeTrace}
              suggestedAgents={m.data?.decision?.suggested_agents}
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
              {routeTrace.length > 1 && (
                <RoutingTraceGraph
                  trace={routeTrace}
                  suggestedAgents={liveDecision?.suggested_agents}
                />
              )}
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
  );
}
