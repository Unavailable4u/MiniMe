"""
tests/unit/test_dynamic_chain_rank_by_headroom.py — Phase 5 of the
reliability overhaul (see reliability_overhaul_plan.md §PHASE 5, Patch B).

Covers eo/dynamic_chain.py's new _rank_by_live_headroom() / _candidate_pool()
/ _rank_accounts() behavior: candidates are now ordered by live headroom
(Patch A's _headroom_score(), ascending) first, daily quota fraction
second, instead of delegating straight to eo/panel.py's _best_match().
Patch A's own precedence/fail-open rules inside _headroom_score() are
already covered by test_dynamic_chain_headroom_score.py and are not
re-tested here -- this file only exercises the NEW ordering layer on
top of it. Provider-spreading/cooldown/exclude behavior is unchanged
from pre-Patch-B and is covered by Patch D's acceptance test alongside
Patch C, not here.
"""
from eo import dynamic_chain


def _patch_pool(monkeypatch, capabilities):
    monkeypatch.setattr(dynamic_chain, "AGENT_CAPABILITIES", capabilities)


def _patch_headroom(monkeypatch, scores: dict):
    """scores: {agent_key: headroom_score}. Any key not in the dict
    raises, so a test can't accidentally rely on the real
    rate_ledger-backed implementation by mistake."""
    def fake_headroom_score(key):
        return scores[key]
    monkeypatch.setattr(dynamic_chain, "_headroom_score", fake_headroom_score)


def _patch_usage_fraction(monkeypatch, fractions: dict):
    from eo import panel

    def fake_usage_fraction(key_env, quota_status):
        return fractions.get(key_env, 0.0)
    monkeypatch.setattr(panel, "_usage_fraction", fake_usage_fraction)


def _patch_no_cooldowns(monkeypatch):
    from eo import panel
    monkeypatch.setattr(panel, "_is_cooling_down", lambda key, quota_status: False)


def test_orders_by_live_headroom_ascending_as_primary_signal(monkeypatch):
    """Two candidates with EQUAL daily quota but different live headroom
    must be ordered by the live signal -- the exact companion case
    Patch D's plan text calls out for Patch B."""
    _patch_pool(monkeypatch, {
        "GROQ_A": {"provider": "groq", "natural_roles": ["technical_writer"]},
        "GROQ_B": {"provider": "groq", "natural_roles": ["technical_writer"]},
    })
    _patch_no_cooldowns(monkeypatch)
    # Equal daily quota fraction for both -- headroom must be the
    # deciding factor, not a coincidental tiebreak.
    _patch_usage_fraction(monkeypatch, {"GROQ_A": 0.5, "GROQ_B": 0.5})
    _patch_headroom(monkeypatch, {"GROQ_A": 0.9, "GROQ_B": 0.1})

    pool = dynamic_chain._candidate_pool("technical_writer", quota_status={}, exclude=set())
    assert pool[0] == "GROQ_B"  # lower live headroom score (more open) wins
    assert pool[1] == "GROQ_A"


def test_falls_back_to_daily_quota_when_headroom_scores_tie(monkeypatch):
    """A fresh account with nothing recorded yet (headroom 0.0, same as
    any other unused account) must still be distinguishable via the
    daily-quota tiebreaker rather than ordering arbitrarily."""
    _patch_pool(monkeypatch, {
        "GROQ_A": {"provider": "groq", "natural_roles": ["technical_writer"]},
        "GROQ_B": {"provider": "groq", "natural_roles": ["technical_writer"]},
    })
    _patch_no_cooldowns(monkeypatch)
    _patch_headroom(monkeypatch, {"GROQ_A": 0.0, "GROQ_B": 0.0})
    _patch_usage_fraction(monkeypatch, {"GROQ_A": 0.7, "GROQ_B": 0.2})

    pool = dynamic_chain._candidate_pool("technical_writer", quota_status={}, exclude=set())
    assert pool[0] == "GROQ_B"  # lower daily-quota fraction wins the tie
    assert pool[1] == "GROQ_A"


def test_no_natural_role_match_falls_back_to_full_pool_ranked_by_headroom(monkeypatch):
    _patch_pool(monkeypatch, {
        "GROQ_A": {"provider": "groq", "natural_roles": ["idea_planner"]},
        "MISTRAL_A": {"provider": "mistral", "natural_roles": []},
    })
    _patch_no_cooldowns(monkeypatch)
    _patch_usage_fraction(monkeypatch, {})
    _patch_headroom(monkeypatch, {"GROQ_A": 0.4, "MISTRAL_A": 0.1})

    # Nothing is tagged for "technical_writer" -- both accounts are
    # eligible via the full-pool fallback, ranked by headroom.
    pool = dynamic_chain._candidate_pool("technical_writer", quota_status={}, exclude=set())
    assert pool[0] == "MISTRAL_A"
    assert "GROQ_A" in pool


def test_rank_accounts_still_spreads_across_providers(monkeypatch):
    """_rank_accounts()'s provider-spreading loop structure must be
    untouched by Patch B -- confirms the new pool/ranking layer slots
    into the existing loop without breaking the one-account-per-
    provider-per-round preference."""
    _patch_pool(monkeypatch, {
        "GROQ_A": {"provider": "groq", "natural_roles": ["technical_writer"]},
        "GROQ_B": {"provider": "groq", "natural_roles": ["technical_writer"]},
        "MISTRAL_A": {"provider": "mistral", "natural_roles": ["technical_writer"]},
    })
    _patch_no_cooldowns(monkeypatch)
    _patch_usage_fraction(monkeypatch, {})
    # GROQ_A has the best (most-open) headroom of all three, so it must
    # be picked first; the second step should still prefer a fresh
    # provider (MISTRAL_A) over the also-open GROQ_B.
    _patch_headroom(monkeypatch, {"GROQ_A": 0.0, "GROQ_B": 0.05, "MISTRAL_A": 0.2})

    chain = dynamic_chain._rank_accounts(
        "technical_writer", quota_status={}, exclude=set(), max_steps=2,
    )
    assert chain == ["GROQ_A", "MISTRAL_A"]
