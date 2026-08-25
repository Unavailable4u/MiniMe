"""
tests/unit/test_rate_ledger_neurons.py — Patch I.2 follow-up.

utils/rate_ledger.py (1200+ lines) had zero test coverage before this
patch series, and the "neurons" gating mode it added for Cloudflare
Workers AI (_estimate_neurons, _reserve_neurons_mode,
release_reservation()'s neurons branch, headroom_snapshot()'s neurons
branch) is the newest, least-exercised part of it. This file covers
that mode specifically, not the pre-existing tokens/requests modes
(those still have no dedicated test file either — out of scope for this
patch, which is about neurons mode).

Style notes (mirrors tests/unit/test_eo_quota_sentinel.py's own
documented conventions):
  - rate_ledger's *_limit_for()/_neuron_rates_for()/_config_for() all do
    `from utils.llm_client import QUOTA_CONFIG` INSIDE the function body
    (a deferred import, dodging a circular import — see rate_ledger.py's
    own _config_for() docstring). That means monkeypatching
    utils.rate_ledger.QUOTA_CONFIG would NOT reach these functions —
    they re-resolve the name from utils.llm_client at call time. Every
    test below patches utils.llm_client.QUOTA_CONFIG directly (via
    monkeypatch.setitem, so only the touched provider key is patched and
    automatically restored, leaving the rest of the real table alone
    for any other test module that imports it in the same session).
  - memory.bus needs no mocking: tests/conftest.py's autouse fake_bus
    fixture already swaps memory.bus.redis for an in-memory FakeRedis
    before every test.
  - No date-freezing library available (checked requirements.txt) — the
    UTC-day bucket key is deterministic from wall-clock time within a
    single test's execution window, so tests don't need to freeze it;
    _neuron_day_bucket_key()'s own day rollover isn't under test here
    (rpd/rpm mode's equivalent day-rollover behavior isn't tested
    elsewhere in this codebase either — same scope boundary).
"""
import pytest

import utils.llm_client as llm_client_module
import utils.rate_ledger as rate_ledger

PROVIDER = "cloudflare"
MODEL = "@cf/meta/llama-3.1-8b-instruct"
KEY_ID = "TEST_CF_ACCOUNT_ID"

# Real, published Workers AI rates (developers.cloudflare.com/workers-ai/
# platform/pricing/) for the model this patch series actually wired in —
# same figures llm_client.py's QUOTA_CONFIG carries, duplicated here (not
# imported) so this test module doesn't silently start passing/failing
# for a reason unrelated to what it's testing if that table ever changes.
NEURON_CONFIG = {
    "neurons_rpd": 10000,
    "neurons_per_m_input_tokens": 25608,
    "neurons_per_m_output_tokens": 75147,
}


@pytest.fixture(autouse=True)
def _fake_cloudflare_quota_config(monkeypatch):
    monkeypatch.setitem(llm_client_module.QUOTA_CONFIG, PROVIDER, {MODEL: dict(NEURON_CONFIG)})
    yield


def _expected_neurons(input_tokens: int, output_tokens: int) -> float:
    return ((input_tokens / 1_000_000) * NEURON_CONFIG["neurons_per_m_input_tokens"]
            + (output_tokens / 1_000_000) * NEURON_CONFIG["neurons_per_m_output_tokens"])


# ---------------------------------------------------------------------
# _gating_mode_for
# ---------------------------------------------------------------------

def test_gating_mode_is_neurons_when_only_neurons_rpd_present():
    assert rate_ledger._gating_mode_for(PROVIDER, MODEL) == "neurons"


def test_gating_mode_prefers_tpm_over_neurons_rpd_if_both_present(monkeypatch):
    """Documented precedence (see _gating_mode_for()'s own docstring):
    a real per-minute/per-day TOKEN ceiling is a more precise signal
    than the neuron proxy, so tpm wins if a provider ever published
    both."""
    monkeypatch.setitem(llm_client_module.QUOTA_CONFIG, PROVIDER,
                         {MODEL: {**NEURON_CONFIG, "tpm": 50000}})
    assert rate_ledger._gating_mode_for(PROVIDER, MODEL) == "tokens"


def test_gating_mode_falls_back_to_tokens_with_no_config_at_all():
    assert rate_ledger._gating_mode_for(PROVIDER, "@cf/meta/some-unconfigured-model") == "tokens"


