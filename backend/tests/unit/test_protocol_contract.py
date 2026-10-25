"""
Patch P00 (M2C Wave 1) -- tests for the protocol and tool-schema contract.

What this file proves (the "Done when" of work package P00):

  1. Every golden transcript in protocol/fixtures/ validates, line by line
     against protocol/v1, AND as a whole (sequence numbers, call-id pairing,
     resume cursors -- see protocol/validate.py check_transcript()).
  2. Invalid examples fail, and fail for the *intended* reason (each case in
     protocol/fixtures/invalid_examples.jsonl names the keyword and the path
     it must trip, so a case cannot pass vacuously by being broken in some
     unrelated way).
  3. protocol/v1 is the only source of truth: no second copy of a schema lives
     anywhere in the repo except the client's synced copy at
     m2c/src/m2c/protocol/v1/, which must be byte-identical when present
     (and which is allowed to be absent -- C01 creates that package in
     parallel with this patch).
  4. The contract itself: message types, run_event subtypes, error codes,
     run.start fields, the nine v0 tools and their risk levels, and the
     "no file contents or paths in server-persisted metadata" rule (O1).

Dependency note: jsonschema is deliberately NOT in backend/requirements.txt
(this patch may not widen it). The module-level importorskip below keeps the
normal `cd backend && pytest tests/unit tests/integration -v` job green when
jsonschema is absent; the dedicated `protocol` CI job installs
protocol/requirements.txt and runs this file with --noconftest (backend's
conftest.py imports eo.registry at module scope, which would drag the whole
backend dependency set into that job).
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip(
    "jsonschema",
    reason="jsonschema is not in backend/requirements.txt; install "
           "protocol/requirements.txt to run the protocol contract tests "
           "(they run in the `protocol` CI job)",
)

ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "protocol"
assert PROTOCOL.is_dir(), f"expected {PROTOCOL} (repo root detection: {ROOT})"

_spec = importlib.util.spec_from_file_location(
    "m2c_protocol_validate", PROTOCOL / "validate.py"
)
V = importlib.util.module_from_spec(_spec)
sys.modules["m2c_protocol_validate"] = V
_spec.loader.exec_module(V)

# ---------------------------------------------------------------------------
# The contract, restated from the work package. These literals are
# intentionally duplicated here (not imported from the schemas): a test that
# derived its expectations from the thing under test could never fail.
# ---------------------------------------------------------------------------
CLIENT_TYPES = ["hello", "run.start", "user_message", "tool_result",
                "tool_stream", "approval", "cancel", "run.resume", "ping"]
SERVER_TYPES = ["welcome", "run_event", "tool_proposed", "budget_warning",
                "error", "pong"]
RUN_EVENT_TYPES = ["text_delta", "text_done", "tool_started", "tool_finished",
                   "state_changed", "usage", "notice"]
ERROR_CODES = ["unauthorized", "device_revoked", "quota_exhausted",
               "run_not_found", "protocol_unsupported", "rate_limited",
               "provider_unavailable", "tool_timeout", "invalid_message",
               "internal"]
RUN_START_FIELDS = {"profile", "mode", "cwd_label", "instructions", "rules",
                    "memory_index", "tool_catalog_version", "history",
                    "model_pref"}
# tool -> (risk, risk_max or None). Product guide 7.2: delete_path is "R1/R4".
TOOLS = {
    "read_file": ("R0", None),
    "list_dir": ("R0", None),
    "glob": ("R0", None),
    "grep": ("R0", None),
    "edit_file": ("R1", None),
    "write_file": ("R1", None),
    "delete_path": ("R1", "R4"),
    "bash": ("R2", None),
    "plan_exit": ("R0", None),
}
MINIMAL_INPUT = {
    "read_file": {"path": "a.txt"},
    "list_dir": {"path": "."},
    "glob": {"pattern": "**/*.py"},
    "grep": {"pattern": "TODO"},
    "edit_file": {"path": "a.txt", "old_string": "x", "new_string": "y"},
    "write_file": {"path": "a.txt", "content": "hi"},
    "delete_path": {"path": "a.txt"},
    "bash": {"command": "ls"},
    "plan_exit": {"plan": "1. do the thing"},
}
REQUIRED_FIXTURES = ["01_plain_chat.jsonl", "02_edit_with_approval.jsonl",
                     "03_disconnect_resume.jsonl", "04_quota_error.jsonl"]
NO_RUN_ID = {"hello", "welcome", "ping", "pong"}
NEEDS_SEQ = {"run_event", "tool_proposed", "budget_warning"}

FIXTURE_FILES = sorted(
    p for p in (PROTOCOL / "fixtures").glob("*.jsonl")
    if p.name != "invalid_examples.jsonl"
) if (PROTOCOL / "fixtures").is_dir() else []


def msg(t, payload, *, run_id="run_test_0001", seq=None, **over):
    """Build an envelope with sane defaults; `over` can break any field."""
    m = {"v": 1, "t": t, "id": str(uuid.uuid4()),
         "ts": "2026-11-02T09:00:00Z", "payload": payload}
    if t not in NO_RUN_ID:
        m["run_id"] = run_id
    if t in NEEDS_SEQ:
        m["seq"] = 1 if seq is None else seq
    elif seq is not None:
        m["seq"] = seq
    m.update(over)
    return m


def issues(m, **kw):
    return V.validate_message(m, **kw)


def has_issue(found, validator, path):
    return any(i.validator == validator and i.path == path for i in found)


# ---------------------------------------------------------------------------
# 1. The schemas themselves
# ---------------------------------------------------------------------------

def test_schema_files_are_wellformed():
    assert V.check_schemas() == []


def test_every_payload_schema_is_named_after_a_message_type():
    types = set(CLIENT_TYPES) | set(SERVER_TYPES)
    stems = {p.stem for p in (PROTOCOL / "v1").glob("*.json")}
    assert types <= stems, f"missing payload schemas: {types - stems}"
    assert stems - types == {"common", "envelope", "tool_definition"}


def test_message_types_match_the_work_package():
    env = V.load_json(PROTOCOL / "v1" / "envelope.json")
    assert env["x-m2c-types"]["client_to_server"] == CLIENT_TYPES
    assert env["x-m2c-types"]["server_to_client"] == SERVER_TYPES
    assert set(env["properties"]["t"]["enum"]) == set(CLIENT_TYPES + SERVER_TYPES)


def test_error_codes_are_the_ten_stable_strings():
    err = V.deref_payload_schema("error")
    assert err["properties"]["code"]["enum"] == ERROR_CODES


def test_run_event_subtypes():
    ev = V.load_json(PROTOCOL / "v1" / "run_event.json")
    assert ev["properties"]["type"]["enum"] == RUN_EVENT_TYPES
    assert set(ev["$defs"]) == set(RUN_EVENT_TYPES)


def test_run_start_payload_fields():
    rs = V.deref_payload_schema("run.start")
    assert set(rs["properties"]) == RUN_START_FIELDS
    # Only history is optional (work package item 5).
    assert set(rs["required"]) == RUN_START_FIELDS - {"history"}
    assert rs["properties"]["profile"]["enum"] == ["code"]          # O8
    assert rs["properties"]["mode"]["enum"] == ["plan", "ask", "accept-edits"]
    assert rs["additionalProperties"] is False


def test_welcome_declares_minimum_supported_version():
    w = V.deref_payload_schema("welcome")
    assert {"protocol", "min_protocol"} <= set(w["required"])


def test_hello_negotiates_versions():
    h = V.deref_payload_schema("hello")
    assert {"protocol_versions", "device_credential",
            "client_version", "capabilities"} <= set(h["required"])


def test_all_schema_ids_live_under_v1():
    for path, doc in V.iter_v1_documents():
        if "$id" in doc:
            assert "/protocol/v1/" in doc["$id"], path


# ---------------------------------------------------------------------------
# 2. Tool schemas v0
# ---------------------------------------------------------------------------

def test_tool_catalog_is_exactly_the_nine_v0_tools():
    assert set(V.load_tools()) == set(TOOLS)


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_definition_risk_and_shape(name):
    t = V.load_tools()[name]
    risk, risk_max = TOOLS[name]
    assert t["name"] == name
    assert t["version"] == "v0"
    assert t["risk"] == risk
    assert t.get("risk_max") == risk_max
    assert t["description"].strip()
    assert t["input_schema"]["type"] == "object"
    assert t["input_schema"]["additionalProperties"] is False
    assert t["input_schema"]["required"], "every v0 tool has a required arg"


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_definition_matches_meta_schema(name):
    assert V.check_tool_definition(V.load_tools()[name]) == []


ALLOWED_KEYWORDS = {"type", "properties", "required", "enum", "default",
                    "description", "minimum", "maximum", "minLength",
                    "maxLength", "items", "additionalProperties", "minItems",
                    "maxItems"}


def _keywords(schema):
    """Every keyword used in a schema, ignoring property *names*."""
    found = set()
    if isinstance(schema, dict):
        for k, v in schema.items():
            found.add(k)
            if k == "properties":
                for sub in v.values():
                    found |= _keywords(sub)
            else:
                found |= _keywords(v)
    elif isinstance(schema, list):
        for v in schema:
            found |= _keywords(v)
    return found


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_input_schemas_use_only_provider_portable_keywords(name):
    """S05 hands these to OpenAI-compatible and Anthropic-style APIs; stick to
    the subset every provider accepts (no $ref, oneOf, pattern, format...)."""
    used = _keywords(V.load_tools()[name]["input_schema"])
    assert used <= ALLOWED_KEYWORDS, used - ALLOWED_KEYWORDS


def test_edit_file_matches_the_design_note_example():
    s = V.load_tools()["edit_file"]["input_schema"]
    assert set(s["properties"]) == {"path", "old_string", "new_string",
                                    "replace_all"}
    assert s["properties"]["replace_all"]["type"] == "boolean"
    assert s["properties"]["replace_all"]["default"] is False
    assert s["required"] == ["path", "old_string", "new_string"]


def test_bash_v0_has_no_background_mode():
    """Product guide 7.2 files background shells under v1 (bash_background +
    bash_output + bash_kill, WP C16); C07's v0 shell cannot honour it."""
    s = V.load_tools()["bash"]["input_schema"]
    assert "background" not in s["properties"]
    assert s["required"] == ["command"]
    assert s["properties"]["timeout_s"]["default"] == 120


