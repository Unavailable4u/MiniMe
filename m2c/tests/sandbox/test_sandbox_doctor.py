"""
m2c/tests/sandbox/test_sandbox_doctor.py -- Patch C11a: what ``m2c doctor`` says about the sandbox.

The doctor's bubblewrap line used to mean "the program is on PATH".  Now, when it is, the doctor runs the sandbox
self-test and the line says the truth: ok (with how many checks passed), or a warning naming exactly which check failed
or why the sandbox cannot be built.  These tests drive the check with fake probe results through ``Environment.
sandbox_probe`` (no bubblewrap needed) and, where bubblewrap works, with the real one.

Pinned: a sandbox that does not work is NEVER "ok"; a probe that crashes is not "ok" either; the line stays a warning
(never a failure: m2c still runs without a sandbox, with its warning); the hints are specific; nothing in the text is a
path of the throw-away world.

Run: cd m2c && pytest tests/sandbox/test_sandbox_doctor.py -v -rs
"""
from __future__ import annotations

import sys

import pytest

from m2c import doctor, sandbox
from m2c.doctor import Status
from m2c.sandbox.probe import CHECK_NAMES, REASONS, CheckOutcome, ProbeResult



def result(report: doctor.Report, name_fragment: str) -> doctor.CheckResult:
    """The one result whose name contains ``name_fragment`` (a local copy of tests/test_doctor.py's finder: importing
    that module pulls in ``conftest`` by bare name, which resolves to tests/tools/conftest.py when only the sandbox
    and tools tests are collected)."""
    matches = [r for r in report.results if name_fragment.lower() in r.name.lower()]
    assert len(matches) == 1, [r.name for r in report.results]
    return matches[0]

TOOLS = {"rg": "/usr/bin/rg", "git": "/usr/bin/git", "bwrap": "/usr/bin/bwrap"}


def good() -> ProbeResult:
    return ProbeResult(True, "ok", "the sandbox works", tuple(CheckOutcome(n, True, "ok") for n in CHECK_NAMES), "/usr/bin/bwrap", 100)


def check_failed(*names: str) -> ProbeResult:
    checks = tuple(CheckOutcome(n, n not in names, "ok" if n not in names else "violated") for n in CHECK_NAMES)
    return ProbeResult(False, "check_failed", f"{REASONS['check_failed']}: {', '.join(names)}", checks, "/usr/bin/bwrap", 100)


def cannot_start(code: str) -> ProbeResult:
    return ProbeResult(False, code, REASONS[code], (CheckOutcome("basic", False, "setup_failed"),), "/usr/bin/bwrap", 10)


def line(make_env, probe, **kw):
    return result(doctor.run_doctor(env=make_env(tools=kw.pop("tools", TOOLS), sandbox_probe=probe, **kw)), "bubblewrap")


# ==========================================================================
# ok
# ==========================================================================
def test_a_working_sandbox_is_ok_and_says_how_many_checks_passed(make_env):
    r = line(make_env, good)
    assert r.status is Status.OK
    assert "/usr/bin/bwrap" in r.detail and f"all {len(CHECK_NAMES)} sandbox checks passed" in r.detail


def test_the_report_is_all_green_with_a_working_sandbox(make_env):
    report = doctor.run_doctor(env=make_env(sandbox_probe=good))
    text = report.render()
    assert report.ok and "WARN" not in text and "FAIL" not in text and "All hard requirements are met" in text


def test_the_line_keeps_its_name(make_env):
    assert line(make_env, good).name == "bubblewrap (bwrap)"


def test_without_a_probe_the_line_is_what_it_was_before(make_env):
    r = result(doctor.run_doctor(env=make_env()), "bubblewrap")
    assert r.status is Status.OK and r.detail == "/usr/bin/bwrap"


# ==========================================================================
# the truth when it does not work
# ==========================================================================
@pytest.mark.parametrize("failed", [("write_git",), ("network_unreachable",), ("write_outside_project", "cd_up_write")])
def test_a_failed_check_is_a_warning_that_names_it(make_env, failed):
    r = line(make_env, lambda: check_failed(*failed))
    assert r.status is Status.WARN
    assert "does not work" in r.detail
    for name in failed:
        assert name in r.detail
    assert "without a sandbox" in r.detail, "the consequence is stated"


