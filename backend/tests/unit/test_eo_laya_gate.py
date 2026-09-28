"""
tests/unit/test_eo_laya_gate.py

eo/laya_gate.py (Laya migration, 2026-09-27): shared loader, load-failure
cooldown, input-budget helpers (chunk/clip), and the opt-in decision log.

No real model is ever loaded: a fake `laya` module is injected into
sys.modules, and module-level state (_agent, _last_load_failure, decision
log path) is reset per test.
"""
import json
import sys
import types

import pytest

from eo import laya_gate as lg


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(lg, "_agent", None)
    monkeypatch.setattr(lg, "_last_load_failure", 0.0)
    monkeypatch.setattr(lg, "_DECISION_LOG_PATH", "")
    yield


def _install_fake_laya(monkeypatch, load):
    fake = types.ModuleType("laya")
    fake.load = load
    monkeypatch.setitem(sys.modules, "laya", fake)


class _FakeAgent:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def predict(self, state, questions):
        self.calls.append((state, questions))
        if self.exc:
            raise self.exc
        return self.result


# ---------------------------------------------------------------------
# loading / cooldown
# ---------------------------------------------------------------------

def test_agent_is_loaded_once_and_reused(monkeypatch):
    loads = []
    agent = _FakeAgent()
    _install_fake_laya(monkeypatch, lambda name: loads.append(name) or agent)

    assert lg.get_agent() is agent
    assert lg.get_agent() is agent
    assert loads == [lg.LAYA_MODEL]


def test_failed_load_is_remembered_and_not_retried_within_cooldown(monkeypatch):
    loads = []

    def failing_load(name):
        loads.append(name)
        raise OSError("no network")
    _install_fake_laya(monkeypatch, failing_load)
    monkeypatch.setattr(lg, "_LOAD_RETRY_SECONDS", 300.0)

    assert lg.get_agent() is None
    assert lg.get_agent() is None
    assert lg.predict({"t": "x"}, {}) is None
    assert len(loads) == 1   # one attempt, not one per call


def test_failed_load_is_retried_after_cooldown(monkeypatch):
    attempts = []
    agent = _FakeAgent()

    def flaky_load(name):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no network")
        return agent
    _install_fake_laya(monkeypatch, flaky_load)
    monkeypatch.setattr(lg, "_LOAD_RETRY_SECONDS", 0.0)

    assert lg.get_agent() is None
    assert lg.get_agent() is agent   # cooldown 0 -> retry succeeds
    assert len(attempts) == 2


def test_missing_laya_package_fails_open(monkeypatch):
    monkeypatch.setitem(sys.modules, "laya", None)   # import raises ImportError
    assert lg.get_agent() is None
    assert lg.preload() is False


def test_preload_reports_ready(monkeypatch):
    _install_fake_laya(monkeypatch, lambda name: _FakeAgent())
    assert lg.preload() is True


# ---------------------------------------------------------------------
# predict
# ---------------------------------------------------------------------

def test_predict_returns_agent_result(monkeypatch):
    agent = _FakeAgent(result={"answers": {"q": {"choice": "a"}}})
    _install_fake_laya(monkeypatch, lambda name: agent)
    out = lg.predict({"text": "hi"}, {"q": {}}, site="t")
    assert out == {"answers": {"q": {"choice": "a"}}}
    assert agent.calls == [({"text": "hi"}, {"q": {}})]


def test_predict_swallows_inference_errors(monkeypatch):
    _install_fake_laya(monkeypatch, lambda name: _FakeAgent(exc=RuntimeError("boom")))
    assert lg.predict({"text": "hi"}, {}) is None


# ---------------------------------------------------------------------
# chunk / clip
# ---------------------------------------------------------------------

def test_chunk_splits_and_caps():
    assert lg.chunk("abcdefghij", 4, 10) == ["abcd", "efgh", "ij"]
    assert lg.chunk("a" * 100, 10, 3) == ["a" * 10] * 3


