"use client";
import ReactFlow, { Background, Controls } from "reactflow";
import "reactflow/dist/style.css";

// `map` shape (from agents/dependency_mapper.py's actual output):
// { module_name: { depends_on: ["other_module", ...], notes: "..." } }
export default function DependencyGraph({ map }) {
  const moduleNames = Object.keys(map);

  const nodes = moduleNames.map((name, i) => ({
    id: name,
    position: { x: (i % 4) * 180, y: Math.floor(i / 4) * 120 },
    data: { label: name },
  }));

  const edges = moduleNames.flatMap((name) =>
    (map[name]?.depends_on || []).map((dep, i) => ({
      id: `${name}-${dep}-${i}`,
      source: name,
      target: dep,
    }))
  );

  return (
    <div style={{ height: 260 }}>
      <ReactFlow nodes={nodes} edges={edges} fitView>
        <Background /><Controls />
      </ReactFlow>
    </div>
  );
}