"""
m2c/src/m2c/interact/question.py -- Patch L01a: ``ask_user``, the request, the answer and their rules.

The model asks one question and waits.  It may offer up to six options (each a short label and an optional line of
explanation) and may allow the person to answer in their own words.  The call ends in one of four ways, and the
runner turns each into a ``tool_result``:

    answered   ``ok`` true; the text built here by ``answer_text`` ("The person answered." and then one
               ``Chose: <label>`` line per option and a ``Wrote: <words>`` line);
    declined   ``ok`` false, fixed text (the person closed the question without answering);
    timed out  ``ok`` false, fixed text (``timeout_s`` passed with no answer);
    no person  ``ok`` false, fixed text (headless run, or a screen that cannot ask).

The functions here are pure.  ``parse_question`` is the second line of defence behind the policy engine's type check
(C06 only knows an argument's JSON type, not "at most six options" or "labels are unique") and the first line of
defence for the rules a JSON schema cannot say.  It raises ``QuestionError`` with a FIXED message that names the rule
and never repeats what the model wrote.  ``answer_text`` is the check on the OTHER side: the screen is code too, and
a buggy or hostile one must not be able to put an option the model never offered, or a type that is not text, into
the model's context.

Defaults and limits mirror ``protocol/v1/tools/ask_user.json`` (a test compares them with it).  ``allow_multiple``
(several options at once) is deliberately NOT a request field: the protocol is additive-only, so a field the screen
cannot yet honour would be a promise.  ``UserAnswer.choices`` is a tuple so a later additive field can use it
without a type change.

Standard library only.

Place this file at: m2c/src/m2c/interact/question.py
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Union

from . import has_control_chars

__all__ = [
    "ANSWER_CUT_NOTICE", "BAD_ANSWER", "DEFAULT_TIMEOUT_S", "Declined", "MAX_ANSWER_CHARS", "MAX_LABEL_CHARS",
    "MAX_OPTIONS", "MAX_OPTION_DESCRIPTION_CHARS", "MAX_QUESTION_CHARS", "MAX_TIMEOUT_S", "MIN_TIMEOUT_S",
    "QuestionCallable", "QuestionError", "QuestionOption", "UserAnswer", "UserQuestion", "answer_text",
    "parse_question",
]

# protocol/v1/tools/ask_user.json
MAX_QUESTION_CHARS = 2000
MAX_OPTIONS = 6
MAX_LABEL_CHARS = 80
MAX_OPTION_DESCRIPTION_CHARS = 300
MIN_TIMEOUT_S, MAX_TIMEOUT_S, DEFAULT_TIMEOUT_S = 1, 3600, 600
# What the model reads of a typed answer.  Far under the wire's 200000; a person does not type more than this, and a
# pasted file is not an answer.
MAX_ANSWER_CHARS = 20_000
ANSWER_CUT_NOTICE = f"\n[the answer was cut: it was longer than {MAX_ANSWER_CHARS} characters]"

_PREFIX = "error: invalid question: "
NOT_AN_OBJECT = _PREFIX + "the arguments must be an object"
UNKNOWN_ARGUMENT = _PREFIX + "unknown argument; the allowed ones are question, options, allow_free_text, timeout_s"
BAD_QUESTION = _PREFIX + f"question must be non-blank text of at most {MAX_QUESTION_CHARS} characters with no control characters"
BAD_OPTIONS = _PREFIX + f"options must be a list of at most {MAX_OPTIONS} items"
BAD_OPTION = _PREFIX + "each option must be an object with a label, an optional description and nothing else"
BAD_LABEL = _PREFIX + (f"an option label must be 1 to {MAX_LABEL_CHARS} characters and a description at most "
                       f"{MAX_OPTION_DESCRIPTION_CHARS}, each on one line")
DUPLICATE_LABELS = _PREFIX + "option labels must be unique"
BAD_FREE_TEXT = _PREFIX + "allow_free_text must be true or false, and may be false only when options are offered"
BAD_TIMEOUT = _PREFIX + f"timeout_s must be a whole number from {MIN_TIMEOUT_S} to {MAX_TIMEOUT_S}"

# The screen handed back something that is not a valid answer to this question.  Not an "invalid question": the
# model's request was fine.  Fixed, and tells the model not to guess what the person meant.
BAD_ANSWER = ("error: the answer could not be used (it did not match the question) and was discarded; "
              "the person's answer is unknown, so do not assume one")


class QuestionError(Exception):
    """A rule failed.  ``message`` is fixed text (also ``str(error)``) that never repeats the model's words."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str = ""


@dataclass(frozen=True)
class UserQuestion:
    """A checked ``ask_user`` request, in the shape a screen is handed."""

    question: str
    options: tuple[QuestionOption, ...] = ()
    allow_free_text: bool = True
    timeout_s: int = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class UserAnswer:
    """What a screen returns when the person answered: the labels they chose (as offered, in the order chosen) and/or
    the words they typed.  ``text`` is None, not "", when they typed nothing."""

    choices: tuple[str, ...] = ()
    text: str | None = None


