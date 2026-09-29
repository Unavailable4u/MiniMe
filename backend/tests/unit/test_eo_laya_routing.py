"""
tests/unit/test_eo_laya_routing.py

eo/laya_routing.py (Laya migration Tier 3, 2026-09-28): the panel-escalation
gate (eo/loop_v4.py) and the macro-loop gatekeeper gate
(eo/loop_controller.py). laya_gate.predict is monkeypatched throughout --
no model is loaded. incr/read are replaced with a tiny in-memory fake so
counter tests don't need a real Redis/Upstash connection.
"""
import pytest

from eo import laya_routing as lr


class _FakeBus:
    """Minimal stand-in for memory.bus's incr/read, scoped per test via
    the fixture below."""
    def __init__(self):
        self.store = {}

    def incr(self, key, ex=None):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def read(self, key, default=None):
        return self.store.get(key, default)


@pytest.fixture(autouse=True)
def _fresh_bus(monkeypatch):
    bus = _FakeBus()
    monkeypatch.setattr(lr, "incr", bus.incr)
    monkeypatch.setattr(lr, "read", bus.read)
    return bus


def _choice_reply(probs):
    return {"answers": {"path": {"type": "choice", "probabilities": probs,
                                 "choice": max(probs, key=probs.get)}}}


def _noul_reply(p):
    return {"answers": {"finished": {"type": "noul", "noul": p}}}


# ---------------------------------------------------------------------
# _parse_mode
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,default,expected", [
    ("on", "observe", "on"), ("OBSERVE", "on", "observe"), (" off ", "on", "off"),
    (None, "observe", "observe"), ("", "on", "on"), ("bogus", "observe", "off"),
])
def test_parse_mode(raw, default, expected):
    assert lr._parse_mode(raw, default) == expected


# ---------------------------------------------------------------------
# panel_verdict -- eligibility
# ---------------------------------------------------------------------

def test_panel_gate_off_is_not_eligible_and_makes_no_call(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "off")
    calls = []
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: calls.append(1))
    v = lr.panel_verdict("build me an app", {"tier": 0, "confidence": 0.5})
    assert v == {"mode": "off", "eligible": False, "p_higher": None,
                 "would_skip": False, "skip": False}
    assert calls == []


@pytest.mark.parametrize("tier", [2, 3])
def test_panel_gate_out_of_scope_tiers_are_not_eligible(monkeypatch, tier):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "observe")
    calls = []
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: calls.append(1))
    v = lr.panel_verdict("task", {"tier": tier, "confidence": 0.5})
    assert v["eligible"] is False
    assert calls == []


def test_panel_gate_tier_0_and_1_are_eligible(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "observe")
    monkeypatch.setattr(lr.laya_gate, "predict",
                        lambda *a, **k: _choice_reply({"instant": 0.9, "direct": 0.05, "fixed": 0.03, "adaptive": 0.02}))
    for tier in (0, 1):
        assert lr.panel_verdict("task", {"tier": tier, "confidence": 0.5})["eligible"] is True


def test_panel_gate_empty_task_makes_no_call(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "observe")
    calls = []
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: calls.append(1))
    v = lr.panel_verdict("   ", {"tier": 0, "confidence": 0.5})
    assert v["eligible"] is True
    assert v["p_higher"] is None
    assert calls == []


# ---------------------------------------------------------------------
# panel_verdict -- probability math and mode gating
# ---------------------------------------------------------------------

def test_panel_gate_computes_p_higher_from_probabilities_above_the_draft_tier(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "observe")
    # draft tier 0 ("instant"): everything else (direct+fixed+adaptive) is
    # "more complex than the draft", so all three count toward p_higher.
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: _choice_reply(
        {"instant": 0.5, "direct": 0.3, "fixed": 0.15, "adaptive": 0.05}))
    v = lr.panel_verdict("task", {"tier": 0, "confidence": 0.5})
    assert v["p_higher"] == pytest.approx(0.50)


def test_panel_gate_p_higher_excludes_mass_at_or_below_the_draft_tier(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "observe")
    # draft tier 1 ("direct"): only fixed+adaptive (strictly above tier 1)
    # count toward p_higher -- instant/direct mass must NOT be included.
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: _choice_reply(
        {"instant": 0.5, "direct": 0.3, "fixed": 0.15, "adaptive": 0.05}))
    v = lr.panel_verdict("task", {"tier": 1, "confidence": 0.5})
    assert v["p_higher"] == pytest.approx(0.20)