def test_unknown_tool_fails_closed():
    found = V.validate_tool_input("rm_rf", {})
    assert found and "unknown tool" in found[0].message
    assert V.validate_tool_input("bash", {"command": "ls"}) == []
    assert V.validate_tool_input("bash", {"cmd": "ls"}) != []


@pytest.mark.parametrize("name", sorted(TOOLS))
@pytest.mark.parametrize("level", ["R0", "R1", "R2", "R3", "R4"])
def test_tool_proposed_risk_matrix_follows_the_catalog(name, level):
    """A tool's calls carry its catalog risk, or its risk_max when it has one
    (delete_path: R1 inside the project, R4 otherwise) -- nothing between."""
    risk, risk_max = TOOLS[name]
    allowed = {risk} | ({risk_max} if risk_max else set())
    m = msg("tool_proposed", {"call_id": "call_1", "tool": name,
                              "input": MINIMAL_INPUT[name], "risk": level})
    assert (issues(m) == []) == (level in allowed)


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_proposed_validates_input_per_tool(name):
    ok = msg("tool_proposed", {"call_id": "call_1", "tool": name,
                               "input": MINIMAL_INPUT[name],
                               "risk": TOOLS[name][0]})
    assert issues(ok) == []
    bad = json.loads(json.dumps(ok))
    bad["payload"]["input"] = {"surprise": True}
    assert has_issue(issues(bad), "required", "payload/input") or \
        has_issue(issues(bad), "additionalProperties", "payload/input")


