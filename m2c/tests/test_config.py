"""
m2c/tests/test_config.py -- Patch C01: environment-driven settings.
"""
from __future__ import annotations

import logging

import pytest

from m2c import config
from m2c.paths import ConfigDirError


def test_defaults(monkeypatch, tmp_path):
    cfg = config.load_config({})
    assert cfg.log_level == logging.WARNING
    assert cfg.home.name == ".m2c"


def test_home_follows_m2c_home(tmp_path):
    cfg = config.load_config({"M2C_HOME": str(tmp_path / "h")})
    assert cfg.home == tmp_path / "h"


def test_bad_home_raises_config_dir_error():
    with pytest.raises(ConfigDirError):
        config.load_config({"M2C_HOME": "not/absolute"})


@pytest.mark.parametrize(
    "value, expected",
    [
        ("DEBUG", logging.DEBUG),
        ("debug", logging.DEBUG),
        (" Info ", logging.INFO),
        ("WARNING", logging.WARNING),
        ("error", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
    ],
)
def test_log_level_names(value, expected):
    assert config.log_level_from_env({"M2C_LOG_LEVEL": value}) == expected


@pytest.mark.parametrize("value", ["", "verbose", "10", "NOTSET", "💥"])
def test_unknown_log_level_falls_back_to_default(value):
    """Logging configuration fails open: a typo must never stop the CLI."""
    assert config.log_level_from_env({"M2C_LOG_LEVEL": value}) == logging.WARNING
