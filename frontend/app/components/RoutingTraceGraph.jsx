"use client";
import ReactFlow, { Background, Controls } from "reactflow";
import "reactflow/dist/style.css";

const REASON_COLORS = {
  plan: "#6b7280",
  "error-priority": "#ef4444",
  recheck: "#f59e0b",
  skip: "#8b5cf6",
  escalate: "#ec4899",
};

export default function RoutingTraceGraph({ trace }) {
  const nodes = trace.map((step, i) => ({
    id: String(i),
    position: { x: i * 160, y: (i % 2) * 80 },
    data: { label: step.destination },
    style: { borderColor: REASON_COLORS[step.reason] || "#6b7280" },
  }));
  const edges = trace.slice(1).map((step, i) => ({
    id: `e${i}`,
    source: String(i),
    target: String(i + 1),
    label: step.reason,
    style: { stroke: REASON_COLORS[step.reason] || "#6b7280" },
  }));
  return (
    <div style={{ height: 220 }}>
      <ReactFlow nodes={nodes} edges={edges} fitView>
        <Background /><Controls />
      </ReactFlow>
    </div>
  );
}
