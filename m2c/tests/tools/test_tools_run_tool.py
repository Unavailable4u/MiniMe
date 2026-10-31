"""
m2c/tests/tools/test_tools_run_tool.py -- Patch C05: the executor entry point
(``m2c.tools.run_tool``) and the properties that cut across every tool.

* Dispatch fails closed: unknown tools, non-dict arguments, unknown / missing /
  mistyped / out-of-range arguments are errors, and the error never echoes the
  offending value.
* The argument table is checked against the real schemas in
  ``m2c/protocol/v1/tools/*.json`` so the two cannot drift apart silently.
* The result has the wire shape of protocol/v1 ``tool_result`` (ok, output,
  truncated, trust, duration_ms) and never exceeds the 200000-character limit.
* The hard denylist is enforced by every tool, including the three that list
  or search (their output must not even *name* a protected file).
* Logging: tool name, status, code, size and duration only; never a path,
  pattern, argument or file content, and no call site bypasses ``log_event``.

Run: cd m2c && pytest tests/tools/test_tools_run_tool.py -v
"""
from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path

import pytest
from tools_fixtures import (
    PROTECTED_FILES,
    SAFE_FILE,
    SECRET,
    call,
    fails,
    make_fake_rg,
    ok,
    plant_protected,
    write,
)

import m2c
from m2c import logging_setup
from m2c.tools import ARG_SPECS, Caps, ToolContext, ToolResult, paths, run_tool, tool_names

PROTOCOL_DIR = Path(m2c.__file__).resolve().parent / "protocol" / "v1"
TOOLS_SRC = Path(m2c.__file__).resolve().parent / "tools"
EXECUTED = ["read_file", "list_dir", "glob", "grep", "edit_file", "write_file", "delete_path"]


# ==========================================================================
# The tool table and the protocol schemas
# ==========================================================================
def test_the_executor_knows_exactly_the_seven_c05_tools():
    assert sorted(tool_names()) == sorted(EXECUTED)
    assert sorted(ARG_SPECS) == sorted(EXECUTED)


@pytest.mark.parametrize("name", EXECUTED)
def test_the_argument_table_matches_the_protocol_schema(name):
    schema = json.loads((PROTOCOL_DIR / "tools" / f"{name}.json").read_text())["input_schema"]
    spec = ARG_SPECS[name]
    assert schema.get("additionalProperties") is False
    assert set(spec) == set(schema["properties"])
    json_types = {"string": str, "integer": int, "boolean": bool}
    required = set(schema.get("required", []))
    # P00 quirk, recorded here on purpose: list_dir's schema lists "path" as
    # required while also giving it a default of "." and describing it as
    # optional.  The executor follows the description (the server validates
    # tool_proposed.input first, so an omitted path never reaches the client).
    if name == "list_dir":
        assert required == {"path"} and schema["properties"]["path"]["default"] == "."
        required = set()
    assert {k for k, s in spec.items() if s.required} == required
    for key, prop in schema["properties"].items():
        s = spec[key]
        assert s.type is json_types[prop["type"]], key
        assert s.minimum == prop.get("minimum"), key
        assert s.maximum == prop.get("maximum"), key
        assert s.default == prop.get("default"), key


def test_the_result_payload_matches_the_tool_result_schema():
    schema = json.loads((PROTOCOL_DIR / "tool_result.json").read_text())
    common = json.loads((PROTOCOL_DIR / "common.json").read_text())
    trust_values = set(common["$defs"]["trust"]["enum"])
    max_len = common["$defs"]["bulk_text"]["maxLength"]
    result = ToolResult(ok=True, output="hi", truncated=False, trust="local_project", code=None, duration_ms=3)
    payload = result.to_payload("call_1")
    assert set(payload) <= set(schema["properties"])
    assert set(schema["required"]) <= set(payload)
    assert "code" not in payload  # the local error code is never on the wire
    assert payload == {"call_id": "call_1", "ok": True, "output": "hi", "truncated": False,
                       "trust": "local_project", "duration_ms": 3}
    assert payload["trust"] in trust_values
    assert max_len == 200000


