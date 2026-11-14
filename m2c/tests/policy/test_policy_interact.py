"""
m2c/tests/policy/test_policy_interact.py -- Patch L01a: what C06 decides for ``ask_user`` and ``tasks``.

Neither tool touches a file or runs a command, so both are risk R0 in the catalog and the engine allows them in
every mode with no approval prompt (``ask_user`` IS the prompt).  Everything else the engine does still holds:
a deny rule wins, an ask rule asks, malformed input is denied, nothing is auto-allowed when the settings could
not be read, and a risk above R0 asks.
"""
from __future__ import annotations

import pytest
from policy_helpers import ctx, layers, run

from m2c.policy import MODES, TOOLS, Context, Request, RuleLayers, RuleSet
from m2c.policy.decide import decide
from m2c.policy.modes import INTERACT, mode_default

A, K, D = "allow", "ask", "deny"

ASK_USER = {"question": "Which database should the project use?"}
ASK_USER_FULL = {"question": "Which?", "options": [{"label": "A"}, {"label": "B", "description": "second"}],
                 "allow_free_text": False, "timeout_s": 3600}
TASKS = {"tasks": [{"id": "t1", "title": "Read the code", "status": "done"}]}
BOTH = [("ask_user", ASK_USER), ("ask_user", ASK_USER_FULL), ("tasks", TASKS), ("tasks", {"tasks": []})]


# ---- the table ---------------------------------------------------------------------------------------------

def test_both_tools_are_known_to_the_policy_table_as_r0_interact_tools():
    for name in ("ask_user", "tasks"):
        spec = TOOLS[name]
        assert spec.tool_class == INTERACT == "interact" and spec.risk == "R0", name
    assert TOOLS["ask_user"].required == ("question",) and TOOLS["tasks"].required == ("tasks",)
    assert TOOLS["ask_user"].arg_types == {"question": str, "options": list, "allow_free_text": bool, "timeout_s": int}
    assert TOOLS["tasks"].arg_types == {"tasks": list}


# ---- allowed in every mode, by the mode default, with no prompt ----------------------------------------------

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool,inp", BOTH)
def test_allowed_in_every_mode_by_the_mode_default(mode, tool, inp):
    d = run(tool, inp, mode=mode)
    assert d.action == A and d.basis == "mode" and d.matched_rule is None and d.risk == "R0", (mode, tool, d)


@pytest.mark.parametrize("tool,inp", BOTH)
def test_still_allowed_when_nobody_can_approve(tool, inp):
    # allow stays allow: the RUNNER answers ask_user with "no person is available" (it never hangs)
    assert run(tool, inp, non_interactive=True).action == A


@pytest.mark.parametrize("tool,inp", BOTH)
def test_the_mode_default_gives_the_same_answer_for_the_class(tool, inp):
    assert mode_default("plan", INTERACT, "R0", inside_project=True).action == A
    assert mode_default("ask", INTERACT, "R0", inside_project=False).action == A
    assert mode_default("accept-edits", INTERACT, "R0", inside_project=True, protected=True).action == A


def test_the_mode_default_asks_when_the_risk_is_above_r0():
    for mode in MODES:
        for risk in ("R1", "R2", "R3"):
            assert mode_default(mode, INTERACT, risk, inside_project=True).action == K, (mode, risk)


def test_a_server_risk_above_r0_asks():
    for risk in ("R1", "R2"):
        assert run("ask_user", ASK_USER, risk=risk).action == K


# ---- rules -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool,inp", BOTH)
def test_a_deny_rule_wins_in_every_mode(mode, tool, inp):
    d = run(tool, inp, mode=mode, lay=layers(deny=[f"{tool}(*)"]))
    assert d.action == D and d.matched_rule is not None and tool in d.matched_rule


def test_a_deny_rule_can_match_the_question_text():
    lay = layers(deny=["ask_user(*password*)"])
    assert run("ask_user", {"question": "What is your password?"}, lay=lay).action == D
    assert run("ask_user", ASK_USER, lay=lay).action == A


