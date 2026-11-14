"""
m2c/tests/headless/test_headless_wiring.py -- Patch C10b: ``m2c -p`` runs tools for real, under a policy nobody can answer.

Every test drives the real CLI (``cli.main``) or ``headless.run_headless`` against C04's fake server in a thread and a
temporary project folder.  The tool runner, the policy engine, the shell tool, the checkpoints and the store are the real
ones; only the server and the keyring are fakes.  What is NOT covered here, and why:

* the real bubblewrap sandbox (no ``bwrap`` on the machine this patch was written on): the sandbox modes are tested with
  the suite's "no working sandbox" status and with a counting stand-in for the probe;
* SIGTERM and SIGINT, which need a real child process: test_headless_wiring_process.py.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiring_rig import *  # noqa: E402,F401,F403
from wiring_rig import TASK, call_headless, checkpoint_rows, make_setup, steps_tail, tool_results  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "secret_handling"))

from m2c import cli  # noqa: E402

EDIT = ("c2", "edit_file", {"path": "a.txt", "old_string": "one", "new_string": "two"}, "R1", "tool_result")
READ = ("c1", "read_file", {"path": "a.txt"}, "R0", "tool_result")
TOUCH = ("b1", "bash", {"command": "touch ran.marker"}, "R2", "tool_result")
TOUCH_DENIED = ("b1", "bash", {"command": "touch ran.marker"}, "R2", "approval")
ALLOW_TOUCH = ("--allow", "bash(touch ran.marker)")


def json_args(*extra: str, mode: str = "ask") -> list[str]:
    return ["-p", TASK, "--output-format", "json", "--mode", mode, *extra]


def run_steps(capsys, args: list[str], *steps):
    return run_cli(capsys, args, scripted(steps_tail(*steps)))


def write_user_settings(text: str) -> None:
    home = Path(os.environ["M2C_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.toml").write_text(text, encoding="utf-8")


# ---- 1. an edit task completes ------------------------------------------------------------------------------

def test_an_edit_task_completes_in_accept_edits_mode_with_no_network(project, capsys, network_guard):
    (project / "a.txt").write_text("one\n")
    (project / "M2C.md").write_text("Prefer short answers. marker-instr-4417\n")
    write = ("c3", "write_file", {"path": "new.txt", "content": "hello\n"}, "R1", "tool_result")
    out = run_steps(capsys, json_args(mode="accept-edits"), READ, EDIT, write)

    assert out.code == 0, out.err
    doc = out.json()
    assert doc["ok"] is True and doc["result"].endswith("All done.")
    assert (project / "a.txt").read_text() == "two\n"
    assert (project / "new.txt").read_text() == "hello\n"
    assert all(r["ok"] for r in tool_results(out.server).values()) and len(tool_results(out.server)) == 3
    assert doc["files_changed"] == ["a.txt", "new.txt"]
    assert doc["tool_calls"] == {"ok": 3, "failed": 0, "denied": 0, "cancelled": 0}
    # the server was offered the capability, and got the C12 context fields
    (hello,) = out.server.messages("hello")
    assert "tools" in hello["payload"]["capabilities"]
    (start,) = out.server.messages("run.start")
    assert "marker-instr-4417" in start["payload"]["instructions"]
    out.server.assert_clean()


def test_text_mode_keeps_stdout_for_the_models_text_only(project, capsys):
    (project / "a.txt").write_text("one\n")
    out = run_steps(capsys, ["-p", TASK, "--mode", "accept-edits"], READ, EDIT)
    assert out.code == 0
    assert out.out.strip().endswith("All done.")
    assert "a.txt" not in out.out and "edit_file" not in out.out


# ---- 2. bash needs an allow rule ----------------------------------------------------------------------------

def test_a_bash_call_without_an_allow_rule_is_denied_with_exit_3_and_runs_nothing(project, capsys):
    out = run_steps(capsys, json_args(), TOUCH_DENIED)
    assert out.code == 3
    doc = out.json()
    assert doc["ok"] is False and doc["exit_code"] == 3 and doc["error"]["code"] == "policy_denied"
    message = doc["error"]["message"]
    assert "bash" in message and "--allow" in message and "never asks" in message
    assert "ran.marker" not in out.out + out.err           # the command text never appears
    assert not (project / "ran.marker").exists()           # and nothing ran
    types = out.server.types()
    assert types[-2:] == ["approval", "cancel"] and "tool_result" not in types
    assert doc["tool_calls"]["denied"] == 1 and doc["files_changed"] == []


def test_the_same_call_runs_with_an_allow_rule(project, capsys):
    out = run_steps(capsys, json_args(*ALLOW_TOUCH), TOUCH)
    assert out.code == 0, out.err
    assert (project / "ran.marker").exists()
    assert out.json()["tool_calls"] == {"ok": 1, "failed": 0, "denied": 0, "cancelled": 0}


def test_text_mode_reports_the_denial_on_stderr_without_the_command(project, capsys):
    out = run_steps(capsys, ["-p", TASK], TOUCH_DENIED)
    assert out.code == 3
    assert "bash" in out.err and "--allow" in out.err and "ran.marker" not in out.err + out.out


@pytest.mark.parametrize("edit_tool, expect_words", [
    ("write_file", ("--mode accept-edits", "write_file")),
    ("delete_path", ("delete_path",)),
])
def test_the_message_says_which_rule_an_edit_would_need(project, capsys, edit_tool, expect_words):
    (project / "a.txt").write_text("one\n")
    tool_input = {"path": "a.txt", "content": "x"} if edit_tool == "write_file" else {"path": "a.txt"}
    out = run_steps(capsys, json_args(), ("e1", edit_tool, tool_input, "R1", "approval"))
    assert out.code == 3
    message = out.json()["error"]["message"]
    for word in expect_words:
        assert word in message
    assert (project / "a.txt").read_text() == "one\n"


def test_plan_mode_denies_a_change_and_says_so(project, capsys):
    out = run_steps(capsys, json_args(mode="plan"),
                    ("w1", "write_file", {"path": "x.txt", "content": "x"}, "R1", "approval"))
    assert out.code == 3
    assert "plan mode" in out.json()["error"]["message"].lower()
    assert not (project / "x.txt").exists()


# ---- 3. a deny always wins ----------------------------------------------------------------------------------

@pytest.mark.parametrize("deny", ["bash(touch ran.marker)", "bash(touch *)"])
def test_a_deny_rule_beats_an_allow_rule(project, capsys, deny):
    out = run_steps(capsys, json_args(*ALLOW_TOUCH, "--deny", deny), TOUCH_DENIED)
    assert out.code == 3
    assert "deny rule" in out.json()["error"]["message"]
    assert not (project / "ran.marker").exists()


def test_a_deny_rule_beats_the_mode_default_too(project, capsys):
    out = run_steps(capsys, json_args("--deny", "write_file(blocked.txt)", mode="accept-edits"),
                    ("w1", "write_file", {"path": "blocked.txt", "content": "x"}, "R1", "approval"))
    assert out.code == 3 and not (project / "blocked.txt").exists()


def test_a_deny_rule_in_the_user_settings_beats_an_allow_flag(project, capsys):
    write_user_settings('[permissions]\ndeny = ["bash(touch ran.marker)"]\n')
    out = run_steps(capsys, json_args(*ALLOW_TOUCH), TOUCH_DENIED)
    assert out.code == 3 and not (project / "ran.marker").exists()


def test_an_ask_rule_in_the_user_settings_cannot_be_overridden_by_an_allow_flag(project, capsys):
    write_user_settings('[permissions]\nask = ["bash(touch ran.marker)"]\n')
    out = run_steps(capsys, json_args(*ALLOW_TOUCH), TOUCH_DENIED)
    assert out.code == 3 and not (project / "ran.marker").exists()


def test_user_settings_allow_rules_load_as_they_do_in_the_tui(project, capsys):
    write_user_settings('[permissions]\nallow = ["bash(touch ran.marker)"]\n')
    out = run_steps(capsys, json_args(), TOUCH)
    assert out.code == 0 and (project / "ran.marker").exists()


def test_unreadable_user_settings_allow_nothing_and_say_why(project, capsys):
    write_user_settings("this is [not toml")
    out = run_steps(capsys, json_args(*ALLOW_TOUCH), TOUCH_DENIED)
    assert out.code == 3 and not (project / "ran.marker").exists()
    assert "settings" in out.err and "could not be read" in out.err
    assert "could not be read" in out.json()["error"]["message"]


# ---- 4. a bad rule is a usage error -------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["--allow", "--deny"])
@pytest.mark.parametrize("rule", ["not-a-rule-zq91", "bash(", "bash()", "bash", "nosuchtool(zq91)"])
def test_a_bad_rule_exits_2_before_anything_connects(project, capsys, flag, rule):
    out = run_steps(capsys, json_args(flag, rule), TOUCH_DENIED)
    assert out.code == 2
    doc = out.json()
    assert doc["error"]["code"] == "usage" and doc["session_id"] is None
    assert flag in doc["error"]["message"]
    assert "zq91" not in out.out + out.err                  # the rule text is never echoed
    assert out.server.messages() == []                      # no connection, no run


def test_the_new_flags_only_apply_to_a_headless_run(capsys):
    for flag in (["--allow", "bash(echo hi)"], ["--deny", "bash(echo hi)"], ["--sandbox", "off"], ["--cwd", "."]):
        assert cli.main(flag) == 2
        assert "-p" in capsys.readouterr().err


def test_an_unknown_sandbox_mode_is_a_usage_error(project, capsys):
    assert cli.main(["-p", TASK, "--sandbox", "maybe"]) == 2          # argparse's own choices check
    assert "--sandbox" in capsys.readouterr().err


# ---- 5. the sandbox modes -----------------------------------------------------------------------------------

@pytest.fixture
def probe_calls(monkeypatch):
    """Replace the suite's "no sandbox" status by one that counts how often the shell tool asked for it."""
    from m2c import sandbox

    calls: list[int] = []

    def status(**_kwargs):
        calls.append(1)
        return sandbox.SandboxStatus(False, "bwrap_missing", sandbox.probe.REASONS["bwrap_missing"], None)

    monkeypatch.setattr(sandbox, "get_status", status)
    return calls


