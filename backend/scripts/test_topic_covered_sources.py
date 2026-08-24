"""
Notebooks Chat-First refinement, Phase 6 step 6.11.d.

"Small helper: given a workspace_id + topic_id, call get_packet_depth()
and return that topic's covers list -- test standalone."

Standalone in the same sense as scripts/test_tool_calling.py (steps
2.3/2.4): no FastAPI app, no auth, no real workspace -- just
eo/source_index.py:get_topic_covered_sources() pointed at a throwaway
JSON store seeded with a small fake topic tree, so this can run without
touching whatever's actually in data/graph/_secondary_data.json.

Does this by monkeypatching eo.secondary_data.SECONDARY_DATA_PATH to a
tempfile before eo.source_index is imported -- source_index.py imports
get_secondary_data_scoped by reference, and secondary_data.py's own
_read()/_write() re-read SECONDARY_DATA_PATH off the module each call
(not a captured constant), so this is enough; no need to reload either
module.

Usage:
    python scripts/test_topic_covered_sources.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Point the store at a scratch file BEFORE importing anything that reads
# it, so no real workspace data is ever touched by this script.
_scratch = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_scratch.close()

from eo import secondary_data

secondary_data.SECONDARY_DATA_PATH = _scratch.name

from eo.source_index import get_topic_covered_sources

WORKSPACE_ID = "ws_test_6_11_d"

FAKE_DOC = {
    WORKSPACE_ID: {
        "topics": {
            "topic_recursion": {
                "name": "Recursion",
                "summary": "Functions that call themselves.",
                "parent": None,
                "source_section_ids": ["sec_101", "sec_102"],
                "content_hint": "conceptual",
            },
            "topic_base_case": {
                "name": "Base cases",
                "summary": "Why a recursive function needs one.",
                "parent": "topic_recursion",
                "source_section_ids": ["sec_103"],
                "content_hint": "conceptual",
            },
            "topic_no_sources": {
                # Deliberately empty -- exercises the "covers is a valid
                # empty list, not an error" case the helper's docstring
                # calls out.
                "name": "Untouched topic",
                "summary": "Nothing ingested against this one yet.",
                "parent": None,
                "source_section_ids": [],
                "content_hint": "conceptual",
            },
        },
        "connections": [
            {"from_topic": "topic_base_case", "to_topic": "topic_recursion", "relation": "elaborates-on"},
        ],
    }
}


def _seed():
    with open(_scratch.name, "w", encoding="utf-8") as f:
        json.dump(FAKE_DOC, f)


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

    def _two_sections():
        got = get_topic_covered_sources(WORKSPACE_ID, "topic_recursion")
        assert got == ["sec_101", "sec_102"], got

    def _one_section():
        got = get_topic_covered_sources(WORKSPACE_ID, "topic_base_case")
        assert got == ["sec_103"], got

    def _no_sections():
        got = get_topic_covered_sources(WORKSPACE_ID, "topic_no_sources")
        assert got == [], got

    def _unknown_topic():
        try:
            get_topic_covered_sources(WORKSPACE_ID, "topic_does_not_exist")
        except KeyError:
            return
        raise AssertionError("did not raise KeyError for an unknown topic_id")

    def _empty_workspace():
        try:
            get_topic_covered_sources("", "topic_recursion")
        except ValueError:
            return
        raise AssertionError("did not raise ValueError for an empty workspace_id")

    check("topic with two covered sections", _two_sections)
    check("topic with one covered section", _one_section)
    check("topic with no covered sections (must be [], not an error)", _no_sections)
    check("unknown topic_id raises KeyError", _unknown_topic)
    check("empty workspace_id raises ValueError", _empty_workspace)

    print()
    if any_failure:
        print("One or more checks FAILED -- see above.")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(_scratch.name)
        except OSError:
            pass
