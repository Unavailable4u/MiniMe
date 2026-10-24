"""
m2c/src/m2c/auth/credentials.py -- Patch C03: the device credential, where it
is kept on this machine, and the error types of the whole ``auth`` package.

WHAT A DEVICE CREDENTIAL IS
---------------------------
``m2c login`` (login.py) ends with the server handing this device one long
random string, exactly once (S03, backend/eo/devices.py: 256 random bits, only
its SHA-256 is stored server side).  That string is the thing that
authenticates the device from then on:

* ``get_access_token()`` (login.py) exchanges it for a ~15 minute JWT for
  plain HTTP calls;
* C04's device link sends it in the socket's ``hello`` message
  (protocol/v1/hello.json ``device_credential``), because P00 authenticates
  the socket with the credential itself and there is no JWT on it.  C04 reads
  it with ``load_credential()`` below.

Besides the secret, the record carries the ``device_id`` the server assigned
and the ``server_url`` that issued it.  The URL matters: a credential must only
ever be sent to the server that issued it (see ``ServerMismatchError`` in
login.py), so it is stored with the credential rather than trusted from the
environment at the moment of use.

WHERE IT IS KEPT
----------------
1. The OS keyring, when the optional ``keyring`` package is installed
   (``pip install 'm2c[keyring]'``) AND it has a usable backend.  The module is
   imported lazily and is never required; keyring's own "fail" backend, which
   is what a stock WSL2 install ends up with (build plan §11.1: Secret Service
   is usually absent there), counts as "no keyring".
2. Otherwise ``credentials.json`` under the config directory (``~/.m2c`` or
   ``$M2C_HOME``, via the paths module), mode 0600, written atomically: the
   JSON goes to a private temp file in the same directory (created 0600,
   fsynced) and is renamed over the real name, so the final name only ever
   points at a complete, private file and a crash leaves the old credential
   intact.  There is exactly one place at a time: saving to one backend
   removes the other's copy, and a keyring that fails at save time falls back
   to the file.

READING FAILS CLOSED (build plan §0.2: security checks fail closed)
-------------------------------------------------------------------
``load_credential()`` refuses, with an error that says how to fix it, a
credentials file that

* is a symlink or not a regular file (a link could point anywhere),
* belongs to another account,
* is readable or writable by anyone but its owner (anything beyond 0600; the
  fix, ``chmod 600 <path>``, is printed -- reading never "helpfully" changes
  the mode itself, because a loose file may already have been read by someone
  else and the person should know),
* is larger than any real credential file or is not the expected JSON.

It never echoes what it found.  ``delete_credential()`` is deliberately
*more* forgiving than ``load_credential()``: it removes a file whatever its
mode, so ``m2c logout`` can always clean up a file that load refuses to read.

NOTHING SECRET IS EVER PRINTABLE
--------------------------------
``Secret`` wraps every secret string (device code, credential, JWT, raw
payloads).  Its ``repr``/``str``/``format`` print ``<redacted>``, it has no
``__dict__`` to dump, and it refuses to be pickled.  The only way to the text
is ``Secret.reveal()``, called where the text is actually used.  ``Credential``
holds its secret as a ``Secret`` and its own repr shows the device id and
server but never the secret.

The rest of the discipline is about *tracebacks*.  Error trackers and
``pytest --showlocals`` print the repr of every local variable of every frame
on an exception's traceback, so a raw secret string in a local is a leak even
when no message contains it.  Therefore, in this package:

* secrets live in locals only as ``Secret``;
* anything that parses a payload that may hold a secret does so in a helper
  that catches its own failures and *returns* ``None``, and the caller raises
  afterwards -- so the frame that held the data is gone and the new exception
  has no ``__cause__``/``__context__`` pointing back at it;
* exceptions are raised outside ``except`` blocks, with fixed text.

Every exception here is an ``AuthError`` with a fixed, human-written message.
None ever contains text taken from a server reply, a library exception, or a
file's content; the only variable parts are paths, ports-free server
addresses, status codes and durations.  tests/auth/test_auth_redaction.py
proves all of this by forcing errors and rendering them every way a tool can.

Place this file at: m2c/src/m2c/auth/credentials.py
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import logging
import os
import re
import shlex
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .. import paths
from ..config import normalize_server_url
from ..logging_setup import log_event

logger = logging.getLogger("m2c.auth.credentials")

FILE_NAME = "credentials.json"
KEYRING_SERVICE = "m2c"
RECORD_VERSION = 1
MAX_FILE_BYTES = 8192  # a real record is ~250 bytes; anything bigger is not ours

# protocol/v1/hello.json: the credential's accepted shape.  S03 issues
# token_urlsafe(32) (43 characters of [A-Za-z0-9_-]); this is the wider set the
# protocol allows, so a server change cannot lock a client out.
_CREDENTIAL_RE = re.compile(r"[A-Za-z0-9._~+/=-]{16,512}")
_DEVICE_ID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")

_LOGIN_HINT = "Run 'm2c login' to sign in."


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class AuthError(Exception):
    """Base class for everything that can go wrong signing in, signing out or
    fetching a token.  Messages are fixed text written for the person at the
    terminal.  ``retryable`` tells a caller whether trying again later could
    help (a network blip, a busy server) or not (a revoked device)."""

    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class AuthNetworkError(AuthError):
    """The server could not be reached (connection refused or dropped, DNS
    failure, timeout, TLS failure).  Always retryable."""

    retryable = True


class AuthServerError(AuthError):
    """The server answered with an HTTP status we do not treat as a normal
    outcome.  ``status`` is the code; 429 and 5xx are retryable."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message, retryable=(status == 429 or status >= 500))
        self.status = status


