"""
tests/unit/test_concurrency_gate_neurons.py

Regression coverage for two bugs found by tracing eo/concurrency_gate.py's
outer per-account throttle against the CURRENT utils/rate_ledger.py:

  1. _admit() always reserved 1 unit with no max_output_tokens, regardless
     of gating mode. Harmless for "tokens"/"requests" (a deliberate
     placeholder there -- see _outer_reservation_size()'s docstring), but
     in "neurons" mode that "1" is interpreted as a 1-token input
     estimate with no output ceiling, so _estimate_neurons() always
     returned a fraction-of-a-neuron figure -- reserve() could never
     actually refuse admission for a Cloudflare account, no matter how
     exhausted its real daily neuron budget was.

  2. The cancelled-future rollback path in _run_one() called
     rate_ledger.release_reservation(reservation_id) with no other args,
     relying on that meaning "full rollback". release_reservation()'s
     current signature defaults dispatched=True (settle), so that call
     was silently settling instead of rolling back -- a cancelled task's
     outer reservation stayed permanently booked.

Uses the REAL utils.rate_ledger module (not a mock) against the
autouse fake_bus fixture (tests/conftest.py) so these tests exercise the
actual reserve()/release_reservation()/_gating_mode_for() code, the same
way test_rate_ledger_neurons.py does.
"""
import threading
import time
from concurrent.futures import Future

import pytest

import utils.llm_client as llm_client_module
import utils.rate_ledger as rate_ledger
from eo import concurrency_gate
from eo.concurrency_gate import GatedTask, run_gated

PROVIDER = "cloudflare"
MODEL = "@cf/meta/llama-3.1-8b-instruct"
KEY_ID = "TEST_CF_ACCOUNT_ID"

# Same real, published Workers AI rates test_rate_ledger_neurons.py uses --
# duplicated (not imported) so this file doesn't silently start
# passing/failing for a reason unrelated to what it tests if that table
# ever changes.
NEURON_CONFIG = {
    "neurons_rpd": 10000,
    "neurons_per_m_input_tokens": 25608,
    "neurons_per_m_output_tokens": 75147,
}

CLOUDFLARE_STEP = {
    "provider": PROVIDER,
    "model": MODEL,
    "account_id_env": KEY_ID,
    "token_env": "TEST_CF_TOKEN",
}


@pytest.fixture(autouse=True)
def _fake_cloudflare_quota_config(monkeypatch):
    monkeypatch.setitem(llm_client_module.QUOTA_CONFIG, PROVIDER, {MODEL: dict(NEURON_CONFIG)})
    yield


def _max_tokens_for_cloudflare_step() -> int:
    from utils.llm_client import _max_tokens_for
    return _max_tokens_for(MODEL, CLOUDFLARE_STEP)


# ---------------------------------------------------------------------
# Bug 1: the outer throttle now actually gates neurons-mode accounts
# ---------------------------------------------------------------------

def test_admit_reserves_a_real_neuron_estimate_not_one_token():
    """_admit() (via _outer_reservation_size()) must reserve the step's
    real max_tokens ceiling on both sides for a neurons-mode step, not
    the bare "1" every other mode uses."""
    estimated_units, max_output_tokens = concurrency_gate._outer_reservation_size(CLOUDFLARE_STEP)
    ceiling = _max_tokens_for_cloudflare_step()
    assert estimated_units == ceiling
    assert max_output_tokens == ceiling
    assert estimated_units > 1  # would be exactly 1 under the old bug


def test_admit_denies_when_daily_neuron_budget_cannot_fit_the_estimate():
    """Regression for bug 1. Pre-load today's neuron total to leave only
    10 neurons of headroom -- nowhere near enough for a worst-case
    max_tokens-ceiling-sized call (hundreds of neurons for this model),
    but comfortably enough for the OLD, buggy 1-input-token-no-output-
    ceiling estimate (~0.1 neurons) to still fit. Confirms the outer
    gate's own _admit() correctly refuses admission on the real
    estimate; under the old code this specific gap would have wrongly
    admitted every time regardless of how little real headroom
    remained."""
    rate_ledger._adjust_daily_neurons(PROVIDER, KEY_ID, 9990.0)
    task = GatedTask(lambda: "unused", step=CLOUDFLARE_STEP)

    ok, wait, reservation_id = concurrency_gate._admit(task)

    assert ok is False
    assert reservation_id is None
    assert wait >= 0


def test_admit_allows_when_budget_has_real_room():
    """Sanity counterpart: with a nearly-empty daily total, the same
    step is admitted and gets a real reservation id."""
    rate_ledger._adjust_daily_neurons(PROVIDER, KEY_ID, 10.0)
    task = GatedTask(lambda: "unused", step=CLOUDFLARE_STEP)

    ok, wait, reservation_id = concurrency_gate._admit(task)

    assert ok is True
    assert reservation_id is not None


# ---------------------------------------------------------------------
# Bug 1 continued: settling must NOT double-book against the real,
# inner-layer-tracked usage -- the outer estimate has to roll back to
# zero once the task is done, not stay booked at its worst-case size.
# ---------------------------------------------------------------------

