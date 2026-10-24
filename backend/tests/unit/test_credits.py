"""eo/credits.py (S02): cost-weighted credits for one model call.

Two kinds of test live here, on purpose:

  * HERMETIC tests (most of them). They point CREDITS_CONFIG_PATH at a tmp
    file and monkeypatch utils.llm_client._TOKEN_PRICES_PER_MILLION with a
    small fake table, so every expected number below is worked out by hand
    from prices written in this file. A price edit in the real table or in
    config/credits.json cannot make these fail.
  * SHIPPED-DATA tests (the last section). They read the real
    config/credits.json and the real llm_client table and pin the
    invariants a future edit could break: every priced model has a cached
    price, a cached read never costs more than an uncached one, and the
    unknown-model default is at least as expensive as every priced model.

No database is touched. eo.credits imports eo.plans (for the single
credits_per_usd constant), which imports eo.db, but nothing here opens a
connection.
"""
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from eo import credits, plans
from utils import llm_client

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
CREDITS_JSON = BACKEND_DIR / "config" / "credits.json"

# $ per 1,000,000 tokens. Input/output come from the (fake) llm_client
# table, cached-read from the (fake) credits.json, exactly as in production.
FAKE_TABLE = {
    "groq":       {"model-a": {"input": 0.15, "output": 0.75}},
    "mistral":    {"model-b": {"input": 0.40, "output": 2.00}},
    "gemini":     {"model-c": {"input": 0.15, "output": 0.60}},
    "cloudflare": {"model-d": {"input": 0.29, "output": 2.25}},
}
FAKE_CREDITS = {
    "default_prices_usd_per_million": {"input": 1.0, "cached_input": 1.0, "output": 3.0},
    "models": {
        "groq":       {"model-a": {"cached_input": 0.075}},
        "mistral":    {"model-b": {"cached_input": 0.04}},
        "gemini":     {"model-c": {"cached_input": 0.15}},      # no discount
        "cloudflare": {"model-d": {"cached_input": 0.29}},      # no discount
        # a model that is NOT in the llm_client table: full entry here
        "groq-extra": {"model-e": {"input": 0.075, "cached_input": 0.0375, "output": 0.30}},
    },
}
CREDITS_PER_USD = 1000.0  # what config/plans.json ships today (D5 placeholder)


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    cfg = tmp_path / "credits.json"
    cfg.write_text(json.dumps(FAKE_CREDITS), encoding="utf-8")
    monkeypatch.setenv("CREDITS_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(llm_client, "_TOKEN_PRICES_PER_MILLION", FAKE_TABLE)
    monkeypatch.setattr(credits.plans, "credits_per_usd", lambda: CREDITS_PER_USD)
    credits.reload_config()
    yield
    credits.reload_config()


def usd(per_million_sum: float) -> float:
    """Credits for a price-sum expressed in $-per-million-token units."""
    return per_million_sum / 1_000_000 * CREDITS_PER_USD


# ---- four models, hand-computed --------------------------------------

@pytest.mark.parametrize("provider, model, cached_price, in_price, out_price", [
    ("groq", "model-a", 0.075, 0.15, 0.75),
    ("mistral", "model-b", 0.04, 0.40, 2.00),
    ("gemini", "model-c", 0.15, 0.15, 0.60),
    ("cloudflare", "model-d", 0.29, 0.29, 2.25),
])
def test_each_model_is_priced_from_its_own_three_prices(provider, model, cached_price,
                                                       in_price, out_price):
    # 10,000 prompt tokens (4,000 of them cached) and 1,000 output tokens
    got = credits.credits_for(provider, model, 10_000, 4_000, 1_000)
    want = usd(6_000 * in_price + 4_000 * cached_price + 1_000 * out_price)
    assert got == pytest.approx(want, abs=1e-6)


def test_model_missing_from_the_table_is_priced_from_credits_json():
    got = credits.credits_for("groq-extra", "model-e", 10_000, 4_000, 1_000)
    assert got == pytest.approx(usd(6_000 * 0.075 + 4_000 * 0.0375 + 1_000 * 0.30), abs=1e-6)


def test_table_wins_for_input_and_output_json_wins_for_cached(tmp_path, monkeypatch):
    # credits.json carries a different input/output for a table model: ignored.
    cfg = json.loads(json.dumps(FAKE_CREDITS))
    cfg["models"]["groq"]["model-a"] = {"input": 99.0, "cached_input": 0.075, "output": 99.0}
    p = tmp_path / "override.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("CREDITS_CONFIG_PATH", str(p))
    credits.reload_config()
    got = credits.credits_for("groq", "model-a", 10_000, 4_000, 1_000)
    assert got == pytest.approx(usd(6_000 * 0.15 + 4_000 * 0.075 + 1_000 * 0.75), abs=1e-6)


# ---- the agent-style pattern ------------------------------------------

def test_agent_loop_call_is_cost_weighted_not_raw_token_counts():
    # One step of an agent loop: a 50,000-token prompt of which 45,000 is the
    # cached prefix re-read, and a short 500-token reply.
    got = credits.credits_for("groq", "model-a", 50_000, 45_000, 500)
    # (5,000 x 0.15 + 45,000 x 0.075 + 500 x 0.75) / 1e6 = $0.0045 -> 4.5 credits
    assert got == pytest.approx(4.5, abs=1e-6)

    raw_tokens = 50_000 + 500
    assert got != raw_tokens and got < raw_tokens / 1000          # nowhere near token count
    no_cache = credits.credits_for("groq", "model-a", 50_000, 0, 500)
    assert no_cache == pytest.approx(7.875, abs=1e-6)             # same call, cold cache
    assert got < no_cache                                         # the cache discount is applied
    # 1,000 such steps cost what they should, not 50.5M "token credits":
    assert got * 1_000 == pytest.approx(4_500.0, abs=1e-3)


def test_cached_prefix_dominates_but_output_is_still_charged():
    prefix_only = credits.credits_for("groq", "model-a", 50_000, 50_000, 0)
    with_reply = credits.credits_for("groq", "model-a", 50_000, 50_000, 500)
    assert with_reply - prefix_only == pytest.approx(usd(500 * 0.75), abs=1e-6)


# ---- unknown models ---------------------------------------------------

def test_unknown_model_uses_the_default_price_and_is_charged_at_least_something():
    got = credits.credits_for("nobody", "mystery-1", 10_000, 0, 1_000)
    assert got == pytest.approx(usd(10_000 * 1.0 + 1_000 * 3.0), abs=1e-6)  # 13.0
    assert got > 0


def test_unknown_model_logs_once_per_provider_model(caplog):
    with caplog.at_level(logging.WARNING, logger="eo.credits"):
        for _ in range(5):
            credits.credits_for("nobody", "mystery-1", 1_000, 0, 100)
        assert len(caplog.records) == 1
        assert "nobody" in caplog.text and "mystery-1" in caplog.text

        credits.credits_for("nobody", "mystery-2", 1_000, 0, 100)    # different model
        credits.credits_for("other", "mystery-1", 1_000, 0, 100)     # different provider
        assert len(caplog.records) == 3

        credits.credits_for("nobody", "mystery-1", 1_000, 0, 100)    # still silent
        assert len(caplog.records) == 3


def test_known_models_never_log(caplog):
    with caplog.at_level(logging.WARNING, logger="eo.credits"):
        credits.credits_for("groq", "model-a", 1_000, 500, 100)
        credits.credits_for("groq-extra", "model-e", 1_000, 500, 100)
    assert caplog.records == []


def test_a_json_entry_without_input_output_is_not_a_price(tmp_path, monkeypatch, caplog):
    # cached_input alone, for a model that is not in the table: cannot price
    # the other two components, so it is an unknown model, not a free one.
    cfg = json.loads(json.dumps(FAKE_CREDITS))
    cfg["models"]["ghost"] = {"model-g": {"cached_input": 0.01}}
    p = tmp_path / "ghost.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("CREDITS_CONFIG_PATH", str(p))
    credits.reload_config()
    with caplog.at_level(logging.WARNING, logger="eo.credits"):
        got = credits.credits_for("ghost", "model-g", 10_000, 0, 1_000)
    assert got == pytest.approx(usd(10_000 * 1.0 + 1_000 * 3.0), abs=1e-6)
    assert len(caplog.records) == 1


def test_table_model_without_a_cached_price_is_billed_at_input_price_and_logs_once(
        tmp_path, monkeypatch, caplog):
    cfg = json.loads(json.dumps(FAKE_CREDITS))
    del cfg["models"]["groq"]["model-a"]
    p = tmp_path / "nocached.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("CREDITS_CONFIG_PATH", str(p))
    credits.reload_config()
    with caplog.at_level(logging.WARNING, logger="eo.credits"):
        for _ in range(3):
            got = credits.credits_for("groq", "model-a", 10_000, 4_000, 1_000)
    # cached tokens cost the same as uncached ones: no invented discount
    assert got == pytest.approx(usd(10_000 * 0.15 + 1_000 * 0.75), abs=1e-6)
    assert len(caplog.records) == 1


# ---- the token convention and odd numbers -----------------------------

def test_input_includes_cached_so_uncached_is_the_difference():
    # 3,000 cached of 10,000: 7,000 uncached. Same as passing the split directly.
    a = credits.credits_for("groq", "model-a", 10_000, 3_000, 0)
    assert a == pytest.approx(usd(7_000 * 0.15 + 3_000 * 0.075), abs=1e-6)


def test_cached_greater_than_input_is_clamped_to_input():
    clamped = credits.credits_for("groq", "model-a", 1_000, 5_000, 0)
    all_cached = credits.credits_for("groq", "model-a", 1_000, 1_000, 0)
    assert clamped == all_cached == pytest.approx(usd(1_000 * 0.075), abs=1e-6)
    # never more than the prompt would cost with no cache at all
    assert clamped <= credits.credits_for("groq", "model-a", 1_000, 0, 0)


def test_none_values_count_as_zero():
    assert credits.credits_for("groq", "model-a", None, None, None) == 0.0
    assert credits.credits_for("groq", "model-a", None, 5_000, 1_000) == pytest.approx(
        usd(1_000 * 0.75), abs=1e-6)                       # cached > input(0) -> clamped to 0
    assert credits.credits_for("groq", "model-a", 2_000, None, None) == pytest.approx(
        usd(2_000 * 0.15), abs=1e-6)


def test_negative_values_count_as_zero():
    assert credits.credits_for("groq", "model-a", -10, -5, -1) == 0.0
    assert credits.credits_for("groq", "model-a", -10, 0, 1_000) == pytest.approx(
        usd(1_000 * 0.75), abs=1e-6)
    assert credits.credits_for("groq", "model-a", 1_000, -500, 0) == pytest.approx(
        usd(1_000 * 0.15), abs=1e-6)


@pytest.mark.parametrize("junk", ["abc", "", float("nan"), float("inf"), float("-inf"),
                                  object(), [1], {"a": 1}, True, 10 ** 400])
def test_garbage_token_values_never_raise_and_never_go_negative(junk):
    for args in [(junk, 0, 0), (1_000, junk, 0), (1_000, 0, junk), (junk, junk, junk)]:
        got = credits.credits_for("groq", "model-a", *args)
        assert isinstance(got, float) and got >= 0.0


def test_numeric_strings_and_floats_are_accepted():
    assert credits.credits_for("groq", "model-a", "2000", None, 0) == pytest.approx(
        usd(2_000 * 0.15), abs=1e-6)
    assert credits.credits_for("groq", "model-a", 2000.9, 0, 0) == credits.credits_for(
        "groq", "model-a", 2000, 0, 0)


@pytest.mark.parametrize("provider, model", [
    (None, None), ("", ""), (None, "model-a"), ("groq", None), (123, 456),
    (object(), object()), (["groq"], {"model-a": 1}),
])
def test_odd_provider_or_model_falls_back_instead_of_raising(provider, model):
    got = credits.credits_for(provider, model, 10_000, 0, 1_000)
    assert got == pytest.approx(usd(10_000 * 1.0 + 1_000 * 3.0), abs=1e-6)


def test_result_is_rounded_to_six_decimals_like_the_ledger():
    got = credits.credits_for("groq", "model-a", 12_345, 6_789, 321)
    assert got == round(got, 6)
    # 1 cached token at $0.075/M x 1000 credits/$ = 0.000075 credits
    assert credits.credits_for("groq", "model-a", 1, 1, 0) == pytest.approx(0.000075, abs=1e-9)
    # a sub-microcredit amount is rounded to the ledger's 6 decimals, not kept
    tiny = credits.credits_for("groq-extra", "model-e", 1, 1, 0)   # exactly 0.0000375 before rounding
    assert tiny == round(tiny, 6) and tiny == pytest.approx(0.0000375, abs=1e-6)
    assert credits.credits_for("groq-extra", "model-e", 0, 0, 0) == 0.0


def test_zero_everything_is_zero():
    assert credits.credits_for("groq", "model-a", 0, 0, 0) == 0.0


# ---- the 1-credit constant stays single-sourced -----------------------

def test_credit_size_is_read_from_plans_not_from_credits_json(monkeypatch):
    base = credits.credits_for("groq", "model-a", 10_000, 4_000, 1_000)
    monkeypatch.setattr(credits.plans, "credits_per_usd", lambda: 500.0)
    assert credits.credits_for("groq", "model-a", 10_000, 4_000, 1_000) == pytest.approx(
        base / 2, abs=1e-6)


def test_shipped_credits_json_does_not_duplicate_the_credit_constant():
    raw = json.loads(CREDITS_JSON.read_text(encoding="utf-8"))
    assert "credits_per_usd" not in raw
    assert "credits_per_usd" not in json.dumps(raw["models"])


# ---- fail-open ---------------------------------------------------------

def test_a_broken_plans_config_returns_zero_instead_of_raising(monkeypatch):
    def boom():
        raise KeyError("credits_per_usd")
    monkeypatch.setattr(credits.plans, "credits_per_usd", boom)
    assert credits.credits_for("groq", "model-a", 10_000, 0, 1_000) == 0.0


@pytest.mark.parametrize("bad_rate", [0, -5, float("nan"), float("inf"), None, "x"])
def test_a_nonsense_credit_rate_returns_zero_instead_of_raising(monkeypatch, bad_rate):
    monkeypatch.setattr(credits.plans, "credits_per_usd", lambda: bad_rate)
    assert credits.credits_for("groq", "model-a", 10_000, 0, 1_000) == 0.0


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"models": 5}', "null"])
def test_a_missing_or_corrupt_credits_json_still_prices_every_call(tmp_path, monkeypatch, content):
    p = tmp_path / "bad.json"
    p.write_text(content, encoding="utf-8")
    monkeypatch.setenv("CREDITS_CONFIG_PATH", str(p))
    credits.reload_config()
    got = credits.credits_for("groq", "model-a", 10_000, 4_000, 1_000)
    # Table model with no usable cached price: input/output from the table,
    # cached at the input price. Still a real, positive, finite number.
    assert got == pytest.approx(usd(10_000 * 0.15 + 1_000 * 0.75), abs=1e-6)


