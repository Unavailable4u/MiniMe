"""S04: what the device socket does when it cannot do its job, and how it is mounted.

Deliberately NOT guarded by pytest.importorskip("jsonschema"): the socket validates
every message against protocol/v1 with jsonschema (loaded lazily, see codeagent/ws.py
"Runtime needs"), and the promise is that when that -- or the protocol/ directory,
which the backend image does not contain today -- is unavailable, the server still
starts, the rest of the app is unaffected, and the device socket REFUSES connections
instead of accepting messages it cannot check. These tests do not need jsonschema.
"""
import pathlib
import subprocess
import sys

import pytest

from codeagent import ws as ws_module
from tests.unit.codeagent_testkit import Rig

BACKEND = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh_protocol_cache():
    ws_module.reset_protocol_cache()
    yield
    ws_module.reset_protocol_cache()


def test_without_the_protocol_validator_the_socket_refuses_instead_of_accepting_blind(monkeypatch):
    rig = Rig(monkeypatch)
    credential, _ = rig.store.add_device("user-a")

    def unavailable():
        raise ws_module.ProtocolUnavailable("jsonschema is not installed")

    monkeypatch.setattr(ws_module, "load_protocol", unavailable)
    with rig.connect() as dev:
        reply = dev.recv()
        assert reply["t"] == "error" and reply["payload"]["code"] == "internal"
        assert "jsonschema" not in str(reply)              # the reason goes to the log, not the wire
        assert dev.expect_close() == ws_module.CLOSE_INTERNAL
    assert rig.store.touch_calls == 0                      # nobody was authenticated
    assert rig.registry.count() == 0


def test_importing_the_endpoint_does_not_load_the_validator():
    """api/server.py imports codeagent.ws at startup; a missing jsonschema must not be able
    to stop the whole backend from starting."""
    code = ("import sys, codeagent.ws; "
            "assert 'codeagent_protocol_validate' not in sys.modules, 'validator loaded at import'")
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


def _fake_protocol_dir(tmp_path, validate_source):
    proto = tmp_path / "protocol"
    (proto / "v1").mkdir(parents=True)
    (proto / "validate.py").write_text(validate_source, encoding="utf-8")
    return proto


def test_load_protocol_reports_a_missing_schema_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_module, "_protocol_candidates", lambda: [tmp_path / "nowhere"])
    with pytest.raises(ws_module.ProtocolUnavailable):
        ws_module.load_protocol()


def test_load_protocol_reports_a_missing_dependency_without_leaking_paths(monkeypatch, tmp_path):
    proto = _fake_protocol_dir(tmp_path, "raise ImportError('No module named jsonschema')\n")
    monkeypatch.setattr(ws_module, "_protocol_candidates", lambda: [proto])
    with pytest.raises(ws_module.ProtocolUnavailable) as caught:
        ws_module.load_protocol()
    assert str(tmp_path) not in str(caught.value)


def test_load_protocol_does_not_cache_a_failure_forever_but_does_cache_success(monkeypatch, tmp_path):
    bad = _fake_protocol_dir(tmp_path, "raise ImportError('nope')\n")
    monkeypatch.setattr(ws_module, "_protocol_candidates", lambda: [bad])
    with pytest.raises(ws_module.ProtocolUnavailable):
        ws_module.load_protocol()
    good = tmp_path / "second" / "protocol"
    (good / "v1").mkdir(parents=True)
    (good / "validate.py").write_text("MARK = object()\n", encoding="utf-8")
    monkeypatch.setattr(ws_module, "_protocol_candidates", lambda: [good])
    first = ws_module.load_protocol()
    assert ws_module.load_protocol() is first              # loaded once
    ws_module.reset_protocol_cache()
    assert ws_module.load_protocol() is not first


def test_server_mounts_the_socket_and_it_is_not_behind_the_web_apps_auth():
    """Through the real api.server app, with no Authorization header at all: the request
    reaches the device handshake and is answered in protocol v1 shape. (invalid_message
    when the validator is available, internal when it is not: both are this endpoint.)"""
    from fastapi.testclient import TestClient

    from api.server import app

    with TestClient(app).websocket_connect("/v1/code/device") as sock:
        sock.send_text("this is not a hello")
        reply = sock.receive_json()
        assert reply["v"] == 1 and reply["t"] == "error"
        assert reply["payload"]["code"] in {"invalid_message", "internal"}
