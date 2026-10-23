"""
m2c/tests/test_paths.py -- Patch C01: the config directory.

Covers the M2C_HOME override, creation with mode 0700 (regardless of
umask), tightening of a looser pre-existing directory, and the fail-closed
cases (not a directory, owned by someone else, mode cannot be set).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from m2c import paths
from m2c.paths import ConfigDirError


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------
# Where is the directory?
# --------------------------------------------------------------------------
def test_default_is_dot_m2c_under_home(monkeypatch, _hermetic_env):
    monkeypatch.delenv("M2C_HOME", raising=False)
    assert paths.config_dir() == _hermetic_env / ".m2c"


def test_m2c_home_relocates_the_directory(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("M2C_HOME", str(target))
    assert paths.config_dir() == target


def test_empty_m2c_home_is_treated_as_unset(monkeypatch, _hermetic_env):
    monkeypatch.setenv("M2C_HOME", "")
    assert paths.config_dir() == _hermetic_env / ".m2c"


def test_m2c_home_expands_tilde(monkeypatch, _hermetic_env):
    monkeypatch.setenv("M2C_HOME", "~/custom")
    assert paths.config_dir() == _hermetic_env / "custom"


def test_m2c_home_is_normalised_lexically(tmp_path, monkeypatch):
    monkeypatch.setenv("M2C_HOME", f"{tmp_path}/a/../b")
    assert paths.config_dir() == tmp_path / "b"


def test_relative_m2c_home_is_rejected(monkeypatch):
    monkeypatch.setenv("M2C_HOME", "relative/dir")
    with pytest.raises(ConfigDirError, match="absolute"):
        paths.config_dir()


def test_explicit_environ_mapping_is_used_instead_of_os_environ(tmp_path):
    assert paths.config_dir({"M2C_HOME": str(tmp_path / "x")}) == tmp_path / "x"


# --------------------------------------------------------------------------
# Creation and mode 0700
# --------------------------------------------------------------------------
def test_creates_directory_with_mode_0700():
    created = paths.ensure_config_dir()
    assert created.is_dir()
    assert _mode(created) == 0o700


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077, 0o002])
def test_mode_is_0700_whatever_the_umask(umask):
    old = os.umask(umask)
    try:
        created = paths.ensure_config_dir()
    finally:
        os.umask(old)
    assert _mode(created) == 0o700


def test_creation_only_touches_the_m2c_home_location(tmp_path, _hermetic_env):
    paths.ensure_config_dir()
    # The (temp) HOME must be left alone: nothing named .m2c appears there
    # when M2C_HOME relocates the directory.
    assert not (_hermetic_env / ".m2c").exists()
    assert (tmp_path / "m2c-home").is_dir()


def test_nested_new_path_is_created_and_leaf_is_0700(tmp_path, monkeypatch):
    monkeypatch.setenv("M2C_HOME", str(tmp_path / "a" / "b" / "c"))
    created = paths.ensure_config_dir()
    assert _mode(created) == 0o700


def test_ensure_is_idempotent():
    first = paths.ensure_config_dir()
    (first / "keep.txt").write_text("x")
    second = paths.ensure_config_dir()
    assert first == second
    assert (second / "keep.txt").read_text() == "x"
    assert _mode(second) == 0o700


def test_looser_existing_directory_is_tightened_to_0700(tmp_path, monkeypatch):
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o755)
    monkeypatch.setenv("M2C_HOME", str(loose))
    paths.ensure_config_dir()
    assert _mode(loose) == 0o700


def test_existing_file_instead_of_directory_is_an_error(tmp_path, monkeypatch):
    f = tmp_path / "afile"
    f.write_text("not a dir")
    monkeypatch.setenv("M2C_HOME", str(f))
    with pytest.raises(ConfigDirError, match="not a directory"):
        paths.ensure_config_dir()


def test_parent_that_is_a_file_is_an_error(tmp_path, monkeypatch):
    f = tmp_path / "afile"
    f.write_text("not a dir")
    monkeypatch.setenv("M2C_HOME", str(f / "sub"))
    with pytest.raises(ConfigDirError):
        paths.ensure_config_dir()


def test_directory_owned_by_another_user_is_refused(monkeypatch):
    paths.ensure_config_dir()
    real_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_uid + 1)
    with pytest.raises(ConfigDirError, match="owned by another user"):
        paths.ensure_config_dir()


def test_fails_closed_when_mode_cannot_be_set(tmp_path, monkeypatch):
    """A filesystem that ignores chmod (typical of /mnt/c) must be an error,
    not a silently world-readable credentials directory."""
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o755)
    monkeypatch.setenv("M2C_HOME", str(loose))
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    with pytest.raises(ConfigDirError, match="0700"):
        paths.ensure_config_dir()


def test_chmod_oserror_is_reported_as_config_dir_error(tmp_path, monkeypatch):
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o755)
    monkeypatch.setenv("M2C_HOME", str(loose))

    def boom(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", boom)
    with pytest.raises(ConfigDirError, match="0700"):
        paths.ensure_config_dir()


# --------------------------------------------------------------------------
# Writability probe
# --------------------------------------------------------------------------
def test_prepare_returns_path_and_leaves_no_probe_file_behind():
    result = paths.prepare_config_dir()
    assert result.is_dir()
    assert list(result.iterdir()) == []


def test_prepare_reports_unwritable_directory(monkeypatch):
    """Root bypasses mode bits, so simulate the failure at the syscall."""
    import tempfile

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", denied)
    with pytest.raises(ConfigDirError, match="not writable"):
        paths.prepare_config_dir()
