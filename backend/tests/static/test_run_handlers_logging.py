"""S14c: the run handlers sit on the path of every run message (run.start carries the whole
prompt context), so their logging is checked structurally, like router.py's, emitter.py's,
buffer.py's and run_manager.py's.

tests/unit/test_codeagent_run_handlers.py::test_no_content_reaches_the_log proves at run time
that a canary in a run.start payload and in an exception's text never reaches the log. This
reads codeagent/run_handlers.py and fails if a logging call names a variable that carries a
message, a payload, a history or an exception's text, asks for a traceback, or logs an
exception object instead of its type. Run ids, device ids, user ids, state names, error
codes and exception TYPE names are what the log may hold.
"""
import ast
import pathlib

from tests.static.test_codeagent_logging import FORBIDDEN_NAMES, _logging_calls, _names_outside_type_calls

BACKEND = pathlib.Path(__file__).resolve().parents[2]
HANDLERS = BACKEND / "codeagent" / "run_handlers.py"

EXTRA_FORBIDDEN = {
    "payload", "body", "envelope", "event", "events", "replay", "history", "state",
    "row", "rows", "args", "kwargs", "reason", "stored", "env", "fixed", "mapped", "details",
    "hook", "hooks", "runner",
}


def scan(tree):
    problems = []
    for call in _logging_calls(tree):
        where = f"run_handlers.py:{call.lineno}"
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


def test_run_handlers_logging_calls_name_only_safe_values():
    assert HANDLERS.exists(), "codeagent/run_handlers.py is part of S14c"
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    assert len(list(_logging_calls(tree))) >= 6               # it does log, so the scan has teeth
    assert scan(tree) == []


def test_the_scan_would_catch_a_violation():
    bad = ast.parse(
        "logger.info('got %s', msg)\n"
        "logger.info('p %s', payload)\n"
        "logger.info('h %s', history)\n"
        "logger.exception('x')\n"
        "logger.warning('y', exc_info=True)\n"
        "logger.warning('z %s', exc)\n"
        "logger.warning('d %s', details)\n"
    )
    assert len(scan(bad)) == 7


def test_ids_state_names_codes_and_exception_types_are_allowed():
    good = ast.parse(
        "logger.info('started run=%s device=%s user=%s', run.id, conn.device_id, conn.user_id)\n"
        "logger.warning('refused code=%s error=%s', code, type(exc).__name__)\n"
        "logger.info('paused run=%s state=%s', run.id, run.state)\n"
    )
    assert scan(good) == []
