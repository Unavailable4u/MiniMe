"""
tests/unit/test_eo_code_findings.py -- W8.3a.

Covers eo/code_findings.py (turning sandbox_tester / security_scanner
output into {path, line, severity, message, source}), the
GET /api/workspaces/{ws}/code/findings route, and the hook in
api/task_runner.py's _write_code_files() that stores findings between
the file write and the CODE_FILE_UPDATED event.

Priorities, worst-silent-failure first:

  1. A finding must never point at a line that only exists in the
     appended generated tests (sandbox_tester runs `code.rstrip() + marker
     + test_code`; those tests are never saved, so a line past the end of
     the module is a line that isn't in the file). Checked both with
     hand-built IPython tracebacks and with REAL Python tracebacks from
     exec()ing the same combined text the sandbox runs, so the marker
     arithmetic is tested against Python's own line numbering rather than
     against my idea of it.
  2. The e2b ExecutionError arrives as str() of a dataclass -- a repr with
     ANSI codes and newlines in escaped literal form -- so the parser is
     tested against exactly that spelling, not just a tidy traceback.
  3. Infrastructure failures ("Sandbox failed to run", "No code found")
     must not become findings about the user's code.
  4. The write-back is ordered (findings stored BEFORE the event) and
     fail-open (a findings failure never skips the event or raises).

DB access uses the FakeCursor/FakeCursorContext convention from
test_eo_workspace_code_files.py.
"""
import traceback as tb_module

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.routes.code as code_routes
from api import task_runner
from api.deps import require_auth
from eo import code_findings
from memory.bus import KEYS, write

TEST_MARKER = "\n\n# --- Generated tests (Test Writer) ---\n"   # same text sandbox_tester appends


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def _ipython_traceback(frames, exc_name="NameError"):
    """An IPython-style traceback with ANSI colour codes, the way the e2b
    kernel reports it. `frames`: [(line, func_or_None), ...] outermost
    first -- a func name renders the `, in func(...)` suffix IPython adds
    for a frame inside a function defined in the same cell."""
    out = [
        "\x1b[0;31m" + "-" * 75 + "\x1b[0m",
        f"\x1b[0;31m{exc_name}\x1b[0m                                      Traceback (most recent call last)",
    ]
    for line, func in frames:
        head = f"Cell \x1b[0;32mIn[1], line {line}\x1b[0m"
        if func:
            head += f", in \x1b[0;36m{func}\x1b[0;34m(x)\x1b[0m"
        out.append(head)
        out.append(f"\x1b[1;32m    {line}\x1b[0m     some_source_line()")
    out.append(f"\x1b[0;31m{exc_name}\x1b[0m: boom")
    return "\n".join(out)


def _execution_error_str(name, value, traceback_text):
    """str(ExecutionError(...)) -- e2b's ExecutionError is a dataclass, so
    this IS its repr, escapes and all."""
    return f"ExecutionError(name={name!r}, value={value!r}, traceback={traceback_text!r})"


def _failed(error=None, stderr="", stdout=""):
    return {"passed": False, "stdout": stdout, "stderr": stderr, "error": error}


CODE_20 = "\n".join(f"x{i} = {i}" for i in range(1, 21)) + "\n\n\n"   # 20 real lines + trailing blanks


# ---------------------------------------------------------------------
# line counting
# ---------------------------------------------------------------------

def test_code_line_count_ignores_trailing_blank_lines():
    assert code_findings._code_line_count("a\nb\n\n\n") == 2
    assert code_findings._code_line_count(CODE_20) == 20


def test_code_line_count_of_empty_or_non_string_is_zero():
    assert code_findings._code_line_count("") == 0
    assert code_findings._code_line_count("   \n ") == 0
    assert code_findings._code_line_count(None) == 0


# ---------------------------------------------------------------------
# line_from_traceback
# ---------------------------------------------------------------------

def test_traceback_line_from_ipython_cell_frame_with_ansi_codes():
    text = _ipython_traceback([(12, None)])
    assert code_findings.line_from_traceback(text, 20) == 12


