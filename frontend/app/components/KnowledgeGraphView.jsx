"use client";
import { useMemo, useRef, useState } from "react";
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
 *
 * FIX (dangling-edge crash): eo/graph_edges.py's list_edges() scopes by
 * "node_id belongs to this workspace OR the other endpoint does" -- so
 * a cross-workspace edge, or an edge left behind by a node deletion
 * that didn't cascade (see knowledge_graph.py's delete_node() docstring,
 * which explicitly punts that cleanup to the caller), can arrive here
 * referencing a node_id that isn't in this render's `nodes` prop.
 * ForceGraphBase hands {nodes, links} straight to react-force-graph-2d,
 * whose underlying d3-force-3d throws "node not found: node:..." the
 * moment it tries to resolve a link's source/target against a node
 * that isn't there -- an unhandled exception that took down the whole
 * page. Filtering links down to edges whose BOTH endpoints are present
 * in the current node set keeps a stale/cross-scope edge from ever
 * reaching ForceGraphBase, regardless of which upstream cause produced
 * it.
 */
export default function KnowledgeGraphView({ nodes, edges, onSelectNode }) {
  const [hoveredNode, setHoveredNode] = useState(null);

  // FIX (nodes fly away on hover): ForceGraphBase's own docstring warns
  // that react-force-graph keys physics/position state (x/y/vx/vy) off
  // node OBJECT IDENTITY, not id -- callers that rebuild fresh node
  // objects on every render (as the old version of the useMemo below
  // did) look like an entirely new graph to the simulation each time,
  // so it reheats and flings nodes to new positions. Hovering sets
  // hoveredNode state, which re-renders this component; if that (or
  // anything upstream) causes graphNodes to be rebuilt, this was firing.
  // Keeping one persistent object per node id -- created once, mutated
  // in place thereafter -- means react-force-graph always sees the SAME
  // object for a given node across renders, so its simulation state
  // (and the node's current x/y) survives re-renders untouched. Same
  // pattern RoutingTraceGraph.jsx's nodeObjectsRef already uses.
  const nodeObjectsRef = useRef(new Map());

  const graphData = useMemo(() => {
    // FIX (dropped-edges bug): the node-listing API returns each node's
    // bare `node_id` (see api/server.py's delete route, which has to
    // manually build `f"node:{ws_id}:{node_id}"` before it can compare
    // against graph_edges.py data -- proof that node_id alone isn't in
    // that prefixed shape). graph_edges.py's edges, however, store
    // from_node_id/to_node_id in the full `node:{workspace_id}:{node_id}`
    // form (see _workspace_of). Using the bare id here made nodeIds a
    // set graph_edges' ids could never match, so every detected edge
    // got dropped by the filter below, 100% of the time.
    const seenIds = new Set();
    const graphNodes = (nodes || []).map((n) => {
      const id = `node:${n.workspace_id}:${n.node_id}`;
      seenIds.add(id);
      let obj = nodeObjectsRef.current.get(id);
      if (!obj) {
        obj = { id };
        nodeObjectsRef.current.set(id, obj);
      }
      // Mutate the existing object in place -- this is the part that
      // keeps identity stable. Never do `obj = {...}` here.
      obj.section = n.section;
      obj.node_type = n.node_type;
      obj.title = n.title || n.node_id;
      obj.created_by = n.created_by;
      obj.created_at = n.created_at;
      obj.tags = n.tags || [];
      obj.raw = n;
      return obj;
    });

    // Drop persistent objects for nodes no longer present (deleted,
    // filtered, navigated away from) so this map doesn't grow forever.
    for (const id of nodeObjectsRef.current.keys()) {
      if (!seenIds.has(id)) nodeObjectsRef.current.delete(id);
    }

    // NEW — the set of node ids actually present in this render, so an
    // edge referencing a missing/filtered/not-yet-loaded node can never
    // reach ForceGraphBase -> d3-force-3d, which throws "node not
    // found" when it can't resolve a link's source/target by id.
    const nodeIds = new Set(graphNodes.map((n) => n.id));

    const droppedEdges = [];
    const links = (edges || [])
      .filter((e) => e.from_node_id !== e.to_node_id)
      .filter((e) => {
        const ok = nodeIds.has(e.from_node_id) && nodeIds.has(e.to_node_id);
        if (!ok) droppedEdges.push(e);
        return ok;
      })
      .map((e) => ({
        source: e.from_node_id,
        target: e.to_node_id,
        relation: e.relation,
        edge_id: e.edge_id,
      }));

    // NEW — a dropped edge means real backend data is inconsistent
    // (cross-workspace edge, or a node deletion that didn't cascade to
    // its edges), not just a frontend timing blip. Surface it instead
    // of silently hiding it, so the underlying gap doesn't go unnoticed.
    if (droppedEdges.length && typeof window !== "undefined") {
      console.warn(
        `[KnowledgeGraphView] dropped ${droppedEdges.length} edge(s) referencing ` +
        `a node not present in this workspace's node list:`,
        droppedEdges
      );
    }

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