def test_tool_proposed_allows_tools_outside_the_v0_catalog():
    """MCP and v1 tools exist; the *client* fails closed on them, the wire
    format must not stop the server from proposing them."""
    m = msg("tool_proposed", {"call_id": "call_9", "tool": "mcp__github__list_issues",
                              "input": {"repo": "x"}, "risk": "R3"})
    assert issues(m) == []


# ---------------------------------------------------------------------------
# 3. Envelope rules
# ---------------------------------------------------------------------------

def test_minimal_valid_message_of_every_type_validates():
    samples = {
        "hello": {"protocol_versions": [1], "device_credential": "m2c-fixture-credential-aaaaaaaa",
                  "client_version": "0.0.1", "capabilities": []},
        "welcome": {"protocol": 1, "min_protocol": 1, "token_ttl_s": 900,
                    "limits": {"max_parallel_runs": 3}},
        "run.start": {"profile": "code", "mode": "ask", "cwd_label": "proj",
                      "instructions": "", "rules": [], "memory_index": "",
                      "tool_catalog_version": "v0", "model_pref": "auto"},
        "user_message": {"text": "hi"},
        "tool_result": {"call_id": "call_1", "ok": True, "output": "x",
                        "truncated": False, "trust": "local_project"},
        "tool_stream": {"call_id": "call_1", "stream": "stdout", "chunk": "x"},
        "approval": {"call_id": "call_1", "decision": "allow", "scope": "once"},
        "cancel": {},
        "run.resume": {"last_seq": 0},
        "ping": {},
        "pong": {},
        "run_event": {"type": "text_delta", "text": "hi"},
        "tool_proposed": {"call_id": "call_1", "tool": "read_file",
                          "input": {"path": "a"}, "risk": "R0"},
        "budget_warning": {"scope": "run", "spent_pct": 80},
        "error": {"code": "internal", "message": "boom"},
    }
    assert set(samples) == set(CLIENT_TYPES + SERVER_TYPES)
    for t, payload in samples.items():
        assert issues(msg(t, payload)) == [], t