def test_traceback_line_survives_the_escaped_repr_spelling():
    # Same traceback, but as it looks inside str(ExecutionError(...)):
    # ESC bytes and newlines turned into literal backslash sequences.
    escaped = repr(_ipython_traceback([(12, None)]))[1:-1]
    assert "\\x1b" in escaped and "\x1b" not in escaped
    assert code_findings.line_from_traceback(escaped, 20) == 12


def test_traceback_takes_the_innermost_frame_in_the_module():
    text = _ipython_traceback([(15, None), (4, "helper")])
    assert code_findings.line_from_traceback(text, 20) == 4


def test_traceback_skips_frames_inside_the_generated_tests():
    # test at line 60 (past a 20-line module) -> app function at line 7
    text = _ipython_traceback([(60, None), (7, "divide")])
    assert code_findings.line_from_traceback(text, 20) == 7


def test_traceback_skips_an_innermost_frame_that_is_in_the_tests():
    text = _ipython_traceback([(7, "divide"), (60, "check")])
    assert code_findings.line_from_traceback(text, 20) == 7


def test_traceback_only_in_generated_tests_gives_no_line():
    assert code_findings.line_from_traceback(_ipython_traceback([(60, None)]), 20) is None


def test_traceback_line_boundaries():
    assert code_findings.line_from_traceback(_ipython_traceback([(20, None)]), 20) == 20
    assert code_findings.line_from_traceback(_ipython_traceback([(21, None)]), 20) is None
    assert code_findings.line_from_traceback(_ipython_traceback([(0, None)]), 20) is None


def test_traceback_ignores_library_frames():
    ipython_lib = (
        "File \x1b[0;32m/usr/lib/python3.12/json/decoder.py:355\x1b[0m, in "
        "\x1b[0;36mJSONDecoder.raw_decode\x1b[0;34m(self, s, idx)\x1b[0m"
    )
    classic_lib = 'File "/usr/lib/python3.12/json/decoder.py", line 355, in raw_decode'
    assert code_findings.line_from_traceback(ipython_lib, 400) is None
    assert code_findings.line_from_traceback(classic_lib, 400) is None
    own = _ipython_traceback([(5, None)])
    assert code_findings.line_from_traceback(own + "\n" + ipython_lib, 400) == 5


def test_traceback_line_from_classic_string_frame():
    text = (
        "Traceback (most recent call last):\n"
        '  File "<string>", line 9, in <module>\n'
        '  File "<string>", line 3, in divide\n'
        "ZeroDivisionError: division by zero\n"
    )
    assert code_findings.line_from_traceback(text, 20) == 3


def test_traceback_garbage_input():
    assert code_findings.line_from_traceback(None, 20) is None
    assert code_findings.line_from_traceback("no frames here", 20) is None
    assert code_findings.line_from_traceback(_ipython_traceback([(5, None)]), 0) is None


# Real Python tracebacks over the exact combined text the sandbox runs.

def _real_traceback_for(code, test_code):
    full_code = code.rstrip() + TEST_MARKER + test_code
    try:
        exec(compile(full_code, "<string>", "exec"), {})
    except BaseException:   # AssertionError included
        return tb_module.format_exc()
    raise AssertionError("expected the combined program to fail")


def test_real_traceback_error_inside_app_code_reports_the_app_line():
    code = "def divide(a, b):\n    return a / b\n"
    text = _real_traceback_for(code, "assert divide(1, 0) == 1\n")
    # frames: generated-test line (past the module), then app line 2
    assert code_findings.line_from_traceback(text, code_findings._code_line_count(code)) == 2


def test_real_traceback_failing_assertion_in_generated_tests_reports_no_line():
    code = "def divide(a, b):\n    return a / b\n"
    text = _real_traceback_for(code, "assert divide(4, 2) == 3\n")
    assert "AssertionError" in text
    assert code_findings.line_from_traceback(text, code_findings._code_line_count(code)) is None


def test_real_traceback_error_at_module_level_of_app_code():
    code = "x = 1\ny = undefined_name\n"
    text = _real_traceback_for(code, "assert x == 1\n")
    assert code_findings.line_from_traceback(text, code_findings._code_line_count(code)) == 2


