"""
m2c/src/m2c/paths.py -- Patch C01: where M2C keeps its files on this device.

Per build plan §O1 the device is the source of truth, so everything the
client stores (the SQLite state database from C02, the device credential
from C03, logs, checkpoints, memory ...) goes under one directory:

    ~/.m2c/                     the default
    $M2C_HOME                   overrides it, when set and non-empty

``M2C_HOME`` exists so that tests (and anyone running two profiles side by
side) never touch the real home directory.  Rules for the variable:

* It must be an absolute path (``~`` is expanded first).  A relative path is
  rejected rather than silently resolved against whatever the current
  directory happens to be, because that would scatter credentials around.
* Set but empty counts as unset, the usual shell convention.
* It names the directory itself, not its parent: ``M2C_HOME=/tmp/x`` means
  files land in ``/tmp/x/state.db``, not ``/tmp/x/.m2c/state.db``.
* Parent directories that do not exist are created with default
  permissions; only the final directory is forced to mode 0700.

The directory will hold a device credential, so this module is a *security*
check and fails closed (build plan §0.2).  ``ensure_config_dir`` raises
``ConfigDirError`` instead of carrying on when the directory

* exists but is not a directory,
* is owned by a different user (for example ``sudo m2c`` with a user's HOME),
* or cannot be put into mode 0700 -- typically because the filesystem
  ignores chmod, as drvfs under ``/mnt/c`` does.  Carrying on would leave a
  credentials directory readable by others without anyone noticing.

A pre-existing directory with looser permissions (say 0755) that the current
user owns is tightened to 0700; the explicit chmod also makes the result
independent of the process umask.

Place this file at: m2c/src/m2c/paths.py
"""
from __future__ import annotations

import contextlib
import logging
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .logging_setup import log_event

logger = logging.getLogger("m2c.paths")

ENV_HOME = "M2C_HOME"
DEFAULT_DIRNAME = ".m2c"
CONFIG_DIR_MODE = 0o700


class ConfigDirError(Exception):
    """The config directory cannot be located, created or trusted.

    Messages are written for the person at the terminal and may name paths;
    they never contain anything the user typed into a conversation.
    """


def config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the config directory path without touching the filesystem."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_HOME, "")
    if raw == "":
        try:
            return Path.home() / DEFAULT_DIRNAME
        except RuntimeError as exc:  # no HOME and no passwd entry
            raise ConfigDirError(
                f"cannot determine the home directory; set {ENV_HOME} to an absolute path"
            ) from exc
    try:
        expanded = Path(raw).expanduser()
    except RuntimeError as exc:
        raise ConfigDirError(f"cannot expand ~ in {ENV_HOME}={raw!r}") from exc
    if not expanded.is_absolute():
        raise ConfigDirError(f"{ENV_HOME} must be an absolute path (got {raw!r})")
    return Path(os.path.normpath(expanded))


def ensure_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Create the config directory with mode 0700 (or verify/tighten it).

    Idempotent.  Raises ConfigDirError on every condition listed in the
    module docstring.  Returns the directory path.
    """
    path = config_dir(environ)
    try:
        path.mkdir(mode=CONFIG_DIR_MODE, parents=True, exist_ok=True)
    except FileExistsError:
        # exist_ok only forgives an existing *directory*.
        raise ConfigDirError(f"{path} exists but is not a directory") from None
    except OSError as exc:
        raise ConfigDirError(f"cannot create {path} ({exc.strerror or type(exc).__name__})") from exc

    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:  # native Windows: doctor reports the OS first, this is the backstop
        raise ConfigDirError("POSIX file permissions are required to protect the config directory")
    try:
        info = path.stat()
    except OSError as exc:
        raise ConfigDirError(f"cannot inspect {path} ({exc.strerror or type(exc).__name__})") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigDirError(f"{path} exists but is not a directory")
    if info.st_uid != geteuid():
        raise ConfigDirError(f"{path} is owned by another user; refusing to use it")

    if stat.S_IMODE(info.st_mode) != CONFIG_DIR_MODE:
        try:
            os.chmod(path, CONFIG_DIR_MODE)
        except OSError as exc:
            raise ConfigDirError(
                f"cannot set mode 0700 on {path} ({exc.strerror or type(exc).__name__})"
            ) from exc
        if stat.S_IMODE(path.stat().st_mode) != CONFIG_DIR_MODE:
            raise ConfigDirError(
                f"cannot set mode 0700 on {path}: the filesystem ignores permission changes "
                "(this is typical under /mnt/c); choose a directory on the Linux filesystem"
            )
        log_event(logger, logging.INFO, "paths.config_dir_tightened")
    return path


def prepare_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """ensure_config_dir() plus a real write test.

    ``os.access`` is not used because it lies for root and on some network
    and drvfs mounts; instead a throwaway file is created and removed.
    """
    path = ensure_config_dir(environ)
    try:
        fd, probe = tempfile.mkstemp(dir=path, prefix=".write-probe-")
    except OSError as exc:
        raise ConfigDirError(f"{path} is not writable ({exc.strerror or type(exc).__name__})") from exc
    try:
        os.close(fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(probe)
    return path
