"""
tests/unit/test_agent_prompt_writer.py — Patch 7f-1.

Covers agents/prompt_writer.py: the production (tier-2/3) Prompt/Spec
Writer. Three things worth locking down given this module's own
docstring/comments:

  1. _parse_fenced_json() — fail-quiet convention (always [] on anything
     unparseable, matching api/server.py's own copy).
  2. _maybe_add_monitoring_module() (Part 7 §7.5) — idempotent append of
     MONITORING_MODULE_SPEC when integration_flagger flagged
     "monitoring" for this session, no-op otherwise, no-op on falsy
     session_id, and never double-added across repeat calls.
  3. run() — reads current_plan off the bus, strips a fenced-json
     response before parsing, writes module_specs, and returns it.

generate_text() is faked via the shared `mock_llm` fixture; memory.bus
reads/writes go through the autouse `fake_bus` fixture (both from
tests/conftest.py), so nothing here touches real Upstash Redis.
"""
import json

import pytest

import agents.prompt_writer as prompt_writer
from memory.bus import read, write, KEYS


# ---------------------------------------------------------------------------
# 1. _parse_fenced_json
# ---------------------------------------------------------------------------

class TestParseFencedJson:
    def test_none_or_empty_returns_empty_list(self):
        assert prompt_writer._parse_fenced_json(None) == []
        assert prompt_writer._parse_fenced_json("") == []

    def test_fenced_json_with_integrations_key(self):
        text = '```json\n{"integrations": [{"type": "monitoring"}]}\n```'
        result = prompt_writer._parse_fenced_json(text)
        assert result == [{"type": "monitoring"}]

    def test_unfenced_json_still_parses(self):
        text = '{"integrations": [{"type": "auth"}]}'
        result = prompt_writer._parse_fenced_json(text)
        assert result == [{"type": "auth"}]

    def test_malformed_json_returns_empty_list_not_raise(self):
        assert prompt_writer._parse_fenced_json("```json\nnot valid json\n```") == []

    def test_valid_json_but_not_a_dict_returns_empty_list(self):
        assert prompt_writer._parse_fenced_json('```json\n["a", "list"]\n```') == []

    def test_dict_missing_integrations_key_returns_empty_list(self):
        assert prompt_writer._parse_fenced_json('{"other_key": []}') == []


# ---------------------------------------------------------------------------
# 2. _maybe_add_monitoring_module
# ---------------------------------------------------------------------------

class TestMaybeAddMonitoringModule:
    def test_falsy_session_id_is_a_no_op(self, fake_bus):
        specs = {"modules": []}
        result = prompt_writer._maybe_add_monitoring_module(specs, None)
        assert result == {"modules": []}

    def test_no_flagged_integration_is_a_no_op(self, fake_bus):
        write("stage_output:sess-1:integration_flagger", '```json\n{"integrations": []}\n```')
        specs = {"modules": []}
        result = prompt_writer._maybe_add_monitoring_module(specs, "sess-1")
        assert result["modules"] == []

    def test_flagged_monitoring_appends_the_module(self, fake_bus):
        write(
            "stage_output:sess-1:integration_flagger",
            '```json\n{"integrations": [{"type": "monitoring"}]}\n```',
        )
        specs = {"modules": []}
        result = prompt_writer._maybe_add_monitoring_module(specs, "sess-1")
        names = [m["name"] for m in result["modules"]]
        assert prompt_writer.MONITORING_MODULE_NAME in names

    def test_already_present_is_not_duplicated(self, fake_bus):
        write(
            "stage_output:sess-1:integration_flagger",
            '```json\n{"integrations": [{"type": "monitoring"}]}\n```',
        )
        specs = {"modules": [{"name": prompt_writer.MONITORING_MODULE_NAME}]}
        result = prompt_writer._maybe_add_monitoring_module(specs, "sess-1")
        count = sum(1 for m in result["modules"] if m["name"] == prompt_writer.MONITORING_MODULE_NAME)
        assert count == 1

    def test_case_insensitive_name_match_prevents_duplicate(self, fake_bus):
        write(
            "stage_output:sess-1:integration_flagger",
            '```json\n{"integrations": [{"type": "monitoring"}]}\n```',
        )
        specs = {"modules": [{"name": prompt_writer.MONITORING_MODULE_NAME.upper()}]}
        result = prompt_writer._maybe_add_monitoring_module(specs, "sess-1")
        assert len(result["modules"]) == 1

    def test_no_cached_flagger_output_is_a_no_op(self, fake_bus):
        specs = {"modules": []}
        result = prompt_writer._maybe_add_monitoring_module(specs, "sess-never-ran-flagger")
        assert result["modules"] == []

    def test_creates_modules_list_if_absent(self, fake_bus):
        write(
            "stage_output:sess-1:integration_flagger",
            '```json\n{"integrations": [{"type": "monitoring"}]}\n```',
        )
        specs = {}
        result = prompt_writer._maybe_add_monitoring_module(specs, "sess-1")
        assert "modules" in result
        assert result["modules"][0]["name"] == prompt_writer.MONITORING_MODULE_NAME