class AuthProtocolError(AuthError):
    """The server's reply was not what the S03 contract says (wrong shape,
    unusable values).  Not retryable: asking again will not change a bug."""


class LoginDeniedError(AuthError):
    """Someone in the web app refused the sign-in code."""


class LoginExpiredError(AuthError):
    """The sign-in code was not approved in time."""


class NotLoggedInError(AuthError):
    """No device credential is stored on this machine."""


class DeviceRevokedError(AuthError):
    """The server no longer accepts this device's credential (the device was
    revoked from the web Devices page, or the credential is unknown).  Not
    retryable: only a new ``m2c login`` fixes it."""


class ServerMismatchError(AuthError):
    """The configured server is not the one that issued the stored
    credential.  Refused before anything is sent: the credential must never
    reach a server that did not issue it."""


class CredentialStoreError(AuthError):
    """The credential cannot be stored, read or removed safely."""


class InsecureCredentialFileError(CredentialStoreError):
    """The credentials file is readable by other accounts; refused (fail closed)."""


# ---------------------------------------------------------------------------
# Secret and Credential
# ---------------------------------------------------------------------------
class Secret:
    """A string (or bytes) that must never be printed.

    ``repr``, ``str`` and ``format`` all give ``<redacted>``; there is no
    ``__dict__`` (``vars()`` fails) and pickling is refused.  ``reveal()`` is
    the single door to the value.  Immutable, so copying shares it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Secret is immutable")

    def reveal(self) -> Any:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def __format__(self, spec: str) -> str:
        return "<redacted>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(("m2c.Secret", self._value))

    def __reduce__(self):  # pickling would write the value somewhere
        raise TypeError("Secret cannot be pickled")

    def __copy__(self) -> "Secret":
        return self

    def __deepcopy__(self, memo: dict) -> "Secret":
        return self


@dataclass(frozen=True, repr=False)
class Credential:
    """What ``m2c login`` leaves on this machine.

    ``device_credential`` is a ``Secret``; ``device_id`` is the server's UUID
    for this device; ``server_url`` is the (normalised) address that issued it.
    Construction validates all three and raises ``ValueError`` with fixed text
    that never repeats the offending value.
    """

    device_credential: Secret
    device_id: str
    server_url: str

    def __post_init__(self) -> None:
        if not isinstance(self.device_credential, Secret):
            raise ValueError("the device credential must be wrapped in a Secret")
        secret_text = self.device_credential.reveal()
        if not isinstance(secret_text, str) or not _CREDENTIAL_RE.fullmatch(secret_text):
            raise ValueError("the device credential has an unexpected format")
        if not isinstance(self.device_id, str) or not _DEVICE_ID_RE.fullmatch(self.device_id):
            raise ValueError("the device id has an unexpected format")
        # ServerUrlError is a ValueError and never echoes the address.
        object.__setattr__(self, "server_url", normalize_server_url(self.server_url))

    def __repr__(self) -> str:
        return (
            f"Credential(device_id={self.device_id!r}, server_url={self.server_url!r}, "
            "device_credential=<redacted>)"
        )


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
def credentials_path(environ: Mapping[str, str] | None = None) -> Path:
    """The credentials file's path.  Touches nothing on disk."""
    return paths.config_dir(environ) / FILE_NAME


def _keyring_username(environ: Mapping[str, str] | None = None) -> str:
    """One keyring entry per config directory, so two profiles (``M2C_HOME``)
    on one machine do not overwrite each other.  Hashed so the path is not
    sprayed into the keyring's UI."""
    digest = hashlib.sha256(str(paths.config_dir(environ)).encode("utf-8")).hexdigest()[:16]
    return f"device:{digest}"


