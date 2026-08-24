"""
tests/unit/test_agent_responder.py — Patch 7f-1.

Covers agents/responder.py's run(): the tier-0 no-plan/no-pipeline answer
path. Three behaviors specific to this module, per its own docstring,
are the ones worth locking down:

  1. task_text is mandatory (tier 0 has no memory.bus fallback, Part 5.1)
     -- missing/falsy task_text raises ValueError rather than silently
     reading stale state.
  2. key_override resolution: None -> EO_INSPECTOR_GROQ_KEY_1 default,
     a bare string -> used as the primary key, a list -> only its FIRST
     entry is used (no parallel-pool meaning here, unlike pool agents).
     Either way the hardcoded openrouter fallback step is unaffected.
  3. Part 23 fix: session_id pulls conversation_memory.get_full_context()
     and prepends it to the text sent to the model, but the raw
     `task_text` argument itself is never mutated.

generate_text() itself is faked via the shared `mock_llm` fixture
(tests/conftest.py) -- these tests assert on the chain/kwargs responder.py
builds and passes to it, not on real LLM behavior.
"""
import pytest

from agents import responder

# ---------------------------------------------------------------------------
# 1. task_text is mandatory
# ---------------------------------------------------------------------------

class TestTaskTextRequired:
    def test_missing_task_text_raises_value_error(self, mock_llm):
        with pytest.raises(ValueError):
            responder.run()

    def test_empty_string_task_text_raises_value_error(self, mock_llm):
        with pytest.raises(ValueError):
            responder.run(task_text="")

    def test_none_task_text_raises_value_error(self, mock_llm):
        with pytest.raises(ValueError):
            responder.run(task_text=None)


# ---------------------------------------------------------------------------
# 2. key_override resolution
# ---------------------------------------------------------------------------

class TestKeyOverrideResolution:
    def test_default_uses_eo_inspector_groq_key_1(self, mock_llm):
        mock_llm.set_response("an answer")
        responder.run(task_text="what is 2+2?")
        chain = mock_llm.mock.call_args.kwargs["chain"]
        assert chain[0]["key_env"] == "EO_INSPECTOR_GROQ_KEY_1"
        assert chain[1]["key_env"] == "EO_INSPECTOR_GROQ_KEY_1"

    def test_string_override_becomes_primary_key(self, mock_llm):
        mock_llm.set_response("an answer")
        responder.run(task_text="what is 2+2?", key_override="SOME_OTHER_KEY")
        chain = mock_llm.mock.call_args.kwargs["chain"]
        assert chain[0]["key_env"] == "SOME_OTHER_KEY"
        assert chain[1]["key_env"] == "SOME_OTHER_KEY"

    def test_list_override_uses_only_first_entry(self, mock_llm):
        mock_llm.set_response("an answer")
        responder.run(task_text="what is 2+2?", key_override=["KEY_A", "KEY_B", "KEY_C"])
        chain = mock_llm.mock.call_args.kwargs["chain"]
        assert chain[0]["key_env"] == "KEY_A"
        assert chain[1]["key_env"] == "KEY_A"
        # KEY_B/KEY_C are dropped entirely, not appended anywhere in the chain
        assert all(step["key_env"] != "KEY_B" for step in chain)
        assert all(step["key_env"] != "KEY_C" for step in chain)

    def test_fallback_step_is_unaffected_by_key_override(self, mock_llm):
        mock_llm.set_response("an answer")
        responder.run(task_text="what is 2+2?", key_override="SOME_OTHER_KEY")
        chain = mock_llm.mock.call_args.kwargs["chain"]
        fallback = chain[-1]
        assert fallback["provider"] == "openrouter"
        assert fallback["key_env"] == "OPENROUTER_API_KEY_4"

    def test_chain_has_exactly_three_steps(self, mock_llm):
        mock_llm.set_response("an answer")
        responder.run(task_text="what is 2+2?")
        chain = mock_llm.mock.call_args.kwargs["chain"]
        assert len(chain) == 3


# ---------------------------------------------------------------------------
# 3. Conversation-memory prepend (Part 23 fix)
# ---------------------------------------------------------------------------

class TestConversationContextPrepend:
    def test_no_session_id_sends_task_text_unmodified(self, mock_llm):
        mock_llm.set_response("an answer")
        responder.run(task_text="what is 2+2?")
        user_content = mock_llm.mock.call_args.kwargs["user_content"]
        assert user_content == "what is 2+2?"

    def test_session_id_with_no_prior_context_sends_task_text_unmodified(self, mock_llm, fake_bus):
        # fake_bus is empty -- get_full_context() reads nothing back and
        # returns "" per its own docstring, so nothing should be prepended.
        mock_llm.set_response("an answer")
        responder.run(task_text="what is 2+2?", session_id="sess-1")
        user_content = mock_llm.mock.call_args.kwargs["user_content"]
        assert user_content == "what is 2+2?"

    def test_session_id_with_prior_context_is_prepended(self, monkeypatch, mock_llm):
        monkeypatch.setattr(
            "eo.conversation_memory.get_full_context",
            lambda session_id, *a, **k: "[user]: hi\n\n[assistant]: hello",
        )
        mock_llm.set_response("an answer")
        responder.run(task_text="who's older, me or you?", session_id="sess-1")
        user_content = mock_llm.mock.call_args.kwargs["user_content"]
        assert "Recent conversation:" in user_content
        assert "[user]: hi" in user_content
        assert user_content.endswith("Task: who's older, me or you?")

    def test_raw_task_text_argument_is_not_mutated_by_prepend(self, monkeypatch, mock_llm):
        # The prepend only changes what's SENT to the model -- the
        # function's own local `task_text` binding stays the original.
        calls = {}

        def fake_generate_text(**kwargs):
            calls["user_content"] = kwargs["user_content"]
            return "answer"

        monkeypatch.setattr(
            "eo.conversation_memory.get_full_context",
            lambda session_id, *a, **k: "some prior context",
        )
        mock_llm.mock.side_effect = lambda *a, **k: fake_generate_text(**k)
        result = responder.run(task_text="original task", session_id="sess-1")
        assert "original task" in calls["user_content"]
        assert result == "answer"


# ---------------------------------------------------------------------------
# 4. Return value / passthrough kwargs
# ---------------------------------------------------------------------------

class TestReturnValueAndPassthrough:
    def test_returns_stripped_answer(self, mock_llm):
        mock_llm.set_response("  an answer with padding  \n")
        result = responder.run(task_text="what is 2+2?")
        assert result == "an answer with padding"

    def test_forwards_path_and_domain_and_session_id(self, mock_llm):
        mock_llm.set_response("an answer")
        responder.run(task_text="hi", session_id="sess-9", path="instant", domain="coding")
        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["session_id"] == "sess-9"
        assert kwargs["path"] == "instant"
        assert kwargs["domain"] == "coding"
        assert kwargs["agent_name"] == "Responder"

    def test_system_prompt_is_forwarded(self, mock_llm):
        mock_llm.set_response("an answer")
        responder.run(task_text="hi")
        kwargs = mock_llm.mock.call_args.kwargs
        assert kwargs["system_prompt"] == responder.SYSTEM_PROMPT
