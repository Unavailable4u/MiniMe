"""S04: the device socket must never log message content (build plan 11.4: log device
id, user id, message type and error code only).

tests/unit/test_codeagent_ws.py::test_no_message_content_or_credential_is_logged proves
it at run time for the paths it exercises. This test is the structural half: it reads
codeagent/ws.py and codeagent/connections.py and fails if any logging call mentions a
name that carries content, asks for a traceback (an exception's text can contain
content), or logs an exception object instead of its type. A new log line that does
any of those must be justified by editing this test.

Only the two S04 files are scanned: other patches add modules under backend/codeagent/
and answer for their own logging.
"""
import ast
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]
FILES = ["codeagent/ws.py", "codeagent/connections.py"]
LEVELS = {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
# Names that hold (or can hold) message content, a credential, or an exception's text.
FORBIDDEN_NAMES = {
    "msg", "message", "payload", "frame", "text", "raw", "data", "body", "envelope", "hello",
    "credential", "device_credential", "exc", "err", "error", "e", "ex", "websocket", "ws",
    "issues", "issue", "client_host", "address", "addr", "ip",
}


def _logging_calls(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in LEVELS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"logger", "log", "logging"}):
            yield node


def _names_outside_type_calls(node):
    """Every Name under `node`, except the argument of type(...) -- logging
    type(exc).__name__ is the sanctioned way to say what failed."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type":
        return
    if isinstance(node, ast.Name):
        yield node.id
    for child in ast.iter_child_nodes(node):
        yield from _names_outside_type_calls(child)


def scan(tree, rel):
    """Human-readable problems with the logging calls in `tree`."""
    problems = []
    for call in _logging_calls(tree):
        where = f"{rel}:{call.lineno}"
        if call.func.attr == "exception":
            problems.append(f"{where}: logger.exception() prints a traceback with the exception's text")
        for kw in call.keywords:
            if kw.arg in ("exc_info", "stack_info"):
                problems.append(f"{where}: {kw.arg}= prints the exception's text")
        for arg in list(call.args) + [kw.value for kw in call.keywords]:
            bad = sorted(set(_names_outside_type_calls(arg)) & FORBIDDEN_NAMES)
            if bad:
                problems.append(f"{where}: logs {', '.join(bad)}")
    return problems


@pytest.mark.parametrize("rel", FILES)
def test_logging_calls_name_only_safe_values(rel):
    path = BACKEND / rel
    assert path.exists(), f"{rel} is part of S04"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert list(_logging_calls(tree)) or rel.endswith("connections.py")   # ws.py certainly logs
    assert scan(tree, rel) == []


def test_the_scan_would_catch_a_violation():
    bad = ast.parse(
        "logger.info('got %s', msg)\n"
        "logger.exception('x')\n"
        "logger.warning('y', exc_info=True)\n"
        "logger.error('z %s', exc)\n"
        "logger.error('ok %s', type(exc).__name__)\n"
        "logger.info('ok device=%s user=%s type=%s', device_id, user_id, msg_type)\n"
    )
    problems = scan(bad, "snippet")
    assert len(problems) == 4
    assert [p.split(":")[1] for p in problems] == ["1", "2", "3", "4"]