@pytest.mark.parametrize("code", ["userns_unavailable", "proc_unavailable", "netns_unavailable", "bwrap_failed", "timeout"])
def test_a_sandbox_that_cannot_start_says_why(make_env, code):
    r = line(make_env, lambda: cannot_start(code))
    assert r.status is Status.WARN and REASONS[code] in r.detail


def test_user_namespace_trouble_comes_with_the_actual_fix(make_env):
    r = line(make_env, lambda: cannot_start("userns_unavailable"))
    assert "kernel.apparmor_restrict_unprivileged_userns=0" in r.hint and "unprivileged_userns_clone" in r.hint


def test_other_reasons_get_their_own_or_a_generic_hint(make_env):
    assert "/proc" in line(make_env, lambda: cannot_start("proc_unavailable")).hint
    assert "namespace" in line(make_env, lambda: cannot_start("netns_unavailable")).hint
    assert line(make_env, lambda: check_failed("write_git")).hint


@pytest.mark.parametrize("probe", [
    lambda: ProbeResult(False, "check_failed", "x", ()),
    lambda: ProbeResult(False, "probe_error", REASONS["probe_error"]),
    lambda: cannot_start("userns_unavailable"),
    lambda: None,
    lambda: object(),
    lambda: "works",
])
def test_nothing_that_is_not_a_clean_pass_is_ever_ok(make_env, probe):
    assert line(make_env, probe).status is Status.WARN


def test_a_probe_that_crashes_is_a_warning_not_a_green_line(make_env):
    def boom():
        raise RuntimeError("secret detail " + "x" * 5)

    r = line(make_env, boom)
    assert r.status is Status.WARN and "could not be run" in r.detail and "secret detail" not in r.detail + r.hint


def test_a_failing_sandbox_never_fails_the_doctor(make_env):
    report = doctor.run_doctor(env=make_env(sandbox_probe=lambda: check_failed("write_git")))
    assert report.ok, "warnings do not change the exit status"
    assert "WARN" in report.render() and "write_git" in report.render()


def test_no_bubblewrap_is_a_warning_and_the_probe_is_not_run(make_env):
    called = []
    r = line(make_env, lambda: called.append(1), tools={"rg": "/r", "git": "/g"})
    assert r.status is Status.WARN and "not found" in r.detail and "no command sandbox" in r.detail
    assert "apt install bubblewrap" in r.hint and called == []


def test_the_text_contains_no_path_of_the_throw_away_world(make_env, tmp_path):
    r = line(make_env, lambda: check_failed("write_git"))
    for text in (r.detail, r.hint):
        assert str(tmp_path) not in text and "m2c-probe-" not in text and "/var/tmp" not in text


# ==========================================================================
# wiring
# ==========================================================================
def test_the_real_environment_runs_a_fresh_probe_each_time(monkeypatch):
    calls = []
    fakes = [good(), check_failed("write_git")]
    monkeypatch.setattr(sandbox, "fresh_probe", lambda **kw: calls.append(1) or fakes[len(calls) - 1])
    env = doctor.Environment.detect()
    assert env.sandbox_probe is not None
    assert env.sandbox_probe().works is True and env.sandbox_probe().works is False and len(calls) == 2


def test_the_doctor_does_not_use_the_cached_session_probe(monkeypatch):
    monkeypatch.setattr(sandbox, "get_status", lambda **kw: pytest.fail("the doctor must look fresh, not at the cache"))
    monkeypatch.setattr(sandbox, "fresh_probe", lambda **kw: good())
    r = result(doctor.run_doctor(env=doctor.Environment.detect()), "bubblewrap")
    assert r.status in (Status.OK, Status.WARN)


def test_an_environment_built_by_hand_without_a_probe_still_works(make_env):
    assert make_env().sandbox_probe is None


# ==========================================================================
# the real thing
# ==========================================================================
@pytest.mark.real_sandbox
def test_the_real_doctor_reports_a_working_sandbox(make_env):
    from m2c.sandbox import bwrap

    if bwrap.find_bwrap() is None or not sandbox.fresh_probe().works:
        reason = "bubblewrap is missing or does not work here"
        sys.__stderr__.write(f"\nSKIPPED: real-sandbox doctor test: {reason}\n")
        pytest.skip(reason)
    r = line(make_env, sandbox.fresh_probe, tools={**TOOLS, "bwrap": bwrap.find_bwrap()})
    assert r.status is Status.OK and "sandbox checks passed" in r.detail
