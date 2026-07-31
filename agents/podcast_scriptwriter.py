"""
agents/podcast_scriptwriter.py — Notebooks Chat-First Refinement, Phase 5
step 5.2: wires POST /api/workspaces/{ws_id}/notebooks/podcast (step 5.1's
route skeleton) to the podcast_scriptwriter role.

eo/registry.py already has a working, hand-written brief for
"podcast_scriptwriter" (two-host "HOST A:"/"HOST B:" format) — the
guide's own §0 finding is that this role and the whole podcast/video
subsystem live in a separate, non-workspace-scoped "notes" domain today
(POST /api/notes/podcast/synthesize), reachable only by a caller who
already has script_text in hand from somewhere else. This file is the
missing "somewhere else": read the workspace's own sources (the same
source_planner_lean.py Mode B/C read agents/study_generator.py already
uses for Flashcards/Quiz/Study Guide) and run podcast_scriptwriter
through generic_worker directly, so a script can be produced starting
from nothing but a workspace_id — no separate script-writing step the
caller has to orchestrate themselves.

Deliberately mirrors agents/study_generator.py's shape closely (a single
fixed role rather than a panel_key -> role table, since this module only
ever drives the one role) rather than generalizing the two into one
shared helper — same "keep this step's diff small, don't refactor an
unrelated existing module" reasoning frontend/app/lib/notebookAffinities.js
gives for not folding into TARGETS_BY_KEY's existing duplicates.

Does NOT call agents/tts_synthesizer.py:synthesize_podcast() — that's
step 5.3. Does NOT persist anything via eo/panel_content.py — that's
step 5.4. This module's only job is: given a workspace_id (and an
optional source scope), return the raw two-host Markdown script.

Place this file at: agents/podcast_scriptwriter.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_planner_lean import plan

# Same per-source truncation reasoning as agents/study_generator.py /
# agents/fact_detector.py — a single source is a whole document post-§3
# PDF fix, so an uncapped read lets one long source crowd out every
# other source in scope. Matches study_generator's own 8000: a podcast
# script wants the same "draw on real, specific detail" depth a
# flashcard deck or study guide does, not just enough to spot a
# one-line fact.
MAX_CONTENT_CHARS_PER_SOURCE = 8000

_ROLE = "podcast_scriptwriter"


def _context_for(topics: dict) -> str:
    """Identical shape to agents/study_generator.py's helper of the same
    name — one section per topic, Mode B excerpts where flagged,
    summary/content_hint otherwise. Not imported from there rather than
    duplicated on purpose, matching that module's own posture of keeping
    small, near-identical per-caller helpers separate rather than
    sharing one across unrelated call sites."""
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


def generate_podcast_script(workspace_id: str, source_node_ids: list[str] | None = None) -> str:
    """Returns the raw two-host Markdown ("HOST A:"/"HOST B:" lines) a
    podcast_scriptwriter role produces over the given sources, or every
    source in the workspace when `source_node_ids` is falsy — same
    "blank scope = whole notebook" convention agents/study_generator.py
    and agents/fact_detector.py already use.

    Raises LookupError if the resolved scope has zero readable topic
    content, same contract agents/study_generator.py's
    generate_study_content() already gives its own caller — so
    api/server.py's route can turn it into a clear 400 instead of
    silently asking the model to write a script from nothing.
    """
    packet = plan(
        workspace_id,
        task_text=(
            "Write a two-host podcast script that draws on real detail "
            "from the source material — specific facts, figures, and "
            "explanations a one-line summary wouldn't capture."
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

    from agents.generic_worker import run as run_role   # deferred, same
                                                          # circular-import
                                                          # reason
                                                          # agents/study_generator.py
                                                          # and
                                                          # agents/fact_detector.py
                                                          # already give

    task_text = "Source material:\n\n" + context
    result = run_role(
        role=_ROLE,
        task_text=task_text,
        input_keys=[],
        session_id=None,
        # Same reasoning as agents/study_generator.py's own
        # include_conversation_context=False: the source excerpts above
        # already ARE this call's context — a podcast script built from
        # notebook sources has no business pulling in unrelated chat
        # history.
        include_conversation_context=False,
        domain="notes",
    )
    return (result.get("text") or "").strip()


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("usage: python -m agents.podcast_scriptwriter <workspace_id>")
    else:
        text = generate_podcast_script(_sys.argv[1])
        print(text[:1000])
