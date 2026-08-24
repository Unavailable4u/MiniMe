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
from memory.bus import KEYS, delete, read, read_many, write


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
