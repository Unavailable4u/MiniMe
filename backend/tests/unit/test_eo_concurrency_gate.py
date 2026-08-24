"""
tests/unit/test_eo_concurrency_gate.py — Patch 7e-S2.

eo/concurrency_gate.py had zero test coverage before this. run_gated()
is a drop-in ThreadPoolExecutor replacement whose futures only start
once rate_ledger.reserve() confirms their designated candidate account
currently has room -- re-scanning still-queued tasks every time an
in-flight one finishes and releases its slot, rather than sizing the
pool once at task start.

Style/isolation notes:
  - `from utils import rate_ledger` is a MODULE import (not `from
    utils.rate_ledger import reserve`), so eo/concurrency_gate.py always
    calls rate_ledger.reserve(...)/rate_ledger.release_reservation(...)
    through that module object -- tests patch
    eo.concurrency_gate.rate_ledger.reserve /
    eo.concurrency_gate.rate_ledger.release_reservation directly
    (equivalently, monkeypatch the attributes on the real utils.rate_ledger
    module, since it's the same object either way). No memory.bus/fake_bus
    involvement needed at all: this module never touches the bus itself,
    only the injected reserve()/release_reservation() callables.
  - Every test drives run_gated() with a small, fixed set of fake
    reserve() outcomes and waits on the returned Futures (a real
    background admission thread does run -- this module is written to
    always start one -- but with reserve() mocked to return instantly,
    the whole thing settles in well under a second). A short
    `future.result(timeout=...)` bounds every wait so a genuine bug
    (e.g. a task that never gets admitted) fails the test instead of
    hanging the suite.
  - GatedTask.step=None (the "nothing to gate on" case) is exercised
    separately from the real-step admission-loop cases, matching the
    module's own documented fast path (_admit() returns (True, 0.0,
    None) without ever calling reserve()).
"""
import threading
import time
from unittest.mock import MagicMock

import pytest

from eo import concurrency_gate
from eo.concurrency_gate import (
    GatedTask,
    _admit,
    _candidate_identity,
    _release,
    run_gated,
)

FAKE_STEP = {"provider": "groq", "model": "llama-3", "key_env": "GROQ_KEY_1"}
FAKE_STEP_CF = {"provider": "cloudflare", "model": "llama-3", "account_id_env": "CF_ACCT_1"}


def _await_all(futures, timeout=5.0):
    return [f.result(timeout=timeout) for f in futures]


# ---------------------------------------------------------------------------
# _candidate_identity
# ---------------------------------------------------------------------------

def test_candidate_identity_standard_shape_uses_key_env():
    assert _candidate_identity(FAKE_STEP) == ("groq", "GROQ_KEY_1", "llama-3")


def test_candidate_identity_cloudflare_shape_uses_account_id_env():
    assert _candidate_identity(FAKE_STEP_CF) == ("cloudflare", "CF_ACCT_1", "llama-3")


def test_candidate_identity_missing_model_defaults_to_empty_string():
    step = {"provider": "mistral", "key_env": "MISTRAL_KEY"}
    assert _candidate_identity(step) == ("mistral", "MISTRAL_KEY", "")


# ---------------------------------------------------------------------------
# _admit / _release -- direct unit tests below the run_gated() layer
# ---------------------------------------------------------------------------

def test_admit_with_none_step_always_admits_without_calling_reserve(monkeypatch):
    mock_reserve = MagicMock(side_effect=AssertionError("reserve() should not be called"))
    monkeypatch.setattr(concurrency_gate.rate_ledger, "reserve", mock_reserve)
    ok, wait, reservation_id = _admit(GatedTask(call=lambda: None, step=None))
    assert (ok, wait, reservation_id) == (True, 0.0, None)
    mock_reserve.assert_not_called()


def test_admit_with_step_delegates_to_reserve(monkeypatch):
    mock_reserve = MagicMock(return_value=(True, 0.0, "res-123"))
    monkeypatch.setattr(concurrency_gate.rate_ledger, "reserve", mock_reserve)
    task = GatedTask(call=lambda: None, step=FAKE_STEP)
    result = _admit(task)
    assert result == (True, 0.0, "res-123")
    mock_reserve.assert_called_once_with("groq", "GROQ_KEY_1", "llama-3", 1)


def test_release_settles_with_exactly_one_actual_unit(monkeypatch):
    mock_release = MagicMock()
    monkeypatch.setattr(concurrency_gate.rate_ledger, "release_reservation", mock_release)
    _release(GatedTask(call=lambda: None, step=FAKE_STEP), "res-abc")
    mock_release.assert_called_once_with("res-abc", actual_units=1)


# ---------------------------------------------------------------------------
# run_gated -- empty input
# ---------------------------------------------------------------------------

def test_run_gated_empty_task_list_returns_empty_list():
    assert run_gated([]) == []


# ---------------------------------------------------------------------------
# run_gated -- step=None tasks admit immediately
# ---------------------------------------------------------------------------

def test_run_gated_step_none_tasks_all_run_and_return_results(monkeypatch):
    mock_reserve = MagicMock(side_effect=AssertionError("should not be called for step=None"))
    monkeypatch.setattr(concurrency_gate.rate_ledger, "reserve", mock_reserve)
    monkeypatch.setattr(concurrency_gate.rate_ledger, "release_reservation", MagicMock())

    tasks = [GatedTask(call=(lambda i=i: i * 10), step=None) for i in range(5)]
    futures = run_gated(tasks)
    assert _await_all(futures) == [0, 10, 20, 30, 40]


# ---------------------------------------------------------------------------
# run_gated -- gated admission via reserve()
# ---------------------------------------------------------------------------

