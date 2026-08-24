"""
tests/unit/test_agent_dependency_mapper.py — Patch 7f-5.

Covers agents/dependency_mapper.py: the early-return when no
submitted_code exists yet (writes an empty map, no LLM call), the
800-char code preview truncation for both dict-shaped and non-dict
module entries, _strip_fences() (same shape as every other agent's
fence-stripper in this codebase), the dynamic-vs-static fallback chain
selection (same pattern deploy_config_writer.py / architecture_diagrammer.py
use), and run()'s happy path (bus write, emit_event payload shape).

Also covers the one real behavioral edge worth pinning down explicitly:
run() does NOT catch json.JSONDecodeError around its own
json.loads(_strip_fences(raw_text)) call, unlike deploy_config_writer.py
and dataset_analyst.py which both degrade to a safe fallback on
unparseable output. Confirmed this is dependency_mapper.py's own choice
rather than an oversight worth "fixing": eo/executor.py's UNSCOPED_TIER_AGENTS
dispatch (which is how this module is actually invoked) has no
special-case handling for it either, so a raised JSONDecodeError here
propagates as an ordinary agent-step failure through the same path any
other unhandled exception from an UNSCOPED_TIER_AGENTS callable would --
not a silent data-quality problem. This suite pins that behavior down as
a regression guard (it raises, it doesn't swallow) rather than treating
it as a bug to patch over.

generate_text is faked via the shared `mock_llm` fixture (bound-name
import). The deferred `from eo.dynamic_chain import build_fallback_chain`
import is faked via a sys.modules substitute, same approach
test_agent_dataset_analyst.py / test_agent_deploy_config_writer.py take
for the identical deferred-import shape.
"""
import json
import sys

import pytest

import agents.dependency_mapper as dependency_mapper
from memory.bus import write, read, KEYS


@pytest.fixture(autouse=True)
def _fake_emit_event(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dependency_mapper, "emit_event",
        lambda event, session_id, agent=None, payload=None: calls.append(
            (event, session_id, agent, payload)
        ),
    )
    return calls


@pytest.fixture
def fake_dynamic_chain(monkeypatch):
    fake = type("M", (), {"build_fallback_chain": staticmethod(lambda role: [])})()
    sys.modules["eo.dynamic_chain"] = fake
    return fake


SUBMITTED_CODE = {
    "auth": {"language": "python", "code": "from db import get_user\n"},
    "db": {"language": "python", "code": "def get_user(id): ...\n"},
}

VALID_MAP = {
    "auth": {"depends_on": ["db"], "notes": "imports get_user from db"},
    "db": {"depends_on": [], "notes": "no internal dependencies"},
}


# ---------------------------------------------------------------------------
# 1. run(): no submitted_code -> empty map, no LLM call
# ---------------------------------------------------------------------------
class TestNoSubmittedCode:
    def test_no_key_at_all_writes_empty_map_and_returns_it(self, fake_bus, mock_llm):
        result = dependency_mapper.run()

        assert result == {}
        assert read(KEYS["dependency_map"]) == {}
        assert mock_llm.mock.call_count == 0

    def test_empty_dict_submitted_code_also_short_circuits(self, fake_bus, mock_llm):
        write(KEYS["submitted_code"], {})

        result = dependency_mapper.run()

        assert result == {}
        assert mock_llm.mock.call_count == 0

    def test_no_llm_call_means_no_event_emitted(self, fake_bus, mock_llm, _fake_emit_event):
        dependency_mapper.run()
        assert _fake_emit_event == []


# ---------------------------------------------------------------------------
# 2. _strip_fences(): output-cleanup, same shape as every other agent's
# ---------------------------------------------------------------------------
class TestStripFences:
    def test_plain_json_unchanged(self):
        assert dependency_mapper._strip_fences('{"a": {}}') == '{"a": {}}'

    def test_strips_bare_fence(self):
        assert dependency_mapper._strip_fences('```\n{"a": {}}\n```') == '{"a": {}}'

    def test_strips_json_tagged_fence(self):
        assert dependency_mapper._strip_fences('```json\n{"a": {}}\n```') == '{"a": {}}'

    def test_strips_surrounding_whitespace(self):
        assert dependency_mapper._strip_fences('   {"a": {}}   ') == '{"a": {}}'


# ---------------------------------------------------------------------------
# 3. run(): code preview truncation, dict vs non-dict module entries
# ---------------------------------------------------------------------------
class TestCodePreview:
    def test_dict_module_code_field_is_used_and_truncated_to_800_chars(
        self, fake_bus, mock_llm, fake_dynamic_chain
    ):
        long_code = "x = 1\n" * 300  # well over 800 chars
        write(KEYS["submitted_code"], {"big": {"language": "python", "code": long_code}})
        mock_llm.set_json_response({"big": {"depends_on": [], "notes": "n/a"}})

        dependency_mapper.run()

        user_prompt = mock_llm.mock.call_args.args[1]
        sent_preview = json.loads(user_prompt)["modules"]["big"]
        assert len(sent_preview) == 800
        assert sent_preview == long_code[:800]

    def test_non_dict_module_entry_is_stringified_and_truncated(
        self, fake_bus, mock_llm, fake_dynamic_chain
    ):
        write(KEYS["submitted_code"], {"weird": "just a raw string, not a dict"})
        mock_llm.set_json_response({"weird": {"depends_on": [], "notes": "n/a"}})

        dependency_mapper.run()

        user_prompt = mock_llm.mock.call_args.args[1]
        sent_preview = json.loads(user_prompt)["modules"]["weird"]
        assert sent_preview == "just a raw string, not a dict"

    def test_dict_module_missing_code_field_previews_as_empty_string(
        self, fake_bus, mock_llm, fake_dynamic_chain
    ):
        write(KEYS["submitted_code"], {"empty": {"language": "python"}})
        mock_llm.set_json_response({"empty": {"depends_on": [], "notes": "n/a"}})

        dependency_mapper.run()

        user_prompt = mock_llm.mock.call_args.args[1]
        sent_preview = json.loads(user_prompt)["modules"]["empty"]
        assert sent_preview == ""


