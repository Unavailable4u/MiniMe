"""
tests/integration/test_loop_v4_laya_panel_gate.py

eo/loop_v4.py's _get_decision(), Laya pre-gate for the panel escalation
(Tier 3 of the Laya migration, 2026-09-28) -- see eo/laya_routing.py.

Calls _get_decision() directly (a module-level function, same "private
but directly tested" convention tests/integration/test_gatekeeper.py
already uses for _run_gatekeeper()) rather than going through main(), to
avoid CLI argv parsing unrelated to what's under test here.

The suite-wide conftest.py autouse fixture forces
eo.laya_routing.PANEL_GATE_MODE = "off"; each test below opts back in by
monkeypatching it on the laya_routing module directly (loop_v4.py imports
that module, not individual names from it, so
`loop_v4.laya_routing.PANEL_GATE_MODE` and `laya_routing.PANEL_GATE_MODE`
are the same attribute -- same conftest.py generate_text gotcha this
file's sibling tests already document elsewhere in this suite).
laya_gate.predict is monkeypatched -- no model is loaded, and the real
eo_panel.run_panel is replaced with a stub, so this never makes a real
LLM call either.
"""
import pytest

from eo import laya_routing, loop_v4

LOW_CONF_TIER0 = {
    "path": "instant", "tier": 0, "directed_task_type": None, "confidence": 0.3,
    "suggested_agents": ["responder"], "reasoning": "uncertain",
    "domain": None, "execution_order": [], "parallel_groups": [],
}

LOW_CONF_TIER1 = {**LOW_CONF_TIER0, "path": "direct", "tier": 1,
                  "suggested_agents": ["implementer"]}

HIGH_CONF_TIER0 = {**LOW_CONF_TIER0, "confidence": 0.95}   # should_escalate is False

TIER2_DRAFT = {**LOW_CONF_TIER0, "path": "fixed", "tier": 2,
               "directed_task_type": "refactor", "confidence": 0.95}   # always escalates (tier==2)


def _choice_reply(probs):
    return {"answers": {"path": {"type": "choice", "probabilities": probs,
                                 "choice": max(probs, key=probs.get)}}}


PANEL_RAISED = {**LOW_CONF_TIER0, "tier": 3, "panel_reviewed": True}   # a distinguishable "panel ran" result
PANEL_CONFIRMED = {**LOW_CONF_TIER0, "panel_reviewed": True}           # tier unchanged, panel still ran


def _stub_common(monkeypatch, draft: dict, panel_result: dict = None):
    """Same stubbing shape as test_loop_v4_tier3.py's _stub_common(),
    scoped to just what _get_decision() itself touches."""
    monkeypatch.setattr(loop_v4, "classify", lambda task_text, context=None, session_id=None: dict(draft))
    panel_calls = []

    def fake_run_panel(task_text, d):
        panel_calls.append(task_text)
        return dict(panel_result) if panel_result is not None else dict(d)
    monkeypatch.setattr(loop_v4.eo_panel, "run_panel", fake_run_panel)
    monkeypatch.setattr(loop_v4.routing_memory, "retrieve_similar_outcomes", lambda *a, **k: "")
    monkeypatch.setattr(loop_v4.conversation_memory, "get_light_context", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4, "write", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4, "build_execution_graph", lambda *a, **k: [])
    return panel_calls


def test_gate_off_by_default_panel_still_runs_as_before(monkeypatch):
    """Sanity check on the conftest fixture: with the suite default, no
    Laya call happens and a low-confidence draft still escalates."""
    calls = []
    monkeypatch.setattr(laya_routing.laya_gate, "predict", lambda *a, **k: calls.append(1))
    panel_calls = _stub_common(monkeypatch, LOW_CONF_TIER0, PANEL_CONFIRMED)

    decision = loop_v4._get_decision("some task", None, None, session_id="s")

    assert panel_calls, "panel must still run when the gate is off"
    assert decision["panel_reviewed"] is True
    assert calls == []


def test_gate_observe_mode_still_runs_the_panel(monkeypatch):
    monkeypatch.setattr(laya_routing, "PANEL_GATE_MODE", "observe")
    monkeypatch.setattr(laya_routing.laya_gate, "predict", lambda *a, **k: _choice_reply(
        {"instant": 0.99, "direct": 0.005, "fixed": 0.003, "adaptive": 0.002}))
    panel_calls = _stub_common(monkeypatch, LOW_CONF_TIER0, PANEL_CONFIRMED)

    decision = loop_v4._get_decision("trivial task", None, None, session_id="s")

    assert panel_calls, "observe mode must never skip the real panel call"
    assert decision["panel_reviewed"] is True
    stats = laya_routing.get_panel_gate_stats()
    assert stats["would_skip"] == 1