@pytest.mark.parametrize("code", ERROR_CODES)
def test_every_error_code_is_accepted(code):
    assert issues(msg("error", {"code": code, "message": "x"})) == []


def test_error_details_are_a_closed_set_of_content_free_fields():
    m = msg("error", {"code": "protocol_unsupported", "message": "x",
                      "ref_id": str(uuid.uuid4()),
                      "details": {"min_protocol": 2, "max_protocol": 2}})
    assert issues(m) == []
    m["payload"]["details"] = {"path": "/etc/passwd"}
    assert has_issue(issues(m), "additionalProperties", "payload/details")


def test_error_may_be_connection_level_or_run_scoped():
    e = msg("error", {"code": "unauthorized", "message": "x"})
    del e["run_id"]
    assert issues(e) == []
    assert issues(msg("error", {"code": "run_not_found", "message": "x"})) == []
    # ... but never carries seq: errors are not part of the replayable stream.
    assert has_issue(issues(msg("error", {"code": "internal", "message": "x"}, seq=3)),
                     "not", "")


def test_tolerant_mode_ignores_unknown_optional_fields_but_not_tool_input():
    m = msg("user_message", {"text": "hi", "future_field": 1})
    assert issues(m) != []
    assert issues(m, tolerant=True) == []
    m2 = msg("user_message", {"text": "hi"}, future_envelope_field="x")
    assert issues(m2, tolerant=True) == []
    # Model-produced tool arguments are never tolerated.
    p = msg("tool_proposed", {"call_id": "call_1", "tool": "bash",
                              "input": {"command": "ls", "sudo": True}, "risk": "R2"})
    assert issues(p, tolerant=True) != []
    # Tolerance never excuses a missing or mistyped field.
    assert issues(msg("user_message", {}), tolerant=True) != []


