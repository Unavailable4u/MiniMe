"""
m2c/tests/sandbox/test_net_doctor.py -- Patch C11b: what ``m2c doctor`` says about the command network.

The report keeps its seven checks (the existing tests pin their names); the network is reported on the sandbox line:
the mode (``prompt`` or ``off``) and whether the bridge can run.  A network that cannot run is a WARNING with a fixed
reason and a fix, never an error and never a change of the sandbox's own verdict, and commands then simply have no
network.  Everything is faked: no bubblewrap, no proxy.

Place this file at: m2c/tests/sandbox/test_net_doctor.py
"""
from __future__ import annotations

import pytest

from m2c import doctor, sandbox
from m2c.doctor import Status
from m2c.sandbox.probe import CHECK_NAMES, NETWORK_CHECK_NAMES, NETWORK_REASONS, CheckOutcome, NetworkProbeResult

NAME = "bubblewrap (bwrap)"
TOOLS = {"rg": "/usr/bin/rg", "git": "/usr/bin/git", "bwrap": "/usr/bin/bwrap"}


def sandbox_ok():
    return sandbox.ProbeResult(True, "ok", "the sandbox works", tuple(CheckOutcome(n, True, "ok") for n in CHECK_NAMES),
                               "/usr/bin/bwrap", 100)


def sandbox_bad():
    return sandbox.ProbeResult(False, "userns_unavailable", sandbox.probe.REASONS["userns_unavailable"], (), "/usr/bin/bwrap")


def net_ok():
    return NetworkProbeResult(True, "ok", "the network works", tuple(CheckOutcome(n, True, "ok") for n in NETWORK_CHECK_NAMES),
                              "/usr/bin/python3", 50)


def net_bad(code, checks=()):
    return NetworkProbeResult(False, code, NETWORK_REASONS[code], tuple(checks), None, 10)


def line(make_env, **overrides):
    report = doctor.run_doctor(env=make_env(tools=dict(TOOLS), **overrides))
    return next(r for r in report.results if r.name == NAME)


def test_the_report_still_has_the_same_seven_checks(make_env):
    names = [r.name for r in doctor.run_doctor(env=make_env()).results]
    assert len(names) == 7 and names.count(NAME) == 1


def test_prompt_mode_with_a_working_bridge_is_green_and_says_so(make_env):
    result = line(make_env, sandbox_probe=sandbox_ok, network_probe=net_ok)
    assert result.status is Status.OK
    assert "all 11 sandbox checks passed" in result.detail
    assert "network: prompt mode" in result.detail and "all 6 network checks passed" in result.detail


def test_off_mode_says_off_and_does_not_run_the_network_probe(make_env):
    def never():
        raise AssertionError("the network probe must not run when the network is off")

    result = line(make_env, sandbox_probe=sandbox_ok, network_probe=never, network_mode="off")
    assert result.status is Status.OK and "network: off (setting)" in result.detail


def test_no_system_python_is_a_warning_with_the_fix(make_env):
    result = line(make_env, sandbox_probe=sandbox_ok, network_probe=lambda: net_bad("no_python"))
    assert result.status is Status.WARN
    assert NETWORK_REASONS["no_python"] in result.detail and "no network access" in result.detail
    assert "python3" in result.hint and "apt install" in result.hint
    assert "all 11 sandbox checks passed" in result.detail                      # the sandbox's own verdict is unchanged


def test_a_failed_network_check_names_the_check(make_env):
    bad = NetworkProbeResult(False, "check_failed", NETWORK_REASONS["check_failed"],
                             (CheckOutcome("direct_connect_fails", False, "violated"),), "/usr/bin/python3")
    result = line(make_env, sandbox_probe=sandbox_ok, network_probe=lambda: bad)
    assert result.status is Status.WARN and "direct_connect_fails" in result.detail


@pytest.mark.parametrize("code", ["bridge_failed", "proxy_failed", "timeout", "probe_error", "check_failed"])
def test_every_failure_is_a_warning_never_an_error(make_env, code):
    result = line(make_env, sandbox_probe=sandbox_ok, network_probe=lambda: net_bad(code))
    assert result.status is Status.WARN and result.hint


def test_a_network_probe_that_crashes_is_a_warning_and_leaks_nothing(make_env):
    def boom():
        raise RuntimeError("/home/x/secret exploded")

    result = line(make_env, sandbox_probe=sandbox_ok, network_probe=boom)
    assert result.status is Status.WARN and "no network access" in result.detail
    assert "secret" not in result.detail and "secret" not in result.hint


def test_a_broken_sandbox_is_reported_as_before_and_the_network_probe_is_not_run(make_env):
    def never():
        raise AssertionError("no network probe without a working sandbox")

    result = line(make_env, sandbox_probe=sandbox_bad, network_probe=never)
    assert result.status is Status.WARN and "does not work" in result.detail and "network:" not in result.detail


def test_without_bwrap_the_line_is_unchanged(make_env):
    report = doctor.run_doctor(env=make_env(tools={"rg": "/usr/bin/rg", "git": "/usr/bin/git"}, network_probe=lambda: net_ok()))
    result = next(r for r in report.results if r.name == NAME)
    assert result.status is Status.WARN and "not found on PATH" in result.detail and "network" not in result.detail.lower()


def test_an_environment_built_by_hand_without_the_new_fields_still_works(make_env):
    result = line(make_env, sandbox_probe=sandbox_ok)
    assert result.status is Status.OK and "network" not in result.detail


def test_the_detected_environment_wires_the_real_network_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox.probe, "run_network_probe", lambda **kw: calls.append(kw) or net_ok())
    env = doctor.Environment.detect()
    assert env.network_mode == "prompt" and env.network_probe is not None
    assert env.network_probe().works and calls == [{}]


def test_the_rendered_report_shows_the_network_note(make_env):
    text = doctor.run_doctor(env=make_env(tools=dict(TOOLS), sandbox_probe=sandbox_ok, network_probe=net_ok)).render()
    assert "network: prompt mode" in text
