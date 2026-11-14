"""
m2c/tests/sandbox/test_net_probe.py -- Patch C11b: the network self-test (``probe.run_network_probe``), without bubblewrap.

The probe starts a REAL host-side proxy with a fake resolver, a loopback fake upstream and the test-only loopback flag,
then runs one sandboxed command and judges it from what that command printed AND what the host saw.  Here the "sandbox"
is a fake runner that EMULATES the in-sandbox client (it talks to the proxy's unix socket from the host, with the real
client text), so the verdict logic runs for real: every check passes with the emulation, and each failure mode is
produced by a variant of it.  The real thing is in test_net_real_sandbox.py.

Also pinned: the main probe's ``CHECK_NAMES`` and ``works`` are untouched, results hold fixed text only, the cache, and
cleanup (proxy stopped, directory gone, fake upstream closed).

Place this file at: m2c/tests/sandbox/test_net_probe.py
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import socket
import sys

import pytest

from m2c.sandbox import bwrap, netproxy, probe
from m2c.sandbox.probe import (
    CHECK_NAMES, NETWORK_CHECK_NAMES, NETWORK_REASONS, RunResult, reset_session_cache, run_network_probe,
    session_network_probe,
)

REAL_RUN_NETWORK_PROBE = run_network_probe   # the suite-wide conftest replaces probe.run_network_probe for non-real tests

BWRAP = "/usr/bin/bwrap"        # never executed: the fake runner answers
PYTHON = "/usr/bin/python3"


class Emulator:
    """A fake ``runner`` that behaves like the sandbox from the point of view of the probe."""

    def __init__(self, *, direct="FAILED", writable=False, mutate=None, ran=True, timeout=False, boom=False):
        self.calls: list[list[str]] = []
        self.dirs: list[str] = []
        self.direct = direct
        self.writable = writable
        self.mutate = mutate
        self.ran = ran
        self.timeout = timeout
        self.boom = boom

    def __call__(self, argv, *, env, timeout_s):
        self.calls.append(list(argv))
        if self.boom:
            raise RuntimeError("runner exploded: /secret/path")
        if self.timeout:
            return RunResult(None, "", "", True)
        if not self.ran:
            return RunResult(1, "", "bwrap: No permissions to create new namespace", False)
        i = argv.index("--ro-bind")
        while argv[i + 2] != "/run/m2c-net":
            i = argv.index("--ro-bind", i + 1)
        directory = argv[i + 1]
        self.dirs.append(directory)
        script = argv[-1]
        port = re.search(r"' (\d+)\n", script).group(1)
        client = probe._NETWORK_CLIENT
        client = client.replace('s = socket.create_connection(("127.0.0.1", 3128), 5)',
                                's = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(%r)' % os.path.join(directory, "proxy.sock"))
        client = client.replace("socket.create_connection(addr, 3).close()\n        return \"CONNECTED\"",
                                "raise OSError()" if self.direct == "FAILED" else "return \"CONNECTED\"")
        out = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["client", port]
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(client, "<client>", "exec"), {"__name__": "client"})
        finally:
            sys.argv = old_argv
        text = out.getvalue()
        if self.direct != "FAILED":
            text = text.replace("DIRECT_DOC FAILED", "DIRECT_DOC CONNECTED")
        if self.writable:
            text += "NETDIR_WRITABLE\n"
        if self.mutate:
            text = self.mutate(text)
        return RunResult(0, text + "PROBE_RAN\n", "", False)


def run(emulator=None, **kw):
    emulator = emulator or Emulator()
    kw.setdefault("bwrap_path", BWRAP)
    kw.setdefault("python", PYTHON)
    return emulator, run_network_probe(runner=emulator, **kw)


# --------------------------------------------------------------------------
# the main probe is untouched
# --------------------------------------------------------------------------
def test_the_main_probes_check_names_are_exactly_what_c11a_pinned():
    assert CHECK_NAMES == (
        "basic", "write_outside_project", "write_git", "write_m2c", "home_secrets_hidden", "write_rc_file",
        "network_unreachable", "cd_up_write", "pid_isolation", "private_tmp", "env_cleared",
    )
    assert not set(NETWORK_CHECK_NAMES) & set(CHECK_NAMES)


def test_the_network_check_names_are_the_six_in_the_work_package():
    assert NETWORK_CHECK_NAMES == (
        "direct_connect_fails", "not_allowed_refused", "ip_literal_refused", "private_resolution_refused",
        "allowed_host_works", "socket_dir_private",
    )


# --------------------------------------------------------------------------
# the verdicts
# --------------------------------------------------------------------------
def test_with_an_honest_sandbox_every_check_passes():
    emulator, result = run()
    assert result.works and result.reason_code == "ok" and result.python == PYTHON
    assert [c.name for c in result.checks] == list(NETWORK_CHECK_NAMES)
    assert all(c.passed and c.code == "ok" for c in result.checks)
    assert result.describe() == "all 6 network checks passed"
    assert len(emulator.calls) == 1                                  # ONE sandboxed run does it all


def test_the_probe_command_has_the_shape_the_wrapper_uses_for_a_real_command():
    emulator, _ = run()
    argv = emulator.calls[0]
    assert argv[0] == BWRAP
    sep = argv.index("--")
    assert argv[sep + 1:sep + 4] == [PYTHON, "-I", "/run/m2c-net/bridge.py"]
    assert "--unshare-net" in argv                                    # the namespace stays unshared for the probe too
    assert argv[-3] .endswith("sh") or argv[-3].endswith("bash")
    assert argv[-2] == "-c"


def test_the_mount_is_read_only_and_before_the_remount():
    emulator, _ = run()
    argv = emulator.calls[0]
    i = argv.index("/run/m2c-net")
    assert argv[i - 2] == "--ro-bind"


def test_a_direct_connection_that_works_fails_the_probe():
    _, result = run(Emulator(direct="CONNECTED"))
    assert not result.works and result.reason_code == "check_failed"
    assert result.failed == ("direct_connect_fails",)
    assert "direct_connect_fails" in result.describe()


def test_a_writable_socket_directory_fails_the_probe():
    _, result = run(Emulator(writable=True))
    assert not result.works and result.failed == ("socket_dir_private",)


@pytest.mark.parametrize("tag, check", [
    ("DENIED", "not_allowed_refused"), ("IPLIT", "ip_literal_refused"), ("PRIVATE", "private_resolution_refused"),
])
def test_a_refusal_that_is_not_one_fails_its_check(tag, check):
    def mutate(text):
        return re.sub(rf"REPLY_BEGIN {tag} \d+", f"REPLY_BEGIN {tag} 200", text)

    _, result = run(Emulator(mutate=mutate))
    assert not result.works and check in result.failed


def test_an_allowed_host_that_does_not_work_fails_its_check():
    def mutate(text):
        return re.sub(r"REPLY_BEGIN ALLOWED \d+", "REPLY_BEGIN ALLOWED 502", text)

    _, result = run(Emulator(mutate=mutate))
    assert not result.works and "allowed_host_works" in result.failed


def test_the_banner_must_come_from_the_host_upstream():
    def mutate(text):
        return re.sub(r"M2CNET[0-9a-f]{12}", "M2CNETdeadbeefcafe", text)

    _, result = run(Emulator(mutate=mutate))
    assert "allowed_host_works" in result.failed


def test_a_refused_destination_that_somehow_reached_the_upstream_fails_the_refusals(monkeypatch):
    """The upstream's accept count is the host-side evidence: more than the one allowed connection fails the refusals."""
    real_serve = probe._FakeUpstream._serve

    def extra_hit(self):
        self.accepted += 1                          # as if a refused destination had been connected too
        real_serve(self)

    monkeypatch.setattr(probe._FakeUpstream, "_serve", extra_hit)
    _, result = run()
    assert not result.works
    assert {"not_allowed_refused", "ip_literal_refused", "private_resolution_refused", "allowed_host_works"} <= set(result.failed)


