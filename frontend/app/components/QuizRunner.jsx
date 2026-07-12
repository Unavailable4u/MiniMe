"use client";
import { useMemo, useState } from "react";
import { useSession } from "../../context/SessionContext";
import { CheckCircle2, XCircle, RotateCcw } from "lucide-react";

// Client-side mirror of eo/quiz_progress.py's parse_quiz() grammar, for
// DISPLAY only — deliberately does not read which option is marked
// '[x]', so the answer key never reaches the browser before grading.
// Grading itself always happens server-side (POST .../quiz/grade),
// which is the actual source of truth per that module's own docstring.
function parseQuizForDisplay(markdown) {
  const lines = (markdown || "").split("\n");
  let title = "Quiz";
  const questions = [];
  let current = null;

  for (const raw of lines) {
    const line = raw.trimEnd();
    const h1 = /^#\s+(.*)$/.exec(line);
    const h2 = /^##\s*(.*)$/.exec(line);
    const option = /^-\s*\[( |x|X)\]\s+(.*)$/.exec(line);
    if (h1) {
      title = h1[1].trim();
    } else if (h2) {
      if (current) questions.push(current);
      current = { question: h2[1].trim(), options: [] };
    } else if (option && current) {
      current.options.push(option[2].trim());
    }
  }
  if (current) questions.push(current);
  return { title, questions };
}

export default function QuizRunner({ quizText, workspaceId, quizNodeId }) {
  const { gradeQuiz, recordQuizAttempt, fetchMissedQuestions } = useSession();
  const { title, questions } = useMemo(() => parseQuizForDisplay(quizText), [quizText]);
  const [answers, setAnswers] = useState({});
  const [result, setResult] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [missedOnly, setMissedOnly] = useState(null); // Set of question strings, or null = show all

  const visibleIndices = questions
    .map((q, i) => i)
    .filter((i) => !missedOnly || missedOnly.has(questions[i].question));

  function selectAnswer(qIndex, optIndex) {
    setAnswers((prev) => ({ ...prev, [qIndex]: optIndex }));
  }

  async function submit() {
    setSubmitting(true);
    const orderedAnswers = questions.map((_, i) => (answers[i] ?? null));
    try {
      const graded = await gradeQuiz(quizText, orderedAnswers);
      setResult(graded);
      if (workspaceId && quizNodeId) {
        await recordQuizAttempt(workspaceId, quizNodeId, quizText, orderedAnswers);
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function retakeMissedOnly() {
    if (!workspaceId || !quizNodeId) return;
    const missed = await fetchMissedQuestions(workspaceId, quizNodeId);
    setMissedOnly(new Set(missed.map((m) => m.question)));
    setResult(null);
    setAnswers({});
  }

  if (questions.length === 0) {
    return <p className="text-xs text-[var(--neutral-500)]">Couldn't parse any questions from this quiz.</p>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-[var(--neutral-200)]">{title}</h3>
        {workspaceId && quizNodeId && (
          <button
            onClick={retakeMissedOnly}
            className="flex items-center gap-1 text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
          >
            <RotateCcw size={11} /> Re-run missed only
          </button>
        )}
      </div>
      {missedOnly && (
        <p className="text-[11px] text-amber-400">Showing only questions you've most recently missed.</p>
      )}

      {visibleIndices.map((i) => {
        const q = questions[i];
        const r = result?.results?.[i];
        return (
          <div key={i} className="rounded-lg border border-[var(--neutral-800)] p-3 space-y-2">
            <div className="text-xs font-medium text-[var(--neutral-200)]">{q.question}</div>
            <div className="space-y-1">
              {q.options.map((opt, oi) => {
                const selected = answers[i] === oi;
                const showCorrect = r && oi === r.correct_index;
                const showWrongPick = r && selected && !r.is_correct;
                return (
                  <label
                    key={oi}
                    className={`flex items-center gap-2 text-xs rounded px-2 py-1.5 cursor-pointer border ${
                      showCorrect ? "border-green-700 bg-green-950/40 text-green-300"
                      : showWrongPick ? "border-red-700 bg-red-950/40 text-red-300"
                      : selected ? "border-[var(--cyber-cyan)] text-[var(--neutral-200)]"
                      : "border-[var(--neutral-800)] text-[var(--neutral-400)] hover:border-[var(--neutral-700)]"
                    }`}
                  >
                    <input
                      type="radio"
                      name={`q-${i}`}
                      checked={selected}
                      onChange={() => selectAnswer(i, oi)}
                      disabled={!!result}
                    />
                    {opt}
                    {showCorrect && <CheckCircle2 size={12} className="ml-auto shrink-0" />}
                    {showWrongPick && <XCircle size={12} className="ml-auto shrink-0" />}
                  </label>
                );
              })}
            </div>
            {r?.explanation && (
              <p className="text-[11px] text-[var(--neutral-500)] pt-1 border-t border-[var(--neutral-900)]">{r.explanation}</p>
            )}
          </div>
        );
      })}

      {!result ? (
        <button
          onClick={submit}
          disabled={submitting}
          className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
        >
          {submitting ? "Grading…" : "Submit answers"}
        </button>
      ) : (
        <div className="text-xs text-[var(--neutral-300)]">
          Score: <span className="font-medium">{result.score}/{result.total}</span> ({result.percent}%)
        </div>
      )}
    </div>
  );
}
