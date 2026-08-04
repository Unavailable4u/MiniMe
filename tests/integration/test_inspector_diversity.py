"""
tests/integration/test_inspector_diversity.py — mocked rebuild of the old
tests/test_inspector_diversity.py.

Bug caught by writing this test: the old script read `result['tier']`
off classify()'s return value. Per eo/inspector.py's own "Migration Part
12 §8.2/§8.4" comment, the output schema changed "tier" (int 0-3) to
"path" (string) -- classify()'s _validate() no longer puts a "tier" key
in its return dict at all. The old test would raise KeyError the moment
it ran against the current code instead of printing a diversity report,
which is exactly the kind of silent collection-time failure this sector
is meant to catch instead of finding out the hard way in production.

This also can't reasonably assert the SAME task always maps to the SAME
suggested_agents across a real model (that's what the old script
actually measured, by hitting a live LLM 5 times and eyeballing the
labels) -- with a mocked LLM we control the output directly, so instead
this asserts the schema classify() promises callers, per-field, plus the
same "not every task should collapse to 'responder'" signal the old
test's diversity check was actually protecting against, restated as a
deterministic assertion over canned-but-varied responses.
"""
import json

from eo.inspector import classify, VALID_PATHS
import eo.inspector as inspector  # noqa: F401  (ensures mock_llm patches this module)

TASKS = [
    "Write a Python function that reverses a linked list.",
    "Research the pros and cons of three different caching strategies and summarize them.",
    "Review this pull request for style and correctness issues.",
    "Fact-check this paragraph about the history of the printing press.",
    "Design a simple flowchart showing our user signup process.",
]

CANNED_RESPONSES = [
    json.dumps({
        "path": "direct", "directed_task_type": None, "confidence": 0.9,
        "suggested_agents": ["implementer"], "reasoning": "single coding task",
        "domain": None, "execution_order": ["implementer"], "parallel_groups": [],
    }),
    json.dumps({
        "path": "adaptive", "directed_task_type": None, "confidence": 0.7,
        "suggested_agents": ["researcher", "writer"], "reasoning": "needs research + synthesis",
        "domain": None, "execution_order": ["researcher", "writer"], "parallel_groups": [],
    }),
    json.dumps({
        "path": "fixed", "directed_task_type": "review", "confidence": 0.85,
        "suggested_agents": ["verifier"], "reasoning": "a directed review task",
        "domain": None, "execution_order": ["verifier"], "parallel_groups": [],
    }),
    json.dumps({
        "path": "direct", "directed_task_type": None, "confidence": 0.6,
        "suggested_agents": ["fact_checker"], "reasoning": "single fact-check task",
        "domain": None, "execution_order": ["fact_checker"], "parallel_groups": [],
    }),
    json.dumps({
        "path": "direct", "directed_task_type": None, "confidence": 0.8,
        "suggested_agents": ["diagram_designer"], "reasoning": "single diagramming task",
        "domain": None, "execution_order": ["diagram_designer"], "parallel_groups": [],
    }),
]


def test_output_schema_uses_path_not_tier(mock_llm):
    """Regression guard for the exact bug the old test would have hit:
    'tier' must be absent, 'path' must be present and valid."""
    mock_llm.set_response(CANNED_RESPONSES[0])

    result = classify(TASKS[0])

    assert "tier" not in result, (
        "Migration Part 12 §8.2/§8.4 replaced 'tier' with 'path' -- a "
        "caller still reading result['tier'] would KeyError"
    )
    assert result["path"] in VALID_PATHS


def test_classifier_produces_more_than_one_distinct_role_label_across_varied_tasks(mock_llm):
    mock_llm.set_sequence(CANNED_RESPONSES)

    all_agents_seen = set()
    for task in TASKS:
        result = classify(task)
        all_agents_seen.update(result["suggested_agents"])

    assert all_agents_seen != {"responder"}, (
        "every task classified to only 'responder' would suggest the "
        "prompt/parsing isn't taking effect"
    )
    assert len(all_agents_seen) > 1


def test_every_classification_has_the_full_expected_shape(mock_llm):
    mock_llm.set_sequence(CANNED_RESPONSES)

    for task in TASKS:
        result = classify(task)
        for field in ("path", "directed_task_type", "confidence", "suggested_agents",
                      "reasoning", "domain", "execution_order", "parallel_groups"):
            assert field in result, f"missing '{field}' in classify() output for: {task}"
        assert isinstance(result["confidence"], float)
        assert 0.0 <= result["confidence"] <= 1.0
        assert isinstance(result["suggested_agents"], list)


def test_raises_valueerror_on_invalid_path(mock_llm):
    import pytest

    mock_llm.set_json_response({
        "path": "not_a_real_path", "directed_task_type": None, "confidence": 0.5,
        "suggested_agents": [], "reasoning": "bad output",
    })

    with pytest.raises(ValueError):
        classify("some task")
