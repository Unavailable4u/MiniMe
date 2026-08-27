"""
tests/unit/test_memory_bus.py — Part 5 checklist item, rebuilt around
the FakeRedis fixture (was a print-and-eyeball script hitting real
Upstash; see the old tests/test_memory_bus.py).

Covers memory/bus.py's basic contract: write/read round-trips any
JSON-serializable value, read() returns the given default for a missing
key instead of raising, read_many() batches multiple keys through one
MGET and keys its result by the ORIGINAL (un-namespaced) key names, and
delete() actually removes a key rather than leaving an empty record.
"""
from memory.bus import KEYS, delete, read, read_many, write, write_plan


def test_write_read_round_trips_a_string():
    write(KEYS["original_idea"], "A simple todo list app")
    assert read(KEYS["original_idea"]) == "A simple todo list app"


def test_write_read_round_trips_a_dict():
    plan = {"features": ["add task", "delete task"], "cycle_goal": "build add task"}
    write(KEYS["current_plan"], plan)
    assert read(KEYS["current_plan"]) == plan


def test_read_missing_key_returns_default():
    assert read("some_key_never_written") is None
    assert read("some_key_never_written", default="fallback") == "fallback"


def test_read_many_batches_and_keys_by_original_name():
    write(KEYS["original_idea"], "idea text")
    write(KEYS["current_plan"], {"features": ["a"]})
    # KEYS["test_code"] deliberately never written -- should come back
    # as the default, not raise or get dropped from the result dict.
    result = read_many(
        [KEYS["original_idea"], KEYS["current_plan"], KEYS["test_code"]],
        default="MISSING",
    )
    assert result == {
        "original_idea": "idea text",
        "current_plan": {"features": ["a"]},
        "test_code": "MISSING",
    }


def test_read_many_empty_list_returns_empty_dict():
    assert read_many([]) == {}


def test_delete_removes_key_entirely():
    write("some_temp_key", {"x": 1})
    assert read("some_temp_key") is not None
    delete("some_temp_key")
    assert read("some_temp_key") is None
    # Distinct from write(key, None): a deleted key should behave
    # exactly like one that was never written, i.e. honor `default`.
    assert read("some_temp_key", default="gone") == "gone"


# --- Patch B11 -- Plan/Guide Changelog Versioning ---------------------


def test_write_plan_still_overwrites_current_plan_in_full():
    plan_v1 = {"features": ["a", "b"], "target_feature": "a", "cycle_goal": "build a"}
    plan_v2 = {"features": ["a", "b"], "target_feature": "b", "cycle_goal": "build b"}
    write_plan(plan_v1)
    write_plan(plan_v2)
    # current_plan is still the single full-snapshot key every existing
    # reader (structure_architect, report_writer, get_tasks(), ...)
    # expects -- write_plan() must not change that contract.
    assert read(KEYS["current_plan"]) == plan_v2


def test_write_plan_returns_the_plan_unchanged():
    plan = {"features": ["a"], "target_feature": "a", "cycle_goal": "build a"}
    assert write_plan(plan) == plan


def test_write_plan_first_call_logs_initial_plan():
    plan = {"features": ["a"], "target_feature": "a", "cycle_goal": "build a"}
    write_plan(plan, why="cycle 1 plan")
    changelog = read(KEYS["plan_changelog"])
    assert len(changelog) == 1
    assert changelog[0]["what"] == "Initial plan"
    assert changelog[0]["why"] == "cycle 1 plan"
    assert "at" in changelog[0]


def test_write_plan_appends_diff_entry_not_a_full_snapshot():
    plan_v1 = {"features": ["a", "b"], "target_feature": "a", "cycle_goal": "build a",
               "priorities": ["a", "b"]}
    plan_v2 = {"features": ["a", "b"], "target_feature": "b", "cycle_goal": "build b",
               "priorities": ["a", "b"]}
    write_plan(plan_v1, why="cycle 1 plan")
    write_plan(plan_v2, why="re-plan after prior cycle report")

    changelog = read(KEYS["plan_changelog"])
    assert len(changelog) == 2
    second = changelog[1]
    # A compact delta description, NOT a duplicated full plan dict --
    # the whole point of "git-style supersede-with-diff" over
    # "duplicate-with-full-text".
    assert "target_feature" in second["what"]
    assert "cycle_goal" in second["what"]
    assert second["why"] == "re-plan after prior cycle report"
    assert "features" not in second  # no full plan smuggled into the entry


def test_write_plan_no_detected_change_when_tracked_fields_identical():
    plan = {"features": ["a"], "target_feature": "a", "cycle_goal": "build a",
            "priorities": ["a"]}
    write_plan(plan)
    write_plan(dict(plan))  # a fresh, equal-valued dict -- same content
    changelog = read(KEYS["plan_changelog"])
    assert changelog[1]["what"] == "No detected change"


def test_write_plan_changelog_is_bounded_not_unbounded():
    from memory.bus import _PLAN_CHANGELOG_MAX_ENTRIES

    for i in range(_PLAN_CHANGELOG_MAX_ENTRIES + 10):
        write_plan({"features": [str(i)], "target_feature": str(i),
                    "cycle_goal": f"build {i}"})
    changelog = read(KEYS["plan_changelog"])
    assert len(changelog) == _PLAN_CHANGELOG_MAX_ENTRIES
    # Oldest entries roll off the front -- newest survives.
    assert changelog[-1]["what"].count(str(
        _PLAN_CHANGELOG_MAX_ENTRIES + 9)) >= 1
