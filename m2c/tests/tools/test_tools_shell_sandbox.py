"""
m2c/tests/tools/test_tools_shell_sandbox.py -- Patch C11a: how the shell tool uses the sandbox (``ShellConfig.sandbox``).

No bubblewrap is needed.  ``m2c.sandbox.get_status`` is replaced by a fake that says "available" or "not available,
for this reason", and ``m2c.sandbox.wrap_command`` by one that builds a command through ``fake-bwrap``: a small
script that records that it was used and then runs what follows the ``--``.  So these tests prove the WIRING:

* auto: sandbox when the probe passed; otherwise unsandboxed, with the warning, once, and the reason;
* required: no sandbox, no command, a fixed error;
* off: the probe is never even asked, the warning says why;
* once the probe passed, a profile that cannot be built NOW is a refusal and never a quiet run without the sandbox
  (otherwise ``ln -s x .git`` would be a way for the model to switch the sandbox off);
* nothing may call the sandbox active, or say "full privileges", when it is the other way round;
* the status lines: 129..159 under the sandbox read "(terminated by signal N)".

The real bubblewrap is exercised in tests/sandbox/test_sandbox_escape.py and test_sandbox_shell_regression.py.

Run: cd m2c && pytest tests/tools/test_tools_shell_sandbox.py -v
"""
from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import pytest
from tools_fixtures import write  # noqa: F401  (keeps the sibling import style of the other tool tests)

