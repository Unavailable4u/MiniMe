"""
agents/topic_note_writer.py — source-grounded, single-topic note
generator.

CONFIRMED GAP (2026-08-01, Notebooks Chat-First refinement testing):
api/server.py's "suggested_notes" Generate target (_generate_suggested_notes)
looks, from its label and CAPABILITIES_MANIFEST description ("Scan the
sources for note-worthy passages..."), like it should write a note
about whatever topic/scope you give it. It doesn't -- it calls
agents/note_taker.py's scan_conversation(), which scans the *chat
transcript* for decisions/action items worth remembering, and silently
ignores its own `scope` argument entirely. Asking it "write a note on
topic X" reliably returns nothing, because a request sentence isn't a
decision or action item -- confirmed by direct test against a live
workspace.

This module is the actually-source-grounded generator that gap implied
existed. Given a single topic_id, it pulls that topic's real source
content (same `covers` walk as eo/source_index.py's
get_topic_covered_sources(), step 6.11.d) and asks a dedicated role to
write ONE self-contained note summarizing it -- not a whole-notebook
scan, not a chat-transcript scan.

Same accept/reject discipline as agents/note_taker.py and
agents/fact_detector.py: this never writes into the knowledge graph
directly. It only ever calls eo/note_candidates.py's propose_note() --
a human still has to accept the candidate (via the existing Notes
candidates UI, eo/note_candidates.py's own API surface) before it
becomes a real write_node() call. Deliberately reuses that same
pending-review store rather than inventing a second one: from the
review UI's point of view, a topic_note_writer candidate and a
note_taker candidate are indistinguishable (same shape), which is
correct -- the user doesn't need to know or care which generator wrote
the draft, only that a human still approves it.

Runs through generic_worker like every other single-shot reasoning
role, same shape agents/fact_detector.py's detect_facts() and
agents/note_taker.py's _propose_from_context() both already use.

Place this file at: agents/topic_note_writer.py
"""
import os
import sys
import json
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.source_index import get_packet_depth
from eo.knowledge_graph import get_node
from eo.registry import get_role_prompt, add_role_prompt
from eo import note_candidates

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

# Same per-source cap agents/fact_detector.py uses (MAX_CONTENT_CHARS_PER_SOURCE)
# -- long enough for real signal, short enough that one long source
# doesn't crowd out a topic's other covered sources.
MAX_CONTENT_CHARS_PER_SOURCE = 6000

# Mirrors eo/registry.py's ROLE_PROMPTS_SEED entry for this role (added
# alongside this file), also registered defensively here for the same
# "already-running deployment's store predates this role" reason
# agents/fact_detector.py's own _ensure_role_registered() comment gives.
TOPIC_NOTE_WRITER_BRIEF = (
    "You read the source excerpts for a single topic from a project's "
    "notebook and write ONE clear, self-contained note summarizing "
    "what a student or reader needs to understand about this topic -- "
    "not a list of facts, not a restatement of every sentence, a "
    "genuine explanatory note someone could study from later without "
    "re-reading the source. If the excerpts don't contain enough "
    "actual content to write a real note (e.g. only a title, no "
    "substance), output exactly the single word NONE and nothing "
    "else. Otherwise output a single fenced ```json code block "
    "containing one JSON object with \"title\" (a short descriptive "
    "title for the note), \"content\" (the note itself, several "
    "sentences, self-contained and understandable without the source "
    "it came from), and \"tags\" (a short list of relevant keyword "
    "tags) — nothing else outside that code block. Never invent "
    "content the excerpts didn't actually support."
)


def _ensure_role_registered() -> None:
    if not get_role_prompt("topic_note_writer"):
        add_role_prompt("topic_note_writer", TOPIC_NOTE_WRITER_BRIEF, source="topic_note_writer_seed")


def _excerpt_for_topic(workspace_id: str, topic: dict) -> str:
    """Same walk-covers-through-get_node shape as
    agents/source_planner_lean.py's _attach_excerpts(), just for one
    topic instead of a flagged batch -- inlined rather than imported
    from that (private, leading-underscore) helper, since a
    topic-scoped caller that already knows it wants excerpts has no use
    for that module's Mode B "does this topic need excerpts?" judgment
    call in between. Falls back to summary/content_hint when there's no
    real source content to pull, same fallback agents/fact_detector.py's
    _context_for() takes for an un-flagged topic.
    """
    parts = []
    for node_id in topic.get("covers") or []:
        node = get_node(workspace_id, node_id)
        if not node:
            continue
        content = (node.get("content") or "").strip()[:MAX_CONTENT_CHARS_PER_SOURCE]
        if content:
            parts.append(content)
    excerpt = "\n\n".join(parts)
    if excerpt:
        return excerpt
    return (topic.get("summary") or topic.get("content_hint") or "").strip()


def generate_topic_note(workspace_id: str, topic_id: str, scope: str = "project",
                         session_id: str = None) -> dict | None:
    """Given a single topic, writes one source-grounded note candidate
    and returns it (same shape note_candidates.propose_note() returns),
    or None if there wasn't enough real content to write one, or the
    role judged nothing worth writing.

    Raises KeyError if topic_id doesn't resolve in the given scope --
    same "caller bug, not an empty-result situation" convention
    eo/source_index.py's get_packet_depth()/get_topic_covered_sources()
    already document for a bad topic_id.
    """
    packet = get_packet_depth(
        workspace_id, starting_topic_id=topic_id, requested_depth=0,
        scope=scope, session_id=session_id,
    )
    topic = packet["topics"][topic_id]
    excerpt = _excerpt_for_topic(workspace_id, topic)
    if not excerpt:
        return None

    _ensure_role_registered()
    from agents.generic_worker import run as run_role   # deferred, same
                                                          # circular-import
                                                          # reason
                                                          # agents/fact_detector.py's
                                                          # own generic_worker
                                                          # call defers this

    title = topic.get("name") or "Untitled topic"
    task_text = (
        f"Write one note summarizing the topic \"{title}\" from the "
        f"source excerpt(s) below.\n\n{excerpt}"
    )
    result = run_role(
        role="topic_note_writer",
        task_text=task_text,
        input_keys=[],
        session_id=None,
        # The excerpt above already IS this call's context -- no chat
        # history to fold in, same reasoning agents/fact_detector.py's
        # own detect_facts() call gives for include_conversation_context=False.
        include_conversation_context=False,
        domain="notes",
    )
    raw = (result.get("text") or "").strip()
    if raw.upper() == "NONE":
        return None

    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    note_title = (parsed.get("title") or "").strip()
    note_content = (parsed.get("content") or "").strip()
    if not note_title or not note_content:
        return None
    tags = parsed.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    return note_candidates.propose_note(
        workspace_id=workspace_id,
        title=note_title,
        content=note_content,
        tags=tags,
        proposed_by="topic_note_writer",
    )


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) != 3:
        print("Usage: python -m agents.topic_note_writer <workspace_id> <topic_id>")
        _sys.exit(1)
    found = generate_topic_note(_sys.argv[1], _sys.argv[2])
    print(json.dumps(found, indent=2) if found else "No candidate produced.")