# ---------------------------------------------------------------------
# line_from_description
# ---------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("python.lang.security.audit.eval at line 12: avoid eval", 12),
    ("Hardcoded secret detected (aws-key) at line 3: AKIA...", 3),
    ("The password is hardcoded on Line 7 of the module", 7),
    ("Unsafe use spanning lines 5-9", 5),
    ("semgrep rule at line ?: something", None),
    ("No line information at all", None),
    ("at line 0: odd", None),
    ("at line 999: past the end of a 20-line file", None),
])
def test_line_from_description(text, expected):
    assert code_findings.line_from_description(text, 20) == expected


def test_line_from_description_uses_only_the_first_mention():
    assert code_findings.line_from_description("at line 999: see also line 4", 20) is None


def test_line_from_description_garbage_input():
    assert code_findings.line_from_description(None, 20) is None
    assert code_findings.line_from_description("at line 3", 0) is None


# ---------------------------------------------------------------------
# findings_from_test_result
# ---------------------------------------------------------------------

def test_passing_result_has_no_findings():
    assert code_findings.findings_from_test_result(
        {"passed": True, "stdout": "ok", "stderr": "", "error": None}, CODE_20) == []


def test_non_dict_result_has_no_findings():
    assert code_findings.findings_from_test_result(None, CODE_20) == []
    assert code_findings.findings_from_test_result("failed", CODE_20) == []


def test_execution_error_repr_becomes_an_error_finding_with_name_value_and_line():
    error = _execution_error_str(
        "NameError", "name 'x' is not defined", _ipython_traceback([(60, None), (7, "helper")]),
    )
    found = code_findings.findings_from_test_result(_failed(error=error), CODE_20)
    assert found == [{
        "line": 7,
        "severity": "error",
        "message": "NameError: name 'x' is not defined",
        "source": "sandbox_test",
    }]


def test_execution_error_with_only_generated_test_frames_is_a_file_level_finding():
    error = _execution_error_str("AssertionError", "", _ipython_traceback([(60, None)], "AssertionError"))
    found = code_findings.findings_from_test_result(_failed(error=error), CODE_20)
    assert found == [{
        "line": None, "severity": "error", "message": "AssertionError", "source": "sandbox_test",
    }]


def test_execution_error_value_with_both_quote_kinds_and_escapes_is_unescaped():
    error = _execution_error_str("ValueError", "bad 'a' and \"b\"\nsecond line", _ipython_traceback([(3, None)]))
    found = code_findings.findings_from_test_result(_failed(error=error), CODE_20)
    assert found[0]["message"] == "ValueError: bad 'a' and \"b\" second line"
    assert found[0]["line"] == 3


def test_unparseable_error_text_still_yields_a_finding():
    found = code_findings.findings_from_test_result(
        _failed(error="RuntimeError: kernel died unexpectedly"), CODE_20)
    assert len(found) == 1
    assert found[0]["severity"] == "error"
    assert "RuntimeError: kernel died unexpectedly" in found[0]["message"]
    assert found[0]["line"] is None


def test_line_falls_back_to_stderr_when_the_error_has_none():
    error = _execution_error_str("RuntimeError", "x", "no frames")
    stderr = _ipython_traceback([(9, None)])
    found = code_findings.findings_from_test_result(_failed(error=error, stderr=stderr), CODE_20)
    assert found[0]["line"] == 9


def test_stderr_only_failure_is_a_warning_headed_by_the_warning_line():
    stderr = (
        "/tmp/ipykernel_1/123.py:5: DeprecationWarning: foo is deprecated\n"
        "  foo()\n"
    )
    found = code_findings.findings_from_test_result(_failed(stderr=stderr), CODE_20)
    assert found == [{
        "line": None,
        "severity": "warning",
        "message": "/tmp/ipykernel_1/123.py:5: DeprecationWarning: foo is deprecated",
        "source": "sandbox_test",
    }]


@pytest.mark.parametrize("result", [
    _failed(error="Sandbox failed to run: connection reset"),
    _failed(error="Unexpected module data shape: int"),
    _failed(stderr="No code found for this module."),
    _failed(),   # failed with nothing to say
])
def test_infrastructure_failures_are_not_findings_about_the_code(result):
    assert code_findings.findings_from_test_result(result, CODE_20) == []


