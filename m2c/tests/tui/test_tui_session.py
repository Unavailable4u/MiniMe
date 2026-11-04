"""Patch C09b: the session controller against C04's fake server, with the real runner, tools, policy and checkpoints."""
import os

from tui_rig import Chat, Rig, ask_once, call_id, run_async



def reply_to_user(text="Hello there."):
    async def handler(chat, msg):
        if msg["t"] == "user_message":
            await chat.say(msg["run_id"], text, total=0.5)
    return handler


# --------------------------------------------------------------------------------------------- chat and the log
def test_a_streamed_reply_is_shown_and_logged_once(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat(reply_to_user("Hello there.\nSecond line."))) as rig:
            await rig.send("hi")
            await rig.until(lambda: rig.ui.has("Second line."), "the reply")
            assert rig.ui.has("> hi")
            types = rig.log_types()
            assert types[:2] == ["run.start", "user.message"]
            assert types.count("assistant.message") == 1
            log = dict((t, d) for t, d in rig.log() if t == "assistant.message")
            assert log["assistant.message"] == {"text": "Hello there.\nSecond line."}
            await rig.until(lambda: "credits: 0.5" in rig.ui.status[0], "the credits")
            assert "ctx ~1%" in rig.ui.status[0]
            assert "working" not in rig.ui.status[0]
            assert len(rig.chat.starts) == 1
    run_async(scenario())


def test_a_second_message_uses_the_same_run_and_the_first_start_has_no_history(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat(reply_to_user())) as rig:
            await rig.send("one")
            await rig.until(lambda: len(rig.chat.user_messages) == 1 and "working" not in rig.ui.status[0])
            await rig.send("two")
            await rig.until(lambda: len(rig.chat.user_messages) == 2)
            assert len(rig.chat.starts) == 1
            assert "history" not in rig.chat.starts[0]["payload"]
            assert rig.chat.user_messages[0][0] == rig.chat.user_messages[1][0]
            payload = rig.chat.starts[0]["payload"]
            assert payload["profile"] == "code" and payload["mode"] == "ask" and payload["cwd_label"] == "proj"
    run_async(scenario())


