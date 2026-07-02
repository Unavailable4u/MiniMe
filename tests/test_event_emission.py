"""
tests/test_event_emission.py — Stage 6, step 1 of the roadmap (Part 10):
proof-of-concept coverage for relay/emitter.py, wired so far into the
Inspector only (eo/inspector.py).

Mocks Pusher entirely -- no real credentials, no real network call. This
is the "mock Pusher, assert the right event types fire in the right
order" test from Part 11, scoped down to what's actually wired today
(just the Inspector). Extend the FakePusher-based assertions here as
more agents get wrapped in later steps.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay.emitter as emitter


class _FakePusher:
    """Records every trigger() call instead of hitting the network."""
    def __init__(self, **kwargs):
        self.calls = []

    def trigger(self, channel, event_name, data):
        self.calls.append((channel, event_name, data))


def _configure_fake_env(monkeypatch):
    monkeypatch.setenv("PUSHER_APP_ID", "fake_app_id")
    monkeypatch.setenv("PUSHER_KEY", "fake_key")
    monkeypatch.setenv("PUSHER_SECRET", "fake_secret")
    monkeypatch.setenv("PUSHER_CLUSTER", "fake_cluster")


# ---------------------------------------------------------------------------
# 1. emit_event() itself
# ---------------------------------------------------------------------------

def test_no_session_id_is_a_silent_noop(monkeypatch):
    """The default-safe path: no session_id means no channel to publish
    on, so nothing should even attempt a client lookup."""
    _configure_fake_env(monkeypatch)
    result = emitter.emit_event("agent_start", session_id=None, agent="inspector")
    assert result is False


def test_unconfigured_pusher_is_a_silent_noop(monkeypatch):
    """No PUSHER_* env vars set at all -- must not raise, must return False."""
    for var in ("PUSHER_APP_ID", "PUSHER_KEY", "PUSHER_SECRET", "PUSHER_CLUSTER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(emitter, "_pusher_client", None)
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    result = emitter.emit_event("agent_start", session_id="sess_abc", agent="inspector")
    assert result is False


def test_invalid_event_type_raises(monkeypatch):
    _configure_fake_env(monkeypatch)
    try:
        emitter.emit_event("not_a_real_type", session_id="sess_abc")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_event_fires_with_correct_shape(monkeypatch):
    _configure_fake_env(monkeypatch)
    fake_client = _FakePusher()
    monkeypatch.setattr(emitter, "_pusher_client", fake_client)
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    result = emitter.emit_event(
        "routing_decision", session_id="sess_abc123", agent="inspector",
        tier=1, payload={"confidence": 0.9},
    )

    assert result is True
    assert len(fake_client.calls) == 1
    channel, event_name, data = fake_client.calls[0]
    assert channel == "session-sess_abc123"
    assert event_name == "routing_decision"
    assert data["type"] == "routing_decision"
    assert data["session_id"] == "sess_abc123"
    assert data["agent"] == "inspector"
    assert data["tier"] == 1
    assert data["payload"] == {"confidence": 0.9}
    assert "timestamp" in data


def test_channel_name_sanitizes_unsafe_characters(monkeypatch):
    _configure_fake_env(monkeypatch)
    fake_client = _FakePusher()
    monkeypatch.setattr(emitter, "_pusher_client", fake_client)
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    emitter.emit_event("agent_start", session_id="sess with spaces!", agent="x")
    channel, _, _ = fake_client.calls[0]
    assert " " not in channel and "!" not in channel


def test_failed_trigger_is_caught_not_raised(monkeypatch):
    _configure_fake_env(monkeypatch)

    class _BrokenPusher:
        def trigger(self, *a, **kw):
            raise RuntimeError("network down")

    monkeypatch.setattr(emitter, "_pusher_client", _BrokenPusher())
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    result = emitter.emit_event("agent_start", session_id="sess_abc", agent="inspector")
    assert result is False  # must not raise


# ---------------------------------------------------------------------------
# 2. Inspector wiring (proof of concept -- Stage 6 step 1)
# ---------------------------------------------------------------------------

def test_inspector_emits_start_and_done_and_routing_decision(monkeypatch):
    _configure_fake_env(monkeypatch)
    fake_client = _FakePusher()
    monkeypatch.setattr(emitter, "_pusher_client", fake_client)
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    import eo.inspector as inspector

    class _FakeResponse:
        GOOD_JSON = (
            '{"tier": 0, "directed_task_type": null, "confidence": 0.95, '
            '"suggested_agents": ["responder"], "reasoning": "trivial question"}'
        )

        class choices:
            pass

    class _FakeChoice:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})

    class _FakeMsgResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    class _FakeWorkingClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return _FakeMsgResponse(_FakeResponse.GOOD_JSON)

    import utils.llm_client as llm_client
    monkeypatch.setattr(llm_client, "_get_groq",
                         lambda key_env, timeout=None: _FakeWorkingClient())

    result = inspector.classify("what's 2+2", session_id="sess_xyz")

    assert result["tier"] == 0
    event_types = [call[1] for call in fake_client.calls]
    assert event_types == ["agent_start", "routing_decision", "agent_done"]
    # routing_decision payload should be exactly the classification result
    routing_call = fake_client.calls[1][2]
    assert routing_call["payload"]["tier"] == 0
    assert routing_call["tier"] == 0


def test_inspector_without_session_id_emits_nothing(monkeypatch):
    """Backward-compat guarantee: no session_id -> zero relay traffic,
    same as before Stage 6 existed."""
    _configure_fake_env(monkeypatch)
    fake_client = _FakePusher()
    monkeypatch.setattr(emitter, "_pusher_client", fake_client)
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    import eo.inspector as inspector
    import utils.llm_client as llm_client

    class _FakeChoice:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})

    class _FakeMsgResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    GOOD_JSON = (
        '{"tier": 0, "directed_task_type": null, "confidence": 0.95, '
        '"suggested_agents": ["responder"], "reasoning": "trivial question"}'
    )

    class _FakeWorkingClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return _FakeMsgResponse(GOOD_JSON)

    monkeypatch.setattr(llm_client, "_get_groq",
                         lambda key_env, timeout=None: _FakeWorkingClient())

    inspector.classify("what's 2+2")  # no session_id
    assert fake_client.calls == []


if __name__ == "__main__":
    print("Run via pytest -- this file uses monkeypatch fixtures throughout:")
    print("  python -m pytest tests/test_event_emission.py -v")