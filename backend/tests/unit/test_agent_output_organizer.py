"""
tests/unit/test_agent_output_organizer.py — Patch 7f-5.

Covers agents/output_organizer.py:

  1. _parse_organizer_response() — splits SYSTEM_PROMPT's two-part
     contract (markdown answer + ---DEDUP_NOTES--- + one JSON line) and
     degrades gracefully (marker missing / notes line malformed / notes
     parsed to a non-dict) without ever losing the answer text itself.
  2. organize_final_answer() — the 0/1-role defensive short-circuits
     (no LLM call), and the real multi-role synthesis path via the
     shared `mock_llm` fixture (patches utils.llm_client.generate_text
     everywhere it's bound, per conftest.py).
  3. organize_final_answer_stream() — the async-generator twin; only
     yields text chunks (never the dedup-notes JSON), driven here with
     a fake `stream_completion` patched directly onto the module (no
     shared fixture exists for it — this is the only module that
     imports it).

TRACING_ENABLED is False in this test environment (no Langfuse env vars
set), so _open_organizer_span() is a no-op and doesn't need mocking.

build_fallback_chain() is faked via the `fake_fallback_chain` fixture
below for every test that reaches the deferred `from eo.dynamic_chain
import build_fallback_chain` call site (both multi-role
organize_final_answer() tests and the multi-role
organize_final_answer_stream() test) — that call resolves through
eo.quota_sentinel.get_quota_snapshot(), which pulls model defaults from
agents.generic_worker at call time. That import path is unrelated to
anything this module owns, so it's out of scope for these tests
regardless; faking it directly (forcing FALLBACK_CHAIN, the module's
own documented last-resort) keeps this file testing output_organizer's
own merge/parse/stream logic only, not eo.dynamic_chain's account-
ranking internals.
"""
import json

import pytest

import agents.output_organizer as output_organizer


@pytest.fixture
def fake_fallback_chain(monkeypatch):
    """Forces organize_final_answer()/organize_final_answer_stream()'s
    `build_fallback_chain(...) or FALLBACK_CHAIN` onto FALLBACK_CHAIN,
    without touching eo.quota_sentinel/agents.generic_worker at all.
    Patched on eo.dynamic_chain itself (not on output_organizer) since
    the real call site does a deferred, function-body import that
    re-resolves the name from that module at call time."""
    import eo.dynamic_chain as dynamic_chain_module
    monkeypatch.setattr(dynamic_chain_module, "build_fallback_chain", lambda *a, **k: [])


# ---------------------------------------------------------------------------
# 1. _parse_organizer_response()
# ---------------------------------------------------------------------------

class TestParseOrganizerResponse:
    def test_no_marker_returns_whole_response_as_answer(self):
        result = output_organizer._parse_organizer_response("Just the merged answer, no marker.")
        assert result == {"answer": "Just the merged answer, no marker.", "dedup_notes": {}}

    def test_splits_answer_and_valid_dedup_notes(self):
        raw = (
            "The merged answer text.\n"
            "---DEDUP_NOTES---\n"
            '{"reviewer": "folded into implementer\'s section"}'
        )
        result = output_organizer._parse_organizer_response(raw)
        assert result["answer"] == "The merged answer text."
        assert result["dedup_notes"] == {"reviewer": "folded into implementer's section"}

    def test_malformed_json_notes_degrade_to_empty_dict_without_losing_answer(self):
        raw = "The merged answer.\n---DEDUP_NOTES---\nnot valid json at all"
        result = output_organizer._parse_organizer_response(raw)
        assert result["answer"] == "The merged answer."
        assert result["dedup_notes"] == {}

    def test_notes_json_that_parses_to_a_list_degrades_to_empty_dict(self):
        raw = "The merged answer.\n---DEDUP_NOTES---\n[\"not\", \"a\", \"dict\"]"
        result = output_organizer._parse_organizer_response(raw)
        assert result["answer"] == "The merged answer."
        assert result["dedup_notes"] == {}

    def test_empty_object_notes_parsed_correctly(self):
        raw = "Answer.\n---DEDUP_NOTES---\n{}"
        result = output_organizer._parse_organizer_response(raw)
        assert result["dedup_notes"] == {}


# ---------------------------------------------------------------------------
# 2. organize_final_answer()
# ---------------------------------------------------------------------------

