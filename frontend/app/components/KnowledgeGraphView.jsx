"use client";
import { useMemo, useState } from "react";
import ForceGraphBase from "./ForceGraphBase";

// One color per domain ("section" in node metadata, Section 0.1). Kept
// as a flat palette rather than reusing agentRoleIcons.js's categorize()
// -- that table is keyed on agent/role names for the routing graph, a
// different vocabulary than "notes"/"research"/"plan"/etc.
const SECTION_COLORS = {
  notes: "#38bdf8",
  research: "#a78bfa",
  plan: "#f59e0b",
  simulate: "#34d399",
  growth: "#f472b6",
  build: "#818cf8",
  admin: "#94a3b8",
};
const DEFAULT_COLOR = "#6b7280";

// relation -> edge color. Deliberately small and un-opinionated: 0.2's
// `relation` field is free-form, so anything not in this table just
// falls back to a neutral gray line rather than the graph erroring or
// needing this table updated every time a new relation string appears.
const RELATION_COLORS = {
  supports: "#22c55e",
  cites: "#38bdf8",
  contradicts: "#ef4444",
  promoted_from: "#f59e0b",
};

function truncate(text, n = 220) {
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "…" : text;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/**
 * KnowledgeGraphView — second caller of ForceGraphBase (Part 0 Section
 * 0.2). Renders the cross-domain node/edge graph instead of an agent
 * routing trace; ForceGraphBase itself has no idea which one it's
 * drawing.
 *
 * `nodes`: node records as returned by the node-listing API (Section
 * 0.1) -- {node_id, workspace_id, section, node_type, created_by,
 * created_at, title, tags?}.
 * `edges`: edge records as returned by eo/graph_edges.py's list_edges()
 * -- {edge_id, from_node_id, to_node_id, relation, created_by, created_at}.
 * `onSelectNode`: optional callback(nodeRecord) fired on node click --
 * the natural hook for "open this note/finding/spec in its own panel."
 */
export default function KnowledgeGraphView({ nodes, edges, onSelectNode }) {
  const [hoveredNode, setHoveredNode] = useState(null);

  const graphData = useMemo(() => {
    const graphNodes = (nodes || []).map((n) => ({
      id: n.node_id,
      section: n.section,
      node_type: n.node_type,
      title: n.title || n.node_id,
      created_by: n.created_by,
      created_at: n.created_at,
      tags: n.tags || [],
      raw: n,
    }));

    const links = (edges || [])
      .filter((e) => e.from_node_id !== e.to_node_id)
      .map((e) => ({
        source: e.from_node_id,
        target: e.to_node_id,
        relation: e.relation,
        edge_id: e.edge_id,
      }));

    return { nodes: graphNodes, links };
  }, [nodes, edges]);

  const legend = useMemo(() => {
    const seenSections = new Set((nodes || []).map((n) => n.section).filter(Boolean));
    return Array.from(seenSections).map((section) => (
      <span key={section} className="flex items-center gap-1">
        <span
          className="inline-block w-2 h-2 rounded-full"
          style={{ backgroundColor: SECTION_COLORS[section] || DEFAULT_COLOR }}
        />
        {section}
      </span>
    ));
  }, [nodes]);

  return (
    <ForceGraphBase
      nodes={graphData.nodes}
      links={graphData.links}
      linkColor={(link) => RELATION_COLORS[link.relation] || DEFAULT_COLOR}
      linkWidth={1}
      linkLabel={(link) => link.relation}
      onNodeHover={setHoveredNode}
      onNodeClick={(node) => onSelectNode?.(node.raw)}
      nodeLabel={(node) => {
        const parts = [
          `<div style="font-weight:600">${escapeHtml(node.title)}</div>`,
          `<div style="opacity:.7">${escapeHtml(node.section || "")}${node.node_type ? ` · ${escapeHtml(node.node_type)}` : ""}</div>`,
        ];
        if (node.tags?.length) {
          parts.push(`<div style="opacity:.6;margin-top:2px">${node.tags.map(escapeHtml).join(", ")}</div>`);
        }
        return `<div style="background:#171717;border:1px solid #404040;border-radius:6px;padding:6px 8px;font-size:11px;color:#e5e5e5;max-width:320px;white-space:normal;word-break:break-word">${parts.join("")}</div>`;
      }}
      nodeCanvasObject={(node, ctx, globalScale) => {
        const color = SECTION_COLORS[node.section] || DEFAULT_COLOR;
        const r = 9;

        ctx.save();
        ctx.beginPath();
        ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.globalAlpha = 0.85;
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.lineWidth = node === hoveredNode ? 3 : 1.5;
        ctx.strokeStyle = "#e5e5e5";
        ctx.stroke();
        ctx.restore();

        const fontSize = 10 / globalScale;
        ctx.font = `${fontSize}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "alphabetic";
        ctx.fillStyle = "#a3a3a3";
        ctx.fillText(truncate(node.title, 28), node.x, node.y + r + 11 / globalScale);
      }}
      nodePointerAreaPaint={(node, color, ctx) => {
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(node.x, node.y, 12, 0, 2 * Math.PI);
        ctx.fill();
      }}
      legend={legend}
    />
  );
}