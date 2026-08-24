"""
tests/unit/test_eo_notify.py — Patch 7e-S6.

eo/notify.py had zero test coverage before this. notify() is the one
boundary every §9a call site fires through, and its own module
docstring pins two hard contracts worth regression-testing directly:

  - session_id=None is a no-op, not an error (callers that sometimes
    run without a session shouldn't have to guard for that themselves).
  - An unrecognized kind is logged and skipped, never raised -- this
    used to raise ValueError and was deliberately changed so a caller
    bug in the notification side-channel can never take down the real
    agent work riding alongside it.

_deliver() (§9b + Phase 4 step 4.2) pushes through BOTH transports
(eo.ws_registry.push() and relay.emitter.emit_event()), each
independently guarded -- one transport raising must never block or
skip the other. These tests pin that isolation directly, since it's
exactly the kind of "obviously true reading the code" contract that
silently breaks the first time someone "simplifies" the two try/except
blocks into one.

Isolation: notify.py does `from eo.ws_registry import push as _ws_push`
and `from relay.emitter import emit_event as _emit_pusher_event` (bound
names in its own namespace), so tests patch `notify._ws_push` and
`notify._emit_pusher_event` directly rather than patching the origin
modules, same gotcha as every other module in this tree that imports a
callable by name instead of by module reference.
"""
from unittest.mock import MagicMock

import pytest

from eo import notify


@pytest.fixture(autouse=True)
def _stub_transports(monkeypatch):
    """Every test gets both transports stubbed out to plain MagicMocks
    by default -- individual tests override one or the other (e.g. to
    raise) as needed. Prevents any test accidentally hitting a real
    ws_registry connection registry or a real Pusher client."""
    monkeypatch.setattr(notify, "_ws_push", MagicMock())
    monkeypatch.setattr(notify, "_emit_pusher_event", MagicMock())


# A real member of VALID_KINDS (derived from relay/emitter.py's own
# NOTIFY_KINDS) -- not hand-typed, so this test file can't drift out of
# sync with that set the same way the old two-independently-maintained
# string sets used to.
_A_VALID_KIND = next(iter(notify.VALID_KINDS))


# ---------------------------------------------------------------------
# notify() — validation boundary
# ---------------------------------------------------------------------

def test_session_id_none_is_a_noop_and_returns_none():
    result = notify.notify(None, _A_VALID_KIND, {"x": 1})
    assert result is None
    notify._ws_push.assert_not_called()
    notify._emit_pusher_event.assert_not_called()


def test_unknown_kind_is_skipped_not_raised():
    result = notify.notify("sess-1", "definitely_not_a_real_kind", {})
    assert result is None
    notify._ws_push.assert_not_called()
    notify._emit_pusher_event.assert_not_called()


def test_valid_kind_returns_the_event_dict_that_was_pushed():
    result = notify.notify("sess-1", _A_VALID_KIND, {"foo": "bar"})
    assert result["kind"] == _A_VALID_KIND
    assert result["session_id"] == "sess-1"
    assert result["payload"] == {"foo": "bar"}
    assert "timestamp" in result


def test_payload_defaults_to_empty_dict_when_omitted():
    result = notify.notify("sess-1", _A_VALID_KIND)
    assert result["payload"] == {}


def test_valid_kind_actually_delivers_through_both_transports():
    notify.notify("sess-1", _A_VALID_KIND, {"a": 1})
    notify._ws_push.assert_called_once()
    notify._emit_pusher_event.assert_called_once()


def test_every_notify_kinds_member_is_independently_valid():
    """Regression guard for the exact bug class this module's docstring
    describes (a kind silently missing from one side's set) -- every
    member NOTIFY_KINDS actually exports must be accepted here."""
    for kind in notify.VALID_KINDS:
        result = notify.notify("sess-1", kind, {})
        assert result is not None, f"{kind!r} should be a valid notify() kind"


# ---------------------------------------------------------------------
# _deliver() — dual-transport fan-out, each independently guarded
# ---------------------------------------------------------------------

def test_ws_push_receives_the_full_event_shape():
    notify.notify("sess-1", _A_VALID_KIND, {"a": 1})
    event = notify._ws_push.call_args[0][0]
    assert event["kind"] == _A_VALID_KIND
    assert event["session_id"] == "sess-1"
    assert event["payload"] == {"a": 1}


def test_pusher_mirror_receives_kind_session_id_and_payload():
    notify.notify("sess-1", _A_VALID_KIND, {"a": 1})
    _, kwargs = notify._emit_pusher_event.call_args
    args = notify._emit_pusher_event.call_args[0]
    # emit_event(event["kind"], session_id=event["session_id"], payload=event["payload"])
    assert args[0] == _A_VALID_KIND
    assert kwargs["session_id"] == "sess-1"
    assert kwargs["payload"] == {"a": 1}


def test_ws_push_failure_does_not_prevent_pusher_mirror(monkeypatch):
    monkeypatch.setattr(notify, "_ws_push", MagicMock(side_effect=RuntimeError("boom")))
    notify.notify("sess-1", _A_VALID_KIND, {})  # must not raise
    notify._emit_pusher_event.assert_called_once()


def test_pusher_mirror_failure_does_not_prevent_ws_push(monkeypatch):
    monkeypatch.setattr(notify, "_emit_pusher_event", MagicMock(side_effect=RuntimeError("boom")))
    notify.notify("sess-1", _A_VALID_KIND, {})  # must not raise
    notify._ws_push.assert_called_once()


def test_both_transports_failing_does_not_raise(monkeypatch):
    monkeypatch.setattr(notify, "_ws_push", MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(notify, "_emit_pusher_event", MagicMock(side_effect=RuntimeError("boom")))
    result = notify.notify("sess-1", _A_VALID_KIND, {})  # must not raise
    assert result["kind"] == _A_VALID_KIND
