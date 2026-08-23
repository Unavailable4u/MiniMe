"""
daemon/tests/test_config.py — F2 Part 1 + Part 2: proves config loading
fails loudly on every unsafe/incomplete case rather than silently
starting the daemon with a bad root, a missing token, or (Part 2) an
unusable backend websocket URL / workspace id.

Run: pytest daemon/tests/test_config.py -v
"""
import pytest

from daemon.config import ConfigError, load_config

VALID_TOKEN = "a" * 32
VALID_WS_URL = "ws://localhost:8000"  # matches daemon/.env.example's own local-dev example
VALID_WORKSPACE_ID = "test-workspace"


def _write_env(path, token=VALID_TOKEN, root=None, ws_url=VALID_WS_URL,
                workspace_id=VALID_WORKSPACE_ID):
    root_line = f"MINIME_ALLOWED_ROOT={root}" if root is not None else ""
    ws_url_line = f"MINIME_BACKEND_WS_URL={ws_url}" if ws_url is not None else ""
    workspace_id_line = (
        f"MINIME_WORKSPACE_ID={workspace_id}" if workspace_id is not None else ""
    )
    path.write_text(
        f"MINIME_PAIRING_TOKEN={token}\n{root_line}\n"
        f"{ws_url_line}\n{workspace_id_line}\n"
    )


def test_load_config_succeeds_with_valid_env(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    env_file = tmp_path / ".env"
    _write_env(env_file, root=str(project_root))

    config = load_config(env_path=env_file)

    assert config.pairing_token == VALID_TOKEN
    assert config.allowed_root == project_root.resolve()
    assert config.backend_ws_url == VALID_WS_URL
    assert config.workspace_id == VALID_WORKSPACE_ID


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


# --- Part 2: MINIME_BACKEND_WS_URL / MINIME_WORKSPACE_ID -------------------

def test_load_config_missing_ws_url_raises(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    env_file = tmp_path / ".env"
    _write_env(env_file, root=str(project_root), ws_url="")

    with pytest.raises(ConfigError, match="MINIME_BACKEND_WS_URL is not set"):
        load_config(env_path=env_file)


def test_load_config_bad_ws_url_scheme_raises(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    env_file = tmp_path / ".env"
    _write_env(env_file, root=str(project_root), ws_url="http://localhost:8000")

    with pytest.raises(ConfigError, match="must start with ws:// or wss://"):
        load_config(env_path=env_file)


def test_load_config_accepts_wss_url(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    env_file = tmp_path / ".env"
    _write_env(env_file, root=str(project_root), ws_url="wss://example.com")

    config = load_config(env_path=env_file)

    assert config.backend_ws_url == "wss://example.com"


def test_load_config_missing_workspace_id_raises(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    env_file = tmp_path / ".env"
    _write_env(env_file, root=str(project_root), workspace_id="")

    with pytest.raises(ConfigError, match="MINIME_WORKSPACE_ID is not set"):
        load_config(env_path=env_file)

