"use client";
import { useEffect, useRef } from "react";
import mermaid from "mermaid";

mermaid.initialize({ startOnLoad: false, theme: "dark" });

export default function StructurePlanDiagram({ mermaidText }) {
  const ref = useRef(null);

  useEffect(() => {
    let cancelled = false;
    if (ref.current && mermaidText) {
      const renderId = `structure-plan-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      mermaid.render(renderId, mermaidText)
        .then(({ svg }) => {
          if (!cancelled && ref.current) {
            ref.current.innerHTML = svg;
          }
        })
        .catch((err) => {
          console.error("Mermaid render failed:", err);
        });
    }
    return () => { cancelled = true; };
  }, [mermaidText]);

  return <div ref={ref} />;
}