"""
tests/integration/test_event_emission.py — Stage 6, step 1 of the
roadmap (Part 10): proof-of-concept coverage for relay/emitter.py,
wired so far into the Inspector (eo/inspector.py) and, since Part 2
§2.4, eo/executor.py's resume_graph() (see test_resume_graph.py for the
pause/resume-specific coverage, including the "execution_resumed" event
this file's own baseline audit found missing from VALID_EVENT_TYPES).

Mocks Pusher entirely -- no real credentials, no real network call.

Moved from tests/test_event_emission.py (B1 audit) and updated for
Migration Part 12/15's tier -> path rename: eo/inspector.py's classify()
emits and returns "path" now, not "tier" -- both the fake LLM response
JSON and the assertions below were written against the old schema and
needed updating, or classify()'s own _validate() rejects the response
outright (KeyError on parsed["path"] via the "path not in VALID_PATHS"
check) before any event ever fires.
"""
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


def test_unknown_event_type_is_skipped_not_raised(monkeypatch, capsys):
    """PATCH-C: emit_event() used to raise ValueError on an unrecognized
    event_type, which could take down an entire task run mid-execution
    (see this module's docstring / relay/emitter.py's own design rule).
    It now degrades the same way every other failure path here does:
    log and return False, never raise."""
    _configure_fake_env(monkeypatch)
    result = emitter.emit_event("not_a_real_type", session_id="sess_abc")
    assert result is False
    # still needs to be observable somewhere, not silently swallowed
    assert "not_a_real_type" in capsys.readouterr().out


def test_unknown_event_type_is_skipped_for_user_events_too(monkeypatch, capsys):
    """Same PATCH-C coverage for emit_user_event()'s independent
    validation branch."""
    _configure_fake_env(monkeypatch)
    result = emitter.emit_user_event("not_a_real_type", user_id="user_abc")
    assert result is False
    assert "not_a_real_type" in capsys.readouterr().out


def test_previously_missing_event_types_are_now_valid():
    """Regression coverage for the exact incident that prompted
    PATCH-A/B/C: deploy_config_writer.py fired 'deploy_config_proposed'
    (and eight sibling literals across other agent files) that were
    never added to the old hand-maintained VALID_EVENT_TYPES set, so
    every one of those calls raised ValueError the first time that
    code path actually ran in production. All nine are now real
    EventType members."""
    previously_missing = {
        "architecture_diagram", "plan_handoff", "schema_diagram",
        "device_spec", "deploy_config_proposed", "deploy_config_written",
        "deploy_confirmed", "deploy_declined", "uptimerobot_registered",
        "uptimerobot_registration_failed",
    }
    assert previously_missing <= emitter.VALID_EVENT_TYPES


def test_execution_resumed_is_a_valid_event_type():
    # Regression coverage for the bug this rewrite found (see module
    # docstring / test_resume_graph.py): eo/executor.py's resume_graph()
    # has always fired this event type, but it was missing from
    # VALID_EVENT_TYPES, so every real resume call raised ValueError
    # right after applying the human's decision.
    assert "execution_resumed" in emitter.VALID_EVENT_TYPES


def test_event_type_enum_and_valid_event_types_never_drift():
    """PATCH-A: VALID_EVENT_TYPES is now *derived* from the EventType
    enum, not hand-copied, so this can never go stale the way the old
    set did -- but assert the derivation itself stays wired up."""
    assert emitter.VALID_EVENT_TYPES == {e.value for e in emitter.EventType}


def test_emit_event_accepts_enum_member_directly(monkeypatch):
    """Call sites are expected to pass EventType.X (see agents/* call
    sites) rather than a raw string going forward -- confirm that path
    still produces a plain-string event `type` on the wire, not an
    Enum instance leaking into the payload sent to Pusher."""
    _configure_fake_env(monkeypatch)
    fake_client = _FakePusher()
    monkeypatch.setattr(emitter, "_pusher_client", fake_client)
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    result = emitter.emit_event(emitter.EventType.DEPLOY_CONFIG_PROPOSED, session_id="sess_abc")
    assert result is True
    _, event_name, data = fake_client.calls[0]
    assert event_name == "deploy_config_proposed"
    assert data["type"] == "deploy_config_proposed"
    assert isinstance(data["type"], str) and not isinstance(data["type"], emitter.EventType)