def test_test_finding_message_is_one_line_and_bounded():
    value = "first\n\n   second\t" + "z" * 2000
    error = _execution_error_str("ValueError", value, _ipython_traceback([(2, None)]))
    msg = code_findings.findings_from_test_result(_failed(error=error), CODE_20)[0]["message"]
    assert "\n" not in msg and "\t" not in msg and "  " not in msg
    assert len(msg) <= code_findings.MAX_MESSAGE_LENGTH
    assert msg.startswith("ValueError: first second")


# ---------------------------------------------------------------------
# findings_from_scan_result
# ---------------------------------------------------------------------

def test_scan_findings_map_severity_source_and_line():
    result = {"findings": [
        {"severity": "critical", "description": "Hardcoded secret detected (aws) at line 3: AKIA", "source": "gitleaks"},
        {"severity": "moderate", "description": "eval-injection at line 12: avoid eval", "source": "semgrep"},
        {"severity": "minor", "description": "Consider pinning dependencies"},
    ]}
    assert code_findings.findings_from_scan_result(result, CODE_20) == [
        {"line": 3, "severity": "error", "message": "Hardcoded secret detected (aws) at line 3: AKIA", "source": "gitleaks"},
        {"line": 12, "severity": "warning", "message": "eval-injection at line 12: avoid eval", "source": "semgrep"},
        {"line": None, "severity": "info", "message": "Consider pinning dependencies", "source": "security_scan"},
    ]


def test_scan_unknown_or_missing_severity_is_a_warning_not_a_dropped_finding():
    result = {"findings": [
        {"severity": "catastrophic", "description": "odd one"},
        {"description": "no severity at all"},
    ]}
    found = code_findings.findings_from_scan_result(result, CODE_20)
    assert [f["severity"] for f in found] == ["warning", "warning"]


def test_scan_severity_and_source_are_case_insensitive_and_trimmed():
    result = {"findings": [{"severity": " CRITICAL ", "description": "x", "source": " Semgrep "}]}
    found = code_findings.findings_from_scan_result(result, CODE_20)
    assert found[0]["severity"] == "error"
    assert found[0]["source"] == "semgrep"


def test_scan_line_past_the_end_of_the_file_is_dropped_not_kept():
    result = {"findings": [{"severity": "minor", "description": "bad at line 500: x"}]}
    assert code_findings.findings_from_scan_result(result, CODE_20)[0]["line"] is None


def test_scan_skips_malformed_findings_and_tool_errors():
    result = {
        "findings": ["a string", None, {"severity": "minor"}, {"description": 5}, {"description": "   "},
                     {"severity": "minor", "description": "kept"}],
        "tool_error": "gitleaks install failed",
        "error": "LLM failed",
    }
    found = code_findings.findings_from_scan_result(result, CODE_20)
    assert [f["message"] for f in found] == ["kept"]


def test_scan_non_dict_or_missing_findings():
    assert code_findings.findings_from_scan_result(None, CODE_20) == []
    assert code_findings.findings_from_scan_result({"findings": "nope"}, CODE_20) == []
    assert code_findings.findings_from_scan_result({"tool_error": "x"}, CODE_20) == []


# ---------------------------------------------------------------------
# build_findings
# ---------------------------------------------------------------------

def _modules():
    return {
        "app": {"path": "src/app.py", "code": CODE_20},
        "util": {"path": "src/util.py", "code": "def f():\n    return 1\n"},
    }


def test_build_findings_attaches_the_path_and_combines_tests_with_scans():
    tests = {"app": _failed(error=_execution_error_str("KeyError", "'k'", _ipython_traceback([(60, None), (8, "g")])))}
    scans = {"app": {"findings": [{"severity": "critical", "description": "secret at line 2: x", "source": "gitleaks"}]}}
    found = code_findings.build_findings(_modules(), tests, scans)
    # both are "error", so the sort (stable) keeps producer order: tests, then scans
    assert found == [
        {"path": "src/app.py", "line": 8, "severity": "error", "message": "KeyError: 'k'", "source": "sandbox_test"},
        {"path": "src/app.py", "line": 2, "severity": "error", "message": "secret at line 2: x", "source": "gitleaks"},
    ]


def test_build_findings_with_no_results_is_empty():
    assert code_findings.build_findings(_modules(), None, None) == []
    assert code_findings.build_findings(_modules(), {}, {}) == []
    assert code_findings.build_findings({}, {"app": _failed(error="x")}, None) == []