def test_sandbox_auto_is_the_default_and_warns_once_when_there_is_none(project, capsys, probe_calls):
    out = run_steps(capsys, json_args(*ALLOW_TOUCH), TOUCH)
    assert out.code == 0 and (project / "ran.marker").exists()
    assert probe_calls, "the default is auto: the shell tool must ask whether a sandbox works"
    assert out.err.count("no sandbox is active") == 1
    assert out.out.count("\n") == 1                         # still exactly one JSON line


def test_sandbox_off_never_asks_for_a_sandbox_and_the_one_warning_says_it_is_off(project, capsys, probe_calls):
    out = run_steps(capsys, json_args("--sandbox", "off", *ALLOW_TOUCH), TOUCH)
    assert out.code == 0 and (project / "ran.marker").exists()
    assert probe_calls == [] and out.err.count("no sandbox is active") == 1 and "turned off" in out.err


def test_sandbox_required_refuses_the_command_when_there_is_no_sandbox_but_the_run_goes_on(project, capsys, probe_calls):
    out = run_steps(capsys, json_args("--sandbox", "required", *ALLOW_TOUCH), TOUCH)
    assert out.code == 0                                    # a refused command is the model's problem, not the run's
    (result,) = tool_results(out.server).values()
    assert result["ok"] is False
    assert not (project / "ran.marker").exists()
    assert out.json()["tool_calls"] == {"ok": 0, "failed": 1, "denied": 0, "cancelled": 0}


