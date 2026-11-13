"""
m2c/tests/sandbox/test_sandbox_probe.py -- Patch C11a: the sandbox self-test (``m2c.sandbox.probe``).

Two halves.

1. Logic, with a FAKE runner (no bubblewrap needed, runs everywhere).  ``FakeSandbox`` behaves like a
   correct sandbox: it answers each check's script the way a sandbox that held would (and makes the one
   host-side change the "basic" check expects).  Every failure mode is then one deliberate deviation:
   a leak on the real disk, a leak in the output, a sandbox that does not start (each classified error),
   a hang, a crash, a missing binary, a refused profile.  What is pinned: the probe says "does not work"
   in every one of them and says WHICH check, the result never contains a path or output, the temporary
   world is always removed, and nothing is logged but words and codes.

2. The probe against the REAL bubblewrap, weakened on purpose (marked ``real_sandbox``; skipped loudly
   when bubblewrap or user namespaces are missing).  A probe that passes proves little unless it also
   fails when the sandbox is weak, so each mutation removes one protection from the real command line
   and the matching check, and only that one, must fail.

Run: cd m2c && pytest tests/sandbox/test_sandbox_probe.py -v -rs
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading

import pytest

from m2c.sandbox import probe
from m2c.sandbox.probe import CHECK_NAMES, RunResult, classify_start_failure, run_probe


# ==========================================================================
# a fake bubblewrap
# ==========================================================================
def _check_name(argv) -> str:
    return re.search(r"probe_check=(\w+)", argv[-1]).group(1)


def _unique(argv) -> str:
    return re.search(r"escape_home_([0-9a-f]+)", " ".join(argv)).group(1)


class FakeSandbox:
    """Answers like a sandbox that held.  ``deviations`` maps a check name to a function(argv, ctx) -> RunResult|None
    that may touch the real disk (the world is real, only the process is fake) and replace the answer."""

    def __init__(self, deviations=None):
        self.deviations = deviations or {}
        self.calls: list[str] = []
        self.argvs: list[list[str]] = []
        self.envs: list[dict] = []
        self.project = self.home = self.root = ""

    def __call__(self, argv, *, env, timeout_s):
        name = _check_name(argv)
        self.calls.append(name)
        self.argvs.append(list(argv))
        self.envs.append(dict(env))
        self.project = argv[argv.index("--chdir") + 1]
        self.home = os.path.dirname(self.project)
        self.root = os.path.dirname(self.home)
        if name in self.deviations:
            answer = self.deviations[name](argv, self)
            if answer is not None:
                return answer
        return self.good(name, argv)

    def good(self, name, argv) -> RunResult:
        script = argv[-1]
        out = ""
        if name == "basic":
            token = re.search(r"m2c-probe-[0-9a-f]+", script).group(0)
            with open(os.path.join(self.project, "probe_out.txt"), "w") as handle:
                handle.write(token + "\n")
            out = token + "\n"
        elif name == "home_secrets_hidden":
            out = "LISTING_BEGIN\nproj\nLISTING_END\n"
        elif name == "network_unreachable":
            out = "IFACES_BEGIN\nlo\nIFACES_END\n"
        elif name == "env_cleared":
            out = f"PATH=/usr/bin\nHOME={self.home}\nPWD={self.project}\n"
        return RunResult(0, out + "PROBE_RAN\n", "")


def no_marker(_token):
    return lambda: None


def go(fake, **kw):
    kw.setdefault("bwrap_path", "/usr/bin/bwrap")
    kw.setdefault("marker_factory", no_marker)
    return run_probe(runner=fake, **kw)


def scribble(path, text="x"):
    with open(path, "a") as handle:
        handle.write(text)


# ==========================================================================
# a sandbox that holds
# ==========================================================================
def test_a_sandbox_that_holds_passes_every_check_in_order():
    fake = FakeSandbox()
    result = go(fake)
    assert result.works is True and result.reason_code == "ok"
    assert [c.name for c in result.checks] == list(CHECK_NAMES) == fake.calls
    assert all(c.passed and c.code == "ok" for c in result.checks)
    assert result.failed == () and result.passed_count == len(CHECK_NAMES)
    assert result.describe() == f"all {len(CHECK_NAMES)} sandbox checks passed"


def test_the_check_list_is_the_one_in_the_work_package():
    assert set(CHECK_NAMES) >= {"basic", "write_outside_project", "write_git", "write_m2c", "home_secrets_hidden",
                                "write_rc_file", "network_unreachable", "cd_up_write", "pid_isolation"}
    assert len(CHECK_NAMES) == len(set(CHECK_NAMES))


def test_every_check_runs_the_real_profile_with_the_script_as_one_argument():
    fake = FakeSandbox()
    go(fake)
    for argv in fake.argvs:
        assert argv[0] == "/usr/bin/bwrap"
        i = argv.index("--")
        assert argv[i + 2] == "-c" and i + 4 == len(argv), "[.., '--', shell, '-c', script] and nothing after"
        for flag in ("--unshare-net", "--unshare-pid", "--die-with-parent", "--new-session", "--clearenv"):
            assert flag in argv, flag


def test_the_world_is_removed_afterwards_and_never_leaks_into_the_arguments():
    fake = FakeSandbox()
    go(fake)
    assert fake.root and not os.path.exists(fake.root), "the temporary world must be removed"
    joined = "\n".join("\n".join(a) for a in fake.argvs)
    # canaries are file CONTENTS: they must never appear in a process command line (ps would show them)
    assert "ssh_canary" not in joined
    ssh_like = re.findall(r"m2c-probe-[0-9a-f]{16}", joined)
    assert len(set(ssh_like)) <= 3, "only the 'basic' token and the host markers may be in scripts"


def test_the_host_marker_is_started_with_its_token_and_stopped_afterwards():
    seen = {}

    def factory(token):
        seen["token"] = token
        return lambda: seen.setdefault("stopped", True)

    fake = FakeSandbox()
    result = go(fake, marker_factory=factory)
    assert result.works
    assert seen["token"].startswith("m2c-probe-marker-") and seen["stopped"] is True


def test_the_pid_script_does_not_contain_the_marker_it_looks_for():
    """The probing shell's own command line must not match itself, or "host process visible" is always true."""
    seen = {}
    go(FakeSandbox(), marker_factory=lambda token: seen.setdefault("t", token) and (lambda: None))
    fake = FakeSandbox()
    go(fake, marker_factory=lambda token: seen.update(t=token) or (lambda: None))
    script = next(a[-1] for a in fake.argvs if "probe_check=pid_isolation" in a[-1])
    assert seen["t"] not in script


