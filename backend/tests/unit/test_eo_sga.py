"""
tests/unit/test_eo_sga.py — Patch 7e-S6.

eo/sga.py had zero test coverage before this. Three things worth
pinning directly, per this module's own docstrings:

  - _requests_verification() / _requests_simulation_domain(): the two
    deterministic pre-checks that skip SGA entirely (zero LLM calls)
    for tasks it structurally cannot fulfill alone, regardless of how
    easy the underlying content is (Migration Part 26 gap fix, Test
    tab audit Bug 1). Getting either pattern set wrong either wastes
    a call on a task doomed to escalate anyway, or -- worse -- lets
    SGA answer something it can't actually deliver on.
  - _parse_structured_response(): must fail OPEN on malformed model
    output (bad JSON, missing "answer" key, non-string answer) to the
    documented {"answer": <raw text>, "memorable": False, "category":
    None} shape, never raise -- including the fenced-code-block strip
    and the stray-JSON-wrapper fallback (Bug 2 fix).
  - attempt()'s stage relay: resolves on the first non-ESCALATE
    result, degrades stage-by-stage on ESCALATE/exception, and the two
    pre-check skip paths return {"resolved": False} with zero calls
    into _call_one() at all.

Isolation: sga.py calls eo.conversation_memory.get_full_context()
directly off the imported module (`from eo import conversation_memory`)
-- fake_bus (conftest, autouse) already makes that call safe (returns
an empty string) without needing its own patch for tests that don't
care about conversation-context wiring specifically. _call_one() itself is
monkeypatched directly for attempt()'s own tests, since exercising the
real generate_text() chain is _parse_structured_response()'s job, not
attempt()'s.
"""
import json
from unittest.mock import MagicMock

import pytest

import eo.sga as sga


# ---------------------------------------------------------------------
# _requests_verification
# ---------------------------------------------------------------------

@pytest.mark.parametrize("task_text", [
    "Write a function and don't stop until a reviewer approves it.",
    "This requires a review before it's done.",
    "Get it reviewed by someone else first.",
    "Please peer-review this before finishing.",
    "Iterate until the tests pass and someone signs off.",
    "Keep improving until the panel approves.",
])
def test_requests_verification_true_for_review_phrasings(task_text):
    assert sga._requests_verification(task_text) is True


@pytest.mark.parametrize("task_text", [
    "What is 2+2?",
    "Write a Python function to reverse a linked list.",
    "Summarize this article for me.",
    "",
    None,
])
def test_requests_verification_false_for_ordinary_tasks(task_text):
    assert sga._requests_verification(task_text) is False


# ---------------------------------------------------------------------
# _requests_simulation_domain
# ---------------------------------------------------------------------

def test_requests_simulation_domain_true_for_a_fixed_dispatch_prefix():
    task = ("Simulate a focus group — an enthusiastic customer, a skeptical "
            "customer, and a professional critic, each reacting independently — "
            "to: the new $12/mo Pro tier.")
    assert sga._requests_simulation_domain(task) is True


def test_requests_simulation_domain_false_for_free_form_chat_mentioning_simulate():
    # Deliberately NOT anchored -- module docstring: only this tab's own
    # known dispatch shape, not general free-form text that happens to
    # mention simulation.
    task = "Can you simulate what a customer might think about this?"
    assert sga._requests_simulation_domain(task) is False


def test_requests_simulation_domain_false_for_ordinary_task():
    assert sga._requests_simulation_domain("What is 2+2?") is False


def test_requests_simulation_domain_handles_none():
    assert sga._requests_simulation_domain(None) is False


# ---------------------------------------------------------------------
# _parse_structured_response
# ---------------------------------------------------------------------

def test_parses_well_formed_json():
    raw = json.dumps({"answer": "The answer is 4.", "memorable": False, "category": None})
    result = sga._parse_structured_response(raw)
    assert result == {"answer": "The answer is 4.", "memorable": False, "category": None}


def test_strips_a_json_markdown_fence():
    raw = "```json\n" + json.dumps({"answer": "hi", "memorable": False, "category": None}) + "\n```"
    result = sga._parse_structured_response(raw)
    assert result["answer"] == "hi"


def test_memorable_true_with_valid_category_is_preserved():
    raw = json.dumps({"answer": "Use TypeScript for this.", "memorable": True, "category": "preference"})
    result = sga._parse_structured_response(raw)
    assert result == {"answer": "Use TypeScript for this.", "memorable": True, "category": "preference"}


def test_invalid_category_is_coerced_to_none():
    raw = json.dumps({"answer": "x", "memorable": True, "category": "not_a_real_category"})
    result = sga._parse_structured_response(raw)
    assert result["category"] is None


def test_memorable_false_forces_category_to_none_even_if_set():
    raw = json.dumps({"answer": "x", "memorable": False, "category": "idea"})
    result = sga._parse_structured_response(raw)
    assert result["category"] is None


def test_escalate_answer_is_never_memorable_even_if_flagged_true():
    raw = json.dumps({"answer": "ESCALATE", "memorable": True, "category": "idea"})
    result = sga._parse_structured_response(raw)
    assert result["memorable"] is False
    assert result["category"] is None


def test_malformed_json_fails_open_to_raw_text():
    raw = "not valid json at all"
    result = sga._parse_structured_response(raw)
    assert result == {"answer": "not valid json at all", "memorable": False, "category": None}


