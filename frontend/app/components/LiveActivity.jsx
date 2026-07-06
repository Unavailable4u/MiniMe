"use client";
import RoutingTraceCard from "./RoutingTraceCard";
import RoutingTraceGraph from "./RoutingTraceGraph";
import DependencyGraph from "./DependencyGraph";
import StructurePlanDiagram from "./StructurePlanDiagram";
import AgentStepList from "./AgentStepList";

export default function LiveActivity({ decision, steps, routeTrace, dependencyMap, structurePlan }) {
  if (!decision) {
    return (
      <div className="text-neutral-500 text-sm animate-pulse">
        Classifying and routing...
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <RoutingTraceCard decision={decision} />
      {routeTrace.length > 1 && (
        <RoutingTraceGraph trace={routeTrace} suggestedAgents={decision?.suggested_agents} />
      )}
      {Object.keys(dependencyMap).length > 0 && <DependencyGraph map={dependencyMap} />}
      {structurePlan && <StructurePlanDiagram mermaidText={structurePlan} />}
      <AgentStepList steps={steps} />
    </div>
  );
}
