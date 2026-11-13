"""
backend/tests/canary/conftest.py -- Patch Q06: the hermetic environment of the canary tests.

The canary session runs the REAL device client, which writes its store, its checkpoints and its logs under
``$M2C_HOME`` and reads ``$HOME``.  Both are pointed at a per-test temporary directory (the same thing m2c/tests/
conftest.py does for the client's own tests), so no test can read or create anything in the real ``~/.m2c``.

The backend's own tests/conftest.py (parent directory) still applies: it swaps ``memory.bus.redis`` for an in-memory
twin for every test, which the canary scan also looks at.
"""
import pytest


def pytest_report_header(config):
    """Say which mode ran, in the first lines of the output (README: "Run it")."""
    from tests.canary import harness
    if harness.MODE == "real":
        return ["canary mode: REAL DATABASE (MINIME_TEST_DATABASE_URL set; every table scanned on the scan connection)"]
    return ["canary mode: FAKE RECORDER (no MINIME_TEST_DATABASE_URL; every SQL statement and parameter is scanned)"]


@pytest.fixture(autouse=True)
def _hermetic_device_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("M2C_HOME", str(tmp_path / "m2c-home"))
    monkeypatch.delenv("M2C_LOG_LEVEL", raising=False)
    monkeypatch.delenv("M2C_SERVER_URL", raising=False)
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)             # the device talks to 127.0.0.1 and must never be proxied
    return home
