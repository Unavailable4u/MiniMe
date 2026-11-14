"""
m2c/tests/sandbox/test_netpolicy_prompt.py -- Patch C11b: the prompt flow of m2c.sandbox.netpolicy.

allow_session, allow_always (saved, reloaded by a new policy), deny, timeout, a broken prompt, many simultaneous
requests for one host sharing ONE prompt, a cancelled waiter not cancelling the others, the cap on pending prompts, the
deny cooldown, no prompt at all (deny plus the "unprompted denial" flag), the records the policy hands out (host,
decision word, scope: nothing else), and the cross-loop helper.

Place this file at: m2c/tests/sandbox/test_netpolicy_prompt.py
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from m2c.sandbox.netpolicy import (
    ALLOW, DENY, AllowFile, Decision, NetworkPolicy, prompt_on_loop,
)


class Scripted:
    """A prompt that records its calls and answers from a script (or waits for a gate)."""

    def __init__(self, *answers, gate: asyncio.Event | None = None):
        self.answers = list(answers)
        self.calls: list[tuple[str, int, str]] = []
        self.gate = gate

    async def __call__(self, host, port, label):
        self.calls.append((host, port, label))
        if self.gate is not None:
            await self.gate.wait()
        return self.answers.pop(0) if self.answers else "deny"


def run(coro):
    return asyncio.run(coro)


def test_allow_session_allows_this_and_later_requests_without_asking_again():
    async def go():
        prompt = Scripted("allow_session")
        p = NetworkPolicy(prompt=prompt)
        first = await p.decide("new.example", 443, "pip install x")
        second = await p.decide("NEW.example.", 443, "other")
        return prompt.calls, first, second

    calls, first, second = run(go())
    assert calls == [("new.example", 443, "pip install x")]       # the normalized host and the label, nothing more
    assert first == Decision(ALLOW, "session") and second == Decision(ALLOW, "session")


def test_allow_session_is_not_saved(tmp_path):
    async def go():
        store = AllowFile(tmp_path / "network.json")
        p = NetworkPolicy(prompt=Scripted("allow_session"), store=store)
        await p.decide("new.example", 443)
        return store

    store = run(go())
    assert not store.path.exists()
    assert NetworkPolicy(store=store).check("new.example", 443).action == "ask"


def test_allow_always_is_saved_and_a_new_policy_reloads_it(tmp_path):
    async def go():
        store = AllowFile(tmp_path / "network.json")
        p = NetworkPolicy(prompt=Scripted("allow_always"), store=store)
        d = await p.decide("Saved.Example", 443)
        return d

    d = run(go())
    assert d == Decision(ALLOW, "always")
    assert AllowFile(tmp_path / "network.json").load() == ["saved.example"]
    fresh = NetworkPolicy(store=AllowFile(tmp_path / "network.json"))
    assert fresh.check("saved.example", 443) == Decision(ALLOW, "listed")        # no prompt needed any more
    # and the next decision in the SAME policy does not ask again either
    async def again():
        prompt = Scripted("deny")
        p = NetworkPolicy(prompt=prompt, store=AllowFile(tmp_path / "network.json"))
        return await p.decide("saved.example", 443), prompt.calls

    verdict, calls = run(again())
    assert verdict.action == ALLOW and calls == []


def test_allow_always_still_allows_when_the_file_cannot_be_written(tmp_path):
    blocked = tmp_path / "network.json"
    blocked.mkdir()                                  # the store cannot write here
    async def go():
        p = NetworkPolicy(prompt=Scripted("allow_always"), store=AllowFile(blocked))
        return await p.decide("a.example", 443), p.check("a.example", 443)

    first, later = run(go())
    assert first.action == ALLOW and later.action == ALLOW        # allowed for the session, just not remembered


def test_deny_denies_and_is_remembered_for_the_cooldown():
    async def go():
        now = [100.0]
        prompt = Scripted("deny", "allow_session")
        p = NetworkPolicy(prompt=prompt, deny_cooldown_s=15, clock=lambda: now[0])
        first = await p.decide("x.example", 443)
        second = await p.decide("x.example", 443)              # a retry loop: no new question
        now[0] += 16
        third = await p.decide("x.example", 443)               # asked again after the cooldown
        return first, second, third, len(prompt.calls)

    first, second, third, asked = run(go())
    assert first == Decision(DENY, "user")
    assert second == Decision(DENY, "cooldown")
    assert third == Decision(ALLOW, "session")
    assert asked == 2


def test_unknown_answers_are_a_deny():
    async def go():
        return await NetworkPolicy(prompt=Scripted("yes please")).decide("x.example", 443)

    assert run(go()).action == DENY


def test_a_timeout_is_a_deny():
    async def go():
        gate = asyncio.Event()                                   # never set: the person never answers
        p = NetworkPolicy(prompt=Scripted(gate=gate), prompt_timeout_s=0.05)
        return await p.decide("x.example", 443), p._pending

    verdict, pending = run(go())
    assert verdict == Decision(DENY, "timeout")
    assert pending == {}


def test_the_default_timeout_is_about_a_minute():
    assert NetworkPolicy().prompt_timeout_s == 60


def test_a_broken_prompt_is_a_deny():
    async def boom(host, port, label):
        raise RuntimeError("the terminal went away")

    async def go():
        return await NetworkPolicy(prompt=boom).decide("x.example", 443)

    assert run(go()) == Decision(DENY, "prompt_error")


def test_many_simultaneous_requests_for_one_host_share_one_prompt():
    async def go():
        gate = asyncio.Event()
        prompt = Scripted("allow_session", gate=gate)
        p = NetworkPolicy(prompt=prompt)
        tasks = [asyncio.ensure_future(p.decide("one.example", 443, f"cmd {i}")) for i in range(25)]
        await asyncio.sleep(0.05)
        assert len(prompt.calls) == 1                           # ONE question while 25 callers wait
        gate.set()
        return await asyncio.gather(*tasks), prompt.calls

    results, calls = run(go())
    assert len(calls) == 1
    assert all(r == Decision(ALLOW, "session") for r in results)


def test_a_waiter_that_gives_up_does_not_cancel_the_question_for_the_others():
    async def go():
        gate = asyncio.Event()
        prompt = Scripted("allow_session", gate=gate)
        p = NetworkPolicy(prompt=prompt)
        first = asyncio.ensure_future(p.decide("one.example", 443))
        second = asyncio.ensure_future(p.decide("one.example", 443))
        await asyncio.sleep(0.02)
        first.cancel()
        await asyncio.sleep(0.02)
        gate.set()
        return await second, first.cancelled()

    verdict, cancelled = run(go())
    assert cancelled and verdict.action == ALLOW


def test_different_hosts_are_asked_separately_but_only_a_few_at_once():
    async def go():
        gate = asyncio.Event()
        prompt = Scripted("allow_session", "allow_session", "allow_session", gate=gate)
        p = NetworkPolicy(prompt=prompt, max_pending_prompts=3)
        tasks = [asyncio.ensure_future(p.decide(f"h{i}.example", 443)) for i in range(6)]
        await asyncio.sleep(0.05)
        asked = len(prompt.calls)
        gate.set()
        results = await asyncio.gather(*tasks)
        return asked, results

    asked, results = run(go())
    assert asked == 3
    assert [r.scope for r in results].count("too_many_prompts") == 3
    assert [r.action for r in results].count(ALLOW) == 3


def test_no_prompt_means_deny_and_the_flag_is_counted():
    async def go():
        p = NetworkPolicy()
        a = await p.decide("x.example", 443)
        b = await p.decide("y.example", 443)
        listed = await NetworkPolicy(allow=["ok.example"]).decide("ok.example", 443)
        return a, b, p.unprompted_denials, listed

    a, b, count, listed = run(go())
    assert a == Decision(DENY, "no_prompt") and b == Decision(DENY, "no_prompt")
    assert count == 2
    assert listed.action == ALLOW


def test_denials_that_need_no_question_are_not_counted_as_unprompted():
    async def go():
        p = NetworkPolicy()
        await p.decide("127.0.0.1", 443)           # an IP literal is denied outright
        await p.decide("x.example", 80)            # a wrong port too
        return p.unprompted_denials

    assert run(go()) == 0


def test_set_prompt_installs_a_prompt_later():
    async def go():
        p = NetworkPolicy()
        assert not p.has_prompt
        p.set_prompt(Scripted("allow_session"))
        return p.has_prompt, await p.decide("x.example", 443)

    has, verdict = run(go())
    assert has and verdict.action == ALLOW


def test_records_hold_only_host_decision_and_scope():
    async def go():
        records = []
        p = NetworkPolicy(allow=["ok.example"], prompt=Scripted("deny"), record=records.append)
        await p.decide("ok.example", 443, "a secret-looking label")
        await p.decide("no.example", 443, "another label")
        await p.decide("127.0.0.1", 443)
        return records

    records = run(go())
    assert records == [
        {"host": "ok.example", "decision": "allow", "scope": "listed"},
        {"host": "no.example", "decision": "deny", "scope": "user"},
        {"host": "127.0.0.1", "decision": "deny", "scope": "ip_literal"},
    ]
    assert "label" not in repr(records)


def test_a_failing_recorder_never_changes_the_decision():
    def bad(_):
        raise OSError("disk full")

    async def go():
        return await NetworkPolicy(allow=["ok.example"], record=bad).decide("ok.example", 443)

    assert run(go()).action == ALLOW


def test_aclose_cancels_a_waiting_question():
    async def go():
        gate = asyncio.Event()
        p = NetworkPolicy(prompt=Scripted(gate=gate))
        waiter = asyncio.ensure_future(p.decide("x.example", 443))
        await asyncio.sleep(0.02)
        await p.aclose()
        results = await asyncio.gather(waiter, return_exceptions=True)
        return results[0], p._pending

    result, pending = run(go())
    assert isinstance(result, BaseException) or result.action == DENY      # never an allow
    assert pending == {}


# --------------------------------------------------------------------------
# crossing event loops (the proxy's thread -> the terminal's loop)
# --------------------------------------------------------------------------
def _terminal_loop():
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def runner():
        asyncio.set_event_loop(loop)
        loop.call_soon(started.set)
        loop.run_forever()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    started.wait(5)
    return loop, t


def _stop(loop, t):
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
    loop.close()


def test_prompt_on_loop_runs_the_prompt_on_the_other_loop_and_returns_the_answer():
    loop, t = _terminal_loop()
    try:
        seen = {}

        async def ui(host, port, label):
            seen["thread"] = threading.current_thread()
            return "allow_always"

        async def go():
            return await prompt_on_loop(ui, loop)("x.example", 443, "label")

        assert run(go()) == "allow_always"
        assert seen["thread"] is t
    finally:
        _stop(loop, t)


def test_prompt_on_loop_maps_unknown_answers_to_deny():
    loop, t = _terminal_loop()
    try:
        async def ui(host, port, label):
            return "maybe"

        assert run(prompt_on_loop(ui, loop)("x.example", 443, "")) == "deny"
    finally:
        _stop(loop, t)


def test_a_timeout_withdraws_the_question_on_the_terminal_loop():
    loop, t = _terminal_loop()
    try:
        state = {"cancelled": threading.Event(), "started": threading.Event()}

        async def ui(host, port, label):
            state["started"].set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"].set()
                raise

        async def go():
            p = NetworkPolicy(prompt=prompt_on_loop(ui, loop), prompt_timeout_s=0.1)
            return await p.decide("x.example", 443)

        assert run(go()) == Decision(DENY, "timeout")
        assert state["started"].is_set()
        assert state["cancelled"].wait(5)
    finally:
        _stop(loop, t)


@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")
def test_a_closed_terminal_loop_is_a_deny_not_a_crash():
    loop, t = _terminal_loop()
    _stop(loop, t)

    async def ui(host, port, label):
        return "allow_session"

    async def go():
        return await NetworkPolicy(prompt=prompt_on_loop(ui, loop)).decide("x.example", 443)

    assert run(go()).action == DENY
