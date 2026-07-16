"use client";
import { useMemo, useState } from "react";
import { useSession } from "../../context/SessionContext";
import { ChevronLeft, ChevronRight, CheckCircle2, XCircle, RotateCcw } from "lucide-react";

// quiz_writer (a generic_worker role, per §4.5) emits the same
// '# Title' / '## heading' Markdown grammar as flashcard_writer.
// Convention used here: each '## ' heading is the question; within
// its body, a line starting with '- [ ]' or '- [x]' is an option.
//
// IMPORTANT: this parser is DISPLAY ONLY. It deliberately does not read
// which option is marked '[x]', so the answer key never reaches the
// browser before grading. Grading always happens server-side via
// POST /api/notes/study/quiz/grade (or .../attempts, which grades AND
// persists in one call) — see eo/quiz_progress.py for the source of
// truth. If a question has no '- [ ]' options at all, it's treated as
// a free-text question whose answer is only shown after everything is
// submitted (the server always counts these as correct).
function parseQuizForDisplay(markdown) {
  const lines = (markdown || "").split("\n");
  let title = "Quiz";
  const questions = [];
  let current = null;

  function flush() {
    if (current) questions.push(current);
  }

  for (const raw of lines) {
    const line = raw.trimEnd();
    const h1 = /^#\s+(.*)$/.exec(line);
    const h2 = /^##\s*(.*)$/.exec(line);
    const option = /^\s*[-*]\s*\[( |x|X)\]\s*(.*)$/.exec(line);

    if (h1) {
      title = h1[1].trim();
    } else if (h2) {
      flush();
      current = { question: h2[1].trim(), options: [] };
    } else if (current && option) {
      current.options.push(option[2].trim());
    }
  }
  flush();

  return { title, questions };
}

