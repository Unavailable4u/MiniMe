"""
agents/study_generator.py — Notebooks integration guide §6.1: Flashcards /
Quiz / Study Guide wired to Generate.

eo/registry.py already has working, hand-written briefs for all three
roles this file drives (flashcard_writer, quiz_writer, study_guide_writer)
-- guide §0 confirmed they were already runnable, just only ever reached
through a manual role chat the user ran and pasted the answer back from.
This file is the missing direct call: read the scoped source material,
run the appropriate role through generic_worker (single-hire, same shape
as agents/note_taker.py and agents/fact_detector.py -- no Panel staffing,
no role handoffs, per guide §2's "known, fixed, single-purpose job"
reasoning), and hand the raw Markdown back to the caller.

Deliberately does NOT call eo/panel_content.py's set_content() itself --
saving needs the actual requesting user's id (for the audit-log/
updated_by field), which this module has no business knowing about.
api/server.py's Generate dispatch already has owner_id in hand and saves
the result after this returns.

CHANGED — Data Layer architecture §6c: was reading every in-scope
node's raw content straight off eo/knowledge_graph.py's list_nodes().
Now reads agents/source_planner_lean.py:plan() instead -- Mode B/C
(§5's distinction), same retrofit shape as agents/fact_detector.py's
§6b change: the lean role judges from the topic skeleton alone which
topics need their real excerpts pulled in for a flashcard/quiz/study
guide to draw real detail from, and which are already covered well
enough by their own summary.

Place this file at: agents/study_generator.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_planner_lean import plan

# Same per-source truncation reasoning as agents/fact_detector.py: with
# §3's PDF fix a single source is now the whole document, so without a
# cap one long source could crowd out every other source in scope. A
# bit more headroom than fact_detector's 6000 -- flashcards/quizzes/
# study guides want to draw from the source's actual detail, not just
# enough to spot a fact worth a one-line note.
MAX_CONTENT_CHARS_PER_SOURCE = 8000

# panel_key -> the registry role that already knows how to write that
# format. eo/panel_content.py's VALID_PANEL_KEYS is the source of truth
# for which keys are real; this dict only needs the three Study ones.
ROLES_BY_PANEL_KEY = {
    "study_flashcards": "flashcard_writer",
    "study_quiz": "quiz_writer",
    "study_guide": "study_guide_writer",
}


def _context_for(topics: dict) -> str:
    """One section per topic: its Mode B excerpts if source_planner_lean
    flagged it as needing them, otherwise its name/summary/content_hint
    as-is -- same shape as agents/fact_detector.py's helper of the same
    name."""
    parts = []
    for topic in topics.values():
        title = topic.get("name") or "Untitled topic"
        body = topic.get("excerpts")
        if not body:
            body = topic.get("summary") or topic.get("content_hint") or ""
        body = body.strip()[:MAX_CONTENT_CHARS_PER_SOURCE]
        if not body:
            continue
        parts.append(f"--- {title} ---\n{body}")
    return "\n\n".join(parts)


def generate_study_content(panel_key: str, workspace_id: str, source_node_ids: list[str] | None = None) -> str:
    """Returns the raw Markdown a flashcard_writer/quiz_writer/
    study_guide_writer role produces over the given sources (or every
    source in the workspace when `source_node_ids` is falsy -- "blank
    scope = whole notebook," same convention as agents/fact_detector.py
    and Notebooks integration guide §4.2).

    CHANGED — Data Layer architecture §6c: `source_node_ids` scoping
    used to mean "only these Primary Source nodes"; read the same way
    §6a/§6b's retrofits read it -- "only topics whose `covers` list
    touches one of these node ids."

    Raises ValueError for an unrecognized panel_key and LookupError if
    the resolved scope has zero readable topic content, so the caller
    (api/server.py's Generate dispatch) can turn either into a clear
    per-branch error instead of silently saving an empty deck.
    """
    role = ROLES_BY_PANEL_KEY.get(panel_key)
    if role is None:
        raise ValueError(f"unrecognized study panel_key {panel_key!r}")

    packet = plan(
        workspace_id,
        task_text=(
            f"Write a {panel_key.replace('_', ' ')} that draws on real "
            "detail from the source material -- specific facts, figures, "
            "and explanations a one-line summary wouldn't capture."
        ),
        scope="project",
    )
    topics = packet["topics"]
    if source_node_ids:
        wanted = set(source_node_ids)
        topics = {tid: t for tid, t in topics.items()
                  if wanted & set(t.get("covers") or [])}

    context = _context_for(topics)
    if not context:
        raise LookupError("no readable topic content in scope")

    from agents.generic_worker import run as run_role  # deferred, same
                                                          # circular-import
                                                          # reason as
                                                          # agents/note_taker.py
                                                          # and
                                                          # agents/fact_detector.py

    task_text = "Source material:\n\n" + context
    result = run_role(
        role=role,
        task_text=task_text,
        input_keys=[],
        session_id=None,
        # The source excerpts above already ARE this call's context --
        # no chat history belongs in a flashcard deck built from
        # notebook sources. Same reasoning as fact_detector's own
        # include_conversation_context=False.
        include_conversation_context=False,
        domain="notes",
    )
    return (result.get("text") or "").strip()


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 3:
        print("usage: python -m agents.study_generator <panel_key> <workspace_id>")
    else:
        text = generate_study_content(_sys.argv[1], _sys.argv[2])
        print(text[:1000])
