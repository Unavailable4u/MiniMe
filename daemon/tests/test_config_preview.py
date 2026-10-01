"""
daemon/tests/test_config_preview.py — W7.4: the optional preview-proxy
settings in daemon/.env (MINIME_PREVIEW_TARGET / MINIME_FRONTEND_ORIGIN /
MINIME_PREVIEW_PORT) as load_config() sees them.

Kept apart from test_config.py on purpose: that file's fixture predates
W7.4, and "no preview settings at all" (what every one of its tests
writes) is itself one of the cases proven here.

Run: pytest daemon/tests/test_config_preview.py -v
"""
import os

import pytest

from daemon.config import ConfigError, load_config

_PREVIEW_VARS = ("MINIME_PREVIEW_TARGET", "MINIME_FRONTEND_ORIGIN", "MINIME_PREVIEW_PORT")


@pytest.fixture(autouse=True)
def _isolate_preview_env():
    # load_config() uses load_dotenv(override=True), which writes into the
    # real os.environ and outlives the test -- so clear these before and
    # after, or one test's preview settings leak into the next config load
    # (including test_config.py's, which expects none).
    for name in _PREVIEW_VARS:
        os.environ.pop(name, None)
    yield
    for name in _PREVIEW_VARS:
        os.environ.pop(name, None)


def _env(tmp_path, extra=""):
    root = tmp_path / "project"
    root.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MINIME_PAIRING_TOKEN=" + "a" * 32 + "\n"
        f"MINIME_ALLOWED_ROOT={root}\n"
        "MINIME_BACKEND_WS_URL=ws://localhost:8000\n"
        "MINIME_WORKSPACE_ID=test-workspace\n" + extra
    )
    return env_file


def test_preview_is_off_when_not_configured(tmp_path):
    assert load_config(env_path=_env(tmp_path)).preview is None


def test_preview_is_configured_from_env(tmp_path):
    config = load_config(
        env_path=_env(
            tmp_path,
            "MINIME_PREVIEW_TARGET=http://localhost:5173\n"
            "MINIME_FRONTEND_ORIGIN=http://localhost:3000\n"
            "MINIME_PREVIEW_PORT=5200\n",
        )
    )
    assert config.preview is not None
    assert config.preview.target_origin == "http://localhost:5173"
    assert config.preview.frontend_origin == "http://localhost:3000"
    assert config.preview.port == 5200


def test_preview_port_defaults_to_the_stable_default(tmp_path):
    config = load_config(
        env_path=_env(
            tmp_path,
            "MINIME_PREVIEW_TARGET=localhost:5173\nMINIME_FRONTEND_ORIGIN=http://localhost:3000\n",
        )
    )
    assert config.preview.port == 5199


def test_target_without_frontend_origin_fails_at_startup(tmp_path):
    env_file = _env(tmp_path, "MINIME_PREVIEW_TARGET=http://localhost:5173\n")
    with pytest.raises(ConfigError, match="MINIME_FRONTEND_ORIGIN is not set"):
        load_config(env_path=env_file)


def test_a_non_local_target_fails_at_startup(tmp_path):
    env_file = _env(
        tmp_path,
        "MINIME_PREVIEW_TARGET=http://example.com:3000\nMINIME_FRONTEND_ORIGIN=http://localhost:3000\n",
    )
    with pytest.raises(ConfigError, match="local address"):
        load_config(env_path=env_file)


def test_a_bad_preview_port_fails_at_startup(tmp_path):
    env_file = _env(
        tmp_path,
        "MINIME_PREVIEW_TARGET=http://localhost:5173\n"
        "MINIME_FRONTEND_ORIGIN=http://localhost:3000\n"
        "MINIME_PREVIEW_PORT=nope\n",
    )
    with pytest.raises(ConfigError, match="MINIME_PREVIEW_PORT"):
        load_config(env_path=env_file)


def test_the_blank_lines_in_env_example_mean_off(tmp_path):
    # daemon/.env.example ships these three keys empty; copying it as-is
    # (the documented setup step) must not turn the proxy on or fail.
    env_file = _env(
        tmp_path,
        "MINIME_PREVIEW_TARGET=\nMINIME_FRONTEND_ORIGIN=\nMINIME_PREVIEW_PORT=\n",
    )
    assert load_config(env_path=env_file).preview is None