def test_the_outer_environment_has_a_canary_and_nothing_secret():
    fake = FakeSandbox()
    go(fake)
    for env in fake.envs:
        assert set(env) == {"PATH", "PROBE_OUTER_VALUE"}


# ==========================================================================
# a sandbox that leaks: each deviation is caught by its own check, and only by it
# ==========================================================================
def _write_outside_disk(argv, fake):
    scribble(os.path.join(fake.home, f"escape_home_{_unique(argv)}"))


def _write_outside_output(argv, fake):
    return RunResult(0, "WROTE_HOME\nPROBE_RAN\n")


def _git_append(argv, fake):
    scribble(os.path.join(fake.project, ".git", "config"))


def _git_new_file(argv, fake):
    scribble(os.path.join(fake.project, ".git", "probe_new_" + re.search(r"probe_new_([0-9a-f]+)", argv[-1]).group(1)))


def _git_moved(argv, fake):
    os.rename(os.path.join(fake.project, ".git"), os.path.join(fake.project, ".git_moved"))


def _git_output(argv, fake):
    return RunResult(0, "MOVED\nPROBE_RAN\n")


def _m2c_append(argv, fake):
    scribble(os.path.join(fake.project, ".m2c", "state"))


def _m2c_removed(argv, fake):
    os.remove(os.path.join(fake.project, ".m2c", "state"))


def _secret_in_output(argv, fake):
    with open(os.path.join(fake.home, ".ssh", "id_probe")) as handle:
        return RunResult(0, handle.read() + "\nLISTING_BEGIN\nproj\nLISTING_END\nPROBE_RAN\n")