def test_a_missing_credits_json_file_still_prices_unknown_models(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDITS_CONFIG_PATH", str(tmp_path / "does-not-exist.json"))
    credits.reload_config()
    got = credits.credits_for("nobody", "mystery-1", 10_000, 0, 1_000)
    assert got == pytest.approx(usd(10_000 * 1.0 + 1_000 * 3.0), abs=1e-6)


def test_the_built_in_last_resort_default_matches_the_shipped_default():
    shipped = json.loads(CREDITS_JSON.read_text(encoding="utf-8"))["default_prices_usd_per_million"]
    assert credits._LAST_RESORT_DEFAULT == shipped


def test_an_unimportable_llm_client_falls_back_to_credits_json_only(monkeypatch, caplog):
    def boom():
        raise ImportError("llm_client unavailable")
    monkeypatch.setattr(credits, "_llm_price_table", boom)
    with caplog.at_level(logging.WARNING, logger="eo.credits"):
        full = credits.credits_for("groq-extra", "model-e", 10_000, 4_000, 1_000)
        table_model = credits.credits_for("groq", "model-a", 10_000, 4_000, 1_000)
    assert full == pytest.approx(usd(6_000 * 0.075 + 4_000 * 0.0375 + 1_000 * 0.30), abs=1e-6)
    assert table_model == pytest.approx(usd(10_000 * 1.0 + 1_000 * 3.0), abs=1e-6)  # default


def test_bad_entries_in_credits_json_are_dropped_not_fatal(tmp_path, monkeypatch):
    cfg = json.loads(json.dumps(FAKE_CREDITS))
    cfg["models"]["groq"]["model-a"] = {"cached_input": "cheap"}      # not a number
    cfg["models"]["mistral"]["model-b"] = {"cached_input": -1}        # negative
    cfg["models"]["gemini"] = "oops"                                  # not an object
    p = tmp_path / "dirty.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("CREDITS_CONFIG_PATH", str(p))
    credits.reload_config()
    # bad cached prices degrade to the input price; the good entry still works
    assert credits.credits_for("groq", "model-a", 1_000, 1_000, 0) == pytest.approx(
        usd(1_000 * 0.15), abs=1e-6)
    assert credits.credits_for("mistral", "model-b", 1_000, 1_000, 0) == pytest.approx(
        usd(1_000 * 0.40), abs=1e-6)
    assert credits.credits_for("cloudflare", "model-d", 1_000, 1_000, 0) == pytest.approx(
        usd(1_000 * 0.29), abs=1e-6)


# ---- the USD companion --------------------------------------------------

def test_cost_usd_for_is_the_dollar_side_of_the_same_calculation():
    usd_cost = credits.cost_usd_for("groq", "model-a", 50_000, 45_000, 500)
    assert usd_cost == pytest.approx(0.0045, abs=1e-8)
    assert credits.credits_for("groq", "model-a", 50_000, 45_000, 500) == pytest.approx(
        usd_cost * CREDITS_PER_USD, abs=1e-6)


def test_prices_for_reports_whether_the_model_is_known():
    known = credits.prices_for("groq", "model-a")
    assert (known.input, known.cached_input, known.output, known.known) == (0.15, 0.075, 0.75, True)
    unknown = credits.prices_for("nobody", "mystery-1")
    assert (unknown.input, unknown.cached_input, unknown.output, unknown.known) == (1.0, 1.0, 3.0, False)


# ---- import hygiene -----------------------------------------------------

def test_importing_credits_does_not_import_llm_client():
    """llm_client is read lazily so a future llm_client -> credits import can
    never become a cycle."""
    code = ("import sys; import eo.credits; "
            "sys.exit(1 if 'utils.llm_client' in sys.modules else 0)")
    proc = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_DIR,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]


