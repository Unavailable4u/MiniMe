"use client";
import { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";

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
export default function MermaidDiagram({ mermaidText, onNodeClick }) {
  const ref = useRef(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    if (ref.current && mermaidText) {
      const renderId = `mermaid-diagram-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      mermaid.render(renderId, mermaidText)
        .then(({ svg }) => {
          if (!cancelled && ref.current) {
            ref.current.innerHTML = svg;
            // §4.7 — wire click handling onto the rendered nodes. Mermaid
            // gives every node group a `.node` class regardless of
            // diagram type (mindmap/flowchart/graph), so this one
            // delegated listener covers all of them without needing to
            // know which diagram type was actually rendered. The node's
            // own visible label text is the closest thing to a stable
            // identifier available client-side (mermaid doesn't expose
            // the author's original node id in the rendered DOM for
            // every diagram type) -- good enough to hand off to a
            // sub-chat prompt ("tell me more about <label>"), which is
            // all onNodeClick is for.
            if (onNodeClick) {
              const nodeEls = ref.current.querySelectorAll(".node, .mindmap-node");
              nodeEls.forEach((el) => {
                el.style.cursor = "pointer";
                el.addEventListener("click", () => {
                  const label = el.querySelector("text, .nodeLabel")?.textContent?.trim() || el.textContent?.trim();
                  if (label) onNodeClick(label);
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
  }, [mermaidText, onNodeClick]);

  if (failed) {
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

  return <div ref={ref} />;
}