def test_every_result_is_a_valid_tool_result_payload(ctx, proj):
    write(proj, "f.txt", "x\n")
    for r in (
        call(ctx, "read_file", path="f.txt"),
        call(ctx, "read_file", path="missing"),
        call(ctx, "list_dir"),
        call(ctx, "glob", pattern="*"),
        call(ctx, "no_such_tool"),
    ):
        p = r.to_payload("abc-123")
        assert isinstance(p["ok"], bool) and isinstance(p["output"], str) and isinstance(p["truncated"], bool)
        assert p["trust"] == "local_project"
        assert isinstance(p["duration_ms"], int) and p["duration_ms"] >= 0
        assert len(p["output"]) <= 200000


def test_a_failed_result_has_a_code_and_readable_output(ctx):
    r = call(ctx, "read_file", path="missing")
    assert r.ok is False and r.code == "not_found" and r.output.startswith("error")
    assert r.truncated is False


def test_an_oversized_output_is_clamped_below_the_wire_limit(proj):
    # Caps are validated: read_output_chars cannot exceed what the wire allows.
    with pytest.raises(ValueError):
        Caps(read_output_chars=250_000)
    big = ToolContext(proj, caps=Caps(read_output_chars=150_000, max_line_chars=150_000, max_file_bytes=8 * 1024 * 1024))
    write(proj, "wide.txt", "z" * 190_000 + "\n")
    r = call(big, "read_file", path="wide.txt")
    assert r.ok and len(r.output) <= 200000 and r.truncated


# ==========================================================================
# Fail-closed dispatch
# ==========================================================================
@pytest.mark.parametrize("name", ["bash", "plan_exit", "rm", "delete", "glob_search", "READ_FILE", "", " read_file", "read_file\n",
                                  "mcp__x__y", "../read_file", None, 5, ["read_file"]])
def test_unknown_tools_are_refused(ctx, name):
    r = run_tool(ctx, name, {})
    assert not r.ok and r.code == "unknown_tool"


@pytest.mark.parametrize("args", [None, [], "path=f", 5, ("path", "f")])
def test_arguments_must_be_an_object(ctx, args):
    assert run_tool(ctx, "read_file", args).code == "invalid_args"


def test_unknown_arguments_are_refused_and_the_allowed_names_are_listed(ctx, proj):
    write(proj, "f.txt")
    r = call(ctx, "read_file", path="f.txt", offset=1)
    assert r.code == "invalid_args" and "unknown argument" in r.output
    assert "path" in r.output and "start_line" in r.output and "end_line" in r.output
    assert "offset" not in r.output  # model-supplied names are not echoed back


@pytest.mark.parametrize(
    "name,args",
    [
        ("read_file", {}),
        ("glob", {}),
        ("grep", {}),
        ("edit_file", {"path": "f", "old_string": "a"}),
        ("write_file", {"path": "f"}),
        ("delete_path", {}),
    ],
)
def test_missing_required_arguments(ctx, name, args):
    r = run_tool(ctx, name, args)
    assert r.code == "invalid_args" and "required" in r.output


@pytest.mark.parametrize(
    "name,args",
    [
        ("read_file", {"path": 5}),
        ("read_file", {"path": None}),
        ("read_file", {"path": "f", "start_line": "1"}),
        ("read_file", {"path": "f", "start_line": 1.5}),
        ("read_file", {"path": "f", "start_line": True}),  # bool is an int subclass: still wrong
        ("read_file", {"path": "f", "start_line": 0}),
        ("read_file", {"path": "f", "end_line": -3}),
        ("list_dir", {"path": ".", "max_depth": 0}),
        ("list_dir", {"path": ".", "max_depth": 9}),
        ("list_dir", {"path": ["."]}),
        ("glob", {"pattern": "*", "max_results": 0}),
        ("glob", {"pattern": "*", "max_results": 2001}),
        ("grep", {"pattern": "x", "context_lines": 11}),
        ("grep", {"pattern": "x", "context_lines": -1}),
        ("grep", {"pattern": "x", "max_matches": 1001}),
        ("grep", {"pattern": "x", "case_insensitive": "yes"}),
        ("grep", {"pattern": "x", "case_insensitive": 1}),
        ("edit_file", {"path": "f", "old_string": "a", "new_string": 1}),
        ("edit_file", {"path": "f", "old_string": "a", "new_string": "b", "replace_all": "true"}),
        ("write_file", {"path": "f", "content": b"bytes"}),
        ("write_file", {"path": "f", "content": ["x"]}),
        ("delete_path", {"path": "f", "recursive": True}),
    ],
)
def test_malformed_arguments_are_refused_without_touching_anything(ctx, proj, name, args):
    write(proj, "f", "keep\n")
    r = run_tool(ctx, name, args)
    assert not r.ok and r.code == "invalid_args"
    assert (proj / "f").read_text() == "keep\n"


