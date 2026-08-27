"""
tests/unit/test_eo_fact_summarizer.py — Patch 7e (content/knowledge group).

eo/fact_summarizer.py had zero test coverage before this. extract_fact()
is explicitly documented to "never raise" -- it's called once per
tier-2/3 task from api/task_runner.py, fail-open, so callers can treat
any bad path (LLM/parse error, malformed JSON, unrecognized category,
missing title/summary, worth_remembering: false) identically: skip the
write, don't block the task response. A regression that turned any of
those into an uncaught exception would break the task response itself,
not just the fact-write, which is what makes this worth pinning down
directly rather than trusting by inspection.

Isolation: fact_summarizer.py does `from utils.llm_client import
generate_text` (a bound name in its own namespace) -- tests patch
`generate_text` on the fact_summarizer module object itself, same
gotcha as every other LLM-calling module in this batch (conftest's
`mock_llm` fixture also works here since it sweeps every module holding
a bound reference, but these tests patch directly for tighter control
over call args/exceptions per test).
"""
import pytest

from eo import fact_summarizer

# ---------------------------------------------------------------------
# _parse
# ---------------------------------------------------------------------

def test_parse_plain_json():
    result = fact_summarizer._parse('{"worth_remembering": true, "category": "idea", "title": "t", "summary": "s"}')
    assert result == {"worth_remembering": True, "category": "idea", "title": "t", "summary": "s"}


def test_parse_strips_a_json_fenced_code_block():
    raw = '```json\n{"worth_remembering": false, "category": "", "title": "", "summary": ""}\n```'
    result = fact_summarizer._parse(raw)
    assert result == {"worth_remembering": False, "category": "", "title": "", "summary": ""}


def test_parse_strips_a_bare_fenced_code_block_with_no_json_tag():
    raw = '```\n{"worth_remembering": true, "category": "context", "title": "t", "summary": "s"}\n```'
    result = fact_summarizer._parse(raw)
    assert result["worth_remembering"] is True


def test_parse_handles_none_input_as_empty_string():
    with pytest.raises(Exception):
        fact_summarizer._parse(None)


def test_parse_raises_on_malformed_json():
    with pytest.raises(Exception):
        fact_summarizer._parse("not json at all")


# ---------------------------------------------------------------------
# extract_fact — happy path
# ---------------------------------------------------------------------

def test_extract_fact_returns_the_parsed_dict_when_worth_remembering(monkeypatch):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: '{"worth_remembering": true, "category": "decision", "title": "Use TS", "summary": "Always use TypeScript."}',
    )
    result = fact_summarizer.extract_fact("some task", "some answer")
    assert result == {
        "worth_remembering": True,
        "category": "decision",
        "title": "Use TS",
        "summary": "Always use TypeScript.",
        "profile_signals": [],
    }


def test_extract_fact_passes_the_chain_and_agent_name_through_to_generate_text(monkeypatch):
    seen = {}

    def fake_generate_text(**kwargs):
        seen.update(kwargs)
        return '{"worth_remembering": false, "category": "", "title": "", "summary": ""}'

    monkeypatch.setattr(fact_summarizer, "generate_text", fake_generate_text)
    fact_summarizer.extract_fact("task", "answer", session_id="sess-1")

    assert seen["chain"] == fact_summarizer.CHAIN
    assert seen["agent_name"] == "fact_summarizer"
    assert seen["session_id"] == "sess-1"
    assert "task" in seen["user_content"]
    assert "answer" in seen["user_content"]


# ---------------------------------------------------------------------
# extract_fact — fail-open cases, each returns None and never raises
# ---------------------------------------------------------------------

def test_extract_fact_returns_none_when_worth_remembering_is_false(monkeypatch):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: '{"worth_remembering": false, "category": "", "title": "", "summary": ""}',
    )
    assert fact_summarizer.extract_fact("task", "answer") is None


def test_extract_fact_returns_none_when_the_llm_call_raises(monkeypatch):
    def boom(**k):
        raise RuntimeError("all providers exhausted")
    monkeypatch.setattr(fact_summarizer, "generate_text", boom)
    assert fact_summarizer.extract_fact("task", "answer") is None