@pytest.mark.parametrize("tool,inp", BOTH)
def test_an_ask_rule_asks_and_non_interactive_turns_it_into_a_deny(tool, inp):
    lay = layers(ask=[f"{tool}(*)"])
    assert run(tool, inp, lay=lay).action == K
    assert run(tool, inp, lay=lay, non_interactive=True).action == D


def test_deny_beats_ask_beats_the_default():
    lay = layers(ask=["tasks(*)"], deny=["tasks(*)"])
    assert run("tasks", TASKS, lay=lay).action == D


def test_a_deny_for_one_tool_does_not_touch_the_other():
    assert run("tasks", TASKS, lay=layers(deny=["ask_user(*)"])).action == A
    assert run("ask_user", ASK_USER, lay=layers(deny=["tasks(*)"])).action == A


def test_an_allow_rule_changes_nothing_here():
    d = run("ask_user", ASK_USER, lay=layers(allow=["ask_user(*)"]))
    assert d.action == A


def test_the_project_layer_is_ignored_until_the_folder_is_trusted():
    lay = layers(project=((), (), ("ask_user(*)",)))
    assert run("ask_user", ASK_USER, lay=lay).action == A                         # untrusted project: its deny is ignored
    assert run("ask_user", ASK_USER, lay=lay, project_trusted=True).action == D


# ---- fail closed -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool,inp", BOTH)
def test_nothing_is_auto_allowed_when_the_settings_could_not_be_read(tool, inp):
    broken = RuleLayers(user=RuleSet((), invalid=True))
    assert run(tool, inp, lay=broken).action == K
    assert run(tool, inp, lay=broken, non_interactive=True).action == D
    assert decide(Request(tool, inp, "R0"), Context(layers="not layers")).action in (K, D)


MALFORMED = [
    ("ask_user", {}),                                           # the question is required
    ("ask_user", {"question": 5}),
    ("ask_user", {"question": None}),
    ("ask_user", {"question": ["q"]}),
    ("ask_user", {"question": "q", "options": "A"}),
    ("ask_user", {"question": "q", "options": {"label": "A"}}),
    ("ask_user", {"question": "q", "allow_free_text": "yes"}),
    ("ask_user", {"question": "q", "allow_free_text": 1}),
    ("ask_user", {"question": "q", "timeout_s": "60"}),
    ("ask_user", {"question": "q", "timeout_s": True}),
    ("ask_user", {"question": "q", "timeout_s": 0}),
    ("ask_user", {"question": "q", "timeout_s": 3601}),
    ("ask_user", {"question": "q", "timeout_s": -5}),
    ("ask_user", {"question": "q", "colour": "red"}),            # unknown argument
    ("ask_user", ["q"]),
    ("ask_user", "q"),
    ("ask_user", None),
    ("tasks", {}),
    ("tasks", {"tasks": "t1"}),
    ("tasks", {"tasks": {"id": "t1"}}),
    ("tasks", {"tasks": None}),
    ("tasks", {"tasks": [], "extra": 1}),
    ("tasks", []),
    ("tasks", None),
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool,inp", MALFORMED)
def test_malformed_input_is_denied_in_every_mode(mode, tool, inp):
    d = run(tool, inp, mode=mode)
    assert d.action == D and d.basis is None, (tool, inp, d)


def test_a_malformed_call_is_denied_even_with_an_allow_rule():
    assert run("ask_user", {"question": 5}, lay=layers(allow=["ask_user(*)"])).action == D


def test_an_enormous_question_is_denied_by_the_engines_text_bound():
    # the engine only bounds a string by its own 4 MiB cap; the 2000-character rule is the runner's (fixed error)
    assert run("ask_user", {"question": "x" * (5 * 1024 * 1024)}).action == D


def test_the_decision_never_repeats_the_question_text():
    marker = "zq-canary-" + "0c9e44"
    for inp in ({"question": marker}, {"question": marker, "colour": marker}):
        d = run("ask_user", inp, lay=layers(deny=["ask_user(*)"]))
        assert marker not in d.reason and marker not in (d.matched_rule or "")
