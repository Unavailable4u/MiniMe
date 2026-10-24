"""
m2c/tests/auth/auth_fixtures.py -- Patch C03: fixtures for the login tests.

Why this is not a conftest.py: tests/test_doctor.py does ``from conftest import
...``, which resolves by module name, and a second conftest.py in tests/auth
would shadow the real one.  So these fixtures live in an ordinary module that
each test file under tests/auth imports explicitly (``from auth_fixtures import
*``).  That also keeps the autouse fixtures below scoped to the auth tests; a
plugin or a nested conftest would apply them to every test in the suite.

The parent conftest already points M2C_HOME and HOME at a temp directory for
every test.  This module adds, for the auth tests only:

* ``_no_real_keyring`` (autouse): the OS keyring is replaced by "not
  available" so no test can read or write a developer's real keychain, even
  on a machine where the optional ``keyring`` package is installed.  Tests
  that want a keyring ask for ``fake_keyring``.
* ``_fresh_token_cache`` (autouse): the in-memory JWT cache is module state,
  so it is emptied around every test.
* ``server``: a started ``MockServer``; the test fails if the client made a
  call nobody scripted.
* ``fake_time``: a ``FakeTime``.
* ``m2c_logs``: caplog, attached directly to the ``m2c`` logger at DEBUG.
  ``cli.main`` turns propagation off on that logger, so plain caplog would
  silently see nothing; attaching to the logger itself also means the tests
  read the RAW records (no ContentFreeFilter in the way), which is stricter
  than what a user's terminal would show.
* ``fake_keyring``: a stand-in for the ``keyring`` module, with switches to
  make each operation fail.
"""
from __future__ import annotations

import logging

import pytest

from mock_server import FakeKeyring, FakeTime, MockServer


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch):
    from m2c.auth import credentials

    monkeypatch.setattr(credentials, "_load_keyring", lambda: None)


@pytest.fixture(autouse=True)
def _loopback_is_never_proxied(monkeypatch):
    """urllib honours http_proxy/https_proxy; CI sandboxes and corporate
    machines set them, and the mock server lives on 127.0.0.1."""
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    from m2c.auth import login

    login.reset_token_cache()
    yield
    login.reset_token_cache()


@pytest.fixture
def server():
    srv = MockServer().start()
    try:
        yield srv
    finally:
        srv.stop()
    assert srv.unscripted == [], f"client made unscripted calls: {srv.unscripted}"


@pytest.fixture
def fake_time():
    return FakeTime()


@pytest.fixture
def m2c_logs(caplog):
    logger = logging.getLogger("m2c")
    saved = (logger.level, logger.propagate)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="m2c")
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]


@pytest.fixture
def fake_keyring(monkeypatch):
    from m2c.auth import credentials

    kr = FakeKeyring()
    monkeypatch.setattr(credentials, "_load_keyring", lambda: kr)
    return kr


__all__ = [
    "_fresh_token_cache",
    "_loopback_is_never_proxied",
    "_no_real_keyring",
    "fake_keyring",
    "fake_time",
    "m2c_logs",
    "server",
]
