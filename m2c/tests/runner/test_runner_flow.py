"""
m2c/tests/runner/test_runner_flow.py -- Patch C24: propose -> decide -> ask -> execute -> approval -> result.

Real C05 tools, real C06 policy, real C07 shell, a fake link and a fake person (``ask``).  The protocol rules pinned
here (protocol/README.md sections 2, 3 and 5; fixtures 02 and 03):

* an allowed call is answered with ``approval{allow}`` and then a ``tool_result``;
* a denied call gets ``approval{deny}`` and NO ``tool_result``;
* the person's "allow for the session" is the EXACT call, in memory only;
* an edited call is re-checked, and what actually ran is the first thing the model reads.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from runner_rig import MARKER, RUN_ID, make_layers, make_rig, proposed, run

from m2c.runner import EDITED_NOTICE, ApprovalRequest, Choice, ToolRunner


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---- an automatically allowed read ------------------------------------------------------------------------

def test_an_allowed_read_sends_approval_then_result(tmp_path):
    rig = make_rig(tmp_path)
    write(rig.root, "notes.txt", "The colour of the sky is blue.\n")
    run(rig.call("call_read_1", "read_file", {"path": "notes.txt"}))
    assert rig.link.types() == ["approval", "tool_result"]
    assert rig.link.approval("call_read_1") == {"call_id": "call_read_1", "decision": "allow", "scope": "once"}
    result = rig.link.result("call_read_1")
    assert result["ok"] is True and "colour of the sky" in result["output"]
    assert result["trust"] == "local_project" and result["truncated"] is False
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
    assert "exit_code" not in result
    assert rig.asked == [] and rig.runner.policy_denials == 0


def test_every_tool_runs_through_the_same_flow(tmp_path):
    rig = make_rig(tmp_path, mode="accept-edits", allow=["bash(echo:*)", "delete_path(*)"])
    write(rig.root, "src/a.py", "alpha\nbeta\n")

    async def scenario():
        await rig.call("c_list", "list_dir", {"path": "."})
        await rig.call("c_glob", "glob", {"pattern": "**/*.py"})
        await rig.call("c_read", "read_file", {"path": "src/a.py"})
        await rig.call("c_edit", "edit_file", {"path": "src/a.py", "old_string": "alpha", "new_string": "ALPHA"})
        await rig.call("c_write", "write_file", {"path": "src/b.py", "content": "new\n"})
        await rig.call("c_bash", "bash", {"command": "echo hi"}, "R2")
        await rig.call("c_delete", "delete_path", {"path": "src/b.py"})

    run(scenario())
    for call_id in ("c_list", "c_glob", "c_read", "c_edit", "c_write", "c_bash", "c_delete"):
        assert rig.link.approval(call_id)["decision"] == "allow", call_id
        assert rig.link.result(call_id)["ok"] is True, (call_id, rig.link.result(call_id)["output"])
    assert (rig.root / "src/a.py").read_text() == "ALPHA\nbeta\n"
    assert not (rig.root / "src/b.py").exists()
    bash = rig.link.result("c_bash")
    assert bash["trust"] == "external" and bash["exit_code"] == 0 and "hi" in bash["output"]


def test_approval_always_precedes_the_result_of_its_call(tmp_path):
    rig = make_rig(tmp_path)
    write(rig.root, "a.txt", "x\n")
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    assert [t for t, _ in rig.link.for_call("c1")] == ["approval", "tool_result"]


def test_auto_allowed_calls_can_skip_the_approval_message(tmp_path):
    """protocol/fixtures/02 shows an auto-allowed R0 read with a result alone; ``approve_auto_allowed=False`` is that."""
    rig = make_rig(tmp_path, approve_auto_allowed=False)
    write(rig.root, "a.txt", "x\n")
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    assert rig.link.types() == ["tool_result"]


# ---- ask ------------------------------------------------------------------------------------------------------

def test_ask_then_allow_shows_the_real_input_and_runs_the_call(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow"))
    path = write(rig.root, "notes.txt", "The colour of the sky is blue.\n")
    run(rig.call("call_edit_1", "read_file", {"path": "notes.txt"}))           # read first (read-before-write)
    run(rig.call("call_edit_2", "edit_file", {"path": "notes.txt", "old_string": "colour", "new_string": "color"}, "R1"))
    (request,) = rig.asked
    assert isinstance(request, ApprovalRequest)
    assert request.call_id == "call_edit_2" and request.tool == "edit_file" and request.risk == "R1"
    assert request.input == {"path": "notes.txt", "old_string": "colour", "new_string": "color"}
    assert request.mode == "ask" and request.reason
    assert rig.link.approval("call_edit_2") == {"call_id": "call_edit_2", "decision": "allow", "scope": "once"}
    assert rig.link.result("call_edit_2")["ok"] is True
    assert path.read_text() == "The color of the sky is blue.\n"
    assert rig.link.types().index("approval") < len(rig.link.types())


def test_ask_then_deny_sends_a_denial_and_no_result(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("deny"))
    path = write(rig.root, "notes.txt", "keep me\n")
    run(rig.call("c_read", "read_file", {"path": "notes.txt"}))
    run(rig.call("c_edit", "edit_file", {"path": "notes.txt", "old_string": "keep", "new_string": "lose"}, "R1"))
    assert rig.link.approval("c_edit") == {"call_id": "c_edit", "decision": "deny", "scope": "once"}
    assert rig.link.for_call("c_edit", "tool_result") == []                  # a denied call gets NO tool_result
    assert path.read_text() == "keep me\n"
    assert rig.runner.policy_denials == 0                                     # the person said no; that is not a policy denial


def test_a_deny_with_session_scope_is_reported_as_once(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("deny", "session"))
    run(rig.call("c1", "bash", {"command": "ls"}, "R2"))
    assert rig.link.approval("c1") == {"call_id": "c1", "decision": "deny", "scope": "once"}


@pytest.mark.parametrize("answer", [None, "allow", object(), Choice("maybe"), Choice("allow", "forever"),
                                    Choice("allow", "once", edited_input="rm -rf /")])
def test_a_malformed_answer_from_ask_is_a_denial(tmp_path, answer):
    rig = make_rig(tmp_path, ask_fn=lambda request: answer)
    run(rig.call("c1", "bash", {"command": "echo ran > ran.txt"}, "R2"))
    assert rig.link.approval("c1")["decision"] == "deny"
    assert rig.link.for_call("c1", "tool_result") == [] and not (rig.root / "ran.txt").exists()


def test_an_exception_in_ask_is_a_denial_and_its_text_is_not_kept(tmp_path, caplog):
    def broken(request):
        raise RuntimeError(f"the UI crashed on {MARKER}")

    rig = make_rig(tmp_path, ask_fn=broken)
    with caplog.at_level("DEBUG"):
        run(rig.call("c1", "bash", {"command": "ls"}, "R2"))
    assert rig.link.approval("c1")["decision"] == "deny" and rig.link.for_call("c1", "tool_result") == []
    assert MARKER not in caplog.text


# ---- scope session: the exact call only, in memory only ------------------------------------------------------

def test_a_session_allow_covers_the_exact_same_input_only(tmp_path):
    answers = iter([Choice("allow", "session")])
    rig = make_rig(tmp_path, ask_fn=lambda request: next(answers, Choice("deny")))

    async def scenario():
        await rig.call("c1", "bash", {"command": "echo one"}, "R2")          # asks; allowed for the session
        await rig.call("c2", "bash", {"command": "echo one"}, "R2")          # the same call: not asked again
        await rig.call("c3", "bash", {"command": "echo two"}, "R2")          # another input: asked, and refused
        await rig.call("c4", "bash", {"command": "echo one", "timeout_s": 5}, "R2")   # same command, other input: asked

    run(scenario())
    assert [r.call_id for r in rig.asked] == ["c1", "c3", "c4"]
    assert rig.link.approval("c1") == {"call_id": "c1", "decision": "allow", "scope": "session"}
    assert rig.link.approval("c2") == {"call_id": "c2", "decision": "allow", "scope": "session"}
    assert "one" in rig.link.result("c2")["output"]
    assert rig.link.approval("c3")["decision"] == "deny" and rig.link.approval("c4")["decision"] == "deny"


def test_a_session_allow_is_never_a_pattern_and_never_for_another_tool(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", "session"))
    write(rig.root, "a.txt", "one\n")

    async def scenario():
        await rig.call("c1", "bash", {"command": "echo hi"}, "R2")
        await rig.call("c2", "bash", {"command": "echo hi there"}, "R2")     # a longer command: asked again
        await rig.call("c3", "bash", {"command": "echo hi "}, "R2")          # not even trailing whitespace
    run(scenario())
    assert [r.call_id for r in rig.asked] == ["c1", "c2", "c3"]


def test_a_session_allow_never_beats_a_deny(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", "session"))

    async def scenario():
        await rig.call("c1", "bash", {"command": "echo hi"}, "R2")
        rig.state["layers"] = make_layers(deny=["bash(echo:*)"])   # the policy changes
        await rig.call("c2", "bash", {"command": "echo hi"}, "R2")
    run(scenario())
    assert rig.link.approval("c1")["decision"] == "allow"
    assert rig.link.approval("c2")["decision"] == "deny" and rig.link.for_call("c2", "tool_result") == []
    assert len(rig.asked) == 1


def test_a_session_allow_lives_in_memory_only(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", "session"))
    command = f"echo {MARKER}"
    run(rig.call("c1", "bash", {"command": command}, "R2"))
    dump = "\n".join(str(tuple(row)) for row in rig.store.execute("SELECT * FROM events")) + "\n" + \
           "\n".join(str(tuple(row)) for row in rig.store.execute("SELECT * FROM approvals"))
    assert MARKER not in dump                                                  # neither the command nor its output
    # a new runner (a restart) starts with no grants at all
    fresh_asked = []

    async def asking(request):
        fresh_asked.append(request)
        return Choice("deny")

    fresh = ToolRunner(rig.link, run_id=RUN_ID, tool_context=rig.tool_context, policy_context=rig.runner._get_policy,
                       store=rig.store, ask=asking, replay_settle_s=0.0)

    async def scenario():
        fresh.on_tool_proposed(proposed("c9", "bash", {"command": command}, "R2"))
        await fresh.wait_idle()

    run(scenario())
    assert [r.call_id for r in fresh_asked] == ["c9"]


def test_plan_exit_is_never_remembered_for_the_session(tmp_path):
    rig = make_rig(tmp_path, mode="plan", ask_fn=lambda request: Choice("allow", "session"))

    async def scenario():
        await rig.call("c1", "plan_exit", {"plan": "1. do it"})
        rig.state["mode"] = "plan"
        await rig.call("c2", "plan_exit", {"plan": "1. do it"})
    run(scenario())
    assert [r.call_id for r in rig.asked] == ["c1", "c2"]


# ---- an edited call -------------------------------------------------------------------------------------------

def test_an_edited_call_is_rechecked_and_a_deny_wins(tmp_path):
    """The person edits a harmless edit into one aimed at a protected file: C06 denies it and nothing runs."""
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", edited_input={
        "path": ".env", "old_string": "nothing", "new_string": MARKER}))
    secret = write(rig.root, ".env", "TOKEN=nothing\n")
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    run(rig.call("c2", "edit_file", {"path": "a.txt", "old_string": "x", "new_string": "y"}, "R1"))
    assert secret.read_text() == "TOKEN=nothing\n"
    assert rig.link.approval("c2") == {"call_id": "c2", "decision": "deny", "scope": "once"}
    assert rig.link.for_call("c2", "tool_result") == []
    assert rig.runner.policy_denials >= 1


@pytest.mark.parametrize("edited", [
    {"command": "echo hi", "surprise": 1},      # an argument the schema does not have
    {"timeout_s": 5},                           # a required argument missing
    {"command": 17},                            # the wrong type
    {"command": "echo hi", "timeout_s": 0},     # out of range
    "echo hi",                                  # not an object at all
    ["echo hi"],
])
def test_an_edited_input_that_does_not_validate_runs_nothing(tmp_path, edited):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", edited_input=edited))
    run(rig.call("c1", "bash", {"command": "echo original > ran.txt"}, "R2"))
    assert rig.link.approval("c1")["decision"] == "deny" and rig.link.for_call("c1", "tool_result") == []
    assert not (rig.root / "ran.txt").exists()


def test_an_edited_call_runs_the_edited_input_and_says_so_first(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", edited_input={"command": "echo edited-output"}))
    run(rig.call("c1", "bash", {"command": "echo original-output"}, "R2"))
    output = rig.link.result("c1")["output"]
    first, rest = output.split("\n", 1)
    assert first == EDITED_NOTICE                                              # one fixed line, first
    assert "echo edited-output" in rest.split("\n\n", 1)[0]                    # the command that actually ran
    assert "edited-output" in output and "original-output" not in output
    assert rig.link.result("c1")["ok"] is True


def test_an_edited_non_shell_call_shows_the_input_that_ran(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice(
        "allow", edited_input={"path": "b.txt", "content": "edited body\n"}))
    run(rig.call("c2", "write_file", {"path": "a.txt", "content": "original body\n"}, "R1"))
    output = rig.link.result("c2")["output"]
    assert output.startswith(EDITED_NOTICE + "\n") and '"path": "b.txt"' in output.split("\n\n", 1)[0]
    assert (rig.root / "b.txt").read_text() == "edited body\n" and not (rig.root / "a.txt").exists()


def test_a_call_the_policy_allows_without_asking_is_never_marked_as_edited(tmp_path):
    rig = make_rig(tmp_path, mode="accept-edits", ask_fn=lambda request: Choice(
        "allow", edited_input={"path": "b.txt", "content": "edited body\n"}))
    run(rig.call("c1", "write_file", {"path": "a.txt", "content": "original body\n"}, "R1"))
    assert rig.asked == [] and (rig.root / "a.txt").exists()
    assert not rig.link.result("c1")["output"].startswith(EDITED_NOTICE)


def test_an_edited_input_equal_to_the_original_is_not_marked_as_edited(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", edited_input={"command": "echo same"}))
    run(rig.call("c1", "bash", {"command": "echo same"}, "R2"))
    assert not rig.link.result("c1")["output"].startswith(EDITED_NOTICE)


# ---- policy denials, malformed calls --------------------------------------------------------------------------

@pytest.mark.parametrize("tool, args", [
    ("edit_file", {"path": "a.txt", "old_string": "x"}),                       # a required argument missing
    ("read_file", {"path": "a.txt", "surprise": True}),                        # an unknown argument
    ("read_file", {"path": 7}),                                                # the wrong type
    ("grep", "not an object"),
    ("mcp__something__else", {"x": 1}),                                        # a tool that is not in the catalog
    ("", {}),
])
def test_unknown_tools_and_malformed_arguments_are_denied_and_never_run(tmp_path, tool, args):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow"))
    run(rig.call("c1", tool, args))
    assert rig.link.approval("c1") == {"call_id": "c1", "decision": "deny", "scope": "once"}
    assert rig.link.for_call("c1", "tool_result") == [] and rig.asked == []
    assert rig.runner.policy_denials == 1


def test_a_malformed_risk_is_denied(tmp_path):
    rig = make_rig(tmp_path)
    write(rig.root, "a.txt", "x\n")
    run(rig.call("c1", "read_file", {"path": "a.txt"}, "R9"))
    assert rig.link.approval("c1")["decision"] == "deny" and rig.link.for_call("c1", "tool_result") == []


def test_the_effective_risk_is_the_higher_of_the_server_and_the_client(tmp_path):
    """The server says R0 for a delete; C06's own risk is R1 (inside) and the prompt shows the higher one."""
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("deny"))
    write(rig.root, "a.txt", "x\n")
    run(rig.call("c1", "delete_path", {"path": "a.txt"}, "R0"))
    assert rig.asked[0].risk == "R1"