# ---- 6. checkpoints, secrets, ordering ------------------------------------------------------------------------

def test_a_checkpoint_exists_after_an_edit_and_rewinding_restores_the_project(project, capsys):
    from m2c import checkpoints
    from m2c.store import open_db

    (project / "a.txt").write_text("one\n")
    write = ("c3", "write_file", {"path": "new.txt", "content": "hello\n"}, "R1", "tool_result")
    out = run_steps(capsys, json_args(mode="accept-edits"), READ, EDIT, write)
    assert out.code == 0
    session_id = out.json()["session_id"]

    rows = checkpoint_rows(session_id)
    assert {path for _step, path, _blob in rows} == {"a.txt", "new.txt"}
    assert dict((path, blob) for _step, path, blob in rows)["new.txt"] is None        # it did not exist before
    assert dict((path, blob) for _step, path, blob in rows)["a.txt"] is not None

    conn = open_db()
    try:
        result = checkpoints.rewind(session_id, min(step for step, _p, _b in rows), project, conn=conn)
    finally:
        conn.close()
    assert result.complete
    assert (project / "a.txt").read_text() == "one\n" and not (project / "new.txt").exists()


def test_a_failed_edit_is_counted_as_failed_and_is_not_a_changed_file(project, capsys):
    (project / "a.txt").write_text("one\n")
    bad = ("c2", "edit_file", {"path": "a.txt", "old_string": "nothing like this", "new_string": "x"}, "R1", "tool_result")
    out = run_steps(capsys, json_args(mode="accept-edits"), READ, bad)
    assert out.code == 0
    doc = out.json()
    assert doc["tool_calls"] == {"ok": 1, "failed": 1, "denied": 0, "cancelled": 0}
    assert doc["files_changed"] == []
    assert (project / "a.txt").read_text() == "one\n"