def test_chunk_always_returns_at_least_one_piece():
    assert lg.chunk("", 10, 3) == [""]
    assert lg.chunk(None, 10, 3) == [""]
    assert lg.chunk("short", 10, 3) == ["short"]


def test_clip_head_tail_both():
    text = "0123456789" * 10   # 100 chars
    assert lg.clip("short", 50) == "short"
    assert lg.clip(text, 20, "head") == text[:20]
    assert lg.clip(text, 20, "tail") == text[-20:]
    both = lg.clip(text, 20, "both")
    assert len(both) == 20
    assert both.startswith(text[:10]) and both.endswith(text[-5:]) and " ... " in both


def test_clip_none_is_empty():
    assert lg.clip(None, 10) == ""


# ---------------------------------------------------------------------
# decision log
# ---------------------------------------------------------------------

def test_decision_log_is_off_by_default(monkeypatch, tmp_path):
    _install_fake_laya(monkeypatch, lambda name: _FakeAgent(result={"answers": {}}))
    lg.predict({"text": "hi"}, {}, site="t")
    assert list(tmp_path.iterdir()) == []


def test_decision_log_writes_one_json_line_per_prediction(monkeypatch, tmp_path):
    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(lg, "_DECISION_LOG_PATH", str(path))
    _install_fake_laya(monkeypatch, lambda name: _FakeAgent(
        result={"answers": {"q": {"choice": "a", "confidence": 0.9}}}))

    lg.predict({"text": "hello"}, {"q": {}}, site="my.site")
    lg.predict({"text": "again"}, {"q": {}}, site="my.site")

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["site"] == "my.site"
    assert rec["state"] == {"text": "hello"}
    assert rec["answers"] == {"q": {"choice": "a", "confidence": 0.9}}


def test_decision_log_clips_long_fields(monkeypatch, tmp_path):
    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(lg, "_DECISION_LOG_PATH", str(path))
    _install_fake_laya(monkeypatch, lambda name: _FakeAgent(result={"answers": {}}))
    lg.predict({"text": "x" * 10000}, {}, site="t")
    rec = json.loads(path.read_text().splitlines()[0])
    assert len(rec["state"]["text"]) == lg._LOG_FIELD_CHARS


def test_decision_log_stops_at_size_cap(monkeypatch, tmp_path):
    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(lg, "_DECISION_LOG_PATH", str(path))
    monkeypatch.setattr(lg, "_DECISION_LOG_MAX_BYTES", 1)
    _install_fake_laya(monkeypatch, lambda name: _FakeAgent(result={"answers": {}}))
    lg.predict({"text": "one"}, {}, site="t")   # file doesn't exist yet -> written
    lg.predict({"text": "two"}, {}, site="t")   # now over the cap -> skipped
    assert len(path.read_text().strip().splitlines()) == 1


def test_decision_log_failure_never_breaks_predict(monkeypatch, tmp_path):
    monkeypatch.setattr(lg, "_DECISION_LOG_PATH", str(tmp_path / "no" / "such" / "dir" / "x.jsonl"))
    _install_fake_laya(monkeypatch, lambda name: _FakeAgent(result={"answers": {"q": 1}}))
    assert lg.predict({"text": "hi"}, {}, site="t") == {"answers": {"q": 1}}


# ---------------------------------------------------------------------
# serialized inference
# ---------------------------------------------------------------------

def test_concurrent_predicts_never_overlap_inside_the_agent(monkeypatch):
    """laya's thread-safety is unverified, so predict() must not let two
    threads inside Agent.predict at once."""
    import threading
    import time as _t

    state = {"active": 0, "max_active": 0}
    guard = threading.Lock()

    class SlowAgent:
        def predict(self, s, q):
            with guard:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            _t.sleep(0.02)
            with guard:
                state["active"] -= 1
            return {"answers": {}}

    _install_fake_laya(monkeypatch, lambda name: SlowAgent())
    threads = [threading.Thread(target=lg.predict, args=({"t": i}, {})) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert state["max_active"] == 1