def test_build_findings_only_reports_for_the_given_modules():
    tests = {"other_module": _failed(error="RuntimeError: nope")}
    assert code_findings.build_findings(_modules(), tests, None) == []


def test_build_findings_collapses_exact_duplicates_only():
    scans = {"app": {"findings": [
        {"severity": "minor", "description": "same at line 4: m", "source": "semgrep"},
        {"severity": "minor", "description": "same at line 4: m", "source": "semgrep"},
        {"severity": "minor", "description": "same at line 4: m", "source": "gitleaks"},   # different source
        {"severity": "minor", "description": "same at line 5: m", "source": "semgrep"},    # different line
    ]}}
    found = code_findings.build_findings(_modules(), None, scans)
    assert len(found) == 3


def test_build_findings_orders_most_severe_first_and_cap_drops_least_severe(monkeypatch):
    monkeypatch.setattr(code_findings, "MAX_FINDINGS_PER_FILE", 2)
    scans = {"app": {"findings": [
        {"severity": "minor", "description": "info one"},
        {"severity": "moderate", "description": "warn one"},
        {"severity": "critical", "description": "error one"},
        {"severity": "minor", "description": "info two"},
    ]}}
    found = code_findings.build_findings(_modules(), None, scans)
    assert [f["message"] for f in found] == ["error one", "warn one"]


def test_build_findings_cap_is_per_file(monkeypatch):
    monkeypatch.setattr(code_findings, "MAX_FINDINGS_PER_FILE", 1)
    scans = {
        "app": {"findings": [{"severity": "minor", "description": "a1"}, {"severity": "minor", "description": "a2"}]},
        "util": {"findings": [{"severity": "minor", "description": "u1"}, {"severity": "minor", "description": "u2"}]},
    }
    found = code_findings.build_findings(_modules(), None, scans)
    assert sorted(f["message"] for f in found) == ["a1", "u1"]


def test_build_findings_end_to_end_against_real_python_line_numbers():
    code = "def divide(a, b):\n    return a / b\n"
    tb = _real_traceback_for(code, "assert divide(1, 0) == 1\n")
    # the real sandbox hands back an IPython traceback; the classic one
    # has the same frames in the other spelling the parser accepts.
    result = _failed(error=_execution_error_str("ZeroDivisionError", "division by zero", tb))
    found = code_findings.build_findings({"calc": {"path": "calc.py", "code": code}}, {"calc": result}, None)
    assert found == [{"path": "calc.py", "line": 2, "severity": "error",
                      "message": "ZeroDivisionError: division by zero", "source": "sandbox_test"}]


# ---------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------

class FakeCursor:
    def __init__(self, fetchall_results=None):
        self.executed = []
        self._fetchall_queue = list(fetchall_results or [])

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._fetchall_queue.pop(0) if self._fetchall_queue else []


class FakeCursorContext:
    def __init__(self, cursor, calls_log, **kwargs):
        self.cursor = cursor
        self.calls_log = calls_log
        self.kwargs = kwargs

    def __enter__(self):
        self.calls_log.append(self.kwargs)
        return self.cursor

    def __exit__(self, *exc_info):
        return False


def _install_fake_cursor(monkeypatch, cursor):
    calls_log = []
    monkeypatch.setattr(
        code_findings.db, "cursor",
        lambda **kwargs: FakeCursorContext(cursor, calls_log, **kwargs),
    )
    return calls_log


def _finding(path="src/app.py", line=3, severity="error", message="boom", source="sandbox_test"):
    return {"path": path, "line": line, "severity": severity, "message": message, "source": source}


