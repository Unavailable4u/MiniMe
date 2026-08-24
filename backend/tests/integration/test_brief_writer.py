"""
tests/integration/test_brief_writer.py — mocked rebuild of the old
tests/test_brief_writer.py.

The old version was a print-and-eyeball script: it called
eo.panel._get_or_write_role_prompt() twice against REAL Redis and a REAL
LLM, timed both calls, and eyeballed that call 2 was "dramatically
faster" than call 1. That's a real behavior worth protecting (the whole
point of the registry cache is "write a role's brief once, ever"), but it
doesn't need a live LLM or a stopwatch to verify — it needs to assert
call 1 hits generate_text() and writes the registry, and call 2 doesn't
call generate_text() again and returns the identical brief.

Note there is no dedicated agents/brief_writer.py module — "brief writer"
is eo/panel.py's _get_or_write_role_prompt(), which every tab in the
Master Guide's B1 sector-2 list groups with the other agent-backed
integration tests since it's the one role-resolution path that makes its
own LLM call.
"""

from eo import panel
from eo.registry import ROLE_PROMPTS_KEY, get_role_prompt
from memory.bus import read

TEST_ROLE = "diagram_designer_test_role"  # deliberately not in ROLE_PROMPTS_SEED


def test_first_call_writes_a_new_brief_via_the_llm(mock_llm):
    assert get_role_prompt(TEST_ROLE) is None, "test role must start absent from the registry"

    mock_llm.set_response("Design clear, accurate diagrams of system architecture and data flow.")

    brief = panel._get_or_write_role_prompt(
        TEST_ROLE,
        task_text="Design a clear architecture diagram showing how the EO Panel, "
                  "Inspector, and Registry interact.",
    )

    assert mock_llm.mock.call_count == 1, "slow path should make exactly one LLM call"
    assert brief == "Design clear, accurate diagrams of system architecture and data flow."
    assert get_role_prompt(TEST_ROLE) == brief, "brief must be persisted to the registry"


def test_second_call_is_a_cache_hit_with_no_llm_call(mock_llm):
    mock_llm.set_response("Design clear, accurate diagrams of system architecture and data flow.")
    brief1 = panel._get_or_write_role_prompt(TEST_ROLE, task_text="irrelevant on the slow path too")
    assert mock_llm.mock.call_count == 1

    # A completely different canned response -- if the fast path ever
    # accidentally called generate_text() again, this would surface as a
    # mismatched brief instead of silently passing.
    mock_llm.set_response("THIS SHOULD NEVER BE RETURNED")

    brief2 = panel._get_or_write_role_prompt(TEST_ROLE, task_text="irrelevant on the fast path")

    assert mock_llm.mock.call_count == 1, "fast path must not make a second LLM call"
    assert brief2 == brief1, "fast path returned a DIFFERENT brief than the slow path wrote"


def test_registry_entry_survives_a_read_from_the_raw_bus_key(mock_llm):
    """Sanity check on the storage side: ROLE_PROMPTS_KEY is the global,
    non-namespaced store the guide's §0.1 snapshot describes -- confirm
    the brief written above is actually sitting there, not just visible
    through get_role_prompt()'s own accessor."""
    mock_llm.set_response("A short, reusable role brief.")
    panel._get_or_write_role_prompt(TEST_ROLE, task_text="anything")

    raw = read(ROLE_PROMPTS_KEY, default={})
    assert TEST_ROLE in raw
    assert raw[TEST_ROLE]["brief"] == "A short, reusable role brief."
