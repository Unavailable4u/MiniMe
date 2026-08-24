"""
tests/unit/test_agent_prompt_writer_lean.py — Patch 7f-1.

Covers agents/prompt_writer_lean.py: tier-1 pipeline, first step. Things
worth locking down per this module's own docstring/history:

  1. task_text sourcing — an explicit arg is written to tier1_task_text
     and used; omitted, it must be read back off the bus, raising
     ValueError if neither exists (first step of the pipeline).
  2. The CHAIN is a real multi-provider fallback (2x groq + 3x openrouter
     + 1x gemini, six steps, three independent OpenRouter accounts) —
     the 2026-08-12 / Patch 8.8 / OR-3b fixes this file's docstring
     describes, not a single point of failure.
  3. Part 23 fix: conversation context is prepended to what's sent to
     the model, but the raw task text written to tier1_task_text is
     unaffected.
  4. _strip_fences() handles a fenced ```json response correctly.
"""
import json

import pytest

from agents import prompt_writer_lean
from memory.bus import KEYS, read, write

# ---------------------------------------------------------------------------
# 1. _strip_fences
# ---------------------------------------------------------------------------

class TestStripFences:
    def test_plain_json_passes_through(self):
        raw = '{"name": "x"}'
        assert prompt_writer_lean._strip_fences(raw) == raw

    def test_fenced_json_block_is_unwrapped(self):
        raw = '```json\n{"name": "x"}\n```'
        assert prompt_writer_lean._strip_fences(raw) == '{"name": "x"}'

    def test_bare_fence_without_json_tag(self):
        raw = '```\n{"name": "x"}\n```'
        assert prompt_writer_lean._strip_fences(raw) == '{"name": "x"}'

    def test_strips_surrounding_whitespace(self):
        raw = '   {"name": "x"}   \n'
        assert prompt_writer_lean._strip_fences(raw) == '{"name": "x"}'


# ---------------------------------------------------------------------------
# 2. task_text sourcing
# ---------------------------------------------------------------------------

class TestTaskTextSourcing:
    def test_no_task_text_arg_and_nothing_in_memory_raises(self, fake_bus, mock_llm):
        with pytest.raises(ValueError):
            prompt_writer_lean.run()

    def test_explicit_task_text_is_written_to_memory(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"name": "m", "description": "d", "language": "python",
                                     "inputs": "i", "outputs": "o", "edge_cases": [], "constraints": []})
        prompt_writer_lean.run(task_text="reverse a string")
        assert read(KEYS["tier1_task_text"]) == "reverse a string"

    def test_falls_back_to_stored_task_text_when_none_given(self, fake_bus, mock_llm):
        write(KEYS["tier1_task_text"], "stored task from earlier")
        mock_llm.set_json_response({"name": "m", "description": "d", "language": "python",
                                     "inputs": "i", "outputs": "o", "edge_cases": [], "constraints": []})
        prompt_writer_lean.run()
        user_content = mock_llm.mock.call_args.kwargs["user_content"]
        assert "stored task from earlier" in user_content


# ---------------------------------------------------------------------------
# 3. CHAIN composition
# ---------------------------------------------------------------------------

