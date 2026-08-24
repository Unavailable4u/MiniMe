"""
agents/rehearsal_scriptwriter.py — Notebooks Chat-First Refinement, Phase 5
step 5.9: the presentation_rehearsal prompt profile. Deliberately mirrors
agents/podcast_scriptwriter.py's shape closely (same source_planner_lean
usage, same per-source truncation, same generic_worker call, same "blank
scope = whole notebook" convention) rather than sharing a helper between
the two — same "keep this step's diff small, don't refactor an unrelated
existing module" reasoning podcast_scriptwriter's own docstring already
gives.

Unlike podcast_scriptwriter (a single fixed format), this role has three
modes ("judge" / "two_host" / "devils_advocate") and a difficulty level
("novice" / "expert") — both are steered via task_text, not via separate
role entries, matching the "rehearsal_scriptwriter" brief's own design
(see eo/registry.py's ROLE_LIBRARY, step 5.9 comment).

Does NOT call agents/tts_synthesizer.py:synthesize_podcast() and does NOT
persist anything via eo/panel_content.py — those are step 5.10. This
module's only job is: given a workspace_id, mode, and difficulty (plus an
optional source scope), return the raw "LABEL:"/"[PAUSE]"-formatted
Markdown script.

Place this file at: agents/rehearsal_scriptwriter.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.source_planner_lean import plan

# Same per-source truncation reasoning as agents/podcast_scriptwriter.py /
# agents/study_generator.py — a single source is a whole document post-§3
# PDF fix, so an uncapped read lets one long source crowd out every other
# source in scope. Matches podcast_scriptwriter's own 8000: a rehearsal
# script wants the same "draw on real, specific detail" depth so its
# questions (and model answers) are actually grounded, not generic.
MAX_CONTENT_CHARS_PER_SOURCE = 8000

_ROLE = "rehearsal_scriptwriter"

_VALID_MODES = {"judge", "two_host", "devils_advocate"}
_VALID_DIFFICULTIES = {"novice", "expert"}
_DEFAULT_MODE = "judge"
_DEFAULT_DIFFICULTY = "expert"


def _context_for(topics: dict) -> str:
    """Identical shape to agents/podcast_scriptwriter.py's helper of the
    same name — not imported from there on purpose, matching that
    module's own posture of keeping small, near-identical per-caller
    helpers separate rather than sharing one across unrelated call
    sites."""
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


def generate_rehearsal_script(
    workspace_id: str,
    mode: str = _DEFAULT_MODE,
    difficulty: str = _DEFAULT_DIFFICULTY,
    source_node_ids: list[str] | None = None,
) -> str:
    """Returns the raw rehearsal_scriptwriter Markdown script for the
    given workspace — "JUDGE:"/"HOST A:"/"HOST B:"/"ADVOCATE:" dialogue
    lines depending on `mode`, plus "[PAUSE:N]" and "MODEL ANSWER:"
    lines, over every source in the workspace when `source_node_ids` is
    falsy (same "blank scope = whole notebook" convention every other
    Generate target already uses).

    Raises ValueError for an unrecognized mode/difficulty — fail loudly
    at the call site rather than silently falling back to a default the
    caller didn't ask for. Raises LookupError if the resolved scope has
    zero readable topic content, same contract
    agents/podcast_scriptwriter.py's generate_podcast_script() already
    gives its own caller.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown rehearsal mode {mode!r} — expected one of {sorted(_VALID_MODES)}")
    if difficulty not in _VALID_DIFFICULTIES:
        raise ValueError(f"unknown rehearsal difficulty {difficulty!r} — expected one of {sorted(_VALID_DIFFICULTIES)}")

    packet = plan(
        workspace_id,
        task_text=(
            "Write a presentation-rehearsal script that quizzes the user "
            "on real detail from the source material — specific facts, "
            "figures, and explanations a one-line summary wouldn't "
            "capture."
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
                                                          # reason
                                                          # agents/podcast_scriptwriter.py
                                                          # already gives

    task_text = (
        f"Mode: {mode}\n"
        f"Difficulty: {difficulty}\n\n"
        "Source material:\n\n" + context
    )
    result = run_role(
        role=_ROLE,
        task_text=task_text,
        input_keys=[],
        session_id=None,
        # Same reasoning as podcast_scriptwriter's own
        # include_conversation_context=False: the source excerpts above
        # already ARE this call's context — a rehearsal script built
        # from notebook sources has no business pulling in unrelated
        # chat history.
        include_conversation_context=False,
        domain="notes",
    )
    return (result.get("text") or "").strip()


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("usage: python -m agents.rehearsal_scriptwriter <workspace_id> [mode] [difficulty]")
    else:
        ws_id = _sys.argv[1]
        mode_arg = _sys.argv[2] if len(_sys.argv) > 2 else _DEFAULT_MODE
        difficulty_arg = _sys.argv[3] if len(_sys.argv) > 3 else _DEFAULT_DIFFICULTY
        text = generate_rehearsal_script(ws_id, mode_arg, difficulty_arg)
        print(text[:1000])
