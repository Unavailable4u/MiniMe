"""
m2c/tests/tools/test_tools_shell.py -- Patch C07: the shell tool v0
(``m2c.tools.shell``).

What is covered (the "Done when" line asks for timeout, kill, env stripping and
truncation; the rest is what the work package lists):

* a normal command and its exit code, a failing command, death by a signal;
* the timeout, and that the WHOLE process group is killed (a child and a
  grandchild are started and both must be gone), including a command that
  ignores SIGTERM and a command that closed its own output;
* cancel through a threading.Event (promptly, and a cancel that is already set
  runs nothing);
* the sanitized environment (fake secrets with many names, the cloud prefixes,
  the extra names from the context, PATH and HOME kept, rc files never sourced);
* output truncation: head and tail, the marker and its exact byte count, a cut
  in the middle of a multi-byte character, and bounded memory for very large
  output (measured with tracemalloc, no gigabytes needed);
* streaming: coalescing, the order of stdout and stderr, escape sequences split
  across reads, control characters, invalid UTF-8;
* stdin is closed, the working directory is pinned, a missing working
  directory is an error and nothing is started;
* authorization fails closed; C06's ``decide`` can still say deny;
* ``timeout_s`` bounds;
* the sandbox warning is given once per session;
* logs never contain the command or its output.

Nothing here looks like a real secret: fake values are built at run time.

Run: cd m2c && pytest tests/tools/test_tools_shell.py -v
"""
from __future__ import annotations

import ast
import codecs
import os
import re
import shutil
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest
from tools_fixtures import write

