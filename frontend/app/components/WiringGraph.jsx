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

// Rendering fix (Bug 8 / detailed-wiring follow-up): a "which two parts
// are connected" arrow isn't enough to actually wire something from --
// see agents/hardware_speccer.py's SYSTEM_PROMPT for the schema change
// this pairs with (wiring.edges now carries "from_pin"/"to_pin" when the
// model can resolve them). Falls back gracefully for an edge that only
// has one side resolved, or a spec generated before this schema change
// (both fields simply absent) -- callers below already skip drawing
// anything when this returns "".
function pinLabel(link) {
  if (link.fromPin && link.toPin) return `${link.fromPin} → ${link.toPin}`;
  if (link.fromPin) return `${link.fromPin} →`;
  if (link.toPin) return `→ ${link.toPin}`;
  return "";
}

/**
 * WiringGraph — third caller of ForceGraphBase (after RoutingTraceGraph.jsx
 * and KnowledgeGraphView.jsx). Renders Blueprint's wiring sub-view:
 * device_spec.wiring's {nodes, edges} straight from the spec produced by
 * agents/hardware_speccer.py -- ForceGraphBase itself has no idea which of
 * the three it's drawing.
 *
 * `wiring`: the wiring slice of the device spec -- {nodes: [{id, label,
 * type}], edges: [{from, to, kind, from_pin, to_pin}]} (Blueprint design
 * guide §0; from_pin/to_pin added alongside hardware_speccer.py's schema
 * change -- both optional, null when the model couldn't resolve a named
 * pin). Nodes are
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
        // Rendering fix (Bug 8 / detailed-wiring follow-up): carry the
        // pin/terminal names hardware_speccer's schema now proposes
        // (see agents/hardware_speccer.py's SYSTEM_PROMPT -- "from_pin"/
        // "to_pin") through to the render layer. Both are optional --
        // an older spec generated before that schema change, or an edge
        // the model genuinely couldn't resolve to a named pin, simply
        // has them as null/undefined, and every place below treats
        // "missing" as "don't draw/show a pin label", never as an error.
        fromPin: e.from_pin || null,
        toPin: e.to_pin || null,
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
      linkLabel={(link) => {
        const pin = pinLabel(link);
        return pin ? `${link.kind} · ${pin}` : link.kind;
      }}
      linkCanvasObjectMode={() => "after"}
      linkCanvasObject={(link, ctx, globalScale) => {
        const label = pinLabel(link);
        if (!label) return;
        const start = link.source;
        const end = link.target;
        // react-force-graph mutates source/target from raw ids into the
        // actual node objects once the sim has laid them out -- before
        // that (first render / still settling), skip rather than draw
        // at a bogus position.
        if (typeof start !== "object" || typeof end !== "object"
            || start.x == null || end.x == null) return;

        // Bug fix (T2b, step 18a): the label used to sit exactly on the
        // raw link midpoint every time, which put it directly on top of
        // whatever node circle happened to be there. Nudge it a few
        // pixels along the perpendicular (normal) of the link's
        // direction vector instead of drawing right on the line.
        const dx = end.x - start.x;
        const dy = end.y - start.y;
        const len = Math.hypot(dx, dy) || 1;
        const offset = 6 / globalScale;
        const nx = -dy / len;
        const ny = dx / len;
        const baseMidX = (start.x + end.x) / 2;
        const baseMidY = (start.y + end.y) / 2;

        // T2b, step 18b: don't always offset to the same fixed side --
        // check both candidate positions against the graph's own node
        // list (already in scope via graphData) and pick whichever side
        // has fewer nodes nearby, so the label actually dodges node
        // circles instead of just moving a fixed distance off the line.
        const DENSITY_RADIUS = 40 / globalScale;
        const countNearby = (cx, cy) => {
          let count = 0;
          for (const n of graphData.nodes) {
            if (n.x == null || n.y == null) continue;
            if (Math.hypot(n.x - cx, n.y - cy) < DENSITY_RADIUS) count++;
          }
          return count;
        };
        const posCount = countNearby(baseMidX + nx * offset, baseMidY + ny * offset);
        const negCount = countNearby(baseMidX - nx * offset, baseMidY - ny * offset);
        const side = posCount <= negCount ? 1 : -1;

        const midX = baseMidX + nx * offset * side;
        const midY = baseMidY + ny * offset * side;
        const fontSize = 9 / globalScale;
        ctx.font = `${fontSize}px sans-serif`;
        const textWidth = ctx.measureText(label).width;
        const pad = 2 / globalScale;

        // Small dark backing box so the label stays legible over
        // whatever link line or other label it happens to sit on top of.
        ctx.fillStyle = "rgba(10, 10, 15, 0.75)";
        ctx.fillRect(
          midX - textWidth / 2 - pad,
          midY - fontSize / 2 - pad,
          textWidth + pad * 2,
          fontSize + pad * 2
        );

        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = EDGE_COLORS[link.kind] || DEFAULT_COLOR;
        ctx.fillText(label, midX, midY);
      }}
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
