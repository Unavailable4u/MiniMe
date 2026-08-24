"""
tests/unit/test_dynamic_chain_headroom_score.py — Phase 5 of the
reliability overhaul (see reliability_overhaul_plan.md §PHASE 5, Patch A).

Covers eo/dynamic_chain.py's _headroom_score() in isolation: the pure
read helper that will become _rank_accounts()'s live-headroom signal in
Patch B. No chain-building behavior is exercised here (that's Patch B's
own test) -- just that this function reads rate_ledger.headroom_snapshot()
correctly and applies the right precedence/fail-open rules.
"""
from eo import dynamic_chain


def _snapshot(gating_mode="tokens", provider_reported=None,
              sliding_window=None, daily=None):
    return {
        "gating_mode": gating_mode,
        "provider_reported": provider_reported,
        "sliding_window": sliding_window or {"used_last_60s": 0, "tpm_limit": None, "pct_used": None},
        "daily": daily,
    }


def _patch_pool(monkeypatch, capabilities):
    monkeypatch.setattr(dynamic_chain, "AGENT_CAPABILITIES", capabilities)


def test_unknown_key_reads_as_fully_open(monkeypatch):
    _patch_pool(monkeypatch, {})
    assert dynamic_chain._headroom_score("NOT_A_REAL_KEY") == 0.0


def test_no_provider_in_capabilities_reads_as_fully_open(monkeypatch):
    _patch_pool(monkeypatch, {"WEIRD_KEY": {}})
    assert dynamic_chain._headroom_score("WEIRD_KEY") == 0.0


def test_prefers_provider_reported_zero_remaining_over_window_pct(monkeypatch):
    """A live provider response saying 0 remaining is the strongest
    signal available and should short-circuit to 1.0 even if the
    self-tracked window would otherwise say there's still headroom."""
    _patch_pool(monkeypatch, {"GROQ_KEY_1": {"provider": "groq"}})

    import utils.rate_ledger as real_rate_ledger

    def fake_snapshot(provider, key_id, model):
        assert provider == "groq"
        assert key_id == "GROQ_KEY_1"
        return _snapshot(
            gating_mode="tokens",
            provider_reported={"remaining_tokens": 0, "remaining_requests": None,
                                "reset_at": None, "recorded_at": None},
            sliding_window={"used_last_60s": 10, "tpm_limit": 10000, "pct_used": 0.001},
        )

    monkeypatch.setattr(real_rate_ledger, "headroom_snapshot", fake_snapshot)
    assert dynamic_chain._headroom_score("GROQ_KEY_1") == 1.0


def test_provider_reported_nonzero_remaining_falls_through_to_window_pct(monkeypatch):
    _patch_pool(monkeypatch, {"GROQ_KEY_1": {"provider": "groq"}})

    import utils.rate_ledger as real_rate_ledger

    def fake_snapshot(provider, key_id, model):
        return _snapshot(
            gating_mode="tokens",
            provider_reported={"remaining_tokens": 500, "remaining_requests": None,
                                "reset_at": None, "recorded_at": None},
            sliding_window={"used_last_60s": 6000, "tpm_limit": 10000, "pct_used": 0.6},
        )

    monkeypatch.setattr(real_rate_ledger, "headroom_snapshot", fake_snapshot)
    assert dynamic_chain._headroom_score("GROQ_KEY_1") == 0.6


def test_requests_mode_checks_remaining_requests_not_remaining_tokens(monkeypatch):
    _patch_pool(monkeypatch, {"OPENROUTER_KEY_1": {"provider": "openrouter"}})

    import utils.rate_ledger as real_rate_ledger

    def fake_snapshot(provider, key_id, model):
        return _snapshot(
            gating_mode="requests",
            provider_reported={"remaining_tokens": None, "remaining_requests": 0,
                                "reset_at": None, "recorded_at": None},
            sliding_window={"used_last_60s": 3, "tpm_limit": 20, "pct_used": 0.15},
        )

    monkeypatch.setattr(real_rate_ledger, "headroom_snapshot", fake_snapshot)
    assert dynamic_chain._headroom_score("OPENROUTER_KEY_1") == 1.0


def test_no_provider_reported_uses_window_pct(monkeypatch):
    _patch_pool(monkeypatch, {"GROQ_KEY_1": {"provider": "groq"}})

    import utils.rate_ledger as real_rate_ledger

    def fake_snapshot(provider, key_id, model):
        return _snapshot(
            gating_mode="tokens",
            provider_reported=None,
            sliding_window={"used_last_60s": 2500, "tpm_limit": 10000, "pct_used": 0.25},
        )

    monkeypatch.setattr(real_rate_ledger, "headroom_snapshot", fake_snapshot)
    assert dynamic_chain._headroom_score("GROQ_KEY_1") == 0.25


def test_requests_mode_falls_back_to_daily_pct_when_window_pct_missing(monkeypatch):
    """rpm_limit unset (window_limit None -> pct_used None) but rpd_limit
    set -- daily.pct_used should be used instead of defaulting to 0.0."""
    _patch_pool(monkeypatch, {"OPENROUTER_KEY_1": {"provider": "openrouter"}})

    import utils.rate_ledger as real_rate_ledger

    def fake_snapshot(provider, key_id, model):
        return _snapshot(
            gating_mode="requests",
            provider_reported=None,
            sliding_window={"used_last_60s": 3, "tpm_limit": None, "pct_used": None},
            daily={"used_today": 40, "rpd_limit": 50, "pct_used": 0.8},
        )

    monkeypatch.setattr(real_rate_ledger, "headroom_snapshot", fake_snapshot)
    assert dynamic_chain._headroom_score("OPENROUTER_KEY_1") == 0.8


def test_no_usable_signal_at_all_reads_as_fully_open(monkeypatch):
    """No verified QUOTA_CONFIG limit for this provider/model (both
    pct_used fields None) and no provider-reported data -- fail open,
    same posture as the rest of rate_ledger.py."""
    _patch_pool(monkeypatch, {"MISTRAL_KEY_1": {"provider": "mistral"}})

    import utils.rate_ledger as real_rate_ledger

    def fake_snapshot(provider, key_id, model):
        return _snapshot(gating_mode="tokens", provider_reported=None,
                          sliding_window={"used_last_60s": 0, "tpm_limit": None, "pct_used": None})

    monkeypatch.setattr(real_rate_ledger, "headroom_snapshot", fake_snapshot)
    assert dynamic_chain._headroom_score("MISTRAL_KEY_1") == 0.0


def test_cloudflare_key_id_derived_from_account_id_env(monkeypatch):
    """Cloudflare accounts key the ledger on their own account_id_env
    (info['key_id']), not the AGENT_CAPABILITIES key itself -- same
    convention chain_step_for()/_candidate_identity() already use."""
    _patch_pool(monkeypatch, {
        "CF_SCANNER_1": {"provider": "cloudflare", "key_id": "CLOUDFLARE_ACCOUNT_ID_1"},
    })

    import utils.rate_ledger as real_rate_ledger
    seen = {}

    def fake_snapshot(provider, key_id, model):
        seen["provider"] = provider
        seen["key_id"] = key_id
        return _snapshot(gating_mode="tokens", provider_reported=None,
                          sliding_window={"used_last_60s": 0, "tpm_limit": None, "pct_used": None})

    monkeypatch.setattr(real_rate_ledger, "headroom_snapshot", fake_snapshot)
    dynamic_chain._headroom_score("CF_SCANNER_1")
    assert seen == {"provider": "cloudflare", "key_id": "CLOUDFLARE_ACCOUNT_ID_1"}
