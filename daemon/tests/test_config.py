"""
daemon/tests/test_config.py — F2 Part 1: proves config loading fails
loudly on every unsafe/incomplete case rather than silently starting
the daemon with a bad root or a missing token.

Run: pytest daemon/tests/test_config.py -v
"""
import pytest

from daemon.config import ConfigError, load_config

VALID_TOKEN = "a" * 32


def _write_env(path, token=VALID_TOKEN, root=None):
    root_line = f"MINIME_ALLOWED_ROOT={root}" if root is not None else ""
    path.write_text(f"MINIME_PAIRING_TOKEN={token}\n{root_line}\n")


def test_load_config_succeeds_with_valid_env(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    env_file = tmp_path / ".env"
    _write_env(env_file, root=str(project_root))

    config = load_config(env_path=env_file)

    assert config.pairing_token == VALID_TOKEN
    assert config.allowed_root == project_root.resolve()


def test_load_config_missing_file_raises(tmp_path):
    missing_env = tmp_path / "does-not-exist.env"
    with pytest.raises(ConfigError, match="no config found"):
        load_config(env_path=missing_env)


def test_load_config_missing_token_raises(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    env_file = tmp_path / ".env"
    _write_env(env_file, token="", root=str(project_root))

    with pytest.raises(ConfigError, match="MINIME_PAIRING_TOKEN is not set"):
        load_config(env_path=env_file)


def test_load_config_short_token_raises(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    env_file = tmp_path / ".env"
    _write_env(env_file, token="tooshort", root=str(project_root))

    with pytest.raises(ConfigError, match="too short"):
        load_config(env_path=env_file)


def test_load_config_missing_root_raises(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(env_file, root="")

    with pytest.raises(ConfigError, match="MINIME_ALLOWED_ROOT is not set"):
        load_config(env_path=env_file)


def test_load_config_unsafe_root_raises(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(env_file, root="/")

    with pytest.raises(ConfigError, match="too broad"):
        load_config(env_path=env_file)


def test_load_config_nonexistent_root_raises(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(env_file, root=str(tmp_path / "nope"))

    with pytest.raises(ConfigError, match="does not exist"):
        load_config(env_path=env_file)