# =====================================================================
# SHIPPED-DATA tests: the real config/credits.json against the real
# llm_client price table. Un-hermetic on purpose (they undo the fixture).
# =====================================================================

@pytest.fixture
def shipped(monkeypatch):
    monkeypatch.delenv("CREDITS_CONFIG_PATH", raising=False)
    monkeypatch.undo()          # restore the real llm_client table and plans
    credits.reload_config()
    yield json.loads(CREDITS_JSON.read_text(encoding="utf-8"))
    credits.reload_config()


def _all_priced(shipped_cfg):
    """[(provider, model, input, cached, output)] for every model the shipped
    data can price (llm_client table first, then credits.json-only entries)."""
    rows = []
    for provider, models in llm_client._TOKEN_PRICES_PER_MILLION.items():
        for model in models:
            p = credits.prices_for(provider, model)
            rows.append((provider, model, p.input, p.cached_input, p.output))
    for provider, models in shipped_cfg["models"].items():
        for model, entry in models.items():
            if provider in llm_client._TOKEN_PRICES_PER_MILLION and \
                    model in llm_client._TOKEN_PRICES_PER_MILLION[provider]:
                continue
            if {"input", "cached_input", "output"} <= entry.keys():
                rows.append((provider, model, entry["input"], entry["cached_input"], entry["output"]))
    return rows