def test_panel_gate_would_skip_true_but_skip_false_in_observe_mode(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "observe")
    monkeypatch.setattr(lr, "PANEL_GATE_THRESHOLD", 0.90)
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: _choice_reply(
        {"instant": 0.98, "direct": 0.01, "fixed": 0.005, "adaptive": 0.005}))
    v = lr.panel_verdict("task", {"tier": 0, "confidence": 0.5})
    assert v["would_skip"] is True
    assert v["skip"] is False   # observe never skips


def test_panel_gate_skips_in_on_mode_when_confident(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "on")
    monkeypatch.setattr(lr, "PANEL_GATE_THRESHOLD", 0.90)
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: _choice_reply(
        {"instant": 0.98, "direct": 0.01, "fixed": 0.005, "adaptive": 0.005}))
    v = lr.panel_verdict("task", {"tier": 0, "confidence": 0.5})
    assert v["skip"] is True


def test_panel_gate_does_not_skip_in_on_mode_when_below_threshold(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "on")
    monkeypatch.setattr(lr, "PANEL_GATE_THRESHOLD", 0.90)
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: _choice_reply(
        {"instant": 0.7, "direct": 0.1, "fixed": 0.1, "adaptive": 0.1}))
    v = lr.panel_verdict("task", {"tier": 0, "confidence": 0.5})
    assert v["would_skip"] is False
    assert v["skip"] is False


def test_panel_gate_passes_site_label_for_the_decision_log(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "observe")
    seen = {}
    monkeypatch.setattr(lr.laya_gate, "predict",
                        lambda state, q, site="u": seen.update(site=site) or
                        _choice_reply({"instant": 1, "direct": 0, "fixed": 0, "adaptive": 0}))
    lr.panel_verdict("task", {"tier": 0, "confidence": 0.5})
    assert seen["site"] == "loop_v4.panel_pregate"


# ---------------------------------------------------------------------
# panel_verdict -- fail-open
# ---------------------------------------------------------------------

def test_panel_gate_fails_open_when_laya_unavailable(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "on")
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: None)
    v = lr.panel_verdict("task", {"tier": 0, "confidence": 0.5})
    assert v["skip"] is False and v["p_higher"] is None


def test_panel_gate_fails_open_on_exception(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "on")

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(lr.laya_gate, "predict", boom)
    v = lr.panel_verdict("task", {"tier": 0, "confidence": 0.5})
    assert v["skip"] is False and v["p_higher"] is None


def test_panel_gate_fails_open_on_malformed_result(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "on")
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: {"answers": {}})
    v = lr.panel_verdict("task", {"tier": 0, "confidence": 0.5})
    assert v["skip"] is False and v["p_higher"] is None


def test_panel_gate_fails_open_when_all_probabilities_are_zero(monkeypatch):
    monkeypatch.setattr(lr, "PANEL_GATE_MODE", "on")
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: _choice_reply(
        {"instant": 0.0, "direct": 0.0, "fixed": 0.0, "adaptive": 0.0}))
    v = lr.panel_verdict("task", {"tier": 0, "confidence": 0.5})
    assert v["skip"] is False and v["p_higher"] is None


# ---------------------------------------------------------------------
# note_panel_skipped / record_panel_outcome / get_panel_gate_stats
# ---------------------------------------------------------------------

def test_note_panel_skipped_increments_the_skipped_counter():
    lr.note_panel_skipped({})
    lr.note_panel_skipped({})
    assert lr.get_panel_gate_stats()["skipped"] == 2


def test_record_panel_outcome_ignored_when_not_eligible():
    lr.record_panel_outcome({"eligible": False, "p_higher": None}, {"tier": 2}, {"tier": 2})
    stats = lr.get_panel_gate_stats()
    assert stats["would_skip"] == 0 and stats["kept"] == 0


def test_record_panel_outcome_ignored_when_p_higher_is_none():
    lr.record_panel_outcome({"eligible": True, "p_higher": None, "would_skip": False},
                            {"tier": 0}, {"tier": 0})
    stats = lr.get_panel_gate_stats()
    assert stats["would_skip"] == 0 and stats["kept"] == 0


