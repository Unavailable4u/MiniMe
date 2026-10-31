"""S07: the agent loop handles every prompt, reply, tool argument and tool result, so its logging is checked
structurally, like broker.py's and emitter.py's (build plan 11.4; protocol README 6).

tests/unit/test_codeagent_loop.py::test_no_content_reaches_the_log proves at run time that a canary in the user's
text, the reply, a tool's arguments and output, and in an exception's text never reaches the log. This reads
codeagent/loop.py and fails if a logging call names a variable that carries any of that, asks for a traceback, or
logs an exception object instead of its type. What the log may hold: run id, user id, device id, provider and
model names, bucket and code words, counts, state names and exception TYPE names. prompts.py has no logger at all
(tests/unit/test_codeagent_prompts.py pins that).
"""
import ast
import pathlib

from tests.static.test_codeagent_logging import FORBIDDEN_NAMES, _logging_calls, _names_outside_type_calls

BACKEND = pathlib.Path(__file__).resolve().parents[2]
LOOP = BACKEND / "codeagent" / "loop.py"

EXTRA_FORBIDDEN = {
    "payload", "body", "envelope", "event", "events", "row", "rows", "args", "kwargs", "env",
    "tool_input", "arguments", "input", "inputs", "output", "result", "results", "chunk", "piece", "content",
    "outcome", "parsed", "fields", "reply", "items", "item", "conversation", "calls", "planned", "problems",
    "usage", "raw", "why", "notice", "request", "assembled", "user_text",
}


def scan(tree):
    problems = []
    for call in _logging_calls(tree):
        where = f"loop.py:{call.lineno}"
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


def test_loop_logging_calls_name_only_safe_values():
    assert LOOP.exists(), "codeagent/loop.py is part of S07"
    tree = ast.parse(LOOP.read_text(encoding="utf-8"))
    assert len(list(_logging_calls(tree))) >= 10                  # it does log, so the scan has teeth
    assert scan(tree) == []


def test_the_scan_would_catch_a_violation():
    bad = ast.parse(
        "logger.info('out %s', output)\n"
        "logger.info('in %s', tool_input)\n"
        "logger.info('reply %s', reply)\n"
        "logger.info('p %s', payload)\n"
        "logger.exception('x')\n"
        "logger.warning('y', exc_info=True)\n"
        "logger.warning('z %s', exc)\n"
        "logger.warning('ok %s', type(exc).__name__)\n"
        "logger.info('ok run=%s user=%s count=%d', run_id, user_id, n)\n"
    )
    problems = scan(bad)
    assert [p.split(":")[1] for p in problems] == ["1", "2", "3", "4", "5", "6", "7"]
