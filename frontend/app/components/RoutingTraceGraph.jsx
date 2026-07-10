"use client";
import { useMemo, useRef, useState } from "react";
import ForceGraphBase from "./ForceGraphBase";

// The full icon table (role name -> {icon, color}, spanning coding,
// writing, music, legal, medical, business, science, trades, etc --
// this app isn't just for coding tasks) lives in agentRoleIcons.js so
// it can grow without bloating this file. "Writing a new role brief: X"
// (eo/panel.py's panel_brief_writer step) is matched there too, first,
// so it doesn't fall through to the generic default.
import { categorize, INPUT_CATEGORY, OUTPUT_CATEGORY } from "./agentRoleIcons";

const REASON_COLORS = {
  plan: "#6b7280",
  "error-priority": "#ef4444",
  recheck: "#f59e0b",
  skip: "#8b5cf6",
  escalate: "#ec4899",
  requested: "#22d3ee",   // agent_requested_role edges
};

// Matches eo/agents/code_writers.py's "Code Writer {worker_id} — {name}"
// and agents/reviewer.py's "Reviewer {worker_index}" -- both fire their
// own agent_start/agent_done WHILE the outer dispatcher-level step
// (e.g. "implementer") is still open, i.e. genuinely parallel workers
// under one stage, not a sequential chain. See groupWorkers() below.
function workerGroupOf(label) {
  const m = /^(Code Writer|Reviewer)\s+(\d+)/i.exec(label || "");
  if (!m) return null;
  return m[1].toLowerCase().startsWith("code") ? "Code Writer Pool" : "Reviewer Pool";
}

function briefedRole(label) {
  const m = /^Writing a new role brief:\s*(.+)$/i.exec(label || "");
  return m ? m[1].trim() : null;
}

// Short form for the label drawn UNDER the node on canvas, where space
// is genuinely tight. This is cosmetic-only -- the hover tooltip always
// uses fullNameOf() instead (see nodeLabel below), so the complete name
// is never actually lost, just abbreviated in the one place that can't
// fit it.
function shortLabel(id) {
  const brief = briefedRole(id);
  if (brief) return `Brief: ${brief}`;
  return id.length > 26 ? id.slice(0, 24) + "…" : id;
}

// Untruncated version, used ONLY for the hover tooltip. Every upsert()
// call below sets both `display` (shortLabel) and `fullName`
// (fullNameOf) on the node -- this is the piece that was previously
// missing: a node could carry a fullName field in theory, but if
// nothing ever assigned it, nodeLabel silently fell back to the
// truncated `display` anyway. Every call site here is wired to set it.
function fullNameOf(id) {
  const brief = briefedRole(id);
  return brief ? `Brief: ${brief}` : id;
}

