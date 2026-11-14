"""
m2c/tests/tui/test_tui_network_prompt.py -- Patch C11b: the terminal's "Allow network access to <host>?" prompt.

Two parts.  The pure part: ``DomainPromptMachine`` (which key means what; nothing but y, a, n and Esc ever decides) and
``describe_domain_request`` (what is shown: the host, the port and the short label, nothing else).  The session part runs
the REAL ``Session`` on the usual rig with a REAL proxy service whose policy asks through the session: the question comes
from the proxy's own thread and loop, is shown on the terminal's loop one at a time, and every way of leaving it
(a key, Esc, Ctrl-C, a timeout, the session closing) ends as the policy's own answer, with a deny for all but y and a.

Place this file at: m2c/tests/tui/test_tui_network_prompt.py
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from tui_rig import Chat, Rig, run_async

from m2c.paths import config_dir
from m2c.sandbox import netproxy
from m2c.sandbox.netpolicy import AllowFile
from m2c.tui.approval import DOMAIN_KEY_HELP, DomainDone, DomainPromptMachine, Pending, describe_domain_request


# ==========================================================================
# the pure part
# ==========================================================================
@pytest.mark.parametrize("key, answer", [
    ("y", "allow_session"), ("Y", "allow_session"), ("a", "allow_always"), ("A", "allow_always"),
    ("n", "deny"), ("N", "deny"), ("escape", "deny"),
])
def test_the_keys_that_decide(key, answer):
    step = DomainPromptMachine().feed(key)
    assert step == DomainDone(answer)


@pytest.mark.parametrize("key", ["d", "e", "v", "g", "1", " ", "x", "?", "yy", "", "enter", "tab", "\t", "\r", "c-i",
                                 "up", "ESC", "Escape", None, 5, "\x1b"])
def test_no_other_key_ever_decides(key):
    machine = DomainPromptMachine()
    assert isinstance(machine.feed(key), Pending)
    assert machine.answer is None


def test_a_printable_stray_key_gets_a_hint_and_a_control_key_gets_none():
    assert DomainPromptMachine().feed("x").message == "Press y, a or n."
    assert DomainPromptMachine().feed("tab").message == ""


def test_once_decided_it_stays_decided():
    machine = DomainPromptMachine()
    assert machine.feed("n") == DomainDone("deny")
    assert isinstance(machine.feed("y"), Pending)
    assert machine.answer == "deny"


def test_the_prompt_text_names_the_host_the_port_the_label_and_the_keys():
    blocks = describe_domain_request("pypi.org", 443, "pip install requests")
    text = "\n".join(b.text for b in blocks)
    assert blocks[0].text.startswith("Allow network access to pypi.org?")
    assert "port 443" in text and "pip install requests" in text and DOMAIN_KEY_HELP in text
    assert "[y]" in DOMAIN_KEY_HELP and "[a]" in DOMAIN_KEY_HELP and "[n]" in DOMAIN_KEY_HELP


def test_the_prompt_text_shows_nothing_about_the_command_when_there_is_no_label():
    blocks = describe_domain_request("pypi.org", 443, "")
    assert not any("command" in b.text for b in blocks) and len(blocks) == 2


def test_hostile_text_cannot_inject_terminal_controls():
    blocks = describe_domain_request("evil\x1b[2Jhost.example\r\nAllow? y", 443, "ls\x1b]0;title\x07\x1b[31m red")
    for block in blocks:
        assert "\x1b" not in block.text and "\r" not in block.text and "\x07" not in block.text
        assert "\n" not in block.text


def test_a_label_and_a_host_are_cut_to_a_sane_length():
    blocks = describe_domain_request("a" * 1000 + ".example", 443, "x" * 1000)
    assert all(len(b.text) < 400 for b in blocks)


def test_a_strange_port_is_shown_as_unknown_not_crashed_on():
    assert "port ?" in describe_domain_request("a.example", "443; rm", "")[0].text


# ==========================================================================
# the session part
# ==========================================================================
def start_service(rig, **kw):
    """A real proxy service whose policy asks through the session (as the shell tool builds it)."""
    session = rig.session
    service = netproxy.make_service(prompt=session._network_prompt, record=session._network_record, **kw)
    service.start()
    return service


async def ask_from_proxy(service, host, port=443, label="label"):
    """Make the proxy's policy decide, on the proxy's own thread and loop, and await the answer here."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: service.run(service.policy.decide(host, port, label), 30))


def shown(rig, host):
    return rig.ui.has(f"Allow network access to {host}?")