def test_error_messages_do_not_echo_argument_values(ctx):
    marker = "MARKER_VALUE_THAT_MUST_NOT_BE_ECHOED"
    for name, args in (
        ("read_file", {"path": "f", "start_line": marker}),
        ("read_file", {"path": "f", marker: 1}),
        ("grep", {"pattern": "x", "case_insensitive": marker}),
        ("write_file", {"path": marker * 2, "content": 5}),
    ):
        r = run_tool(ctx, name, args)
        assert not r.ok and marker not in r.output


def test_run_tool_never_raises_on_junk(ctx):
    junk = [None, 0, -1, "", "x", b"x", [], {}, [None], {"a": {"b": 1}}, float("nan"), 10**30, object()]
    for name in EXECUTED + ["bogus"]:
        for a in junk:
            r = run_tool(ctx, name, a)
            assert isinstance(r, ToolResult)
        for v in junk:
            for key in ("path", "pattern", "content", "old_string", "new_string", "start_line", "max_results"):
                r = run_tool(ctx, name, {key: v})
                assert isinstance(r, ToolResult)


def test_an_unexpected_exception_becomes_a_fixed_internal_error(ctx, monkeypatch, caplog):
    def explode(context, **kw):
        raise RuntimeError("MARKER_SECRET_IN_EXCEPTION /home/me/.ssh/id_rsa")

    from m2c import tools as tools_pkg

    monkeypatch.setitem(tools_pkg.TOOLS, "read_file", explode)
    with caplog.at_level(logging.DEBUG, logger="m2c"):
        r = call(ctx, "read_file", path="f")
    assert not r.ok and r.code == "internal_error"
    assert "MARKER_SECRET" not in r.output and "id_rsa" not in r.output
    assert "MARKER_SECRET" not in caplog.text and "id_rsa" not in caplog.text
    assert "RuntimeError" in caplog.text  # the type name is logged, never the message


