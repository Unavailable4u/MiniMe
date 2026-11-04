"""Patch C09a: the approval prompt as a state machine (m2c.tui.approval)."""
import pytest

from m2c.runner import ApprovalRequest, Choice
from m2c.tui.approval import (ApprovalMachine, Done, EditStart, Expand, Pending, SessionMemory, canonical_key,
                              describe_request)


def req(tool="bash", args=None, risk="R2", reason="needs approval in ask mode", mode="ask"):
    return ApprovalRequest("c1", tool, risk, {"command": "ls -la"} if args is None else args, reason, mode)


def test_y_allows_once_n_denies_once():
    assert ApprovalMachine(req()).feed("y") == Done(Choice("allow", "once"))
    assert ApprovalMachine(req()).feed("n") == Done(Choice("deny", "once"))


def test_a_allows_this_exact_call_for_the_session_and_is_remembered():
    step = ApprovalMachine(req()).feed("a")
    assert step == Done(Choice("allow", "session"), remember_allow=True)


def test_a_on_plan_exit_is_only_ever_once():
    step = ApprovalMachine(req("plan_exit", {"plan": "do x"})).feed("a")
    assert step == Done(Choice("allow", "once")) and not step.remember_allow


def test_d_denies_once_and_asks_the_app_to_remember_the_deny():
    step = ApprovalMachine(req()).feed("d")
    assert step == Done(Choice("deny", "once"), remember_deny=True)


def test_keys_are_case_insensitive():
    assert ApprovalMachine(req()).feed("Y").choice.decision == "allow"
    assert ApprovalMachine(req()).feed("N").choice.decision == "deny"


@pytest.mark.parametrize("key", ["x", "z", "1", " ", "enter", "escape", "up", "c-c", "", "yy", "ctrl-y", "?"])
def test_unknown_keys_decide_nothing(key):
    step = ApprovalMachine(req()).feed(key)
    assert isinstance(step, Pending)


def test_a_printable_unknown_key_gives_a_hint_that_lists_e_only_for_bash():
    assert "e" in ApprovalMachine(req()).feed("x").message
    assert ", e" not in ApprovalMachine(req("edit_file", {"path": "a", "old_string": "a", "new_string": "b"})).feed("x").message


def test_v_and_tab_expand():
    for key in ("v", "tab", "\t", "c-i", "V"):
        assert isinstance(ApprovalMachine(req()).feed(key), Expand)


def test_e_on_bash_opens_the_editor_and_submit_returns_the_edited_input():
    m = ApprovalMachine(req(args={"command": "rm -rf build", "timeout_s": 30, "reason": "clean"}))
    assert m.feed("e") == EditStart("rm -rf build") and m.editing
    step = m.submit_edit("rm -rf build/tmp")
    assert step == Done(Choice("allow", "once", edited_input={"command": "rm -rf build/tmp", "timeout_s": 30,
                                                              "reason": "clean"}))
    assert not m.editing


def test_edited_input_is_exactly_the_command_when_that_is_all_the_call_has():
    m = ApprovalMachine(req(args={"command": "ls"}))
    m.feed("e")
    assert m.submit_edit("ls -la").choice.edited_input == {"command": "ls -la"}


def test_an_unchanged_edit_is_a_plain_allow():
    m = ApprovalMachine(req())
    m.feed("e")
    assert m.submit_edit("ls -la") == Done(Choice("allow", "once"))


def test_an_empty_edit_is_refused_and_the_editor_stays_open():
    m = ApprovalMachine(req())
    m.feed("e")
    step = m.submit_edit("   \n ")
    assert isinstance(step, Pending) and "empty" in step.message and m.editing


def test_escape_leaves_the_editor_back_to_the_menu_and_other_keys_do_nothing_while_editing():
    m = ApprovalMachine(req())
    m.feed("e")
    assert isinstance(m.feed("y"), Pending) and m.editing          # y while editing is text, not an answer
    assert isinstance(m.feed("escape"), Pending) and not m.editing
    assert m.feed("y") == Done(Choice("allow", "once"))


