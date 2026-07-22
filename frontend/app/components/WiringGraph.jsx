"use client";
import { useMemo, useState } from "react";
import ForceGraphBase from "./ForceGraphBase";

// One color per wiring.nodes[].type. Same flat-palette approach as
// KnowledgeGraphView.jsx's SECTION_COLORS -- device-spec categories are a
// small fixed set (Blueprint §0's schema), so no lookup table shared with
// KnowledgeGraphView's own domain vocabulary is needed.
const TYPE_COLORS = {
  mcu: "#22d3ee",
  sensor: "#60a5fa",
  actuator: "#fb923c",
  power: "#fbbf24",
  module: "#c084fc",
};
const DEFAULT_COLOR = "#6b7280";

// wiring.edges[].kind -> edge color.
const EDGE_COLORS = {
  data: "#22c55e",
  power: "#f59e0b",
  ground: "#6b7280",
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/**
 * WiringGraph — third caller of ForceGraphBase (after RoutingTraceGraph.jsx
 * and KnowledgeGraphView.jsx). Renders Blueprint's wiring sub-view:
 * device_spec.wiring's {nodes, edges} straight from the spec produced by
 * agents/hardware_speccer.py -- ForceGraphBase itself has no idea which of
 * the three it's drawing.
 *
 * `wiring`: the wiring slice of the device spec -- {nodes: [{id, label,
 * type}], edges: [{from, to, kind}]} (Blueprint design guide §0). Nodes are
 * rebuilt from device_spec on every render via useMemo, same as
 * KnowledgeGraphView.jsx -- this view is a static per-fetch snapshot (spec
 * only changes on regeneration or a price refresh, not something that
 * streams live node-by-node), so there's no need for RoutingTraceGraph.jsx's
 * persistent-object-per-id pattern that live/incremental graphs require to
 * keep physics state across re-renders.
 */
export default function WiringGraph({ wiring }) {
  const [hoveredNode, setHoveredNode] = useState(null);

  const graphData = useMemo(() => {
    const nodes = (wiring?.nodes || []).map((n) => ({
      id: n.id,
      label: n.label || n.id,
      type: n.type,
    }));

    const links = (wiring?.edges || [])
      .filter((e) => e.from !== e.to)
      .map((e) => ({
        source: e.from,
        target: e.to,
        kind: e.kind,
      }));

    return { nodes, links };
  }, [wiring]);

  const legend = useMemo(() => {
    const seenTypes = new Set((wiring?.nodes || []).map((n) => n.type).filter(Boolean));
    return Array.from(seenTypes).map((type) => (
      <span key={type} className="flex items-center gap-1">
        <span
          className="inline-block w-2 h-2 rounded-full"
          style={{ backgroundColor: TYPE_COLORS[type] || DEFAULT_COLOR }}
        />
        {type}
      </span>
    ));
  }, [wiring]);

  return (
    <ForceGraphBase
      nodes={graphData.nodes}
      links={graphData.links}
      height={480}
      linkColor={(link) => EDGE_COLORS[link.kind] || DEFAULT_COLOR}
      linkWidth={1.5}
      linkLabel={(link) => link.kind}
      onNodeHover={setHoveredNode}
      nodeLabel={(node) => {
        return `<div style="background:#171717;border:1px solid #404040;border-radius:6px;padding:6px 8px;font-size:11px;color:#e5e5e5;max-width:260px;white-space:normal;word-break:break-word">
          <div style="font-weight:600">${escapeHtml(node.label)}</div>
          <div style="opacity:.7">${escapeHtml(node.type || "")}</div>
        </div>`;
      }}
      nodeCanvasObject={(node, ctx, globalScale) => {
        const color = TYPE_COLORS[node.type] || DEFAULT_COLOR;
        const r = 6;

        ctx.save();
        ctx.beginPath();
        ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = node === hoveredNode ? 2.5 : 1;
        ctx.strokeStyle = "#e5e5e5";
        ctx.stroke();
        ctx.restore();

        const fontSize = 10 / globalScale;
        ctx.font = `${fontSize}px sans-serif`;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillStyle = "#ddd";
        ctx.fillText(node.label, node.x + r + 3, node.y);
      }}
      nodePointerAreaPaint={(node, color, ctx) => {
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(node.x, node.y, 9, 0, 2 * Math.PI);
        ctx.fill();
      }}
      legend={legend}
    />
  );
}