def test_replace_findings_deletes_then_inserts_in_one_transaction(monkeypatch):
    cur = FakeCursor()
    calls = _install_fake_cursor(monkeypatch, cur)
    findings = [
        _finding("a.py", 3, "error", "m1"),
        _finding("a.py", None, "info", "m2", "semgrep"),
        _finding("b.py", 9, "warning", "m3", "gitleaks"),
    ]
    inserted = code_findings.replace_findings(
        "ws_1", ["a.py", "b.py", "clean.py"], findings, "user_1",
        file_versions={"a.py": 4, "b.py": 1, "clean.py": 2},
    )
    assert inserted == 3
    assert calls == [{"user_id": "user_1"}]   # real user, NOT trusted
    assert len(cur.executed) == 2
    delete_sql, delete_params = cur.executed[0]
    assert delete_sql.startswith("delete from workspace_code_findings")
    assert delete_params == ("ws_1", ["a.py", "b.py", "clean.py"])   # clean.py is cleared too
    insert_sql, insert_params = cur.executed[1]
    assert insert_sql.startswith("insert into workspace_code_findings")
    assert insert_sql.count("(%s, %s, %s, %s, %s, %s, %s, %s)") == 3
    assert insert_params == [
        "ws_1", "a.py", 0, 3, "error", "m1", "sandbox_test", 4,
        "ws_1", "a.py", 1, None, "info", "m2", "semgrep", 4,
        "ws_1", "b.py", 0, 9, "warning", "m3", "gitleaks", 1,
    ]


def test_replace_findings_with_no_findings_only_clears(monkeypatch):
    cur = FakeCursor()
    _install_fake_cursor(monkeypatch, cur)
    assert code_findings.replace_findings("ws_1", ["a.py"], [], "user_1") == 0
    assert len(cur.executed) == 1
    assert cur.executed[0][0].startswith("delete from workspace_code_findings")


def test_replace_findings_with_no_paths_never_touches_the_database(monkeypatch):
    def boom(**kwargs):
        raise AssertionError("db.cursor must not be called")
    monkeypatch.setattr(code_findings.db, "cursor", boom)
    assert code_findings.replace_findings("ws_1", [], [_finding()], "user_1") == 0


def test_replace_findings_ignores_findings_for_paths_it_was_not_asked_to_replace(monkeypatch):
    cur = FakeCursor()
    _install_fake_cursor(monkeypatch, cur)
    inserted = code_findings.replace_findings(
        "ws_1", ["a.py"], [_finding("a.py", 1), _finding("other.py", 2)], "user_1")
    assert inserted == 1
    assert "other.py" not in cur.executed[1][1]


def test_replace_findings_skips_rows_the_check_constraints_would_reject(monkeypatch):
    cur = FakeCursor()
    _install_fake_cursor(monkeypatch, cur)
    findings = [
        _finding("a.py", 1, "fatal", "bad severity"),
        _finding("a.py", 0, "error", "line below 1"),
        _finding("a.py", True, "error", "bool line"),
        _finding("a.py", "3", "error", "string line"),
        _finding("a.py", 2, "error", ""),
        _finding("a.py", 4, "error", "good"),
    ]
    assert code_findings.replace_findings("ws_1", ["a.py"], findings, "user_1") == 1
    assert cur.executed[1][1] == ["ws_1", "a.py", 0, 4, "error", "good", "sandbox_test", None]


def test_replace_findings_dedupes_repeated_paths(monkeypatch):
    cur = FakeCursor()
    _install_fake_cursor(monkeypatch, cur)
    code_findings.replace_findings("ws_1", ["a.py", "a.py"], [], "user_1")
    assert cur.executed[0][1] == ("ws_1", ["a.py"])


def test_list_findings_shape_and_trusted_cursor(monkeypatch):
    rows = [
        {"file_path": "a.py", "line": 3, "severity": "error", "message": "m", "source": "semgrep", "file_version": 2},
        {"file_path": "a.py", "line": None, "severity": "info", "message": "n", "source": "security_scan", "file_version": None},
    ]
    cur = FakeCursor(fetchall_results=[rows])
    calls = _install_fake_cursor(monkeypatch, cur)
    assert code_findings.list_findings("ws_1") == [
        {"path": "a.py", "line": 3, "severity": "error", "message": "m", "source": "semgrep", "file_version": 2},
        {"path": "a.py", "line": None, "severity": "info", "message": "n", "source": "security_scan", "file_version": None},
    ]
    assert calls == [{"trusted": True}]
    sql, params = cur.executed[0]
    assert "order by file_path, line nulls last, seq" in sql
    assert params == ("ws_1",)


def test_list_findings_empty(monkeypatch):
    _install_fake_cursor(monkeypatch, FakeCursor())
    assert code_findings.list_findings("ws_1") == []