def test_secrets_are_redacted_in_what_leaves_the_machine(project, capsys):
    from secret_fakes import github_token  # a fake, built at run time

    import random

    secret = github_token(random.Random(11)).secret
    (project / "notes.txt").write_text(f"deploy with {secret} today\n")
    out = run_steps(capsys, json_args(), ("r1", "read_file", {"path": "notes.txt"}, "R0", "tool_result"))
    assert out.code == 0
    assert secret not in json.dumps(out.server.messages()) and secret not in out.out + out.err
    (result,) = tool_results(out.server).values()
    assert result["ok"] is True and "deploy with" in json.dumps(result)


def test_calls_that_arrive_together_run_one_at_a_time_in_order(project, capsys):
    calls = [
        ("s1", "write_file", {"path": "s.txt", "content": "A\n"}),
        ("s2", "edit_file", {"path": "s.txt", "old_string": "A", "new_string": "B"}),
        ("s3", "edit_file", {"path": "s.txt", "old_string": "B", "new_string": "C"}),
    ]

    async def tail(conn, stream, _start, _user):
        await stream.text("working")
        await stream.done("tool_use")
        await stream.usage()
        for call_id, tool, tool_input in calls:             # all three before any answer
            await stream.proposed(call_id, tool, tool_input, "R1")
        for call_id, tool, _ in calls:
            message = await expect(conn, "tool_result")
            assert message["payload"]["call_id"] == call_id and message["payload"]["ok"] is True
            await stream.tool_finished(call_id, tool, "ok")
        await stream.reply("done")

    out = run_cli(capsys, json_args(mode="accept-edits"), scripted(tail))
    assert out.code == 0, out.err
    assert (project / "s.txt").read_text() == "C\n"          # each edit saw the one before it
    assert out.json()["files_changed"] == ["s.txt"]


def test_a_replayed_tool_proposed_does_not_run_twice(project, capsys):
    (project / "mark.py").write_text("open('count.txt', 'a').write('x')\n")
    proposal = {"command": "python3 mark.py"}

    async def tail(conn, stream, _start, _user):
        await stream.text("working")
        await stream.done("tool_use")
        await stream.usage()
        await stream.proposed("p1", "bash", proposal, "R2")
        await stream.proposed("p1", "bash", proposal, "R2")   # replayed while it is still running
        await expect(conn, "tool_result")
        await stream.tool_finished("p1", "bash", "ok")
        await stream.proposed("p1", "bash", proposal, "R2")   # replayed after it finished
        await stream.reply("done")

    out = run_cli(capsys, json_args("--allow", "bash(python3 mark.py)"), scripted(tail))
    assert out.code == 0, out.err
    assert (project / "count.txt").read_text() == "x"
    assert len(out.server.messages("tool_result")) == 1
    assert out.json()["tool_calls"]["ok"] == 1


# ---- 7. json output -----------------------------------------------------------------------------------------

def test_json_output_is_exactly_one_object_even_with_notes_and_a_denial(project, capsys):
    (project / ".m2c").mkdir()
    (project / ".m2c" / "settings.toml").write_text('[permissions]\nallow = ["bash(touch ran.marker)"]\n')
    out = run_steps(capsys, json_args(), TOUCH_DENIED)
    assert out.code == 3
    doc = out.json()                                        # the rig asserts: one line, one object, nothing else
    assert set(doc) >= {"ok", "exit_code", "session_id", "result", "error", "files_changed", "tool_calls"}
    assert out.err.strip() != ""


# ---- 8. project settings stay ignored -----------------------------------------------------------------------

