"""
tests/integration/test_security_scanner.py — mocked rebuild of the old
tests/test_security_scanner.py, updated for B2 (Gitleaks/Semgrep via
agents/static_scan.py now run before the LLM call).

run(...) fans one module out per worker (round-robin across a 5-worker
pool, 8 if expanded), each an independent generate_text() call -- mocked
the same "every worker thread sees the same patched function" way as
agents/reviewer.py's pool (see tests/integration/test_reviewer.py).

New this pass: every module now goes through run_static_scan() FIRST
(mocked via mock_static_scan — see tests/conftest.py — so no test spins up
a real E2B sandbox). The LLM call is gated on that step returning at least
one finding; a module the tools found clean never reaches generate_text()
at all.
"""
from agents import security_scanner
from memory.bus import KEYS, read, write

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

TOOL_FINDINGS = [{"severity": "critical", "description": "hardcoded secret", "source": "gitleaks"}]


def test_returns_findings_shape_per_module(mock_static_scan, mock_llm):
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_static_scan.set_findings(TOOL_FINDINGS)
    mock_llm.set_json_response({
        "findings": [{"severity": "critical", "description": "hardcoded secret, explained"}],
    })

    results = security_scanner.run(session_id="sess_test")

    assert set(results.keys()) == set(FIXED_CODE.keys())
    for module_result in results.values():
        assert "findings" in module_result
        assert "tool_error" in module_result
    assert read(KEYS["security_scan_results"]) == results


def test_clean_module_skips_the_llm_call_entirely(mock_static_scan, mock_llm):
    """A module the tools found nothing in should never reach
    generate_text() — the LLM has nothing to summarize."""
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_static_scan.set_findings([])  # tools report clean for every module

    results = security_scanner.run(session_id="sess_test")

    for module_result in results.values():
        assert module_result["findings"] == []
        assert module_result.get("tool_error") is None
    assert mock_llm.mock.call_count == 0


def test_default_pool_is_five_workers(mock_static_scan, mock_llm):
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_static_scan.set_findings(TOOL_FINDINGS)  # give the LLM something to summarize
    mock_llm.set_json_response({"findings": TOOL_FINDINGS})

    security_scanner.run(session_id="sess_test")

    # Only 2 modules here, so at most 2 workers actually get called even
    # though the pool is sized for 5 -- confirms the round-robin
    # assignment doesn't over-call.
    assert mock_llm.mock.call_count == 2


def test_llm_failure_falls_back_to_raw_tool_findings(mock_static_scan, mock_llm):
    """Real, tool-confirmed findings shouldn't disappear just because the
    summarization call failed -- see security_scanner.py's _scan_one()
    comment on why the except branch keeps tool_findings rather than
    returning an empty list."""
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_static_scan.set_findings(TOOL_FINDINGS)
    mock_llm.raise_on_call(RuntimeError("all providers exhausted"))

    results = security_scanner.run(session_id="sess_test")

    for module_result in results.values():
        assert module_result.get("error")
        assert module_result.get("findings") == TOOL_FINDINGS


def test_tool_error_is_captured_without_raising(mock_static_scan, mock_llm):
    """run_static_scan() itself never raises (sandbox/tool failure
    degrades to an empty findings list + tool_error) -- confirm that
    shape survives through to the module result untouched, and that an
    empty-findings tool_error still skips the LLM call same as a clean scan."""
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_static_scan.raise_via_tool_error("sandbox failed to start: quota exceeded")

    results = security_scanner.run(session_id="sess_test")

    for module_result in results.values():
        assert module_result["findings"] == []
        assert "quota exceeded" in module_result["tool_error"]
    assert mock_llm.mock.call_count == 0


def test_returns_empty_dict_with_no_code_to_scan(mock_static_scan, mock_llm):
    results = security_scanner.run(session_id="sess_test")

    assert results == {}
    assert mock_static_scan.mock.call_count == 0
    assert mock_llm.mock.call_count == 0
    assert read(KEYS["security_scan_results"]) == {}


def test_prefers_fixed_code_over_submitted_code(mock_static_scan, mock_llm):
    write(KEYS["submitted_code"], {"only_submitted": {"language": "python", "code": "x = 1\n"}})
    write(KEYS["fixed_code"], FIXED_CODE)
    mock_static_scan.set_findings([])

    results = security_scanner.run(session_id="sess_test")

    assert "only_submitted" not in results
    assert set(results.keys()) == set(FIXED_CODE.keys())