def test_extract_fact_returns_none_on_malformed_json(monkeypatch):
    monkeypatch.setattr(fact_summarizer, "generate_text", lambda **k: "not valid json")
    assert fact_summarizer.extract_fact("task", "answer") is None


def test_extract_fact_returns_none_when_response_is_not_a_json_object(monkeypatch):
    """A syntactically valid JSON value that isn't a dict (e.g. a bare
    JSON array or string) must be treated the same as a parse failure,
    not passed through to `.get()` calls that would raise on a
    non-dict."""
    monkeypatch.setattr(fact_summarizer, "generate_text", lambda **k: '["not", "a", "dict"]')
    assert fact_summarizer.extract_fact("task", "answer") is None


def test_extract_fact_returns_none_for_an_unrecognized_category(monkeypatch):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: '{"worth_remembering": true, "category": "nonsense", "title": "t", "summary": "s"}',
    )
    assert fact_summarizer.extract_fact("task", "answer") is None


@pytest.mark.parametrize("category", ["decision", "preference", "idea", "context"])
def test_extract_fact_accepts_every_category_in_category_to_section(monkeypatch, category):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: f'{{"worth_remembering": true, "category": "{category}", "title": "t", "summary": "s"}}',
    )
    result = fact_summarizer.extract_fact("task", "answer")
    assert result is not None
    assert result["category"] == category


def test_extract_fact_returns_none_when_title_is_missing(monkeypatch):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: '{"worth_remembering": true, "category": "idea", "title": "", "summary": "s"}',
    )
    assert fact_summarizer.extract_fact("task", "answer") is None


def test_extract_fact_returns_none_when_summary_is_missing(monkeypatch):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: '{"worth_remembering": true, "category": "idea", "title": "t", "summary": ""}',
    )
    assert fact_summarizer.extract_fact("task", "answer") is None


def test_extract_fact_never_raises_even_on_completely_unexpected_output(monkeypatch):
    """Belt-and-suspenders: the docstring promises 'this function never
    raises' -- confirm that promise holds even for a response that's
    neither valid JSON nor a fenced block, exercising the fail-open
    except branch end to end."""
    monkeypatch.setattr(fact_summarizer, "generate_text", lambda **k: "")
    assert fact_summarizer.extract_fact("task", "answer") is None


# ---------------------------------------------------------------------
# _validate_profile_signals — Patch B2
# ---------------------------------------------------------------------

def test_validate_profile_signals_returns_empty_list_for_non_list_input():
    assert fact_summarizer._validate_profile_signals(None) == []
    assert fact_summarizer._validate_profile_signals("not a list") == []
    assert fact_summarizer._validate_profile_signals({"type": "like"}) == []


def test_validate_profile_signals_keeps_a_well_formed_entry():
    raw = [{"type": "like", "key": "diagrams", "value": "prefers visual explanations", "explicit": True}]
    result = fact_summarizer._validate_profile_signals(raw)
    assert result == [{
        "type": "like", "key": "diagrams",
        "value": "prefers visual explanations", "explicit": True,
    }]


def test_validate_profile_signals_defaults_explicit_to_false_when_absent():
    raw = [{"type": "dislike", "key": "verbose output", "value": "said it was too long"}]
    result = fact_summarizer._validate_profile_signals(raw)
    assert result[0]["explicit"] is False


def test_validate_profile_signals_drops_entries_with_an_unrecognized_type():
    raw = [{"type": "not_a_real_type", "key": "x", "value": "y"}]
    assert fact_summarizer._validate_profile_signals(raw) == []


def test_validate_profile_signals_drops_entries_missing_value():
    raw = [{"type": "like", "key": "diagrams", "value": ""}]
    assert fact_summarizer._validate_profile_signals(raw) == []


def test_validate_profile_signals_drops_non_format_preference_entries_missing_key():
    raw = [{"type": "expertise_signal", "key": "", "value": "intermediate"}]
    assert fact_summarizer._validate_profile_signals(raw) == []