def test_a_policy_deny_rule_is_final(tmp_path):
    rig = make_rig(tmp_path, deny=["read_file(secret*)"], ask_fn=lambda request: Choice("allow"))
    write(rig.root, "secret.txt", MARKER)
    run(rig.call("c1", "read_file", {"path": "secret.txt"}))
    assert rig.link.approval("c1")["decision"] == "deny" and rig.link.for_call("c1", "tool_result") == []
    assert rig.asked == [] and rig.runner.policy_denials == 1


def test_plan_mode_denies_changes(tmp_path):
    rig = make_rig(tmp_path, mode="plan", ask_fn=lambda request: Choice("allow"))
    run(rig.call("c1", "bash", {"command": "echo hi"}, "R2"))
    assert rig.link.approval("c1")["decision"] == "deny" and rig.asked == []


def test_a_policy_that_cannot_be_read_denies(tmp_path):
    rig = make_rig(tmp_path)
    rig.runner._get_policy = lambda: (_ for _ in ()).throw(RuntimeError("no settings"))
    write(rig.root, "a.txt", "x\n")
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    assert rig.link.approval("c1")["decision"] == "deny" and rig.link.for_call("c1", "tool_result") == []
    rig.runner._get_policy = lambda: "not a context"
    run(rig.call("c2", "read_file", {"path": "a.txt"}))
    assert rig.link.approval("c2")["decision"] == "deny"