def test_a_missing_reply_fails_closed():
    _, result = run(Emulator(mutate=lambda text: re.sub(r"REPLY_BEGIN[^\n]*\n[^\n]*\nREPLY_END [A-Z]+\n", "", text)))
    assert not result.works and set(result.failed) >= {"not_allowed_refused", "allowed_host_works"}


# --------------------------------------------------------------------------
# the ways the probe cannot even run
# --------------------------------------------------------------------------
def test_a_sandbox_that_does_not_start_is_bridge_failed_with_fixed_text():
    _, result = run(Emulator(ran=False))
    assert not result.works and result.reason_code == "bridge_failed"
    assert result.reason == NETWORK_REASONS["bridge_failed"] and "namespace" not in result.reason


def test_a_timeout_is_a_timeout():
    _, result = run(Emulator(timeout=True))
    assert result.reason_code == "timeout" and not result.works


def test_a_runner_that_raises_is_probe_error_and_leaks_nothing():
    _, result = run(Emulator(boom=True))
    assert result.reason_code == "probe_error" and not result.works
    assert "secret" not in result.reason and "exploded" not in result.reason


def test_no_bubblewrap_is_bwrap_missing(monkeypatch):
    monkeypatch.setattr(probe, "find_bwrap", lambda: None)
    result = run_network_probe(runner=Emulator(), python=PYTHON)
    assert result.reason_code == "bwrap_missing" and result.reason == NETWORK_REASONS["bwrap_missing"]


