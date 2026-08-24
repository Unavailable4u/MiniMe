"""
Notebooks Chat-First refinement, Phase 6 step 6.11.h.

"End-to-end test: click 'Work through' on a topic with sources+notes
vs. one with none, confirm the response visibly differs."

A REAL end-to-end click-through (through the API, a live LLM call, and
a real workspace) needs GROQ_API_KEY / HUGGINGFACE_API_KEY /
UPSTASH_VECTOR_REST_URL+TOKEN and an actual ingested workspace — same
"manual integration check, not CI-repeatable" category
scripts/test_topic_related_notes.py already documents for itself. This
script covers the part that CAN be made deterministic and offline: it
exercises api/task_runner.py's actual 6.11.f code
(_topic_scoped_task_text() / _grounded_task_text()) directly, against a
monkeypatched secondary-data store (same technique
scripts/test_topic_covered_sources.py uses for 6.11.d) plus stubbed
get_node()/get_topic_related_notes() calls — so it needs no network
access and no API keys, and proves the actual splice logic is correct
independent of any one live workspace's data.

For the real click-through afterwards, see the "LIVE CHECK" section in
this file's __main__ block.

Usage:
    python scripts/test_topic_scoped_grounding.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Same "point the store at a scratch file before anything imports it"
# technique test_topic_covered_sources.py uses, so this never touches
# a real workspace's data/graph/_secondary_data.json.
_scratch = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_scratch.close()

from eo import secondary_data

secondary_data.SECONDARY_DATA_PATH = _scratch.name

WORKSPACE_ID = "ws_test_6_11_h"

FAKE_DOC = {
    WORKSPACE_ID: {
        "topics": {
            # Has both covered sources AND (via the stub below) a
            # related note — the "should ground richly" case.
            "topic_salient_pole": {
                "name": "Salient Pole Rotor",
                "summary": "Used in low-to-medium speed alternators.",
                "parent": None,
                "source_section_ids": ["sec_rotor_1", "sec_rotor_2"],
                "content_hint": "conceptual",
            },
            # Real topic, zero covers, zero notes — the "should fall
            # back cleanly, not error" case 6.11.d/e's own docstrings
            # call out as valid-not-an-error.
            "topic_untouched": {
                "name": "Untouched Topic",
                "summary": "Nothing ingested against this one yet.",
                "parent": None,
                "source_section_ids": [],
                "content_hint": "conceptual",
            },
        },
        "connections": [],
    }
}


def _seed():
    with open(_scratch.name, "w", encoding="utf-8") as f:
        json.dump(FAKE_DOC, f)


# Fake source-node content, keyed the same way eo.knowledge_graph.get_node()
# would return it (a dict with "title"/"content").
_FAKE_NODES = {
    "sec_rotor_1": {
        "title": "Rotor Construction, p.12",
        "content": ("Salient pole rotors have a large diameter and short "
                     "axial length, with laminated projecting poles bolted "
                     "to the rotor spider."),
    },
    "sec_rotor_2": {
        "title": "Rotor Construction, p.13",
        "content": ("They are unsuitable for high-speed operation because "
                     "the projecting poles create excessive windage losses "
                     "and mechanical stress."),
    },
}

_FAKE_NOTES = [
    {
        "node_id": "note_1",
        "title": "Notes: Salient Pole Rotor",
        "content": "Key point: low-speed use only, laminated poles, not for turbo-alternators.",
        "score": 0.91,
    },
]


def _fake_get_node(workspace_id, node_id):
    return _FAKE_NODES.get(node_id)


def _fake_get_topic_related_notes(workspace_id, topic_id, top_k=10, min_score=None,
                                   scope="project", session_id=None):
    if topic_id == "topic_salient_pole":
        return _FAKE_NOTES
    return []


def main() -> None:
    _seed()
    any_failure = False

    def check(label, fn):
        nonlocal any_failure
        try:
            fn()
            print(f"OK   {label}")
        except Exception as exc:
            any_failure = True
            print(f"FAIL {label}: {exc.__class__.__name__}: {exc}")

    # Import AFTER the store is monkeypatched (same ordering
    # test_topic_covered_sources.py relies on) and INSIDE main() so the
    # patch decorators below can target task_runner's own bound names.
    from api import task_runner

    task_text = 'Let\'s work through: "Salient Pole Rotor"'

    def _rich_topic_grounds_and_differs():
        with patch.object(task_runner, "get_node", side_effect=_fake_get_node), \
             patch.object(task_runner, "get_topic_related_notes", side_effect=_fake_get_topic_related_notes):
            grounded_text, node_ids = task_runner._grounded_task_text(
                WORKSPACE_ID, task_text, topic_id="topic_salient_pole")
        assert grounded_text != task_text, "grounded text should differ from the bare task text"
        assert "Source excerpts for this topic" in grounded_text
        assert "laminated projecting poles" in grounded_text, "should contain real source content"
        assert "Existing notes on this topic" in grounded_text
        assert "turbo-alternators" in grounded_text, "should contain the related note's content"
        assert set(node_ids) == {"sec_rotor_1", "sec_rotor_2"}

    def _empty_topic_falls_back_cleanly():
        # topic_untouched has zero covers and (per the stub) zero notes
        # — _topic_scoped_task_text() should return (None, source_ids),
        # and _grounded_task_text() should fall through to its own
        # generic search path rather than raising or looping.
        with patch.object(task_runner, "get_node", side_effect=_fake_get_node), \
             patch.object(task_runner, "get_topic_related_notes", side_effect=_fake_get_topic_related_notes), \
             patch.object(task_runner, "search_nodes", return_value=[]):
            grounded_text, node_ids = task_runner._grounded_task_text(
                WORKSPACE_ID, task_text, topic_id="topic_untouched")
        # Falls all the way through to "no workspace/no matches" shape:
        # original task_text back, unchanged.
        assert grounded_text == task_text, "should fall back to the bare task text, not error"
        assert node_ids == [], node_ids

    def _unknown_topic_id_fails_open():
        # A bad topic_id is a caller bug per get_topic_covered_sources()'s
        # own contract (raises KeyError) — _topic_scoped_task_text() must
        # swallow that and let the caller fall back, not propagate it and
        # 500 the chat turn.
        with patch.object(task_runner, "get_node", side_effect=_fake_get_node), \
             patch.object(task_runner, "get_topic_related_notes", side_effect=_fake_get_topic_related_notes), \
             patch.object(task_runner, "search_nodes", return_value=[]):
            grounded_text, node_ids = task_runner._grounded_task_text(
                WORKSPACE_ID, task_text, topic_id="topic_does_not_exist")
        assert grounded_text == task_text
        assert node_ids == []

    def _no_topic_id_behaves_exactly_as_before():
        # Regression guard: omitting topic_id entirely (every non-Notebooks
        # caller, and Notebooks chat turns with no workflow-step context)
        # must take the pre-6.11.f code path unchanged.
        with patch.object(task_runner, "search_nodes", return_value=[]):
            grounded_text, node_ids = task_runner._grounded_task_text(
                WORKSPACE_ID, task_text)
        assert grounded_text == task_text
        assert node_ids == []

    def _topic_note_writer_never_called():
        # Per the 6.11.g decision: this path must never invoke
        # agents/topic_note_writer.py — it's read-only grounding, not
        # note drafting. Patch it to raise if touched at all.
        from agents import topic_note_writer
        with patch.object(task_runner, "get_node", side_effect=_fake_get_node), \
             patch.object(task_runner, "get_topic_related_notes", side_effect=_fake_get_topic_related_notes), \
             patch.object(topic_note_writer, "generate_topic_note",
                           side_effect=AssertionError("topic_note_writer must not be called by chat grounding")):
            task_runner._grounded_task_text(WORKSPACE_ID, task_text, topic_id="topic_salient_pole")

    check("topic with sources+notes grounds richly and visibly differs", _rich_topic_grounds_and_differs)
    check("topic with no sources/notes falls back to plain task_text", _empty_topic_falls_back_cleanly)
    check("unknown topic_id fails open instead of raising", _unknown_topic_id_fails_open)
    check("no topic_id at all behaves exactly as pre-6.11.f", _no_topic_id_behaves_exactly_as_before)
    check("never calls topic_note_writer (6.11.g decision)", _topic_note_writer_never_called)

    print()
    if any_failure:
        print("One or more checks FAILED -- see above.")
        sys.exit(1)
    print("All checks passed. 6.11.f's splice logic is confirmed correct offline.")
    print(
        "\nLIVE CHECK (optional, needs a real workspace + API keys, same\n"
        "category as scripts/test_topic_related_notes.py):\n"
        "  1. Seed a real topic's note: python scripts/seed_test_note.py <ws_id> <title> <summary>\n"
        "  2. POST /api/task with topic_id=<that topic's id> and task_text=\n"
        '     \'Let\\\'s work through: "<title>"\' -- confirm the answer visibly\n'
        "     cites the seeded source/note content.\n"
        "  3. Repeat with a real topic_id that has zero sources/notes and\n"
        "     confirm the answer reads generically (no fabricated specifics)\n"
        "     instead of hallucinating topic content it was never given."
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_scratch.name)
        except OSError:
            pass
