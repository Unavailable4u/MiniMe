"""
m2c/tests/test_config_server_url.py -- Patch C03: the M2C_SERVER_URL setting.

The address is where the device credential gets sent, so it is a security
setting and fails closed: TLS only (cleartext is allowed for loopback hosts so
a local dev backend works), no credentials embedded in the URL, and a bad
value is an error rather than a silent fallback.
"""
from __future__ import annotations

import pytest

from m2c import config
from m2c.config import ServerUrlError


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://api.example.test", "https://api.example.test"),
        ("https://api.example.test/", "https://api.example.test"),
        ("  https://api.example.test  ", "https://api.example.test"),
        ("HTTPS://API.Example.TEST", "https://api.example.test"),
        ("https://api.example.test:8443", "https://api.example.test:8443"),
        ("https://api.example.test/m2c/", "https://api.example.test/m2c"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://LOCALHOST:8000/", "http://localhost:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://127.0.0.5:8000", "http://127.0.0.5:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("https://localhost", "https://localhost"),
    ],
)
def test_good_addresses_are_normalised(raw, expected):
    assert config.normalize_server_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "api.example.test",
        "//api.example.test",
        "ftp://api.example.test",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://api.example.test",  # cleartext to a real host
        "http://192.168.1.5:8000",  # private, but not loopback
        "http://0.0.0.0:8000",
        "http://localhost.evil.test",  # looks local, is not
        "http://127.0.0.1.evil.test",
        "https://",
        "https://user@api.example.test",
        "https://user:hunter2@api.example.test",
        "https://api.example.test?x=1",
        "https://api.example.test#frag",
        "https://api.example.test:notaport",
        "https://api.example.test:99999",
        "https://api.exam ple.test",
        "https://api.example.test/a\nb",  # embedded newline (a trailing one is trimmed, by design)
        "https://api.example.test/\x1b[2J",
    ],
)
def test_bad_addresses_are_refused(raw):
    with pytest.raises(ServerUrlError):
        config.normalize_server_url(raw)


def test_non_strings_are_refused():
    with pytest.raises(ServerUrlError):
        config.normalize_server_url(None)  # type: ignore[arg-type]


def test_an_error_never_echoes_the_value_it_rejected():
    """A URL can carry a password; the message must not repeat it."""
    with pytest.raises(ServerUrlError) as info:
        config.normalize_server_url("https://user:hunter2@api.example.test")
    assert "hunter2" not in str(info.value)
    assert "user" not in str(info.value).replace("username", "")
    assert config.ENV_SERVER_URL in str(info.value)


def test_unset_or_blank_means_no_server_configured():
    assert config.server_url_from_env({}) is None
    assert config.server_url_from_env({"M2C_SERVER_URL": ""}) is None
    assert config.server_url_from_env({"M2C_SERVER_URL": "   "}) is None


def test_the_variable_is_read_and_normalised():
    assert config.server_url_from_env({"M2C_SERVER_URL": "https://API.example.test/"}) == "https://api.example.test"


def test_a_bad_value_is_an_error_not_a_fallback():
    with pytest.raises(ServerUrlError):
        config.server_url_from_env({"M2C_SERVER_URL": "http://api.example.test"})


def test_load_config_carries_the_server_url(tmp_path):
    cfg = config.load_config({"M2C_HOME": str(tmp_path / "h"), "M2C_SERVER_URL": "https://api.example.test"})
    assert cfg.server_url == "https://api.example.test"


def test_load_config_without_a_server_url_still_works(tmp_path):
    """doctor and --version must not start failing because login is unconfigured."""
    assert config.load_config({"M2C_HOME": str(tmp_path / "h")}).server_url is None


def test_the_environment_variable_name_is_stable():
    assert config.ENV_SERVER_URL == "M2C_SERVER_URL"