def test_control_flow_exceptions_are_not_swallowed(ctx, monkeypatch):
    from m2c import tools as tools_pkg

    def interrupt(context, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setitem(tools_pkg.TOOLS, "list_dir", interrupt)
    with pytest.raises(KeyboardInterrupt):
        call(ctx, "list_dir")


# ==========================================================================
# The context
# ==========================================================================
def test_context_holds_root_denylist_seen_state_and_caps(proj):
    c = ToolContext(proj, user_denylist=["*.secret"], caps=Caps(list_max_entries=7))
    assert c.root == proj
    assert c.caps.list_max_entries == 7
    assert c.denylist.is_protected(("a.secret",))
    assert len(c.seen) == 0
    assert c.rg_path is None


def test_context_requires_a_safe_existing_directory(tmp_path):
    with pytest.raises(paths.PathGuardError):
        ToolContext("/")
    with pytest.raises(paths.PathGuardError):
        ToolContext(tmp_path / "missing")
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(paths.PathGuardError):
        ToolContext(f)


def test_two_contexts_do_not_share_seen_state(proj):
    write(proj, "f.txt", "a\n")
    one, two = ToolContext(proj), ToolContext(proj)
    ok(call(one, "read_file", path="f.txt"))
    fails(call(two, "write_file", path="f.txt", content="b\n"), "not_read")


@pytest.mark.parametrize(
    "kw",
    [
        {"read_output_chars": 0}, {"read_output_chars": -1}, {"read_output_chars": 250_000},
        {"max_line_chars": 0}, {"max_file_bytes": 0}, {"max_write_bytes": 0}, {"list_max_entries": 0},
        {"glob_max_results": 0}, {"glob_timeout_s": 0}, {"grep_timeout_s": -1}, {"grep_output_bytes": 0},
        {"diff_max_chars": 0}, {"max_delete_entries": 0}, {"read_output_chars": True}, {"read_output_chars": "5"},
    ],
)
def test_caps_are_validated(kw):
    with pytest.raises(ValueError):
        Caps(**kw)


# ==========================================================================
# The hard denylist, in every tool
# ==========================================================================
@pytest.fixture
def planted(proj):
    plant_protected(proj)
    return proj


def _mutations(rel):
    return [
        ("read_file", {"path": rel}),
        ("edit_file", {"path": rel, "old_string": SECRET, "new_string": "x"}),
        ("write_file", {"path": rel, "content": "overwritten"}),
        ("delete_path", {"path": rel}),
        ("list_dir", {"path": rel}),
        ("glob", {"pattern": "*", "path": rel}),
        ("grep", {"pattern": SECRET, "path": rel}),
    ]


@pytest.mark.parametrize("rel", PROTECTED_FILES)
def test_every_tool_refuses_every_protected_path(planted, rel):
    c = ToolContext(planted)
    before = (planted / rel).read_text()
    for name, args in _mutations(rel):
        r = run_tool(c, name, args)
        assert not r.ok and r.code == "protected_path", (name, rel, r.output)
        assert "protected path" in r.output
        assert SECRET not in r.output and rel not in r.output, (name, rel)
    assert (planted / rel).read_text() == before


@pytest.mark.parametrize("rel", PROTECTED_FILES)
def test_protected_paths_that_do_not_exist_are_refused_not_reported_missing(proj, rel):
    c = ToolContext(proj)
    for name, args in _mutations(rel):
        r = run_tool(c, name, args)
        assert r.code == "protected_path", (name, rel, r.code)
    assert not (proj / rel).exists(), "a refused write must not create the file"


def test_a_protected_directory_is_refused_as_a_whole(planted):
    c = ToolContext(planted)
    for d in (".ssh", ".aws", ".kube", ".docker", ".config/gcloud", ".gnupg"):
        for name, args in (("list_dir", {"path": d}), ("glob", {"pattern": "*", "path": d}),
                           ("write_file", {"path": f"{d}/new", "content": "x"})):
            assert run_tool(c, name, args).code == "protected_path", (name, d)
    assert not (planted / ".ssh" / "new").exists()


def test_list_dir_output_never_names_a_protected_entry(planted):
    out = ok(call(ToolContext(planted), "list_dir", path=".", max_depth=8))
    assert SAFE_FILE in out
    for frag in (".env", "id_rsa", ".ssh", ".aws", ".pem", ".key", "p12", ".netrc", "history",
                 ".kube", ".docker", "gcloud", "id_ed25519"):
        assert frag not in out, frag


def test_glob_output_never_names_a_protected_file(planted):
    for pattern in ("**/*", "*", ".*", "**/.*", "**/id_*", "**/*.pem", "**/.env*"):
        out = ok(call(ToolContext(planted), "glob", pattern=pattern))
        for frag in (".env", "id_rsa", "id_ed25519", ".ssh", ".aws", ".netrc", ".pem", ".key", ".p12",
                     "history", ".kube", ".docker", "gcloud", ".git-credentials"):
            assert frag not in out, (pattern, frag)


def test_grep_output_never_contains_a_protected_file_even_when_rg_returns_one(planted, tmp_path):
    lines = b"".join(b"./%s\x001:%s leaked\n" % (rel.encode(), SECRET.encode()) for rel in PROTECTED_FILES)
    script = make_fake_rg(tmp_path / "fb", f"sys.stdout.buffer.write({lines!r} + b'./README.md\\x001:ok line\\n')\n")
    out = ok(call(ToolContext(planted, rg_path=str(script)), "grep", pattern=SECRET, max_matches=1000))
    assert out.splitlines() == ["README.md:1:ok line"]


def test_symlinks_cannot_be_used_to_reach_protected_files_through_any_tool(planted):
    (planted / "innocent.txt").symlink_to(".env")
    (planted / "shortcut").symlink_to(".ssh")
    c = ToolContext(planted)
    for name, args in (
        ("read_file", {"path": "innocent.txt"}),
        ("read_file", {"path": "shortcut/config"}),
        ("list_dir", {"path": "shortcut"}),
        ("delete_path", {"path": "innocent.txt"}),
    ):
        r = run_tool(c, name, args)
        assert not r.ok and r.code == "protected_path", (name, args, r.code)
    assert (planted / "innocent.txt").is_symlink()


def test_the_user_list_applies_to_every_tool_and_adds_to_the_hard_list(proj, tmp_path):
    write(proj, "db.secret", SECRET)
    write(proj, "private/x.txt", SECRET)
    write(proj, "public.txt", "fine\n")
    write(proj, ".env", SECRET)
    c = ToolContext(proj, user_denylist=["*.secret", "/private/"])
    for rel in ("db.secret", "private/x.txt", ".env"):
        for name, args in _mutations(rel):
            r = run_tool(c, name, args)
            assert r.code == "protected_path", (name, rel)
    listing = ok(call(c, "list_dir", path=".", max_depth=3))
    assert "public.txt" in listing and "db.secret" not in listing and "private" not in listing
    assert ok(call(c, "read_file", path="public.txt"))


# ==========================================================================
# Logging
# ==========================================================================
def test_calls_are_logged_with_tool_status_code_size_and_duration_only(ctx, proj, caplog):
    write(proj, "MARKER_FILENAME.txt", "MARKER_CONTENT\n")
    write(proj, "MARKER_UNREAD.txt", "MARKER_CONTENT\n")
    with caplog.at_level(logging.DEBUG, logger="m2c"):
        call(ctx, "read_file", path="MARKER_FILENAME.txt")
        call(ctx, "grep", pattern="MARKER_PATTERN", path="MARKER_DIR")
        call(ctx, "write_file", path="MARKER_NEW.txt", content="MARKER_WRITTEN")
        call(ctx, "edit_file", path="MARKER_UNREAD.txt", old_string="MARKER_CONTENT", new_string="MARKER_NEWTEXT")
        call(ctx, "no_such_tool", path="MARKER_X")
    text = caplog.text
    assert "MARKER" not in text
    events = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tools.call")]
    assert len(events) == 5
    assert re.search(r"tools\.call check=read_file status=ok count=\d+ duration_ms=\d+", events[0])
    assert re.search(r"check=grep status=error code=not_found", events[1])
    assert "check=write_file status=ok" in events[2]
    assert "check=edit_file status=error code=not_read" in events[3]
    assert "check=unknown status=error code=unknown_tool" in events[4]  # model-supplied names are never logged


def test_nothing_in_the_tools_package_logs_except_through_log_event():
    """The convention (see logging_setup.py): log literals or use log_event.  A
    direct ``logger.info(some_value)`` anywhere in m2c/tools could leak a path."""
    offenders = []
    for py in sorted(TOOLS_SRC.glob("*.py")):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"debug", "info", "warning", "error", "exception", "critical", "log"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"logger", "log", "logging"}
            ):
                offenders.append(f"{py.name}:{node.lineno}")
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
                offenders.append(f"{py.name}:{node.lineno} print")
    assert offenders == []


