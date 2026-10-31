"""
m2c/tests/tools/test_tools_shell_dispatch.py -- Patch C07: the three small,
backward-compatible changes to C05's ``m2c.tools`` entry point that the shell
tool needs, tested on their own (the shell itself is in test_tools_shell.py):

(a) ``bash`` is in ``ARG_SPECS`` (it mirrors protocol/v1/tools/bash.json; the
    parametrized schema test in test_tools_run_tool.py compares them) and in
    ``TOOLS``;
(b) ``ToolResult`` has an optional ``exit_code`` that ``to_payload`` includes
    only when it is set;
(c) ``run_tool`` takes an optional keyword-only ``call_env`` that is handed
    only to executors that declare a ``call_env`` parameter, and an executor
    may return a ``ToolOutcome`` (a result that can be a failure and still keep
    its output, trust label, exit code and a local code).

Every existing tool keeps its exact behaviour: a tool that returns the old
``(output, truncated)`` tuple is untouched.

Run: cd m2c && pytest tests/tools/test_tools_shell_dispatch.py -v
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from tools_fixtures import call, write

import m2c
from m2c import tools as tools_pkg
from m2c.tools import ARG_SPECS, TOOLS, CallEnv, ToolOutcome, ToolResult, run_tool, tool_names
from m2c.tools.shell import AUTH_USER_APPROVED

PROTOCOL_DIR = Path(m2c.__file__).resolve().parent / "protocol" / "v1"


# ---- (a) the table --------------------------------------------------------------------------------------
def test_bash_is_in_the_tool_table_and_the_argument_table():
    assert "bash" in tool_names() and "bash" in TOOLS and "bash" in ARG_SPECS
    spec = ARG_SPECS["bash"]
    assert set(spec) == {"command", "timeout_s", "reason"}
    assert spec["command"].type is str and spec["command"].required
    assert spec["timeout_s"].type is int and (spec["timeout_s"].minimum, spec["timeout_s"].maximum, spec["timeout_s"].default) == (1, 3600, 120)
    assert spec["reason"].type is str and not spec["reason"].required


def test_bash_matches_the_protocol_schema_exactly():
    schema = json.loads((PROTOCOL_DIR / "tools" / "bash.json").read_text())["input_schema"]
    assert set(ARG_SPECS["bash"]) == set(schema["properties"]) and schema["required"] == ["command"]
    assert schema["properties"]["timeout_s"]["default"] == 120 and schema["properties"]["reason"]["maxLength"] == 200


def test_a_bash_call_with_bad_arguments_is_refused_before_anything_runs(ctx, proj):
    env = CallEnv(authorization=AUTH_USER_APPROVED)
    for args in ({}, {"command": 5}, {"command": "touch ran.flag", "timeout_s": "5"}, {"command": "touch ran.flag", "timeout_s": True},
                 {"command": "touch ran.flag", "extra": 1}, {"command": "touch ran.flag", "reason": 5}, None, [], "touch ran.flag"):
        r = run_tool(ctx, "bash", args, call_env=env)
        assert not r.ok and r.code == "invalid_args", args
    assert not (proj / "ran.flag").exists()


def test_run_tool_never_raises_on_junk_for_bash(ctx):
    junk = [None, 0, -1, "", "x", b"x", [], {}, [None], {"a": {"b": 1}}, float("nan"), 10**30, object()]
    env = CallEnv(authorization=AUTH_USER_APPROVED)
    for a in junk:
        assert isinstance(run_tool(ctx, "bash", a, call_env=env), ToolResult)
    for v in junk:
        for key in ("command", "timeout_s", "reason"):
            if key == "command" and v == "x":
                continue  # a real (harmless but failing) command; the rest are all invalid
            assert isinstance(run_tool(ctx, "bash", {"command": "true", key: v} if key != "command" else {key: v}, call_env=env), ToolResult)


# ---- (b) ToolResult.exit_code ---------------------------------------------------------------------------
def test_exit_code_is_optional_and_off_the_wire_when_unset():
    r = ToolResult(ok=True, output="hi", truncated=False, trust="local_project", code=None, duration_ms=3)
    assert r.exit_code is None
    assert "exit_code" not in r.to_payload("c1")
    assert r.to_payload("c1") == {"call_id": "c1", "ok": True, "output": "hi", "truncated": False, "trust": "local_project", "duration_ms": 3}


def test_exit_code_is_on_the_wire_when_set_including_zero():
    for code in (0, 1, 137):
        p = ToolResult(True, "", False, "external", None, 5, code).to_payload("c1")
        assert p["exit_code"] == code and isinstance(p["exit_code"], int)
    schema = json.loads((PROTOCOL_DIR / "tool_result.json").read_text())
    assert set(p) <= set(schema["properties"]) and set(schema["required"]) <= set(p)


def test_existing_positional_construction_still_works():
    r = ToolResult(False, "x", True, "local_project", "not_found", 9)
    assert (r.ok, r.output, r.truncated, r.trust, r.code, r.duration_ms, r.exit_code) == (False, "x", True, "local_project", "not_found", 9, None)


# ---- (c) call_env and ToolOutcome ------------------------------------------------------------------------
def test_call_env_is_keyword_only_and_defaults_to_none():
    p = inspect.signature(run_tool).parameters["call_env"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is None
    assert list(inspect.signature(run_tool).parameters)[:3] == ["context", "name", "args"]


def test_call_env_reaches_only_executors_that_declare_it(ctx, proj, monkeypatch):
    write(proj, "f.txt", "x\n")
    seen = {}

    def declares(context, path, call_env=None):
        seen["declares"] = call_env
        return "ok", False

    def keyword_only(context, path, *, call_env):
        seen["keyword_only"] = call_env
        return "ok", False

    def legacy(context, pattern):
        seen["legacy"] = "called"
        return "ok", False

    def var_kw(context, **kw):
        seen["var_kw"] = dict(kw)
        return "ok", False

    sentinel = CallEnv(authorization=AUTH_USER_APPROVED)
    for name, fn in (("read_file", declares), ("list_dir", keyword_only), ("glob", legacy)):
        monkeypatch.setitem(tools_pkg.TOOLS, name, fn)
    assert run_tool(ctx, "read_file", {"path": "f.txt"}, call_env=sentinel).ok
    assert run_tool(ctx, "list_dir", {"path": "."}, call_env=sentinel).ok
    assert run_tool(ctx, "glob", {"pattern": "*"}, call_env=sentinel).ok
    assert seen["declares"] is sentinel and seen["keyword_only"] is sentinel and seen["legacy"] == "called"
    monkeypatch.setitem(tools_pkg.TOOLS, "grep", var_kw)
    assert run_tool(ctx, "grep", {"pattern": "x"}, call_env=sentinel).ok
    assert "call_env" not in seen["var_kw"], "**kwargs does not declare call_env"
    seen.clear()
    assert run_tool(ctx, "read_file", {"path": "f.txt"}).ok and seen["declares"] is None


def test_the_old_tools_do_not_receive_call_env(ctx, proj):
    write(proj, "f.txt", "hello\n")
    r = run_tool(ctx, "read_file", {"path": "f.txt"}, call_env=CallEnv(authorization=AUTH_USER_APPROVED))
    assert r.ok and "hello" in r.output and r.exit_code is None and r.trust == "local_project"


def test_an_executor_can_return_a_failed_result_that_keeps_its_output(ctx, monkeypatch):
    monkeypatch.setitem(tools_pkg.TOOLS, "read_file", lambda context, path: ToolOutcome(
        output="partial output", truncated=True, ok=False, trust="external", exit_code=7, code="exit_nonzero"))
    r = call(ctx, "read_file", path="f")
    assert (r.ok, r.output, r.truncated, r.trust, r.exit_code, r.code) == (False, "partial output", True, "external", 7, "exit_nonzero")
    assert r.duration_ms >= 0
    p = r.to_payload("c9")
    assert p == {"call_id": "c9", "ok": False, "output": "partial output", "truncated": True, "trust": "external",
                 "duration_ms": r.duration_ms, "exit_code": 7}


def test_an_outcome_that_succeeds_carries_its_trust_label(ctx, monkeypatch):
    monkeypatch.setitem(tools_pkg.TOOLS, "read_file", lambda context, path: ToolOutcome("fine", exit_code=0, trust="local_other"))
    r = call(ctx, "read_file", path="f")
    assert r.ok and r.trust == "local_other" and r.exit_code == 0 and r.code is None


def test_an_outcome_is_clamped_to_the_wire_limit(ctx, monkeypatch):
    monkeypatch.setitem(tools_pkg.TOOLS, "read_file", lambda context, path: ToolOutcome("z" * 250_000, ok=False))
    r = call(ctx, "read_file", path="f")
    assert len(r.output) <= 200_000 and r.truncated is True and not r.ok


@pytest.mark.parametrize("trust", ["untrusted", "", None, 5, "LOCAL_PROJECT"])
def test_an_outcome_with_an_unknown_trust_label_is_treated_as_external(ctx, monkeypatch, trust):
    monkeypatch.setitem(tools_pkg.TOOLS, "read_file", lambda context, path: ToolOutcome("x", trust=trust))
    assert call(ctx, "read_file", path="f").trust == "external"


@pytest.mark.parametrize("exit_code", [True, "1", 1.5, None, [1]])
def test_an_outcome_with_a_bad_exit_code_has_none(ctx, monkeypatch, exit_code):
    monkeypatch.setitem(tools_pkg.TOOLS, "read_file", lambda context, path: ToolOutcome("x", exit_code=exit_code))
    r = call(ctx, "read_file", path="f")
    assert r.exit_code is None and "exit_code" not in r.to_payload("c")


@pytest.mark.parametrize("code", ["has space", "x" * 80, "UPPER", "../x", 5, ""])
def test_an_outcome_code_that_is_not_a_plain_identifier_is_dropped(ctx, monkeypatch, code):
    monkeypatch.setitem(tools_pkg.TOOLS, "read_file", lambda context, path: ToolOutcome("x", ok=False, code=code))
    assert call(ctx, "read_file", path="f").code is None


def test_an_outcome_that_is_not_a_tooloutcome_or_a_tuple_is_an_internal_error(ctx, monkeypatch):
    monkeypatch.setitem(tools_pkg.TOOLS, "read_file", lambda context, path: {"output": "x"})
    r = call(ctx, "read_file", path="f")
    assert not r.ok and r.code == "internal_error"


def test_tooloutcome_is_importable_from_the_package_and_is_frozen():
    out = ToolOutcome("x")
    assert (out.ok, out.truncated, out.trust, out.exit_code, out.code) == (True, False, "local_project", None, None)
    with pytest.raises(Exception):
        out.output = "y"  # type: ignore[misc]


def test_call_env_is_a_frozen_value_with_safe_defaults():
    env = CallEnv()
    assert env.authorization is None and env.policy is None and env.cancel is None and env.on_chunk is None
    with pytest.raises(Exception):
        env.authorization = AUTH_USER_APPROVED  # type: ignore[misc]