def test_shipped_every_table_model_has_a_cached_price_in_credits_json(shipped):
    missing = [(prov, model)
               for prov, models in llm_client._TOKEN_PRICES_PER_MILLION.items()
               for model in models
               if "cached_input" not in shipped["models"].get(prov, {}).get(model, {})]
    assert missing == [], (
        f"add a cached_input price to config/credits.json for: {missing}")


def test_shipped_at_least_four_models_are_priced(shipped):
    assert len({(p, m) for p, m, *_ in _all_priced(shipped)}) >= 4


def test_shipped_a_cached_read_never_costs_more_than_an_uncached_one(shipped):
    for provider, model, inp, cached, _out in _all_priced(shipped):
        assert cached <= inp, f"{provider}/{model}: cached {cached} > input {inp}"


def test_shipped_default_is_at_least_as_expensive_as_every_priced_model(shipped):
    d = shipped["default_prices_usd_per_million"]
    for provider, model, inp, cached, out in _all_priced(shipped):
        assert d["input"] >= inp, f"default input < {provider}/{model}"
        assert d["cached_input"] >= cached, f"default cached_input < {provider}/{model}"
        assert d["output"] >= out, f"default output < {provider}/{model}"


def test_shipped_unknown_model_costs_at_least_the_priciest_known_one(shipped):
    pattern = (50_000, 45_000, 500)
    unknown = credits.credits_for("nobody", "mystery-1", *pattern)
    for provider, model, *_ in _all_priced(shipped):
        assert unknown >= credits.credits_for(provider, model, *pattern), f"{provider}/{model}"


def test_shipped_agent_pattern_on_a_real_model_is_cost_weighted(shipped):
    cached_call = credits.credits_for("groq", "openai/gpt-oss-120b", 50_000, 45_000, 500)
    cold_call = credits.credits_for("groq", "openai/gpt-oss-120b", 50_000, 0, 500)
    assert 0 < cached_call < cold_call
    assert cached_call < (50_000 + 500) / 1000          # not raw tokens at 1 credit per 1k


def test_shipped_free_model_is_free_and_not_reported_unknown(shipped, caplog):
    with caplog.at_level(logging.WARNING, logger="eo.credits"):
        assert credits.credits_for("openrouter", "openrouter/free", 1_000_000, 500_000, 1_000_000) == 0.0
    assert caplog.records == []


def test_shipped_credits_json_values_are_sane(shipped):
    d = shipped["default_prices_usd_per_million"]
    assert set(d) == {"input", "cached_input", "output"}
    for entry in [d] + [e for models in shipped["models"].values() for e in models.values()]:
        for key, value in entry.items():
            assert key in {"input", "cached_input", "output"}
            assert isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    assert plans.credits_per_usd() > 0
