"""S15: codeagent/wiring.py reads settings and sits next to every prompt and reply, so its logging is checked
structurally, like loop.py's and metering.py's (build plan 11.4, O1; tests/static/test_loop_logging.py is the model).

What the log may hold: run id, user id, setting NAMES, mode words and exception TYPE names. Never a setting's VALUE,
a message, a prompt or an exception's text.
"""
import ast
import pathlib

from tests.static.test_codeagent_logging import FORBIDDEN_NAMES, _logging_calls, _names_outside_type_calls

BACKEND = pathlib.Path(__file__).resolve().parents[2]
WIRING = BACKEND / "codeagent" / "wiring.py"

EXTRA_FORBIDDEN = {"environ", "value", "raw", "items", "item", "conversation", "events", "reply", "payload", "secret"}


def scan(tree):
    problems = []
    for call in _logging_calls(tree):
        where = f"wiring.py:{call.lineno}"
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


def test_wiring_logging_calls_name_only_safe_values():
    tree = ast.parse(WIRING.read_text(encoding="utf-8"))
    assert len(list(_logging_calls(tree))) >= 4                   # it does log, so the scan has teeth
    assert scan(tree) == []


def test_the_scan_would_catch_a_violation():
    bad = ast.parse(
        "logger.info('v %s', value)\n"
        "logger.warning('e %s', environ)\n"
        "logger.warning('x', exc_info=True)\n"
        "logger.warning('z %s', exc)\n"
        "logger.warning('ok %s', type(exc).__name__)\n"
        "logger.warning('ok setting %s', name)\n"
    )
    assert [p.split(":")[1] for p in scan(bad)] == ["1", "2", "3", "4"]


def test_wiring_imports_nothing_from_the_other_patches_of_this_wave():
    tree = ast.parse(WIRING.read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "codeagent"
                for a in n.names}
    assert imported <= {"broker", "buffer", "context", "hello", "loop", "metering", "run_handlers", "emitter"}
