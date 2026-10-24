"""
m2c/src/m2c/config.py -- Patch C01: settings that come from the environment.

Deliberately thin.  The client has three knobs so far, all environment
variables so nothing about them is written to disk:

    M2C_HOME        relocates the config directory (documented in paths.py)
    M2C_LOG_LEVEL   DEBUG | INFO | WARNING | ERROR | CRITICAL (default WARNING)
    M2C_SERVER_URL  where the MiniMe backend lives (added by Patch C03; C01
                    had no server setting).  There is no default: the public
                    address is not decided yet, so ``m2c login`` asks for it.
                    After a login the address is stored with the credential,
                    so later commands do not need the variable.

Later work packages (the store in C02, login in C03, the device link in
C04) read their settings through ``Config`` rather than reaching into
``os.environ`` themselves, so tests can pass a plain dict.

Failure policy follows the repo-wide rule: a bad *log level* falls back to
the default (logging fails open and must never stop the CLI), while a bad
*M2C_HOME* raises ``paths.ConfigDirError`` (it decides where credentials
are stored, so it fails closed).  A bad *M2C_SERVER_URL* fails closed too
(``ServerUrlError``): it decides where the device credential is sent.

M2C_SERVER_URL rules, enforced by ``normalize_server_url``:

* ``https`` only.  Cleartext ``http`` is accepted for loopback hosts
  (``localhost``, ``127.0.0.0/8``, ``::1``) so a backend on the developer's
  own machine works; anything else is refused, because the credential and
  the pairing codes would cross the network in the clear.
* No user name or password in the URL, no query string, no fragment (a URL
  with a password in it is exactly the kind of string that ends up in shell
  history and bug reports).
* A path prefix is allowed (``https://host/api``); a trailing slash and the
  case of the scheme and host are normalised away, so the same server always
  compares equal to itself.
* Errors never repeat the rejected value, for the same reason.

Place this file at: m2c/src/m2c/config.py
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import paths

ENV_LOG_LEVEL = "M2C_LOG_LEVEL"
ENV_SERVER_URL = "M2C_SERVER_URL"
DEFAULT_LOG_LEVEL = logging.WARNING

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class ServerUrlError(ValueError):
    """The server address is missing a required part or is not safe to use.

    The message names the setting and the rule, never the rejected value.
    """


@dataclass(frozen=True)
class Config:
    home: Path
    log_level: int
    server_url: str | None = None


def log_level_from_env(environ: Mapping[str, str] | None = None) -> int:
    """Parse M2C_LOG_LEVEL; anything unrecognised means the default."""
    env = os.environ if environ is None else environ
    return _LEVELS.get(env.get(ENV_LOG_LEVEL, "").strip().upper(), DEFAULT_LOG_LEVEL)


_PLAIN_URL_RE = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no spaces or controls
_MAX_URL_LENGTH = 512


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_server_url(raw: str) -> str:
    """Validate a server address and return it in canonical form.

    Raises ServerUrlError (never echoing ``raw``) unless it is an ``https``
    URL (or ``http`` to a loopback host) with a host, an optional port and
    path, and no credentials, query or fragment.
    """
    if not isinstance(raw, str):
        raise ServerUrlError(f"{ENV_SERVER_URL} must be text")
    text = raw.strip()
    if not text or len(text) > _MAX_URL_LENGTH or not _PLAIN_URL_RE.match(text):
        raise ServerUrlError(
            f"{ENV_SERVER_URL} must be a plain address such as https://api.example.com"
        )
    try:
        parts = urlsplit(text)
        port = parts.port  # raises ValueError for a non-numeric or out-of-range port
        host = parts.hostname
    except ValueError:
        raise ServerUrlError(f"{ENV_SERVER_URL} has an invalid port or host") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        raise ServerUrlError(f"{ENV_SERVER_URL} must start with https:// and name a host")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ServerUrlError(f"{ENV_SERVER_URL} must not contain a username or password")
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise ServerUrlError(f"{ENV_SERVER_URL} must not contain a query string or fragment")
    if scheme == "http" and not _is_loopback_host(host):
        raise ServerUrlError(
            f"{ENV_SERVER_URL} must use https (plain http is only allowed for localhost)"
        )
    shown_host = f"[{host}]" if ":" in host else host  # IPv6 literals keep their brackets
    netloc = shown_host if port is None else f"{shown_host}:{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


def server_url_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    """The configured server address, or None when the variable is unset or
    blank.  A value that is set but unsafe raises ServerUrlError rather than
    quietly falling back: this is where a credential would be sent."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_SERVER_URL, "")
    if raw.strip() == "":
        return None
    return normalize_server_url(raw)


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Resolve settings.  Raises paths.ConfigDirError for a bad M2C_HOME and
    ServerUrlError for a bad M2C_SERVER_URL; an unset server address is fine
    (``server_url`` is None) so ``doctor`` and ``--version`` never need one."""
    return Config(
        home=paths.config_dir(environ),
        log_level=log_level_from_env(environ),
        server_url=server_url_from_env(environ),
    )
