"use client";
import { useMemo, useRef } from "react";
import Markdown from "../Markdown";

function extractHeadings(markdown) {
  return (markdown || "")
    .split("\n")
    .map((l) => /^##\s+(.*)$/.exec(l.trimEnd())?.[1])
    .filter(Boolean);
}

export default function StudyGuideViewer({ markdownText, onCitationClick }) {
  const headings = useMemo(() => extractHeadings(markdownText), [markdownText]);
  const bodyRef = useRef(null);

  function jumpTo(heading) {
    const el = bodyRef.current?.querySelectorAll("h2, h3");
    if (!el) return;
    for (const node of el) {
      if (node.textContent?.trim() === heading) {
        node.scrollIntoView({ behavior: "smooth", block: "start" });
        break;
      }
    }
  }

  return (
    <div className="flex gap-4">
      {headings.length > 1 && (
        <div className="w-40 shrink-0 space-y-1 sticky top-0 self-start">
          <div className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-1">On this page</div>
          {headings.map((h, i) => (
            <button
              key={i}
              onClick={() => jumpTo(h)}
              className="block text-left text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)] truncate w-full"
            >
              {h}
            </button>
          ))}
        </div>
      )}
      <div ref={bodyRef} className="flex-1 min-w-0">
        <Markdown onCitationClick={onCitationClick}>{markdownText}</Markdown>
      </div>
    </div>
  );
}
