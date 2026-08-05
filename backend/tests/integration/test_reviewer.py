"""
tests/integration/test_reviewer.py — mocked rebuild of the old
tests/test_reviewer.py.

run_reviewer() fans out to 3 (or 5, expanded) genuinely parallel workers
(ThreadPoolExecutor) that each call generate_text() independently, then
merges them via agents.review_aggregator.aggregate_reviews(). Since
mock_llm's LLMPatcher patches generate_text everywhere it's already
bound, every worker thread sees the SAME mocked function -- exactly what
we want here: assert the merge/shape contract, not per-worker variation
(review_aggregator.py's own dedup logic is out of scope, covered
separately if/when it gets its own unit test).
"""
import agents.reviewer as reviewer  # noqa: F401  (ensures mock_llm patches this module)
from memory.bus import write, read, KEYS

SUBMITTED_CODE = {
    "todo_storage": (
        "def add_todo(todos, item):\n"
        "    todos.append(item)\n"
        "    return todos\n"
    ),
    "todo_api": (
        "def get_todo(todos, index):\n"
        "    return todos[index]\n"
    ),
}


def test_reviewer_returns_expected_shape_and_writes_the_bus(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    mock_llm.set_json_response({
        "issues": [
            {"module": "todo_api", "severity": "critical", "description": "no bounds check"},
        ],
        "summary": "One critical issue found.",
    })

    notes = reviewer.run_reviewer(session_id="sess_test")

    assert mock_llm.mock.call_count == 3, "default (non-expanded) pool is 3 workers"
    assert "issues" in notes and isinstance(notes["issues"], list)
    assert "summary" in notes
    assert "next_destination" in notes, "Migration Part 11 §2 field must always be present"
    assert read(KEYS["review_notes"]) == notes


def test_expanded_pool_uses_five_workers(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    mock_llm.set_json_response({"issues": [], "summary": "clean"})

    reviewer.run_reviewer(session_id="sess_test", expanded=True)

    assert mock_llm.mock.call_count == 5


def test_skips_gracefully_with_no_submitted_code(mock_llm):
    """Bug-fix regression guard: a plan can staff "verifier" without ever
    staffing "implementer" (e.g. a hardware/embedded task) -- this must
    fail soft with an empty, well-shaped review, not raise or crash the
    whole run."""
    notes = reviewer.run_reviewer(session_id="sess_test")

    assert mock_llm.mock.call_count == 0
    assert notes["issues"] == []
    assert notes["next_destination"] is None


def test_worker_output_that_is_not_valid_json_is_discarded_not_crashed(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    mock_llm.set_response("not json at all, the model ignored instructions")

    notes = reviewer.run_reviewer(session_id="sess_test")

    assert notes["issues"] == []
    assert "summary" in notes
