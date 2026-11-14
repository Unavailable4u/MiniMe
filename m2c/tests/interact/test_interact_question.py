"""
m2c/tests/interact/test_interact_question.py -- Patch L01a: the ``ask_user`` request, the answer and their rules.

Pure functions only (no runner, no link): ``parse_question`` turns the model's arguments into a ``UserQuestion`` or
fails with a FIXED message that names the rule and never repeats the model's text; ``answer_text`` checks what the
screen handed back and turns it into the text the model reads.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from m2c.interact import question as q
from m2c.interact.question import (
    Declined,
    QuestionError,
    QuestionOption,
    UserAnswer,
    UserQuestion,
    answer_text,
    parse_question,
)

CANARY = "zq-canary-" + "7f3a9c"                      # a marker that must never be echoed in an error


def opts(*labels):
    return [{"label": label} for label in labels]


# ---- the types ---------------------------------------------------------------------------------------------

def test_the_types_are_frozen_values():
    question = UserQuestion("Which?", (QuestionOption("A"),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        question.question = "other"                                             # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        UserAnswer(("A",)).choices = ()                                         # type: ignore[misc]
    assert Declined() == Declined()


def test_the_defaults_match_the_schema():
    question = UserQuestion("Which?")
    assert question.options == () and question.allow_free_text is True and question.timeout_s == 600
    assert (q.MAX_OPTIONS, q.MIN_TIMEOUT_S, q.MAX_TIMEOUT_S, q.DEFAULT_TIMEOUT_S) == (6, 1, 3600, 600)


# ---- parse_question: good input ----------------------------------------------------------------------------

def test_a_bare_question_gets_the_schema_defaults():
    parsed = parse_question({"question": "Which database?"})
    assert parsed == UserQuestion("Which database?", (), True, 600)


def test_options_and_every_field_are_carried_over():
    parsed = parse_question({
        "question": "Which database?", "allow_free_text": False, "timeout_s": 30,
        "options": [{"label": "SQLite", "description": "one file"}, {"label": "Postgres"}],
    })
    assert parsed.options == (QuestionOption("SQLite", "one file"), QuestionOption("Postgres", ""))
    assert parsed.allow_free_text is False and parsed.timeout_s == 30


def test_a_multi_line_question_is_allowed_but_the_labels_are_single_line():
    assert parse_question({"question": "Line one\nLine two\tTabbed"}).question == "Line one\nLine two\tTabbed"
    with pytest.raises(QuestionError):
        parse_question({"question": "ok", "options": opts("two\nlines")})


def test_the_limits_are_inclusive():
    parse_question({"question": "x" * q.MAX_QUESTION_CHARS})
    parse_question({"question": "q", "options": opts(*[f"o{i}" for i in range(6)])})
    parse_question({"question": "q", "options": [{"label": "l" * q.MAX_LABEL_CHARS,
                                                  "description": "d" * q.MAX_OPTION_DESCRIPTION_CHARS}]})
    parse_question({"question": "q", "timeout_s": 1})
    parse_question({"question": "q", "timeout_s": 3600})


# ---- parse_question: every rule fails closed with a fixed message ------------------------------------------

BAD = {
    "question_missing": {},
    "question_not_text": {"question": 5},
    "question_empty": {"question": ""},
    "question_blank": {"question": "   \n\t "},
    "question_too_long": {"question": "x" * (q.MAX_QUESTION_CHARS + 1)},
    "question_control_chars": {"question": "hello \x1b[31mred"},
    "options_not_a_list": {"question": "q", "options": "A,B"},
    "too_many_options": {"question": "q", "options": opts(*[f"o{i}" for i in range(7)])},
    "option_not_an_object": {"question": "q", "options": ["A"]},
    "option_without_label": {"question": "q", "options": [{"description": "d"}]},
    "option_empty_label": {"question": "q", "options": opts("")},
    "option_label_too_long": {"question": "q", "options": opts("l" * (q.MAX_LABEL_CHARS + 1))},
    "option_label_control": {"question": "q", "options": opts("a\x1bb")},
    "option_extra_key": {"question": "q", "options": [{"label": "A", "value": 1}]},
    "option_description_not_text": {"question": "q", "options": [{"label": "A", "description": 3}]},
    "option_description_too_long": {"question": "q", "options": [{"label": "A", "description": "d" * 301}]},
    "duplicate_labels": {"question": "q", "options": opts("A", "A")},
    "free_text_off_without_options": {"question": "q", "allow_free_text": False},
    "free_text_off_with_empty_options": {"question": "q", "allow_free_text": False, "options": []},
    "free_text_not_a_bool": {"question": "q", "allow_free_text": "yes"},
    "timeout_not_an_int": {"question": "q", "timeout_s": "30"},
    "timeout_is_a_bool": {"question": "q", "timeout_s": True},
    "timeout_too_small": {"question": "q", "timeout_s": 0},
    "timeout_too_large": {"question": "q", "timeout_s": 3601},
    "unknown_argument": {"question": "q", "colour": "red"},
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_input_fails_with_a_fixed_message(name):
    with pytest.raises(QuestionError) as err:
        parse_question(BAD[name])
    message = err.value.message
    assert message.startswith("error: invalid question: ") and message == str(err.value)
    assert "\n" not in message and len(message) < 200


def test_error_messages_never_repeat_the_models_text():
    for args in (
        {"question": CANARY + "\x1b"},
        {"question": "q", "options": opts(CANARY, CANARY)},
        {"question": "q", "options": [{"label": CANARY, "extra": CANARY}]},
        {"question": "q", "options": [{"label": "A", "description": CANARY * 200}]},
        {"question": "q", CANARY: 1},
        {"question": CANARY * 400},
    ):
        with pytest.raises(QuestionError) as err:
            parse_question(args)
        assert CANARY not in err.value.message


def test_the_messages_are_distinct_per_rule():
    seen = {}
    for name, args in BAD.items():
        with pytest.raises(QuestionError) as err:
            parse_question(args)
        seen.setdefault(err.value.message, []).append(name)
    # a handful of rules share one message on purpose (all the label rules, say) but each message is a real rule
    assert len(seen) >= 8, seen


def test_the_arguments_must_be_a_mapping():
    for junk in (None, [], "q", 5):
        with pytest.raises(QuestionError):
            parse_question(junk)                                                # type: ignore[arg-type]


# ---- answer_text: what the model reads ---------------------------------------------------------------------

ASK = UserQuestion("Which database?", (QuestionOption("SQLite"), QuestionOption("Postgres"), QuestionOption("Redis")))


def test_one_chosen_option():
    text, truncated = answer_text(ASK, UserAnswer(choices=("Postgres",)))
    assert text == "The person answered.\nChose: Postgres" and truncated is False


def test_several_chosen_options_come_back_in_the_order_given():
    text, _ = answer_text(ASK, UserAnswer(choices=("Redis", "SQLite")))
    assert text == "The person answered.\nChose: Redis\nChose: SQLite"


def test_free_text():
    text, truncated = answer_text(ASK, UserAnswer(text="Use DuckDB\nit is embedded"))
    assert text == "The person answered.\nWrote: Use DuckDB\nit is embedded" and truncated is False


def test_an_option_and_free_text_together():
    text, _ = answer_text(ASK, UserAnswer(choices=("SQLite",), text="but keep it small"))
    assert text == "The person answered.\nChose: SQLite\nWrote: but keep it small"


def test_an_open_question_takes_free_text():
    text, _ = answer_text(UserQuestion("Name it?"), UserAnswer(text="Orion"))
    assert text == "The person answered.\nWrote: Orion"


def test_a_very_long_answer_is_cut_and_flagged():
    text, truncated = answer_text(UserQuestion("Paste it?"), UserAnswer(text="y" * (q.MAX_ANSWER_CHARS + 500)))
    assert truncated is True and "y" * q.MAX_ANSWER_CHARS in text and "y" * (q.MAX_ANSWER_CHARS + 1) not in text
    assert text.endswith(q.ANSWER_CUT_NOTICE)


@pytest.mark.parametrize("answer", [
    UserAnswer(),                                            # nothing at all
    UserAnswer(text=""),                                     # empty words
    UserAnswer(text="   \n"),                                # blank words
    UserAnswer(choices=("Mongo",)),                          # not an offered label
    UserAnswer(choices=("SQLite", "SQLite")),                # the same label twice
    UserAnswer(choices=("sqlite",)),                         # labels match exactly
    UserAnswer(choices=["SQLite"]),                          # not a tuple      # type: ignore[arg-type]
    UserAnswer(choices=(5,)),                                # not text         # type: ignore[arg-type]
    UserAnswer(text=5),                                      # not text         # type: ignore[arg-type]
    "SQLite",                                                # not an answer at all
    None,
    Declined(),                                              # a decline is handled before, never formatted
])
def test_a_malformed_answer_is_refused(answer):
    with pytest.raises(QuestionError) as err:
        answer_text(ASK, answer)                                                # type: ignore[arg-type]
    assert err.value.message == q.BAD_ANSWER


def test_free_text_is_refused_when_the_question_did_not_allow_it():
    closed = UserQuestion("Which?", (QuestionOption("A"), QuestionOption("B")), allow_free_text=False)
    assert answer_text(closed, UserAnswer(choices=("A",)))[0].endswith("Chose: A")
    with pytest.raises(QuestionError):
        answer_text(closed, UserAnswer(text="C"))
    with pytest.raises(QuestionError):
        answer_text(closed, UserAnswer(choices=("A",), text="and C"))


def test_choices_are_refused_when_the_question_offered_none():
    with pytest.raises(QuestionError):
        answer_text(UserQuestion("Name it?"), UserAnswer(choices=("A",)))


# ---- the callable interface --------------------------------------------------------------------------------

def test_a_plain_async_function_satisfies_the_interface():
    async def screen(request: UserQuestion):
        return UserAnswer(choices=(request.options[0].label,))

    answer = asyncio.run(screen(ASK))
    assert answer == UserAnswer(("SQLite",)) and q.QuestionCallable is not None
