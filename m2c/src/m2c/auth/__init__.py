"""
m2c/src/m2c/auth/__init__.py -- Patch C03: signing in, and the credential.

    login.py         ``m2c login`` (the device-code flow), ``m2c logout`` and
                     ``get_access_token()``
    credentials.py   the stored device credential (OS keyring or a 0600 file),
                     the redacting ``Secret``/``Credential`` types and every
                     exception this package raises

What other work packages use:

* C04 (device link) calls ``load_credential()`` and sends the credential in the
  socket's ``hello`` message (P00 authenticates the socket with the credential
  itself; there is no JWT on it).
* Later HTTP calls call ``get_access_token()``.  Nothing in Wave 2 or 3 needs it
  yet.

Importing this package imports neither ``keyring`` nor anything outside the
standard library; the keyring is loaded lazily, only when a credential is
saved, read or deleted, and is never required.

``login`` the function is deliberately NOT re-exported here: ``m2c.auth.login``
is the module, and a function of the same name would shadow it.  Call
``m2c.auth.login.login(...)``.

Place this file at: m2c/src/m2c/auth/__init__.py
"""
from . import credentials, login
from .credentials import (
    AuthError,
    AuthNetworkError,
    AuthProtocolError,
    AuthServerError,
    Credential,
    CredentialStoreError,
    DeviceRevokedError,
    InsecureCredentialFileError,
    LoginDeniedError,
    LoginExpiredError,
    NotLoggedInError,
    Secret,
    ServerMismatchError,
    delete_credential,
    load_credential,
    save_credential,
)
from .login import get_access_token, logout

__all__ = [
    "AuthError",
    "AuthNetworkError",
    "AuthProtocolError",
    "AuthServerError",
    "Credential",
    "CredentialStoreError",
    "DeviceRevokedError",
    "InsecureCredentialFileError",
    "LoginDeniedError",
    "LoginExpiredError",
    "NotLoggedInError",
    "Secret",
    "ServerMismatchError",
    "credentials",
    "delete_credential",
    "get_access_token",
    "load_credential",
    "login",
    "logout",
    "save_credential",
]