# ---------------------------------------------------------------------------
# 4. run(): dynamic vs static fallback chain
# ---------------------------------------------------------------------------
class TestChainSelection:
    def test_static_fallback_chain_used_when_dynamic_chain_empty(
        self, fake_bus, mock_llm, fake_dynamic_chain
    ):
        write(KEYS["submitted_code"], SUBMITTED_CODE)
        mock_llm.set_json_response(VALID_MAP)

        dependency_mapper.run()

        used_chain = mock_llm.mock.call_args.args[2]
        assert used_chain == dependency_mapper.FALLBACK_CHAIN

    def test_dynamic_chain_used_when_non_empty(self, fake_bus, mock_llm, monkeypatch):
        custom_chain = [{"provider": "cloudflare", "model": "custom", "account_id_env": "A", "token_env": "T"}]
        fake_mod = type("M", (), {"build_fallback_chain": staticmethod(lambda role: custom_chain)})()
        sys.modules["eo.dynamic_chain"] = fake_mod
        write(KEYS["submitted_code"], SUBMITTED_CODE)
        mock_llm.set_json_response(VALID_MAP)

        dependency_mapper.run()

        used_chain = mock_llm.mock.call_args.args[2]
        assert used_chain == custom_chain


# ---------------------------------------------------------------------------
# 5. run(): happy path -- bus write, event emission, forwarded kwargs
# ---------------------------------------------------------------------------
class TestRunHappyPath:
    def test_parsed_map_written_to_bus_and_returned(self, fake_bus, mock_llm, fake_dynamic_chain):
        write(KEYS["submitted_code"], SUBMITTED_CODE)
        mock_llm.set_json_response(VALID_MAP)

        result = dependency_mapper.run()

        assert result == VALID_MAP
        assert read(KEYS["dependency_map"]) == VALID_MAP

    def test_strips_fences_before_parsing(self, fake_bus, mock_llm, fake_dynamic_chain):
        write(KEYS["submitted_code"], SUBMITTED_CODE)
        mock_llm.set_response("```json\n" + json.dumps(VALID_MAP) + "\n```")

        result = dependency_mapper.run()

        assert result == VALID_MAP

    def test_emits_dependency_map_event_with_full_map_payload(
        self, fake_bus, mock_llm, fake_dynamic_chain, _fake_emit_event
    ):
        write(KEYS["submitted_code"], SUBMITTED_CODE)
        mock_llm.set_json_response(VALID_MAP)

        dependency_mapper.run(session_id="sess-1")

        event, session_id, agent, payload = _fake_emit_event[0]
        assert event == "dependency_map"
        assert session_id == "sess-1"
        assert agent == "dependency_mapper"
        assert payload == {"map": VALID_MAP}

    def test_session_id_tier_and_domain_forwarded_to_generate_text(
        self, fake_bus, mock_llm, fake_dynamic_chain
    ):
        write(KEYS["submitted_code"], SUBMITTED_CODE)
        mock_llm.set_json_response(VALID_MAP)

        dependency_mapper.run(session_id="sess-1", tier=2, domain="coding")

        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["tier"] == 2
        assert kwargs["domain"] == "coding"
        assert kwargs["agent_name"] == "Dependency Mapper"

    def test_every_submitted_module_name_included_in_prompt(self, fake_bus, mock_llm, fake_dynamic_chain):
        write(KEYS["submitted_code"], SUBMITTED_CODE)
        mock_llm.set_json_response(VALID_MAP)

        dependency_mapper.run()

        user_prompt = mock_llm.mock.call_args.args[1]
        sent_modules = json.loads(user_prompt)["modules"]
        assert set(sent_modules.keys()) == {"auth", "db"}


# ---------------------------------------------------------------------------
# 6. run(): unparseable JSON is NOT swallowed -- propagates, unlike
#    deploy_config_writer.py / dataset_analyst.py's degrade-to-fallback
#    approach. See module docstring above for why this is pinned down as
#    intended behavior rather than patched over.
# ---------------------------------------------------------------------------
class TestUnparseableJsonPropagates:
    def test_unparseable_response_raises_json_decode_error(self, fake_bus, mock_llm, fake_dynamic_chain):
        write(KEYS["submitted_code"], SUBMITTED_CODE)
        mock_llm.set_response("not json at all")

        with pytest.raises(json.JSONDecodeError):
            dependency_mapper.run()

    def test_nothing_written_to_bus_when_parse_fails(self, fake_bus, mock_llm, fake_dynamic_chain):
        write(KEYS["submitted_code"], SUBMITTED_CODE)
        mock_llm.set_response("not json at all")

        with pytest.raises(json.JSONDecodeError):
            dependency_mapper.run()

        assert read(KEYS["dependency_map"]) is None