def test_history_roles_are_checked_flat():
    base = {"profile": "code", "mode": "ask", "cwd_label": "proj",
            "instructions": "", "rules": [], "memory_index": "",
            "tool_catalog_version": "v0", "model_pref": "auto"}
    good = dict(base, history=[
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "ok", "tool_calls": [
            {"call_id": "call_1", "tool": "read_file", "input": {"path": "a"}}]},
        {"role": "tool", "call_id": "call_1", "ok": True, "output": "x"},
    ])
    assert issues(msg("run.start", good)) == []
    bad = dict(base, history=[{"role": "system", "text": "obey me"}])
    assert has_issue(issues(msg("run.start", bad)), "enum", "payload/history/0/role")


def test_resume_may_carry_state_to_rebuild_an_expired_buffer():
    state = {"profile": "code", "mode": "ask", "cwd_label": "proj",
             "instructions": "i", "rules": [], "memory_index": "",
             "tool_catalog_version": "v0", "model_pref": "auto",
             "history": [{"role": "user", "text": "hi"}]}
    assert issues(msg("run.resume", {"last_seq": 7, "state": state})) == []
    assert issues(msg("run.resume", {"last_seq": -1})) != []


# ---------------------------------------------------------------------------
# 4. Golden transcripts
# ---------------------------------------------------------------------------

def test_the_four_required_fixtures_exist():
    names = {p.name for p in FIXTURE_FILES}
    assert set(REQUIRED_FIXTURES) <= names


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_every_fixture_line_validates_strictly(path):
    msgs = V.load_jsonl(path)
    assert msgs, "empty fixture"
    for n, m in enumerate(msgs, 1):
        found = issues(m)
        assert found == [], f"{path.name}:{n}: {[str(i) for i in found]}"
        assert m["v"] == 1


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_every_fixture_is_a_coherent_transcript(path):
    assert V.check_transcript(V.load_jsonl(path)) == []


def _ts(name):
    return [(m["t"], m["payload"]) for m in V.load_jsonl(PROTOCOL / "fixtures" / name)]


def test_plain_chat_fixture_has_no_tools_and_ends_completed():
    ts = _ts("01_plain_chat.jsonl")
    assert not any(t in ("tool_proposed", "tool_result", "approval") for t, _ in ts)
    states = [p["state"] for t, p in ts
              if t == "run_event" and p["type"] == "state_changed"]
    assert states[-1] == "completed"
    assert any(t == "run_event" and p["type"] == "text_delta" for t, p in ts)
    assert any(t == "run_event" and p["type"] == "usage" for t, p in ts)


def test_edit_fixture_has_one_edit_and_one_approval():
    ts = _ts("02_edit_with_approval.jsonl")
    approvals = [p for t, p in ts if t == "approval"]
    edits = [p for t, p in ts if t == "tool_proposed" and p["tool"] == "edit_file"]
    assert len(approvals) == 1 and approvals[0]["decision"] == "allow"
    assert len(edits) == 1 and edits[0]["risk"] == "R1"
    assert approvals[0]["call_id"] == edits[0]["call_id"]
    results = [p for t, p in ts if t == "tool_result"
               and p["call_id"] == edits[0]["call_id"]]
    assert len(results) == 1 and results[0]["ok"] is True


def test_resume_fixture_has_two_connections_and_a_cursor():
    msgs = V.load_jsonl(PROTOCOL / "fixtures" / "03_disconnect_resume.jsonl")
    assert [m["t"] for m in msgs].count("hello") == 2
    resumes = [m for m in msgs if m["t"] == "run.resume"]
    assert len(resumes) == 1 and resumes[0]["payload"]["last_seq"] >= 1
    # The cut call is closed as disconnected, as product guide 13.5 requires.
    assert any(m["t"] == "run_event" and m["payload"]["type"] == "tool_finished"
               and m["payload"]["status"] == "disconnected" for m in msgs)
    assert any(m["t"] == "run_event" and m["payload"].get("state") == "paused_disconnected"
               for m in msgs)


def test_quota_fixture_has_budget_warning_error_and_pause():
    msgs = V.load_jsonl(PROTOCOL / "fixtures" / "04_quota_error.jsonl")
    errs = [m for m in msgs if m["t"] == "error"]
    assert [e["payload"]["code"] for e in errs] == ["quota_exhausted"]
    assert any(m["t"] == "budget_warning" for m in msgs)
    last = [m for m in msgs if m["t"] == "run_event"][-1]["payload"]
    assert last["type"] == "state_changed" and last["state"] == "paused_budget"


