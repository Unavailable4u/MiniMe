"""S09: the tool broker handles every tool argument and every tool output, so its logging is
checked structurally, like router.py's, emitter.py's and run_handlers.py's (build plan 11.4;
protocol README 6: `tool_proposed`, `tool_result` and `tool_stream` are `content`).

tests/unit/test_codeagent_broker.py::test_no_content_reaches_the_log proves at run time that a
canary in a tool's input, output and stream chunk, and in an exception's text, never reaches
the log. This reads codeagent/broker.py and fails if a logging call names a variable that
carries a tool argument, a tool result, a chunk, a payload or an exception's text, asks for a
traceback, or logs an exception object instead of its type. What the log may hold: run id,
call id, tool name, sizes (byte counts), status names, error codes and exception TYPE names.
"""
import ast
import pathlib

from tests.static.test_codeagent_logging import FORBIDDEN_NAMES, _logging_calls, _names_outside_type_calls

BACKEND = pathlib.Path(__file__).resolve().parents[2]
BROKER = BACKEND / "codeagent" / "broker.py"

EXTRA_FORBIDDEN = {
    "payload", "body", "envelope", "event", "events", "state", "row", "rows", "args", "kwargs", "reason", "env",
    # the tool call's content, under every name the module might give it
    "tool_input", "arguments", "input", "inputs", "output", "result", "results", "chunk", "piece",
    "content", "outcome", "parsed", "marker", "fields",
}


def scan(tree):
    problems = []
    for call in _logging_calls(tree):
        where = f"broker.py:{call.lineno}"
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


def test_broker_logging_calls_name_only_safe_values():
    assert BROKER.exists(), "codeagent/broker.py is part of S09"
    tree = ast.parse(BROKER.read_text(encoding="utf-8"))
    assert len(list(_logging_calls(tree))) >= 5               # it does log, so the scan has teeth
    assert scan(tree) == []


def test_the_scan_would_catch_a_violation():
    bad = ast.parse(
        "logger.info('out %s', output)\n"
        "logger.info('in %s', tool_input)\n"
        "logger.info('chunk %s', chunk)\n"
        "logger.info('p %s', payload)\n"
        "logger.exception('x')\n"
        "logger.warning('y', exc_info=True)\n"
        "logger.warning('z %s', exc)\n"
        "logger.warning('r %s', result)\n"
    )
    assert len(scan(bad)) == 8


def test_ids_sizes_status_names_and_exception_types_are_allowed():
    good = ast.parse(
        "logger.info('proposed run=%s call=%s tool=%s', run_id, call_id, tool)\n"
        "logger.info('truncated run=%s call=%s omitted=%s', run_id, call.call_id, omitted_bytes)\n"
        "logger.info('finished run=%s call=%s status=%s', run_id, call_id, status)\n"
        "logger.error('failed run=%s error=%s', run_id, type(exc).__name__)\n"
    )
    assert scan(good) == []


def test_the_broker_never_logs_at_all_in_a_way_that_could_hold_a_tool_argument_by_attribute():
    """`call.output` would be a Name `call` (allowed) plus an attribute: attribute access is not
    caught by the name scan, so no logging call in broker.py may use an attribute named like content."""
    tree = ast.parse(BROKER.read_text(encoding="utf-8"))
    risky = {"output", "tool_input", "input", "chunk", "result", "payload", "text"}
    found = []
    for call in _logging_calls(tree):
        for arg in list(call.args) + [kw.value for kw in call.keywords]:
            for node in ast.walk(arg):
                if isinstance(node, ast.Attribute) and node.attr in risky:
                    found.append(f"broker.py:{call.lineno}: logs .{node.attr}")
    assert found == []
