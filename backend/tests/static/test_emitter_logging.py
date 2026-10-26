"""S14b: the emitter handles every event a run streams (reply text, tool input), so its
logging is checked structurally, like router.py's, buffer.py's and run_manager.py's.

tests/unit/test_codeagent_emitter.py::test_no_content_reaches_the_log proves at run time that
a canary in an event's text never reaches the log. This reads codeagent/emitter.py and fails
if a logging call names a variable that carries an event, a payload or an exception's text,
asks for a traceback, or logs an exception object instead of its type. Run ids, device ids,
user ids, seq numbers, state names and exception TYPE names are what the log may hold.
"""
import ast
import pathlib

from tests.static.test_codeagent_logging import FORBIDDEN_NAMES, _logging_calls, _names_outside_type_calls

BACKEND = pathlib.Path(__file__).resolve().parents[2]
EMITTER = BACKEND / "codeagent" / "emitter.py"

EXTRA_FORBIDDEN = {
    "payload", "body", "envelope", "event", "events", "replay", "history", "state",
    "row", "rows", "args", "kwargs", "reason", "stored", "env",
}


def scan(tree):
    problems = []
    for call in _logging_calls(tree):
        where = f"emitter.py:{call.lineno}"
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


def test_emitter_logging_calls_name_only_safe_values():
    assert EMITTER.exists(), "codeagent/emitter.py is part of S14b"
    tree = ast.parse(EMITTER.read_text(encoding="utf-8"))
    assert len(list(_logging_calls(tree))) >= 2               # it does log, so the scan has teeth
    assert scan(tree) == []


def test_the_scan_would_catch_a_violation():
    bad = ast.parse(
        "logger.info('got %s', envelope)\n"
        "logger.info('p %s', payload)\n"
        "logger.info('e %s', events)\n"
        "logger.exception('x')\n"
        "logger.warning('y', exc_info=True)\n"
        "logger.warning('z %s', exc)\n"
    )
    assert len(scan(bad)) == 6


def test_ids_seq_numbers_state_names_and_exception_types_are_allowed():
    good = ast.parse(
        "logger.info('kept run=%s device=%s user=%s seq=%s', run_id, device_id, user_id, seq)\n"
        "logger.info('final run=%s state=%s', run_id, state_name)\n"
        "logger.error('failed run=%s error=%s', run_id, type(exc).__name__)\n"
    )
    assert scan(good) == []