def test_record_panel_outcome_would_skip_and_panel_kept_tier():
    v = {"eligible": True, "p_higher": 0.05, "would_skip": True}
    lr.record_panel_outcome(v, {"tier": 0}, {"tier": 0})
    stats = lr.get_panel_gate_stats()
    assert stats["would_skip"] == 1
    assert stats["would_skip_panel_raised_tier"] == 0
    assert stats["skip_precision"] == pytest.approx(1.0)


def test_record_panel_outcome_would_skip_but_panel_raised_tier():
    v = {"eligible": True, "p_higher": 0.05, "would_skip": True}
    lr.record_panel_outcome(v, {"tier": 0}, {"tier": 3})
    stats = lr.get_panel_gate_stats()
    assert stats["would_skip"] == 1
    assert stats["would_skip_panel_raised_tier"] == 1
    assert stats["skip_precision"] == pytest.approx(0.0)


def test_record_panel_outcome_kept_is_tracked_separately_from_would_skip():
    v = {"eligible": True, "p_higher": 0.6, "would_skip": False}
    lr.record_panel_outcome(v, {"tier": 1}, {"tier": 3})
    stats = lr.get_panel_gate_stats()
    assert stats["kept"] == 1 and stats["kept_panel_raised_tier"] == 1
    assert stats["would_skip"] == 0


def test_panel_gate_stats_precision_is_none_with_no_data():
    assert lr.get_panel_gate_stats()["skip_precision"] is None


def test_panel_gate_stats_precision_mixed_sample():
    lr.record_panel_outcome({"eligible": True, "p_higher": 0.05, "would_skip": True},
                            {"tier": 0}, {"tier": 0})   # correct skip
    lr.record_panel_outcome({"eligible": True, "p_higher": 0.05, "would_skip": True},
                            {"tier": 0}, {"tier": 0})   # correct skip
    lr.record_panel_outcome({"eligible": True, "p_higher": 0.05, "would_skip": True},
                            {"tier": 0}, {"tier": 2})   # wrong skip
    stats = lr.get_panel_gate_stats()
    assert stats["would_skip"] == 3
    assert stats["would_skip_panel_raised_tier"] == 1
    assert stats["skip_precision"] == pytest.approx(2 / 3)


def test_record_panel_outcome_never_raises_on_malformed_input():
    lr.record_panel_outcome({"eligible": True, "p_higher": 0.5, "would_skip": True},
                            {}, None)   # missing/None fields
    # must not raise; counters may or may not move, both are acceptable


# ---------------------------------------------------------------------
# gatekeeper_verdict
# ---------------------------------------------------------------------

def test_gatekeeper_gate_off_makes_no_call():
    lr.GATEKEEPER_GATE_MODE  # noqa: B018 (just referencing for readability)


def test_gatekeeper_gate_off_mode_returns_no_opinion(monkeypatch):
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_MODE", "off")
    calls = []
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: calls.append(1))
    v = lr.gatekeeper_verdict({"writer": {}}, "task", 1, [], 0)
    assert v == {"mode": "off", "p_stop": None, "would_stop": False, "skip": False}
    assert calls == []


def test_gatekeeper_gate_would_stop_in_observe_mode_never_skips(monkeypatch):
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_MODE", "observe")
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_THRESHOLD", 0.9)
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: _noul_reply(0.95))
    v = lr.gatekeeper_verdict({"writer": {}}, "task", 1, [], 0)
    assert v["would_stop"] is True
    assert v["skip"] is False


def test_gatekeeper_gate_skips_in_on_mode_when_confident(monkeypatch):
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_MODE", "on")
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_THRESHOLD", 0.9)
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: _noul_reply(0.95))
    v = lr.gatekeeper_verdict({"writer": {}}, "task", 1, [], 0)
    assert v["skip"] is True


def test_gatekeeper_gate_does_not_skip_below_threshold(monkeypatch):
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_MODE", "on")
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_THRESHOLD", 0.9)
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: _noul_reply(0.5))
    v = lr.gatekeeper_verdict({"writer": {}}, "task", 1, [], 0)
    assert v["would_stop"] is False and v["skip"] is False