def test_gate_on_mode_skips_panel_when_laya_is_confident(monkeypatch):
    monkeypatch.setattr(laya_routing, "PANEL_GATE_MODE", "on")
    monkeypatch.setattr(laya_routing, "PANEL_GATE_THRESHOLD", 0.90)
    monkeypatch.setattr(laya_routing.laya_gate, "predict", lambda *a, **k: _choice_reply(
        {"instant": 0.99, "direct": 0.005, "fixed": 0.003, "adaptive": 0.002}))
    panel_calls = _stub_common(monkeypatch, LOW_CONF_TIER0, PANEL_CONFIRMED)

    decision = loop_v4._get_decision("trivial task", None, None, session_id="s")

    assert panel_calls == [], "the panel must never be called when Laya's verdict is trusted"
    assert decision == LOW_CONF_TIER0   # the Inspector's own draft, unmodified
    assert laya_routing.get_panel_gate_stats()["skipped"] == 1


def test_gate_on_mode_still_escalates_when_laya_is_not_confident(monkeypatch):
    monkeypatch.setattr(laya_routing, "PANEL_GATE_MODE", "on")
    monkeypatch.setattr(laya_routing, "PANEL_GATE_THRESHOLD", 0.90)
    monkeypatch.setattr(laya_routing.laya_gate, "predict", lambda *a, **k: _choice_reply(
        {"instant": 0.5, "direct": 0.2, "fixed": 0.2, "adaptive": 0.1}))
    panel_calls = _stub_common(monkeypatch, LOW_CONF_TIER0, PANEL_RAISED)

    decision = loop_v4._get_decision("ambiguous task", None, None, session_id="s")

    assert panel_calls, "an unconfident Laya verdict must not block a real escalation"
    assert decision["tier"] == 3


def test_gate_fails_open_to_the_panel_on_laya_error(monkeypatch):
    monkeypatch.setattr(laya_routing, "PANEL_GATE_MODE", "on")

    def boom(*a, **k):
        raise RuntimeError("laya unavailable")
    monkeypatch.setattr(laya_routing.laya_gate, "predict", boom)
    panel_calls = _stub_common(monkeypatch, LOW_CONF_TIER0, PANEL_CONFIRMED)

    decision = loop_v4._get_decision("some task", None, None, session_id="s")

    assert panel_calls, "a Laya error must fall through to the normal panel call"
    assert decision["panel_reviewed"] is True


LOW_CONF_TIER3 = {**LOW_CONF_TIER0, "path": "adaptive", "tier": 3,
                  "suggested_agents": ["writer"], "confidence": 0.3}   # escalates on low confidence


@pytest.mark.parametrize("draft", [TIER2_DRAFT, LOW_CONF_TIER3])
def test_gate_is_never_consulted_outside_tier_0_and_1(monkeypatch, draft):
    """TIER2_DRAFT always escalates (tier == 2, directed_task_type needs
    validating regardless of confidence) and LOW_CONF_TIER3 escalates on
    low confidence -- both are out of the gate's scope (see
    eo/laya_routing.py's own docstring on why: the panel also validates
    directed_task_type at tier 2 and staffs roles at tier 3, neither of
    which Laya can judge), so no Laya call should happen even in 'on' mode."""
    assert draft["confidence"] < 0.75 or draft["tier"] == 2, "fixture must actually escalate"
    monkeypatch.setattr(laya_routing, "PANEL_GATE_MODE", "on")
    calls = []
    monkeypatch.setattr(laya_routing.laya_gate, "predict", lambda *a, **k: calls.append(1))
    panel_calls = _stub_common(monkeypatch, draft, {**draft, "panel_reviewed": True})

    loop_v4._get_decision("some task", None, None, session_id="s")

    assert calls == [], "Laya must not be consulted for tier 2/3 drafts"
    assert panel_calls, "the real panel must still run for these tiers"


def test_high_confidence_tier0_never_escalates_or_calls_laya(monkeypatch):
    """draft confidence 0.95 >= CONFIDENCE_THRESHOLD -> should_escalate is
    False before the gate is ever reached -- matches
    test_loop_v4_tier0.py's own TIER0_DRAFT shape."""
    monkeypatch.setattr(laya_routing, "PANEL_GATE_MODE", "on")
    calls = []
    monkeypatch.setattr(laya_routing.laya_gate, "predict", lambda *a, **k: calls.append(1))
    panel_calls = _stub_common(monkeypatch, HIGH_CONF_TIER0)

    decision = loop_v4._get_decision("trivial task", None, None, session_id="s")

    assert calls == [] and panel_calls == []
    assert decision == HIGH_CONF_TIER0
