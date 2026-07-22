"use client";
import { useRef, useEffect, useState, useMemo } from "react";
import dynamic from "next/dynamic";

// react-force-graph-2d touches the canvas/window at import time, so it
// has to load client-side only -- same reasoning as any other
// canvas/WebGL library under Next.js's app router.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

/**
 * ForceGraphBase — the part of RoutingTraceGraph.jsx that had nothing to
 * do with routing traces specifically: sizing, the dynamic ForceGraph2D
 * import, hover state, and the debounced zoom-to-fit. Every domain-
 * specific piece (what a node looks like, what an edge color means, what
 * the legend says) is a prop, not baked in here.
 *
 * Callers: RoutingTraceGraph.jsx (agent routing/dispatch graph) and
 * KnowledgeGraphView.jsx (Part 0 Section 0.2 cross-domain node graph).
 * Both hand this component a `{nodes, links}` pair plus their own
 * render callbacks -- this component doesn't know or care what a node
 * "is" beyond having an `id`.
 *
 * Node object identity matters: react-force-graph keys physics/position
 * state off object identity, not id. Callers that want nodes to keep
 * their x/y across re-renders (instead of the whole simulation
 * restarting) should keep one persistent object per node id themselves
 * (see RoutingTraceGraph.jsx's nodeObjectsRef) and pass THOSE objects in,
 * mutated in place rather than rebuilt from scratch each render.
 *
 * FIX (nodes fly away on hover, part 2): stable node objects alone
 * aren't enough. react-force-graph's prop diffing checks the `graphData`
 * prop itself by reference -- if this component builds a fresh
 * `{ nodes, links }` object literal on every render (as it used to,
 * inline in JSX), that counts as "new data" even when the nodes/links
 * arrays inside are unchanged. Setting graphData again reheats the
 * simulation (alpha -> 1), which flings already-settled nodes apart.
 * Since onNodeHover causes callers to re-render this component on every
 * mouse-move over the canvas, that reheat was firing constantly.
 * Memoizing graphData on [nodes, links] keeps its reference stable
 * across hover-driven re-renders, so only genuine data changes reheat
 * the sim.
 */
export default function ForceGraphBase({
  nodes,
  links,
  height = 360,
  backgroundColor, // omit to follow the theme; pass a literal color to override
  linkColor,
  linkWidth,
  linkLabel,
  linkCurvature = "curvature",
  linkDirectionalArrowLength = 5,
  linkDirectionalArrowRelPos = 1,
  nodeLabel,
  nodeCanvasObject,
  nodePointerAreaPaint,
  onNodeClick,
  onNodeHover,
  cooldownTicks = 60,
  legend = null,
}) {
  const fgRef = useRef();
  const containerRef = useRef(null);
  const [dims, setDims] = useState({ width: 600, height });
  const lastZoomRef = useRef(0);
  // Canvas fillStyle can't resolve "var(--neutral-950)" itself the way a
  // CSS property can, so when no literal backgroundColor override is
  // passed, resolve the current --neutral-950 value from the DOM once on
  // mount (single dark theme, so the value never changes at runtime).
  const [resolvedBg, setResolvedBg] = useState("#0a0a0a");
  useEffect(() => {
    if (backgroundColor) return;
    const v = getComputedStyle(document.documentElement).getPropertyValue("--neutral-950").trim();
    if (v) setResolvedBg(v);
  }, [backgroundColor]);

  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    const observer = new ResizeObserver(([entry]) => {
      setDims({ width: entry.contentRect.width, height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [height]);

  // See FIX comment above the component docstring: this reference must
  // stay stable across renders that don't actually change the node/link
  // data (e.g. hover-driven re-renders), or react-force-graph reheats
  // the simulation and already-settled nodes go flying.
  const graphData = useMemo(() => ({ nodes, links }), [nodes, links]);

  return (
    <div ref={containerRef} className="relative rounded-lg border border-[var(--neutral-800)] overflow-hidden">
      <ForceGraph2D
        ref={fgRef}
        graphData={graphData}
        width={dims.width}
        height={dims.height}
        backgroundColor={backgroundColor || resolvedBg}
        linkColor={linkColor}
        linkCurvature={linkCurvature}
        linkDirectionalArrowLength={linkDirectionalArrowLength}
        linkDirectionalArrowRelPos={linkDirectionalArrowRelPos}
        linkWidth={linkWidth}
        linkLabel={linkLabel}
        cooldownTicks={cooldownTicks}
        onEngineStop={() => {
          // Debounced re-frame: only nudge the view outward as new nodes
          // arrive rather than fully re-centering on every single event,
          // so the camera doesn't jump around mid-run/mid-edit.
          const now = Date.now();
          if (now - lastZoomRef.current > 250) {
            lastZoomRef.current = now;
            fgRef.current?.zoomToFit(400, 60);
          }
        }}
        onNodeClick={onNodeClick}
        onNodeHover={onNodeHover}
        nodeLabel={nodeLabel}
        nodeCanvasObject={nodeCanvasObject}
        nodePointerAreaPaint={nodePointerAreaPaint}
      />
      {legend && (
        <div className="absolute bottom-1 right-1 flex flex-wrap items-center gap-2 rounded bg-black/60 px-2 py-1 text-[10px] text-[var(--neutral-400)] max-w-[90%]">
          {legend}
        </div>
      )}
    </div>
  );
}