# ---------------------------------------------------------------------------
# 3. run()
# ---------------------------------------------------------------------------

class TestRun:
    def _seed_plan(self, goal="build a todo app"):
        write(KEYS["current_plan"], {"cycle_goal": goal})

    def test_reads_cycle_goal_and_sends_it_to_the_model(self, fake_bus, mock_llm):
        self._seed_plan("build a chat app")
        mock_llm.set_json_response({"modules": [{"name": "m1", "description": "d",
                                                   "inputs": "i", "outputs": "o", "edge_cases": []}]})
        prompt_writer.run()
        user_content = mock_llm.mock.call_args.kwargs.get("user_content") or mock_llm.mock.call_args[0][1]
        assert "build a chat app" in user_content

    def test_strips_fenced_json_before_parsing(self, fake_bus, mock_llm):
        self._seed_plan()
        payload = {"modules": [{"name": "m1", "description": "d", "inputs": "i",
                                 "outputs": "o", "edge_cases": []}]}
        mock_llm.set_response(f"```json\n{json.dumps(payload)}\n```")
        result = prompt_writer.run()
        assert result["modules"][0]["name"] == "m1"

    def test_unfenced_json_also_parses(self, fake_bus, mock_llm):
        self._seed_plan()
        payload = {"modules": [{"name": "m1", "description": "d", "inputs": "i",
                                 "outputs": "o", "edge_cases": []}]}
        mock_llm.set_json_response(payload)
        result = prompt_writer.run()
        assert result["modules"][0]["name"] == "m1"

    def test_writes_result_to_module_specs_key(self, fake_bus, mock_llm):
        self._seed_plan()
        payload = {"modules": [{"name": "m1", "description": "d", "inputs": "i",
                                 "outputs": "o", "edge_cases": []}]}
        mock_llm.set_json_response(payload)
        prompt_writer.run()
        stored = read(KEYS["module_specs"])
        assert stored["modules"][0]["name"] == "m1"

    def test_monitoring_module_appended_when_flagged(self, fake_bus, mock_llm):
        self._seed_plan()
        write(
            "stage_output:sess-1:integration_flagger",
            '```json\n{"integrations": [{"type": "monitoring"}]}\n```',
        )
        payload = {"modules": [{"name": "m1", "description": "d", "inputs": "i",
                                 "outputs": "o", "edge_cases": []}]}
        mock_llm.set_json_response(payload)
        result = prompt_writer.run(session_id="sess-1")
        names = [m["name"] for m in result["modules"]]
        assert prompt_writer.MONITORING_MODULE_NAME in names

    def test_forwards_session_id_tier_and_domain(self, fake_bus, mock_llm):
        self._seed_plan()
        mock_llm.set_json_response({"modules": []})
        prompt_writer.run(session_id="sess-5", tier=2, domain="coding")
        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs.get("session_id") == "sess-5"
        assert kwargs.get("tier") == 2
        assert kwargs.get("domain") == "coding"