# ---------------------------------------------------------------------
# GET /api/workspaces/{ws}/code/findings
# ---------------------------------------------------------------------

OWNER_ID = "owner_1"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(code_routes.router)
    app.dependency_overrides[require_auth] = lambda: OWNER_ID
    return TestClient(app)


def test_findings_route_returns_the_stored_findings(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(code_routes.chat_workspace, "get_workspace",
                        lambda ws_id, owner_id: seen.setdefault("gate", (ws_id, owner_id)))
    stored = [{"path": "a.py", "line": 3, "severity": "error", "message": "m",
               "source": "semgrep", "file_version": 2}]
    monkeypatch.setattr(code_routes.code_findings, "list_findings",
                        lambda ws_id: seen.setdefault("listed", ws_id) and stored)
    resp = client.get("/api/workspaces/ws_1/code/findings")
    assert resp.status_code == 200
    assert resp.json() == {"findings": stored}
    assert seen["gate"] == ("ws_1", OWNER_ID)
    assert seen["listed"] == "ws_1"


def test_findings_route_empty_is_an_empty_list_not_an_error(client, monkeypatch):
    monkeypatch.setattr(code_routes.chat_workspace, "get_workspace", lambda ws_id, owner_id: {"id": ws_id})
    monkeypatch.setattr(code_routes.code_findings, "list_findings", lambda ws_id: [])
    resp = client.get("/api/workspaces/ws_1/code/findings")
    assert resp.status_code == 200
    assert resp.json() == {"findings": []}


def test_findings_route_404s_for_a_workspace_the_caller_cannot_see(client, monkeypatch):
    def deny(ws_id, owner_id):
        raise FileNotFoundError(ws_id)
    monkeypatch.setattr(code_routes.chat_workspace, "get_workspace", deny)

    def must_not_run(ws_id):
        raise AssertionError("findings must not be read before the ownership gate passes")
    monkeypatch.setattr(code_routes.code_findings, "list_findings", must_not_run)
    resp = client.get("/api/workspaces/someone_elses/code/findings")
    assert resp.status_code == 404


def test_findings_route_does_not_collide_with_the_file_routes(client, monkeypatch):
    monkeypatch.setattr(code_routes.chat_workspace, "get_workspace", lambda ws_id, owner_id: {"id": ws_id})
    monkeypatch.setattr(code_routes.code_findings, "list_findings", lambda ws_id: [])
    monkeypatch.setattr(code_routes.workspace_code_files, "get_file",
                        lambda ws_id, path: {"file_path": path, "content": "", "version": 0})
    assert client.get("/api/workspaces/ws_1/code/findings").json() == {"findings": []}
    assert client.get("/api/workspaces/ws_1/code/files/findings").json()["file_path"] == "findings"


# ---------------------------------------------------------------------
# _write_code_files() hook
# ---------------------------------------------------------------------

@pytest.fixture
def hook(monkeypatch):
    """Wires _write_code_files() to recording fakes. `calls` is the
    ordered list of ("replace" | "emit", payload) so ordering between
    the findings write and the event can be asserted."""
    calls = []
    monkeypatch.setattr(task_runner.chat_workspace, "workspace_for_chat", lambda sid, owner: {"id": "ws_1"})
    monkeypatch.setattr(
        task_runner, "write_code_files_batch",
        lambda ws_id, files, owner: [{"file_path": f["file_path"], "version": 5} for f in files],
    )

    def fake_replace(ws_id, paths, findings, user_id, file_versions=None):
        calls.append(("replace", {"ws_id": ws_id, "paths": paths, "findings": findings,
                                  "user_id": user_id, "file_versions": file_versions}))
        return len(findings)
    monkeypatch.setattr(task_runner.code_findings, "replace_findings", fake_replace)

    def fake_emit(event_type, **kwargs):
        calls.append(("emit", {"event_type": event_type, **kwargs}))
    monkeypatch.setattr(task_runner, "emit_workspace_event", fake_emit)
    return calls


def _seed_bus(code, file_map, tests=None, scans=None):
    write(KEYS["fixed_code"], code)
    write("file_map", file_map)
    if tests is not None:
        write(KEYS["test_results"], tests)
    if scans is not None:
        write(KEYS["security_scan_results"], scans)


def _run_hook():
    task_runner._write_code_files({"status": "ok", "tier": 3}, "sess_1", "user_1")


def test_hook_stores_findings_before_the_code_file_updated_event(hook):
    code = "def divide(a, b):\n    return a / b\n"
    _seed_bus(
        {"calc": {"language": "python", "code": code}, "clean": {"language": "python", "code": "x = 1\n"}},
        {"calc": "src/calc.py", "clean": "src/clean.py"},
        tests={"calc": _failed(error=_execution_error_str(
            "ZeroDivisionError", "division by zero", _ipython_traceback([(40, None), (2, "divide")])))},
        scans={"calc": {"findings": [{"severity": "moderate", "description": "x at line 1: y", "source": "semgrep"}]}},
    )
    _run_hook()

    assert [kind for kind, _ in hook] == ["replace", "emit"]
    replace = hook[0][1]
    assert replace["ws_id"] == "ws_1"
    assert replace["user_id"] == "user_1"
    assert replace["paths"] == ["src/calc.py", "src/clean.py"]      # the clean file is replaced (cleared) too
    assert replace["file_versions"] == {"src/calc.py": 5, "src/clean.py": 5}
    assert replace["findings"] == [
        {"path": "src/calc.py", "line": 2, "severity": "error",
         "message": "ZeroDivisionError: division by zero", "source": "sandbox_test"},
        {"path": "src/calc.py", "line": 1, "severity": "warning",
         "message": "x at line 1: y", "source": "semgrep"},
    ]
    emit = hook[1][1]
    assert emit["event_type"] is task_runner.EventType.CODE_FILE_UPDATED
    assert emit["payload"]["file_paths"] == ["src/calc.py", "src/clean.py"]


def test_hook_ignores_traceback_lines_inside_the_appended_generated_tests(hook):
    code = "def f():\n    return 1\n"   # 2 lines; anything past line 2 is a generated test
    _seed_bus(
        {"m": {"language": "python", "code": code}}, {"m": "m.py"},
        tests={"m": _failed(error=_execution_error_str(
            "AssertionError", "", _ipython_traceback([(9, None)], "AssertionError")))},
    )
    _run_hook()
    findings = hook[0][1]["findings"]
    assert len(findings) == 1
    assert findings[0]["line"] is None


def test_hook_with_no_scan_or_test_data_still_clears_the_rewritten_files(hook):
    _seed_bus({"m": "x = 1\n"}, {"m": "m.py"})
    _run_hook()
    replace = hook[0][1]
    assert replace["paths"] == ["m.py"]
    assert replace["findings"] == []
    assert [kind for kind, _ in hook] == ["replace", "emit"]


def test_hook_does_not_store_findings_for_a_module_whose_generation_failed(hook):
    _seed_bus(
        {"good": "x = 1\n", "bad": "# CODE WRITER FAILED: empty response"},
        {"good": "good.py", "bad": "bad.py"},
        tests={"bad": _failed(error="RuntimeError: nope")},
    )
    _run_hook()
    replace = hook[0][1]
    assert replace["paths"] == ["good.py"]
    assert replace["findings"] == []


def test_hook_is_fail_open_when_storing_findings_fails(hook, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")
    monkeypatch.setattr(task_runner.code_findings, "replace_findings", boom)
    _seed_bus({"m": "x = 1\n"}, {"m": "m.py"})
    _run_hook()   # must not raise
    assert [kind for kind, _ in hook] == ["emit"]   # the refetch event still goes out
    assert "code findings write-back failed" in capsys.readouterr().out


def test_hook_is_fail_open_when_building_findings_fails(hook, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("unexpected shape")
    monkeypatch.setattr(task_runner.code_findings, "build_findings", boom)
    _seed_bus({"m": "x = 1\n"}, {"m": "m.py"})
    _run_hook()
    assert [kind for kind, _ in hook] == ["emit"]


def test_hook_writes_nothing_for_a_non_tier_3_response(hook):
    _seed_bus({"m": "x = 1\n"}, {"m": "m.py"})
    task_runner._write_code_files({"status": "ok", "tier": 1}, "sess_1", "user_1")
    assert hook == []