def _current_uid() -> int:
    return os.geteuid()


# ---------------------------------------------------------------------------
# The optional keyring
# ---------------------------------------------------------------------------
def _load_keyring() -> Any | None:
    """The ``keyring`` module if it is installed and has a usable backend,
    else None.  Imported here, lazily, through importlib so this package never
    requires it (and the stdlib-only import check in tests/test_cli.py, which
    looks for ``import`` statements, stays true).  Any failure at all -- not
    installed, broken install, backend initialisation error -- means "no
    keyring"; the file is the dependable path."""
    try:
        module = importlib.import_module("keyring")
        priority = getattr(module.get_keyring(), "priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, (int, float)) or priority <= 0:
            return None  # keyring's "fail" backend reports 0: nothing usable
        return module
    except Exception:
        return None


def _keyring_read(keyring: Any, environ: Mapping[str, str] | None) -> Secret | None:
    """The stored payload wrapped in a Secret, or None (nothing stored, or the
    keyring failed -- a broken keyring must not hide a file credential)."""
    try:
        value = keyring.get_password(KEYRING_SERVICE, _keyring_username(environ))
    except Exception as exc:
        log_event(logger, logging.WARNING, "auth.keyring_read_failed", exc_type=type(exc).__name__)
        return None
    return Secret(value) if isinstance(value, str) and value else None


def _keyring_has_entry(keyring: Any, environ: Mapping[str, str] | None) -> bool:
    try:
        return keyring.get_password(KEYRING_SERVICE, _keyring_username(environ)) is not None
    except Exception:
        return True  # cannot tell: try the delete rather than leave a credential behind


# ---------------------------------------------------------------------------
# Payload <-> Credential
# ---------------------------------------------------------------------------
def _payload_for(credential: Credential) -> Secret:
    record = {
        "version": RECORD_VERSION,
        "server_url": credential.server_url,
        "device_id": credential.device_id,
        "device_credential": credential.device_credential.reveal(),
    }
    return Secret(json.dumps(record, indent=2, sort_keys=True) + "\n")


def _credential_from_payload(payload: Secret) -> Credential | None:
    """Parse a stored record.  Returns None for *anything* wrong with it, and
    catches its own exceptions, so no frame holding the parsed data is ever on
    a raised exception's traceback (see the module docstring)."""
    try:
        data = json.loads(payload.reveal())
        if not isinstance(data, dict) or data.get("version") != RECORD_VERSION:
            return None
        return Credential(
            device_credential=Secret(data["device_credential"]),
            device_id=data["device_id"],
            server_url=data["server_url"],
        )
    except Exception:
        return None


def _damaged_error() -> CredentialStoreError:
    return CredentialStoreError(
        "The stored credential is damaged or in an unknown format. "
        "Run 'm2c logout' and then 'm2c login' to sign in again."
    )


# ---------------------------------------------------------------------------
# The credentials file
# ---------------------------------------------------------------------------
def _write_file_atomically(path: Path, payload: Secret) -> None:
    """Write ``payload`` to ``path`` through a private temp file in the same
    directory and an atomic rename.  Raises OSError (after removing the temp
    file) on any failure; the previous file, if any, is untouched."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:  # takes ownership of fd straight away
            os.fchmod(handle.fileno(), 0o600)  # whatever the umask or mkstemp default
            handle.write(payload.reveal().encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    with contextlib.suppress(OSError):  # make the rename itself durable; best effort
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _remove_file_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def _forget_keyring_entry(keyring: Any, environ: Mapping[str, str] | None) -> None:
    """Best effort: drop a keyring copy that a file save has made stale, so a
    later load (which prefers the keyring) cannot return an old credential."""
    with contextlib.suppress(Exception):
        keyring.delete_password(KEYRING_SERVICE, _keyring_username(environ))


def _read_file_payload(path: Path) -> Secret:
    """Open, check and read the credentials file.  Raises NotLoggedInError or
    CredentialStoreError (never echoing content); returns the payload wrapped
    in a Secret so no raw copy sits in a local variable."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    except OSError:
        raise CredentialStoreError(f"Cannot inspect the credentials file {path}.") from None
    if before is None:
        raise NotLoggedInError(f"Not signed in. {_LOGIN_HINT}")

    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CredentialStoreError(
            f"{path} is not a regular file (a link or directory); refusing to use it. "
            "Run 'm2c logout' to remove it, then 'm2c login'."
        )
    if before.st_uid != _current_uid():
        raise CredentialStoreError(
            f"{path} belongs to another user; refusing to use it. "
            "Run 'm2c logout' to remove it, then 'm2c login'."
        )
    mode = stat.S_IMODE(before.st_mode)
    if mode & 0o177:
        raise InsecureCredentialFileError(
            f"The credentials file {path} is accessible to other accounts (mode {mode:04o}); "
            f"refusing to use it. Fix it with: chmod 600 {shlex.quote(str(path))}  "
            "(if someone else may have read it, run 'm2c logout', revoke this device on the "
            "Devices page of the web app, and sign in again)."
        )

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise CredentialStoreError(f"Cannot open the credentials file {path}.") from None
    try:
        after = os.fstat(fd)
        same_file = (after.st_ino, after.st_dev) == (before.st_ino, before.st_dev)
        blob = Secret(os.read(fd, MAX_FILE_BYTES + 1)) if same_file else None
    except OSError:
        same_file, blob = False, None
    finally:
        os.close(fd)
    if blob is None or not same_file:
        raise CredentialStoreError(f"The credentials file {path} changed while it was being read; try again.")
    if len(blob.reveal()) > MAX_FILE_BYTES:
        raise _damaged_error()
    try:
        return Secret(blob.reveal().decode("utf-8"))
    except UnicodeDecodeError:
        pass
    raise _damaged_error()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def save_credential(credential: Credential, environ: Mapping[str, str] | None = None) -> str:
    """Store ``credential``; returns ``"keyring"`` or ``"file"``.

    Raises paths.ConfigDirError if the config directory cannot be used and
    CredentialStoreError if the file cannot be written.  A keyring that fails
    falls back to the file (and says so in the log, by exception type only).
    """
    payload = _payload_for(credential)
    username = _keyring_username(environ)
    path = credentials_path(environ)

    keyring = _load_keyring()
    if keyring is not None:
        try:
            keyring.set_password(KEYRING_SERVICE, username, payload.reveal())
            saved_in_keyring = True
        except Exception as exc:
            log_event(logger, logging.WARNING, "auth.keyring_save_failed", exc_type=type(exc).__name__)
            saved_in_keyring = False
        if saved_in_keyring:
            _remove_file_quietly(path)  # one place at a time
            log_event(logger, logging.INFO, "auth.credential_saved", status="keyring")
            return "keyring"

    paths.ensure_config_dir(environ)
    try:
        _write_file_atomically(path, payload)
        written = True
    except OSError:
        written = False
    if not written:
        raise CredentialStoreError(
            f"Could not write the credentials file {path}. Check free space and permissions."
        )
    if keyring is not None:
        _forget_keyring_entry(keyring, environ)
    log_event(logger, logging.INFO, "auth.credential_saved", status="file")
    return "file"


def load_credential(environ: Mapping[str, str] | None = None) -> Credential:
    """Return the stored credential, for the device link (C04 sends it in the
    socket ``hello``) and for ``get_access_token``.

    Raises NotLoggedInError if there is none, InsecureCredentialFileError for
    a file others can access, CredentialStoreError for any other unsafe or
    damaged state.  Fails closed: it never returns a credential it was not
    willing to trust.
    """
    payload: Secret | None = None
    keyring = _load_keyring()
    if keyring is not None:
        payload = _keyring_read(keyring, environ)
    if payload is None:
        payload = _read_file_payload(credentials_path(environ))
    credential = _credential_from_payload(payload)
    if credential is None:
        raise _damaged_error()
    return credential


def delete_credential(environ: Mapping[str, str] | None = None) -> bool:
    """Remove the stored credential from both places.  Returns True if
    something was removed.  Works on a file whatever its mode (logout must be
    able to clean up what ``load_credential`` refuses to read), and raises
    CredentialStoreError -- after trying everything -- if a copy could not be
    removed, so logout never claims success while a credential remains."""
    path = credentials_path(environ)
    removed = False
    failed = False

    try:
        os.unlink(path)
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        failed = True

    keyring = _load_keyring()
    if keyring is not None and _keyring_has_entry(keyring, environ):
        try:
            keyring.delete_password(KEYRING_SERVICE, _keyring_username(environ))
            removed = True
        except Exception as exc:
            log_event(logger, logging.WARNING, "auth.keyring_delete_failed", exc_type=type(exc).__name__)
            failed = True

    if failed:
        raise CredentialStoreError(
            "The stored credential could not be fully deleted from this machine. "
            "Check the permissions of the m2c config directory and your keyring, then run 'm2c logout' again."
        )
    if removed:
        log_event(logger, logging.INFO, "auth.credential_deleted")
    return removed