def test_fixtures_hold_no_real_looking_secrets():
    for path in FIXTURE_FILES:
        for m in V.load_jsonl(path):
            if m["t"] == "hello":
                cred = m["payload"]["device_credential"]
                assert cred.startswith("m2c-fixture-"), cred


# ---------------------------------------------------------------------------
# 5. Invalid examples
# ---------------------------------------------------------------------------

def test_invalid_examples_all_fail_for_the_intended_reason():
    assert V.check_invalid_examples() == []


def test_invalid_examples_are_numerous_and_unique():
    cases = V.load_jsonl(PROTOCOL / "fixtures" / "invalid_examples.jsonl")
    names = [c["case"] for c in cases]
    assert len(names) >= 25 and len(set(names)) == len(names)
    for c in cases:
        assert set(c) == {"case", "expect", "message"}, c["case"]
        assert set(c["expect"]) == {"validator", "path"}, c["case"]


@pytest.mark.parametrize("mutate,validator,path", [
    (lambda m: m.update(v=2), "const", "v"),
    (lambda m: m.update(t="run.finish"), "enum", "t"),
    (lambda m: m.update(id="not-a-uuid"), "pattern", "id"),
    (lambda m: m.update(ts="yesterday"), "pattern", "ts"),
    (lambda m: m.update(seq=0), "minimum", "seq"),
    (lambda m: m.pop("payload"), "required", ""),
    (lambda m: m.update(extra="x"), "additionalProperties", ""),
])
def test_envelope_violations_are_caught(mutate, validator, path):
    m = msg("run_event", {"type": "text_delta", "text": "x"})
    mutate(m)
    assert has_issue(issues(m), validator, path), [str(i) for i in issues(m)]


def test_run_id_and_seq_placement_rules():
    # client messages never carry seq
    assert has_issue(issues(msg("user_message", {"text": "x"}, seq=4)), "not", "")
    # run-scoped messages need run_id
    m = msg("user_message", {"text": "x"})
    del m["run_id"]
    assert has_issue(issues(m), "required", "")
    # server stream messages need seq
    m = msg("run_event", {"type": "text_delta", "text": "x"})
    del m["seq"]
    assert has_issue(issues(m), "required", "")
    # handshake and keepalive have no run
    # (msg() leaves run_id off these types, so add it by hand)
    ping = msg("ping", {})
    ping["run_id"] = "run_test_0001"
    assert has_issue(issues(ping), "not", "")
    hello = msg("hello", {"protocol_versions": [1],
                          "device_credential": "m2c-fixture-credential-aaaaaaaa",
                          "client_version": "0", "capabilities": []})
    hello["run_id"] = "run_test_0001"
    assert has_issue(issues(hello), "not", "")


def test_cwd_label_can_never_be_a_path():
    base = {"profile": "code", "mode": "ask", "instructions": "", "rules": [],
            "memory_index": "", "tool_catalog_version": "v0", "model_pref": "auto"}
    for bad in ["/home/u/proj", "~/proj", "a/b", "C:\\Users\\u", "..", ".", "",
                "x" * 129, "tab\there"]:
        found = issues(msg("run.start", dict(base, cwd_label=bad)))
        assert found, bad
    for good in ["proj", "my project", "m2c-client", "repo.v2"]:
        assert issues(msg("run.start", dict(base, cwd_label=good))) == [], good


# ---------------------------------------------------------------------------
# 6. "No file contents or paths in server-persisted metadata" (O1, 11.4)
# ---------------------------------------------------------------------------

