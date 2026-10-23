"""
m2c/tests/test_doctor.py -- Patch C01: ``m2c doctor``.

Every check is exercised on its own through a fake ``doctor.Environment``
(see conftest.make_env), so none of these tests depends on the machine they
run on: not on its OS, its Python, what is installed, or where the repo is
checked out.  The few tests that use the real filesystem do so only inside
pytest's tmp_path.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from m2c import doctor, paths
from m2c.doctor import Status

from conftest import NATIVE_RELEASE, WSL1_RELEASE, WSL2_RELEASE


def result(report: doctor.Report, name_fragment: str) -> doctor.CheckResult:
    matches = [r for r in report.results if name_fragment.lower() in r.name.lower()]
    assert len(matches) == 1, [r.name for r in report.results]
    return matches[0]


# --------------------------------------------------------------------------
# All green
# --------------------------------------------------------------------------
def test_all_green_report_passes(make_env):
    report = doctor.run_doctor(env=make_env())
    assert report.ok
    assert all(r.status is Status.OK for r in report.results)
    text = report.render()
    assert "All hard requirements are met" in text
    assert "FAIL" not in text and "WARN" not in text


def test_report_covers_exactly_the_checks_in_the_work_package(make_env):
    names = [r.name for r in doctor.run_doctor(env=make_env()).results]
    assert names == [
        "Operating system",
        "Python",
        "git",
        "ripgrep (rg)",
        "bubblewrap (bwrap)",
        "Project location",
        "Config directory",
    ]


# --------------------------------------------------------------------------
# Operating system: Linux / WSL2 pass; WSL1, native Windows and others fail
# --------------------------------------------------------------------------
def test_wsl2_is_supported_and_named(make_env):
    r = result(doctor.run_doctor(env=make_env(system="Linux", release=WSL2_RELEASE)), "operating")
    assert r.status is Status.OK
    assert "WSL2" in r.detail


def test_native_linux_is_supported(make_env):
    r = result(doctor.run_doctor(env=make_env(system="Linux", release=NATIVE_RELEASE)), "operating")
    assert r.status is Status.OK
    assert "WSL" not in r.detail


def test_wsl1_is_rejected_with_a_clear_message(make_env):
    report = doctor.run_doctor(env=make_env(system="Linux", release=WSL1_RELEASE))
    r = result(report, "operating")
    assert r.status is Status.FAIL
    assert "WSL1" in r.detail and "not supported" in r.detail
    assert "wsl --set-version" in r.hint
    assert not report.ok


@pytest.mark.parametrize("system", ["Windows", "CYGWIN_NT-10.0-19045", "MSYS_NT-10.0", "MINGW64_NT-10.0"])
def test_native_windows_is_rejected_with_a_clear_message(make_env, system):
    report = doctor.run_doctor(env=make_env(system=system, release="10"))
    r = result(report, "operating")
    assert r.status is Status.FAIL
    assert "Windows" in r.detail and "not supported" in r.detail
    assert "WSL2" in r.hint
    assert not report.ok


def test_macos_is_rejected_as_not_yet_supported(make_env):
    r = result(doctor.run_doctor(env=make_env(system="Darwin", release="23.5.0")), "operating")
    assert r.status is Status.FAIL
    assert "macOS" in r.detail


def test_unknown_system_is_rejected(make_env):
    r = result(doctor.run_doctor(env=make_env(system="FreeBSD", release="14.0")), "operating")
    assert r.status is Status.FAIL
    assert "FreeBSD" in r.detail


@pytest.mark.parametrize(
    "release, expected",
    [
        ("5.15.153.1-microsoft-standard-WSL2", "wsl2"),
        ("6.6.87.2-microsoft-standard-WSL2", "wsl2"),
        ("4.4.0-19041-Microsoft", "wsl1"),
        ("4.4.0-17763-Microsoft", "wsl1"),
        ("6.8.0-45-generic", "linux"),
        ("6.1.0-13-amd64", "linux"),
        ("", "linux"),
    ],
)
def test_classify_linux_release_strings(release, expected):
    assert doctor.classify_linux(release) == expected


# --------------------------------------------------------------------------
# Python version
# --------------------------------------------------------------------------
@pytest.mark.parametrize("version", [(3, 11, 0), (3, 11, 9), (3, 12, 3), (3, 13, 0), (4, 0, 0)])
def test_supported_python_versions_pass(make_env, version):
    r = result(doctor.run_doctor(env=make_env(python_version=version)), "python")
    assert r.status is Status.OK
    assert "%d.%d.%d" % version in r.detail


@pytest.mark.parametrize("version", [(3, 10, 12), (3, 9, 18), (3, 8, 0), (2, 7, 18)])
def test_old_python_is_a_hard_failure(make_env, version):
    report = doctor.run_doctor(env=make_env(python_version=version))
    r = result(report, "python")
    assert r.status is Status.FAIL
    assert "3.11" in r.detail
    assert not report.ok


# --------------------------------------------------------------------------
# Tools: git and rg are hard; bwrap is a warning
# --------------------------------------------------------------------------
def test_missing_git_is_a_hard_failure(make_env):
    report = doctor.run_doctor(env=make_env(tools={"rg": "/usr/bin/rg", "bwrap": "/usr/bin/bwrap"}))
    r = result(report, "git")
    assert r.status is Status.FAIL
    assert "not found" in r.detail
    assert "apt install git" in r.hint
    assert not report.ok


def test_missing_rg_is_a_hard_failure(make_env):
    report = doctor.run_doctor(env=make_env(tools={"git": "/usr/bin/git", "bwrap": "/usr/bin/bwrap"}))
    r = result(report, "ripgrep")
    assert r.status is Status.FAIL
    assert "apt install ripgrep" in r.hint
    assert not report.ok


def test_missing_bwrap_is_only_a_warning_with_clear_wording(make_env):
    report = doctor.run_doctor(env=make_env(tools={"rg": "/usr/bin/rg", "git": "/usr/bin/git"}))
    r = result(report, "bubblewrap")
    assert r.status is Status.WARN
    assert "sandbox" in r.detail.lower()
    assert "apt install bubblewrap" in r.hint
    assert report.ok  # warnings do not change the exit status
    assert "WARN" in report.render()


def test_everything_missing_reports_every_problem_not_just_the_first(make_env):
    report = doctor.run_doctor(env=make_env(tools={}))
    statuses = {r.name: r.status for r in report.results}
    assert statuses["git"] is Status.FAIL
    assert statuses["ripgrep (rg)"] is Status.FAIL
    assert statuses["bubblewrap (bwrap)"] is Status.WARN
    assert "2 failed" in report.render()


def test_tool_path_is_shown_when_found(make_env):
    r = result(doctor.run_doctor(env=make_env(tools={"git": "/opt/bin/git", "rg": "/x/rg", "bwrap": "/y/bwrap"})), "git")
    assert "/opt/bin/git" in r.detail


# --------------------------------------------------------------------------
# Project location (/mnt/c)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["/mnt/c", "/mnt/c/Users/me/proj", "/mnt/c/code/app/src"],
)
def test_project_under_mnt_c_warns(make_env, path):
    report = doctor.run_doctor(env=make_env(project_path=Path(path), resolved_project_path=Path(path)))
    r = result(report, "project")
    assert r.status is Status.WARN
    assert "/mnt/c" in r.detail
    assert "Linux filesystem" in r.hint
    assert report.ok  # a warning, not a hard failure


@pytest.mark.parametrize("path", ["/mnt/cat/proj", "/mnt/c2/x", "/mnt/d/proj", "/mnt", "/home/u/mnt/c", "/srv/mnt/c/x"])
def test_lookalike_paths_are_not_flagged(make_env, path):
    report = doctor.run_doctor(env=make_env(project_path=Path(path), resolved_project_path=Path(path)))
    assert result(report, "project").status is Status.OK


def test_symlink_that_resolves_into_mnt_c_warns(make_env):
    report = doctor.run_doctor(
        env=make_env(project_path=Path("/home/u/code/link"), resolved_project_path=Path("/mnt/c/real/proj"))
    )
    r = result(report, "project")
    assert r.status is Status.WARN
    assert "resolves to" in r.detail


def test_dotdot_cannot_hide_mnt_c(make_env):
    p = Path("/home/u/../../mnt/c/proj")
    report = doctor.run_doctor(env=make_env(project_path=p, resolved_project_path=p))
    assert result(report, "project").status is Status.WARN


def test_unknown_project_path_is_a_warning_not_a_crash(make_env):
    report = doctor.run_doctor(env=make_env(project_path=None, resolved_project_path=None))
    assert result(report, "project").status is Status.WARN
    assert report.ok


# --------------------------------------------------------------------------
# Config directory
# --------------------------------------------------------------------------
def test_unwritable_config_dir_is_a_hard_failure(make_env):
    def probe():
        raise paths.ConfigDirError("/home/u/.m2c is not writable (Permission denied)")

    report = doctor.run_doctor(env=make_env(prepare_config_dir=probe))
    r = result(report, "config")
    assert r.status is Status.FAIL
    assert "not writable" in r.detail
    assert "M2C_HOME" in r.hint
    assert not report.ok


def test_config_dir_ok_message_mentions_mode_and_path(make_env, tmp_path):
    r = result(doctor.run_doctor(env=make_env(prepare_config_dir=lambda: tmp_path / "cfg")), "config")
    assert r.status is Status.OK
    assert str(tmp_path / "cfg") in r.detail and "0700" in r.detail


def test_real_config_dir_probe_creates_a_0700_directory(tmp_path, monkeypatch, make_env):
    target = tmp_path / "real-probe"
    monkeypatch.setenv("M2C_HOME", str(target))
    env = make_env(prepare_config_dir=paths.prepare_config_dir)
    report = doctor.run_doctor(env=env)
    assert result(report, "config").status is Status.OK
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_real_config_dir_probe_fails_when_path_is_a_file(tmp_path, monkeypatch, make_env):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("M2C_HOME", str(blocker / "sub"))
    report = doctor.run_doctor(env=make_env(prepare_config_dir=paths.prepare_config_dir))
    assert result(report, "config").status is Status.FAIL
    assert not report.ok


def test_bad_m2c_home_is_reported_as_a_failed_check_not_a_traceback(monkeypatch, make_env):
    monkeypatch.setenv("M2C_HOME", "relative/path")
    report = doctor.run_doctor(env=make_env(prepare_config_dir=paths.prepare_config_dir))
    r = result(report, "config")
    assert r.status is Status.FAIL
    assert "absolute" in r.detail


# --------------------------------------------------------------------------
# A check that blows up must not take the report down (and fails closed)
# --------------------------------------------------------------------------
def test_crash_in_a_hard_check_fails_closed(make_env):
    def which(name):
        if name == "git":
            raise OSError("boom")
        return f"/usr/bin/{name}"

    report = doctor.run_doctor(env=make_env(which=which))
    r = result(report, "git")
    assert r.status is Status.FAIL
    assert "OSError" in r.detail
    assert not report.ok
    # the other checks still ran
    assert result(report, "ripgrep").status is Status.OK


def test_crash_in_a_soft_check_is_only_a_warning(make_env):
    def which(name):
        if name == "bwrap":
            raise OSError("boom")
        return f"/usr/bin/{name}"

    report = doctor.run_doctor(env=make_env(which=which))
    assert result(report, "bubblewrap").status is Status.WARN
    assert report.ok


def test_crash_in_config_probe_fails_closed(make_env):
    def probe():
        raise RuntimeError("unexpected")

    report = doctor.run_doctor(env=make_env(prepare_config_dir=probe))
    assert result(report, "config").status is Status.FAIL
    assert not report.ok


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def test_render_shows_status_name_detail_and_hint(make_env):
    text = doctor.run_doctor(env=make_env(tools={"rg": "/usr/bin/rg", "bwrap": "/b"})).render()
    assert "FAIL" in text and "git" in text and "apt install git" in text
    assert "1 failed" in text
    assert "Hard requirement" in text
    assert text.endswith("\n")


def test_render_summary_counts_warnings(make_env):
    text = doctor.run_doctor(env=make_env(tools={"rg": "/r", "git": "/g"})).render()
    assert "1 warning" in text
    assert "All hard requirements are met" in text
    assert "sandbox" in text.lower()


def test_render_is_plain_ascii_without_ansi_codes(make_env):
    text = doctor.run_doctor(env=make_env(tools={})).render()
    assert "\x1b" not in text
    text.encode("ascii")


# --------------------------------------------------------------------------
# Environment.detect (real probes, with the OS-level calls faked)
# --------------------------------------------------------------------------
def test_detect_reads_platform_python_and_path(monkeypatch, tmp_path):
    import platform
    import shutil

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "release", lambda: WSL2_RELEASE)
    monkeypatch.setattr(shutil, "which", lambda n: f"/fake/{n}")
    env = doctor.Environment.detect(project=tmp_path)
    assert env.system == "Linux" and env.release == WSL2_RELEASE
    assert env.which("rg") == "/fake/rg"
    assert env.project_path == tmp_path
    assert env.resolved_project_path == tmp_path.resolve()
    assert env.python_version[:2] >= (3, 11)


def test_detect_defaults_project_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env = doctor.Environment.detect()
    assert env.resolved_project_path == tmp_path.resolve()


def test_detect_resolves_a_symlinked_project(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    env = doctor.Environment.detect(project=link)
    assert env.project_path == link
    assert env.resolved_project_path == real.resolve()


def test_detect_survives_a_deleted_working_directory(monkeypatch, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    env = doctor.Environment.detect()
    assert env.project_path is None
    report = doctor.run_doctor(env=env)
    assert result(report, "project").status is Status.WARN


# --------------------------------------------------------------------------
# Readability
# --------------------------------------------------------------------------
def test_report_lines_fit_in_100_columns_even_with_every_check_failing(make_env):
    env = make_env(
        system="Linux",
        release=WSL1_RELEASE,
        python_version=(3, 9, 1),
        tools={},
        project_path=Path("/mnt/c/Users/someone/Documents/work/client-project/src"),
        resolved_project_path=Path("/mnt/c/Users/someone/Documents/work/client-project/src"),
        prepare_config_dir=lambda: (_ for _ in ()).throw(paths.ConfigDirError("x " * 80)),
    )
    text = doctor.run_doctor(env=env).render()
    too_long = [line for line in text.splitlines() if len(line) > 100]
    assert too_long == []


def test_install_commands_are_on_their_own_lines(make_env):
    text = doctor.run_doctor(env=make_env(tools={"git": "/g", "bwrap": "/b"})).render()
    assert "sudo apt install ripgrep" in text
    assert "sudo dnf install ripgrep" in text
    assert "sudo pacman -S ripgrep" in text
    apt_lines = [line for line in text.splitlines() if "apt install ripgrep" in line]
    assert len(apt_lines) == 1 and "dnf" not in apt_lines[0]


def test_an_overlong_path_is_never_split_across_lines(make_env):
    """Wrapping happens between words only, so a path stays copy-pasteable
    even when it alone is wider than the report."""
    long_path = Path("/mnt/c/" + "/".join(["a-rather-long-directory-name"] * 4))
    env = make_env(project_path=long_path, resolved_project_path=long_path)
    text = doctor.run_doctor(env=env).render()
    assert str(long_path) in text