from m2c import sandbox
from m2c.sandbox.profile import ProfileError
from m2c.tools import ToolContext, ToolError
from m2c.tools import shell
from m2c.tools.shell import (
    AUTH_USER_APPROVED,
    SANDBOX_MODES,
    SANDBOX_REQUIRED_MESSAGE,
    SANDBOX_WARNING,
    ShellConfig,
    configure_shell,
    run_command,
    sandbox_active,
    warning_text,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the shell tool targets Linux and WSL2")


# ==========================================================================
# fakes
# ==========================================================================
def status(available: bool, code: str = "ok", reason: str = ""):
    return sandbox.SandboxStatus(available, code, reason or ("the sandbox works" if available else sandbox.probe.REASONS.get(code, code)), None)


class Rig:
    """Installs a fake status, a fake wrapper and records what the shell tool asked of them."""

    def __init__(self, monkeypatch, tmp_path, available: bool, code="bwrap_missing", reason=""):
        self.status_calls: list[dict] = []
        self.wrap_calls: list[dict] = []
        self.mark = tmp_path / "wrapped.marker"
        self.fake_bwrap = tmp_path / "fake-bwrap"
        self.fake_bwrap.write_text(f'#!/bin/sh\necho wrapped >> {self.mark}\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
        self.fake_bwrap.chmod(0o755)
        self.result = status(available, code if not available else "ok", reason)
        self.wrap_error: ProfileError | None = None

        def get_status(**kw):
            self.status_calls.append(kw)
            return self.result

        def wrap(command, **kw):
            self.wrap_calls.append({"command": command, **kw})
            if self.wrap_error is not None:
                raise self.wrap_error
            return [str(self.fake_bwrap), "--", "/bin/sh", "-c", command]

        monkeypatch.setattr(sandbox, "get_status", get_status)
        monkeypatch.setattr(sandbox, "wrap_command", wrap)

    @property
    def wrapped(self) -> bool:
        return self.mark.exists()


@pytest.fixture
def working(monkeypatch, tmp_path):
    return Rig(monkeypatch, tmp_path, True)


@pytest.fixture
def broken(monkeypatch, tmp_path):
    return Rig(monkeypatch, tmp_path, False, "userns_unavailable")


def make(proj, **cfg):
    warnings: list[str] = []
    context = ToolContext(proj)
    configure_shell(context, ShellConfig(warn=warnings.append, kill_grace_s=0.3, **cfg))
    return context, warnings


def run(context, command="echo hi", timeout_s=20, **kw):
    return run_command(context, command, timeout_s, authorization=AUTH_USER_APPROVED, **kw)


# ==========================================================================
# the setting
# ==========================================================================
def test_the_default_is_auto_and_the_modes_are_the_three_in_the_work_package():
    assert ShellConfig().sandbox == "auto"
    assert SANDBOX_MODES == ("auto", "required", "off")
    assert ShellConfig().sandbox_extra_ro == () and ".nvm" in ShellConfig().sandbox_toolchain_dirs


@pytest.mark.parametrize("bad", ["", "AUTO", "on", "yes", "none", None, True, 1, ["auto"]])
def test_an_unknown_mode_is_refused_at_construction(bad):
    with pytest.raises(ValueError):
        ShellConfig(sandbox=bad)


@pytest.mark.parametrize("bad", ["~/x", 5, ("",), ("a\x00b",), (1,), None])
def test_a_malformed_extra_path_list_is_refused(bad):
    with pytest.raises(ValueError):
        ShellConfig(sandbox_extra_ro=bad)
    with pytest.raises(ValueError):
        ShellConfig(sandbox_toolchain_dirs=bad)


def test_lists_are_stored_as_tuples():
    cfg = ShellConfig(sandbox_extra_ro=["a", "~/b"], sandbox_toolchain_dirs=[".nvm"])
    assert cfg.sandbox_extra_ro == ("a", "~/b") and cfg.sandbox_toolchain_dirs == (".nvm",)


# ==========================================================================
# auto
# ==========================================================================
def test_auto_with_a_working_sandbox_runs_the_command_inside_it_with_no_warning(proj, working):
    context, warnings = make(proj)
    r = run(context, "echo inside")
    assert r.ok and "inside" in r.output
    assert working.wrapped, "the command must have gone through the wrapper"
    assert warnings == [], "a sandboxed run has nothing to warn about"
    assert len(working.wrap_calls) == 1 and working.wrap_calls[0]["command"] == "echo inside"
    assert sandbox_active(context) is True


def test_auto_without_a_working_sandbox_runs_unsandboxed_and_says_so_once(proj, broken):
    context, warnings = make(proj)
    for _ in range(3):
        assert run(context, "echo plain").ok
    assert not broken.wrapped and broken.wrap_calls == []
    assert warnings == [warning_text(broken.result.reason)], "exactly one warning per session"
    assert warnings[0].startswith(SANDBOX_WARNING) and "full privileges" in warnings[0]
    assert sandbox.probe.REASONS["userns_unavailable"] in warnings[0], "the reason is part of the warning"
    assert sandbox_active(context) is False


def test_the_wrapper_receives_the_project_the_sanitized_environment_and_the_settings(proj, working, monkeypatch):
    monkeypatch.setenv("FAKE_API_" + "TOK" + "EN", "x" * 12)
    monkeypatch.setenv("HARMLESS_SETTING", "kept")
    context, _ = make(proj, sandbox_extra_ro=("~/tools",), sandbox_toolchain_dirs=(".nvm",))
    run(context, "true")
    call = working.wrap_calls[0]
    assert call["project"] == os.fspath(proj)
    assert "FAKE_API_TOKEN" not in call["env"] and call["env"].get("HARMLESS_SETTING") == "kept"
    assert call["env"]["TERM"] == "dumb" and call["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert call["extra_ro"] == ("~/tools",) and call["toolchain_dirs"] == (".nvm",)
    assert str(Path(os.environ["M2C_HOME"])) in call["denied"], "m2c's own directory is never exposed"
    assert working.status_calls[0] == {"extra_ro": ("~/tools",), "toolchain_dirs": (".nvm",)}


def test_the_sandboxed_process_is_started_like_the_unsandboxed_one(proj, working, monkeypatch):
    seen = {}
    real = shell._spawn

    def spy(argv, cwd, env):
        seen.update(argv=argv, cwd=cwd, env=env)
        return real(argv, cwd, env)

    monkeypatch.setattr(shell, "_spawn", spy)
    context, _ = make(proj)
    run(context, "true")
    assert seen["argv"][0] == str(working.fake_bwrap) and seen["cwd"] == os.fspath(proj)
    assert set(seen["env"]) == {"PATH"}, "bubblewrap itself gets only a PATH: the command's environment is the profile's"


# ==========================================================================
# required
# ==========================================================================
def test_required_without_a_working_sandbox_runs_nothing(proj, broken):
    context, warnings = make(proj, sandbox="required")
    marker = proj / "ran.txt"
    with pytest.raises(ToolError) as info:
        run(context, f"echo ran > {marker}")
    assert info.value.code == "refused"
    assert str(info.value) == SANDBOX_REQUIRED_MESSAGE
    assert not marker.exists() and not broken.wrapped
    assert warnings == [], "a refusal is not a run: nothing to warn about"


def test_the_required_error_is_fixed_text_that_never_repeats_the_command(proj, broken):
    context, _ = make(proj, sandbox="required")
    secret_command = "echo " + "s3cr3t-" + "q" * 10
    with pytest.raises(ToolError) as info:
        run(context, secret_command)
    assert "s3cr3t" not in str(info.value) and str(proj) not in str(info.value)
    assert "sandbox" in str(info.value) and "required" in str(info.value)


def test_required_with_a_working_sandbox_runs(proj, working):
    context, warnings = make(proj, sandbox="required")
    assert run(context, "echo yes").ok and working.wrapped and warnings == []


def test_required_treats_a_probe_that_cannot_be_asked_as_not_working(proj, monkeypatch):
    monkeypatch.setattr(sandbox, "get_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    context, _ = make(proj, sandbox="required")
    with pytest.raises(ToolError) as info:
        run(context)
    assert info.value.code == "refused"


def test_required_is_refused_before_any_process_exists(proj, broken, monkeypatch):
    monkeypatch.setattr(shell, "_spawn", lambda *a, **k: pytest.fail("nothing may be started"))
    context, _ = make(proj, sandbox="required")
    with pytest.raises(ToolError):
        run(context)


# ==========================================================================
# off
# ==========================================================================
def test_off_never_asks_the_probe_and_keeps_the_warning(proj, working):
    context, warnings = make(proj, sandbox="off")
    assert run(context, "echo plain").ok
    assert working.status_calls == [] and working.wrap_calls == [] and not working.wrapped
    assert len(warnings) == 1 and warnings[0].startswith(SANDBOX_WARNING) and "turned off" in warnings[0]
    assert sandbox_active(context) is False and working.status_calls == []


# ==========================================================================
# never pretend
# ==========================================================================
def test_a_probe_that_cannot_be_asked_is_an_unsandboxed_run_with_the_warning_in_auto(proj, monkeypatch):
    monkeypatch.setattr(sandbox, "get_status", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    context, warnings = make(proj)
    assert run(context, "echo plain").ok
    assert len(warnings) == 1 and "full privileges" in warnings[0]
    assert sandbox_active(context) is False


@pytest.mark.parametrize("mode", SANDBOX_MODES)
@pytest.mark.parametrize("available", [True, False])
def test_the_sandbox_is_reported_active_only_when_it_is_available_and_not_off(proj, monkeypatch, tmp_path, mode, available):
    Rig(monkeypatch, tmp_path, available)
    context, _ = make(proj, sandbox=mode)
    assert sandbox_active(context) is (available and mode != "off")


@pytest.mark.parametrize("mode", SANDBOX_MODES)
@pytest.mark.parametrize("available", [True, False])
def test_the_warning_says_full_privileges_exactly_when_the_command_is_not_sandboxed(proj, monkeypatch, tmp_path, mode, available):
    rig = Rig(monkeypatch, tmp_path, available)
    context, warnings = make(proj, sandbox=mode)
    try:
        run(context, "true")
    except ToolError:
        pass
    ran_sandboxed = rig.wrapped
    assert ran_sandboxed is (available and mode != "off")
    assert bool(warnings) is (not ran_sandboxed and mode != "required")
    for text in warnings:
        assert "full privileges" in text and "no sandbox is active" in text


def test_the_warning_text_never_claims_a_sandbox():
    for reason in (None, "", "bubblewrap (bwrap) is not installed"):
        text = warning_text(reason)
        assert "full privileges" in text and "no sandbox is active" in text
        assert "sandboxed" not in text.lower().replace("no sandbox is active", "")


# ==========================================================================
# once the probe passed, the sandbox is a promise
# ==========================================================================
@pytest.mark.parametrize("mode", ["auto", "required"])
def test_a_profile_that_cannot_be_built_now_is_a_refusal_not_a_downgrade(proj, working, mode):
    working.wrap_error = ProfileError("protected_symlink")
    context, warnings = make(proj, sandbox=mode)
    marker = proj / "ran.txt"
    with pytest.raises(ToolError) as info:
        run(context, f"echo ran > {marker}")
    assert info.value.code == "refused"
    assert "could not be built" in str(info.value) and ProfileError("protected_symlink").message in str(info.value)
    assert not marker.exists() and warnings == []


def test_the_refusal_text_is_fixed_and_has_no_path(proj, working):
    working.wrap_error = ProfileError("project_on_windows_drive")
    context, _ = make(proj)
    with pytest.raises(ToolError) as info:
        run(context, "echo hi")
    assert str(proj) not in str(info.value) and "echo hi" not in str(info.value)


def test_the_model_cannot_switch_the_sandbox_off_with_a_symlink(proj, monkeypatch, tmp_path):
    """The attack: ``ln -s elsewhere .git`` from inside the sandbox (no ``.git`` existed, so none was protected), then the
    next command.  The real profile builder refuses a protected name that is a symlink, and the shell tool must turn that
    refusal into a refusal, not into an unsandboxed run."""
    rig = Rig(monkeypatch, tmp_path, True)
    monkeypatch.undo()  # drop the fakes, keep the temporary files
    monkeypatch.setattr(sandbox, "get_status", lambda **kw: status(True))
    monkeypatch.setattr(sandbox.bwrap, "find_bwrap", lambda *a, **k: str(rig.fake_bwrap))
    (proj / ".git").symlink_to(tmp_path)
    context, warnings = make(proj)
    marker = proj / "ran.txt"
    with pytest.raises(ToolError) as info:
        run(context, f"echo ran > {marker}")
    assert info.value.code == "refused" and "symlink" in str(info.value)
    assert not marker.exists() and warnings == []


# ==========================================================================
# status lines
# ==========================================================================
@pytest.mark.parametrize("code,line", [
    (0, "[exit status: 0]"), (1, "[exit status: 1]"), (127, "[exit status: 127]"), (128, "[exit status: 128]"),
    (160, "[exit status: 160]"), (255, "[exit status: 255]"),
])
def test_ordinary_exit_statuses_read_the_same_with_and_without_the_sandbox(code, line):
    assert shell._exit_status(code, True) == (code, line) == shell._exit_status(code, False)


@pytest.mark.parametrize("code", [129, 130, 137, 143, 159])
def test_a_status_that_stands_for_a_signal_is_annotated_under_the_sandbox_only(code):
    assert shell._exit_status(code, True) == (code, f"[exit status: {code} (terminated by signal {code - 128})]")
    assert shell._exit_status(code, False) == (code, f"[exit status: {code}]")


def test_the_unsandboxed_signal_text_is_unchanged():
    assert shell._exit_status(-9) == (137, "[exit status: 137 (terminated by signal 9)]")
    assert shell._exit_status(-9, False) == shell._exit_status(-9, True) == (137, "[exit status: 137 (terminated by signal 9)]")
    assert shell._exit_status(None, True) == (None, "[exit status: unknown]")


def test_a_failing_sandboxed_command_is_a_failed_outcome_like_any_other(proj, working):
    context, _ = make(proj)
    r = run(context, "echo oops >&2; exit 3")
    assert (r.ok, r.exit_code, r.code) == (False, 3, "exit_nonzero") and "[exit status: 3]" in r.output


def test_a_command_ended_by_a_signal_inside_the_sandbox_keeps_its_annotation(proj, working):
    context, _ = make(proj)
    r = run(context, "exit 137")
    assert r.exit_code == 137 and "(terminated by signal 9)" in r.output


# ==========================================================================
# supervision is unchanged around the wrapper (timeout, cancel, output), with the fake wrapper
# ==========================================================================
def test_timeout_still_kills_the_group_through_the_wrapper(proj, working):
    import time

    context, _ = make(proj)
    started = time.monotonic()
    r = run(context, "sleep 60", 1)
    assert r.code == "timeout" and r.ok is False and r.exit_code is None
    assert time.monotonic() - started < 8


def test_output_cap_still_applies_through_the_wrapper(proj, working):
    context, _ = make(proj, max_output_bytes=2000)
    r = run(context, "head -c 50000 /dev/zero | tr '\\0' x")
    assert r.truncated and "bytes omitted" in r.output and len(r.output.encode()) < 3000


# ==========================================================================
# logging
# ==========================================================================
def test_the_log_says_whether_the_run_was_sandboxed_and_never_the_command(proj, working, caplog):
    caplog.set_level(logging.DEBUG)
    context, _ = make(proj)
    command = "echo " + "needle-" + "z" * 12
    run(context, command)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "check=bash_sandbox" in text and "needle" not in text and str(proj) not in text


def test_a_refused_profile_is_logged_as_a_code_only(proj, working, caplog):
    caplog.set_level(logging.DEBUG)
    working.wrap_error = ProfileError("protected_symlink")
    context, _ = make(proj)
    with pytest.raises(ToolError):
        run(context, "echo needle")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "code=protected_symlink" in text and "needle" not in text