def test_log_event_keys_used_by_the_tools_are_all_on_the_safe_list():
    used = set()
    for py in sorted(TOOLS_SRC.glob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "log_event":
                used |= {kw.arg for kw in node.keywords if kw.arg}
    assert used and used <= logging_setup.SAFE_KEYS


def test_denied_attempts_are_logged_by_code_not_by_path(planted, caplog):
    with caplog.at_level(logging.DEBUG, logger="m2c"):
        call(ToolContext(planted), "read_file", path=".ssh/id_rsa_MARKER")
    assert "code=protected_path" in caplog.text
    assert "MARKER" not in caplog.text and ".ssh" not in caplog.text


def test_the_package_uses_only_the_standard_library_and_m2c():
    import sys

    stdlib = set(sys.stdlib_module_names)
    for py in sorted(TOOLS_SRC.glob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert all(n in stdlib or n == "m2c" for n in names), (py.name, names)


def test_nothing_in_the_tools_package_imports_from_the_daemon():
    for py in sorted(TOOLS_SRC.glob("*.py")):
        assert not re.search(r"^\s*(from|import)\s+daemon\b", py.read_text(), re.M), py.name


def test_ported_files_carry_the_port_header():
    head = (TOOLS_SRC / "paths.py").read_text()
    assert "Ported from daemon/path_guard.py (MiniMe, 6977ddf)" in head
    assert "Ported from daemon/tools.py (MiniMe, 6977ddf)" in (TOOLS_SRC / "files.py").read_text()
