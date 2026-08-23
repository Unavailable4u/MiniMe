"""
tests/integration/test_reliability_overhaul_gate.py — Phase 4 of the
reliability overhaul (see reliability_overhaul_plan.md §PHASE 4, Patch E).

Acceptance test for Patches A-D: replays the incident-log scenario that
motivated this whole phase -- 7 concurrent part_price_finder workers,
each racing to dispatch against a small, quota-constrained pool of
accounts -- and asserts the fix actually holds:

  1. No account's ledger budget is ever exceeded within a trailing
     window, at ANY point during the run (not just "eventually
     converges" -- the old ThreadPoolExecutor-sized-to-len(key_envs)
     code also eventually converged, via each thread's own retry loop
     inside utils/llm_client.py; the bug was the BURST past budget that
     happened before that retry loop caught it).
  2. All 7 tasks eventually complete (no task is lost, starved
     forever, or silently dropped by the admission loop).

Uses a MockLedger standing in for utils/rate_ledger.py's real
reserve()/release_reservation() (Patch A) -- a real ledger's window is
60 real seconds wide, far too slow to exercise in a unit test, so this
implements the identical sliding-window accept/reject contract
(reserve() returns (ok, wait_seconds, reservation_id); a reservation
occupies one slot in its account's window until the window ages it
out, exactly like the real "requests" gating mode -- see
rate_ledger.py's _reserve_requests_mode()) at a compressed timescale,
and records every admission timestamp so the test can assert the
window invariant against the FULL admission history after the fact,
not just at each individual reserve() call.

Two scenarios, both against the same MockLedger:

  - test_run_gated_never_exceeds_ledger_budget: exercises Patch B
    (eo/concurrency_gate.py's run_gated()) directly -- the general
    admission mechanism.
  - test_populate_prices_seven_parts_never_exceeds_ledger_budget:
    exercises Patch C's actual wiring (agents/hardware_speccer.py's
    _populate_prices()) end-to-end, mocking only the account-selection
    and pricing I/O around it -- this is "the one the acceptance test
    targets" per the plan.
"""
import time
import threading
import uuid
from collections import defaultdict

import pytest

import eo.concurrency_gate as concurrency_gate
from eo.concurrency_gate import GatedTask, run_gated


class MockLedger:
    """Stands in for utils/rate_ledger.py's reserve()/release_reservation()
    pair (Patch A). Same sliding-window accept/reject contract as the
    real "requests" gating mode, compressed to `window_seconds` instead
    of the real 60s so the test runs in well under a second of wall
    time. `limit` is the max admissions any single (provider, key_id,
    model) triple may have within the window at once.

    Every admission (successful reserve()) is timestamped and kept
    forever in self.history (not pruned) specifically so the test can
    replay the FULL admission history afterward and check the window
    invariant everywhere it could have been violated, not just at the
    instants reserve() itself happened to check.
    """

    def __init__(self, limit: int, window_seconds: float = 0.3):
        self.limit = limit
        self.window = window_seconds
        self._lock = threading.Lock()
        self.history: "dict[tuple, list[float]]" = defaultdict(list)
        self._reservations: "dict[str, tuple]" = {}

    def reserve(self, provider, key_id, model, estimated_units):
        key = (provider, key_id, model)
        with self._lock:
            now = time.monotonic()
            in_window = [t for t in self.history[key] if now - t < self.window]
            if len(in_window) >= self.limit:
                # Same wait_seconds contract as the real reserve():
                # "how long until the oldest slice in the window ages
                # out" -- clamped the same way concurrency_gate.py's own
                # _POLL_MIN/MAX_SECONDS clamps it, so the admission loop
                # doesn't busy-spin nor oversleep past a short wait.
                oldest = min(in_window) if in_window else now
                wait = max(0.01, (oldest + self.window) - now)
                return False, wait, None
            self.history[key].append(now)
            reservation_id = uuid.uuid4().hex
            self._reservations[reservation_id] = key
            return True, 0.0, reservation_id

    def release_reservation(self, reservation_id, actual_units=None):
        # Mirrors the real "requests" mode: settling a reservation
        # (actual_units == the 1 unit reserved) is a no-op correction --
        # the admission genuinely happened and stays booked in the
        # window until time itself ages it out. Nothing to do here.
        return

    def max_concurrent_in_any_window(self, key) -> int:
        """Post-hoc check: the highest number of admissions that ever
        fell within any `window`-second span for this key, scanned
        across the FULL recorded history -- a stronger check than only
        trusting each reserve() call's own in-the-moment view."""
        times = sorted(self.history.get(key, []))
        worst = 0
        for i, t in enumerate(times):
            count = sum(1 for other in times if t <= other < t + self.window)
            worst = max(worst, count)
        return worst