def test_the_hello_advertises_the_tools_capability(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            hello = rig.server.messages(t="hello")[0]
            assert "tools" in hello["payload"]["capabilities"]
    run_async(scenario())


def test_reply_restarted_is_logged_with_the_final_text_only(tmp_path):
    async def handler(chat, msg):
        if msg["t"] == "user_message":
            run = msg["run_id"]
            await chat.emit(run, type="state_changed", state="running")
            await chat.emit(run, type="text_delta", text="partial words ")
            await chat.emit(run, type="notice", code="reply_restarted", severity="info", message="restarting the reply")
            await chat.emit(run, type="text_delta", text="the final reply")
            await chat.emit(run, type="text_done", stop_reason="end_turn")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("go")
            await rig.until(lambda: "assistant.message" in rig.log_types(), "the reply")
            replies = [d["text"] for t, d in rig.log() if t == "assistant.message"]
            assert replies == ["the final reply"]
            assert rig.ui.has("the reply is being written again")
    run_async(scenario())


# --------------------------------------------------------------------------------------------- tools
def test_an_auto_allowed_read_runs_without_a_prompt_and_is_logged(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "read_file", {"path": "notes.txt"}, "R0")
        elif msg["t"] == "tool_result":
            await chat.finish(run, cid, "read_file", "ok")
            await chat.say(run, "I read it.")

    async def scenario():
        async with Rig(tmp_path, Chat(handler), files={"notes.txt": "alpha beta\n"}) as rig:
            await rig.send("read notes")
            await rig.until(lambda: rig.ui.has("I read it."), "the final reply")
            assert not rig.session.approval_active
            assert rig.ui.has("● read_file  notes.txt")
            assert rig.ui.has("✓ read_file ok")
            kinds = rig.log_types()
            assert kinds.index("tool.call") < kinds.index("tool.result") < kinds.index("tool.closed")
            result = [d for t, d in rig.log() if t == "tool.result"][0]
            assert result["ok"] is True and "alpha beta" in result["output"]
    run_async(scenario())


def test_an_approved_write_shows_a_diff_checkpoints_and_runs(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "write_file", {"path": "new.txt", "content": "one\ntwo\n"}, "R2")
        elif msg["t"] == "tool_result":
            await chat.finish(run, cid, "write_file", "ok")
            await chat.say(run, "Written.")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("write it")
            await ask_once(rig, "y")
            await rig.until(lambda: rig.ui.has("Written."), "the final reply")
            assert (rig.project / "new.txt").read_text() == "one\ntwo\n"
            assert rig.ui.has("DIFF new.txt  +2 -0  (new file)")
            assert rig.ui.has("add:one")
            assert rig.chat.approvals[0]["decision"] == "allow"
            kinds = rig.log_types()
            assert "checkpoint" in kinds
            ck = [d for t, d in rig.log() if t == "checkpoint"][0]
            assert ck == {"step": 1, "call_id": cid, "tool": "write_file"}
            assert rig.ui.prompt[0] == "> "
    run_async(scenario())


def test_a_denied_call_sends_a_deny_and_no_result(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "bash", {"command": "touch hello.txt"}, "R3")
        elif msg["t"] == "approval":
            await chat.finish(run, cid, "bash", "denied")
            await chat.say(run, "Understood.")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("make a file")
            await ask_once(rig, "n")
            await rig.until(lambda: rig.ui.has("Understood."), "the final reply")
            assert rig.chat.approvals[0]["decision"] == "deny"
            assert rig.chat.results == []
            assert not (rig.project / "hello.txt").exists()
            assert rig.ui.has("✗ bash denied")
            assert ("tool.closed", {"call_id": cid, "status": "denied"}) in rig.log()
    run_async(scenario())


def test_d_denies_a_repeat_of_the_exact_call_without_asking(tmp_path):
    first, second = call_id(), call_id()
    state = {"n": 0}

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            state["n"] += 1
            cid = first if state["n"] == 1 else second
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "bash", {"command": "echo repeat"}, "R3")
        elif msg["t"] == "approval":
            cid = first if len(chat.approvals) == 1 else second
            await chat.finish(run, cid, "bash", "denied")
            await chat.say(run, "ok")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("try")
            await ask_once(rig, "d")
            await rig.until(lambda: len(rig.chat.approvals) == 1, "the first answer")
            assert rig.chat.approvals[0] == {"call_id": first, "decision": "deny", "scope": "once"}
            await rig.until(lambda: "working" not in rig.ui.status[0], "the turn to end")
            await rig.send("try again")
            await rig.until(lambda: len(rig.chat.approvals) == 2, "the second answer")
            assert not rig.session.approval_active
            assert rig.chat.approvals[1] == {"call_id": second, "decision": "deny", "scope": "once"}
            assert rig.ui.has("will not be asked about this exact call again")
    run_async(scenario())


def test_a_allows_the_exact_call_for_the_session_even_in_a_new_run(tmp_path):
    ids = [call_id(), call_id()]
    state = {"n": 0}

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            cid = ids[state["n"]]
            state["n"] += 1
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "write_file", {"path": "same.txt", "content": "x\n"}, "R2")
        elif msg["t"] == "tool_result":
            cid = msg["payload"]["call_id"]
            await chat.finish(run, cid, "write_file", "ok")
            await chat.say(run, "done")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("one")
            await ask_once(rig, "a")
            await rig.until(lambda: len(rig.chat.results) == 1, "the first result")
            assert rig.chat.approvals[0]["scope"] == "session"
            await rig.until(lambda: "working" not in rig.ui.status[0])
            await rig.session._cancel_current(say=False)       # a new run is needed: the runner's own grants are gone
            (rig.project / "same.txt").unlink()
            await rig.send("two")
            await rig.until(lambda: len(rig.chat.results) == 2, "the second result, with no prompt")
            assert len(rig.chat.starts) == 2
    run_async(scenario())


def test_edit_is_only_for_bash_and_the_edited_command_is_what_runs(tmp_path):
    cid = call_id()
    seen = {}

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "bash", {"command": "echo original"}, "R3")
        elif msg["t"] == "tool_result":
            seen["output"] = msg["payload"]["output"]
            await chat.finish(run, cid, "bash", "ok")
            await chat.say(run, "ran")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("run it")
            await rig.until(lambda: rig.session.approval_active, "the prompt")
            rig.session.on_key("e")
            assert rig.ui.prompt == ("edit> ", "echo original")
            assert rig.session.captures_keys(True) is False      # typing goes to the edit buffer now
            rig.session.submit("echo edited")
            await rig.until(lambda: "output" in seen, "the result")
            assert "command: echo edited" in seen["output"] and "original" not in seen["output"]
            assert seen["output"].startswith("[The person edited this call")
            assert rig.ui.prompt[0] == "> "
    run_async(scenario())


