"""Patch S15 -- the loop's one additive change: tools are offered only to a connection that can answer them.

The device says what it can do in `hello.capabilities`; the server keeps that on the connection. S15's wiring
gives the loop a small predicate (`offer_tools(conn) -> bool`); the loop asks it once per turn and, when the answer
is no, offers NO tools to the model and refuses any call the model makes anyway, so that a `tool_proposed` is never
sent to a client that cannot answer it. These tests drive the loop alone (the scripted model, the real S06
modules over their in-memory twins, a recording broker); tests/unit/test_codeagent_wiring_flow.py drives the same
rule through the real router, run handlers and broker.

  * no predicate (the default): exactly what S07 did, every visible tool is offered ........ test_default_*
  * predicate says no: no tools in the request, the reply still streams .................... test_no_tools_*
  * a call the model makes anyway: never forwarded, answered with a fixed text ............ test_a_call_*
  * the predicate is given the turn's connection and asked once per turn ................... test_predicate_*
  * a predicate that raises: no tools (fail closed), the type is logged, never the text ..... test_a_failing_*
  * plan mode and the gate compose: the gate can only remove tools, never widen them ....... test_plan_mode_*
"""
import logging

import pytest

from codeagent import loop as loop_module
from codeagent.loop import MSG_NO_TOOLS, TurnResult
from tests.unit.loop_testkit import CANARY, Reply, Rig, call, catalog, go, protocol_problems

pytest.importorskip("jsonschema", reason="the loop validates tool arguments with jsonschema")


@pytest.fixture
def rigs(monkeypatch):
    made = []

    def make(*script, **kw):
        rig = Rig(monkeypatch, *script, **kw)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.teardown()


def tool_items(rig):
    return [i for i in rig.items() if i["role"] == "tool"]


def test_default_offers_every_visible_tool_as_before(rigs):
    rig = rigs(Reply("hi"))
    go(rig.turn())
    assert rig.adapter.tool_names() == set(catalog().names)


def test_the_gate_adds_a_fixed_text_and_nothing_else_to_the_public_surface():
    assert isinstance(MSG_NO_TOOLS, str) and 0 < len(MSG_NO_TOOLS) < 200
    assert CANARY not in MSG_NO_TOOLS
    assert "offer_tools" in loop_module.AgentLoop.__init__.__code__.co_varnames


def test_no_tools_a_plain_answer_still_streams_and_the_request_has_none(rigs):
    rig = rigs(Reply("A plain answer."), offer_tools=lambda conn: False)
    result = go(rig.turn("explain this"))
    assert result == TurnResult("finished", None, 1, 0)
    assert rig.adapter.requests[0].tools == ()
    assert rig.rec.text() == "A plain answer."
    assert rig.state == "running"
    assert rig.broker.calls == []
    assert protocol_problems(rig.rec.events) == []


def test_a_call_the_model_makes_anyway_is_refused_and_never_forwarded(rigs):
    rig = rigs(Reply("Let me look.", [call("read_file", {"path": "a.py"})]), Reply("I cannot read files here."),
               offer_tools=lambda conn: False)
    result = go(rig.turn("what is in a.py?"))
    assert result.outcome == "finished" and result.steps == 2
    assert rig.broker.calls == []                                   # nothing was proposed to the device
    (tool,) = tool_items(rig)
    assert tool["ok"] is False and tool["output"] == MSG_NO_TOOLS
    assert rig.adapter.requests[1].tools == ()                      # still none on the next step
    assert MSG_NO_TOOLS in rig.adapter.last_messages[-1].content    # the model is told why
    assert protocol_problems(rig.rec.events) == []


def test_a_call_to_an_unknown_tool_keeps_its_own_text_when_tools_are_off(rigs):
    rig = rigs(Reply("", [call("no_such_tool", {})]), Reply("ok"), offer_tools=lambda conn: False)
    go(rig.turn())
    (tool,) = tool_items(rig)
    assert tool["output"] == loop_module.MSG_UNKNOWN_TOOL            # the unknown-tool text is not hidden by the gate


def test_predicate_is_given_the_turns_connection_and_asked_once_per_turn(rigs):
    seen = []

    def offer(conn):
        seen.append(conn)
        return True

    rig = rigs(Reply("", [call("read_file", {"path": "a.py"})]), Reply("done"), offer_tools=offer)
    go(rig.turn())
    assert len(rig.broker.calls) == 1                               # a two-step turn with a call: tools were offered
    assert seen == [rig.conn]                                       # one question, about this turn's connection
    assert rig.adapter.tool_names(0) == set(catalog().names)
    assert rig.adapter.tool_names(1) == set(catalog().names)


def test_predicate_is_asked_again_on_the_next_turn_and_can_change_its_mind(rigs):
    answers = iter([False, True])
    rig = rigs(Reply("first"), Reply("second"), offer_tools=lambda conn: next(answers))
    go(rig.turn("one"))
    go(rig.turn("two"))
    assert rig.adapter.requests[0].tools == ()
    assert rig.adapter.tool_names(1) == set(catalog().names)


def test_a_failing_predicate_means_no_tools_and_only_its_type_is_logged(rigs, caplog):
    def offer(conn):
        raise RuntimeError("boom " + CANARY)

    rig = rigs(Reply("plain"), offer_tools=offer)
    with caplog.at_level(logging.DEBUG, logger="codeagent.loop"):
        result = go(rig.turn())
    assert result.outcome == "finished"
    assert rig.adapter.requests[0].tools == ()
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in log and CANARY not in log


def test_plan_mode_and_the_gate_compose_the_gate_never_widens(rigs):
    on = rigs(Reply("plan"), mode="plan", offer_tools=lambda conn: True)
    go(on.turn())
    names = on.adapter.tool_names()
    assert "plan_exit" in names and names < set(catalog().names)    # plan mode still filters
    off = rigs(Reply("plan"), mode="plan", offer_tools=lambda conn: False)
    go(off.turn())
    assert off.adapter.requests[0].tools == ()