def test_json_missing_answer_key_fails_open():
    # Valid, brace-wrapped JSON but with no "answer" field to extract --
    # _strip_stray_json_wrapper()'s own regex finds nothing to pull out,
    # so this degrades all the way to "" rather than raising or leaking
    # the raw JSON text as if it were a real answer.
    raw = json.dumps({"memorable": True, "category": "idea"})
    result = sga._parse_structured_response(raw)
    assert result["answer"] == ""
    assert result["memorable"] is False


def test_non_string_answer_fails_open():
    # "answer" is present but not a string (42, unquoted in the JSON) --
    # the brace-wrapped extractor only matches a QUOTED "answer" value,
    # so this also degrades to "" rather than leaking the raw JSON text.
    raw = json.dumps({"answer": 42, "memorable": False, "category": None})
    result = sga._parse_structured_response(raw)
    assert result["answer"] == ""
    assert result["memorable"] is False


def test_bare_escalate_reply_with_no_json_wrapper_is_transparently_supported():
    result = sga._parse_structured_response("ESCALATE")
    assert result["answer"] == "ESCALATE"
    assert result["memorable"] is False


def test_stray_json_shaped_but_unparseable_text_extracts_answer_field():
    # Trailing comma makes this invalid JSON, but it's brace-wrapped and
    # has a recognizable "answer" field -- Bug 2 fix territory.
    raw = '{"answer": "partial text here", "memorable": false,}'
    result = sga._parse_structured_response(raw)
    assert result["answer"] == "partial text here"


def test_stray_json_shaped_with_no_extractable_answer_degrades_to_empty():
    raw = '{"memorable": false, "category": null,}'
    result = sga._parse_structured_response(raw)
    assert result["answer"] == ""


def test_non_json_shaped_malformed_text_is_left_untouched():
    raw = "The answer is 4, roughly."
    result = sga._parse_structured_response(raw)
    assert result["answer"] == "The answer is 4, roughly."


# ---------------------------------------------------------------------
# attempt() — pre-check short-circuits (zero SGA calls)
# ---------------------------------------------------------------------

def test_attempt_escalates_with_zero_calls_for_verification_request(monkeypatch):
    call_one_mock = MagicMock()
    monkeypatch.setattr(sga, "_call_one", call_one_mock)

    result = sga.attempt("Write code and don't stop until a reviewer approves it.")

    assert result == {"resolved": False}
    call_one_mock.assert_not_called()


def test_attempt_escalates_with_zero_calls_for_simulation_dispatch(monkeypatch):
    call_one_mock = MagicMock()
    monkeypatch.setattr(sga, "_call_one", call_one_mock)

    task = ("Simulate a focus group — an enthusiastic customer, a skeptical "
            "customer, and a professional critic, each reacting independently — "
            "to: something.")
    result = sga.attempt(task)

    assert result == {"resolved": False}
    call_one_mock.assert_not_called()


# ---------------------------------------------------------------------
# attempt() — stage relay
# ---------------------------------------------------------------------

def test_attempt_resolves_at_stage_one_on_a_confident_answer(monkeypatch):
    monkeypatch.setattr(sga, "_call_one", MagicMock(
        return_value={"answer": "It's 4.", "memorable": False, "category": None}))

    result = sga.attempt("What is 2+2?")

    assert result == {"resolved": True, "answer": "It's 4.", "memorable": False, "category": None}
    assert sga._call_one.call_count == 1


def test_attempt_escalates_when_every_stage_returns_escalate(monkeypatch):
    monkeypatch.setattr(sga, "_call_one", MagicMock(
        return_value={"answer": "ESCALATE", "memorable": False, "category": None}))
    monkeypatch.setattr(sga, "STAGE_TIMEOUTS", {1: 0, 2: 0, 3: 0})

    result = sga.attempt("Some hard task")

    assert result == {"resolved": False}


def test_attempt_escalates_when_call_one_raises_at_every_stage(monkeypatch):
    monkeypatch.setattr(sga, "_call_one", MagicMock(side_effect=RuntimeError("provider down")))
    monkeypatch.setattr(sga, "STAGE_TIMEOUTS", {1: 0, 2: 0, 3: 0})

    result = sga.attempt("Some task")  # must not raise

    assert result == {"resolved": False}


def test_attempt_falls_through_to_a_later_stage_that_resolves(monkeypatch):
    # First agent always escalates; whichever agent joins at stage 2
    # onward returns a real answer -- attempt() should pick that up
    # without raising once the deadline allows a second stage.
    calls = {"n": 0}

    def fake_call_one(agent_key, task_text, session_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"answer": "ESCALATE", "memorable": False, "category": None}
        return {"answer": "Second stage got it.", "memorable": False, "category": None}

    monkeypatch.setattr(sga, "_call_one", fake_call_one)
    monkeypatch.setattr(sga, "STAGE_TIMEOUTS", {1: 999, 2: 999, 3: 999})

    result = sga.attempt("Some task")

    assert result["resolved"] is True
    assert result["answer"] == "Second stage got it."


# ---------------------------------------------------------------------
# _rotate_start
# ---------------------------------------------------------------------

def test_rotate_start_cycles_which_agent_leads():
    orders = [sga._rotate_start() for _ in range(3)]
    leaders = [order[0] for order in orders]
    assert set(leaders) == {"sga_1", "sga_2", "sga_3"}
    for order in orders:
        assert set(order) == {"sga_1", "sga_2", "sga_3"}
        assert len(order) == 3
