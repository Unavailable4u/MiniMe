"""Patch C26: the folder-trust prompt as a pure key state machine, what it shows, and the status note.

Like tui/approval.py: no key answers by accident (Enter, Space, Tab and every unknown key do nothing), the machine answers
at most once, and everything taken from the project folder (its file names) is cleaned before it is shown.
"""
import subprocess
import sys

import pytest

from m2c.trust.prompt import (
    MAX_FILES_SHOWN, Answer, Pending, TrustPromptMachine, describe_prompt, ignored_note,
)
from m2c.trust.store import TrustState


def state(files=(".m2c/settings.toml",), reason="never_asked", **over) -> TrustState:
    base = dict(root="/home/me/code/app", trusted=False, needs_prompt=reason in ("never_asked", "changed"),
                files=tuple(files), reason=reason, fingerprint="f" * 64, message="")
    base.update(over)
    return TrustState(**base)


def text_of(blocks) -> str:
    return "\n".join(b.text for b in blocks)


# ---------------------------------------------------------------------------------------------------- the keys
@pytest.mark.parametrize("key, trust, remember", [
    ("t", True, True),
    ("s", True, False),
    ("n", False, True),
    ("escape", False, True),
    ("T", True, True),
    ("S", True, False),
    ("N", False, True),
])
def test_each_key_means_what_the_work_package_says(key, trust, remember):
    step = TrustPromptMachine(state()).feed(key)
    assert step == Answer(trust=trust, remember=remember)


@pytest.mark.parametrize("key", [
    "y", "a", "d", "e", "v", "x", "q", "1", " ", "enter", "\r", "\n", "tab", "\t", "c-i", "c-c", "c-d", "up", "down",
    "left", "backspace", "delete", "", "tt", "ts", "trust", "no", "yes", "ESC", "esc", "\x1b", "\x1b[A", "é", "T ", None, 5, 1.5,
    b"t", ["t"], True,
])
def test_every_other_key_does_nothing_and_never_answers(key):
    machine = TrustPromptMachine(state())
    step = machine.feed(key)
    assert isinstance(step, Pending) and machine.answer is None


@pytest.mark.parametrize("key", ["y", "x", "1", "a", "Q"])
def test_a_wrong_printable_key_gets_a_hint(key):
    step = TrustPromptMachine(state()).feed(key)
    assert isinstance(step, Pending) and "t, s or n" in step.message


@pytest.mark.parametrize("key", ["enter", "tab", "up", "", None, "\x1b"])
def test_a_non_printable_key_gets_no_hint(key):
    assert TrustPromptMachine(state()).feed(key) == Pending("")


def test_the_machine_answers_once_and_cannot_be_flipped():
    machine = TrustPromptMachine(state())
    first = machine.feed("n")
    assert first == Answer(False, True) and machine.answer == first
    for key in ("t", "s", "n", "escape", "x"):
        assert isinstance(machine.feed(key), Pending)
    assert machine.answer == first


def test_a_fresh_machine_has_no_answer_and_is_not_done():
    machine = TrustPromptMachine(state())
    assert machine.answer is None and machine.done is False
    machine.feed("s")
    assert machine.done is True


def test_the_machine_holds_the_state_it_was_made_for():
    st = state()
    assert TrustPromptMachine(st).state is st


# ---------------------------------------------------------------------------------------------------- what is shown
def test_the_prompt_shows_the_folder_the_files_the_warning_and_the_keys():
    text = text_of(describe_prompt(state(files=(".m2c/settings.toml", ".m2c/hooks/pre.sh", ".claude/skills/x/SKILL.md"))))
    lowered = text.lower()
    assert "/home/me/code/app" in text
    assert ".m2c/settings.toml" in text and ".m2c/hooks/pre.sh" in text and ".claude/skills/x/SKILL.md" in text
    assert "3 files" in text
    assert "project settings can allow commands without asking" in lowered
    assert "hooks run commands on your machine" in lowered
    assert "skills add instructions the model follows" in lowered
    for legend in ("[t]", "[s]", "[n]", "esc"):
        assert legend in lowered