@dataclass(frozen=True)
class Declined:
    """What a screen returns when the person closed the question without answering."""


# A screen implements this: ``async def ask(question: UserQuestion) -> UserAnswer | Declined``.  The runner times the
# wait itself and cancels the call, so a screen only has to show the question and return (or let itself be cancelled
# and take the question off the screen).
QuestionCallable = Callable[[UserQuestion], Awaitable[Union[UserAnswer, Declined]]]

_ARGUMENTS = ("question", "options", "allow_free_text", "timeout_s")


def _is_text(value: Any) -> bool:
    return isinstance(value, str)


def _option(raw: Any) -> QuestionOption:
    if not isinstance(raw, dict) or "label" not in raw or not set(raw) <= {"label", "description"}:
        raise QuestionError(BAD_OPTION)
    if not all(isinstance(k, str) for k in raw):
        raise QuestionError(BAD_OPTION)
    label, description = raw["label"], raw.get("description", "")
    if not (_is_text(label) and label.strip() and len(label) <= MAX_LABEL_CHARS and not has_control_chars(label)):
        raise QuestionError(BAD_LABEL)
    if not (_is_text(description) and len(description) <= MAX_OPTION_DESCRIPTION_CHARS
            and not has_control_chars(description)):
        raise QuestionError(BAD_LABEL)
    return QuestionOption(label, description)


def parse_question(args: Any) -> UserQuestion:
    """The model's ``ask_user`` arguments as a ``UserQuestion``, or ``QuestionError``.

    The first rule that fails is the one reported, in this order: shape, unknown argument, question, options,
    free text, timeout.
    """
    if not isinstance(args, dict):
        raise QuestionError(NOT_AN_OBJECT)
    for key in args:
        if not isinstance(key, str) or key not in _ARGUMENTS:
            raise QuestionError(UNKNOWN_ARGUMENT)
    text = args.get("question")
    if not (_is_text(text) and text.strip() and len(text) <= MAX_QUESTION_CHARS
            and not has_control_chars(text, allow_newlines=True)):
        raise QuestionError(BAD_QUESTION)

    raw_options = args.get("options", [])
    if not isinstance(raw_options, list) or len(raw_options) > MAX_OPTIONS:
        raise QuestionError(BAD_OPTIONS)
    options = tuple(_option(raw) for raw in raw_options)
    if len({o.label for o in options}) != len(options):
        raise QuestionError(DUPLICATE_LABELS)

    free_text = args.get("allow_free_text", True)
    if not isinstance(free_text, bool) or (not free_text and not options):
        raise QuestionError(BAD_FREE_TEXT)

    timeout = args.get("timeout_s", DEFAULT_TIMEOUT_S)
    if type(timeout) is not int or not MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S:
        raise QuestionError(BAD_TIMEOUT)
    return UserQuestion(text, options, free_text, timeout)


def answer_text(question: UserQuestion, answer: Any) -> tuple[str, bool]:
    """What the model reads for an answer, and whether the typed words were cut.  Raises ``QuestionError(BAD_ANSWER)``
    for anything the question did not allow.

    Checked: ``answer`` is a ``UserAnswer`` (a ``Declined`` is handled by the caller, never formatted); ``choices`` is
    a tuple of distinct labels that were offered (an exact match); ``text`` is None or text; free text only when the
    question allowed it; and something was answered.  Blank typed words next to a chosen option are dropped; blank
    words alone are not an answer.
    """
    if not isinstance(answer, UserAnswer):
        raise QuestionError(BAD_ANSWER)
    choices, text = answer.choices, answer.text
    if not isinstance(choices, tuple) or not all(isinstance(c, str) for c in choices):
        raise QuestionError(BAD_ANSWER)
    if text is not None and not isinstance(text, str):
        raise QuestionError(BAD_ANSWER)
    offered = {o.label for o in question.options}
    if len(set(choices)) != len(choices) or any(c not in offered for c in choices):
        raise QuestionError(BAD_ANSWER)
    words = text if text is not None and text.strip() else None
    if words is not None and not question.allow_free_text:
        raise QuestionError(BAD_ANSWER)
    if not choices and words is None:
        raise QuestionError(BAD_ANSWER)

    lines = ["The person answered."]
    lines.extend(f"Chose: {c}" for c in choices)
    truncated = False
    if words is not None:
        if len(words) > MAX_ANSWER_CHARS:
            words, truncated = words[:MAX_ANSWER_CHARS], True
        lines.append(f"Wrote: {words}")
    body = "\n".join(lines)
    return (body + ANSWER_CUT_NOTICE if truncated else body), truncated