# ---- non-interactive -----------------------------------------------------------------------------------------

def test_without_ask_an_ask_decision_is_a_denial_and_it_is_recorded(tmp_path):
    rig = make_rig(tmp_path)                                                   # ask=None: nobody can be asked
    write(rig.root, "a.txt", "x\n")
    run(rig.call("c1", "bash", {"command": "echo hi"}, "R2"))
    assert rig.link.approval("c1") == {"call_id": "c1", "decision": "deny", "scope": "once"}
    assert rig.link.for_call("c1", "tool_result") == []
    assert rig.runner.policy_denials == 1 and rig.runner.would_ask == 1
    assert rig.approvals_rows() == [("c1", "bash", "deny", "once", "policy", "")]


def test_without_ask_an_explicit_allow_still_runs(tmp_path):
    rig = make_rig(tmp_path, allow=["bash(echo:*)"])
    run(rig.call("c1", "bash", {"command": "echo fine"}, "R2"))
    assert rig.link.result("c1")["ok"] is True and rig.runner.policy_denials == 0 and rig.runner.would_ask == 0


# ---- plan_exit ------------------------------------------------------------------------------------------------

def test_plan_exit_when_allowed_leaves_plan_mode(tmp_path):
    changes = []
    rig = make_rig(tmp_path, mode="plan", ask_fn=lambda request: Choice("allow"), on_mode_change=changes.append)
    write(rig.root, "a.txt", "x\n")

    async def scenario():
        await rig.call("c1", "bash", {"command": "echo hi"}, "R2")             # plan mode: denied
        assert rig.runner.mode == "plan"
        await rig.call("c2", "plan_exit", {"plan": "1. edit a.txt"})
        assert rig.runner.mode == "ask"
        await rig.call("c3", "bash", {"command": "echo hi"}, "R2")             # now merely asked, and allowed

    run(scenario())
    assert rig.link.approval("c1")["decision"] == "deny"
    assert rig.asked[0].tool == "plan_exit" and rig.asked[0].input == {"plan": "1. edit a.txt"}
    result = rig.link.result("c2")
    assert result["ok"] is True and "plan mode" in result["output"].lower()
    assert changes == ["ask"]
    assert rig.link.result("c3")["ok"] is True