def test_the_session_hands_the_shell_tool_its_prompt_and_recorder(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            cfg = rig.session._tool_context.shell_config
            assert cfg.network == "prompt"
            assert cfg.network_prompt == rig.session._network_prompt
            assert cfg.network_record == rig.session._network_record
    run_async(scenario())


def test_the_question_is_shown_and_y_allows_for_the_session(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            try:
                task = asyncio.ensure_future(ask_from_proxy(service, "pypi.org", 443, "pip install requests"))
                await rig.until(lambda: rig.session.modal is None and rig.session._domain is not None, "the prompt")
                assert shown(rig, "pypi.org") and rig.ui.has("pip install requests") and rig.ui.has("port 443")
                assert rig.ui.prompt[0].startswith("allow network?")
                assert rig.session.captures_keys(False) and rig.session.captures_keys(True)
                assert not rig.session.approval_active
                rig.session.on_key("y")
                verdict = await asyncio.wait_for(task, 10)
                assert (verdict.action, verdict.scope) == ("allow", "session")
                assert rig.session._domain is None and rig.ui.prompt[0] == "> "
                assert not rig.session.captures_keys(True)
                assert not AllowFile().path.exists()                       # a session allow is not saved
            finally:
                service.close()
    run_async(scenario())


def test_a_saves_the_domain_to_the_users_network_file(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            try:
                task = asyncio.ensure_future(ask_from_proxy(service, "Saved.Example"))
                await rig.until(lambda: rig.session._domain is not None, "the prompt")
                rig.session.on_key("a")
                verdict = await asyncio.wait_for(task, 10)
                assert (verdict.action, verdict.scope) == ("allow", "always")
                path = config_dir() / "network.json"
                assert json.loads(path.read_text())["allow"] == ["saved.example"]
                assert oct(os.stat(path).st_mode & 0o777) == "0o600"
            finally:
                service.close()
    run_async(scenario())


@pytest.mark.parametrize("how", ["n", "escape", "interrupt"])
def test_n_escape_and_ctrl_c_all_deny(tmp_path, how):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            try:
                task = asyncio.ensure_future(ask_from_proxy(service, "deny.example"))
                await rig.until(lambda: rig.session._domain is not None, "the prompt")
                if how == "n":
                    rig.session.on_key("n")
                elif how == "escape":
                    rig.session.on_escape()
                else:
                    rig.session.on_interrupt()
                verdict = await asyncio.wait_for(task, 10)
                assert (verdict.action, verdict.scope) == ("deny", "user")
                assert rig.session._domain is None and rig.ui.prompt[0] == "> "
                assert not rig.session.exit_requested.is_set()              # Ctrl-C on a prompt does not quit the app
                assert not AllowFile().path.exists()
            finally:
                service.close()
    run_async(scenario())


@pytest.mark.parametrize("key", ["x", "d", "e", "g", "1", " ", "tab", "enter"])
def test_other_keys_do_not_answer_and_the_prompt_stays(tmp_path, key):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            try:
                task = asyncio.ensure_future(ask_from_proxy(service, "stay.example"))
                await rig.until(lambda: rig.session._domain is not None, "the prompt")
                rig.session.on_key(key)
                await asyncio.sleep(0.1)
                assert not task.done() and rig.session._domain is not None
                rig.session.on_key("n")
                assert (await asyncio.wait_for(task, 10)).action == "deny"
            finally:
                service.close()
    run_async(scenario())


def test_a_stray_printable_key_shows_the_hint(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            try:
                task = asyncio.ensure_future(ask_from_proxy(service, "hint.example"))
                await rig.until(lambda: rig.session._domain is not None, "the prompt")
                rig.session.on_key("q")
                assert rig.ui.has("Press y, a or n.")
                rig.session.on_key("n")
                await asyncio.wait_for(task, 10)
            finally:
                service.close()
    run_async(scenario())


def test_prompts_come_one_at_a_time(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            try:
                first = asyncio.ensure_future(ask_from_proxy(service, "one.example"))
                second = asyncio.ensure_future(ask_from_proxy(service, "two.example"))
                await rig.until(lambda: shown(rig, "one.example") or shown(rig, "two.example"), "a prompt")
                await asyncio.sleep(0.3)
                both = shown(rig, "one.example") and shown(rig, "two.example")
                assert not both                                              # the other waits for its turn
                current = "one.example" if shown(rig, "one.example") else "two.example"
                other = "two.example" if current == "one.example" else "one.example"
                rig.session.on_key("y")
                await rig.until(lambda: shown(rig, other), "the second prompt")
                rig.session.on_key("n")
                verdicts = await asyncio.wait_for(asyncio.gather(first, second), 10)
                assert sorted(v.action for v in verdicts) == ["allow", "deny"]
            finally:
                service.close()
    run_async(scenario())


def test_many_requests_for_one_host_raise_one_prompt(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            try:
                tasks = [asyncio.ensure_future(ask_from_proxy(service, "same.example", 443, f"cmd {i}")) for i in range(8)]
                await rig.until(lambda: rig.session._domain is not None, "the prompt")
                await asyncio.sleep(0.3)
                assert rig.ui.text().count("Allow network access to same.example?") == 1
                rig.session.on_key("y")
                verdicts = await asyncio.wait_for(asyncio.gather(*tasks), 10)
                assert all(v.action == "allow" for v in verdicts)
            finally:
                service.close()
    run_async(scenario())


def test_a_timeout_denies_and_takes_the_question_off_the_screen(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            service.policy.prompt_timeout_s = 0.3
            try:
                verdict = await asyncio.wait_for(ask_from_proxy(service, "slow.example"), 10)
                assert (verdict.action, verdict.scope) == ("deny", "timeout")
                await rig.until(lambda: rig.session._domain is None, "the prompt to go away")
                assert rig.ui.has("[the network request was withdrawn]")
                assert rig.ui.prompt[0] == "> " and not rig.session.captures_keys(True)
                rig.session.on_key("y")                                      # a late key finds nothing to answer
                assert not AllowFile().path.exists()
            finally:
                service.close()
    run_async(scenario())


def test_the_domain_prompt_waits_while_an_approval_prompt_is_open(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            try:
                rig.session._approval = SimpleNamespace(machine=SimpleNamespace(editing=False))   # an approval is up
                task = asyncio.ensure_future(ask_from_proxy(service, "later.example"))
                await asyncio.sleep(0.4)
                assert rig.session._domain is None and not shown(rig, "later.example")
                rig.session._approval = None
                await rig.until(lambda: shown(rig, "later.example"), "the prompt after the approval")
                rig.session.on_key("n")
                assert (await asyncio.wait_for(task, 10)).action == "deny"
            finally:
                rig.session._approval = None
                service.close()
    run_async(scenario())


def test_closing_the_session_with_a_question_open_is_a_deny(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig)
            try:
                task = asyncio.ensure_future(ask_from_proxy(service, "closing.example"))
                await rig.until(lambda: rig.session._domain is not None, "the prompt")
                await rig.session.close()
                verdict = await asyncio.wait_for(task, 10)
                assert verdict.action == "deny"
                after = await rig.session._network_prompt("x.example", 443, "")
                assert after == "deny"                                        # no loop: a deny, not a crash
            finally:
                service.close()
    run_async(scenario())


def test_closing_the_session_stops_the_proxy_of_its_tool_context(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            context = rig.session._tool_context
            service = netproxy.service_for(context, "k", lambda: netproxy.make_service())
            directory = service.directory
            assert os.path.isdir(directory)
            await rig.session.close()
            assert not os.path.exists(directory) and netproxy._SERVICES.get(context) is None
    run_async(scenario())


def test_decisions_are_logged_as_host_decision_scope_and_nothing_else(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            rig.session._network_record({"host": "a.example", "decision": "deny", "scope": "user",
                                         "path": "/secret/path", "payload": "zzz", "label": "pip install"})
            await rig.until(lambda: "network" in rig.log_types(), "the event")
            events = [d for t, d in rig.log() if t == "network"]
            assert events == [{"host": "a.example", "decision": "deny", "scope": "user"}]
    run_async(scenario())


def test_a_record_from_the_proxys_thread_reaches_the_log(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            service = start_service(rig, allow=("ok.example",))
            try:
                verdict = await ask_from_proxy(service, "ok.example")
                assert verdict.action == "allow"
                await rig.until(lambda: "network" in rig.log_types(), "the event")
                assert [d for t, d in rig.log() if t == "network"] == [
                    {"host": "ok.example", "decision": "allow", "scope": "listed"}]
            finally:
                service.close()
    run_async(scenario())


def test_a_record_after_the_session_closed_is_dropped_quietly(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            await rig.session.close()
            rig.session._network_record({"host": "a.example", "decision": "deny", "scope": "user"})
    run_async(scenario())


def test_the_approval_prompt_still_works_as_before_with_the_domain_prompt_in_the_session(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat()) as rig:
            assert not rig.session.approval_active and rig.session._domain is None
            assert not rig.session.captures_keys(True)
    run_async(scenario())