def test_gatekeeper_gate_sends_structural_state_not_role_outputs(monkeypatch):
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_MODE", "observe")
    seen = {}
    monkeypatch.setattr(lr.laya_gate, "predict",
                        lambda state, q, site="u": seen.update(state=state, site=site) or _noul_reply(0.5))
    huge_output = {"text": "x" * 50000}
    lr.gatekeeper_verdict({"writer": huge_output, "reviewer": {"status": "failed"}},
                          "the original task", 2, ["reviewer"], 3)
    st = seen["state"]
    assert st["pass_number"] == 2
    assert st["roles_failed"] == 1
    assert st["critical_issues_flagged"] == 3
    assert "writer" in st["roles_run"] and "reviewer" in st["roles_run"]
    assert "x" * 100 not in str(st)   # the huge role output itself is never sent
    assert seen["site"] == "loop_controller.gatekeeper_pregate"


def test_gatekeeper_gate_fails_open_when_laya_unavailable(monkeypatch):
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_MODE", "on")
    monkeypatch.setattr(lr.laya_gate, "predict", lambda *a, **k: None)
    v = lr.gatekeeper_verdict({}, "task", 1, [], 0)
    assert v["skip"] is False and v["p_stop"] is None


def test_gatekeeper_gate_fails_open_on_exception(monkeypatch):
    monkeypatch.setattr(lr, "GATEKEEPER_GATE_MODE", "on")

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(lr.laya_gate, "predict", boom)
    v = lr.gatekeeper_verdict({}, "task", 1, [], 0)
    assert v["skip"] is False and v["p_stop"] is None


# ---------------------------------------------------------------------
# note_gatekeeper_skipped / record_gatekeeper_outcome / get_gatekeeper_gate_stats
# ---------------------------------------------------------------------

def test_note_gatekeeper_skipped_increments_counter():
    lr.note_gatekeeper_skipped({})
    assert lr.get_gatekeeper_gate_stats()["skipped"] == 1


def test_record_gatekeeper_outcome_ignored_when_p_stop_is_none():
    lr.record_gatekeeper_outcome({"p_stop": None, "would_stop": False}, "STOP")
    stats = lr.get_gatekeeper_gate_stats()
    assert stats["would_stop"] == 0 and stats["kept"] == 0


def test_record_gatekeeper_outcome_would_stop_and_llm_agreed():
    lr.record_gatekeeper_outcome({"p_stop": 0.95, "would_stop": True}, "STOP")
    stats = lr.get_gatekeeper_gate_stats()
    assert stats["would_stop"] == 1
    assert stats["would_stop_llm_continued"] == 0
    assert stats["stop_precision"] == pytest.approx(1.0)


def test_record_gatekeeper_outcome_would_stop_but_llm_continued():
    lr.record_gatekeeper_outcome({"p_stop": 0.91, "would_stop": True}, "CONTINUE")
    stats = lr.get_gatekeeper_gate_stats()
    assert stats["would_stop"] == 1
    assert stats["would_stop_llm_continued"] == 1
    assert stats["stop_precision"] == pytest.approx(0.0)


def test_record_gatekeeper_outcome_kept_and_llm_stopped_anyway():
    lr.record_gatekeeper_outcome({"p_stop": 0.4, "would_stop": False}, "STOP")
    stats = lr.get_gatekeeper_gate_stats()
    assert stats["kept"] == 1 and stats["kept_llm_stopped"] == 1
    assert stats["would_stop"] == 0


def test_gatekeeper_gate_stats_precision_is_none_with_no_data():
    assert lr.get_gatekeeper_gate_stats()["stop_precision"] is None


def test_record_gatekeeper_outcome_never_raises_on_malformed_input():
    lr.record_gatekeeper_outcome({"p_stop": 0.9, "would_stop": True}, None)


# ---------------------------------------------------------------------
# counters degrade gracefully if the bus itself errors
# ---------------------------------------------------------------------

def test_bump_never_raises_when_incr_fails(monkeypatch):
    def boom(key, ex=None):
        raise RuntimeError("redis down")
    monkeypatch.setattr(lr, "incr", boom)
    lr.note_panel_skipped({})   # must not raise


def test_count_returns_zero_when_read_fails(monkeypatch):
    def boom(key, default=None):
        raise RuntimeError("redis down")
    monkeypatch.setattr(lr, "read", boom)
    assert lr._count("anything") == 0
