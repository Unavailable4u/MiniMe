"use client";
import { useEffect, useRef, useState, memo } from "react";
import mermaid from "mermaid";
import { ZoomIn, ZoomOut, Maximize2, Download } from "lucide-react";

// Bug fix: the diagrams behind this component come straight from an LLM
// (generic_worker.py's MARKDOWN_INSTRUCTION asks it to write real Mermaid
// syntax for flowcharts/mindmaps/etc.), so invalid syntax is a real,
// expected case, not a corner case. By default, when mermaid.render() hits
// a parse error, it doesn't just reject the promise -- it also inserts its
// own "Syntax error in text / mermaid version x.y.z" bomb-icon SVG
// directly into the document (not into this component's own ref), which is
// exactly the stray error blocks stacking up outside the chat UI.
// `suppressErrorRendering: true` (supported since mermaid ~10.3, and this
// project is on 11.x) turns that off and makes render() simply reject like
// any other failed async call, so the .catch() below is actually in
// control of what the user sees.
mermaid.initialize({ startOnLoad: false, theme: "dark", suppressErrorRendering: true });

// §4.7 — mind-map nodes only get the "click me" affordance when a
// handler is actually wired up (e.g. the Notebooks tab's mind map), so
// every OTHER caller of this component (structure-plan views, ordinary
// chat-rendered diagrams) keeps today's static, non-interactive look
// with zero behavior change.
//
// hideSourceOnFail (NEW — bug #6a fix): defaults to false, so every
// existing caller keeps today's "fall back to showing the raw source"
// behavior unchanged (genuinely useful for a structure-plan diagram, or
// a ```mermaid block inside a normal chat message, where seeing the
// source is real context). MindMapView passes hideSourceOnFail={true}
// -- per the "Mind Map is a pure visualization surface, never show raw
// source/code" steer, this is the last-resort case where the model's
// fenced block matched (so agents/mind_mapper.py already accepted it as
// "mermaid") but mermaid.render() still rejects it, e.g. a syntax typo
// inside otherwise-valid-looking flowchart syntax.
//
// completedSteps / onToggleStep / stepTypes / currentStepId (NEW — bug
// audit §7 follow-up, "click a step to check it off"): a second,
// mutually-exclusive interaction mode alongside onNodeClick. Mind Map's
// click opens a sub-chat about the clicked node; a workflow diagram's
// click needs to toggle that step's completion instead -- reusing the
// same click semantics for both would make the "same" gesture mean two
// different things depending on which tab you're in. A given
// MermaidDiagram instance is therefore either a "sub-chat" diagram
// (onNodeClick set) or a "checklist" diagram (completedSteps set),
// never both -- WorkflowCard below is the only caller of the latter.
//
// completedSteps: Set<string> of step ids (e.g. "S1") that are done.
// Lifted up into the parent card's own state (not local to this
// component) since the parent is what persists/resets it.
// onToggleStep(id): called with the recovered step id instead of
// onNodeClick when completedSteps is present.
// stepTypes: optional { [id]: "step" | "decision" } -- decision nodes
// (branch points) aren't something you "complete," so they don't get
// the click-to-toggle affordance; ids missing from this map default to
// "step".
// currentStepId: optional id of the "next thing to do" (first step not
// yet in completedSteps) -- gets a distinct highlight, computed by the
// parent since it needs the ordered steps list to know "first".
//
// showControls / maxHeight / exportFilename (NEW — guide §7 refinements
// #5/#6, "zoom/pan for bigger diagrams" + "export for offline study").
// Opt-in, same reasoning as hideSourceOnFail above: existing callers
// (PlanTab's structure-plan diagrams, Markdown.jsx's inline chat
// diagrams) keep today's plain static look with zero behavior change
// unless they explicitly ask for the toolbar. MindMapView and
// WorkflowCard both pass showControls -- a whole-notebook mind map or a
// dozen-plus-step workflow is exactly the "overflow a fixed-height card"
// case the guide calls out. No heavy pan/zoom library: panning is just
// the scroll container's native scrollbars, zoom is a couple of buttons
// that resize the rendered SVG directly.
// Rendering audit, Bug 5: a diagram's labels can carry a literal two-
// character `\"` or `\n` sequence (as opposed to a real quote/newline
// character) if they passed through a JSON-encode step somewhere upstream
// that was never fully decoded back out before landing here -- Mermaid's
// parser has no idea what to do with a bare backslash and treats it as a
// syntax error. This is a defensive normalization pass, not a fix for the
// root cause (that's the server-side label sanitization in
// agents/structure_architect.py's _sanitize_mermaid_label(), reused by
// architecture_diagrammer.py and schema_diagrammer.py) -- it just makes
// sure this component doesn't hand mermaid.render() something it can
// already tell is broken, regardless of which upstream caller produced it.
function normalizeMermaidText(text) {
  if (!text) return text;
  return text.replace(/\\"/g, "'").replace(/\\n/g, " ");
}

function MermaidDiagram({
  mermaidText,
  onNodeClick,
  hideSourceOnFail = false,
  completedSteps = null,
  onToggleStep = null,
  stepTypes = null,
  currentStepId = null,
  showControls = false,
  maxHeight = 480,
  exportFilename = "diagram",
}) {
  const ref = useRef(null);
  const [failed, setFailed] = useState(false);
  // BUGFIX (rendering audit): callers like MindMapView pass onNodeClick as a
  // brand-new inline arrow function on every render (`onNodeClick={(label) =>
  // onOpenSubChat(...)}`). That used to sit directly in the main effect's
  // dependency array below, so mermaid.render() re-ran on *every* re-render
  // of the parent -- not just when mermaidText actually changed -- causing
  // visible flicker and giving an already-known-flaky LLM-authored diagram
  // extra, unnecessary chances to hit its transient render-failure path.
  // Stashing the latest handler in a ref and reading `onNodeClickRef.current`
  // inside the effect keeps the click handler always current without making
  // it a re-render trigger.
  const onNodeClickRef = useRef(onNodeClick);
  useEffect(() => { onNodeClickRef.current = onNodeClick; }, [onNodeClick]);
  const [zoom, setZoom] = useState(1);
  const [baseSize, setBaseSize] = useState(null); // { w, h } natural (unzoomed) pixel size, read from the rendered SVG's viewBox
  const checklistMode = !!completedSteps;

  // Recovers the author's stable node id (e.g. "S1") from mermaid's
  // rendered DOM id for that node group (e.g. "flowchart-S1-3"). Only
  // meaningful for diagrams we control the Mermaid source of --
  // agents/workflow_suggester.py emits S1/S2/... as the literal Mermaid
  // node ids for exactly this reason (guide's refinement #1: matching on
  // rendered label text breaks when two steps share the same wording,
  // e.g. "Check connections" appearing twice in one procedure).
  function recoverNodeId(el) {
    const match = /^flowchart-(.+?)-\d+$/.exec(el.id || "");
    return match ? match[1] : null;
  }

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    if (ref.current && mermaidText) {
      const renderId = `mermaid-diagram-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      mermaid.render(renderId, normalizeMermaidText(mermaidText))
        .then(({ svg }) => {
          if (!cancelled && ref.current) {
            ref.current.innerHTML = svg;
            setZoom(1);
            // §7 refinement #5 — natural (unzoomed) pixel size, read from
            // mermaid's own viewBox so the zoom buttons and PNG export
            // both have a stable "100%" to scale from, independent of
            // whatever CSS is currently squeezing the SVG to fit its
            // container.
            const svgEl = ref.current.querySelector("svg");
            const viewBox = svgEl?.getAttribute("viewBox");
            if (viewBox) {
              const parts = viewBox.split(/\s+/).map(Number);
              if (parts.length === 4 && parts[2] > 0 && parts[3] > 0) {
                setBaseSize({ w: parts[2], h: parts[3] });
              }
            } else if (svgEl?.getBBox) {
              const bbox = svgEl.getBBox();
              if (bbox.width > 0 && bbox.height > 0) setBaseSize({ w: bbox.width, h: bbox.height });
            }
            const nodeEls = ref.current.querySelectorAll(".node, .mindmap-node");
            if (checklistMode) {
              // §7 follow-up — checklist mode: wire toggle clicks only
              // onto nodes whose recovered id resolves to a "step" (not
              // "decision"), and only when we could recover a stable id
              // at all (a diagram this component didn't expect to be in
              // checklist mode for would have no recoverable ids and
              // just silently gets no click affordance, rather than
              // crashing).
              nodeEls.forEach((el) => {
                const id = recoverNodeId(el);
                if (!id) return;
                const type = stepTypes?.[id] || "step";
                if (type === "decision") return;
                el.style.cursor = "pointer";
                el.addEventListener("click", () => onToggleStep?.(id));
              });
            } else if (onNodeClickRef.current) {
              // §4.7 — sub-chat mode: Mermaid gives every node group a
              // `.node` class regardless of diagram type
              // (mindmap/flowchart/graph), so this one delegated
              // listener covers all of them without needing to know
              // which diagram type was actually rendered. The node's
              // own visible label text is the closest thing to a stable
              // identifier available for diagrams we don't control the
              // source of (Mind Map's source is LLM-authored, no
              // guaranteed node ids) -- good enough to hand off to a
              // sub-chat prompt ("tell me more about <label>").
              nodeEls.forEach((el) => {
                el.style.cursor = "pointer";
                el.addEventListener("click", () => {
                  const label = el.querySelector("text, .nodeLabel")?.textContent?.trim() || el.textContent?.trim();
                  if (label) onNodeClickRef.current?.(label);
                });
              });
            }
          }
        })
        .catch((err) => {
          console.error("Mermaid render failed:", err);
          if (!cancelled) setFailed(true);
          // Belt-and-braces: some mermaid versions still append a stray
          // `#renderId` error node to the document body on failure even
          // with suppressErrorRendering set. Clean it up if present so it
          // can never leak into the page layout.
          document.getElementById(renderId)?.remove();
        });
    }
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- checklistMode/stepTypes/onToggleStep intentionally excluded: they don't change the *rendered* SVG, only the second effect below (re-running mermaid.render() on every checkbox click would re-layout the whole diagram and flicker). onNodeClick is read via onNodeClickRef (see BUGFIX above) specifically so it does NOT belong here either -- a fresh inline-function identity from the caller shouldn't re-trigger mermaid.render().
  }, [mermaidText]);

  // §7 follow-up — a SECOND effect, keyed on completedSteps/currentStepId
  // rather than mermaidText, so toggling one step's visual state never
  // re-runs mermaid.render(): it just flips a CSS class (and a checkmark
  // prefix on the label) on the already-rendered SVG node groups whose
  // recovered id is in the set. No re-layout, no flicker.
  useEffect(() => {
    if (!checklistMode || !ref.current) return;
    const nodeEls = ref.current.querySelectorAll(".node, .mindmap-node");
    nodeEls.forEach((el) => {
      const id = recoverNodeId(el);
      if (!id) return;
      const done = completedSteps.has(id);
      el.classList.toggle("step-done", done);
      el.classList.toggle("step-current", !done && id === currentStepId);
      // Non-color "done" signal (guide refinement #4): prefix the
      // rendered label with a checkmark rather than relying on
      // strikethrough/opacity alone, which can be hard to spot at small
      // diagram sizes or for colorblind users. Original text is cached
      // on the element the first time so it can be restored on undo.
      const textEl = el.querySelector("text, .nodeLabel");
      if (textEl) {
        if (!textEl.dataset.origLabel) textEl.dataset.origLabel = textEl.textContent || "";
        textEl.textContent = done ? `✓ ${textEl.dataset.origLabel}` : textEl.dataset.origLabel;
      }
    });
  }, [completedSteps, currentStepId, checklistMode]);

  // §7 refinement #5 — applies the current zoom level directly to the
  // rendered SVG's pixel size (relative to its natural baseSize), rather
  // than a CSS `transform: scale()`. A transform doesn't affect layout
  // size, so the scroll container wouldn't pick up bigger scrollbars at
  // higher zoom -- explicit width/height does, which is what makes
  // "zoom in, then scroll/drag to pan" actually work with nothing more
  // than the container's native overflow.
  useEffect(() => {
    if (!showControls || !ref.current || !baseSize) return;
    const svgEl = ref.current.querySelector("svg");
    if (!svgEl) return;
    svgEl.style.maxWidth = "none"; // mermaid's default max-width:100% would otherwise fight an explicit zoomed width
    svgEl.style.width = `${baseSize.w * zoom}px`;
    svgEl.style.height = `${baseSize.h * zoom}px`;
  }, [zoom, baseSize, showControls]);

  // §7 refinement #6 — "export as image" for offline study. Serializes
  // the already-rendered SVG to a standalone document (at its natural,
  // unzoomed size, so the export is consistent regardless of whatever
  // zoom level the user happens to be looking at), rasterizes it onto a
  // canvas at 2x for a crisp result, and triggers a PNG download.
  function handleExportPng() {
    const svgEl = ref.current?.querySelector("svg");
    if (!svgEl || !baseSize) return;
    const clone = svgEl.cloneNode(true);

    // The .step-done/.step-current highlighting (globals.css) only
    // applies inside this page's own document -- a serialized standalone
    // SVG has no access to that stylesheet, so without this the
    // exported image would silently lose the strikethrough/dim/glow a
    // checklist diagram currently shows on screen. Walk the live
    // (already-styled) tree and the clone in lockstep and bake the
    // relevant computed styles in as inline styles.
    const liveAll = [...svgEl.querySelectorAll("*")];
    const cloneAll = clone.querySelectorAll("*");
    liveAll.forEach((liveEl, i) => {
      if (!liveEl.classList?.contains("step-done") && !liveEl.classList?.contains("step-current")) return;
      const cloneEl = cloneAll[i];
      if (!cloneEl) return;
      const computed = getComputedStyle(liveEl);
      cloneEl.style.opacity = computed.opacity;
      const liveText = liveEl.querySelector("text, .nodeLabel");
      const cloneText = cloneEl.querySelector("text, .nodeLabel");
      if (liveText && cloneText) cloneText.style.textDecoration = getComputedStyle(liveText).textDecoration;
      liveEl.querySelectorAll("rect, polygon, circle, ellipse").forEach((liveShape, j) => {
        const cloneShape = cloneEl.querySelectorAll("rect, polygon, circle, ellipse")[j];
        if (!cloneShape) return;
        const shapeComputed = getComputedStyle(liveShape);
        cloneShape.style.stroke = shapeComputed.stroke;
        cloneShape.style.strokeWidth = shapeComputed.strokeWidth;
      });
    });

    clone.setAttribute("width", baseSize.w);
    clone.setAttribute("height", baseSize.h);
    clone.style.width = "";
    clone.style.height = "";
    clone.style.maxWidth = "";

    const svgString = new XMLSerializer().serializeToString(clone);
    const svgUrl = URL.createObjectURL(new Blob([svgString], { type: "image/svg+xml;charset=utf-8" }));
    const img = new Image();
    img.onload = () => {
      const scale = 2;
      const canvas = document.createElement("canvas");
      canvas.width = baseSize.w * scale;
      canvas.height = baseSize.h * scale;
      const ctx = canvas.getContext("2d");
      // Dark fill so a diagram with a transparent background doesn't
      // turn into near-invisible light text on white when opened
      // outside the app.
      ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--neutral-950")?.trim() || "#0a0a0f";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(svgUrl);
      canvas.toBlob((blob) => {
        if (!blob) return;
        const safeName = (exportFilename || "diagram").replace(/[^a-z0-9\-_ ]/gi, "").trim() || "diagram";
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `${safeName}.png`;
        link.click();
        URL.revokeObjectURL(link.href);
      });
    };
    img.onerror = () => URL.revokeObjectURL(svgUrl);
    img.src = svgUrl;
  }

  if (failed) {
    if (hideSourceOnFail) {
      // NEW — bug #6a fix: no code dump, just a short line — this is a
      // last-resort case (see the prop comment above), rare enough now
      // that mind_mapper.py retries once server-side before ever
      // handing back something that reaches this component at all.
      return (
        <div className="text-[11px] text-[var(--neutral-500)]">
          Couldn't render this as a diagram — try Regenerate.
        </div>
      );
    }
    // Fall back to the raw diagram source instead of a blank/broken box,
    // so the content isn't lost -- just not rendered as a graphic. Styled
    // to match the plain fenced-code-block look Markdown.jsx already uses
    // for non-mermaid code, since this is effectively the same case.
    return (
      <div className="text-[11px] text-[var(--neutral-500)] space-y-1.5">
        <div>Couldn't render this diagram — showing the raw source instead:</div>
        <pre className="overflow-x-auto p-3 text-xs bg-black/30 rounded-md border border-[var(--neutral-800)]">
          <code>{mermaidText}</code>
        </pre>
      </div>
    );
  }

  return (
    <div className={showControls ? "relative" : undefined}>
      {showControls && (
        <div className="absolute top-1.5 right-1.5 z-10 flex items-center gap-0.5 bg-black/60 backdrop-blur-sm rounded-md border border-[var(--neutral-800)] p-0.5">
          <button
            onClick={() => setZoom((z) => Math.max(0.4, +(z - 0.2).toFixed(2)))}
            title="Zoom out"
            className="p-1 rounded text-[var(--neutral-400)] hover:text-[var(--neutral-100)] hover:bg-white/5"
          >
            <ZoomOut size={12} />
          </button>
          <button
            onClick={() => setZoom(1)}
            title="Reset zoom"
            className="p-1 rounded text-[var(--neutral-400)] hover:text-[var(--neutral-100)] hover:bg-white/5"
          >
            <Maximize2 size={12} />
          </button>
          <button
            onClick={() => setZoom((z) => Math.min(3, +(z + 0.2).toFixed(2)))}
            title="Zoom in"
            className="p-1 rounded text-[var(--neutral-400)] hover:text-[var(--neutral-100)] hover:bg-white/5"
          >
            <ZoomIn size={12} />
          </button>
          <button
            onClick={handleExportPng}
            title="Download as image"
            className="p-1 rounded text-[var(--neutral-400)] hover:text-[var(--cyber-cyan)] hover:bg-white/5"
          >
            <Download size={12} />
          </button>
        </div>
      )}
      <div ref={ref} className={showControls ? "overflow-auto" : undefined} style={showControls ? { maxHeight } : undefined} />
    </div>
  );
}

// Item 6 (perf audit): mermaid.render() is the expensive part of this
// component (a full re-parse + re-layout of the diagram), so skipping
// re-renders on unchanged props matters more here than for most leaf
// components. Helps every caller that passes stable props as-is (PlanTab's
// structure-plan diagrams, Markdown.jsx's inline chat diagrams). Doesn't
// help MindMapView's onNodeClick specifically -- see that prop's own
// BUGFIX comment above, a fresh inline function every render defeats
// memo's shallow prop comparison regardless -- but memoizing here is still
// correct and free, and sets this component up to benefit the moment that
// caller is updated to pass a stable callback too.
export default memo(MermaidDiagram);