def _secret_in_stderr(argv, fake):
    with open(os.path.join(fake.home, ".aws", "credentials")) as handle:
        return RunResult(0, "LISTING_BEGIN\nproj\nLISTING_END\nPROBE_RAN\n", handle.read())


def _home_listing_shows_secrets(argv, fake):
    return RunResult(0, "LISTING_BEGIN\n.ssh\n.aws\nproj\nLISTING_END\nPROBE_RAN\n")


def _home_rc_visible(argv, fake):
    with open(os.path.join(fake.home, ".bashrc")) as handle:
        return RunResult(0, handle.read() + "\nLISTING_BEGIN\nproj\nLISTING_END\nPROBE_RAN\n")


def _rc_append(argv, fake):
    scribble(os.path.join(fake.project, ".bashrc"))


def _profile_removed(argv, fake):
    os.remove(os.path.join(fake.project, ".profile"))


def _rc_output(argv, fake):
    return RunResult(0, "APPENDED\nPROBE_RAN\n")


def _net_has_eth0(argv, fake):
    return RunResult(0, "IFACES_BEGIN\nlo\neth0\nIFACES_END\nPROBE_RAN\n")


def _net_connected(argv, fake):
    return RunResult(0, "IFACES_BEGIN\nlo\nIFACES_END\nCONNECTED\nPROBE_RAN\n")


def _net_no_interface_list(argv, fake):
    return RunResult(0, "PROBE_RAN\n")  # cannot show that only loopback exists: not enough evidence


def _cd_up_disk(argv, fake):
    scribble(os.path.join(fake.home, f"up_{_unique_up(argv)}"))


def _unique_up(argv):
    return re.search(r"up_([0-9a-f]+)", argv[-1]).group(1)


def _cd_up_output(argv, fake):
    return RunResult(0, "WROTE_UP\nPROBE_RAN\n")


def _pid_visible(argv, fake):
    return RunResult(0, "VISIBLE\nPROBE_RAN\n")


def _tmp_visible(argv, fake):
    return RunResult(0, "HOST_TMP_VISIBLE\nPROBE_RAN\n")


def _tmp_not_writable(argv, fake):
    return RunResult(0, "TMP_NOT_WRITABLE\nPROBE_RAN\n")


def _env_leaks(argv, fake):
    value = fake.envs[-1]["PROBE_OUTER_VALUE"]
    return RunResult(0, f"PROBE_OUTER_VALUE={value}\nHOME={fake.home}\nPWD={fake.project}\nPROBE_RAN\n")


def _env_wrong_home(argv, fake):
    return RunResult(0, f"HOME=/root\nPWD={fake.project}\nPROBE_RAN\n")


def _basic_cannot_write(argv, fake):
    return RunResult(0, "m2c-probe-0000000000000000\nPROBE_RAN\n")  # printed something, wrote nothing


LEAKS = [
    ("write_outside_project", "disk", _write_outside_disk),
    ("write_outside_project", "output", _write_outside_output),
    ("write_git", "append", _git_append),
    ("write_git", "new_file", _git_new_file),
    ("write_git", "moved_away", _git_moved),
    ("write_git", "output", _git_output),
    ("write_m2c", "append", _m2c_append),
    ("write_m2c", "removed", _m2c_removed),
    ("home_secrets_hidden", "ssh_in_stdout", _secret_in_output),
    ("home_secrets_hidden", "aws_in_stderr", _secret_in_stderr),
    ("home_secrets_hidden", "listing", _home_listing_shows_secrets),
    ("home_secrets_hidden", "rc_in_home", _home_rc_visible),
    ("write_rc_file", "append", _rc_append),
    ("write_rc_file", "removed", _profile_removed),
    ("write_rc_file", "output", _rc_output),
    ("network_unreachable", "interface", _net_has_eth0),
    ("network_unreachable", "connected", _net_connected),
    ("network_unreachable", "no_evidence", _net_no_interface_list),
    ("cd_up_write", "disk", _cd_up_disk),
    ("cd_up_write", "output", _cd_up_output),
    ("pid_isolation", "visible", _pid_visible),
    ("private_tmp", "host_visible", _tmp_visible),
    ("private_tmp", "not_writable", _tmp_not_writable),
    ("env_cleared", "inherited", _env_leaks),
    ("env_cleared", "wrong_home", _env_wrong_home),
    ("basic", "cannot_write", _basic_cannot_write),
]


