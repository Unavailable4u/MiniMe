"""
tests/integration/test_fixer_pool.py — mocked rebuild of the old
tests/test_fixer_pool.py.

Bug caught by writing this test: the old script called
`fixed_code = run_fixer_pool()` and read `fixed_code.get("todo_api")`
directly. Per this module's own Migration Part 11 §2 docstring,
run_fixer_pool() no longer returns the bare {module_name: {...}} dict --
it returns {"fixed_code": {...}, "next_destination": ...} so a real
module named "next_destination" could never collide with the metadata
key. The memory-bus write (KEYS["fixed_code"]) is unaffected and still
gets the plain modules dict, but any DIRECT caller of this function
(exactly what the old test did) silently got the wrong shape back. This
test asserts the current, correct contract instead.
"""
import agents.fixer_pool as fixer_pool  # noqa: F401  (ensures mock_llm patches this module)
from memory.bus import write, read, KEYS

SUBMITTED_CODE = {
    "todo_storage": {
        "language": "python",
        "code": "def add_todo(todos, item):\n    todos.append(item)\n    return todos\n",
    },
    "todo_api": {
        "language": "python",
        "code": "def get_todo(todos, index):\n    return todos[index]\n",
    },
}

REVIEW_NOTES = {
    "issues": [
        {"module": "todo_api", "severity": "critical", "description": "no bounds check"},
    ],
    "summary": "One critical issue.",
}


def test_return_shape_is_fixed_code_plus_next_destination_not_a_bare_dict(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    write(KEYS["review_notes"], REVIEW_NOTES)
    mock_llm.set_json_response({
        "todo_api": {"language": "python", "code": "def get_todo(todos, index):\n    return todos[index] if 0 <= index < len(todos) else None\n"},
    })

    result = fixer_pool.run_fixer_pool()

    assert set(result.keys()) == {"fixed_code", "next_destination"}, (
        "run_fixer_pool() must return {'fixed_code': ..., 'next_destination': ...}, "
        "not the bare modules dict a pre-Migration-Part-11 caller would expect"
    )
    assert set(result["fixed_code"].keys()) == set(SUBMITTED_CODE.keys())


def test_bus_write_stays_the_plain_modules_dict(mock_llm):
    """The memory-bus write is explicitly unaffected by the Part 11 §2
    return-shape change -- downstream readers (sandbox_tester.py etc.)
    must keep seeing the plain {module_name: {...}} dict."""
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    write(KEYS["review_notes"], REVIEW_NOTES)
    mock_llm.set_json_response({
        "todo_api": {"language": "python", "code": "def get_todo(todos, index):\n    return todos[index]\n"},
    })

    result = fixer_pool.run_fixer_pool()

    on_bus = read(KEYS["fixed_code"])
    assert on_bus == result["fixed_code"]
    assert "next_destination" not in on_bus


def test_normalizes_a_bare_string_entry_into_language_code_shape(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    write(KEYS["review_notes"], REVIEW_NOTES)
    # Model returned a bare code string for todo_api instead of the
    # {"language", "code"} object the prompt asked for.
    mock_llm.set_json_response({
        "todo_api": "def get_todo(todos, index):\n    return todos[index]\n",
    })

    result = fixer_pool.run_fixer_pool()

    fixed = result["fixed_code"]["todo_api"]
    assert isinstance(fixed, dict)
    assert fixed["language"] == "python"
    assert "def get_todo" in fixed["code"]


def test_rejects_syntactically_invalid_python_and_keeps_the_original(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    write(KEYS["review_notes"], REVIEW_NOTES)
    mock_llm.set_json_response({
        "todo_api": {"language": "python", "code": "def get_todo(todos, index)\n    return todos[index]\n"},  # missing colon
    })

    result = fixer_pool.run_fixer_pool()

    assert result["fixed_code"]["todo_api"] == SUBMITTED_CODE["todo_api"], (
        "a syntactically broken 'fix' must fall back to the original module, not propagate"
    )


def test_raises_missing_dependency_when_no_submitted_code(mock_llm):
    from eo.errors import MissingDependencyError
    import pytest

    with pytest.raises(MissingDependencyError):
        fixer_pool.run_fixer_pool()

    assert mock_llm.mock.call_count == 0