@pytest.mark.parametrize("tool,args", [
    ("edit_file", {"path": "a", "old_string": "a", "new_string": "b"}),
    ("write_file", {"path": "a", "content": "x"}),
    ("delete_path", {"path": "a"}),
    ("plan_exit", {"plan": "x"}),
    ("bash", "not a dict"),
    ("bash", {"command": 5}),
])
def test_e_is_not_available_for_other_tools_or_a_malformed_bash_call(tool, args):
    m = ApprovalMachine(req(tool, args))
    step = m.feed("e")
    assert isinstance(step, Pending) and "only available for bash" in step.message and not m.editing
    assert isinstance(m.submit_edit("x"), Pending)


def test_session_memory_remembers_deny_and_allow_for_the_exact_call_only():
    mem = SessionMemory()
    r = req()
    mem.record(r.tool, r.input, ApprovalMachine(r).feed("d"))
    assert mem.check("bash", {"command": "ls -la"}) == "deny"
    assert mem.check("bash", {"command": "ls -la "}) is None and mem.check("bash", {"command": "ls"}) is None
    mem.record("read_file", {"path": "a"}, ApprovalMachine(req("read_file", {"path": "a"})).feed("a"))
    assert mem.check("read_file", {"path": "a"}) == "allow" and mem.check("read_file", {"path": "b"}) is None


def test_a_remembered_deny_beats_a_remembered_allow_and_a_once_answer_is_not_remembered():
    mem = SessionMemory()
    mem.record("bash", {"command": "x"}, Done(Choice("allow", "session"), remember_allow=True))
    mem.record("bash", {"command": "x"}, Done(Choice("deny", "once"), remember_deny=True))
    assert mem.check("bash", {"command": "x"}) == "deny"
    mem.record("bash", {"command": "y"}, ApprovalMachine(req(args={"command": "y"})).feed("y"))
    assert mem.check("bash", {"command": "y"}) is None


def test_memory_is_bounded_and_ignores_inputs_it_cannot_key():
    mem = SessionMemory()
    for i in range(1100):
        mem.record("bash", {"command": str(i)}, Done(Choice("deny"), remember_deny=True))
    assert mem.check("bash", {"command": "0"}) is None and mem.check("bash", {"command": "1099"}) == "deny"
    assert canonical_key("bash", {"x": float("nan")}) is None and canonical_key("bash", {"x": object()}) is None
    mem.record("bash", {"x": object()}, Done(Choice("deny"), remember_deny=True))


def texts(blocks):
    return "\n".join(b.text for b in blocks)


def test_the_prompt_shows_the_real_command_risk_and_reason_and_the_keys():
    out = texts(describe_request(req(args={"command": "git push --force", "timeout_s": 60, "reason": "ship it"},
                                     reason="git push needs approval")))
    for needle in ("Allow bash?", "risk R2", "mode ask", "git push needs approval", "$ git push --force",
                   "the agent says: ship it", "timeout: 60", "[y] allow once", "[d]", "[e] edit"):
        assert needle in out


def test_a_multiline_command_is_shown_in_full_line_by_line():
    out = texts(describe_request(req(args={"command": "cd x\nrm -rf *\necho done"})))
    assert "$ cd x" in out and "$ rm -rf *" in out and "$ echo done" in out


def test_a_command_too_long_to_show_says_so():
    out = texts(describe_request(req(args={"command": "echo " + "a" * 20_000})))
    assert "longer than this" in out


def test_edit_prompt_points_at_the_diff_and_offers_expand_but_not_edit():
    out = texts(describe_request(req("edit_file", {"path": "src/a.py", "old_string": "a", "new_string": "b"})))
    assert "path: src/a.py" in out and "press v" in out and "[v] expand diff" in out and "[e] edit" not in out


def test_delete_plan_and_other_tools_show_their_real_input():
    assert "path: old.txt" in texts(describe_request(req("delete_path", {"path": "old.txt"})))
    assert "step one" in texts(describe_request(req("plan_exit", {"plan": "step one\nstep two"})))
    other = texts(describe_request(req("mcp__x__y", {"a": 1, "b": [2]})))
    assert '"a": 1' in other and "mcp__x__y" in other


def test_hostile_input_cannot_reach_the_prompt_as_escape_sequences():
    out = texts(describe_request(req(args={"command": "echo \x1b]0;pwned\x07 hi\u202e", "reason": "\x1b[31mx"},
                                     reason="\x1b[2J")))
    assert "\x1b" not in out and "\x07" not in out and "\u202e" not in out