def test_plan_exit_denied_keeps_plan_mode_and_sends_no_result(tmp_path):
    rig = make_rig(tmp_path, mode="plan", ask_fn=lambda request: Choice("deny"))
    run(rig.call("c1", "plan_exit", {"plan": "1. nothing"}))
    assert rig.runner.mode == "plan"
    assert rig.link.approval("c1")["decision"] == "deny" and rig.link.for_call("c1", "tool_result") == []


def test_plan_exit_follows_the_host_when_it_changes_the_mode_itself(tmp_path):
    rig = make_rig(tmp_path, mode="plan", ask_fn=lambda request: Choice("allow"))

    async def scenario():
        await rig.call("c1", "plan_exit", {"plan": "p"})
        rig.state["mode"] = "accept-edits"                                       # the host synced its own mode
        await rig.call("c2", "write_file", {"path": "n.txt", "content": "x"}, "R1")
        rig.state["mode"] = "plan"                                              # and the person went back to plan mode
        await rig.call("c3", "write_file", {"path": "m.txt", "content": "x"}, "R1")
    run(scenario())
    assert rig.link.approval("c2")["decision"] == "allow"
    assert rig.link.approval("c3")["decision"] == "deny"                        # plan mode again: the override is gone


@pytest.mark.parametrize("mode", ["plan", "bypass", "", None])
def test_a_bad_plan_exit_mode_is_refused_at_construction(tmp_path, mode):
    rig = make_rig(tmp_path)
    with pytest.raises(ValueError):
        ToolRunner(rig.link, run_id=RUN_ID, tool_context=rig.tool_context, policy_context=rig.runner._get_policy,
                   store=rig.store, plan_exit_mode=mode)


# ---- the runner's own protocol answers are valid JSON the schema accepts ----------------------------------------

def test_the_fake_link_validated_every_payload(tmp_path):
    """FakeLink raises ValueError for a payload the real link would refuse; a whole flow must produce none."""
    rig = make_rig(tmp_path, mode="accept-edits", allow=["bash(echo:*)"])
    write(rig.root, "a.txt", "x\n")

    async def scenario():
        await rig.call("c1", "read_file", {"path": "a.txt"})
        await rig.call("c2", "bash", {"command": "echo a; echo b >&2"}, "R2")
    run(scenario())
    json.dumps(rig.link.sent)
    assert "tool_stream" in rig.link.types()