@pytest.mark.parametrize("check,kind,deviation", LEAKS, ids=[f"{c}-{k}" for c, k, _ in LEAKS])
def test_a_leak_fails_its_own_check_and_only_that_one(check, kind, deviation):
    fake = FakeSandbox({check: deviation})
    result = go(fake)
    assert result.works is False
    assert result.reason_code == "check_failed"
    assert result.failed == (check,), f"{check}/{kind} must fail exactly that check"
    assert check in result.reason and check in result.describe()
    assert next(c for c in result.checks if c.name == check).code == "violated"
    assert len(result.checks) == len(CHECK_NAMES), "the other checks still run: the report is complete"


def test_several_leaks_are_all_reported():
    result = go(FakeSandbox({"write_git": _git_output, "write_outside_project": _write_outside_output}))
    assert result.failed == ("write_outside_project", "write_git")
    assert "write_outside_project, write_git" in result.reason


def test_files_a_leaking_sandbox_created_outside_the_world_are_removed(tmp_path):
    """A broken sandbox that really writes to the host must not leave debris behind."""
    made = []

    def leak(argv, fake):
        path = os.path.join(fake.home, f"escape_home_{_unique(argv)}")
        scribble(path)
        made.append(path)

    go(FakeSandbox({"write_outside_project": leak}))
    assert made and not any(os.path.exists(p) for p in made)


# ==========================================================================
# a sandbox that does not start
# ==========================================================================
START_FAILURES = [
    ("bwrap: No permissions to create new namespace, likely because the kernel does not allow non-privileged user namespaces.", "userns_unavailable"),
    ("bwrap: Creating new namespace failed: Operation not permitted", "userns_unavailable"),
    ("bwrap: setting up uid map: Permission denied", "userns_unavailable"),
    ("bwrap: Can't mount proc on /newroot/proc: Operation not permitted", "proc_unavailable"),
    ("bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted", "netns_unavailable"),
    ("bwrap: execvp /usr/bin/bash: No such file or directory", "bwrap_failed"),
    ("something nobody has seen before", "bwrap_failed"),
    ("", "bwrap_failed"),
]


@pytest.mark.parametrize("stderr,code", START_FAILURES)
def test_a_sandbox_that_does_not_start_is_classified_by_its_error_text(stderr, code):
    fake = FakeSandbox({"basic": lambda argv, f: RunResult(1, "", stderr)})
    result = go(fake)
    assert result.works is False and result.reason_code == code
    assert result.reason == probe.REASONS[code]
    assert fake.calls == ["basic"], "if the first run cannot start, the others are not tried"
    assert [(c.name, c.passed, c.code) for c in result.checks] == [("basic", False, "setup_failed")]
    assert stderr not in result.reason or stderr == ""


@pytest.mark.parametrize("stderr,code", START_FAILURES)
def test_classification_alone(stderr, code):
    assert classify_start_failure(RunResult(1, "", stderr)) == code


def test_a_run_that_hangs_is_a_timeout_not_a_pass():
    fake = FakeSandbox({"basic": lambda argv, f: RunResult(None, "", "", True)})
    result = go(fake)
    assert (result.works, result.reason_code) == (False, "timeout")
    assert classify_start_failure(RunResult(None, "", "", True)) == "timeout"


def test_a_later_check_that_hangs_fails_that_check_with_code_timeout():
    result = go(FakeSandbox({"network_unreachable": lambda argv, f: RunResult(None, "", "", True)}))
    assert result.failed == ("network_unreachable",)
    assert next(c for c in result.checks if c.name == "network_unreachable").code == "timeout"


