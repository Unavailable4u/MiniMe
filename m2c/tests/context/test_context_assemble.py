"""
m2c/tests/context/test_context_assemble.py -- Patch C12: the one function that
assembles the three ``run.start`` fields, and the promise that they fit the
protocol.

Written before the implementation.  Groups:

* assembly      the three fields, from a session's (root, cwd, home)
* the protocol  the fields validate against protocol/v1 run.start: always with
                the client's own schema interpreter (no extra dependency),
                and additionally with the reference validator when jsonschema
                is installed
* size          the worst case fits the 1 MiB message ceiling
* isolation     nothing here can send anything: no network modules, and no
                imports from the C05 (tools) or C06 (policy) packages
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import sys
import uuid
from pathlib import Path

import pytest
from context_helpers import make_world, put
from m2c.context import RunContext, assemble_run_context
from m2c.context.memory import memory_dir

from m2c import context

PKG_DIR = Path(context.__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def run_start_payload(fields: dict, **overrides) -> dict:
    payload = {
        "profile": "code",
        "mode": "ask",
        "cwd_label": "repo",
        "tool_catalog_version": "v0",
        "model_pref": "auto",
        **fields,
    }
    payload.update(overrides)
    return payload


def seed(world):
    put(world.home / "M2C.md", "GLOBAL-INSTRUCTION")
    put(world.root / "M2C.md", "REPO-INSTRUCTION")
    put(world.root / "svc" / "AGENTS.md", "SVC-INSTRUCTION")
    put(world.root / ".m2c" / "rules" / "always.md", "ALWAYS-RULE")
    put(world.root / ".m2c" / "rules" / "py.md", "---\npaths: ['**/*.py']\n---\nPY-RULE")
    put(memory_dir(world.root, world.home) / "MEMORY.md", "- remembered fact")


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
def test_assembles_instructions_rules_and_memory_for_a_session(world):
    seed(world)
    ctx = assemble_run_context(world.root, world.root / "svc", world.home)
    assert isinstance(ctx, RunContext)
    assert ctx.instructions.index("GLOBAL-INSTRUCTION") < ctx.instructions.index("REPO-INSTRUCTION") \
        < ctx.instructions.index("SVC-INSTRUCTION")
    assert [r["name"] for r in ctx.rules] == ["project/always"]
    assert ctx.memory_index == "- remembered fact"


def test_run_start_fields_are_exactly_the_three_protocol_fields(world):
    seed(world)
    fields = assemble_run_context(world.root, world.root, world.home).run_start_fields()
    assert set(fields) == {"instructions", "rules", "memory_index"}
    assert isinstance(fields["instructions"], str) and isinstance(fields["memory_index"], str)
    assert isinstance(fields["rules"], list) and all(set(r) == {"name", "text"} for r in fields["rules"])


def test_a_bare_project_gives_valid_empty_fields(world):
    fields = assemble_run_context(world.root, world.root, world.home).run_start_fields()
    assert fields == {"instructions": "", "rules": [], "memory_index": ""}


def test_files_in_play_bring_in_the_path_scoped_rules(world):
    seed(world)
    ctx = assemble_run_context(world.root, world.root, world.home, files_in_play=["svc/app.py"])
    assert [r["name"] for r in ctx.rules] == ["project/always", "project/py"]
    assert assemble_run_context(world.root, world.root, world.home, files_in_play=["README.md"]).rules[0]["name"] \
        == "project/always"


def test_notes_from_all_three_loaders_are_collected(world):
    put(world.root / "M2C.md", "@gone.md")
    put(world.root / ".m2c" / "rules" / "bad.md", "---\npaths: [x\n---\nB")
    index = memory_dir(world.root, world.home) / "MEMORY.md"
    put(index, "\n".join(f"n{i}" for i in range(300)))
    ctx = assemble_run_context(world.root, world.root, world.home)
    codes = {n.code for n in ctx.notes}
    assert {"missing", "bad_front_matter", "truncated"} <= codes


def test_the_home_defaults_from_m2c_home(world, monkeypatch):
    seed(world)
    monkeypatch.setenv("M2C_HOME", str(world.home))
    ctx = assemble_run_context(world.root, world.root)
    assert "GLOBAL-INSTRUCTION" in ctx.instructions and ctx.memory_index == "- remembered fact"


def test_assembling_writes_nothing(world):
    seed(world)
    before = sorted(str(p) for p in world.root.rglob("*")) + sorted(str(p) for p in world.home.rglob("*"))
    assemble_run_context(world.root, world.root / "svc", world.home)
    after = sorted(str(p) for p in world.root.rglob("*")) + sorted(str(p) for p in world.home.rglob("*"))
    assert before == after


def test_a_cwd_outside_the_root_is_rejected(world):
    with pytest.raises(ValueError):
        assemble_run_context(world.root, world.outside, world.home)


def test_the_fields_can_be_passed_straight_to_the_link_clients_start_run(world):
    from m2c.link.client import DeviceLink

    params = inspect.signature(DeviceLink.start_run).parameters
    fields = assemble_run_context(world.root, world.root, world.home).run_start_fields()
    assert set(fields) <= set(params)


# ---------------------------------------------------------------------------
# the protocol
# ---------------------------------------------------------------------------
def test_the_fields_validate_against_the_clients_copy_of_run_start(world):
    from m2c.link.client import _validate_outgoing

    seed(world)
    ctx = assemble_run_context(world.root, world.root / "svc", world.home, files_in_play=["a.py"])
    _validate_outgoing("run.start", run_start_payload(ctx.run_start_fields()))


def test_empty_fields_validate_too(world):
    from m2c.link.client import _validate_outgoing

    _validate_outgoing("run.start", run_start_payload(assemble_run_context(world.root, world.root, world.home).run_start_fields()))


def test_the_schema_really_would_reject_the_work_packages_id_paths_text_shape(world):
    """Why rules go out as {name, text}: protocol/v1/run.start.json is closed."""
    from m2c.link.client import _validate_outgoing

    bad = run_start_payload({"instructions": "", "memory_index": "",
                             "rules": [{"id": "project/x", "paths": ["a/**"], "text": "t"}]})
    with pytest.raises(ValueError):
        _validate_outgoing("run.start", bad)


def test_the_fields_validate_against_the_reference_validator(world):
    pytest.importorskip("jsonschema", reason="install protocol/requirements.txt to run protocol/validate.py")
    validate_py = REPO_ROOT / "protocol" / "validate.py"
    if not validate_py.is_file():
        pytest.skip("protocol/validate.py not found (running from an sdist, outside the repository)")
    spec = importlib.util.spec_from_file_location("protocol_validate_for_c12", validate_py)
    validator = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validator          # its dataclasses look their module up here
    try:
        spec.loader.exec_module(validator)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise

    seed(world)
    ctx = assemble_run_context(world.root, world.root / "svc", world.home, files_in_play=["a.py"])
    envelope = {"v": 1, "t": "run.start", "id": str(uuid.uuid4()), "run_id": str(uuid.uuid4()),
                "ts": "2026-11-02T09:00:00.000Z", "payload": run_start_payload(ctx.run_start_fields())}
    assert validator.validate_message(envelope) == []
    assert validator.validate_message({**envelope, "payload": {**envelope["payload"],
                                       "rules": [{"name": "x", "text": "t", "paths": ["a"]}]}}) != []


# ---------------------------------------------------------------------------
# size
# ---------------------------------------------------------------------------
def test_even_the_worst_case_fits_every_protocol_and_transport_limit(world):
    wide = "ℝ" * 4000 + "😀" * 1000 + '"\\\n'                           # 3- and 4-byte characters, escapes
    put(world.home / "M2C.md", wide * 20)
    put(world.root / "M2C.md", wide * 20)
    put(world.root / "AGENTS.md", wide * 20)
    put(world.root / "CLAUDE.md", wide * 20)
    for i in range(70):
        put(world.root / ".m2c" / "rules" / f"r{i:02d}.md", wide * 6)
    put(memory_dir(world.root, world.home) / "MEMORY.md", (wide + "\n") * 200)
    ctx = assemble_run_context(world.root, world.root, world.home)
    fields = ctx.run_start_fields()
    assert len(fields["rules"]) <= 64
    for text in [fields["instructions"], fields["memory_index"], *[r["text"] for r in fields["rules"]]]:
        assert len(text) <= 200_000                                   # protocol bulk_text
    encoded = json.dumps({"payload": run_start_payload(fields)}, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) < 1_048_576 // 2              # half the ceiling: plenty of headroom


def test_the_default_caps_add_up(world):
    from m2c.context import instructions as ins
    from m2c.context import memory as mem
    from m2c.context import rules as rl

    worst_bytes = ins.MAX_TOTAL_BYTES + rl.MAX_RULES_TOTAL_BYTES + mem.MAX_INDEX_BYTES
    assert worst_bytes * 3 < 1_048_576          # even if every byte became three after JSON escaping


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------
NETWORK_AND_PEERS = {
    "socket", "ssl", "http", "urllib", "asyncio", "websockets", "ftplib", "smtplib", "requests", "subprocess",
}


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            yield ("." * node.level) + (node.module or "")


def test_the_context_package_imports_no_network_or_process_modules():
    offenders = []
    for py in sorted(PKG_DIR.glob("*.py")):
        for name in _imports(py):
            if name.lstrip(".").split(".")[0] in NETWORK_AND_PEERS:
                offenders.append(f"{py.name}: {name}")
    assert offenders == []


def test_the_context_package_does_not_import_the_c05_c06_or_link_packages():
    banned = ("tools", "policy", "link", "auth", "store", "repl", "cli")
    offenders = []
    for py in sorted(PKG_DIR.glob("*.py")):
        for name in _imports(py):
            parts = name.lstrip(".").split(".")
            absolute = parts[0] == "m2c" and len(parts) > 1 and parts[1] in banned
            relative = name.startswith("..") and parts[0] in banned
            if absolute or relative:
                offenders.append(f"{py.name}: {name}")
    assert offenders == []


def test_the_package_exposes_the_documented_entry_points():
    for name in ("assemble_run_context", "RunContext", "load_instructions", "load_rules", "load_memory_index",
                 "prepare_memory_write", "apply_memory_write", "select_rules", "ContextNote"):
        assert hasattr(context, name), name