import m2c
from m2c.policy import Context as PolicyContext
from m2c.policy import Request
from m2c.policy.decide import decide
from m2c.tools import CallEnv, ToolContext, ToolError, ToolOutcome, run_tool
from m2c.tools import shell
from m2c.tools.shell import (
    AUTH_POLICY_ALLOWED,
    AUTH_USER_APPROVED,
    SANDBOX_WARNING,
    ShellConfig,
    TerminalSanitizer,
    configure_shell,
    run_command,
    sanitized_env,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the shell tool targets Linux and WSL2")

SHELL_SRC = Path(m2c.__file__).resolve().parent / "tools" / "shell.py"


# ==========================================================================
# helpers
# ==========================================================================
def run(ctx, command, timeout_s=20, **kw) -> ToolOutcome:
    """run_command with the authorization every test but the refusal tests needs."""
    kw.setdefault("authorization", AUTH_USER_APPROVED)
    return run_command(ctx, command, timeout_s, **kw)


def via_tool(ctx, command, call_env=None, **args):
    """The same, through the real entry point."""
    env = call_env if call_env is not None else CallEnv(authorization=AUTH_USER_APPROVED)
    return run_tool(ctx, "bash", {"command": command, **args}, call_env=env)


def alive(pid: int) -> bool:
    """True while the process exists and is not a zombie (a container's PID 1 may not reap)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def wait_until(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def read_pid(path: Path) -> int:
    assert wait_until(lambda: path.exists() and path.read_text().strip()), f"{path.name} was never written"
    return int(path.read_text().strip())


def fake(label: str, i: int) -> str:
    """A fake secret value built at run time (never a literal)."""
    return f"canary{i}" + "q" * 14 + label.lower()


@pytest.fixture
def quick(proj) -> ToolContext:
    """A context whose kill grace is short, so tests that need SIGKILL do not wait for the default."""
    c = ToolContext(proj)
    configure_shell(c, ShellConfig(kill_grace_s=0.3))
    return c


# ==========================================================================
# the basics
# ==========================================================================
def test_a_normal_command_and_its_exit_code(ctx):
    r = run(ctx, "echo hello")
    assert isinstance(r, ToolOutcome)
    assert r.ok is True and r.exit_code == 0 and r.code is None and r.truncated is False
    assert "hello" in r.output
    assert r.output.rstrip().endswith("[exit status: 0]")


def test_a_failing_command_keeps_its_output_and_exit_code(ctx):
    r = run(ctx, "echo partial; echo oops >&2; exit 3")
    assert r.ok is False and r.exit_code == 3 and r.code == "exit_nonzero"
    assert "partial" in r.output and "oops" in r.output
    assert r.output.rstrip().endswith("[exit status: 3]")


def test_a_command_killed_by_a_signal_reports_the_shell_convention(ctx):
    r = run(ctx, "kill -9 $$")
    assert r.ok is False and r.exit_code == 128 + 9
    assert "signal 9" in r.output and r.output.rstrip().endswith("signal 9)]")


def test_a_command_with_no_output_is_just_the_status_line(ctx):
    assert run(ctx, "true").output == "[exit status: 0]"


def test_the_result_is_untrusted_data_on_the_wire(ctx):
    # protocol/v1 common.json: trust is local_project | local_other | external.  The work package says
    # "untrusted", which is not a wire value; protocol/ wins, and the most distrusted wire value is "external".
    assert shell.SHELL_TRUST == "external"
    assert run(ctx, "true").trust == "external"


def test_through_run_tool_a_failed_command_still_carries_output_and_exit_code(ctx):
    r = via_tool(ctx, "echo MARKER_OUT; exit 4")
    assert r.ok is False and r.exit_code == 4 and r.trust == "external" and r.code == "exit_nonzero"
    assert "MARKER_OUT" in r.output
    payload = r.to_payload("call_1")
    assert payload["exit_code"] == 4 and payload["ok"] is False and payload["trust"] == "external"
    assert set(payload) == {"call_id", "ok", "output", "truncated", "trust", "exit_code", "duration_ms"}
    assert "code" not in payload


def test_through_run_tool_a_successful_command(ctx):
    r = via_tool(ctx, "echo fine", timeout_s=30, reason="a one line reason")
    assert r.ok and r.exit_code == 0 and "fine" in r.output and r.duration_ms >= 0


def test_the_shell_is_bash_when_present_and_never_a_login_or_interactive_shell(ctx, proj, monkeypatch):
    home = Path(os.environ["HOME"])
    for name in (".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc"):
        write(home, name, "echo RC_FILE_SOURCED\n")
    rc_for_bash_env = write(proj.parent, "bashenv.sh", "echo BASH_ENV_SOURCED\n")
    monkeypatch.setenv("BASH_ENV", str(rc_for_bash_env))
    monkeypatch.setenv("ENV", str(rc_for_bash_env))
    r = run(ctx, 'case $- in *i*) echo interactive;; *) echo batch;; esac; shopt -q login_shell && echo login; echo "$BASH_VERSION"')
    assert "RC_FILE_SOURCED" not in r.output and "BASH_ENV_SOURCED" not in r.output
    assert "batch" in r.output and "interactive" not in r.output and "login" not in r.output
    assert shutil.which("bash") is None or re.search(r"\d+\.\d+", r.output.split("batch", 1)[1])


def test_the_shell_falls_back_to_sh_when_bash_is_missing(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sh = bindir / "sh"
    sh.symlink_to(shutil.which("sh"))
    assert shell._find_shell({"PATH": str(bindir)}) == str(sh)
    (bindir / "bash").symlink_to(shutil.which("bash") or shutil.which("sh"))
    assert shell._find_shell({"PATH": str(bindir)}) == str(bindir / "bash")


def test_standard_input_is_closed(ctx):
    started = time.monotonic()
    r = run(ctx, "cat; read answer; echo \"read status $?\"", timeout_s=20)
    assert time.monotonic() - started < 10, "a command waiting for input must fail fast, not hang"
    assert r.exit_code == 0 and "read status 1" in r.output


def test_the_working_directory_is_pinned_to_the_session_root(ctx, proj):
    write(proj, "sub/f.txt", "x")
    r = run(ctx, "pwd; ls")
    assert str(proj) in r.output and "sub" in r.output


def test_a_missing_working_directory_is_an_error_and_nothing_is_started(proj):
    c = ToolContext(proj)
    shutil.rmtree(proj)
    with pytest.raises(ToolError) as e:
        run(c, "echo started > ran.flag")
    assert e.value.code == "not_found"
    r = via_tool(c, "echo started")
    assert r.ok is False and r.code == "not_found" and "started" not in r.output


def test_a_command_with_a_nul_byte_is_refused_as_invalid(ctx):
    with pytest.raises(ToolError) as e:
        run(ctx, "echo a\x00b")
    assert e.value.code == "invalid_args"


# ==========================================================================
# authorization (fail closed) and the policy engine
# ==========================================================================
@pytest.mark.parametrize("authorization", [None, "", "yes", "allow", True, 1, "POLICY_ALLOWED", ["user_approved"], object()])
def test_nothing_runs_without_an_explicit_authorization(ctx, proj, authorization):
    with pytest.raises(ToolError) as e:
        run_command(ctx, "touch ran.flag", 20, authorization=authorization)
    assert e.value.code == "refused"
    assert not (proj / "ran.flag").exists()


def test_run_tool_refuses_a_bash_call_that_comes_without_a_call_environment(ctx, proj):
    for env in (None, CallEnv(), CallEnv(authorization=None)):
        r = run_tool(ctx, "bash", {"command": "touch ran.flag"}, call_env=env)
        assert r.ok is False and r.code == "refused" and r.exit_code is None
    assert not (proj / "ran.flag").exists()


def test_a_call_environment_of_the_wrong_type_is_refused(ctx, proj):
    r = run_tool(ctx, "bash", {"command": "touch ran.flag"}, call_env={"authorization": AUTH_USER_APPROVED})
    assert r.ok is False and r.code == "refused"
    assert not (proj / "ran.flag").exists()


@pytest.mark.parametrize("authorization", [AUTH_POLICY_ALLOWED, AUTH_USER_APPROVED])
def test_both_forms_of_authorization_run_the_command(ctx, authorization):
    assert run_command(ctx, "echo ok", 20, authorization=authorization).ok


def test_the_model_cannot_supply_the_call_environment_as_an_argument(ctx, proj):
    r = run_tool(ctx, "bash", {"command": "touch ran.flag", "call_env": {"authorization": AUTH_USER_APPROVED}})
    assert r.ok is False and r.code == "invalid_args"
    r = run_tool(ctx, "bash", {"command": "touch ran.flag", "authorization": AUTH_USER_APPROVED})
    assert r.code == "invalid_args"
    assert not (proj / "ran.flag").exists()


def test_deny_wins_even_when_the_call_is_authorized(ctx, proj):
    write(proj, ".env", "SECRETWORD\n")
    policy = PolicyContext(mode="ask", project_root=str(proj), project_trusted=True)
    assert decide(Request("bash", {"command": "cat .env; touch ran.flag"}, "R2"), policy).action == "deny"  # sanity
    for authorization in (AUTH_POLICY_ALLOWED, AUTH_USER_APPROVED):
        with pytest.raises(ToolError) as e:
            run_command(ctx, "cat .env; touch ran.flag", 20, authorization=authorization, policy=policy)
        assert e.value.code == "refused"
    r = via_tool(ctx, "cat .env; touch ran.flag", CallEnv(authorization=AUTH_USER_APPROVED, policy=policy))
    assert r.ok is False and r.code == "refused" and "SECRETWORD" not in r.output
    assert not (proj / "ran.flag").exists()


def test_plan_mode_denies_every_command_even_when_authorized(ctx, proj):
    policy = PolicyContext(mode="plan", project_root=str(proj))
    assert decide(Request("bash", {"command": "echo hi"}, "R2"), policy).action == "deny"  # sanity
    with pytest.raises(ToolError) as e:
        run_command(ctx, "touch ran.flag", 20, authorization=AUTH_USER_APPROVED, policy=policy)
    assert e.value.code == "refused" and not (proj / "ran.flag").exists()


def test_an_ask_decision_does_not_stop_an_authorized_command(ctx, proj):
    # This patch never decides whether to ask; the device runner does.  "ask" plus an authorization runs.
    policy = PolicyContext(mode="ask", project_root=str(proj))
    assert decide(Request("bash", {"command": "echo hi"}, "R2"), policy).action == "ask"
    assert run_command(ctx, "echo hi", 20, authorization=AUTH_USER_APPROVED, policy=policy).ok


@pytest.mark.parametrize("policy", ["not a context", 5, {"mode": "ask"}, object()])
def test_a_policy_that_cannot_be_evaluated_refuses(ctx, proj, policy):
    with pytest.raises(ToolError) as e:
        run_command(ctx, "touch ran.flag", 20, authorization=AUTH_USER_APPROVED, policy=policy)
    assert e.value.code == "refused" and not (proj / "ran.flag").exists()


def test_a_policy_engine_that_raises_refuses(ctx, proj, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("MARKER_POLICY_BUG")

    monkeypatch.setattr(shell, "decide", boom)
    with pytest.raises(ToolError) as e:
        run_command(ctx, "touch ran.flag", 20, authorization=AUTH_USER_APPROVED, policy=PolicyContext())
    assert e.value.code == "refused" and "MARKER" not in e.value.message and not (ctx.root / "ran.flag").exists()


def test_a_decision_that_is_not_allow_or_ask_refuses(ctx, monkeypatch):
    class Odd:
        action = "something-new"

    monkeypatch.setattr(shell, "decide", lambda *a, **k: Odd())
    with pytest.raises(ToolError) as e:
        run_command(ctx, "echo hi", 20, authorization=AUTH_USER_APPROVED, policy=PolicyContext())
    assert e.value.code == "refused"


def test_the_refusal_message_never_repeats_the_command(ctx, proj):
    policy = PolicyContext(mode="plan", project_root=str(proj))
    r = via_tool(ctx, "echo MARKER_COMMAND_TEXT", CallEnv(authorization=AUTH_USER_APPROVED, policy=policy))
    assert not r.ok and "MARKER" not in r.output
    r = via_tool(ctx, "echo MARKER_COMMAND_TEXT", CallEnv())
    assert not r.ok and "MARKER" not in r.output


# ==========================================================================
# timeout_s
# ==========================================================================
@pytest.mark.parametrize("bad", [0, -1, 3601, 10**9, True, False, 1.5, "5", None, [5]])
def test_timeout_bounds_are_enforced(ctx, proj, bad):
    with pytest.raises(ToolError) as e:
        run_command(ctx, "touch ran.flag", bad, authorization=AUTH_USER_APPROVED)
    assert e.value.code == "invalid_args"
    assert not (proj / "ran.flag").exists()


@pytest.mark.parametrize("bad", [0, -5, 3601])
def test_run_tool_enforces_the_same_bounds(ctx, proj, bad):
    r = via_tool(ctx, "touch ran.flag", timeout_s=bad)
    assert r.ok is False and r.code == "invalid_args" and not (proj / "ran.flag").exists()


@pytest.mark.parametrize("good", [1, 120, 3600])
def test_the_limits_themselves_are_accepted(ctx, good):
    assert run(ctx, "true", good).ok


def test_the_default_timeout_is_the_schemas_default(ctx):
    import inspect

    assert shell.DEFAULT_TIMEOUT_S == 120
    assert inspect.signature(shell.bash).parameters["timeout_s"].default == 120
    assert inspect.signature(run_command).parameters["timeout_s"].default == 120


def test_a_reason_longer_than_the_schema_allows_is_refused(ctx, proj):
    r = via_tool(ctx, "touch ran.flag", reason="r" * 201)
    assert r.ok is False and r.code == "invalid_args" and not (proj / "ran.flag").exists()
    assert via_tool(ctx, "true", reason="r" * 200).ok


# ==========================================================================
# the timeout, and killing the whole process group
# ==========================================================================
def test_a_command_that_runs_too_long_is_killed_and_reported_as_timed_out(quick):
    started = time.monotonic()
    r = run(quick, "echo before; sleep 300", 1)
    elapsed = time.monotonic() - started
    assert r.ok is False and r.code == "timeout" and r.exit_code is None
    assert "before" in r.output, "output produced before the kill is kept"
    assert "timed out after 1s" in r.output
    assert 0.9 <= elapsed < 8


def test_the_whole_process_group_is_killed_on_timeout(quick, proj):
    # the shell starts a child, which starts a grandchild; both must be gone afterwards
    script = "sh -c 'sleep 300 & echo $! > grandchild.pid; wait' & echo $! > child.pid; wait"
    r = run(quick, script, 1)
    assert r.code == "timeout"
    child, grandchild = read_pid(proj / "child.pid"), read_pid(proj / "grandchild.pid")
    assert wait_until(lambda: not alive(child)), "the child survived the timeout"
    assert wait_until(lambda: not alive(grandchild)), "the grandchild survived the timeout"


def test_a_command_that_ignores_sigterm_is_killed_after_the_grace_period(quick, proj):
    started = time.monotonic()
    r = run(quick, "trap '' TERM; sleep 300 & echo $! > pid; wait", 1)
    assert r.code == "timeout"
    assert time.monotonic() - started < 10
    assert wait_until(lambda: not alive(read_pid(proj / "pid"))), "SIGKILL must follow SIGTERM"


def test_the_process_is_started_in_its_own_session(ctx):
    r = run(ctx, 'echo "$$ $(cut -d" " -f5,6 /proc/$$/stat)"')  # fields 5 and 6 of /proc/PID/stat: pgrp and session
    pid, pgid, sid = (int(x) for x in re.search(r"(\d+) (\d+) (\d+)", r.output).groups())
    assert pid == pgid == sid, "the child must lead its own process group (and session)"
    assert pid != os.getpid() and os.getpgid(0) != pgid


def test_a_command_that_closed_its_own_output_is_still_timed_out(quick, proj):
    # no pipe is open any more, but the shell is still running: the loop must keep enforcing the deadline
    r = run(quick, "exec >&- 2>&-; echo $$ > /dev/null; sleep 300", 1)
    assert r.code == "timeout"


def test_background_processes_left_in_the_group_are_stopped_when_the_command_ends(quick, proj):
    started = time.monotonic()
    r = run(quick, "sleep 300 & echo $! > bg.pid; echo done", 30)
    assert r.ok and "done" in r.output
    assert time.monotonic() - started < 10, "a background job holding the pipes must not block the result"
    assert wait_until(lambda: not alive(read_pid(proj / "bg.pid")))


def test_a_background_job_that_redirected_its_output_is_stopped_too(quick, proj):
    r = run(quick, "sleep 300 >/dev/null 2>&1 & echo $! > bg.pid; echo done", 30)
    assert r.ok
    assert wait_until(lambda: not alive(read_pid(proj / "bg.pid")))


# ==========================================================================
# cancel
# ==========================================================================
def test_cancel_kills_the_process_group_promptly(quick, proj):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    r = run(quick, "sleep 300 & echo $! > pid; echo started; wait", 60, cancel=cancel)
    assert r.ok is False and r.code == "cancelled" and "started" in r.output
    assert "cancelled" in r.output
    assert time.monotonic() - started < 6
    assert wait_until(lambda: not alive(read_pid(proj / "pid")))


def test_a_cancel_that_is_already_set_runs_nothing(ctx, proj):
    cancel = threading.Event()
    cancel.set()
    r = run(ctx, "touch ran.flag", 20, cancel=cancel)
    assert r.ok is False and r.code == "cancelled"
    assert not (proj / "ran.flag").exists()


def test_a_cancel_handle_that_raises_stops_the_command(quick):
    class Broken:
        def is_set(self):
            raise RuntimeError("boom")

    r = run(quick, "sleep 300", 30, cancel=Broken())
    assert r.code == "cancelled"


def test_cancel_through_run_tool(quick):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    r = run_tool(quick, "bash", {"command": "sleep 300"}, call_env=CallEnv(authorization=AUTH_USER_APPROVED, cancel=cancel))
    assert r.ok is False and r.code == "cancelled"


# ==========================================================================
# the sanitized environment
# ==========================================================================
STRIPPED_NAMES = [
    "MY_API_KEY", "KEYBOARD", "github_token", "CLIENT_SECRET", "db_password", "LEGACY_PASSWD",
    "SERVICE_CREDENTIALS", "Mixed_Case_Secret_Thing",
    "AWS_PROFILE", "AWS_REGION", "AZURE_TENANT", "GOOGLE_APPLICATION_THING", "GCP_PROJECT_NAME",
    "GCLOUD_CONFIG", "aws_lowercase", "SSH_AUTH_SOCK", "KUBECONFIG", "DATABASE_URL",
    "M2C_SOMETHING", "M2C_HOME", "BASH_ENV", "ENV", "BASH_FUNC_evil%%",
]
KEPT_NAMES = ["LANG", "EDITOR", "PROJECT_NAME", "KEEP_ME_VISIBLE", "TZ", "MONOREPO_SETTING", "DISPLAY"]


def test_sanitized_env_removes_every_listed_name_case_insensitively():
    base = {name: fake("x", i) for i, name in enumerate(STRIPPED_NAMES + KEPT_NAMES)}
    base["PATH"] = "/usr/bin:/bin"
    base["HOME"] = "/home/someone"
    out = sanitized_env(base)
    for name in STRIPPED_NAMES:
        assert name not in out, name
    for name in KEPT_NAMES:
        assert out[name] == base[name], name
    assert out["PATH"] == "/usr/bin:/bin" and out["HOME"] == "/home/someone"
    assert out["TERM"] == "dumb" and out["NO_COLOR"] == "1"
    assert out["PAGER"] == "cat" and out["GIT_PAGER"] == "cat" and out["GIT_TERMINAL_PROMPT"] == "0"


def test_sanitized_env_overrides_the_terminal_variables_even_when_the_user_set_them():
    out = sanitized_env({"TERM": "xterm-256color", "PAGER": "less", "GIT_PAGER": "delta", "NO_COLOR": "", "PATH": "/bin", "HOME": "/h"})
    assert (out["TERM"], out["PAGER"], out["GIT_PAGER"], out["NO_COLOR"]) == ("dumb", "cat", "cat", "1")


def test_sanitized_env_never_removes_path_or_home_even_if_asked():
    out = sanitized_env({"PATH": "/bin", "HOME": "/h"}, extra=("PATH", "HOME", "P*"))
    assert out["PATH"] == "/bin" and out["HOME"] == "/h"


def test_sanitized_env_gives_a_default_path_when_there_is_none():
    assert sanitized_env({"HOME": "/h"})["PATH"]


def test_extra_names_are_exact_or_prefix_and_case_insensitive():
    base = {"MY_APP_CONFIG": "1", "my_app_config_2": "2", "INTERNAL_A": "3", "internal_b": "4", "INTERNALX": "5", "PATH": "/bin", "HOME": "/h"}
    out = sanitized_env(base, extra=("my_app_config", "INTERNAL_*"))
    assert "MY_APP_CONFIG" not in out and "INTERNAL_A" not in out and "internal_b" not in out
    assert out["my_app_config_2"] == "2" and out["INTERNALX"] == "5"


def test_no_secret_reaches_the_command(ctx, monkeypatch):
    values = {}
    for i, name in enumerate(n for n in STRIPPED_NAMES if n not in ("BASH_ENV", "ENV") and "%" not in n):
        values[name] = fake("secret", i)
        monkeypatch.setenv(name, values[name])
    monkeypatch.setenv("KEEP_ME_VISIBLE", "visible-value")
    r = run(ctx, "env")
    assert r.ok
    for name, value in values.items():
        assert value not in r.output, f"the value of {name} reached the command"
        assert not re.search(rf"^{re.escape(name)}=", r.output, re.M | re.I), name
    assert "KEEP_ME_VISIBLE=visible-value" in r.output
    assert f"PATH={os.environ['PATH']}" in r.output
    assert f"HOME={os.environ['HOME']}" in r.output
    for line in ("TERM=dumb", "NO_COLOR=1", "PAGER=cat", "GIT_PAGER=cat", "GIT_TERMINAL_PROMPT=0"):
        assert line in r.output


def test_extra_names_from_the_context_are_stripped_in_a_real_command(proj, monkeypatch):
    c = ToolContext(proj)
    configure_shell(c, ShellConfig(extra_strip_names=("MY_APP_CONFIG", "INTERNAL_*")))
    monkeypatch.setenv("MY_APP_CONFIG", "canary-config")
    monkeypatch.setenv("INTERNAL_ENDPOINT", "canary-endpoint")
    monkeypatch.setenv("UNRELATED", "canary-unrelated")
    out = run(c, "env").output
    assert "canary-config" not in out and "canary-endpoint" not in out and "UNRELATED=canary-unrelated" in out


def test_the_environment_is_rebuilt_for_every_command(ctx, monkeypatch):
    monkeypatch.setenv("LATE_VARIABLE", "late-value")
    assert "LATE_VARIABLE=late-value" in run(ctx, "env").output
    monkeypatch.delenv("LATE_VARIABLE")
    assert "LATE_VARIABLE" not in run(ctx, "env").output


# ==========================================================================
# truncation and bounded memory
# ==========================================================================
def test_big_output_keeps_head_and_tail_with_a_marker(proj):
    c = ToolContext(proj)
    r = run(c, "seq 1 100000")
    assert r.ok and r.truncated is True
    assert len(r.output) <= shell.DEFAULT_MAX_OUTPUT_BYTES + 300
    assert r.output.startswith("[stdout]\n1\n2\n3\n")
    assert "100000\n[exit status: 0]" in r.output.replace("\r", "")
    m = re.search(r"\[\.\.\. (\d+) bytes omitted \.\.\.\]", r.output)
    assert m and int(m.group(1)) > 400_000


def test_output_under_the_cap_is_not_truncated(ctx):
    r = run(ctx, "seq 1 100")
    assert r.truncated is False and "bytes omitted" not in r.output and "100\n" in r.output


def test_the_cap_is_injectable(proj):
    c = ToolContext(proj)
    configure_shell(c, ShellConfig(max_output_bytes=1000))
    r = run(c, "seq 1 5000")
    assert r.truncated is True and len(r.output) < 1400
    assert "5000" in r.output and r.output.startswith("[stdout]\n1\n")


def test_truncation_counts_omitted_bytes_exactly():
    buf = shell._OutputBuffer(100)  # head 50, tail 50
    buf.add("stdout", "a" * 70)
    buf.add("stdout", "b" * 70)
    text, truncated, total = buf.result()
    assert total == len("[stdout]\n") + 140 and truncated is True
    head = "[stdout]\n" + "a" * 41
    assert text == head + "\n[... 49 bytes omitted ...]\n" + "b" * 50


def test_the_marker_follows_a_head_that_already_ends_in_a_newline():
    buf = shell._OutputBuffer(18)  # head 9 bytes: exactly the "[stdout]" header line
    buf.add("stdout", "0123456789\n" * 5)
    text, truncated, total = buf.result()
    assert truncated and text.startswith("[stdout]\n[... ") and "\n\n[... " not in text


def test_a_cut_in_the_middle_of_a_multibyte_character_leaves_no_replacement_characters():
    buf = shell._OutputBuffer(101)  # odd sizes so both cuts can land inside a character
    buf.add("stdout", "é" * 200)
    text, truncated, total = buf.result()
    assert truncated and "�" not in text
    head_part, omitted, tail_part = re.split(r"\[\.\.\. (\d+) bytes omitted \.\.\.\]\n", text)
    kept = len(head_part.removesuffix("\n").encode()) + len(tail_part.encode())
    assert int(omitted) + kept == total


def test_nothing_is_omitted_when_the_output_fits_exactly():
    buf = shell._OutputBuffer(100)
    buf.add("stdout", "a" * (100 - len("[stdout]\n")))
    text, truncated, total = buf.result()
    assert not truncated and total == 100 and "omitted" not in text


def test_output_of_both_streams_is_capped_together():
    buf = shell._OutputBuffer(200)
    for _ in range(100):
        buf.add("stdout", "o" * 50 + "\n")
        buf.add("stderr", "e" * 50 + "\n")
    text, truncated, total = buf.result()
    assert truncated and len(text.encode()) < 400


def test_memory_stays_bounded_for_gigabytes_of_output():
    """Feed 512 MiB through the same decode, sanitize and buffer path the runner uses and measure the Python heap."""
    chunk = (b"abcdefghijklmnopqrstuvwxyz0123456789" * 2000)[:65536]
    flt = shell._StreamFilter()
    buf = shell._OutputBuffer(100_000)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        for _ in range(512 * 1024 * 1024 // len(chunk)):
            buf.add("stdout", flt.feed(chunk))
        text, truncated, total = buf.result()
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    assert truncated and total >= 512 * 1024 * 1024
    assert len(text) < 101_000
    assert peak < 4 * 1024 * 1024, f"peak {peak} bytes for 512 MiB of input"


def test_a_real_command_producing_a_lot_of_output_is_bounded_and_fast(ctx):
    started = time.monotonic()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        r = run(ctx, "yes abcdefghijklmnopqrstuvwxyz | head -c 67108864", 120)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    assert r.ok and r.truncated
    m = re.search(r"\[\.\.\. (\d+) bytes omitted \.\.\.\]", r.output)
    assert m and int(m.group(1)) > 60 * 1024 * 1024
    assert len(r.output) < shell.DEFAULT_MAX_OUTPUT_BYTES + 300
    assert peak < 8 * 1024 * 1024, f"peak {peak} bytes"
    assert time.monotonic() - started < 60


def test_a_command_that_never_stops_writing_is_cut_off_by_the_timeout_not_by_memory(quick):
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        r = run(quick, "yes", 1)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    assert r.code == "timeout" and r.truncated
    assert peak < 8 * 1024 * 1024


def test_the_shell_config_validates_its_numbers():
    for bad in ({"max_output_bytes": 0}, {"max_output_bytes": 200_000}, {"max_output_bytes": True}, {"kill_grace_s": 0},
                {"drain_grace_s": -1}, {"flush_bytes": 0}, {"flush_interval_s": float("nan")}, {"warn": "not callable"},
                {"extra_strip_names": "MY_VAR"}, {"extra_strip_names": ("A=B",)}, {"extra_strip_names": ("",)},
                {"extra_strip_names": (5,)}):
        with pytest.raises(ValueError):
            ShellConfig(**bad)
    assert ShellConfig().max_output_bytes == shell.DEFAULT_MAX_OUTPUT_BYTES <= 190_000


def test_a_context_with_a_wrong_shell_config_refuses(proj):
    c = ToolContext(proj)
    c.shell_config = "not a ShellConfig"
    with pytest.raises(ToolError):
        run(c, "touch ran.flag")
    assert not (proj / "ran.flag").exists()


# ==========================================================================
# streaming
# ==========================================================================
def test_small_chunks_are_coalesced_and_nothing_is_lost(ctx):
    chunks = []
    r = run(ctx, "for i in $(seq 1 20000); do echo line$i; done", 60, on_chunk=lambda s, t: chunks.append((s, t)))
    assert r.ok
    expected = "".join(f"line{i}\n" for i in range(1, 20001))
    assert "".join(t for s, t in chunks if s == "stdout") == expected
    assert len(chunks) <= 120, f"{len(chunks)} callbacks for {len(expected)} bytes: not coalesced"
    assert all(s in ("stdout", "stderr") for s, _ in chunks)
    big = [t for _, t in chunks[:-1] if len(t) >= shell.FLUSH_BYTES]
    assert big, "full-size chunks are expected while output is flowing"


def test_a_pause_flushes_what_has_arrived_without_waiting_for_a_full_chunk(ctx):
    seen = []
    t0 = time.monotonic()
    r = run(ctx, "echo first; sleep 0.8; echo second", 20, on_chunk=lambda s, t: seen.append((s, t, time.monotonic() - t0)))
    assert r.ok
    assert [(s, t) for s, t, _ in seen] == [("stdout", "first\n"), ("stdout", "second\n")]
    assert seen[0][2] < 0.6, "the first line must be delivered during the pause, not at the end"


def test_stdout_and_stderr_chunks_arrive_in_the_order_they_were_written(ctx):
    seen = []
    r = run(ctx, "echo o1; sleep 0.4; echo e1 >&2; sleep 0.4; echo o2", 20, on_chunk=lambda s, t: seen.append((s, t)))
    assert seen == [("stdout", "o1\n"), ("stderr", "e1\n"), ("stdout", "o2\n")]
    out = r.output
    assert out.index("o1") < out.index("e1") < out.index("o2")


def test_the_final_output_marks_each_stream_switch(ctx):
    r = run(ctx, "echo o1; sleep 0.3; echo e1 >&2; sleep 0.3; echo o2; sleep 0.3; echo o3")
    assert r.output == "[stdout]\no1\n[stderr]\ne1\n[stdout]\no2\no3\n[exit status: 0]"


def test_output_without_a_final_newline_still_ends_with_the_status_on_its_own_line(ctx):
    assert run(ctx, "printf abc").output == "[stdout]\nabc\n[exit status: 0]"


def test_a_failing_callback_never_breaks_the_command(ctx):
    calls = []

    def bad(stream, text):
        calls.append(text)
        raise RuntimeError("display is broken")

    r = run(ctx, "echo a; sleep 0.3; echo b", 20, on_chunk=bad)
    assert r.ok and "a" in r.output and "b" in r.output
    assert len(calls) == 1, "after a failure the callback is not called again"


def test_on_chunk_is_optional(ctx):
    assert run(ctx, "echo x", 20, on_chunk=None).ok


# ==========================================================================
# terminal escape sequences, control characters and bad UTF-8
# ==========================================================================
def test_escape_sequences_never_reach_the_output_or_the_stream(ctx):
    seen = []
    cmd = (
        r"printf '\033[31mred\033[0m \033]0;window title\007visible \033]8;;http://example.invalid\033\\link\033]8;;\033\\ "
        r"\033P1$rdcs\033\\after-dcs \033(Bcharset \033[?25lhidden-cursor \033[2J\033[Hclear\a\bX\tTAB\rCR\n'"
    )
    r = run(ctx, cmd, 20, on_chunk=lambda s, t: seen.append(t))
    streamed = "".join(seen)
    for text in (r.output, streamed):
        assert "\x1b" not in text and "\x07" not in text and "\x08" not in text and "\x9b" not in text
        assert "window title" not in text and "example.invalid" not in text and "dcs" not in text.replace("after-dcs", "")
        for word in ("red", "visible", "link", "after-dcs", "charset", "hidden-cursor", "clear", "X", "TAB\rCR\n"):
            assert word in text, word
        assert "[31m" not in text and "[0m" not in text and "[2J" not in text
    assert "\t" in r.output and "\r" in r.output and "\n" in r.output


def test_an_escape_sequence_split_across_reads_is_still_removed(ctx):
    r = run(ctx, r"printf '\033[3'; sleep 0.3; printf '1mred\033]0;ti'; sleep 0.3; printf 'tle\007 done\n'")
    assert "red done" in r.output
    assert "[31m" not in r.output and "title" not in r.output and "\x1b" not in r.output


def test_control_characters_other_than_newline_return_and_tab_are_removed(ctx):
    r = run(ctx, r"printf 'a\000b\001c\002d\010e\013f\014g\016h\037i\177j\n\tk\rl\n'")
    assert "abcdefghij\n\tk\rl\n" in r.output
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", r.output)


def test_invalid_utf8_is_replaced_not_fatal(ctx):
    seen = []
    r = run(ctx, r"printf 'ok\377\376 mid\303 end\n'", 20, on_chunk=lambda s, t: seen.append(t))
    assert r.ok and "ok�� mid� end" in r.output
    assert "ok�" in "".join(seen)


def test_a_multibyte_character_split_across_reads_is_decoded(ctx):
    r = run(ctx, r"printf '\303'; sleep 0.3; printf '\251\n'")
    assert "é" in r.output and "�" not in r.output


def test_the_streaming_sanitizer_handles_one_character_at_a_time():
    raw = "\x1b[31mred\x1b[0m \x1b]0;title\x07 ok \x1b]8;;u\x1b\\link\x1b]8;;\x1b\\ \x1bP1$r\x1b\\ \x1b(B end\n"
    s = TerminalSanitizer()
    assert "".join(s.feed(c) for c in raw) == "red  ok link   end\n"
    assert TerminalSanitizer().feed(raw) == "red  ok link   end\n"


@pytest.mark.parametrize("raw, want", [
    ("plain text\n", "plain text\n"),
    ("a\x1b[1;31;40mb\x1b[mc", "abc"),
    ("a\x1b[38;2;1;2;3mb", "ab"),
    ("a\x1b[?1049hb", "ab"),
    ("a\x1b[1 qb", "ab"),                      # CSI with an intermediate byte
    ("a\x1bcb", "ab"),                         # RIS
    ("a\x1b7b\x1b8c", "abc"),
    ("a\x1b]0;t\x1b\\b", "ab"),                # OSC ended by ST
    ("a\x1b]0;t\x07b", "ab"),                  # OSC ended by BEL
    ("a\x1b^privacy\x1b\\b", "ab"),            # PM
    ("a\x1b_apc\x1b\\b", "ab"),                # APC
    ("a\x1bXsos\x1b\\b", "ab"),                # SOS
    ("a\x9bb\x9d\x9cc", "abc"),                # C1 controls
    ("a\x1b\nb", "a\nb"),                      # a lone ESC does not eat the newline
    ("a\x1b[3\nb", "a\nb"),                    # an aborted CSI does not eat the newline
    ("a\x1b\x1b[31mb", "ab"),                  # ESC ESC [
    ("a\x1b]0;t\x1b[31mb", "ab"),              # an OSC cut off by another escape: the new sequence is handled
    ("tab\there\r\n", "tab\there\r\n"),
])
def test_sanitizer_cases(raw, want):
    assert TerminalSanitizer().feed(raw) == want


def test_an_unterminated_string_sequence_cannot_swallow_the_rest_of_the_output():
    s = TerminalSanitizer()
    out = s.feed("before \x1b]0;" + "x" * 10_000 + "\nafter the long title\n")
    assert out.startswith("before ") and "after the long title\n" in out


def test_a_very_long_csi_gives_up_instead_of_swallowing_output():
    out = TerminalSanitizer().feed("a\x1b[" + "1;" * 500 + "\nb\n")
    assert "b\n" in out


def test_the_fast_path_returns_clean_text_unchanged_and_the_stream_filter_flushes_a_dangling_byte():
    flt = shell._StreamFilter()
    assert flt.feed(b"clean text\n") == "clean text\n"
    assert flt.feed(b"\xc3") == ""
    assert flt.close() == "�"
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    assert dec.decode(b"\xc3") == ""  # the reference behaviour the filter relies on


# ==========================================================================
# the sandbox warning
# ==========================================================================
def test_the_warning_text_is_the_one_in_the_work_package():
    assert SANDBOX_WARNING == "commands run with your full privileges; there is no sandbox yet"


def test_the_warning_is_given_once_per_session_before_the_first_command(proj):
    events = []
    c = ToolContext(proj)
    configure_shell(c, ShellConfig(warn=lambda text: events.append(("warn", text))))
    run(c, "echo one", 20, on_chunk=lambda s, t: events.append(("chunk", t)))
    run(c, "echo two", 20, on_chunk=lambda s, t: events.append(("chunk", t)))
    assert events[0] == ("warn", SANDBOX_WARNING)
    assert [e for e in events if e[0] == "warn"] == [("warn", SANDBOX_WARNING)]
    assert [t for k, t in events if k == "chunk"] == ["one\n", "two\n"]


def test_each_session_is_warned_separately(proj):
    texts = []
    for _ in range(2):
        c = ToolContext(proj)
        configure_shell(c, ShellConfig(warn=texts.append))
        run(c, "true")
        run(c, "true")
    assert texts == [SANDBOX_WARNING, SANDBOX_WARNING]


def test_a_refused_command_does_not_use_up_the_warning(proj):
    texts = []
    c = ToolContext(proj)
    configure_shell(c, ShellConfig(warn=texts.append))
    with pytest.raises(ToolError):
        run_command(c, "true", 20, authorization=None)
    assert texts == []
    run(c, "true")
    assert texts == [SANDBOX_WARNING]


def test_a_warn_callable_that_fails_does_not_stop_the_command(proj):
    def broken(text):
        raise RuntimeError("terminal is gone")

    c = ToolContext(proj)
    configure_shell(c, ShellConfig(warn=broken))
    assert run(c, "echo still-runs").ok


def test_no_warning_callable_is_fine(ctx):
    assert run(ctx, "true").ok


def test_nothing_in_this_patch_enables_an_auto_approve_mode():
    src = SHELL_SRC.read_text().lower()
    assert "auto_approve" not in src and "auto-approve" not in src and "yolo" not in src


# ==========================================================================
# logging
# ==========================================================================
def test_logs_contain_the_exit_status_and_sizes_but_never_the_command_or_output(ctx, caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="m2c"):
        via_tool(ctx, "echo MARKER_CMD_OUT; echo MARKER_CMD_ERR >&2; exit 2", reason="MARKER_REASON_TEXT")
        via_tool(ctx, "echo MARKER_SECOND_COMMAND")
        run_tool(ctx, "bash", {"command": "echo MARKER_REFUSED_COMMAND"}, call_env=CallEnv())
        with pytest.raises(ToolError):
            run(ctx, "echo MARKER_BAD_TIMEOUT", 0)
    assert "MARKER" not in caplog.text
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tools.shell")]
    assert any("exit_code=2" in line and "check=bash" in line and "status=error" in line for line in lines), lines
    assert any("exit_code=0" in line and "status=ok" in line for line in lines), lines
    assert all(re.search(r"count=\d+", line) and re.search(r"duration_ms=\d+", line) for line in lines)
    assert "tools.call check=bash" in caplog.text


def test_timeouts_and_cancels_are_logged_by_code_only(quick, caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="m2c"):
        run(quick, "echo MARKER_BEFORE_TIMEOUT; sleep 300", 1)
    assert "MARKER" not in caplog.text and "code=timeout" in caplog.text


def test_a_failing_callback_is_logged_by_type_only(ctx, caplog):
    import logging

    def bad(stream, text):
        raise RuntimeError("MARKER_CALLBACK_MESSAGE")

    with caplog.at_level(logging.DEBUG, logger="m2c"):
        run(ctx, "echo MARKER_OUTPUT", 20, on_chunk=bad)
    assert "MARKER" not in caplog.text and "RuntimeError" in caplog.text


# ==========================================================================
# how the module is written (the same source checks C05 applies to its modules)
# ==========================================================================
def test_the_shell_is_never_started_through_shell_true_or_with_inherited_stdin():
    tree = ast.parse(SHELL_SRC.read_text())
    popens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "Popen"]
    assert len(popens) == 1
    kw = {k.arg: k.value for k in popens[0].keywords}
    assert "shell" not in kw
    assert isinstance(kw["start_new_session"], ast.Constant) and kw["start_new_session"].value is True
    assert ast.unparse(kw["stdin"]) == "subprocess.DEVNULL"
    assert ast.unparse(popens[0].args[0]).startswith("[") or isinstance(popens[0].args[0], ast.Name)


def test_the_shell_is_always_run_with_dash_c_and_never_as_a_login_shell():
    src = SHELL_SRC.read_text()
    assert '"-c"' in src
    assert '"-l"' not in src and '"-i"' not in src and '"--login"' not in src and '"-lc"' not in src
