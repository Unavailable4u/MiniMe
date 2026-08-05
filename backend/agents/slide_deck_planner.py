"""
agents/slide_deck_planner.py — Notebooks Chat-First Refinement, Phase 5
step 5.5: wires POST /api/workspaces/{ws_id}/notebooks/video_overview to
the slide_planner role, the same way agents/podcast_scriptwriter.py wired
the podcast route in step 5.2.

eo/registry.py already has a working, hand-written brief for
"slide_planner" ('# Deck Title' + '## Slide Title' + bullet-line Markdown)
— today it's only reachable via the separate, non-workspace-scoped
POST /api/notes/video-overview/build (see that route's own comment for
the guide's §0 finding on this whole subsystem), which requires the
caller to already have both slide_text AND a previously-synthesized
podcast mp3 in hand. This module is the missing "somewhere else" for the
slide half, exactly as podcast_scriptwriter.py is for the narration half:
read the workspace's own sources and run slide_planner through
generic_worker directly, so a deck outline can be produced starting from
nothing but a workspace_id.

Deliberately near-identical in shape to agents/podcast_scriptwriter.py
(same source_planner_lean.py Mode B/C read, same per-source truncation,
same deferred generic_worker import) rather than sharing a helper between
the two — same "keep this step's diff small, don't refactor an unrelated
existing module" reasoning that file's own docstring already gives.

Does NOT call agents/video_overview_builder.py:build_video_overview() —
that's api/server.py's notebooks_video_overview() route, step 5.5's own
"feed it into the builder" leg, same split podcast_scriptwriter.py draws
against agents/tts_synthesizer.py. Does NOT persist anything via
eo/panel_content.py — same route, its own step. This module's only job
is: given a workspace_id (and an optional source scope), return the raw
'# Deck Title' / '## Slide Title' Markdown outline.

Place this file at: agents/slide_deck_planner.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_planner_lean import plan

# Same per-source truncation reasoning as agents/podcast_scriptwriter.py /
# agents/study_generator.py — a single source is a whole document post-§3
# PDF fix, so an uncapped read lets one long source crowd out every other
# source in scope. Matches podcast_scriptwriter's own 8000: a slide deck
# outline wants the same "draw on real, specific detail" depth, not just
# enough to spot a one-line fact.
MAX_CONTENT_CHARS_PER_SOURCE = 8000

_ROLE = "slide_planner"


def _context_for(topics: dict) -> str:
    """Identical shape to agents/podcast_scriptwriter.py's helper of the
    same name — one section per topic, Mode B excerpts where flagged,
    summary/content_hint otherwise. Not imported from there rather than
    duplicated on purpose, matching that module's own posture of keeping
    small, near-identical per-caller helpers separate rather than sharing
    one across unrelated call sites."""
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


def generate_slide_deck(workspace_id: str, source_node_ids: list[str] | None = None) -> str:
    """Returns the raw '# Deck Title' / '## Slide Title' Markdown outline
    a slide_planner role produces over the given sources, or every source
    in the workspace when `source_node_ids` is falsy — same "blank scope
    = whole notebook" convention agents/podcast_scriptwriter.py and
    agents/study_generator.py already use.

    Raises LookupError if the resolved scope has zero readable topic
    content, same contract agents/podcast_scriptwriter.py's
    generate_podcast_script() already gives its own caller — so
    api/server.py's route can turn it into a clear 400 instead of
    silently asking the model to plan a deck from nothing.
    """
    packet = plan(
        workspace_id,
        task_text=(
            "Plan a slide deck outline that draws on real detail from "
            "the source material — specific facts, figures, and "
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
                                                          # agents/podcast_scriptwriter.py
                                                          # already gives

    task_text = "Source material:\n\n" + context
    result = run_role(
        role=_ROLE,
        task_text=task_text,
        input_keys=[],
        session_id=None,
        # Same reasoning as agents/podcast_scriptwriter.py's own
        # include_conversation_context=False: the source excerpts above
        # already ARE this call's context — a slide deck built from
        # notebook sources has no business pulling in unrelated chat
        # history.
        include_conversation_context=False,
        domain="notes",
    )
    return (result.get("text") or "").strip()


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("usage: python -m agents.slide_deck_planner <workspace_id>")
    else:
        text = generate_slide_deck(_sys.argv[1])
        print(text[:1000])
