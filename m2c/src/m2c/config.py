"""
m2c/src/m2c/config.py -- Patch C01: settings that come from the environment.

Deliberately thin in C01.  The client has only two knobs so far, both
environment variables so nothing about them is written to disk:

    M2C_HOME        relocates the config directory (documented in paths.py)
    M2C_LOG_LEVEL   DEBUG | INFO | WARNING | ERROR | CRITICAL (default WARNING)

Later work packages (the store in C02, login in C03, the device link in
C04) read their settings through ``Config`` rather than reaching into
``os.environ`` themselves, so tests can pass a plain dict.

Failure policy follows the repo-wide rule: a bad *log level* falls back to
the default (logging fails open and must never stop the CLI), while a bad
*M2C_HOME* raises ``paths.ConfigDirError`` (it decides where credentials
are stored, so it fails closed).

Place this file at: m2c/src/m2c/config.py
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import paths

ENV_LOG_LEVEL = "M2C_LOG_LEVEL"
DEFAULT_LOG_LEVEL = logging.WARNING

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


@dataclass(frozen=True)
class Config:
    home: Path
    log_level: int


def log_level_from_env(environ: Mapping[str, str] | None = None) -> int:
    """Parse M2C_LOG_LEVEL; anything unrecognised means the default."""
    env = os.environ if environ is None else environ
    return _LEVELS.get(env.get(ENV_LOG_LEVEL, "").strip().upper(), DEFAULT_LOG_LEVEL)


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Resolve settings.  Raises paths.ConfigDirError for a bad M2C_HOME."""
    return Config(home=paths.config_dir(environ), log_level=log_level_from_env(environ))
