"""
m2c/src/m2c/interact/__init__.py -- Patch L01a: the two tools that talk to the person, ``ask_user`` and ``tasks``.

What this package is
--------------------
The pure, screen-free half of two device-executed tools (protocol/v1 ``tools/ask_user.json`` and ``tools/tasks.json``).
Nothing here draws anything, reads a setting, touches a file or opens a link:

    question.py  ``ask_user``: ``parse_question`` (the model's arguments -> a ``UserQuestion``, or a fixed error),
                 the answer types and ``answer_text`` (what the model reads back).  Also the ``QuestionCallable``
                 interface a screen implements (L01b does, for the terminal UI).
    tasks.py     ``tasks``: ``parse_tasks`` (the model's whole list -> a tuple of ``Task``, or a fixed error), the
                 one-line confirmation and the ``TaskSink`` interface a screen implements.

The runner (m2c/runner.py) does the waiting, the timing and the wiring; this package only decides what is valid
and what text goes back to the model.

Rules both modules keep
-----------------------
* Fail closed with a FIXED message.  An error names the rule that failed ("option labels must be unique") and never
  repeats anything the model wrote: a label, an id or a title could carry anything, and an error is logged and
  returned to the model.
* Text that reaches a terminal later (a question, a label, a title) holds no control characters: an escape
  sequence in a label could move the cursor or fake a prompt.  Newlines and tabs are allowed only in a question.
* Standard library only.

Place this file at: m2c/src/m2c/interact/__init__.py
"""
from __future__ import annotations

__all__ = ["has_control_chars"]

# Bidirectional overrides and isolates can make one label read as another on screen; the line and paragraph
# separators break a "one line" rule.  They are refused with the control characters.
_BIDI_AND_SEPARATORS = frozenset("  ‪‫‬‭‮⁦⁧⁨⁩")


def has_control_chars(text: str, *, allow_newlines: bool = False) -> bool:
    """True when ``text`` holds a C0 or C1 control character, DEL, a bidi override or a line separator.

    ``allow_newlines`` lets ``\\n`` and ``\\t`` through (a multi-line question).  ``\\r`` never passes: a carriage
    return rewrites the start of the line on a terminal.
    """
    for ch in text:
        code = ord(ch)
        if allow_newlines and ch in "\n\t":
            continue
        if code < 0x20 or 0x7F <= code <= 0x9F or ch in _BIDI_AND_SEPARATORS:
            return True
    return False