def test_a_check_whose_script_did_not_finish_is_not_a_pass():
    """No sentinel means "we did not see it hold": an empty answer must never count as "it was prevented"."""
    for answer in (RunResult(1, "PROBE_RAN\n"), RunResult(0, ""), RunResult(0, "", "PROBE_RAN"), RunResult(None, "")):
        result = go(FakeSandbox({"write_git": lambda argv, f, a=answer: a}))
        assert result.failed == ("write_git",)
        assert next(c for c in result.checks if c.name == "write_git").code == "setup_failed"


def test_without_a_host_process_to_hide_the_pid_check_cannot_pass():
    result = go(FakeSandbox(), marker_factory=lambda token: None)
    assert result.failed == ("pid_isolation",)
    assert next(c for c in result.checks if c.name == "pid_isolation").code == "setup_failed"


# ==========================================================================
# no bubblewrap, no profile, a crash
# ==========================================================================
def test_bubblewrap_missing_is_reported_and_nothing_is_started(monkeypatch):
    monkeypatch.setattr(probe, "find_bwrap", lambda *a, **k: None)
    called = []
    result = run_probe(runner=lambda *a, **k: called.append(1), marker_factory=no_marker)
    assert (result.works, result.reason_code) == (False, "bwrap_missing")
    assert result.reason == "bubblewrap (bwrap) is not installed"
    assert result.bwrap_path is None and result.checks == () and called == []


def test_an_extra_read_only_path_that_the_profile_refuses_fails_the_probe_without_starting_anything():
    fake = FakeSandbox()
    result = go(fake, extra_ro=("/etc",))
    assert (result.works, result.reason_code) == (False, "extra_ro_denied")
    assert result.reason == probe.REASONS["extra_ro_denied"] and fake.calls == []


def test_a_machine_without_usr_cannot_be_probed(tmp_path):
    """A layout with no /usr is a profile error; the probe reports its code and starts nothing."""
    from m2c.sandbox.profile import ProfileError, SystemLayout

    def no_usr(root="/"):
        raise ProfileError("no_usr")

    fake = FakeSandbox()
    original = probe.detect_system_layout
    probe.detect_system_layout = no_usr
    try:
        result = go(fake)
    finally:
        probe.detect_system_layout = original
    assert (result.works, result.reason_code) == (False, "no_usr") and fake.calls == []


def test_an_exception_in_the_runner_is_a_failed_probe_not_a_crash():
    def boom(argv, *, env, timeout_s):
        raise RuntimeError("secret internal detail " + "x" * 5)

    result = go(boom)
    assert (result.works, result.reason_code) == (False, "probe_error")
    assert "secret internal detail" not in result.reason
    assert result.reason == probe.REASONS["probe_error"]


def test_the_world_is_removed_even_when_the_runner_crashes():
    roots = []

    def boom(argv, *, env, timeout_s):
        roots.append(os.path.dirname(os.path.dirname(argv[argv.index("--chdir") + 1])))
        raise RuntimeError("x")

    go(boom)
    assert roots and not os.path.exists(roots[0])


def test_a_failing_verifier_never_raises(monkeypatch):
    monkeypatch.setattr(probe.os.path, "lexists", lambda p: (_ for _ in ()).throw(OSError("disk gone")))
    result = go(FakeSandbox())
    assert result.works is False


# ==========================================================================
# the words
# ==========================================================================
@pytest.mark.parametrize("check,kind,deviation", [LEAKS[0], LEAKS[3], LEAKS[17]])
def test_reasons_never_contain_a_path_or_output(check, kind, deviation):
    fake = FakeSandbox({check: deviation})
    result = go(fake)
    for text in (result.reason, result.describe()):
        assert fake.root not in text and "/tmp" not in text and "/var" not in text and "m2c-probe-" not in text


def test_every_reason_code_has_fixed_text_without_a_path():
    for code, text in probe.REASONS.items():
        assert text and "/home" not in text and "\n" not in text, code
    for code in ("bwrap_missing", "userns_unavailable", "proc_unavailable", "netns_unavailable", "bwrap_failed", "timeout",
                 "check_failed", "probe_error"):
        assert code in probe.REASONS


