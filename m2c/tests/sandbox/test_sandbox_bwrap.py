"""
m2c/tests/sandbox/test_sandbox_bwrap.py -- Patch C11a: finding bubblewrap and building the full command
(``m2c.sandbox.bwrap``).  No bubblewrap is started: the binary is a file in ``tmp_path`` and the system
directories come from a fake root, so these run on any machine.

Run: cd m2c && pytest tests/sandbox/test_sandbox_bwrap.py -v
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from m2c.sandbox import bwrap
from m2c.sandbox.bwrap import find_bwrap, find_shell, outer_environment, signal_from_exit_code, wrap_command
from m2c.sandbox.profile import ProfileError, detect_system_layout

from test_sandbox_profile import ENV, make_root  # tests/ has no packages: this sibling module is importable by name


def make_exe(path: Path, text="#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _no_system_fallback(monkeypatch):
    """Isolate the PATH search from whatever bubblewrap this machine has."""
    monkeypatch.setattr(bwrap, "_SAFE_PATH_FALLBACK", ())


# ==========================================================================
# finding bubblewrap
# ==========================================================================
def test_bwrap_is_found_in_an_absolute_path_entry(tmp_path):
    exe = make_exe(tmp_path / "bin" / "bwrap")
    assert find_bwrap(f"/nonexistent:{tmp_path / 'bin'}") == str(exe)


def test_a_relative_path_entry_can_never_supply_bubblewrap(tmp_path, monkeypatch):
    """The project directory is the model's workspace: a ``bwrap`` file there must never be what gets run."""
    make_exe(tmp_path / "bwrap")
    make_exe(tmp_path / "bin" / "bwrap")
    monkeypatch.chdir(tmp_path)
    for entry in (".", "", "bin", "./bin", "..", "~"):
        assert find_bwrap(entry) is None, repr(entry)
        assert find_bwrap(f"{entry}{os.pathsep}/nonexistent") is None, repr(entry)


def test_a_file_that_is_not_executable_or_not_a_file_is_not_bubblewrap(tmp_path):
    plain = tmp_path / "a" / "bwrap"
    plain.parent.mkdir()
    plain.write_text("x")
    plain.chmod(0o644)
    (tmp_path / "b" / "bwrap").mkdir(parents=True)
    assert find_bwrap(f"{tmp_path / 'a'}:{tmp_path / 'b'}") is None


def test_the_first_match_in_path_order_wins(tmp_path):
    first = make_exe(tmp_path / "one" / "bwrap")
    make_exe(tmp_path / "two" / "bwrap")
    assert find_bwrap(f"{tmp_path / 'one'}:{tmp_path / 'two'}") == str(first)


