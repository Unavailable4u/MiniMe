"use client";
import { useMemo, useRef, useEffect, useState } from "react";
import dynamic from "next/dynamic";

// react-force-graph-2d touches the canvas/window at import time, so it
// has to load client-side only -- same reasoning as any other
// canvas/WebGL library under Next.js's app router.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

const REASON_COLORS = {
  plan: "#6b7280",
  "error-priority": "#ef4444",
  recheck: "#f59e0b",
  skip: "#8b5cf6",
  escalate: "#ec4899",
};

// `trace`: array of {destination, reason} from eo/dispatcher.py's
// _log_route() via the "dispatch_event" relay event -- confirmed shape,
// unchanged by this rewrite. Consecutive entries are true edges:
// trace[i].destination is exactly what ran and then triggered
// trace[i+1]'s routing decision.
//
// `suggestedAgents`: decision.suggested_agents -- confirmed to exist
// (already read by RoutingTraceCard.jsx). Used only as a best-effort
// seed for the very first node. See Part 18 guide §1 for why this is
// flagged, not guaranteed: `trace` itself never logs the plan's
// starting role (nothing routes "into" step 0), and
// decision.execution_order isn't confirmed present on every tier.
export default function RoutingTraceGraph({ trace, suggestedAgents }) {
  const fgRef = useRef();
  const containerRef = useRef(null);
  const [dims, setDims] = useState({ width: 600, height: 320 });

  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    const observer = new ResizeObserver(([entry]) => {
      setDims({ width: entry.contentRect.width, height: 320 });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const graphData = useMemo(() => {
    const nodeSet = new Set();
    const links = [];

    const seed = suggestedAgents?.[0];
    let prev = seed || null;
    if (seed) nodeSet.add(seed);

    for (const step of trace) {
      nodeSet.add(step.destination);
      if (prev) links.push({ source: prev, target: step.destination, reason: step.reason });
      prev = step.destination;
    }

    // Repeated links (a role revisited more than once) get a small
    // curvature bump per repeat so they fan out visually instead of
    // drawing exactly on top of each other.
    const seenPairs = {};
    for (const link of links) {
      const key = `${link.source}->${link.target}`;
      seenPairs[key] = (seenPairs[key] || 0) + 1;
      link.curvature = seenPairs[key] === 1 ? 0 : 0.25 * seenPairs[key];
    }

    return { nodes: Array.from(nodeSet).map((id) => ({ id })), links };
  }, [trace, suggestedAgents]);

  if (graphData.nodes.length === 0) return null;

  return (
    <div ref={containerRef} className="rounded-lg border border-neutral-800 overflow-hidden">
      <ForceGraph2D
        ref={fgRef}
        graphData={graphData}
        width={dims.width}
        height={dims.height}
        backgroundColor="#0a0a0a"
        linkColor={(link) => REASON_COLORS[link.reason] || "#6b7280"}
        linkCurvature="curvature"
        linkDirectionalArrowLength={4}
        linkDirectionalArrowRelPos={1}
        linkLabel={(link) => link.reason}
        cooldownTicks={80}
        onEngineStop={() => fgRef.current?.zoomToFit(400, 40)}
        nodeCanvasObject={(node, ctx, globalScale) => {
          const fontSize = 12 / globalScale;
          ctx.beginPath();
          ctx.arc(node.x, node.y, 5, 0, 2 * Math.PI);
          ctx.fillStyle = "#e5e5e5";
          ctx.fill();
          ctx.font = `${fontSize}px sans-serif`;
          ctx.textAlign = "center";
          ctx.fillStyle = "#a3a3a3";
          ctx.fillText(node.id, node.x, node.y + 12);
        }}
        nodePointerAreaPaint={(node, color, ctx) => {
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x, node.y, 7, 0, 2 * Math.PI);
          ctx.fill();
        }}
      />
    </div>
  );
}
