"""S04b: in a backend install, the device socket's protocol reader loads.

codeagent/ws.py validates every message with protocol/validate.py, which needs
jsonschema (and `referencing`) and the protocol/v1 schemas. Until S04b a backend
install only got jsonschema transitively (guardrails-ai, litellm) and the container
had no protocol/ directory, so load_protocol() raised ProtocolUnavailable there and the
socket failed closed.

This file is the other half of tests/static/test_protocol_runtime.py (which reads the
requirements files, the Dockerfile and the dev overlay as text): it proves that in the
environment `pip install -r backend/requirements.txt` builds, the production loader
really works. It is deliberately NOT guarded by pytest.importorskip("jsonschema"): a
backend install that lacks jsonschema is exactly the regression to catch, so it must
fail here rather than skip. It lives in tests/unit because CI's backend job runs
`pytest tests/unit tests/integration` and does not collect tests/static.

The image and the dev overlay are checked by hand with the commands in the S04b patch
description (docker build, then `python -c "from codeagent.ws import load_protocol; ..."`).
"""
import importlib
import json
import pathlib

import pytest

from codeagent import ws as ws_module

ROOT = pathlib.Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "protocol" / "fixtures"


@pytest.fixture(autouse=True)
def _fresh_protocol_cache():
    ws_module.reset_protocol_cache()
    yield
    ws_module.reset_protocol_cache()


@pytest.mark.parametrize("module", ["jsonschema", "referencing", "referencing.jsonschema",
                                    "jsonschema_specifications", "rpds"])
def test_the_validator_dependencies_import_in_a_backend_install(module):
    importlib.import_module(module)


def test_load_protocol_succeeds_and_reads_the_schemas_from_a_candidate_directory():
    protocol = ws_module.load_protocol()                    # raises ProtocolUnavailable if not
    source = pathlib.Path(protocol.__file__).resolve()
    assert source.name == "validate.py"
    assert source.parent in [c.resolve() for c in ws_module._protocol_candidates()]
    assert pathlib.Path(protocol.V1_DIR).is_dir()
    assert (pathlib.Path(protocol.V1_DIR) / "envelope.json").is_file()
    assert ws_module.load_protocol() is protocol            # loaded once, then cached


def test_the_loaded_reader_accepts_a_golden_message_and_refuses_a_broken_one():
    protocol = ws_module.load_protocol()
    golden = [json.loads(line) for line in
              (FIXTURES / "01_plain_chat.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    hello = next(m for m in golden if m["t"] == "hello")
    assert protocol.validate_message(hello, tolerant=True) == []
    broken = dict(hello, t="no_such_message_type")
    assert protocol.validate_message(broken, tolerant=True) != []     # fails closed, not open


def test_the_server_types_the_socket_accepts_come_from_the_loaded_schemas():
    # ws.py reads the client-to-server types out of envelope.json's x-m2c-types: if the
    # schemas load, the nine client types are there and a server type is not among them.
    types = ws_module._client_types(ws_module.load_protocol())
    assert "hello" in types and "ping" in types
    assert "welcome" not in types and "pong" not in types
