"use client";
import { useMemo } from "react";

// study_guide_writer (a generic_worker role, per §4.5) emits the same
// '# Title' / '## heading' Markdown grammar as flashcard_writer and
// quiz_writer, plus ordinary paragraphs, bullet lists, and numbered
// lists in section bodies. Unlike FlashcardFlipper/QuizRunner this
// view has no interactive state — it's a straight read-through render,
// so a small hand-rolled block parser is enough; no markdown library
// dependency is introduced here.
function parseBlocks(markdown) {
  const lines = (markdown || "").split("\n");
  const blocks = [];
  let listBuffer = null;

  function flushList() {
    if (listBuffer) {
      blocks.push(listBuffer);
      listBuffer = null;
    }
  }

  for (const raw of lines) {
    const line = raw.trimEnd();
    const h1 = /^#\s+(.*)$/.exec(line);
    const h2 = /^##\s*(.*)$/.exec(line);
    const h3 = /^###\s*(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);

    if (h1) {
      flushList();
      blocks.push({ type: "h1", text: h1[1].trim() });
    } else if (h2) {
      flushList();
      blocks.push({ type: "h2", text: h2[1].trim() });
    } else if (h3) {
      flushList();
      blocks.push({ type: "h3", text: h3[1].trim() });
    } else if (bullet) {
      if (!listBuffer || listBuffer.ordered) { flushList(); listBuffer = { type: "list", ordered: false, items: [] }; }
      listBuffer.items.push(bullet[1].trim());
    } else if (numbered) {
      if (!listBuffer || !listBuffer.ordered) { flushList(); listBuffer = { type: "list", ordered: true, items: [] }; }
      listBuffer.items.push(numbered[1].trim());
    } else if (line.trim().length === 0) {
      flushList();
    } else {
      flushList();
      blocks.push({ type: "p", text: line.trim() });
    }
  }
  flushList();
  return blocks;
}

export default function StudyGuideViewer({ markdownText }) {
  const blocks = useMemo(() => parseBlocks(markdownText), [markdownText]);

  if (blocks.length === 0) {
    return <p className="text-xs text-[var(--neutral-500)]">Couldn't parse a study guide from this text.</p>;
  }

  return (
    <div className="space-y-3 max-w-none">
      {blocks.map((b, i) => {
        if (b.type === "h1") {
          return (
            <h2 key={i} className="text-sm font-semibold text-[var(--neutral-100)] pb-1 border-b border-[var(--neutral-800)]">
              {b.text}
            </h2>
          );
        }
        if (b.type === "h2") {
          return (
            <h3 key={i} className="text-xs font-medium text-[var(--neutral-200)] pt-2">
              {b.text}
            </h3>
          );
        }
        if (b.type === "h3") {
          return (
            <h4 key={i} className="text-[11px] font-medium text-[var(--neutral-400)] uppercase tracking-wide pt-1">
              {b.text}
            </h4>
          );
        }
        if (b.type === "list") {
          const Tag = b.ordered ? "ol" : "ul";
          return (
            <Tag key={i} className={`text-xs text-[var(--neutral-300)] space-y-1 pl-4 ${b.ordered ? "list-decimal" : "list-disc"}`}>
              {b.items.map((item, j) => <li key={j}>{item}</li>)}
            </Tag>
          );
        }
        return (
          <p key={i} className="text-xs text-[var(--neutral-300)] leading-relaxed whitespace-pre-wrap">
            {b.text}
          </p>
        );
      })}
    </div>
  );
}