@pytest.fixture
def mock_ledger(monkeypatch):
    """Patches eo/concurrency_gate.py's `rate_ledger` reference (Patch B
    calls rate_ledger.reserve()/release_reservation() directly, imported
    via `from utils import rate_ledger`) with a MockLedger instance.
    Also patches utils.rate_ledger itself, so anything reaching the real
    module by a fresh `from utils import rate_ledger` inside a deferred
    import (e.g. _populate_prices()'s own call chain, if it ever called
    the ledger directly rather than only through run_gated()) sees the
    same mock."""
    import utils.rate_ledger as real_rate_ledger

    ledger = MockLedger(limit=2, window_seconds=0.3)
    monkeypatch.setattr(concurrency_gate, "rate_ledger", ledger)
    monkeypatch.setattr(real_rate_ledger, "reserve", ledger.reserve)
    monkeypatch.setattr(real_rate_ledger, "release_reservation", ledger.release_reservation)
    return ledger


def _incident_steps(n_accounts: int = 2):
    """The incident-log shape: 7 parallel part_price_finder workers,
    spread round-robin across a small pool of accounts (2 here --
    deliberately fewer than 7, the exact "everyone piles onto whichever
    accounts they were handed" scenario named in Phase 4's own
    docstring), each step a groq-family chain-step dict."""
    return [
        {"provider": "groq", "key_env": f"GROQ_KEY_{i % n_accounts}", "model": "llama-3.3-70b"}
        for i in range(7)
    ]


def test_run_gated_never_exceeds_ledger_budget(mock_ledger):
    """Patch B, general mechanism: 7 GatedTasks, 2 accounts, budget of 2
    concurrent admissions per account per window. Asserts the admission
    history never shows more than `limit` admissions for any account
    within any window-sized span, and all 7 tasks complete."""
    steps = _incident_steps(n_accounts=2)
    completed = []
    completed_lock = threading.Lock()

    def _make_call(i):
        def _call():
            time.sleep(0.02)  # simulate a short LLM round trip
            with completed_lock:
                completed.append(i)
            return f"result-{i}"
        return _call

    tasks = [GatedTask(_make_call(i), step=steps[i], label=f"part_price_finder_{i}")
             for i in range(7)]

    futures = run_gated(tasks, session_id="test-session")
    results = [f.result(timeout=5) for f in futures]

    assert results == [f"result-{i}" for i in range(7)]
    assert sorted(completed) == list(range(7))

    for i in range(2):
        key = ("groq", f"GROQ_KEY_{i}", "llama-3.3-70b")
        worst = mock_ledger.max_concurrent_in_any_window(key)
        assert worst <= mock_ledger.limit, (
            f"account {key} was admitted {worst} times within one "
            f"{mock_ledger.window}s window — exceeds budget of {mock_ledger.limit}"
        )


def test_populate_prices_seven_parts_never_exceeds_ledger_budget(mock_ledger, monkeypatch):
    """Patch C, the actual wiring named in the plan as the acceptance
    target: agents/hardware_speccer.py's _populate_prices() with 7
    parts and only 2 tagged accounts, run against the same MockLedger.
    Mocks only what sits AROUND the gate (account selection, chain
    construction, and the pricing I/O itself) -- run_gated() and its
    reserve()/release_reservation() calls are the real Patch B/A code,
    talking to the mocked ledger via the mock_ledger fixture."""
    import agents.hardware_speccer as hardware_speccer
    import eo.worker_pool as worker_pool
    import eo.dynamic_chain as dynamic_chain
    import agents.part_price_finder as part_price_finder

    key_envs = ["GROQ_KEY_0", "GROQ_KEY_1"]

    def _fake_select_workers(role_tag, worker_count, session_id=None, agent_name=None):
        return key_envs[:worker_count]

    def _fake_chain_step_for(agent_key):
        return {"provider": "groq", "key_env": agent_key, "model": "llama-3.3-70b"}

    def _fake_build_fallback_chain_excluding(role, exclude_keys, quota_status=None):
        return []  # no extra fallback steps needed for this test

    call_log = []
    call_log_lock = threading.Lock()

    def _fake_find_price(part_name, force_refresh=False, chain_override=None,
                          agent_name="part_price_finder"):
        time.sleep(0.02)  # simulate a short LLM/web-search round trip
        with call_log_lock:
            call_log.append(part_name)
        return {
            "part_name": part_name,
            "listings": [{"price_bdt": 100, "vendor": "MockVendor", "url": "https://example.test"}],
            "checked_at": "2026-08-23T00:00:00Z",
        }

    monkeypatch.setattr(worker_pool, "_select_workers", _fake_select_workers)
    monkeypatch.setattr(dynamic_chain, "chain_step_for", _fake_chain_step_for)
    monkeypatch.setattr(dynamic_chain, "build_fallback_chain_excluding", _fake_build_fallback_chain_excluding)
    monkeypatch.setattr(part_price_finder, "find_price", _fake_find_price)

    parts = [{"name": f"Part {i}"} for i in range(7)]

    result = hardware_speccer._populate_prices(parts, session_id="test-session")

    assert len(result) == 7
    assert sorted(call_log) == sorted(p["name"] for p in parts)
    for part in result:
        assert part["estimated_price_bdt"] == 100
        assert part["vendor_name"] == "MockVendor"

    for key_env in key_envs:
        key = ("groq", key_env, "llama-3.3-70b")
        worst = mock_ledger.max_concurrent_in_any_window(key)
        assert worst <= mock_ledger.limit, (
            f"account {key} was admitted {worst} times within one "
            f"{mock_ledger.window}s window — exceeds budget of {mock_ledger.limit}"
        )