def test_no_system_python_is_no_python_and_nothing_is_started(monkeypatch):
    monkeypatch.setattr(bwrap, "find_sandbox_python", lambda root="/": None)
    started: list[object] = []
    monkeypatch.setattr(netproxy.NetworkService, "start", lambda self: started.append(self))
    emulator = Emulator()
    result = run_network_probe(runner=emulator, bwrap_path=BWRAP)
    assert result.reason_code == "no_python" and result.python is None
    assert emulator.calls == [] and started == []


def test_a_proxy_that_cannot_start_is_proxy_failed(monkeypatch):
    def broken(self):
        raise netproxy.NetworkError("start_failed")

    monkeypatch.setattr(netproxy.NetworkService, "start", broken)
    emulator = Emulator()
    result = run_network_probe(runner=emulator, bwrap_path=BWRAP, python=PYTHON)
    assert result.reason_code == "proxy_failed" and emulator.calls == []


def test_every_result_holds_fixed_text_only():
    texts = set(NETWORK_REASONS.values())
    for emulator in (Emulator(ran=False), Emulator(timeout=True), Emulator(boom=True), Emulator(direct="CONNECTED")):
        _, result = run(emulator)
        assert not result.works
        assert result.reason in texts or result.reason.startswith(tuple(texts))
        assert "/" not in result.reason.replace("(bwrap)", "")


# --------------------------------------------------------------------------
# no leftovers
# --------------------------------------------------------------------------
def test_the_proxy_the_directory_and_the_fake_upstream_are_gone_afterwards():
    emulator, result = run()
    assert result.works
    assert emulator.dirs and not os.path.exists(emulator.dirs[0])


def test_cleanup_happens_when_the_probe_fails_too():
    emulator, _ = run(Emulator(boom=False, direct="CONNECTED"))
    assert not os.path.exists(emulator.dirs[0])
    emulator, _ = run(Emulator(timeout=True))


def test_the_fake_upstream_listener_is_closed(monkeypatch):
    ports: list[int] = []
    real = probe._FakeUpstream.__init__

    def spy(self, banner):
        real(self, banner)
        ports.append(self.port)

    monkeypatch.setattr(probe._FakeUpstream, "__init__", spy)
    run()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", ports[0]), 1)


def test_no_thread_is_left_behind():
    import threading

    before = {t.name for t in threading.enumerate()}
    run()
    run(Emulator(direct="CONNECTED"))
    after = {t.name for t in threading.enumerate()}
    assert not {n for n in after - before if n.startswith("m2c-")}


def test_the_probes_loopback_flag_is_used_only_inside_the_probe(monkeypatch):
    seen: list[bool] = []
    real = netproxy.ProxyServer.__init__

    def spy(self, *a, **kw):
        seen.append(bool(kw.get("_test_allow_loopback")))
        real(self, *a, **kw)

    monkeypatch.setattr(netproxy.ProxyServer, "__init__", spy)
    run()
    assert seen == [True]
    seen.clear()
    netproxy.make_service()
    assert seen == [False]


# --------------------------------------------------------------------------
# the cache
# --------------------------------------------------------------------------
def test_the_session_probe_runs_once_per_settings_and_reset_forgets(monkeypatch):
    monkeypatch.setattr(probe, "run_network_probe", REAL_RUN_NETWORK_PROBE)
    reset_session_cache()
    emulator = Emulator()
    kw = dict(runner=emulator, bwrap_path=BWRAP, python=PYTHON)
    first = session_network_probe(**kw)
    second = session_network_probe(**kw)
    assert first is second and len(emulator.calls) == 1
    other = session_network_probe(extra_ro=("~/x",), **kw)
    assert other is not first and len(emulator.calls) == 2
    reset_session_cache()
    session_network_probe(**kw)
    assert len(emulator.calls) == 3
    reset_session_cache()


def test_the_main_sessions_cache_reset_also_clears_the_network_one(monkeypatch):
    monkeypatch.setattr(probe, "run_network_probe", REAL_RUN_NETWORK_PROBE)
    emulator = Emulator()
    kw = dict(runner=emulator, bwrap_path=BWRAP, python=PYTHON)
    session_network_probe(**kw)
    reset_session_cache()
    session_network_probe(**kw)
    assert len(emulator.calls) == 2
    reset_session_cache()