def test_validate_profile_signals_drops_non_dict_items_and_keeps_valid_ones():
    raw = ["not a dict", {"type": "like", "key": "dark mode", "value": "said they love it", "explicit": True}]
    result = fact_summarizer._validate_profile_signals(raw)
    assert len(result) == 1
    assert result[0]["key"] == "dark mode"


def test_validate_profile_signals_format_preference_does_not_require_a_key():
    raw = [{"type": "format_preference", "value": "diagram", "explicit": True}]
    result = fact_summarizer._validate_profile_signals(raw)
    assert result == [{
        "type": "format_preference", "key": "default_format",
        "value": "diagram", "explicit": True,
    }]


@pytest.mark.parametrize("signal_type", list(fact_summarizer.PROFILE_SIGNAL_TYPES))
def test_validate_profile_signals_accepts_every_known_type(signal_type):
    raw = [{"type": signal_type, "key": "some-key", "value": "some-value", "explicit": False}]
    result = fact_summarizer._validate_profile_signals(raw)
    assert len(result) == 1
    assert result[0]["type"] == signal_type


# ---------------------------------------------------------------------
# extract_fact — profile_signals integration, Patch B2
# ---------------------------------------------------------------------

def test_extract_fact_includes_empty_profile_signals_by_default(monkeypatch):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: '{"worth_remembering": true, "category": "idea", "title": "t", "summary": "s"}',
    )
    result = fact_summarizer.extract_fact("task", "answer")
    assert result["profile_signals"] == []


def test_extract_fact_carries_profile_signals_through_alongside_a_workspace_fact(monkeypatch):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: (
            '{"worth_remembering": true, "category": "decision", "title": "Use TS", '
            '"summary": "Always use TypeScript.", '
            '"profile_signals": [{"type": "expertise_signal", "key": "React", '
            '"value": "intermediate", "explicit": false}]}'
        ),
    )
    result = fact_summarizer.extract_fact("task", "answer")
    assert result["worth_remembering"] is True
    assert result["profile_signals"] == [{
        "type": "expertise_signal", "key": "React",
        "value": "intermediate", "explicit": False,
    }]


def test_extract_fact_returns_a_signal_only_result_when_not_worth_remembering(monkeypatch):
    """The whole point of decoupling the two halves: an aside about the
    user's own preference on an otherwise trivial, not-worth-
    remembering task must still reach the caller."""
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: (
            '{"worth_remembering": false, "category": "", "title": "", "summary": "", '
            '"profile_signals": [{"type": "like", "key": "dark mode", '
            '"value": "mentioned liking it", "explicit": true}]}'
        ),
    )
    result = fact_summarizer.extract_fact("task", "answer")
    assert result is not None
    assert result["worth_remembering"] is False
    assert result["category"] == ""
    assert result["profile_signals"] == [{
        "type": "like", "key": "dark mode",
        "value": "mentioned liking it", "explicit": True,
    }]


def test_extract_fact_returns_none_when_neither_half_has_anything(monkeypatch):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: '{"worth_remembering": false, "category": "", "title": "", "summary": "", "profile_signals": []}',
    )
    assert fact_summarizer.extract_fact("task", "answer") is None


def test_extract_fact_still_surfaces_profile_signals_when_category_is_invalid(monkeypatch):
    """An invalid workspace-fact half must not swallow a valid profile
    signal riding alongside it in the same response."""
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: (
            '{"worth_remembering": true, "category": "nonsense", "title": "t", "summary": "s", '
            '"profile_signals": [{"type": "dislike", "key": "verbose answers", '
            '"value": "said it was too much", "explicit": true}]}'
        ),
    )
    result = fact_summarizer.extract_fact("task", "answer")
    assert result is not None
    assert result["worth_remembering"] is False
    assert result["category"] == ""
    assert len(result["profile_signals"]) == 1


def test_extract_fact_never_raises_on_a_malformed_profile_signals_field(monkeypatch):
    monkeypatch.setattr(
        fact_summarizer, "generate_text",
        lambda **k: '{"worth_remembering": false, "category": "", "title": "", "summary": "", "profile_signals": "oops"}',
    )
    assert fact_summarizer.extract_fact("task", "answer") is None