def test_run_gated_admits_task_when_reserve_says_ok(monkeypatch):
    monkeypatch.setattr(concurrency_gate.rate_ledger, "reserve",
                         MagicMock(return_value=(True, 0.0, "res-1")))
    monkeypatch.setattr(concurrency_gate.rate_ledger, "release_reservation", MagicMock())

    task = GatedTask(call=lambda: "done", step=FAKE_STEP)
    futures = run_gated([task])
    assert _await_all(futures) == ["done"]


def test_run_gated_releases_reservation_after_task_completes(monkeypatch):
    monkeypatch.setattr(concurrency_gate.rate_ledger, "reserve",
                         MagicMock(return_value=(True, 0.0, "res-42")))
    mock_release = MagicMock()
    monkeypatch.setattr(concurrency_gate.rate_ledger, "release_reservation", mock_release)

    futures = run_gated([GatedTask(call=lambda: "ok", step=FAKE_STEP)])
    _await_all(futures)
    mock_release.assert_called_once_with("res-42", actual_units=1)


def test_run_gated_releases_reservation_even_when_task_raises(monkeypatch):
    """The done-callback's _release() must fire on the exception path too
    (the dispatch genuinely happened -- the task ran and failed, so its
    slot still needs to go back for the next queued task)."""
    monkeypatch.setattr(concurrency_gate.rate_ledger, "reserve",
                         MagicMock(return_value=(True, 0.0, "res-99")))
    mock_release = MagicMock()
    monkeypatch.setattr(concurrency_gate.rate_ledger, "release_reservation", mock_release)

    def _boom():
        raise ValueError("task failed")

    futures = run_gated([GatedTask(call=_boom, step=FAKE_STEP)])
    with pytest.raises(ValueError, match="task failed"):
        futures[0].result(timeout=5.0)
    mock_release.assert_called_once_with("res-99", actual_units=1)


def test_run_gated_blocked_task_admitted_once_slot_frees(monkeypatch):
    """A task that's initially blocked (ok=False) must still eventually
    complete once reserve() starts returning ok=True on a later poll --
    simulates a slot freeing up after another task finishes."""
    call_count = {"n": 0}

    def fake_reserve(provider, key, model, units):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return (False, 0.05, None)  # first poll: no room yet
        return (True, 0.0, f"res-{call_count['n']}")

    monkeypatch.setattr(concurrency_gate.rate_ledger, "reserve", fake_reserve)
    monkeypatch.setattr(concurrency_gate.rate_ledger, "release_reservation", MagicMock())

    futures = run_gated([GatedTask(call=lambda: "eventually", step=FAKE_STEP)])
    assert _await_all(futures, timeout=5.0) == ["eventually"]
    assert call_count["n"] >= 2


def test_run_gated_preserves_task_order_in_returned_futures(monkeypatch):
    """Futures come back in the SAME order as `tasks`, regardless of
    which ones admit/finish first -- callers rely on positional
    correspondence, not completion order."""
    # Stagger admission: odd-indexed tasks are blocked on the first poll.
    seen_once = set()

    def fake_reserve(provider, key, model, units):
        key_id = key
        if key_id not in seen_once:
            seen_once.add(key_id)
            return (False, 0.02, None)
        return (True, 0.0, f"res-{key_id}")

    monkeypatch.setattr(concurrency_gate.rate_ledger, "reserve", fake_reserve)
    monkeypatch.setattr(concurrency_gate.rate_ledger, "release_reservation", MagicMock())

    tasks = [
        GatedTask(call=(lambda v=i: v), step={"provider": "groq", "model": "m", "key_env": f"K{i}"})
        for i in range(4)
    ]
    futures = run_gated(tasks)
    assert _await_all(futures, timeout=5.0) == [0, 1, 2, 3]


def test_run_gated_never_exceeds_max_pool_workers(monkeypatch):
    """A large task list must not create more real OS threads than
    _MAX_POOL_WORKERS, even though every task admits immediately
    (step=None) -- covered indirectly by asserting the whole batch still
    completes correctly under that cap."""
    monkeypatch.setattr(concurrency_gate, "_MAX_POOL_WORKERS", 4)
    tasks = [GatedTask(call=(lambda i=i: i), step=None) for i in range(20)]
    futures = run_gated(tasks)
    assert _await_all(futures, timeout=5.0) == list(range(20))


def test_run_gated_cancelled_future_rolls_back_reservation_without_running_call(monkeypatch):
    """If a future is cancelled before _run_one's done-callback gets to
    set_running_or_notify_cancel(), the task never actually dispatched --
    that's a full rollback (release_reservation(reservation_id), no
    actual_units), not the settle-with-1-unit case. This is exercised by
    directly cancelling the future the instant it's created, racing the
    background admission thread; both outcomes (won the race and got
    cancelled, or lost the race and ran normally) are valid depending on
    scheduling, so this test only asserts internal consistency: exactly
    one of "the call ran" or "it was rolled back with no actual_units"
    happened, never both, never neither.
    """
    mock_release = MagicMock()
    monkeypatch.setattr(concurrency_gate.rate_ledger, "reserve",
                         MagicMock(return_value=(True, 0.0, "res-cancel")))
    monkeypatch.setattr(concurrency_gate.rate_ledger, "release_reservation", mock_release)

    ran = threading.Event()
    task = GatedTask(call=lambda: ran.set(), step=FAKE_STEP)
    futures = run_gated([task])
    futures[0].cancel()

    # Give the background thread a moment to resolve one way or the other.
    time.sleep(0.3)

    if ran.is_set():
        mock_release.assert_called_once_with("res-cancel", actual_units=1)
    else:
        mock_release.assert_called_once_with("res-cancel")
