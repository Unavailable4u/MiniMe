"""
eo/quiz_progress.py — Study Tools: quiz grading + attempt history (Part 4 §4.5).

Same JSON-store shape as eo/graph_edges.py: a single file, a lock around
read/modify/write, one array of records — quiz attempts aren't semantically
searchable, they're structured history, so they don't belong in Upstash
Vector alongside knowledge_graph.py's nodes.

Grading lives here, not the frontend: quiz_writer's (eo/registry.py, §4.5)
output already carries the correct answer in-band as a GitHub task-list
checkbox ('- [x] correct' / '- [ ] wrong') inside each question's Markdown
section, so re-parsing that same Markdown server-side to grade a submission
is the one source of truth for "what's actually correct" — a frontend that
separately hardcoded answers at render time could drift from the question
text if the artifact is ever re-exported/re-imported.

Reuses agents/importer.py's parse_markdown_text() for the '# Title' /
'## Q<n>: ...' section split (same reuse discipline graph/adapters.py's
markdown_text_to_artifact() already follows for §4.4) — this module adds
only the option/checkbox layer parse_markdown_text() has no reason to know
about.

Place this file at: eo/quiz_progress.py
"""
import os
import sys
import json
import re
import uuid
import threading
from datetime import datetime, timezone

# Same bootstrap as eo/knowledge_graph.py — this module cross-imports
# agents.importer (see parse_quiz() below), so it needs the repo root on
# sys.path the same way that module needs memory.bus/utils.llm_client.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRESS_PATH = os.path.join(BASE_DIR, "data", "study", "_quiz_progress.json")
_lock = threading.Lock()

_OPTION_RE = re.compile(r"^- \[( |x|X)\]\s+(.*)$")
_EXPLANATION_RE = re.compile(r"^Explanation:\s*(.*)$", re.IGNORECASE)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read():
    if not os.path.exists(PROGRESS_PATH):
        return {"attempts": []}
    with open(PROGRESS_PATH) as f:
        return json.load(f)


