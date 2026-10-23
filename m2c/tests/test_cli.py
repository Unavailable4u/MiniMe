"""
m2c/tests/test_cli.py -- Patch C01: the command-line surface and the
packaging promises (stdlib-only runtime, console entry point).
"""
from __future__ import annotations

import ast
import importlib.metadata as md
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import m2c
from m2c import cli, doctor
from m2c import logging_setup as ls

SRC = Path(m2c.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _clean_m2c_logger():
    yield
    logger = logging.getLogger(ls.LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)


def _patch_env(monkeypatch, env):
    monkeypatch.setattr(doctor.Environment, "detect", classmethod(lambda cls, project=None: env))


# --------------------------------------------------------------------------
# --version, help, usage errors
# --------------------------------------------------------------------------
def test_version_flag_prints_version_and_exits_zero(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"m2c {m2c.__version__}"


def test_version_has_no_side_effects(tmp_path, monkeypatch):
    target = tmp_path / "must-not-exist"
    monkeypatch.setenv("M2C_HOME", str(target))
    cli.main(["--version"])
    assert not target.exists()


def test_no_command_is_a_usage_error(capsys):
    assert cli.main([]) == cli.EXIT_USAGE
    assert "usage:" in capsys.readouterr().err


def test_unknown_command_is_a_usage_error(capsys):
    assert cli.main(["frobnicate"]) == cli.EXIT_USAGE
    assert "invalid choice" in capsys.readouterr().err


def test_help_documents_m2c_home(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "M2C_HOME" in out and "M2C_LOG_LEVEL" in out
    for command in ("login", "logout", "doctor"):
        assert command in out


# --------------------------------------------------------------------------
# login / logout stubs
# --------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["login", "logout"])
def test_login_and_logout_are_stubs_that_exit_non_zero(command, capsys):
    code = cli.main([command])
    captured = capsys.readouterr()
    assert code == cli.EXIT_NOT_IMPLEMENTED != 0
    assert "not implemented yet" in captured.err
    assert command in captured.err
    assert captured.out == ""


def test_stubs_do_not_create_the_config_dir(tmp_path, monkeypatch):
    target = tmp_path / "must-not-exist"
    monkeypatch.setenv("M2C_HOME", str(target))
    cli.main(["login"])
    cli.main(["logout"])
    assert not target.exists()


# --------------------------------------------------------------------------
# doctor exit codes
# --------------------------------------------------------------------------
def test_doctor_exits_zero_when_everything_is_fine(monkeypatch, make_env, capsys):
    _patch_env(monkeypatch, make_env())
    assert cli.main(["doctor"]) == cli.EXIT_OK
    assert "All hard requirements are met" in capsys.readouterr().out


def test_doctor_exits_one_when_a_hard_requirement_is_missing(monkeypatch, make_env, capsys):
    _patch_env(monkeypatch, make_env(tools={"git": "/usr/bin/git", "bwrap": "/b"}))  # no rg
    assert cli.main(["doctor"]) == cli.EXIT_FAILURE == 1
    assert "FAIL" in capsys.readouterr().out


def test_doctor_exits_zero_with_only_warnings(monkeypatch, make_env, capsys):
    _patch_env(monkeypatch, make_env(tools={"git": "/g", "rg": "/r"}))  # no bwrap
    assert cli.main(["doctor"]) == cli.EXIT_OK
    assert "WARN" in capsys.readouterr().out


def test_doctor_report_goes_to_stdout_not_stderr(monkeypatch, make_env, capsys):
    _patch_env(monkeypatch, make_env())
    cli.main(["doctor"])
    captured = capsys.readouterr()
    assert "Operating system" in captured.out
    assert captured.err == ""


def test_doctor_project_option_must_be_an_existing_directory(tmp_path, capsys):
    code = cli.main(["doctor", "--project", str(tmp_path / "nope")])
    assert code == cli.EXIT_USAGE
    assert "not a directory" in capsys.readouterr().err


def test_doctor_project_option_is_passed_through(monkeypatch, make_env, tmp_path):
    seen = {}

    def fake_detect(cls, project=None):
        seen["project"] = project
        return make_env()

    monkeypatch.setattr(doctor.Environment, "detect", classmethod(fake_detect))
    cli.main(["doctor", "--project", str(tmp_path)])
    assert seen["project"] == tmp_path


# --------------------------------------------------------------------------
# Process-level smoke tests: the real program, real subprocess
# --------------------------------------------------------------------------
def _run_cli(args, tmp_path, path_dirs=()):
    env = {
        "PATH": os.pathsep.join(str(p) for p in path_dirs),
        "HOME": str(tmp_path / "home"),
        "M2C_HOME": str(tmp_path / "proc-home"),
        "PYTHONPATH": str(SRC.parent),
    }
    return subprocess.run(
        [sys.executable, "-m", "m2c.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_process_doctor_exits_nonzero_when_tools_are_missing(tmp_path):
    """With an empty PATH, git and rg cannot be found whatever machine this
    runs on, so the real ``doctor`` must report them and exit 1."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    proc = _run_cli(["doctor", "--project", str(tmp_path)], tmp_path, [empty])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL" in proc.stdout
    assert "ripgrep" in proc.stdout and "git" in proc.stdout
    assert "Traceback" not in proc.stderr
    assert oct(os.stat(tmp_path / "proc-home").st_mode & 0o777) == "0o700"


def test_process_doctor_exits_zero_with_fake_tools(tmp_path):
    """Put stub git/rg/bwrap on PATH: on Linux/WSL2 the real doctor passes."""
    if sys.platform != "linux":
        pytest.skip("doctor only passes on Linux/WSL2 by design")
    import platform

    if "microsoft" in platform.release().lower() and "wsl2" not in platform.release().lower():
        pytest.skip("running on WSL1, which doctor rejects by design")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("git", "rg", "bwrap"):
        p = bindir / tool
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    proj = tmp_path / "proj"
    proj.mkdir()
    proc = _run_cli(["doctor", "--project", str(proj)], tmp_path, [bindir])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "All hard requirements are met" in proc.stdout


def test_process_version(tmp_path):
    proc = _run_cli(["--version"], tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"m2c {m2c.__version__}"


# --------------------------------------------------------------------------
# Packaging promises
# --------------------------------------------------------------------------
def _distribution():
    try:
        return md.distribution("m2c")
    except md.PackageNotFoundError:
        pytest.skip("m2c is not installed (run: pip install -e '.[dev]')")


def test_console_script_entry_point_is_registered():
    dist = _distribution()
    eps = [e for e in dist.entry_points if e.group == "console_scripts" and e.name == "m2c"]
    assert [e.value for e in eps] == ["m2c.cli:main"]


def test_installed_version_matches_package_version():
    assert _distribution().version == m2c.__version__


def test_requires_python_is_3_11_or_newer():
    assert _distribution().metadata["Requires-Python"] == ">=3.11"


def test_runtime_has_no_third_party_dependencies():
    """Decision for C01: the standard library only.  Dev extras are allowed."""
    reqs = _distribution().requires or []
    runtime = [r for r in reqs if "extra ==" not in r.replace('"', "").replace("'", "")]
    assert runtime == []


def test_runtime_modules_import_only_the_standard_library():
    """Same promise, checked on the source: no module under src/m2c imports
    anything outside the stdlib or m2c itself (works without installing)."""
    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for py in sorted(SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            offenders += [f"{py.name}: {n}" for n in names if n not in stdlib and n != "m2c"]
    assert offenders == []
