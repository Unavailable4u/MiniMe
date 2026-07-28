"use client";
import { useMemo, useRef, useState } from "react";
import ForceGraphBase from "./ForceGraphBase";
import { X } from "lucide-react";

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
  // NEW — topic-tree data source (GET /api/workspaces/{ws_id}/topics/graph):
  // agents/backlink_detector.py's four connection relations render as
  // free-form concept edges by default (not in this table -> falls back to
  // CONCEPT_EDGE_COLOR below); same_fact_as gets its own distinct color so
  // an overlapping_checker.py merge visually stands apart from both an
  // ordinary concept link and parent_of's tree scaffolding.
  same_fact_as: "#c026d3",
  // parent_of: the topic tree's own hierarchy edges (synthetic, not a
  // Backlink Detector relation) -- muted/structural, same treatment as
  // STRUCTURAL_RELATIONS' entries below rather than a "real" link color.
  parent_of: "#64748b",
};

// NEW — Notebooks integration guide §6.6 (Phase 3): the three relation
// strings written by deterministic, zero-LLM passes (source_ingestor's
// same_source chaining, note_clusterer's clustered_with, the old
// backlink_detector's references). Everything else is a free-form
// relation phrase written by agents/concept_linker.py's real semantic
// pass (e.g. "both cover federated learning") -- see that module's
// CONCEPT_LINKER_BRIEF. Kept as an explicit exclusion list rather than
// an allowlist so a brand-new concept relation phrase is never
// mistaken for a structural edge just because this table doesn't know
// about it yet.
const STRUCTURAL_RELATIONS = new Set(["same_source", "clustered_with", "references", "parent_of"]);
// Distinct, brighter than DEFAULT_COLOR and every STRUCTURAL_RELATIONS
// color, so a concept_linker edge visually pops out of the structural
// clutter it's meant to sit alongside rather than blend into.
const CONCEPT_EDGE_COLOR = "#22d3ee";

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
 * When `nodeSummaries` is also passed (see below), a click opens the
 * inline rationale panel instead and `onSelectNode` only fires from
 * that panel's explicit "View full source" action -- see the
 * `nodeSummaries` prop doc for why.
 *
 * `nodeSummaries`: optional {node_id: summary} map, as returned by
 * GET /api/workspaces/{ws_id}/graph/node_summaries (eo/node_summaries.py,
 * written by agents/concept_linker.py's role pass -- guide §6.6/§7).
 * Passing this turns on the two Phase 3 concept-graph behaviors the
 * plain structural graph (ResearchTab's caller, which never passes
 * this) doesn't need:
 *   - concept_linker's free-form relation edges (anything not in
 *     STRUCTURAL_RELATIONS below) render brighter/thicker than the
 *     structural same_source/clustered_with/references edges, so the
 *     real semantic links this phase adds are visually distinct from
 *     the structural scaffolding they sit alongside.
 *   - clicking a node opens an inline "rationale" panel showing that
 *     node's agent-written summary plus every neighboring edge's
 *     relation phrase (the human-readable rationale for *why* it's
 *     linked, per guide §6.6 -- "the relation string itself becomes
 *     the human-readable rationale") instead of immediately jumping to
 *     the full-content modal.
 *
 * `pulsingIds`: optional `Set` of node ids (full `node:{workspace_id}:
 * {node_id}` form, matching graphNodes' own `id` below) that should
 * render gold-highlighted -- populated by a parent subscribed to this
 * workspace's live events (topic_added/topic_merged/connection_added)
 * so a just-changed topic is visually obvious without a manual refresh.
 * Additive: omitting it renders exactly as before.
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
export default function KnowledgeGraphView({ nodes, edges, onSelectNode, nodeSummaries, pulsingIds }) {
  const [hoveredNode, setHoveredNode] = useState(null);
  // NEW — Phase 3: which node's inline rationale panel is open, only
  // ever set when `nodeSummaries` is passed (see docstring above).
  const [rationaleNode, setRationaleNode] = useState(null);

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

    // NEW — Phase 3 "root = source/file, children = topics" cue (guide
    // §6.6). The real node model doesn't have a separate topic-node
    // type (concept_linker links existing whole-source nodes to each
    // other, per §3's one-node-per-file fix -- it doesn't mint new
    // nodes), so this can't be a literal parent/child layout. What the
    // data DOES already carry is source_ingestor's same_source chain:
    // every non-first section of a still-fragmented source points a
    // same_source edge back at its root. Using that to mark "child"
    // nodes (drawn smaller, see nodeCanvasObject below) gives the
    // root-vs-child visual distinction the guide asks for without
    // inventing node types the backend doesn't produce.
    const childIds = new Set(
      (edges || [])
        .filter((e) => e.relation === "same_source")
        .map((e) => e.from_node_id)
    );
    for (const n of graphNodes) n.isChild = childIds.has(n.id);

    // NEW — Phase 3: node_id -> [{other node, relation, edge_id}], used
    // by the rationale panel to answer "why is this node linked to
    // that one" on click. Built here (not off graphData.links) on
    // purpose: react-force-graph mutates a link's `source`/`target`
    // from the plain id string this component sets into an actual
    // node-object reference once the sim ingests it, since it's handed
    // the exact same link objects by reference (ForceGraphBase's own
    // graphData useMemo just wraps, doesn't clone). Recomputing from
    // the raw `e.from_node_id`/`e.to_node_id` strings here keeps this
    // lookup correct regardless of what the simulation has done to
    // graphData.links by the time a click happens.
    const byId = new Map(graphNodes.map((n) => [n.id, n]));
    const neighbors = new Map();
    for (const e of edges || []) {
      if (e.from_node_id === e.to_node_id) continue;
      const a = byId.get(e.from_node_id);
      const b = byId.get(e.to_node_id);
      if (!a || !b) continue;
      if (!neighbors.has(a.id)) neighbors.set(a.id, []);
      if (!neighbors.has(b.id)) neighbors.set(b.id, []);
      neighbors.get(a.id).push({ node: b, relation: e.relation, edge_id: e.edge_id });
      neighbors.get(b.id).push({ node: a, relation: e.relation, edge_id: e.edge_id });
    }

    return { nodes: graphNodes, links, neighbors };
  }, [nodes, edges]);

  const legend = useMemo(() => {
    const seenSections = new Set((nodes || []).map((n) => n.section).filter(Boolean));
    const items = Array.from(seenSections).map((section) => (
      <span key={section} className="flex items-center gap-1">
        <span
          className="inline-block w-2 h-2 rounded-full"
          style={{ backgroundColor: SECTION_COLORS[section] || DEFAULT_COLOR }}
        />
        {section}
      </span>
    ));
    // NEW — Phase 3: only shown in concept-graph mode (nodeSummaries
    // passed), since STRUCTURAL_RELATIONS/CONCEPT_EDGE_COLOR styling
    // below is only meaningfully different from the plain graph in
    // that mode.
    if (nodeSummaries) {
      items.push(
        <span key="__concept_edge_legend" className="flex items-center gap-1">
          <span className="inline-block w-3 h-0.5 rounded-full" style={{ backgroundColor: CONCEPT_EDGE_COLOR }} />
          concept link
        </span>
      );
    }
    // NEW — Backlinks-as-topic-tree: parent_of/same_fact_as only ever
    // appear in edges built by GET /api/workspaces/{ws_id}/topics/graph
    // (api/server.py) -- ResearchTab's plain doc graph and the
    // structural/concept-graph modes above never produce them. Gated on
    // actually seeing the relation in THIS render's edges (not just "is
    // nodeSummaries passed") so a caller that reuses this component for
    // some other future edge vocabulary doesn't inherit a legend entry
    // for a relation it never draws.
    const seenRelations = new Set((edges || []).map((e) => e.relation));
    if (seenRelations.has("parent_of")) {
      items.push(
        <span key="__parent_of_legend" className="flex items-center gap-1">
          <span className="inline-block w-3 h-0.5 rounded-full" style={{ backgroundColor: RELATION_COLORS.parent_of }} />
          parent link
        </span>
      );
    }
    if (seenRelations.has("same_fact_as")) {
      items.push(
        <span key="__same_fact_as_legend" className="flex items-center gap-1">
          <span className="inline-block w-3 h-0.5 rounded-full" style={{ backgroundColor: RELATION_COLORS.same_fact_as }} />
          same fact as
        </span>
      );
    }
    return items;
  }, [nodes, edges, nodeSummaries]);

  const rationaleNeighbors = rationaleNode ? (graphData.neighbors.get(rationaleNode.id) || []) : [];

  return (
    <div className="relative h-full w-full">
      <ForceGraphBase
        nodes={graphData.nodes}
        links={graphData.links}
        linkColor={(link) =>
          RELATION_COLORS[link.relation] ||
          (STRUCTURAL_RELATIONS.has(link.relation) ? DEFAULT_COLOR : CONCEPT_EDGE_COLOR)
        }
        linkWidth={(link) => (STRUCTURAL_RELATIONS.has(link.relation) ? 1 : 2)}
        linkLabel={(link) => link.relation}
        onNodeHover={setHoveredNode}
        onNodeClick={(node) => {
          // NEW — Phase 3: in concept-graph mode, a click opens the
          // inline rationale panel (title + agent summary + every
          // neighbor's relation phrase) instead of immediately handing
          // off to onSelectNode's full-content modal -- see this
          // component's docstring. Plain mode (ResearchTab, no
          // nodeSummaries) keeps the original one-click-to-modal
          // behavior unchanged.
          if (nodeSummaries) setRationaleNode(node);
          else onSelectNode?.(node.raw);
        }}
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
          // NEW — live-event highlight: gold overrides the normal
          // section/default color for ~1.8s after a topic_added/
          // topic_merged/connection_added event names this node id
          // (see pulsingIds prop doc above). Checked before the normal
          // color lookup so it wins regardless of section.
          const color = pulsingIds?.has(node.id)
            ? "#facc15"
            : (SECTION_COLORS[node.section] || DEFAULT_COLOR);
          // NEW — Phase 3 root/child sizing cue (see graphData's
          // childIds comment above): only applied in concept-graph
          // mode so the plain structural graph's node sizing is
          // unchanged for ResearchTab.
          const r = nodeSummaries && node.isChild ? 6 : 9;

          ctx.save();
          ctx.beginPath();
          ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
          ctx.fillStyle = color;
          ctx.globalAlpha = 0.85;
          ctx.fill();
          ctx.globalAlpha = 1;
          ctx.lineWidth = node === hoveredNode || node === rationaleNode ? 3 : 1.5;
          ctx.strokeStyle = node === rationaleNode ? CONCEPT_EDGE_COLOR : "#e5e5e5";
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

      {/* NEW — Phase 3 rationale panel (guide §6.6: "click-to-see-
          rationale using node_summaries"). Only ever rendered in
          concept-graph mode. Positioned over the top-right corner of
          the graph so it doesn't collide with the legend's
          bottom-right slot. */}
      {rationaleNode && (
        <div className="absolute top-2 right-2 z-20 w-64 max-h-[calc(100%-1rem)] overflow-y-auto rounded-lg border border-[var(--neutral-700)] bg-[var(--neutral-900)]/95 p-3 text-xs shadow-lg">
          <div className="flex items-start justify-between gap-2 mb-1.5">
            <h4 className="font-medium text-[var(--neutral-200)] leading-snug">{rationaleNode.title}</h4>
            <button
              onClick={() => setRationaleNode(null)}
              className="shrink-0 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
            >
              <X size={13} />
            </button>
          </div>
          <p className="text-[var(--neutral-400)] leading-relaxed mb-2">
            {nodeSummaries?.[rationaleNode.raw?.node_id] || "No summary yet — run Regenerate to have the agent write one."}
          </p>
          {rationaleNeighbors.length > 0 ? (
            <div className="space-y-1.5 border-t border-[var(--neutral-800)] pt-2">
              <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Why it's linked</p>
              {rationaleNeighbors.map((n) => (
                <div key={n.edge_id} className="flex flex-col">
                  <span className="text-[var(--neutral-300)] truncate">{n.node.title}</span>
                  <span className="text-[10px] text-[var(--neutral-500)] italic">{n.relation}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-[10px] text-[var(--neutral-600)] border-t border-[var(--neutral-800)] pt-2">No links yet.</p>
          )}
          <button
            onClick={() => onSelectNode?.(rationaleNode.raw)}
            className="mt-2.5 w-full text-[11px] text-center rounded border border-[var(--neutral-700)] text-[var(--neutral-300)] hover:text-[var(--neutral-100)] hover:border-[var(--neutral-600)] py-1"
          >
            View full source
          </button>
        </div>
      )}
    </div>
  );
}

