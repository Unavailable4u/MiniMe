"""S06b: the run buffer holds content, so it must never log any of it.

tests/unit/test_codeagent_buffer.py proves at run time that the paths it exercises log
neither content, key names nor the secret. This is the structural half, the same idea as
test_codeagent_logging.py (which scans only the two S04 files): it reads
codeagent/buffer.py and fails if a logging call mentions a name that carries content, a
key, a secret or an exception's text, asks for a traceback, or logs an exception object
instead of its type. A new log line that does any of those must be justified by editing
this test.
"""
import ast
import pathlib

from tests.static.test_codeagent_logging import FORBIDDEN_NAMES, _logging_calls, _names_outside_type_calls

BACKEND = pathlib.Path(__file__).resolve().parents[2]
BUFFER = BACKEND / "codeagent" / "buffer.py"

# On top of S04's list: everything this module holds that is content, a key or a secret.
EXTRA_FORBIDDEN = {
    "run_id", "state", "history", "items", "item", "payload", "plain", "plaintext", "wire", "records",
    "record", "stamp", "key", "keys", "secret", "secrets", "config", "box", "args", "commands", "command",
    "conv", "replay", "stats", "target", "reply", "results", "wires",
}


def scan(tree):
    problems = []
    for call in _logging_calls(tree):
        where = f"buffer.py:{call.lineno}"
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


def test_buffer_logging_calls_name_only_safe_values():
    assert BUFFER.exists(), "codeagent/buffer.py is part of S06b"
    tree = ast.parse(BUFFER.read_text(encoding="utf-8"))
    assert list(_logging_calls(tree))                         # it does log (fault types), so the scan has teeth
    assert scan(tree) == []


def test_the_scan_would_catch_a_violation():
    bad = ast.parse(
        "logger.info('got %s', payload)\n"
        "logger.warning('key %s', key)\n"
        "logger.exception('x')\n"
        "logger.warning('y', exc_info=True)\n"
        "logger.warning('z %s', exc)\n"
    )
    assert len(scan(bad)) == 5


def test_logging_the_type_of_an_exception_is_allowed():
    good = ast.parse("logger.warning('failed (%s)', type(exc).__name__)\n")
    assert scan(good) == []


def test_the_module_never_prints_or_imports_a_traceback_printer():
    tree = ast.parse(BUFFER.read_text(encoding="utf-8"))
    prints = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"]
    assert prints == []
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "traceback" not in imported
