"""
tests/unit/test_planner.py — rebuilt around FakeRedis (was a
print-and-write-only script; see the old tests/test_planner.py).

Scoped deliberately to the bus-level contract agents/idea_planner.py's
run() depends on for its inputs -- original_idea, latest_report, and
feature_status via a single batched read_many() call -- WITHOUT
actually invoking run() itself (that requires an LLM call and belongs
in the mocked-LLM agent-integration suite, alongside the other agent
per-role JSON-shape checks).
"""
from memory.bus import KEYS, read, read_many, write


def test_original_idea_round_trips():
    idea = "A simple to-do list web app where users can add, complete, and delete tasks"
    write(KEYS["original_idea"], idea)
    assert read(KEYS["original_idea"]) == idea


def test_planner_inputs_batch_read_with_sensible_defaults_when_unset():
    # Mirrors exactly the read_many() call idea_planner.run() makes
    # before it ever touches the LLM: on a fresh app_slug, none of
    # these three keys exist yet.
    result = read_many(
        [KEYS["original_idea"], KEYS["latest_report"], KEYS["feature_status"]],
        default=None,
    )
    assert result[KEYS["original_idea"]] is None
    assert result[KEYS["latest_report"]] is None
    # idea_planner.run() itself does `feature_status = ... or {}` on top
    # of this -- confirm the raw bus-level default really is None, not
    # already {}, so that fallback is doing real work.
    assert result[KEYS["feature_status"]] is None


def test_planner_inputs_batch_read_reflects_prior_writes():
    write(KEYS["original_idea"], "A todo app")
    write(KEYS["latest_report"], {"summary": "cycle 1 done"})
    write(KEYS["feature_status"], {"add task": "done", "delete task": "in_progress"})

    result = read_many(
        [KEYS["original_idea"], KEYS["latest_report"], KEYS["feature_status"]],
        default=None,
    )
    assert result[KEYS["original_idea"]] == "A todo app"
    assert result[KEYS["latest_report"]] == {"summary": "cycle 1 done"}
    assert result[KEYS["feature_status"]] == {"add task": "done", "delete task": "in_progress"}