def _write(data):
    os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)
    with open(PROGRESS_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Parsing quiz_writer's Markdown grammar
# ---------------------------------------------------------------------------

def parse_quiz(quiz_markdown: str) -> dict:
    """Parses quiz_writer's output (eo/registry.py §4.5's grammar) into a
    gradable shape:

        {
            "title": str,
            "questions": [
                {
                    "question": str,          # e.g. "Q1: What is ...?"
                    "options": [str, ...],    # in written order
                    "correct_index": int,     # index into options
                    "explanation": str,
                },
                ...
            ],
        }

    Reuses agents/importer.py's parse_markdown_text() for the outer
    '# Title' / '## heading' -> sections split (the same content each
    question's section holds is what the exporter/importer already
    round-trip losslessly), then walks each section's content for the
    '- [ ]'/'- [x]' option lines and the 'Explanation:' line — the part
    parse_markdown_text() itself has no reason to know about.

    A malformed question (no options, no '[x]' marked, or more than one
    '[x]') is skipped rather than raising, so one bad question from the
    model doesn't take down grading for the rest of the quiz — callers
    that care can check len(questions) against the expected count.
    """
    from agents.importer import parse_markdown_text
    artifact = parse_markdown_text(quiz_markdown, default_title="Untitled Quiz")

    questions = []
    for section in artifact["sections"]:
        options = []
        correct_index = None
        explanation = ""
        for line in (section["content"] or "").split("\n"):
            line = line.strip()
            if not line:
                continue
            m = _OPTION_RE.match(line)
            if m:
                is_correct = m.group(1).lower() == "x"
                options.append(m.group(2).strip())
                if is_correct:
                    if correct_index is not None:
                        # more than one [x] -- ambiguous, bail on this question
                        correct_index = "AMBIGUOUS"
                    else:
                        correct_index = len(options) - 1
                continue
            em = _EXPLANATION_RE.match(line)
            if em:
                explanation = em.group(1).strip()

        if not options or correct_index is None or correct_index == "AMBIGUOUS":
            continue

        questions.append({
            "question": section["heading"],
            "options": options,
            "correct_index": correct_index,
            "explanation": explanation,
        })

    return {"title": artifact["title"], "questions": questions}


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def grade_quiz(quiz_markdown: str, answers: list) -> dict:
    """Grades a submission against quiz_writer's own Markdown — the single
    source of truth for correctness (see module docstring).

    answers: list of option-indices, one per question, in the SAME order
        as parse_quiz()'s `questions` list (i.e. the order the quiz was
        rendered in). A missing/None entry for a question counts as
        unanswered rather than a free wrong-answer edge case the caller
        has to special-case — graded the same as any other incorrect
        index.

    Returns:
        {
            "title": str,
            "score": int,            # number correct
            "total": int,            # number of gradable questions
            "percent": float,        # 0-100, rounded to 1 decimal
            "results": [
                {"question": str, "options": [...], "correct_index": int,
                 "given_index": int | None, "is_correct": bool,
                 "explanation": str},
                ...
            ],
        }

    A length mismatch between `answers` and the parsed question count is
    not an error — missing trailing answers are treated as unanswered
    (same "degrade, don't hard-fail" posture as the rest of this
    codebase's store modules). This also means a question parse_quiz()
    had to skip (malformed options) simply isn't graded, rather than
    throwing off every subsequent answer's index.
    """
    parsed = parse_quiz(quiz_markdown)
    results = []
    score = 0
    for i, q in enumerate(parsed["questions"]):
        given = answers[i] if i < len(answers) else None
        is_correct = given is not None and given == q["correct_index"]
        if is_correct:
            score += 1
        results.append({
            "question": q["question"],
            "options": q["options"],
            "correct_index": q["correct_index"],
            "given_index": given,
            "is_correct": is_correct,
            "explanation": q["explanation"],
        })

    total = len(parsed["questions"])
    percent = round((score / total) * 100, 1) if total else 0.0
    return {"title": parsed["title"], "score": score, "total": total,
            "percent": percent, "results": results}


# ---------------------------------------------------------------------------
# Attempt history store (same JSON-store pattern as eo/graph_edges.py)
# ---------------------------------------------------------------------------

def record_attempt(workspace_id: str, quiz_node_id: str, quiz_markdown: str,
                    answers: list, created_by: str) -> dict:
    """Grades the submission via grade_quiz() and persists the attempt.
    quiz_node_id is the vector_id ("node:{workspace_id}:{node_id}") of the
    quiz artifact this attempt was taken against — same node-reference
    convention agents/exporter.py's `node_refs` and graph/adapters.py's
    `related_node_refs` already use, so an attempt can be joined back to
    its quiz node without a second lookup table.

    Stores per-question results (not just the score) so a later "review
    what you missed" flow — or get_missed_questions() below — has
    something to work with, matching the discipline eo/graph_edges.py
    hits with edge_id/created_by/created_at on every record.
    """
    graded = grade_quiz(quiz_markdown, answers)
    attempt = {
        "attempt_id": f"attempt_{uuid.uuid4().hex[:10]}",
        "workspace_id": workspace_id,
        "quiz_node_id": quiz_node_id,
        "created_by": created_by,
        "created_at": _now(),
        "score": graded["score"],
        "total": graded["total"],
        "percent": graded["percent"],
        "results": graded["results"],
    }
    with _lock:
        data = _read()
        data["attempts"].append(attempt)
        _write(data)
    return attempt


def list_attempts(workspace_id: str, quiz_node_id: str = None) -> list:
    """Every attempt in a workspace, optionally scoped to one quiz — what
    a per-quiz history view or a workspace-wide progress dashboard both
    need, same optional-scoping shape as graph_edges.py's list_edges()."""
    attempts = _read()["attempts"]
    attempts = [a for a in attempts if a["workspace_id"] == workspace_id]
    if quiz_node_id is not None:
        attempts = [a for a in attempts if a["quiz_node_id"] == quiz_node_id]
    return attempts


def get_attempt(attempt_id: str) -> dict:
    for a in _read()["attempts"]:
        if a["attempt_id"] == attempt_id:
            return a
    raise FileNotFoundError(attempt_id)


def get_missed_questions(workspace_id: str, quiz_node_id: str) -> list:
    """Every question this user has most recently gotten wrong on this
    quiz — the natural "what should I re-study" list. Reduces across ALL
    attempts for the quiz (oldest to newest), keeping only each
    question's LATEST result, so a question the user has since gotten
    right doesn't linger here forever even though quiz_writer may
    regenerate a different question set on a re-take.
    """
    attempts = sorted(
        list_attempts(workspace_id, quiz_node_id),
        key=lambda a: a["created_at"],
    )
    latest_result_for_question = {}
    for attempt in attempts:
        for r in attempt["results"]:
            latest_result_for_question[r["question"]] = r
    return [r for r in latest_result_for_question.values() if not r["is_correct"]]


if __name__ == "__main__":
    demo_quiz = (
        "# Demo Quiz\n\n"
        "## Q1: What color is the sky?\n\n"
        "- [ ] Green\n"
        "- [x] Blue\n"
        "- [ ] Red\n\n"
        "Explanation: Rayleigh scattering favors blue wavelengths.\n\n"
        "## Q2: What is 2 + 2?\n\n"
        "- [ ] 3\n"
        "- [x] 4\n"
        "- [ ] 5\n\n"
        "Explanation: Basic arithmetic.\n"
    )
    result = grade_quiz(demo_quiz, answers=[1, 0])
    print(json.dumps(result, indent=2))