class TestChainComposition:
    def test_chain_has_six_steps(self):
        assert len(prompt_writer_lean.CHAIN) == 6

    def test_first_two_steps_are_groq_on_the_shared_key(self):
        assert prompt_writer_lean.CHAIN[0]["provider"] == "groq"
        assert prompt_writer_lean.CHAIN[0]["key_env"] == "GROQ_API_KEY"
        assert prompt_writer_lean.CHAIN[1]["provider"] == "groq"
        assert prompt_writer_lean.CHAIN[1]["key_env"] == "GROQ_API_KEY"

    def test_middle_three_steps_are_openrouter_on_independent_accounts(self):
        openrouter_steps = prompt_writer_lean.CHAIN[2:5]
        assert all(step["provider"] == "openrouter" for step in openrouter_steps)
        key_envs = [step["key_env"] for step in openrouter_steps]
        assert key_envs == ["OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2", "OPENROUTER_API_KEY_3"]
        # genuinely independent accounts, not the same key repeated
        assert len(set(key_envs)) == 3

    def test_last_step_is_gemini_as_the_final_provider_fallback(self):
        last = prompt_writer_lean.CHAIN[-1]
        assert last["provider"] == "gemini"
        assert last["key_env"] == "GEMINI_API_KEY_1"

    def test_run_passes_the_module_chain_to_generate_text(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"name": "m", "description": "d", "language": "python",
                                     "inputs": "i", "outputs": "o", "edge_cases": [], "constraints": []})
        prompt_writer_lean.run(task_text="do a thing")
        chain = mock_llm.mock.call_args.kwargs["chain"]
        assert chain == prompt_writer_lean.CHAIN


# ---------------------------------------------------------------------------
# 4. Conversation-memory prepend (Part 23)
# ---------------------------------------------------------------------------

class TestConversationContextPrepend:
    def test_no_session_id_sends_plain_task_prefix(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"name": "m", "description": "d", "language": "python",
                                     "inputs": "i", "outputs": "o", "edge_cases": [], "constraints": []})
        prompt_writer_lean.run(task_text="reverse a string")
        user_content = mock_llm.mock.call_args.kwargs["user_content"]
        assert user_content == "Task: reverse a string"

    def test_conversation_context_is_prepended_when_present(self, monkeypatch, fake_bus, mock_llm):
        monkeypatch.setattr(
            "eo.conversation_memory.get_full_context",
            lambda session_id, *a, **k: "[user]: earlier turn",
        )
        mock_llm.set_json_response({"name": "m", "description": "d", "language": "python",
                                     "inputs": "i", "outputs": "o", "edge_cases": [], "constraints": []})
        prompt_writer_lean.run(task_text="reverse a string", session_id="sess-1")
        user_content = mock_llm.mock.call_args.kwargs["user_content"]
        assert user_content.startswith("Recent conversation:\n[user]: earlier turn")
        assert user_content.endswith("Task: reverse a string")

    def test_stored_tier1_task_text_is_unaffected_by_the_prepend(self, monkeypatch, fake_bus, mock_llm):
        monkeypatch.setattr(
            "eo.conversation_memory.get_full_context",
            lambda session_id, *a, **k: "some prior context",
        )
        mock_llm.set_json_response({"name": "m", "description": "d", "language": "python",
                                     "inputs": "i", "outputs": "o", "edge_cases": [], "constraints": []})
        prompt_writer_lean.run(task_text="reverse a string", session_id="sess-1")
        assert read(KEYS["tier1_task_text"]) == "reverse a string"


# ---------------------------------------------------------------------------
# 5. run() end to end
# ---------------------------------------------------------------------------

class TestRun:
    def test_returns_parsed_spec_and_writes_it_to_memory(self, fake_bus, mock_llm):
        spec = {"name": "reverse_string", "description": "reverses a string", "language": "python",
                "inputs": "a string", "outputs": "the reversed string", "edge_cases": ["empty string"],
                "constraints": []}
        mock_llm.set_response(f"```json\n{json.dumps(spec)}\n```")
        result = prompt_writer_lean.run(task_text="reverse a string")
        assert result == spec
        assert read(KEYS["tier1_module_spec"]) == spec

    def test_forwards_session_id_path_and_domain(self, fake_bus, mock_llm):
        mock_llm.set_json_response({"name": "m", "description": "d", "language": "python",
                                     "inputs": "i", "outputs": "o", "edge_cases": [], "constraints": []})
        prompt_writer_lean.run(task_text="x", session_id="sess-2", path="direct", domain="coding")
        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["session_id"] == "sess-2"
        assert kwargs["path"] == "direct"
        assert kwargs["domain"] == "coding"
        assert kwargs["agent_name"] == "Prompt Writer (lean)"