@pytest.fixture
def caplog(caplog):
    """As in tests/tools/conftest.py: another module may have called ``configure_logging()``, which turns propagation off,
    so the handler is attached to the ``m2c`` logger itself for the duration of the test."""
    logger = logging.getLogger("m2c")
    saved = logger.propagate, logger.level
    logger.addHandler(caplog.handler)
    logger.propagate = False
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.propagate, level = saved
        logger.setLevel(level)


def test_the_probe_logs_words_and_codes_only(caplog):
    caplog.set_level(logging.DEBUG, logger="m2c")
    fake = FakeSandbox({"write_git": _git_output})
    go(fake)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "sandbox.probe" in text and "status=failed" in text and "code=check_failed" in text
    for forbidden in (fake.root, fake.project, "PROBE_RAN", "m2c-probe-", "write_git", "/usr/bin/bwrap", "echo", "touch"):
        assert forbidden not in text, forbidden


# ==========================================================================
# the session cache
# ==========================================================================
def test_the_session_probe_runs_once_per_settings_and_is_remembered():
    probe.reset_session_cache()
    fake = FakeSandbox()
    kw = dict(runner=fake, bwrap_path="/usr/bin/bwrap", marker_factory=no_marker)
    first = probe.session_probe(**kw)
    count = len(fake.calls)
    assert count == len(CHECK_NAMES)
    assert probe.session_probe(**kw) is first and len(fake.calls) == count
    assert probe.session_probe(extra_ro=("/usr/share",), **kw) is not first, "different settings, different probe"
    assert len(fake.calls) == 2 * count
    probe.reset_session_cache()
    probe.session_probe(**kw)
    assert len(fake.calls) == 3 * count


