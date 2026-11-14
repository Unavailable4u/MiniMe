"""
m2c/tests/headless/test_session_setup.py -- Patch C10b: the construction the TUI and headless mode share.

Unit tests for m2c/session_setup.py: the folder check, the rule layers (and the session-only rules of --allow / --deny),
the policy context, the fixed-words denial hints, and the module boundaries (headless.py must still not import the
policy or tool packages, nor this module: the two are joined only by the injected ``tool_setup``).
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiring_rig import *  # noqa: E402,F401,F403

from m2c import headless, session_setup  # noqa: E402
from m2c.policy.decide import decide  # noqa: E402
from m2c.policy import Request  # noqa: E402
from m2c.tools.paths import PathGuardError  # noqa: E402

SRC = Path(session_setup.__file__).resolve().parent


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
            names.update(("." * node.level) + (node.module or "") + "." + alias.name for alias in node.names)
    return names


# ---- --allow / --deny parsing ---------------------------------------------------------------------------------

def test_cli_rules_parse_into_user_layer_rules_in_order():
    rules = session_setup.parse_cli_rules(("bash(echo hi)", "read_file(**)"), ("bash(rm *)",))
    assert [r.label for r in rules] == ["allow:user:bash(echo hi)", "allow:user:read_file(**)", "deny:user:bash(rm *)"]


@pytest.mark.parametrize("bad", ["not-a-rule-zq91", "bash()", "bash(", "", "nosuchtool(zq91)"])
def test_a_bad_cli_rule_is_a_usage_error_that_names_the_flag_and_never_echoes_the_rule(bad):
    for allow, deny, flag in (((bad,), (), "--allow"), ((), (bad,), "--deny")):
        with pytest.raises(headless.ToolSetupError) as caught:
            session_setup.parse_cli_rules(allow, deny)
        assert caught.value.exit_code == 2 and caught.value.code == "usage"
        assert flag in str(caught.value) and "zq91" not in str(caught.value)


# ---- the folder ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["/", "HOME"])
def test_the_filesystem_root_and_the_home_directory_are_refused(where):
    target = os.environ["HOME"] if where == "HOME" else where
    Path(target).mkdir(parents=True, exist_ok=True)
    with pytest.raises(PathGuardError):
        session_setup.open_project(target)


def test_open_project_builds_what_the_tui_builds(project):
    (project / "M2C.md").write_text("be brief marker-4417\n")
    built = session_setup.open_project(str(project))
    assert built.root == str(project.resolve()) and built.tool_context.root == project.resolve()
    assert "marker-4417" in built.run_context.run_start_fields()["instructions"]
    assert built.project_settings_present is False and built.settings_invalid is False
    (project / ".m2c").mkdir()
    (project / ".m2c" / "settings.toml").write_text("")
    assert session_setup.open_project(str(project)).project_settings_present is True


def test_the_project_settings_file_is_never_loaded_into_the_layers(project):
    (project / ".m2c").mkdir()
    (project / ".m2c" / "settings.toml").write_text('[permissions]\nallow = ["bash(echo hi)"]\n')
    built = session_setup.open_project(str(project))
    assert built.layers.project.rules == () and built.layers.project.invalid is False


def test_user_settings_load_from_the_config_home(project):
    home = Path(os.environ["M2C_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.toml").write_text('[permissions]\nallow = ["bash(echo hi)"]\n')
    built = session_setup.open_project(str(project))
    assert [r.raw for r in built.layers.user.rules] == ["bash(echo hi)"]
    (home / "settings.toml").write_text("broken [")
    assert session_setup.open_project(str(project)).settings_invalid is True


def test_the_shell_config_is_attached_with_the_requested_sandbox_mode(project):
    for mode in ("auto", "required", "off"):
        built = session_setup.open_project(str(project), sandbox=mode)
        assert built.tool_context.shell_config.sandbox == mode
    assert session_setup.open_project(str(project)).tool_context.shell_config.sandbox == "auto"


def test_the_sandbox_warning_goes_to_the_callback_given(project):
    seen: list[str] = []
    built = session_setup.open_project(str(project), warn=seen.append)
    built.tool_context.shell_config.warn("a fixed sentence")
    assert seen == ["a fixed sentence"]


# ---- layers and the policy context --------------------------------------------------------------------------

def test_session_rules_join_the_user_layer_and_a_deny_still_wins(project):
    built = session_setup.open_project(str(project))
    rules = session_setup.parse_cli_rules(("bash(echo hi)",), ("bash(echo hi)",))
    layers = session_setup.with_session_rules(built.layers, rules)
    ctx = session_setup.project_policy_context("ask", layers, built.root)
    decision = decide(Request("bash", {"command": "echo hi"}, "R2"), ctx)
    assert decision.action == "deny" and decision.matched_rule == "deny:user:bash(echo hi)"
    assert layers.managed is built.layers.managed and layers.project is built.layers.project


def test_session_rules_keep_the_invalid_flag_of_the_user_layer(project):
    home = Path(os.environ["M2C_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.toml").write_text("broken [")
    built = session_setup.open_project(str(project))
    layers = session_setup.with_session_rules(built.layers, session_setup.parse_cli_rules(("bash(echo hi)",), ()))
    ctx = session_setup.project_policy_context("ask", layers, built.root)
    assert decide(Request("bash", {"command": "echo hi"}, "R2"), ctx).action != "allow"   # fails closed


def test_the_policy_context_is_the_one_the_tui_builds(project):
    built = session_setup.open_project(str(project))
    ctx = session_setup.project_policy_context("accept-edits", built.layers, built.root)
    assert (ctx.mode, ctx.project_root, ctx.cwd, ctx.project_trusted, ctx.tainted, ctx.non_interactive) == \
        ("accept-edits", built.root, built.root, False, False, False)
    assert ctx.home == str(Path.home())


# ---- the fixed-words hints ----------------------------------------------------------------------------------

@pytest.mark.parametrize("tool, action, rule, mode, invalid, must_contain, must_not", [
    ("bash", "ask", None, "ask", False, ("bash", "--allow", "<command pattern>"), ()),
    ("write_file", "ask", None, "ask", False, ("--mode accept-edits", "write_file(<path pattern>)"), ()),
    ("edit_file", "ask", None, "ask", False, ("edit_file(<path pattern>)",), ()),
    ("delete_path", "ask", None, "accept-edits", False, ("delete_path(<path pattern>)",), ("--mode accept-edits",)),
    ("read_file", "ask", None, "ask", False, ("read_file(<path pattern>)",), ()),
    ("plan_exit", "ask", None, "ask", False, ("plan",), ("--allow",)),
    ("bash", "ask", "ask:user:bash(touch *)", "ask", False, ("ask rule", "cannot override"), ("touch",)),
    ("bash", "ask", None, "ask", True, ("could not be read",), ()),
    ("bash", "deny", "deny:user:bash(rm -rf *)", "ask", False, ("deny rule",), ("rm",)),
    ("bash", "deny", None, "plan", False, ("plan mode",), ()),
    ("write_file", "deny", None, "ask", False, ("protected",), ()),
])
def test_denial_hints_are_fixed_words_that_name_the_tool_and_the_rule(tool, action, rule, mode, invalid, must_contain, must_not):
    hint = session_setup.denial_hint(tool, action, rule, mode, settings_invalid=invalid)
    for word in must_contain:
        assert word in hint, (word, hint)
    for word in must_not:
        assert word not in hint, (word, hint)
    assert "\n" not in hint and len(hint) < 300


def test_a_hint_never_repeats_a_tool_name_the_server_made_up():
    hint = session_setup.denial_hint("rm -rf zq91 \n", "deny", None, "ask")
    assert "zq91" not in hint and "\n" not in hint


# ---- module boundaries --------------------------------------------------------------------------------------

def test_headless_still_does_not_import_policy_tools_or_the_setup_module():
    imported = _imports(SRC / "headless.py")
    for name in imported:
        parts = {p for p in name.replace(".", " ").split()}
        assert not parts & {"policy", "tools", "session_setup", "runner", "sandbox", "checkpoints", "context"}, name


def test_the_setup_module_does_not_import_the_tui():
    assert not any("tui" in name for name in _imports(SRC / "session_setup.py"))


def test_tool_setup_error_carries_a_code_and_exit_code():
    err = headless.ToolSetupError("fixed words")
    assert (str(err), err.exit_code, err.code) == ("fixed words", 2, "usage")
    err = headless.ToolSetupError("fixed words", exit_code=1, code="config")
    assert (err.exit_code, err.code) == (1, "config")