# ---------------------------------------------------------------------------
# 1b. eo/notify.py's notify() -- shares the same PATCH-B/C validation
#     posture as emit_event() above, but had no dedicated test coverage
#     of its own before this patch.
# ---------------------------------------------------------------------------

def test_notify_unknown_kind_is_skipped_not_raised(caplog):
    """PATCH-C: notify() used to raise ValueError on an unrecognized
    kind on the theory that it's 'a caller bug, not a runtime condition
    to degrade past' -- but that contradicts relay/emitter.py's own
    stated design rule for this whole subsystem. It now logs a warning
    and returns None, same posture as emit_event()."""
    import eo.notify as notify
    result = notify.notify("sess_abc", "not_a_real_kind")
    assert result is None
    assert any("not_a_real_kind" in rec.message for rec in caplog.records)


def test_notify_valid_kinds_derive_from_emitter_notify_kinds():
    """PATCH-B: notify.VALID_KINDS used to be an independently
    hand-typed set that was only 'meant to' mirror
    relay/emitter.py:VALID_EVENT_TYPES by hand -- exactly the drift
    that let 'notification' silently fail for a period after Part 8.4
    landed. It's now derived from relay.emitter.NOTIFY_KINDS instead."""
    import eo.notify as notify
    assert notify.VALID_KINDS == {k.value for k in emitter.NOTIFY_KINDS}
    # sanity: a real EventType member that is NOT notify()-eligible
    # (e.g. a plain agent-lifecycle event) must stay excluded here --
    # NOTIFY_KINDS is a deliberate curated subset, not "everything".
    assert "agent_start" not in notify.VALID_KINDS


def test_event_fires_with_correct_shape(monkeypatch):
    _configure_fake_env(monkeypatch)
    fake_client = _FakePusher()
    monkeypatch.setattr(emitter, "_pusher_client", fake_client)
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    result = emitter.emit_event(
        "routing_decision", session_id="sess_abc123", agent="inspector",
        path="direct", payload={"confidence": 0.9},
    )

    assert result is True
    assert len(fake_client.calls) == 1
    channel, event_name, data = fake_client.calls[0]
    assert channel == "session-sess_abc123"
    assert event_name == "routing_decision"
    assert data["type"] == "routing_decision"
    assert data["session_id"] == "sess_abc123"
    assert data["agent"] == "inspector"
    assert data["path"] == "direct"
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
#
# These two tests mock eo.inspector.generate_text directly rather than
# reaching down into utils/llm_client.py's provider-client layer -- what's
# under test here is event wiring around classify(), not the Groq/Gemini
# fallback chain itself (that's test_eo_inspector.py's job). Mocking at
# the generate_text boundary keeps this file decoupled from CHAIN's exact
# provider/model/key_env contents, which have already changed once
# (GitHub Models retired, Groq model bumped) since this suite was
# written.

GOOD_JSON = (
    '{"path": "instant", "directed_task_type": null, "confidence": 0.95, '
    '"suggested_agents": ["responder"], "reasoning": "trivial question"}'
)


def test_inspector_emits_start_and_routing_decision_and_done(monkeypatch):
    _configure_fake_env(monkeypatch)
    fake_client = _FakePusher()
    monkeypatch.setattr(emitter, "_pusher_client", fake_client)
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    import eo.inspector as inspector
    monkeypatch.setattr(inspector, "generate_text", lambda **kwargs: GOOD_JSON)

    result = inspector.classify("what's 2+2", session_id="sess_xyz")

    assert result["path"] == "instant"
    event_types = [call[1] for call in fake_client.calls]
    assert event_types == ["agent_start", "routing_decision", "agent_done"]
    # routing_decision payload should be exactly the classification result
    routing_call = fake_client.calls[1][2]
    assert routing_call["payload"]["path"] == "instant"
    assert routing_call["path"] == "instant"


def test_inspector_without_session_id_emits_nothing(monkeypatch):
    """Backward-compat guarantee: no session_id -> zero relay traffic,
    same as before Stage 6 existed."""
    _configure_fake_env(monkeypatch)
    fake_client = _FakePusher()
    monkeypatch.setattr(emitter, "_pusher_client", fake_client)
    monkeypatch.setattr(emitter, "_pusher_unavailable", False)

    import eo.inspector as inspector
    monkeypatch.setattr(inspector, "generate_text", lambda **kwargs: GOOD_JSON)

    inspector.classify("what's 2+2")  # no session_id
    assert fake_client.calls == []