def test_project_settings_are_ignored_and_one_stderr_line_says_so(project, capsys, monkeypatch):
    from m2c.policy import rules

    opened: list[str] = []
    real = rules.load_rule_file

    def spy(path, layer):
        opened.append(str(path))
        return real(path, layer)

    monkeypatch.setattr(rules, "load_rule_file", spy)
    (project / ".m2c").mkdir()
    (project / ".m2c" / "settings.toml").write_text('[permissions]\nallow = ["bash(touch ran.marker)"]\n')
    out = run_steps(capsys, json_args(), TOUCH_DENIED)
    assert out.code == 3 and not (project / "ran.marker").exists()        # the project's allow rule did nothing
    assert out.err.count("project settings") == 1 and "ignored" in out.err
    assert not any(".m2c" in path for path in opened)                      # and the file was never even opened


def test_no_project_settings_file_means_no_such_line(project, capsys):
    out = run_steps(capsys, json_args(*ALLOW_TOUCH), TOUCH)
    assert out.code == 0 and "project settings" not in out.err


# ---- 9. --cwd -----------------------------------------------------------------------------------------------

def test_cwd_runs_the_task_in_another_folder(project, tmp_path, monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    (project / "a.txt").write_text("one\n")
    out = run_steps(capsys, json_args("--cwd", str(project), mode="accept-edits"), READ, EDIT)
    assert out.code == 0, out.err
    assert (project / "a.txt").read_text() == "two\n"
    assert list(elsewhere.iterdir()) == []
    (start,) = out.server.messages("run.start")
    assert start["payload"]["cwd_label"] == "my-project"


@pytest.mark.parametrize("where", ["/", "HOME", "missing", "a-file"])
def test_cwd_rejects_the_root_the_home_directory_and_what_is_not_a_folder(project, tmp_path, capsys, where):
    if where == "HOME":
        target = os.environ["HOME"]
        Path(target).mkdir(parents=True, exist_ok=True)
    elif where == "missing":
        target = str(tmp_path / "no-such-folder-zq91")
    elif where == "a-file":
        (tmp_path / "plain-zq91").write_text("x")
        target = str(tmp_path / "plain-zq91")
    else:
        target = where
    out = run_steps(capsys, json_args("--cwd", target), TOUCH_DENIED)
    assert out.code == 2
    doc = out.json()
    assert doc["error"]["code"] == "usage" and "--cwd" in doc["error"]["message"]
    assert "zq91" not in out.out + out.err                  # the path is not echoed
    assert out.server.messages() == []


def test_the_default_folder_is_checked_too(tmp_path, monkeypatch, capsys):
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(home)
    out = run_steps(capsys, json_args(), TOUCH_DENIED)
    assert out.code == 2 and out.json()["error"]["code"] == "usage"
    assert out.server.messages() == []


# ---- 10. content hygiene ------------------------------------------------------------------------------------

def test_nothing_about_the_task_reaches_the_logs_or_stderr(project):
    canaries = ("canary-body-8841", "canary-cmd-5521", "canary-path-7730")
    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage() + " " + json.dumps(getattr(record, "__dict__", {}), default=str))

    logger = logging.getLogger("m2c")
    handler = Collect(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        steps = (
            ("w1", "write_file", {"path": f"{canaries[2]}.txt", "content": canaries[0]}, "R1", "tool_result"),
            ("b1", "bash", {"command": f"touch {canaries[1]}"}, "R2", "tool_result"),
        )
        with ThreadedServer(scripted(steps_tail(*steps))) as srv:
            sign_in(srv.url)
            out = call_headless(srv, TASK, mode="accept-edits", output_format="json",
                                tool_setup=make_setup(project, allow=[f"bash(touch {canaries[1]})"]))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    assert out.code == 0, out.err
    assert (project / canaries[1]).exists()
    text = "\n".join(records) + out.err
    for canary in canaries:
        assert canary not in text, canary
    assert records, "the run logged nothing at all: the capture is not working"


# ---- direct use of run_headless is unchanged -------------------------------------------------------------------

def test_run_headless_without_a_tool_setup_declares_no_capability_and_adds_no_keys(project):
    with ThreadedServer(replying("hi")) as srv:
        out = call_headless(srv, TASK, output_format="json")
    assert out.code == 0
    assert "files_changed" not in out.json() and "tool_calls" not in out.json()
    (hello,) = srv.messages("hello")
    assert "tools" not in hello["payload"].get("capabilities", [])
