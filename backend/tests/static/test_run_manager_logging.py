"""S06c: the run manager handles run content (the run.start payload, the resume state and its
history, the replay events), so its logging is checked structurally, like buffer.py's.

tests/unit/test_codeagent_run_manager.py proves at run time that a full lifecycle with
a canary in every content field logs none of it. This reads codeagent/run_manager.py and
fails if a logging call names a variable that carries content, a key or an exception's
text, asks for a traceback, or logs an exception object instead of its type. Run ids,
account ids, device ids, state names, counts and type names are what the log may hold.
"""
import ast
import pathlib

from tests.static.test_codeagent_logging import FORBIDDEN_NAMES, _logging_calls, _names_outside_type_calls

BACKEND = pathlib.Path(__file__).resolve().parents[2]
MANAGER = BACKEND / "codeagent" / "run_manager.py"

EXTRA_FORBIDDEN = {
    "payload", "state", "body", "history", "replay", "events", "event", "envelope", "issues", "issue",
    "row", "rows", "page", "secret", "key", "keys", "args", "kwargs",
}


def scan(tree):
    problems = []
    for call in _logging_calls(tree):
        where = f"run_manager.py:{call.lineno}"
        if call.func.attr == "exception":
            problems.append(f"{where}: logger.exception() prints a traceback with the exception's text")
        for kw in call.keywords:
            if kw.arg in ("exc_info", "stack_info"):
                problems.append(f"{where}: {kw.arg}= prints the exception's text")
        for arg in list(call.args) + [kw.value for kw in call.keywords]:
            bad = sorted(set(_names_outside_type_calls(arg)) & (FORBIDDEN_NAMES | EXTRA_FORBIDDEN))
            if bad:
                problems.append(f"{where}: logs {', '.join(bad)}")
    return problems


def test_run_manager_logging_calls_name_only_safe_values():
    assert MANAGER.exists(), "codeagent/run_manager.py is part of S06c"
    tree = ast.parse(MANAGER.read_text(encoding="utf-8"))
    assert len(list(_logging_calls(tree))) >= 5               # it does log, so the scan has teeth
    assert scan(tree) == []


def test_the_scan_would_catch_a_violation():
    bad = ast.parse(
        "logger.info('got %s', payload)\n"
        "logger.info('s %s', state)\n"
        "logger.exception('x')\n"
        "logger.warning('y', exc_info=True)\n"
        "logger.warning('z %s', exc)\n"
        "logger.warning('h %s', history)\n"
    )
    assert len(scan(bad)) == 6


def test_run_ids_state_names_and_exception_types_are_allowed():
    good = ast.parse(
        "logger.info('[run_manager] %s %s', run.id, run.state)\n"
        "logger.error('failed run=%s (%s)', run_id, type(exc).__name__)\n"
    )
    assert scan(good) == []


def test_the_module_never_prints_or_imports_a_traceback_printer():
    tree = ast.parse(MANAGER.read_text(encoding="utf-8"))
    prints = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"]
    assert prints == []
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "traceback" not in imported