def test_the_sandbox_warning_is_shown_once_per_session(tmp_path):
    ids = [call_id(), call_id()]
    state = {"n": 0}

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            cid = ids[state["n"]]
            state["n"] += 1
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "bash", {"command": f"echo n{state['n']}"}, "R3")
        elif msg["t"] == "tool_result":
            await chat.finish(run, msg["payload"]["call_id"], "bash", "ok")
            await chat.say(run, "ok")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("a")
            await ask_once(rig, "y")
            await rig.until(lambda: len(rig.chat.results) == 1)
            await rig.until(lambda: "working" not in rig.ui.status[0])
            await rig.send("b")
            await ask_once(rig, "y")
            await rig.until(lambda: len(rig.chat.results) == 2)
            assert rig.ui.text().count("there is no sandbox yet") == 1
    run_async(scenario())


def test_a_prompt_is_dismissed_when_the_run_is_interrupted(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        if msg["t"] == "user_message":
            await chat.emit(msg["run_id"], type="state_changed", state="running")
            await chat.propose(msg["run_id"], cid, "bash", {"command": "echo hi"}, "R3")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("go")
            await rig.until(lambda: rig.session.approval_active, "the prompt")
            rig.session.on_escape()
            await rig.until(lambda: not rig.session.approval_active, "the prompt to go away")
            await rig.until(lambda: len(rig.chat.cancels) == 1, "cancel")
            assert rig.ui.has("[the request was withdrawn]")
            assert rig.ui.prompt[0] == "> "
            assert rig.chat.approvals == [] and rig.chat.results == []
    run_async(scenario())


# --------------------------------------------------------------------------------------------- interrupt and queue
def test_esc_sends_cancel_and_the_next_message_starts_a_new_run_with_history(tmp_path):
    async def handler(chat, msg):
        if msg["t"] == "user_message":
            run = msg["run_id"]
            if msg["payload"]["text"] == "first":
                await chat.say(run, "first answer")
            else:
                await chat.emit(run, type="state_changed", state="running")     # never answers: the person interrupts
                await chat.emit(run, type="text_delta", text="half a rep")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("first")
            await rig.until(lambda: "assistant.message" in rig.log_types())
            await rig.send("second")
            await rig.until(lambda: rig.ui.has("half a rep"))
            first_run = rig.chat.user_messages[0][0]
            rig.session.on_escape()
            await rig.until(lambda: rig.chat.cancels == [first_run], "cancel")
            await rig.until(lambda: not rig.session.run_open)
            assert rig.ui.has("[stopped]")
            await rig.send("third")
            await rig.until(lambda: len(rig.chat.user_messages) == 3, "the third message")
            history = rig.chat.starts[1]["payload"]["history"]
            assert history == [{"role": "user", "text": "first"}, {"role": "assistant", "text": "first answer"},
                               {"role": "user", "text": "second"}]
            assert rig.chat.user_messages[-1][1] == "third"
            assert rig.chat.user_messages[-1][0] != first_run
            assert rig.ui.has("half a rep")           # what was on screen stays; it is not in the history
    run_async(scenario())


def test_messages_typed_while_working_are_sent_at_once_and_shown_queued(tmp_path):
    async def handler(chat, msg):
        if msg["t"] == "user_message" and msg["payload"]["text"] == "one":
            await chat.emit(msg["run_id"], type="state_changed", state="running")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("one")
            await rig.send("two")
            await rig.until(lambda: len(rig.chat.user_messages) == 2, "both messages at the server")
            assert rig.ui.has("(queued) two")
            assert "1 queued" in rig.ui.status[0] and "working" in rig.ui.status[0]
    run_async(scenario())


def test_rate_limited_is_reported_and_the_message_is_not_kept(tmp_path):
    from link_fakes import error_env

    async def handler(chat, msg):
        if msg["t"] == "user_message":
            run = msg["run_id"]
            if msg["payload"]["text"] == "overflow":
                await chat.conn.send(error_env("rate_limited", run_id=run, ref_id=msg["id"],
                                               details={"retry_after_s": 7}))
            else:
                await chat.emit(run, type="state_changed", state="running")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("one")
            await rig.send("overflow")
            await rig.until(lambda: rig.ui.has("[error: rate_limited]"), "the error")
            assert rig.ui.has("Wait 7 seconds and send it again.")
            await rig.until(lambda: "user.unsent" in rig.log_types())
            assert "1 queued" not in rig.ui.status[0]
    run_async(scenario())


# --------------------------------------------------------------------------------------------- rewind
def test_rewind_restores_files_shows_the_notice_and_starts_the_next_run_from_the_marker(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            text = msg["payload"]["text"]
            if text == "write it":
                await chat.emit(run, type="state_changed", state="running")
                await chat.say(run, "I will write.", stop="tool_use")
                await chat.propose(run, cid, "write_file", {"path": "made.txt", "content": "made\n"}, "R2")
            else:
                await chat.say(run, "fine")
        elif msg["t"] == "tool_result":
            await chat.finish(run, cid, "write_file", "ok")
            await chat.say(run, "Done writing.")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("write it")
            await ask_once(rig, "y")
            await rig.until(lambda: rig.ui.has("Done writing."))
            assert (rig.project / "made.txt").exists()
            await rig.send("/rewind", wait=False)
            await rig.until(lambda: rig.session.modal is not None, "the list")
            assert rig.ui.has("1. ") and rig.ui.has("write_file") and rig.ui.has("made.txt")
            assert "made\n" not in rig.ui.text().split("Rewind to just before")[1]       # names, never contents
            rig.session.submit("1")
            await rig.until(lambda: rig.session.modal is not None and rig.session.modal.label.startswith("confirm"))
            rig.session.submit("y")
            await rig.until(lambda: rig.session.rewinds == 1, "the rewind")
            assert not (rig.project / "made.txt").exists()
            assert rig.ui.has("Rewind restores files in this project only.")
            assert "rewind" in rig.log_types()
            first_run = rig.chat.user_messages[0][0]
            assert first_run in rig.chat.cancels
            await rig.send("what now")
            await rig.until(lambda: len(rig.chat.starts) == 2)
            history = rig.chat.starts[1]["payload"]["history"]
            assert history == [{"role": "user", "text": "write it"}]       # cut just before the step that wrote the file
    run_async(scenario())


def test_rewind_with_nothing_to_rewind_says_so(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            await rig.send("/rewind", wait=False)
            await rig.until(lambda: rig.ui.has("nothing to rewind"))
            assert rig.session.modal is None
    run_async(scenario())


def test_esc_esc_opens_the_rewind_list_and_esc_cancels_it(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "write_file", {"path": "e.txt", "content": "e\n"}, "R2")
        elif msg["t"] == "tool_result":
            await chat.finish(run, cid, "write_file", "ok")
            await chat.say(run, "ok")

    async def scenario():
        times = iter([0.0, 0.1, 5.0, 5.1])
        async with Rig(tmp_path, Chat(handler), clock=lambda: next(times)) as rig:
            await rig.send("w")
            await ask_once(rig, "y")
            await rig.until(lambda: len(rig.chat.results) == 1)
            await rig.until(lambda: rig.ui.has("ok") and "working" not in rig.ui.status[0])
            rig.session.on_escape()
            rig.session.on_escape()
            await rig.until(lambda: rig.session.modal is not None, "the list")
            rig.session.on_escape()
            assert rig.session.modal is None
            assert rig.ui.has("Cancelled.")
            assert (rig.project / "e.txt").exists()
    run_async(scenario())


# --------------------------------------------------------------------------------------------- modes and settings
def test_shift_tab_cycles_ask_and_accept_edits(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            s = rig.session
            assert s.status.mode == "ask"
            s.on_cycle_mode()
            assert s.status.mode == "accept-edits" and "mode: accept-edits" in rig.ui.status[0]
            assert s.policy_context().mode == "accept-edits"
            s.on_cycle_mode()
            assert s.status.mode == "ask" and s.policy_context().mode == "ask"
    run_async(scenario())


def test_shift_tab_never_enters_or_leaves_plan(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat(), mode="plan") as rig:
            for _ in range(3):
                rig.session.on_cycle_mode()
            assert rig.session.status.mode == "plan" and rig.session.policy_context().mode == "plan"
            assert rig.ui.has("Plan mode is left when you approve the plan")
    run_async(scenario())


def test_the_policy_context_reads_the_current_mode_every_time(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "write_file", {"path": "m.txt", "content": "m\n"}, "R1")
        elif msg["t"] == "tool_result":
            await chat.finish(run, cid, "write_file", "ok")
            await chat.say(run, "ok")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            rig.session.on_cycle_mode()                    # accept-edits: the write needs no prompt
            await rig.send("w")
            await rig.until(lambda: len(rig.chat.results) == 1, "a result with no prompt")
            assert rig.chat.starts[0]["payload"]["mode"] == "accept-edits"
    run_async(scenario())


def test_plan_exit_approval_changes_the_mode_and_the_status(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "plan_exit", {"plan": "1. do it"}, "R1")
        elif msg["t"] == "tool_result":
            await chat.finish(run, cid, "plan_exit", "ok")
            await chat.say(run, "ok")

    async def scenario():
        async with Rig(tmp_path, Chat(handler), mode="plan") as rig:
            await rig.send("plan please")
            await ask_once(rig, "a")                       # plan_exit is only ever allowed once
            await rig.until(lambda: rig.session.status.mode == "ask", "the mode change")
            assert "mode: ask" in rig.ui.status[0]
            assert rig.ui.has("the mode is now ask")
            assert rig.chat.approvals[0]["scope"] == "once"
    run_async(scenario())


def test_without_a_project_settings_file_there_is_no_ignored_line(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            assert not any("ignored" in l for l in rig.ui.status)
    run_async(scenario())


def test_an_allow_rule_in_the_project_settings_does_not_take_effect(tmp_path):
    cid = call_id()

    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "bash", {"command": "echo hi"}, "R3")
        elif msg["t"] == "approval":
            await chat.finish(run, cid, "bash", "denied")

    async def scenario():
        files = {".m2c/settings.toml": '[permissions]\nallow = ["bash(echo:*)"]\n'}
        async with Rig(tmp_path, Chat(handler), files=files) as rig:
            assert any("project settings (.m2c/settings.toml) are ignored" in l for l in rig.ui.status)
            await rig.send("go")
            await rig.until(lambda: rig.session.approval_active, "a prompt although the project file allows it")
            rig.session.on_key("n")
    run_async(scenario())


# --------------------------------------------------------------------------------------------- budget
def test_paused_budget_offers_g_and_g_asks_for_more(tmp_path):
    from m2c.tui.grant import GrantResult
    calls = []

    async def grant(run_id):
        calls.append(run_id)
        return GrantResult("granted", state="running", ceiling=20.0, spent=10.0, granted=10.0)

    async def handler(chat, msg):
        if msg["t"] == "user_message":
            run = msg["run_id"]
            await chat.emit(run, type="state_changed", state="running")
            await chat.emit(run, type="state_changed", state="paused_budget")

    async def scenario():
        async with Rig(tmp_path, Chat(handler), grant_fn=grant) as rig:
            await rig.send("go")
            await rig.until(lambda: rig.session.grant_offered, "the offer")
            assert rig.ui.has("Press g to ask for more credits")
            assert "paused budget" in rig.ui.status[0]
            assert rig.session.captures_keys(True) and not rig.session.captures_keys(False)
            rig.session.on_key("g")
            await rig.until(lambda: calls, "the grant call")
            await rig.until(lambda: rig.ui.has("More credits were granted"))
            assert calls == [rig.chat.user_messages[0][0]]
    run_async(scenario())


def test_a_message_after_a_pause_ends_that_run_and_starts_a_new_one_with_history(tmp_path):
    async def handler(chat, msg):
        if msg["t"] == "user_message":
            run = msg["run_id"]
            if msg["payload"]["text"] == "first":
                await chat.say(run, "answer one")
                await chat.emit(run, type="state_changed", state="paused_error")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("first")
            await rig.until(lambda: rig.session.status.paused == "paused_error")
            await rig.send("second")
            await rig.until(lambda: len(rig.chat.starts) == 2)
            assert rig.chat.cancels == [rig.chat.user_messages[0][0]]
            history = rig.chat.starts[1]["payload"]["history"]
            assert history == [{"role": "user", "text": "first"}, {"role": "assistant", "text": "answer one"}]
    run_async(scenario())


def test_budget_warning_is_shown_as_a_notice_in_credits(tmp_path):
    from link_fakes import env

    async def handler(chat, msg):
        if msg["t"] == "user_message":
            run = msg["run_id"]
            chat.seq[run] = chat.seq.get(run, 0) + 1
            await chat.conn.send(env("budget_warning", {"scope": "run", "spent_pct": 80}, run_id=run, seq=chat.seq[run]))

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("go")
            await rig.until(lambda: rig.ui.has("This run has used 80% of its credit limit."))
            assert "$" not in rig.ui.text()
    run_async(scenario())


# --------------------------------------------------------------------------------------------- hostile text and logs
def test_hostile_server_text_never_reaches_the_ui_unclean(tmp_path):
    esc = chr(27)

    async def handler(chat, msg):
        if msg["t"] == "user_message":
            await chat.say(msg["run_id"], f"hello {esc}]0;pwned\x07 and {esc}[31mred{esc}[0m [bold]x[/bold]")

    async def scenario():
        async with Rig(tmp_path, Chat(handler)) as rig:
            await rig.send("go")
            await rig.until(lambda: rig.ui.has("[bold]x[/bold]"))
            assert esc not in rig.ui.text() and "pwned" not in rig.ui.text()
            replies = [d["text"] for t, d in rig.log() if t == "assistant.message"]
            assert esc not in replies[0]
    run_async(scenario())


def test_logs_carry_no_message_content(tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    marker = "quokka-needle-7431"

    async def scenario():
        async with Rig(tmp_path, Chat(reply_to_user(f"the {marker} reply"))) as rig:
            await rig.send(f"please {marker}")
            await rig.until(lambda: rig.ui.has(marker + " reply"))
    run_async(scenario())
    assert marker not in caplog.text


def test_the_transcript_is_written_only_to_the_store(tmp_path):
    marker = "quokka-needle-7431"

    async def scenario():
        async with Rig(tmp_path, Chat(reply_to_user(f"the {marker} reply"))) as rig:
            await rig.send(f"please {marker}")
            await rig.until(lambda: "assistant.message" in rig.log_types())
    run_async(scenario())
    hits = []
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "state.db" and not path.name.startswith("state.db"):
            if marker.encode() in path.read_bytes():
                hits.append(path)
    assert hits == []


# --------------------------------------------------------------------------------------------- completion
def test_at_completion_never_offers_a_protected_file_or_a_symlink(tmp_path):
    async def scenario():
        files = {"src/app.py": "x", ".env": "SECRET=1", "docs/readme.md": "r"}
        async with Rig(tmp_path, Chat(), files=files) as rig:
            os.symlink(str(rig.project / "src" / "app.py"), str(rig.project / "link.py"))
            found = rig.session.complete("look at @")
            assert "src/app.py" in found and "docs/readme.md" in found
            assert ".env" not in found and "link.py" not in found
            assert rig.session.complete("no mention here") == []
    run_async(scenario())


# --------------------------------------------------------------------------------------------- start and stop
def test_an_unreachable_server_ends_the_start_with_exit_5_and_a_line(tmp_path):
    from link_fakes import FakeServer, make_link
    from m2c.store import open_db
    from m2c.tui.session import EXIT_UNREACHABLE, Session
    from tui_rig import FakeUI

    async def scenario():
        ui = FakeUI()
        store = open_db()
        project = tmp_path / "proj"
        project.mkdir()
        async with FakeServer() as dead:               # no script: every connection is closed at once
            session = Session(store, ui, project_path=str(project),
                              link_factory=lambda st, **kw: make_link(st, dead, **kw))
            assert await session.start(1.0) == EXIT_UNREACHABLE
        assert ui.has("Could not reach the server")
        store.close()
    run_async(scenario())


def test_a_revoked_device_ends_the_start_with_exit_4(tmp_path):
    from link_fakes import FakeServer, make_link, reject_script
    from m2c.store import open_db
    from m2c.tui.session import EXIT_REVOKED, Session
    from tui_rig import FakeUI

    async def scenario():
        ui = FakeUI()
        store = open_db()
        project = tmp_path / "proj"
        project.mkdir()
        async with FakeServer(reject_script("device_revoked", 4403)) as srv:
            session = Session(store, ui, project_path=str(project),
                              link_factory=lambda st, **kw: make_link(st, srv, **kw))
            assert await session.start(3.0) == EXIT_REVOKED
        assert ui.has("This device was revoked")
        store.close()
    run_async(scenario())


def test_leaving_cancels_the_open_run(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat(reply_to_user())) as rig:
            await rig.send("hi")
            await rig.until(lambda: rig.ui.has("Hello there."))
            run = rig.chat.user_messages[0][0]
            await rig.session.close()
            assert rig.chat.cancels == [run]
    run_async(scenario())


def test_the_ui_implementations_have_the_protocol_methods():
    from tui_rig import FakeUI
    for cls in (FakeUI,):
        for name in ("show", "set_status", "set_prompt", "run"):
            assert callable(getattr(cls, name))
    pytest_importorskip = __import__("pytest").importorskip
    pytest_importorskip("prompt_toolkit")
    from m2c.tui.app import PromptToolkitUI
    for name in ("show", "set_status", "set_prompt", "run"):
        assert callable(getattr(PromptToolkitUI, name))
