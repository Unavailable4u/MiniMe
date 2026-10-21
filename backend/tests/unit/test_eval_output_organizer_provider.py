"""
tests/unit/test_eval_output_organizer_provider.py

Covers evals/promptfoo/providers/output_organizer_provider.py's chain
pinning. In CI that eval hung on every case until promptfoo's 120s
per-test timeout because organize_final_answer() was building a
quota-ranked chain over the whole production account pool (see the
provider's own PINNED_STEP comment). The provider now forces one
deterministic Groq step -- same approach role_provider.py's
provider_override uses for the Role Library evals -- unless a case/
config opts back in with `live_chain: true`.

No network, no keys: agents.output_organizer.generate_text is replaced
with a fake that records the chain it was handed.
"""
import importlib.util
import json
from pathlib import Path

import pytest

import agents.output_organizer as output_organizer
import eo.dynamic_chain as dynamic_chain

_PROVIDER_PATH = (Path(__file__).resolve().parents[2]
                  / "evals" / "promptfoo" / "providers" / "output_organizer_provider.py")

ROLE_OUTPUTS = {
    "implementer": {"text": "Added a validate_email() helper."},
    "reviewer": {"text": "One issue: the helper also needs a length check."},
}


@pytest.fixture(scope="module")
def provider():
    spec = importlib.util.spec_from_file_location("output_organizer_provider_under_test", _PROVIDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def captured(monkeypatch):
    """Replaces generate_text and records the chain organize_final_answer()
    hands it."""
    seen = {}

    def _fake_generate_text(system_prompt, user_content, chain, agent_name="Agent", **kwargs):
        seen["chain"] = chain
        seen["agent_name"] = agent_name
        return "Merged answer.\n---DEDUP_NOTES---\n{}"

    monkeypatch.setattr(output_organizer, "generate_text", _fake_generate_text)
    return seen


def _context():
    return {"vars": {"role_outputs": ROLE_OUTPUTS, "user_request": "Review this login form."}}


def test_default_run_uses_the_single_pinned_groq_step(provider, captured, monkeypatch):
    # If the pin were NOT applied, this would be what organize_final_answer()
    # picked up instead -- make that unmistakably different.
    monkeypatch.setattr(dynamic_chain, "build_fallback_chain",
                        lambda role, *a, **k: [{"provider": "mistral", "model": "x", "key_env": "NOPE"}])

    result = provider.call_api("{{user_request}}", {}, _context())

    assert captured["chain"] == [provider.PINNED_STEP]
    assert captured["agent_name"] == "output_organizer"
    assert json.loads(result["output"]) == {"answer": "Merged answer.", "dedup_notes": {}}
    assert result["metadata"]["role_count"] == 2


def test_pinned_step_matches_the_ci_scoped_generation_key(provider):
    # GROQ_API_KEY is the CI-scoped *generation* secret; GROQ_API_KEY_GRADING
    # is reserved for llm-rubric grading, so generation must not use it.
    assert provider.PINNED_STEP["provider"] == "groq"
    assert provider.PINNED_STEP["key_env"] == "GROQ_API_KEY"


def test_pin_is_restored_after_the_call(provider, captured):
    original = dynamic_chain.build_fallback_chain

    provider.call_api("{{user_request}}", {}, _context())

    assert dynamic_chain.build_fallback_chain is original


def test_pin_is_restored_even_when_the_call_fails(provider, monkeypatch):
    original = dynamic_chain.build_fallback_chain

    def _boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(output_organizer, "generate_text", _boom)

    result = provider.call_api("{{user_request}}", {}, _context())

    assert "provider exploded" in result["error"]
    assert dynamic_chain.build_fallback_chain is original


def test_live_chain_config_opts_back_into_the_real_chain_builder(provider, captured, monkeypatch):
    live = [{"provider": "mistral", "model": "m", "key_env": "MISTRAL_API_KEY"}]
    monkeypatch.setattr(dynamic_chain, "build_fallback_chain", lambda role, *a, **k: live)

    provider.call_api("{{user_request}}", {"config": {"live_chain": True}}, _context())

    assert captured["chain"] == live


def test_live_chain_test_var_also_opts_in(provider, captured, monkeypatch):
    live = [{"provider": "gemini", "model": "m", "key_env": "GEMINI_API_KEY_1"}]
    monkeypatch.setattr(dynamic_chain, "build_fallback_chain", lambda role, *a, **k: live)
    context = _context()
    context["vars"]["live_chain"] = True

    provider.call_api("{{user_request}}", {}, context)

    assert captured["chain"] == live