# ---------------------------------------------------------------------
# _estimate_neurons
# ---------------------------------------------------------------------

def test_estimate_neurons_uses_published_rates():
    result = rate_ledger._estimate_neurons(PROVIDER, MODEL, 1000, 500)
    assert result == pytest.approx(_expected_neurons(1000, 500))


def test_estimate_neurons_uses_output_ceiling_not_a_guess():
    """Docstring's whole point: the output side is the step's max_tokens
    CEILING, not proportional to input size — two calls with identical
    input but different max_tokens must estimate differently."""
    small_ceiling = rate_ledger._estimate_neurons(PROVIDER, MODEL, 1000, 200)
    large_ceiling = rate_ledger._estimate_neurons(PROVIDER, MODEL, 1000, 8000)
    assert large_ceiling > small_ceiling


def test_estimate_neurons_falls_back_to_input_tokens_when_no_ceiling_given():
    result = rate_ledger._estimate_neurons(PROVIDER, MODEL, 1000, None)
    assert result == pytest.approx(_expected_neurons(1000, 1000))


def test_estimate_neurons_none_when_rates_unverified(monkeypatch):
    monkeypatch.setitem(llm_client_module.QUOTA_CONFIG, PROVIDER, {MODEL: {"neurons_rpd": 10000}})
    assert rate_ledger._estimate_neurons(PROVIDER, MODEL, 1000, 500) is None


# ---------------------------------------------------------------------
# can_proceed() — read-only check
# ---------------------------------------------------------------------

