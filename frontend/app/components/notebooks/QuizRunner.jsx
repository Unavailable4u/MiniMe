"use client";
import { useMemo, useState } from "react";
import { useSession } from "../../context/SessionContext";
import { ChevronLeft, ChevronRight, CheckCircle2, XCircle } from "lucide-react";

// quiz_writer (a generic_worker role, per §4.5) emits the same
// '# Title' / '## heading' Markdown grammar as flashcard_writer.
// Convention used here: each '## ' heading is the question; within
// its body, a line starting with '- [x]' or '*[x]*' marks the correct
// option, plain '- ' lines are distractors. If no options are found,
// the whole body is treated as a free-text answer (revealed on submit).
function parseQuiz(markdown) {
  const lines = (markdown || "").split("\n");
  let title = "Quiz";
  const questions = [];
  let current = null;

  function flush() {
    if (current) {
      questions.push(current);
    }
  }

  for (const raw of lines) {
    const line = raw.trimEnd();
    const h1 = /^#\s+(.*)$/.exec(line);
    const h2 = /^##\s*(.*)$/.exec(line);
    const optionMatch = /^\s*[-*]\s*\[( |x|X)\]\s*(.*)$/.exec(line);

    if (h1) {
      title = h1[1].trim();
    } else if (h2) {
      flush();
      current = { question: h2[1].trim(), options: [], answerLines: [] };
    } else if (current && optionMatch) {
      const correct = optionMatch[1].toLowerCase() === "x";
      current.options.push({ text: optionMatch[2].trim(), correct });
    } else if (current) {
      if (line.trim().length > 0) current.answerLines.push(raw);
    }
  }
  flush();

  return {
    title,
    questions: questions.map((q) => ({
      ...q,
      freeTextAnswer: q.answerLines.join("\n").trim(),
    })),
  };
}

export default function QuizRunner({ quizText, workspaceId, quizNodeId }) {
  const { recordQuizAttempt } = useSession();
  const { title, questions } = useMemo(() => parseQuiz(quizText), [quizText]);

  const [index, setIndex] = useState(0);
  const [selected, setSelected] = useState(null);
  const [revealed, setRevealed] = useState(false);
  const [score, setScore] = useState(0);
  const [finished, setFinished] = useState(false);

  if (questions.length === 0) {
    return <p className="text-xs text-[var(--neutral-500)]">Couldn't parse any questions from this quiz.</p>;
  }

  const q = questions[index];
  const hasOptions = q.options.length > 0;

  function choose(optIdx) {
    if (revealed) return;
    setSelected(optIdx);
  }

  function submit() {
    if (revealed) return;
    setRevealed(true);
    const correct = hasOptions ? !!q.options[selected]?.correct : true;
    if (correct) setScore((s) => s + 1);
  }

  function next() {
    if (index === questions.length - 1) {
      setFinished(true);
      if (quizNodeId) {
        recordQuizAttempt?.(workspaceId, quizNodeId, {
          score,
          total: questions.length,
        });
      }
      return;
    }
    setIndex((i) => i + 1);
    setSelected(null);
    setRevealed(false);
  }

  function restart() {
    setIndex(0);
    setSelected(null);
    setRevealed(false);
    setScore(0);
    setFinished(false);
  }

  if (finished) {
    return (
      <div className="space-y-3 text-center">
        <h3 className="text-sm font-medium text-[var(--neutral-200)]">{title} — Results</h3>
        <div className="text-2xl font-semibold text-[var(--neutral-100)]">
          {score} / {questions.length}
        </div>
        <button
          onClick={restart}
          className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium"
        >
          Retake quiz
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-[var(--neutral-200)]">{title}</h3>
        <span className="text-[11px] text-[var(--neutral-500)]">
          {index + 1} / {questions.length} · Score {score}
        </span>
      </div>

      <div className="rounded-lg border border-[var(--neutral-800)] bg-black/20 px-4 py-4 space-y-3">
        <div className="text-sm text-[var(--neutral-200)] whitespace-pre-wrap">{q.question}</div>

        {hasOptions ? (
          <div className="space-y-1.5">
            {q.options.map((opt, i) => {
              const isSelected = selected === i;
              const showCorrect = revealed && opt.correct;
              const showWrong = revealed && isSelected && !opt.correct;
              return (
                <button
                  key={i}
                  onClick={() => choose(i)}
                  disabled={revealed}
                  className={`w-full flex items-center justify-between gap-2 text-left text-xs rounded-lg border px-3 py-2 transition-colors ${
                    showCorrect
                      ? "border-green-500/60 bg-green-500/10 text-green-300"
                      : showWrong
                      ? "border-red-500/60 bg-red-500/10 text-red-300"
                      : isSelected
                      ? "border-[var(--cyber-cyan)] text-[var(--neutral-100)]"
                      : "border-[var(--neutral-800)] text-[var(--neutral-300)] hover:border-[var(--neutral-700)]"
                  } disabled:cursor-default`}
                >
                  <span>{opt.text}</span>
                  {showCorrect && <CheckCircle2 size={13} className="shrink-0" />}
                  {showWrong && <XCircle size={13} className="shrink-0" />}
                </button>
              );
            })}
          </div>
        ) : (
          <div className="space-y-2">
            {revealed && (
              <div className="text-xs text-[var(--neutral-300)] whitespace-pre-wrap border-t border-[var(--neutral-800)] pt-2">
                {q.freeTextAnswer || "No answer provided."}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="flex items-center justify-between">
        <button
          onClick={() => { setIndex((i) => Math.max(0, i - 1)); setSelected(null); setRevealed(false); }}
          disabled={index === 0}
          className="flex items-center gap-1 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] disabled:opacity-30"
        >
          <ChevronLeft size={14} /> Prev
        </button>

        {!revealed ? (
          <button
            onClick={submit}
            disabled={hasOptions && selected === null}
            className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-30"
          >
            {hasOptions ? "Submit answer" : "Reveal answer"}
          </button>
        ) : (
          <button
            onClick={next}
            className="flex items-center gap-1 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
          >
            {index === questions.length - 1 ? "Finish" : "Next"} <ChevronRight size={14} />
          </button>
        )}

        <span className="w-[46px]" />
      </div>
    </div>
  );
}