class TestOrganizeFinalAnswerShortCircuits:
    def test_empty_role_outputs_returns_blank_answer_no_llm_call(self, mock_llm):
        result = output_organizer.organize_final_answer({}, user_request="anything")
        assert result == {"answer": "", "dedup_notes": {}}
        mock_llm.mock.assert_not_called()

    def test_single_role_renders_directly_without_llm_call(self, mock_llm):
        role_outputs = {"writer": {"text": "Here is the single answer."}}
        result = output_organizer.organize_final_answer(role_outputs, user_request="anything")
        assert result == {"answer": "Here is the single answer.", "dedup_notes": {}}
        mock_llm.mock.assert_not_called()


class TestOrganizeFinalAnswerSynthesis:
    def test_multi_role_calls_llm_and_returns_parsed_answer_and_notes(self, mock_llm, fake_fallback_chain):
        mock_llm.set_response(
            "The organized, merged answer.\n"
            "---DEDUP_NOTES---\n"
            '{"reviewer": "restated implementer\'s point, dropped as duplicate"}'
        )
        role_outputs = {
            "implementer": {"text": "Implemented the feature."},
            "reviewer": {"text": "Looks good, implemented correctly."},
        }
        result = output_organizer.organize_final_answer(
            role_outputs, user_request="Build the feature", final_role="implementer",
        )
        assert result["answer"] == "The organized, merged answer."
        assert result["dedup_notes"] == {"reviewer": "restated implementer's point, dropped as duplicate"}

        call_kwargs = mock_llm.mock.call_args.kwargs
        assert call_kwargs["system_prompt"] == output_organizer.SYSTEM_PROMPT
        assert call_kwargs["agent_name"] == "output_organizer"
        assert "Build the feature" in call_kwargs["user_content"]
        assert "implementer (final role)" in call_kwargs["user_content"]
        assert "Implemented the feature." in call_kwargs["user_content"]
        assert "Looks good, implemented correctly." in call_kwargs["user_content"]

    def test_response_with_no_dedup_marker_still_returns_full_answer(self, mock_llm, fake_fallback_chain):
        mock_llm.set_response("A merged answer with no trailing marker at all.")
        role_outputs = {
            "a": {"text": "First role output."},
            "b": {"text": "Second role output."},
        }
        result = output_organizer.organize_final_answer(role_outputs, user_request="Do the thing")
        assert result["answer"] == "A merged answer with no trailing marker at all."
        assert result["dedup_notes"] == {}


# ---------------------------------------------------------------------------
# 3. organize_final_answer_stream()
# ---------------------------------------------------------------------------

async def _fake_stream_completion(system_prompt, user_content, chain, agent_name="Agent",
                                   session_id=None, tier=None, path=None, domain=None):
    for chunk in ["chunk-one ", "chunk-two ", "chunk-three"]:
        yield chunk


async def _collect(agen):
    return [chunk async for chunk in agen]


class TestOrganizeFinalAnswerStream:
    def test_empty_role_outputs_yields_nothing(self):
        import asyncio
        chunks = asyncio.run(_collect(output_organizer.organize_final_answer_stream({}, user_request="x")))
        assert chunks == []

    def test_single_role_yields_rendered_text_without_streaming_call(self, monkeypatch):
        import asyncio
        called = []
        monkeypatch.setattr(output_organizer, "stream_completion",
                             lambda *a, **k: called.append(True) or _fake_stream_completion(*a, **k))
        role_outputs = {"writer": {"text": "Solo answer."}}
        chunks = asyncio.run(_collect(
            output_organizer.organize_final_answer_stream(role_outputs, user_request="x")
        ))
        assert chunks == ["Solo answer."]
        assert called == []

    def test_multi_role_yields_streamed_chunks_only_no_dedup_json(self, monkeypatch, fake_fallback_chain):
        import asyncio
        monkeypatch.setattr(output_organizer, "stream_completion", _fake_stream_completion)
        role_outputs = {
            "a": {"text": "First."},
            "b": {"text": "Second."},
        }
        chunks = asyncio.run(_collect(
            output_organizer.organize_final_answer_stream(role_outputs, user_request="Do the thing")
        ))
        assert chunks == ["chunk-one ", "chunk-two ", "chunk-three"]
        assert "DEDUP_NOTES" not in "".join(chunks)