def test_can_proceed_true_with_empty_daily_bucket():
    ok, wait = rate_ledger.can_proceed(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    assert ok is True
    assert wait == 0.0


def test_can_proceed_false_once_daily_budget_exhausted():
    rate_ledger._adjust_daily_neurons(PROVIDER, KEY_ID, 9999.0)
    ok, wait = rate_ledger.can_proceed(PROVIDER, KEY_ID, MODEL, 1_000_000, max_output_tokens=1_000_000)
    assert ok is False
    assert wait > 0.0


def test_can_proceed_fails_open_with_no_verified_rates(monkeypatch):
    monkeypatch.setitem(llm_client_module.QUOTA_CONFIG, PROVIDER, {MODEL: {"neurons_rpd": 10000}})
    ok, wait = rate_ledger.can_proceed(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    assert ok is True
    assert wait == 0.0


def test_can_proceed_fails_open_with_no_verified_daily_budget(monkeypatch):
    monkeypatch.setitem(llm_client_module.QUOTA_CONFIG, PROVIDER,
                         {MODEL: {"neurons_per_m_input_tokens": 25608, "neurons_per_m_output_tokens": 75147}})
    ok, wait = rate_ledger.can_proceed(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    assert ok is True
    assert wait == 0.0


# ---------------------------------------------------------------------
# reserve() / release_reservation() — the real gate, read+write
# ---------------------------------------------------------------------

def test_reserve_books_the_estimate_into_the_daily_bucket():
    ok, wait, reservation_id = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    assert ok is True
    assert reservation_id is not None
    assert rate_ledger.daily_neurons_used(PROVIDER, KEY_ID) == pytest.approx(_expected_neurons(1000, 500))


def test_reserve_blocks_once_the_new_estimate_would_exceed_budget():
    rate_ledger._adjust_daily_neurons(PROVIDER, KEY_ID, 9999.99)
    ok, wait, reservation_id = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 1_000_000, max_output_tokens=1_000_000)
    assert ok is False
    assert reservation_id is None
    assert wait > 0.0
    # Blocking must not itself have booked anything.
    assert rate_ledger.daily_neurons_used(PROVIDER, KEY_ID) == pytest.approx(9999.99)


def test_release_reservation_full_rollback_when_not_dispatched():
    """A step that got reserved but then rerouted to a different chain
    step, or errored with nothing usable, must roll all the way back —
    this is the exact bug this patch series fixed: dispatched=False is
    now the explicit signal, decoupled from actual_units."""
    ok, wait, reservation_id = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    assert ok is True
    assert rate_ledger.daily_neurons_used(PROVIDER, KEY_ID) > 0.0

    rate_ledger.release_reservation(reservation_id, dispatched=False)
    assert rate_ledger.daily_neurons_used(PROVIDER, KEY_ID) == pytest.approx(0.0)


def test_release_reservation_leaves_estimate_standing_when_usage_absent():
    """THE bug this patch series fixed: a dispatched call with no usable
    usage split (Cloudflare's routine case) must NOT roll back to zero —
    the worst-case estimate has to stand as the enforced, conservative
    figure. Regression test for the exact scenario found while auditing
    _record_ledger_bookkeeping()'s call site."""
    ok, wait, reservation_id = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    assert ok is True
    booked = rate_ledger.daily_neurons_used(PROVIDER, KEY_ID)
    assert booked > 0.0

    # Dispatched successfully, but usage was absent — exactly what
    # llm_client.py's _record_ledger_bookkeeping() passes on a real,
    # successful Cloudflare call with no usage object in the response.
    rate_ledger.release_reservation(reservation_id, actual_units=None,
                                     actual_input_tokens=None, actual_output_tokens=None,
                                     dispatched=True)
    assert rate_ledger.daily_neurons_used(PROVIDER, KEY_ID) == pytest.approx(booked)


def test_release_reservation_trues_up_to_real_usage_when_available():
    """The minority case: Cloudflare's response DID carry a real
    prompt/completion split. The booking must correct from the
    worst-case estimate down to the real, smaller figure."""
    ok, wait, reservation_id = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=8000)
    worst_case = rate_ledger.daily_neurons_used(PROVIDER, KEY_ID)
    assert worst_case == pytest.approx(_expected_neurons(1000, 8000))

    # Real completion was much shorter than the 8000-token ceiling.
    rate_ledger.release_reservation(reservation_id, actual_input_tokens=1000, actual_output_tokens=120,
                                     dispatched=True)
    trued_up = rate_ledger.daily_neurons_used(PROVIDER, KEY_ID)
    assert trued_up == pytest.approx(_expected_neurons(1000, 120))
    assert trued_up < worst_case


def test_release_reservation_is_idempotent_against_double_release():
    ok, wait, reservation_id = rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    booked = rate_ledger.daily_neurons_used(PROVIDER, KEY_ID)

    rate_ledger.release_reservation(reservation_id, dispatched=False)
    assert rate_ledger.daily_neurons_used(PROVIDER, KEY_ID) == pytest.approx(0.0)

    # A second release of the SAME reservation_id must be a no-op, not a
    # second rollback (there's nothing left to roll back a second time).
    rate_ledger.release_reservation(reservation_id, dispatched=False)
    assert rate_ledger.daily_neurons_used(PROVIDER, KEY_ID) == pytest.approx(0.0)


def test_release_reservation_none_id_is_a_safe_noop():
    rate_ledger.release_reservation(None, dispatched=False)  # must not raise
    assert rate_ledger.daily_neurons_used(PROVIDER, KEY_ID) == pytest.approx(0.0)


def test_two_reservations_accumulate_in_the_same_account_bucket():
    """Confirms the (provider, key_id)-only keying (deliberately no
    `model` — see _neuron_day_bucket_key()'s own docstring on why the
    real Workers AI budget is account-wide): two different reservations
    against the same account both count against the one shared total."""
    rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 2000, max_output_tokens=500)
    expected = _expected_neurons(1000, 500) + _expected_neurons(2000, 500)
    assert rate_ledger.daily_neurons_used(PROVIDER, KEY_ID) == pytest.approx(expected)


# ---------------------------------------------------------------------
# headroom_snapshot()
# ---------------------------------------------------------------------

def test_headroom_snapshot_reports_neurons_mode_daily_figures():
    rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    snap = rate_ledger.headroom_snapshot(PROVIDER, KEY_ID, MODEL)
    assert snap["gating_mode"] == "neurons"
    assert snap["daily"]["used_today"] == pytest.approx(_expected_neurons(1000, 500))
    assert snap["daily"]["rpd_limit"] == 10000
    assert snap["daily"]["pct_used"] == pytest.approx(_expected_neurons(1000, 500) / 10000)


def test_headroom_snapshot_sliding_window_stays_empty_for_neurons_mode():
    """Neurons mode has no per-minute sliding-window concept at all —
    nothing should ever write to that provider/model's window key."""
    rate_ledger.reserve(PROVIDER, KEY_ID, MODEL, 1000, max_output_tokens=500)
    snap = rate_ledger.headroom_snapshot(PROVIDER, KEY_ID, MODEL)
    assert snap["sliding_window"]["used_last_60s"] == 0
    assert snap["sliding_window"]["tpm_limit"] is None
