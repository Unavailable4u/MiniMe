"""
tests/integration/test_report_writer.py — mocked rebuild of the old
tests/test_report_writer.py (which wrote fake data to real Redis and hit
a real LLM, then eyeballed the printed report).

Covers the JSON-shape contract run_report_writer() actually promises
callers: {"text", "summary", "all_tests_passed", "failed_modules",
"target_feature"} -- including the "summary" alias (an explicit bug fix
per this module's own comment: documentation_agent.py and
memory_search.py both read report["summary"], which used to always be
empty since only "text" was ever written).
"""

from memory.bus import write, read, KEYS
import agents.report_writer as report_writer  # noqa: F401  (ensures mock_llm patches this module)

FIXED_CODE = {
    "todo_storage": {"language": "python", "code": "def add_todo(todos, item):\n    todos.append(item)\n"},
    "todo_api": {"language": "python", "code": "def get_todo(todos, index):\n    return todos[index]\n"},
}

TEST_RESULTS_ALL_PASS = {
    "todo_storage": {"passed": True, "stdout": ["ok\n"], "stderr": [], "error": None},
    "todo_api": {"passed": True, "stdout": ["ok\n"], "stderr": [], "error": None},
}

TEST_RESULTS_ONE_FAIL = {
    "todo_storage": {"passed": True, "stdout": ["ok\n"], "stderr": [], "error": None},
    "todo_api": {"passed": False, "stdout": [], "stderr": ["IndexError\n"], "error": "IndexError"},
}

REVIEW_NOTES = {
    "issues": [{"module": "todo_api", "severity": "critical", "description": "no bounds check"}],
    "summary": "One critical bug found and resolved this cycle.",
}


def _seed(test_results):
    write(KEYS["fixed_code"], FIXED_CODE)
    write(KEYS["test_results"], test_results)
    write(KEYS["review_notes"], REVIEW_NOTES)


def test_report_shape_on_a_fully_passing_cycle(mock_llm):
    _seed(TEST_RESULTS_ALL_PASS)
    mock_llm.set_response("Everything built and tested cleanly this cycle. No issues found.")

    report = report_writer.run_report_writer()

    assert mock_llm.mock.call_count == 1
    assert report["text"] == "Everything built and tested cleanly this cycle. No issues found."
    # Bug-fix regression guard: "summary" must alias "text", not be empty.
    assert report["summary"] == report["text"]
    assert report["all_tests_passed"] is True
    assert report["failed_modules"] == []


def test_report_shape_on_a_cycle_with_a_failed_module(mock_llm):
    _seed(TEST_RESULTS_ONE_FAIL)
    mock_llm.set_response("todo_api still fails an index bounds check; everything else is clean.")

    report = report_writer.run_report_writer()

    assert report["all_tests_passed"] is False
    assert report["failed_modules"] == ["todo_api"]


def test_report_is_persisted_to_the_bus(mock_llm):
    _seed(TEST_RESULTS_ALL_PASS)
    mock_llm.set_response("Clean cycle.")

    report = report_writer.run_report_writer()

    assert read(KEYS["latest_report"]) == report


def test_falls_back_to_submitted_code_when_fixed_code_is_absent(mock_llm):
    """Bug-fix regression guard: report_writer should still produce a
    real report from submitted_code alone when the Fixer Pool never ran
    (e.g. review found nothing to fix), not raise."""
    write(KEYS["submitted_code"], FIXED_CODE)
    write(KEYS["test_results"], TEST_RESULTS_ALL_PASS)
    write(KEYS["review_notes"], {"issues": [], "summary": "Nothing to fix."})
    mock_llm.set_response("Built from submitted_code directly; no fixer pass needed.")

    report = report_writer.run_report_writer()

    assert report["all_tests_passed"] is True


def test_raises_missing_dependency_when_no_code_ran_at_all(mock_llm):
    from eo.errors import MissingDependencyError
    import pytest

    with pytest.raises(MissingDependencyError):
        report_writer.run_report_writer()

    assert mock_llm.mock.call_count == 0, "should fail before ever reaching the LLM"
