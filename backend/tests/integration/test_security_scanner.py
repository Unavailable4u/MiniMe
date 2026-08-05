"""
tests/integration/test_security_scanner.py — mocked rebuild of the old
tests/test_security_scanner.py.

run(...) fans one module out per worker (round-robin across a 5-worker
pool, 8 if expanded), each an independent generate_text() call -- mocked
the same "every worker thread sees the same patched function" way as
agents/reviewer.py's pool (see tests/integration/test_reviewer.py).
"""
import agents.security_scanner as security_scanner  # noqa: F401
from memory.bus import write, read, KEYS

FIXED_CODE = {
    "config_loader": {
        "language": "python",
        "code": "API_KEY = 'sk-live-abc123hardcodedsecret'\n",
    },
    "todo_storage": {
        "language": "python",
        "code": "def add_todo(todos, item):\n    todos.append(item)\n",
    },
}


def test_returns_findings_shape_per_module(mock_llm):
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_llm.set_json_response({
        "findings": [{"severity": "critical", "description": "hardcoded secret"}],
    })

    results = security_scanner.run(session_id="sess_test")

    assert set(results.keys()) == set(FIXED_CODE.keys())
    for module_result in results.values():
        assert "findings" in module_result
    assert read(KEYS["security_scan_results"]) == results


def test_default_pool_is_five_workers(mock_llm):
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_llm.set_json_response({"findings": []})

    security_scanner.run(session_id="sess_test")

    # Only 2 modules here, so at most 2 workers actually get called even
    # though the pool is sized for 5 -- confirms the round-robin
    # assignment doesn't over-call.
    assert mock_llm.mock.call_count == 2


def test_worker_error_is_captured_per_module_not_raised(mock_llm):
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_llm.raise_on_call(RuntimeError("all providers exhausted"))

    results = security_scanner.run(session_id="sess_test")

    for module_result in results.values():
        assert module_result.get("error")
        assert module_result.get("findings") == []


def test_returns_empty_dict_with_no_code_to_scan(mock_llm):
    results = security_scanner.run(session_id="sess_test")

    assert results == {}
    assert mock_llm.mock.call_count == 0
    assert read(KEYS["security_scan_results"]) == {}


def test_prefers_fixed_code_over_submitted_code(mock_llm):
    write(KEYS["submitted_code"], {"only_submitted": {"language": "python", "code": "x = 1\n"}})
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_llm.set_json_response({"findings": []})

    results = security_scanner.run(session_id="sess_test")

    assert "only_submitted" not in results
    assert set(results.keys()) == set(FIXED_CODE.keys())