function truncate(text, n = 220) {
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "…" : text;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// `trace`: array of {destination, reason} from eo/dispatcher.py's
// _log_route(), via the "dispatch_event" relay event. Used here only to
// annotate WHY the backbone moved from one dispatcher-level role to the
// next (plan/recheck/escalate coloring) -- it is no longer the source of
// which nodes exist. See `steps` below for that.
//
// `steps` (the same liveSteps/message.steps array AgentStepList.jsx
// renders): this is the actual source of truth for nodes. It captures
// EVERY agent_start/agent_done pair that ran, in the order it started --
// dispatcher-level roles, Inspector, SGA, panel_brief_writer's
// "Writing a new role brief: X" steps, and nested per-worker steps
// (Code Writer N, Reviewer N) alike. Building the graph from this instead
// of `trace` is what makes every worked role show up, including ones
// that never went through the dispatcher.
//
// `roleRequests`: agent_requested_role events -- eo/executor.py
// auto-inserting a missing prerequisite role. Rendered as its own
// distinct edge color/label ("requested").
//
// `suggestedAgents`: decision.suggested_agents -- used to draw a
// "planned pipeline" placeholder chain the moment classification
// completes, before any agent_start has actually arrived yet, so the
// graph has something to show immediately instead of appearing empty
// until the first real step lands.
//
// `runStatus`: "running" | "done" | "error" -- drives the Output node's
// status ring so it visibly flips from pending -> done/error.
//
// NOTE: this component now only computes `{nodes, links}` from routing-
// specific inputs and how to DRAW a routing node/link -- the actual
// ForceGraph2D wiring (sizing, dynamic import, zoom-to-fit) lives in the
// generic ForceGraphBase, shared with KnowledgeGraphView.jsx (Part 0
// Section 0.2). Nothing about the graph SHAPE below changed.
export default function RoutingTraceGraph({ trace, suggestedAgents, steps, roleRequests, runStatus }) {
  const [hoveredNode, setHoveredNode] = useState(null);

  // FIX (graph reflows/jumps on every single event instead of growing
  // smoothly): react-force-graph keys physics state off object identity,
  // not id. Rebuilding a brand-new plain object for every node on every
  // render meant the WHOLE simulation restarted from scratch each time,
  // which is why nothing looked "live" -- it only visually settled once
  // events stopped arriving. Keeping one persistent object per node id
  // (mutated in place, not replaced) lets already-placed nodes keep their
  // x/y and only genuinely new nodes enter unplaced.
  const nodeObjectsRef = useRef(new Map());

  // Most recent step per role/id -- a role can be revisited (recheck/
  // escalate), and the latest run of it is what the node should reflect.
  const stepByRole = useMemo(() => {
    const map = {};
    for (const s of steps || []) map[s.role || s.agent] = s;
    return map;
  }, [steps]);

  const graphData = useMemo(() => {
    const upsert = (id, patch) => {
      let obj = nodeObjectsRef.current.get(id);
      if (!obj) {
        obj = { id };
        nodeObjectsRef.current.set(id, obj);
      }
      Object.assign(obj, patch);
      return obj;
    };

    const links = [];
    const seenPairs = {};
    const addLink = (source, target, reason) => {
      if (!source || !target || source === target) return;
      const key = `${source}=>${target}`;
      seenPairs[key] = (seenPairs[key] || 0) + 1;
      links.push({ source, target, reason, curvature: seenPairs[key] === 1 ? 0 : 0.3 * seenPairs[key] });
    };

    const orderedSteps = steps || [];
    const usedIds = new Set(["__input__", "__output__"]);

    upsert("__input__", {
      category: INPUT_CATEGORY, status: "done", display: "Input", fullName: "Input", isEndpoint: true,
      summary: "Task received", durationMs: null,
    });
    upsert("__output__", {
      category: OUTPUT_CATEGORY,
      status: runStatus === "error" ? "error" : runStatus === "done" ? "done" : "pending",
      display: "Output", fullName: "Output", isEndpoint: true,
      summary: runStatus === "done" ? "Result delivered" : runStatus === "error" ? "Run failed" : null,
      durationMs: null,
    });

    // No real steps yet -- draw the PLANNED pipeline as a placeholder
    // chain the moment routing/classification finishes, so the graph is
    // never just sitting empty waiting for the first agent_start.
    if (orderedSteps.length === 0 && suggestedAgents?.length) {
      let prev = "__input__";
      for (const role of suggestedAgents) {
        usedIds.add(role);
        upsert(role, {
          category: categorize(role), status: "pending",
          display: shortLabel(role), fullName: fullNameOf(role),
        });
        addLink(prev, role, "plan");
        prev = role;
      }
      addLink(prev, "__output__", "plan");
    }

    // dispatcher reasons, keyed by destination role, for backbone edge
    // coloring (plan/recheck/escalate) where they line up.
    const reasonByDestination = {};
    for (const t of trace || []) if (t.destination) reasonByDestination[t.destination] = t.reason;

    let anchor = "__input__";
    let openGroup = null; // { key, members: [] } -- a currently fanned-out worker pool

    for (const step of orderedSteps) {
      const id = step.role || step.agent;
      if (!id) continue;
      usedIds.add(id);
      const group = workerGroupOf(id);

      upsert(id, {
        category: categorize(id),
        status: step.status,
        summary: step.summary,
        durationMs: step.durationMs,
        display: shortLabel(id),
        fullName: fullNameOf(id),
      });

      if (group) {
        // A worker belonging to a pool -- fan out from whatever anchor
        // was current BEFORE this pool started (its outer dispatcher-
        // level step), not chained to the previous worker.
        if (!openGroup || openGroup.key !== group) {
          openGroup = { key: group, anchor, members: [] };
        }
        openGroup.members.push(id);
        addLink(openGroup.anchor, id, "plan");
        continue;
      }

      if (openGroup) {
        // First non-worker step after a pool closes -- fan every pool
        // member back into it, then resume the normal single-file chain.
        for (const m of openGroup.members) addLink(m, id, "plan");
        openGroup = null;
      } else {
        addLink(anchor, id, reasonByDestination[id] || "plan");
      }
      anchor = id;
    }

    // Still-open pool at the end of the recorded steps (mid-run) -- leave
    // it fanned out; it'll converge once the next step lands. If the run
    // is actually finished, converge it into Output instead of leaving
    // it dangling.
    if (openGroup) {
      const target = runStatus && runStatus !== "running" ? "__output__" : null;
      if (target) for (const m of openGroup.members) addLink(m, target, "plan");
    } else if (orderedSteps.length > 0) {
      addLink(anchor, "__output__", reasonByDestination["__output__"] || "plan");
    }

    // agent_requested_role overlay -- a runtime self-heal insertion,
    // kept as its own distinctly colored edge so it's obviously not part
    // of the original plan.
    for (const req of roleRequests || []) {
      if (!req.requestedRole) continue;
      const sourceId =
        orderedSteps.find((s) => s.agent === req.requestingAgent)?.role || req.requestingAgent;
      usedIds.add(sourceId);
      usedIds.add(req.requestedRole);
      upsert(sourceId, (nodeObjectsRef.current.get(sourceId)) || { category: categorize(sourceId), status: "done" });
      upsert(req.requestedRole, nodeObjectsRef.current.get(req.requestedRole) || {
        category: categorize(req.requestedRole), status: stepByRole[req.requestedRole]?.status || "pending",
        display: shortLabel(req.requestedRole), fullName: fullNameOf(req.requestedRole),
      });
      addLink(sourceId, req.requestedRole, "requested");
    }

    // Only surface nodes actually referenced this render (a fresh
    // component instance's nodeObjectsRef starts empty anyway, but this
    // keeps things correct if this ever gets reused across messages).
    const nodes = Array.from(usedIds).map((id) => nodeObjectsRef.current.get(id)).filter(Boolean);

    return { nodes, links };
  }, [trace, suggestedAgents, roleRequests, steps, stepByRole, runStatus]);

  const legend = (
    <>
      <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full border border-emerald-500" /> done</span>
      <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full border border-amber-500" /> running</span>
      <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full border border-red-500" /> error</span>
      <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full border-2 border-yellow-500" style={{ borderStyle: "dashed" }} /> awaiting approval</span>
      <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full border border-neutral-500" style={{ borderStyle: "dashed" }} /> pending</span>
      <span className="flex items-center gap-1"><span className="inline-block w-2 h-0.5 bg-cyan-400" /> requested</span>
    </>
  );

  return (
    <ForceGraphBase
      nodes={graphData.nodes}
      links={graphData.links}
      linkColor={(link) => REASON_COLORS[link.reason] || "#6b7280"}
      linkWidth={(link) => (link.reason === "requested" ? 2 : 1)}
      linkLabel={(link) => link.reason}
      onNodeHover={setHoveredNode}
      nodeLabel={(node) => {
        // FIX (long agent names get cut off on hover too): this used to
        // reuse the same already-truncated `display` string as the
        // under-node label, so anything past ~26 chars was unreadable
        // everywhere, including hover -- the one place with room to
        // show it in full. node.fullName is untruncated and is always
        // set alongside node.display wherever a node is upserted above.
        const parts = [
          `<div style="font-weight:600">${escapeHtml(node.category.icon)} ${escapeHtml(node.fullName || node.display || node.id)}</div>`,
          `<div style="opacity:.7">${escapeHtml(node.status)}${node.durationMs != null ? ` · ${node.durationMs}ms` : ""}</div>`,
        ];
        if (node.summary) {
          parts.push(`<div style="max-width:320px;white-space:normal;word-break:break-word;margin-top:2px">${escapeHtml(truncate(node.summary))}</div>`);
        }
        return `<div style="background:#171717;border:1px solid #404040;border-radius:6px;padding:6px 8px;font-size:11px;color:#e5e5e5;max-width:340px;white-space:normal;word-break:break-word">${parts.join("")}</div>`;
      }}
      nodeCanvasObject={(node, ctx, globalScale) => {
        const { icon, color } = node.category;
        // FIX (icons too small / shapes too busy): flat circular badge
        // for every node -- category shown via color+icon, no more
        // diamond/hexagon/triangle geometry competing with the glyph.
        // Endpoint nodes (Input/Output) get a slightly larger badge so
        // they read as the start/end of the flow, not just another step.
        const r = node.isEndpoint ? 13 : 11;
        // Part 2 §2.4/§2.7 — "awaiting_approval" gets its own distinct
        // ring color (not just amber's "running" — a paused run needs to
        // read as visibly DIFFERENT from one still actively working, not
        // just slow) plus a pulsing dash so a paused node is obviously
        // paused on the graph itself, not only in the step list. Falls
        // back gracefully for every other status, same as before — this
        // is purely additive.
        const isPaused = node.status === "awaiting_approval";
        const ringColor =
          node.status === "error" ? "#ef4444" :
          isPaused ? "#eab308" :
          node.status === "running" ? "#f59e0b" :
          node.status === "done" ? "#22c55e" :
          node.status === "pending" ? "#525252" : "#525252";

        ctx.save();
        ctx.beginPath();
        ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.globalAlpha = node.status === "pending" ? 0.22 : node.status === "running" || isPaused ? 0.6 : 0.92;
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.lineWidth = node === hoveredNode ? 3 : isPaused ? 3 : 2;
        if (node.status === "pending") ctx.setLineDash([2, 2]);
        // Pulsing dashed ring for a paused node — dash offset animates
        // off Date.now() so it visibly "breathes" on every animation
        // frame the force graph already re-renders for physics anyway,
        // no extra timer needed.
        if (isPaused) {
          ctx.setLineDash([4, 3]);
          ctx.lineDashOffset = -(Date.now() / 60) % 7;
        }
        ctx.strokeStyle = ringColor;
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.restore();

        // Big icon glyph, front and center -- sized off the badge
        // radius (not a flat px value) so it always fills the badge.
        const iconSize = (r * 1.65) / globalScale;
        ctx.font = `${iconSize}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(icon, node.x, node.y);

        // Label below.
        const fontSize = 11 / globalScale;
        ctx.font = `${node.isEndpoint ? "700 " : ""}${fontSize}px sans-serif`;
        ctx.textBaseline = "alphabetic";
        ctx.fillStyle = node.isEndpoint ? "#e5e5e5" : "#a3a3a3";
        ctx.fillText(node.display || node.id, node.x, node.y + r + 12 / globalScale);
      }}
      nodePointerAreaPaint={(node, color, ctx) => {
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.isEndpoint ? 15 : 13, 0, 2 * Math.PI);
        ctx.fill();
      }}
      legend={legend}
    />
  );
}