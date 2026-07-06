"use client";
import { useSession } from "../../context/SessionContext";
import RoutingTraceGraph from "../RoutingTraceGraph";
import DependencyGraph from "../DependencyGraph";
import StructurePlanDiagram from "../StructurePlanDiagram";
import RoutingTraceCard from "../RoutingTraceCard";

export default function RoutingStructureTab() {
  const { liveDecision, routeTrace, dependencyMap, structurePlan } = useSession();
  const hasAnything =
    liveDecision || routeTrace.length > 1 || Object.keys(dependencyMap).length > 0 || structurePlan;

  return (
    <div className="h-full overflow-y-auto px-4 py-6 max-w-4xl mx-auto space-y-4">
      {!hasAnything && (
        <p className="text-neutral-500 text-sm">
          Nothing routed yet this session — send a task from the Chat tab.
        </p>
      )}
      {liveDecision && <RoutingTraceCard decision={liveDecision} />}
      {routeTrace.length > 1 && (
        <RoutingTraceGraph trace={routeTrace} suggestedAgents={liveDecision?.suggested_agents} />
      )}
      {Object.keys(dependencyMap).length > 0 && <DependencyGraph map={dependencyMap} />}
      {structurePlan && <StructurePlanDiagram mermaidText={structurePlan} />}
    </div>
  );
}
