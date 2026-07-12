"use client";
import { useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, RotateCw } from "lucide-react";

// flashcard_writer (a generic_worker role, per §4.5) is asked for the
// same '# Title' / '## heading' Markdown grammar every other notes-
// domain generator role uses (graph/adapters.py's markdown_text_to_
// artifact() shape) — each '## ' heading is a card front, its section
// content is the back.
function parseFlashcards(markdown) {
  const lines = (markdown || "").split("\n");
  let title = "Flashcards";
  const cards = [];
  let current = null;

  function flush() {
    if (current) {
      current.back = current.backLines.join("\n").trim();
      delete current.backLines;
      cards.push(current);
    }
  }

  for (const raw of lines) {
    const line = raw.trimEnd();
    const h1 = /^#\s+(.*)$/.exec(line);
    const h2 = /^##\s*(.*)$/.exec(line);
    if (h1) {
      title = h1[1].trim();
    } else if (h2) {
      flush();
      current = { front: h2[1].trim(), backLines: [] };
    } else if (current) {
      current.backLines.push(raw);
    }
  }
  flush();
  return { title, cards };
}

export default function FlashcardFlipper({ markdownText }) {
  const { title, cards } = useMemo(() => parseFlashcards(markdownText), [markdownText]);
  const [index, setIndex] = useState(0);
  const [flipped, setFlipped] = useState(false);

  if (cards.length === 0) {
    return <p className="text-xs text-[var(--neutral-500)]">Couldn't parse any cards from this set.</p>;
  }

  const card = cards[index];

  function go(delta) {
    setFlipped(false);
    setIndex((i) => Math.max(0, Math.min(cards.length - 1, i + delta)));
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-[var(--neutral-200)]">{title}</h3>
        <span className="text-[11px] text-[var(--neutral-500)]">{index + 1} / {cards.length}</span>
      </div>

      <div
        onClick={() => setFlipped((f) => !f)}
        className="min-h-[160px] flex items-center justify-center text-center rounded-lg border border-[var(--neutral-800)] bg-black/20 px-6 py-8 cursor-pointer select-none hover:border-[var(--neutral-700)]"
      >
        <div>
          <div className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2">
            {flipped ? "Back" : "Front"}
          </div>
          <div className="text-sm text-[var(--neutral-200)] whitespace-pre-wrap">
            {flipped ? card.back : card.front}
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between">
        <button onClick={() => go(-1)} disabled={index === 0} className="flex items-center gap-1 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] disabled:opacity-30">
          <ChevronLeft size={14} /> Prev
        </button>
        <button onClick={() => setFlipped((f) => !f)} className="flex items-center gap-1 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)]">
          <RotateCw size={12} /> Flip
        </button>
        <button onClick={() => go(1)} disabled={index === cards.length - 1} className="flex items-center gap-1 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] disabled:opacity-30">
          Next <ChevronRight size={14} />
        </button>
      </div>
    </div>
  );
}