export default function QuizRunner({ quizText, workspaceId, quizNodeId }) {
  const { gradeQuiz, recordQuizAttempt, fetchMissedQuestions } = useSession();
  const { title, questions } = useMemo(() => parseQuizForDisplay(quizText), [quizText]);

  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState({}); // qIndex -> optIndex
  const [result, setResult] = useState(null); // server grading response, once submitted
  const [submitting, setSubmitting] = useState(false);
  const [missedOnly, setMissedOnly] = useState(null); // Set of question strings, or null = show all

  const visibleIndices = questions
    .map((_, i) => i)
    .filter((i) => !missedOnly || missedOnly.has(questions[i].question));

  if (questions.length === 0) {
    return <p className="text-xs text-[var(--neutral-500)]">Couldn't parse any questions from this quiz.</p>;
  }

  const visPos = visibleIndices.indexOf(index);
  const qIndex = visibleIndices[visPos] ?? visibleIndices[0];
  const q = questions[qIndex];
  const hasOptions = q.options.length > 0;
  const selected = answers[qIndex] ?? null;
  const isLast = visPos === visibleIndices.length - 1;

  function choose(optIdx) {
    if (result) return;
    setAnswers((prev) => ({ ...prev, [qIndex]: optIdx }));
  }

  function goPrev() {
    if (visPos > 0) setIndex(visibleIndices[visPos - 1]);
  }

  function goNext() {
    if (!isLast) setIndex(visibleIndices[visPos + 1]);
  }

  async function submitAll() {
    setSubmitting(true);
    // Server-side grading only ever sees option indices, one per question,
    // in question order — the answer key itself never touches the client.
    const orderedAnswers = questions.map((_, i) => (answers[i] ?? null));
    try {
      const graded =
        workspaceId && quizNodeId
          ? await recordQuizAttempt(workspaceId, quizNodeId, quizText, orderedAnswers)
          : await gradeQuiz(quizText, orderedAnswers);
      setResult(graded);
    } finally {
      setSubmitting(false);
    }
  }

  async function retakeMissedOnly() {
    if (!workspaceId || !quizNodeId) return;
    const missed = await fetchMissedQuestions(workspaceId, quizNodeId);
    const missedSet = new Set(missed.map((m) => m.question));
    setMissedOnly(missedSet);
    setResult(null);
    setAnswers({});
    const firstMissed = questions.findIndex((qq) => missedSet.has(qq.question));
    setIndex(firstMissed >= 0 ? firstMissed : 0);
  }

  function restart() {
    setIndex(0);
    setAnswers({});
    setResult(null);
    setMissedOnly(null);
  }

  if (result) {
    const score = result.score ?? 0;
    const total = result.total ?? questions.length;
    const percent = result.percent ?? Math.round((score / Math.max(total, 1)) * 100);
    return (
      <div className="space-y-4">
        <div className="space-y-3 text-center">
          <h3 className="text-sm font-medium text-[var(--neutral-200)]">{title} — Results</h3>
          <div className="text-2xl font-semibold text-[var(--neutral-100)]">
            {score} / {total}
          </div>
          <div className="text-xs text-[var(--neutral-500)]">{percent}%</div>
        </div>

        <div className="space-y-3">
          {questions.map((qq, i) => {
            const r = result.results?.[i];
            if (!hasOptionsFor(qq)) return null;
            return (
              <div key={i} className="rounded-lg border border-[var(--neutral-800)] p-3 space-y-1.5">
                <div className="text-xs font-medium text-[var(--neutral-200)]">{qq.question}</div>
                <div className="space-y-1">
                  {qq.options.map((opt, oi) => {
                    const wasSelected = answers[i] === oi;
                    const showCorrect = r && oi === r.correct_index;
                    const showWrongPick = r && wasSelected && !r.is_correct;
                    return (
                      <div
                        key={oi}
                        className={`flex items-center gap-2 text-xs rounded px-2 py-1.5 border ${
                          showCorrect
                            ? "border-green-700 bg-green-950/40 text-green-300"
                            : showWrongPick
                            ? "border-red-700 bg-red-950/40 text-red-300"
                            : "border-[var(--neutral-800)] text-[var(--neutral-400)]"
                        }`}
                      >
                        <span>{opt}</span>
                        {showCorrect && <CheckCircle2 size={12} className="ml-auto shrink-0" />}
                        {showWrongPick && <XCircle size={12} className="ml-auto shrink-0" />}
                      </div>
                    );
                  })}
                </div>
                {r?.explanation && (
                  <p className="text-[11px] text-[var(--neutral-500)] pt-1 border-t border-[var(--neutral-900)]">
                    {r.explanation}
                  </p>
                )}
              </div>
            );
          })}
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={restart}
            className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium"
          >
            Retake quiz
          </button>
          {workspaceId && quizNodeId && (
            <button
              onClick={retakeMissedOnly}
              className="flex items-center gap-1 text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
            >
              <RotateCcw size={11} /> Re-run missed only
            </button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-[var(--neutral-200)]">{title}</h3>
        <span className="text-[11px] text-[var(--neutral-500)]">
          {visPos + 1} / {visibleIndices.length}
        </span>
      </div>
      {missedOnly && (
        <p className="text-[11px] text-amber-400">Showing only questions you've most recently missed.</p>
      )}

      <div className="rounded-lg border border-[var(--neutral-800)] bg-black/20 px-4 py-4 space-y-3">
        <div className="text-sm text-[var(--neutral-200)] whitespace-pre-wrap">{q.question}</div>

        {hasOptions ? (
          <div className="space-y-1.5">
            {q.options.map((opt, i) => {
              const isSelected = selected === i;
              return (
                <button
                  key={i}
                  onClick={() => choose(i)}
                  className={`w-full flex items-center justify-between gap-2 text-left text-xs rounded-lg border px-3 py-2 transition-colors ${
                    isSelected
                      ? "border-[var(--cyber-cyan)] text-[var(--neutral-100)]"
                      : "border-[var(--neutral-800)] text-[var(--neutral-300)] hover:border-[var(--neutral-700)]"
                  }`}
                >
                  <span>{opt}</span>
                </button>
              );
            })}
          </div>
        ) : (
          <div className="text-[11px] text-[var(--neutral-500)]">
            Free-text question — the answer will be shown once you submit the full quiz.
          </div>
        )}
      </div>

      <div className="flex items-center justify-between">
        <button
          onClick={goPrev}
          disabled={visPos === 0}
          className="flex items-center gap-1 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] disabled:opacity-30"
        >
          <ChevronLeft size={14} /> Prev
        </button>

        {!isLast ? (
          <button
            onClick={goNext}
            className="flex items-center gap-1 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
          >
            Next <ChevronRight size={14} />
          </button>
        ) : (
          <button
            onClick={submitAll}
            disabled={submitting}
            className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {submitting ? "Grading…" : "Submit quiz"}
          </button>
        )}

        <span className="w-[46px]" />
      </div>
    </div>
  );
}

function hasOptionsFor(q) {
  return q.options.length > 0;
}
