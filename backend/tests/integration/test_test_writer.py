"""
tests/integration/test_test_writer.py — mocked rebuild of the old
tests/test_test_writer.py.

Covers the JSON-shape contract (one string of assert-style code per
submitted module) plus the hard backstop this module's own docstring
describes: generated test code with a bare/broad `except` clause gets
dropped entirely, since it could silently swallow the module's own
AssertionError and turn a genuinely failing test into a falsely
"passing" one.
"""
import agents.test_writer as test_writer  # noqa: F401  (ensures mock_llm patches this module)
from memory.bus import write, read, KEYS
from eo.errors import MissingDependencyError

SUBMITTED_CODE = {
    "math_utils": {
        "language": "python",
        "code": "def add(a, b):\n    return a + b\n\ndef is_even(n):\n    return n % 2 == 0\n",
    },
}


def test_returns_test_code_for_every_submitted_module(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    mock_llm.set_json_response({
        "math_utils": "assert add(2, 3) == 5\nassert is_even(4) is True\n",
    })

    result = test_writer.run()

    assert set(result.keys()) == {"math_utils"}
    assert "add(" in result["math_utils"]
    assert read(KEYS["test_code"]) == result


def test_drops_modules_with_a_bare_except_clause(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    mock_llm.set_json_response({
        "math_utils": (
            "try:\n"
            "    assert add(2, 3) == 5\n"
            "except:\n"
            "    pass\n"
        ),
    })

    result = test_writer.run()

    assert "math_utils" not in result, (
        "a bare `except:` can swallow the test's own AssertionError -- "
        "this must be dropped, not kept"
    )


def test_drops_modules_with_a_broad_except_exception_clause(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    mock_llm.set_json_response({
        "math_utils": "try:\n    assert add(2, 3) == 5\nexcept Exception:\n    pass\n",
    })

    result = test_writer.run()

    assert "math_utils" not in result


def test_keeps_a_specific_except_clause(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    mock_llm.set_json_response({
        "math_utils": "try:\n    add(2, 3)\nexcept ValueError:\n    pass\n",
    })

    result = test_writer.run()

    assert "math_utils" in result, "a specific except clause (not bare/broad) must be kept"


def test_unparseable_json_yields_empty_test_code_not_a_crash(mock_llm):
    write(KEYS["submitted_code"], SUBMITTED_CODE)
    mock_llm.set_response("not json")

    result = test_writer.run()

    assert result == {}


def test_raises_missing_dependency_when_no_submitted_code(mock_llm):
    import pytest

    with pytest.raises(MissingDependencyError):
        test_writer.run()

    assert mock_llm.mock.call_count == 0