#: Message payloads (and run_event subtypes) that legitimately carry content,
#: in flight only. Adding to this set is a deliberate, reviewed act: the server
#: must never persist or log any of these (WP S06/S09/Q06 build on that).
CONTENT_UNITS = {
    "run.start", "run.resume", "user_message", "tool_result", "tool_stream",
    "tool_proposed", "run_event:text_delta",
}
FORBIDDEN_IN_METADATA = {
    "path", "paths", "file", "files", "filename", "file_path", "filepath",
    "cwd", "dir", "directory", "content", "contents", "output", "command",
    "cmd", "args", "argv", "diff", "patch", "input", "text", "body", "prompt",
    "stdout", "stderr", "chunk", "old_string", "new_string", "history",
    "instructions", "rules", "memory_index", "plan", "pattern",
}


def test_content_class_units_are_exactly_the_reviewed_set():
    assert set(V.data_class_units("content")) == CONTENT_UNITS


def test_every_payload_declares_a_data_class():
    declared = set(V.data_class_units("content")) | set(V.data_class_units("metadata"))
    expected = set(CLIENT_TYPES + SERVER_TYPES) - {"run_event"}
    expected |= {f"run_event:{s}" for s in RUN_EVENT_TYPES}
    assert declared == expected


@pytest.mark.parametrize("unit", sorted(V.data_class_units("metadata"))
                         if hasattr(V, "data_class_units") else [])
def test_metadata_units_carry_no_content_or_path_fields(unit):
    schema = V.deref_unit(unit)
    names = set(V.property_names(schema))
    assert not (names & FORBIDDEN_IN_METADATA), names & FORBIDDEN_IN_METADATA
    for where, node in V.walk(schema):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, where
            assert "properties" in node, where
        if node.get("type") == "array":
            assert "maxItems" in node, where
        if node.get("type") == "string":
            bounded = ("maxLength" in node or "enum" in node
                       or "const" in node or "pattern" in node)
            assert bounded, f"{unit}: unbounded string at {where}"


def test_only_the_device_credential_is_marked_secret():
    secrets = V.secret_fields()
    assert secrets == {"hello": ["device_credential"]}


def test_metadata_messages_in_fixtures_contain_no_path_like_strings():
    for path in FIXTURE_FILES:
        for m in V.load_jsonl(path):
            unit = V.unit_of(m)
            if V.data_class_of(unit) != "metadata":
                continue
            for s in V.iter_strings(m["payload"]):
                assert not V.looks_like_path(s), (path.name, unit, s)


def test_path_heuristic_itself():
    assert V.looks_like_path("/etc/passwd")
    assert V.looks_like_path("see ~/code/x")
    assert V.looks_like_path("..\\secret")
    assert V.looks_like_path("C:\\Users\\a")
    assert not V.looks_like_path("openai/gpt-oss-120b")
    assert not V.looks_like_path("2026-11-02T09:00:00Z")
    assert not V.looks_like_path("quota_exhausted")


# ---------------------------------------------------------------------------
# 7. protocol/v1 is the only source; the client copy must match byte for byte
# ---------------------------------------------------------------------------

def test_no_second_copy_of_the_schemas_exists_in_the_repo():
    allowed = {(PROTOCOL / "v1").resolve(),
               (ROOT / "m2c" / "src" / "m2c" / "protocol" / "v1").resolve()}
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next"}
    stray = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if "envelope.json" in filenames or "run.start.json" in filenames:
            if Path(dirpath).resolve() not in allowed:
                stray.append(dirpath)
    assert stray == [], f"schema copies outside protocol/v1: {stray}"


def test_real_client_copy_is_identical_when_present():
    client = ROOT / "m2c" / "src" / "m2c" / "protocol" / "v1"
    if not client.exists():
        pytest.skip("m2c/ client package not created yet (WP C01) -- "
                    "check passes vacuously, as designed")
    assert V.compare_client_copy() == []


def test_missing_client_copy_passes(tmp_path):
    assert V.compare_client_copy(client_dir=tmp_path / "nope") == []


def _clone_v1(tmp_path):
    dst = tmp_path / "client" / "v1"
    V.copy_tree(PROTOCOL / "v1", dst)
    return dst


def test_identical_client_copy_passes(tmp_path):
    assert V.compare_client_copy(client_dir=_clone_v1(tmp_path)) == []