def test_two_threads_asking_at_once_run_the_probe_once():
    probe.reset_session_cache()
    fake = FakeSandbox()
    kw = dict(runner=fake, bwrap_path="/usr/bin/bwrap", marker_factory=no_marker)
    results = []
    threads = [threading.Thread(target=lambda: results.append(probe.session_probe(**kw))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 4 and all(r is results[0] for r in results)
    assert len(fake.calls) == len(CHECK_NAMES)


def test_a_failed_probe_is_remembered_too():
    probe.reset_session_cache()
    fake = FakeSandbox({"basic": lambda argv, f: RunResult(1, "", "no permissions to create new namespace")})
    kw = dict(runner=fake, bwrap_path="/usr/bin/bwrap", marker_factory=no_marker)
    assert probe.session_probe(**kw).works is False
    assert probe.session_probe(**kw).works is False
    assert fake.calls == ["basic"]


# ==========================================================================
# the real runner
# ==========================================================================
@pytest.mark.skipif(sys.platform != "linux", reason="uses /bin/sh")
def test_the_default_runner_captures_output_and_exit_status():
    r = probe.default_runner(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], env={"PATH": "/usr/bin:/bin"}, timeout_s=10)
    assert (r.returncode, r.stdout, r.stderr, r.timed_out) == (3, "out\n", "err\n", False)


@pytest.mark.skipif(sys.platform != "linux", reason="uses /bin/sh")
def test_the_default_runner_kills_the_whole_group_on_timeout(tmp_path):
    marker = tmp_path / "alive"
    script = f"(sleep 1; echo late > {marker}) & sleep 30"
    r = probe.default_runner(["/bin/sh", "-c", script], env={"PATH": "/usr/bin:/bin"}, timeout_s=0.3)
    assert r.timed_out is True and r.returncode != 0
    import time

    time.sleep(1.5)
    assert not marker.exists(), "the background child must have been killed with the group"


def test_the_default_runner_survives_a_program_that_cannot_start():
    r = probe.default_runner(["/nonexistent/program"], env={}, timeout_s=1)
    assert r.returncode is None and r.timed_out is False


# ==========================================================================
# the real thing, weakened on purpose
# ==========================================================================
def _need_bwrap():
    from m2c.sandbox import bwrap

    if bwrap.find_bwrap() is None:
        _skip("bubblewrap (bwrap) is not installed")
    result = run_probe()
    if not result.works:
        _skip(result.describe())


def _skip(reason):
    sys.__stderr__.write(f"\nSKIPPED: real-sandbox probe mutation tests: {reason}\n")
    sys.__stderr__.flush()
    pytest.skip(reason)


def _drop_flags(*flags):
    def mutate(argv):
        out, i = [], 0
        while i < len(argv):
            if argv[i] in flags:
                i += 1
                continue
            out.append(argv[i])
            i += 1
        return out
    return mutate


def _drop_option_with_value(option, matches):
    def mutate(argv):
        out, i = [], 0
        while i < len(argv):
            if argv[i] == option and matches(argv, i):
                i += {"--ro-bind": 3, "--tmpfs": 2, "--remount-ro": 2}[option]
                continue
            out.append(argv[i])
            i += 1
        return out
    return mutate


def _bind_home_instead_of_tmpfs(argv):
    home = os.path.dirname(argv[argv.index("--chdir") + 1])
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--tmpfs" and argv[i + 1] == home:
            out += ["--bind", home, home]
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


MUTATIONS = {
    "network namespace shared": (_drop_flags("--unshare-net"), {"network_unreachable"}),
    "pid namespace shared": (_drop_flags("--unshare-pid"), {"pid_isolation"}),
    ".git not read-only": (_drop_option_with_value("--ro-bind", lambda a, i: a[i + 2].endswith("/.git")), {"write_git"}),
    ".m2c not read-only": (_drop_option_with_value("--ro-bind", lambda a, i: a[i + 2].endswith("/.m2c")), {"write_m2c"}),
    "rc file not read-only": (_drop_option_with_value("--ro-bind", lambda a, i: a[i + 2].endswith("/.bashrc")), {"write_rc_file"}),
    "home and root writable": (_drop_option_with_value("--remount-ro", lambda a, i: True), {"write_outside_project", "cd_up_write"}),
    "real home instead of an empty one": (_bind_home_instead_of_tmpfs, {"home_secrets_hidden"}),
    "shared /tmp": (_drop_option_with_value("--tmpfs", lambda a, i: a[i + 1] == "/tmp"), {"private_tmp"}),
    "environment inherited": (_drop_flags("--clearenv"), {"env_cleared"}),
}


@pytest.mark.real_sandbox
@pytest.mark.parametrize("label", list(MUTATIONS))
def test_weakening_the_real_sandbox_makes_the_matching_check_fail(label):
    _need_bwrap()
    mutate, expected = MUTATIONS[label]

    def weakened(argv, *, env, timeout_s):
        return probe.default_runner(mutate(argv), env=env, timeout_s=timeout_s)

    result = run_probe(runner=weakened)
    assert result.works is False, f"the probe passed a sandbox with this weakness: {label}"
    assert set(result.failed) == expected, f"{label}: {result.failed}"


@pytest.mark.real_sandbox
def test_a_command_run_without_any_sandbox_fails_the_probe():
    """The honest baseline: if the "sandbox" is just the shell, nearly every check must fail."""
    _need_bwrap()
    import shutil

    def unsandboxed(argv, *, env, timeout_s):
        script = argv[-1]
        # run the project-local scripts for real, in the throw-away project; refuse to execute the two that write to
        # /usr, /etc and / (a failure there is simulated: a broken sandbox must not make the test touch the machine)
        if "probe_check=write_outside_project" in script or "probe_check=cd_up_write" in script:
            return RunResult(0, "WROTE_HOME\nPROBE_RAN\n")
        project = argv[argv.index("--chdir") + 1]
        shell = shutil.which("sh") or "/bin/sh"
        return probe.default_runner(["/usr/bin/env", "-C", project, shell, "-c", script], env=env, timeout_s=timeout_s)

    result = run_probe(runner=unsandboxed)
    assert result.works is False
    assert {"write_git", "write_m2c", "write_rc_file"} <= set(result.failed)


@pytest.mark.real_sandbox
def test_the_real_sandbox_passes_the_probe():
    _need_bwrap()
    result = run_probe()
    assert result.works is True, result.describe()
    assert [c.name for c in result.checks] == list(CHECK_NAMES)