def test_the_legend_says_what_is_remembered():
    text = text_of(describe_prompt(state())).lower()
    assert "this session only" in text and "remember" in text and "change" in text


def test_one_file_is_singular():
    assert "1 file" in text_of(describe_prompt(state())) and "1 files" not in text_of(describe_prompt(state()))


def test_a_changed_folder_says_so():
    changed = text_of(describe_prompt(state(reason="changed"))).lower()
    first = text_of(describe_prompt(state(reason="never_asked"))).lower()
    assert "changed" in changed and "changed since" not in first


def test_a_long_list_is_cut_with_a_count():
    names = [f".m2c/rules/r{i:03d}.md" for i in range(MAX_FILES_SHOWN + 7)]
    text = text_of(describe_prompt(state(files=names)))
    assert f"r{MAX_FILES_SHOWN - 1:03d}.md" in text and f"r{MAX_FILES_SHOWN:03d}.md" not in text
    assert "and 7 more" in text and f"{MAX_FILES_SHOWN + 7} files" in text


def test_file_names_from_the_project_are_cleaned_before_they_are_shown():
    hostile = ".m2c/rules/\x1b]0;owned\x07red\x1b[31m.md"
    text = text_of(describe_prompt(state(files=(hostile, ".m2c/rules/a‮b.md"), root="/p/\x1b[2Jx")))
    assert "\x1b" not in text and "\x07" not in text and "‮" not in text


def test_a_very_long_name_is_cut():
    long_name = ".m2c/rules/" + "n" * 5000
    assert all(len(b.text) < 800 for b in describe_prompt(state(files=(long_name,))))


def test_every_block_is_a_plain_display_line():
    assert {b.kind for b in describe_prompt(state())} == {"line"}


# ---------------------------------------------------------------------------------------------------- the status note
def test_the_note_for_an_untrusted_folder_with_settings_keeps_the_old_wording():
    for reason in ("never_asked", "declined", "changed"):
        note = ignored_note(state(reason=reason))
        assert note is not None and "project settings (.m2c/settings.toml) are ignored" in note


def test_the_note_without_a_settings_file_names_the_other_places():
    note = ignored_note(state(files=(".claude/skills/x/SKILL.md",), reason="declined"))
    assert note is not None and "settings.toml" not in note and "ignored" in note and ".claude" in note


@pytest.mark.parametrize("reason", ["trusted", "nothing_to_trust"])
def test_no_note_when_there_is_nothing_to_say(reason):
    assert ignored_note(state(reason=reason, files=() if reason == "nothing_to_trust" else (".m2c/settings.toml",))) is None


def test_the_notes_say_why_for_the_reasons_that_cannot_be_trusted():
    assert "symlink" in ignored_note(state(reason="symlink")).lower()
    assert "too" in ignored_note(state(reason="too_large")).lower()
    assert "could not be read" in ignored_note(state(reason="unreadable")).lower()
    unusable = ignored_note(state(reason="store_unusable", message="The trust file /x/trust.json is loose. Fix it with: chmod 600 /x/trust.json"))
    assert "trust file cannot be used" in unusable and "message above" in unusable   # the full fix is shown by the session


def test_a_note_is_one_clean_line():
    for reason in ("never_asked", "declined", "changed", "symlink", "too_large", "unreadable"):
        note = ignored_note(state(reason=reason))
        assert "\n" not in note and "\x1b" not in note


# ---------------------------------------------------------------------------------------------------- purity
def test_importing_the_trust_package_loads_no_terminal_library_and_no_network_code():
    code = (
        "import sys, m2c.trust, m2c.trust.prompt, m2c.trust.store, m2c.trust.fingerprint\n"
        "bad = [m for m in ('prompt_toolkit', 'rich', 'websockets', 'm2c.link', 'm2c.tui.app', 'm2c.tui.session') "
        "if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""