def test_one_changed_byte_is_detected(tmp_path):
    dst = _clone_v1(tmp_path)
    f = dst / "envelope.json"
    f.write_bytes(f.read_bytes() + b" ")
    problems = V.compare_client_copy(client_dir=dst)
    assert len(problems) == 1 and "envelope.json" in problems[0]


def test_whitespace_only_crlf_difference_is_detected(tmp_path):
    dst = _clone_v1(tmp_path)
    f = dst / "common.json"
    f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    assert any("common.json" in p for p in V.compare_client_copy(client_dir=dst))


def test_missing_and_extra_files_are_detected(tmp_path):
    dst = _clone_v1(tmp_path)
    (dst / "tools" / "bash.json").unlink()
    (dst / "tools" / "sneaky.json").write_text("{}\n")
    problems = "\n".join(V.compare_client_copy(client_dir=dst))
    assert "bash.json" in problems and "sneaky.json" in problems


def test_sync_makes_the_copy_identical_and_removes_stale_files(tmp_path):
    pkg = tmp_path / "m2c" / "src" / "m2c"
    pkg.mkdir(parents=True)
    dst = pkg / "protocol" / "v1"
    (dst / "tools").mkdir(parents=True)
    (dst / "tools" / "stale.json").write_text("{}\n")
    (dst / "envelope.json").write_text("{}\n")
    V.sync_client(client_pkg_dir=pkg)
    assert V.compare_client_copy(client_dir=dst) == []
    assert not (dst / "tools" / "stale.json").exists()
    # and non-json files a packager may add are left alone, not compared
    (dst / "__init__.py").write_text("")
    assert V.compare_client_copy(client_dir=dst) == []


def test_sync_refuses_when_the_client_package_does_not_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        V.sync_client(client_pkg_dir=tmp_path / "m2c" / "src" / "m2c")


# ---------------------------------------------------------------------------
# 8. The CLI and the CI wiring
# ---------------------------------------------------------------------------

def _run_cli(*args):
    return subprocess.run([sys.executable, str(PROTOCOL / "validate.py"), *args],
                          capture_output=True, text=True, cwd=ROOT, timeout=120)


def test_cli_default_run_passes():
    r = _run_cli()
    assert r.returncode == 0, r.stdout + r.stderr


def test_cli_check_client_copy_passes_when_absent_or_equal():
    r = _run_cli("--check-client-copy")
    assert r.returncode == 0, r.stdout + r.stderr


def test_cli_rejects_unknown_flags_with_exit_2():
    assert _run_cli("--nope").returncode == 2


def test_ci_has_a_protocol_job_in_the_repo_style():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    m = re.search(r"^  protocol:\n(.*?)(?=^  \S|\Z)", ci, re.S | re.M)
    assert m, "no `protocol:` job in ci.yml"
    job = m.group(1)
    assert "timeout-minutes:" in job
    assert "pip install -r protocol/requirements.txt" in job
    assert "python protocol/validate.py" in job
    assert "test_protocol_contract.py" in job
    for line in job.splitlines():
        if "uses:" in line:
            assert re.search(r"@[0-9a-f]{40} # v\d", line), \
                f"action not pinned to a SHA with a version comment: {line.strip()}"
    assert '"3.11"' in job


def test_protocol_requirements_are_pinned_and_separate_from_backend():
    reqs = (PROTOCOL / "requirements.txt").read_text()
    lines = [ln for ln in reqs.splitlines() if ln.strip() and not ln.startswith("#")]
    assert any(ln.startswith("jsonschema==") for ln in lines)
    assert all("==" in ln for ln in lines), "every protocol requirement is pinned"
    # S04b: P00 left jsonschema out of backend/requirements.txt (this assertion used to
    # say so); the device socket now needs it at runtime, so backend pins it. That the two
    # files carry the SAME pins is guarded by tests/static/test_protocol_runtime.py.
    backend = (ROOT / "backend" / "requirements.txt").read_text()
    assert any(ln.startswith("jsonschema==") for ln in backend.splitlines()), \
        "backend/requirements.txt must pin jsonschema (S04b): the device socket validates with it"