def test_release_rolls_back_the_outer_neuron_estimate_to_zero():
    """After a neurons-mode task completes, run_gated()'s _release()
    call must fully roll back the outer reservation (dispatched=False),
    not settle it -- otherwise the worst-case estimate stays booked
    forever ON TOP OF whatever the task's own real dispatch separately
    books one layer down, double-counting every call."""
    before = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)

    tasks = [GatedTask(lambda: "ok", step=CLOUDFLARE_STEP, label="t")]
    futures = run_gated(tasks)
    results = [f.result(timeout=5) for f in futures]

    assert results == ["ok"]
    after = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)
    # Rolled back to (approximately) where it started -- NOT bumped up
    # by the full max_tokens-ceiling-sized estimate that was reserved
    # for admission-control purposes only.
    assert after == pytest.approx(before, abs=1e-6)


def test_release_leaves_worst_case_booked_would_have_failed_old_behavior():
    """Directly demonstrates what the bug used to do: manually reserve
    a neurons-mode outer slot the way _admit() now does, then release it
    the OLD way (settle, actual_units=1) vs the FIXED way (rollback) --
    confirms the old behavior really did leave the full ceiling stuck,
    and the fix really does clear it."""
    ceiling = _max_tokens_for_cloudflare_step()
    ok, _wait, reservation_id = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, ceiling,
                                                      max_output_tokens=ceiling)
    assert ok is True
    before_release = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)
    assert before_release > 0  # the worst-case estimate really did get booked

    # OLD buggy _release() behavior: settle with actual_units=1, which
    # release_reservation()'s "neurons" branch ignores entirely (only
    # actual_input_tokens/actual_output_tokens matter for that mode) --
    # so the full worst-case estimate stays standing.
    rate_ledger.release_reservation(reservation_id, actual_units=1)
    after_old_behavior = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)
    assert after_old_behavior == pytest.approx(before_release, abs=1e-6)

    # FIXED behavior: a second reservation, released the way the patched
    # _release() now does.
    ok2, _wait2, reservation_id2 = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, ceiling,
                                                        max_output_tokens=ceiling)
    assert ok2 is True
    before_release2 = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)
    rate_ledger.release_reservation(reservation_id2, dispatched=False)
    after_fixed_behavior = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)
    assert after_fixed_behavior < before_release2
    assert after_fixed_behavior == pytest.approx(after_old_behavior, abs=1e-6)


# ---------------------------------------------------------------------
# Bug 2: a cancelled task's reservation is rolled back, not settled
# ---------------------------------------------------------------------

def test_cancelled_future_rolls_back_neuron_reservation():
    """Regression for bug 2. Reproduces _run_one()'s cancellation branch
    directly: reserve a neurons-mode slot, then release it exactly the
    way the fixed cancelled-future path does (dispatched=False), and
    confirm the booking clears -- the pre-fix call
    (release_reservation(reservation_id) with no other args) would have
    left it settled at the old default (dispatched=True), permanently
    occupying budget the task never used."""
    ceiling = _max_tokens_for_cloudflare_step()
    ok, _wait, reservation_id = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, ceiling,
                                                      max_output_tokens=ceiling)
    assert ok is True
    before = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)
    assert before > 0

    # This is the exact call concurrency_gate.py's _run_one() now makes
    # on the cancelled-future branch.
    rate_ledger.release_reservation(reservation_id, dispatched=False)

    after = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)
    assert after < before


def test_cancelled_future_old_call_shape_would_not_have_rolled_back():
    """Confirms the actual pre-fix bug: calling release_reservation()
    with no dispatched kwarg at all (the literal old code) settles
    rather than rolls back, under the CURRENT release_reservation()
    default -- this is what made bug 2 real, not hypothetical."""
    ceiling = _max_tokens_for_cloudflare_step()
    ok, _wait, reservation_id = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, ceiling,
                                                      max_output_tokens=ceiling)
    assert ok is True
    before = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)

    rate_ledger.release_reservation(reservation_id)  # old call shape, no dispatched kwarg

    after = rate_ledger._read_daily_neurons(PROVIDER, KEY_ID)
    assert after == pytest.approx(before, abs=1e-6)  # settled, not rolled back


# ---------------------------------------------------------------------
# Non-neurons modes must be completely unaffected
# ---------------------------------------------------------------------

def test_outer_reservation_size_unchanged_for_tokens_and_requests_modes():
    groq_step = {"provider": "groq", "key_env": "GROQ_KEY", "model": "llama-3.3-70b-versatile"}
    or_step = {"provider": "openrouter", "key_env": "OR_KEY", "model": "openrouter/free"}
    assert concurrency_gate._outer_reservation_size(groq_step) == (1, None)
    assert concurrency_gate._outer_reservation_size(or_step) == (1, None)


def test_gating_mode_lookup_degrades_gracefully_for_mock_style_ledger(monkeypatch):
    """If `rate_ledger` is swapped for something without _gating_mode_for
    (e.g. test_reliability_overhaul_gate.py's MockLedger), the lookup
    must fail safe to None rather than raise."""
    class _Stub:
        pass
    monkeypatch.setattr(concurrency_gate, "rate_ledger", _Stub())
    assert concurrency_gate._gating_mode_for_step(CLOUDFLARE_STEP) is None
    assert concurrency_gate._outer_reservation_size(CLOUDFLARE_STEP) == (1, None)