def test_no_path_means_not_found(monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert find_bwrap() is None
    assert find_bwrap("\x00bad") is None


def test_the_usual_system_directories_are_tried_when_path_is_stripped(tmp_path, monkeypatch):
    exe = make_exe(tmp_path / "usrbin" / "bwrap")
    monkeypatch.setattr(bwrap, "_SAFE_PATH_FALLBACK", (str(tmp_path / "usrbin"),))
    assert find_bwrap("") == str(exe)


# ==========================================================================
# the shell inside the sandbox
# ==========================================================================
def test_the_shell_is_bash_when_there_is_one_else_sh(tmp_path):
    root = make_root(tmp_path)
    assert find_shell(os.fspath(root)) == "/usr/bin/bash"
    (root / "usr/bin/bash").unlink()
    assert find_shell(os.fspath(root)) == "/usr/bin/sh"
    (root / "usr/bin/sh").unlink()
    assert find_shell(os.fspath(root)) is None


def test_the_shell_path_is_absolute_and_inside_the_sandbox_layout(tmp_path):
    root = make_root(tmp_path, merged_usr=False)
    (root / "usr/bin/bash").unlink()
    assert find_shell(os.fspath(root)) == "/bin/bash"


# ==========================================================================
# the full command
# ==========================================================================
@pytest.fixture
def parts(tmp_path):
    layout = detect_system_layout(os.fspath(make_root(tmp_path)))
    exe = make_exe(tmp_path / "bin" / "bwrap")
    home = tmp_path / "home"
    project = home / "proj"
    project.mkdir(parents=True)
    return dict(layout=layout, bwrap_path=str(exe), project=os.path.realpath(project), home=os.path.realpath(home))


def full(parts, command="echo hi", **kw):
    return wrap_command(command, env=ENV, **{**parts, **kw})


def test_the_command_is_bwrap_then_the_profile_then_double_dash_shell_dash_c_command(parts):
    argv = full(parts, "echo hi")
    assert argv[0] == parts["bwrap_path"]
    i = argv.index("--")
    assert argv[i:] == ["--", "/usr/bin/bash", "-c", "echo hi"]
    assert argv[1:i][0] == "--unshare-user-try"


def test_the_command_is_one_element_whatever_it_contains(parts):
    for command in ("echo 'a b' && ls; rm -rf /", "-rf --unshare-all", "--help", "$(id)", "line1\nline2", " ", "é中"):
        argv = full(parts, command)
        assert argv[-1] == command and argv[-2] == "-c" and argv[-4] == "--"
        assert argv.count(command) == 1 or command in ("--help",)


def test_a_command_that_looks_like_an_option_cannot_become_one(parts):
    argv = full(parts, "--share-net")
    assert argv.index("--") < len(argv) - 1 and argv.index("--share-net") == len(argv) - 1
    assert "--share-net" not in argv[:argv.index("--")]


@pytest.mark.parametrize("bad", [5, None, b"x", ["echo"], "a\x00b"])
def test_a_command_that_is_not_a_clean_string_is_refused(parts, bad):
    with pytest.raises(ProfileError) as info:
        full(parts, bad)
    assert info.value.code == "command_invalid"


def test_without_bubblewrap_there_is_no_command(parts, monkeypatch):
    monkeypatch.setenv("PATH", "")
    parts = {**parts, "bwrap_path": None}
    with pytest.raises(ProfileError) as info:
        full(parts)
    assert info.value.code == "bwrap_missing"


def test_without_a_shell_there_is_no_command(tmp_path):
    root = make_root(tmp_path)
    (root / "usr/bin/bash").unlink()
    (root / "usr/bin/sh").unlink()
    layout = detect_system_layout(os.fspath(root))
    home = tmp_path / "home"
    (home / "p").mkdir(parents=True)
    with pytest.raises(ProfileError) as info:
        wrap_command("x", project=os.path.realpath(home / "p"), home=os.path.realpath(home), env=ENV, layout=layout,
                     bwrap_path=str(make_exe(tmp_path / "bwrap")))
    assert info.value.code == "no_shell"


def test_profile_errors_pass_through_with_their_fixed_text(parts):
    with pytest.raises(ProfileError) as info:
        full(parts, project="/mnt/c/Users/x/proj")
    assert info.value.code == "project_on_windows_drive"


def test_the_home_defaults_to_the_real_home(parts, monkeypatch):
    monkeypatch.setenv("HOME", parts["home"])
    argv = wrap_command("x", env=ENV, **{k: v for k, v in parts.items() if k != "home"})
    assert ("--tmpfs", parts["home"]) in zip(argv, argv[1:])
    assert bwrap.default_home() == parts["home"]


def test_a_relative_or_missing_home_falls_back_to_the_passwd_entry(monkeypatch):
    monkeypatch.setenv("HOME", "relative/home")
    home = bwrap.default_home()
    assert home is None or os.path.isabs(home)
    monkeypatch.delenv("HOME")
    home = bwrap.default_home()
    assert home is None or os.path.isabs(home)


def test_the_toolchain_extras_and_denied_lists_reach_the_profile(parts, tmp_path):
    (Path(parts["home"]) / "tools").mkdir()
    argv = full(parts, extra_ro=("tools",), toolchain_dirs=())
    dest = f"{parts['home']}/tools"
    assert any(argv[i] == "--ro-bind" and argv[i + 2] == dest for i in range(len(argv) - 2))
    with pytest.raises(ProfileError):
        full(parts, denied=(parts["project"],))


def test_the_outer_environment_is_only_a_path():
    env = outer_environment()
    assert set(env) == {"PATH"} and all(os.path.isabs(d) for d in env["PATH"].split(":"))


def test_nothing_is_created_or_started(parts, tmp_path):
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    full(parts)
    assert before == sorted(str(p) for p in tmp_path.rglob("*"))


# ==========================================================================
# exit statuses
# ==========================================================================
@pytest.mark.parametrize("code,sig", [(129, 1), (130, 2), (137, 9), (143, 15), (159, 31)])
def test_an_exit_status_from_129_to_159_stands_for_a_signal(code, sig):
    assert signal_from_exit_code(code) == sig


@pytest.mark.parametrize("code", [None, 0, 1, 2, 127, 128, 160, 255, -9, True, False, "137", 137.0])
def test_other_exit_statuses_are_not_signals(code):
    assert signal_from_exit_code(code) is None
