"""
tests/integration/test_dynamic_chain_redundancy_gate.py — Phase 5 of the
reliability overhaul (see reliability_overhaul_plan.md §PHASE 5, Patch D).

Acceptance test for Patches A-C: replays the incident scenario Phase 5
exists to fix -- "most Groq keys in cooldown from the earlier cascade"
right before `technical_writer` ran, which used to leave
build_fallback_chain("technical_writer") returning a single-entry,
all-Groq chain (a crash waiting to happen the moment that one account
also failed) -- and asserts the fix holds end-to-end through the real
public API, not just at the helper-function level (that's what
test_dynamic_chain_rank_by_headroom.py / test_dynamic_chain_cross_org_
redundancy.py already cover in isolation).

Two scenarios:

  - test_build_fallback_chain_survives_groq_cascade_with_natural_spread:
    the plan's own acceptance line verbatim -- most Groq keys cooling
    down, default (dynamically-computed) max_steps, asserts the chain
    has >=1 non-Groq entry. This is what generic_worker.py-style
    provider spreading (Patch B's ranking riding on top of the
    existing loop) already gets you when there's room to spread in.

  - test_build_fallback_chain_forces_second_org_when_max_steps_caps_the_
    natural_spread: the sharper version of the same incident -- forces
    max_steps=1 (the actual shape of "the sole entry crashed" from the
    log: only one candidate ever got a chance) so Patch B's ranking
    alone can't reach a second provider. Patch C's post-pass is the
    only thing standing between this and the original bug; asserts it
    force-appends a genuine non-Groq entry rather than returning the
    single-entry chain that crashed.

  - test_build_fallback_chain_orders_equal_quota_candidates_by_live_
    headroom: Patch B's companion case, run through the real
    build_fallback_chain() entrypoint rather than the lower-level
    _rank_accounts()/_candidate_pool() helpers -- two candidates with
    identical daily quota but different mocked live headroom must come
    out of the PUBLIC chain in headroom order.
"""
import eo.dynamic_chain as dynamic_chain


AGENT_CAPABILITIES = {
    # 4 Groq accounts tagged for technical_writer -- the "most Groq keys
    # in cooldown" pool. GROQ_RESERVE_1 is the lone survivor in every
    # scenario below.
    "GROQ_API_KEY":   {"provider": "groq", "natural_roles": ["technical_writer"]},
    "GROQ_API_KEY_6": {"provider": "groq", "natural_roles": ["technical_writer"]},
    "GROQ_API_KEY_7": {"provider": "groq", "natural_roles": ["technical_writer"]},
    "GROQ_RESERVE_1": {"provider": "groq", "natural_roles": ["technical_writer"]},
    # Not natural-tagged for technical_writer -- only reachable via
    # _candidate_pool()'s full-pool fallback, same as any account that's
    # capable but not specially tagged for a role.
    "MISTRAL_API_KEY": {"provider": "mistral", "natural_roles": []},
}

# "Most Groq keys in cooldown": 3 of 4. GROQ_RESERVE_1 survives but
# with high daily usage, so it's still the worst-looking live
# candidate -- it should get picked because it's the only one left,
# not because it looks good.
QUOTA_STATUS = {
    "GROQ_API_KEY":     {"cooling_down": True,  "pct": 0.95},
    "GROQ_API_KEY_6":   {"cooling_down": True,  "pct": 0.90},
    "GROQ_API_KEY_7":   {"cooling_down": True,  "pct": 0.88},
    "GROQ_RESERVE_1":   {"cooling_down": False, "pct": 0.70},
    "MISTRAL_API_KEY":  {"cooling_down": False, "pct": 0.10},
}


def _patch_pool(monkeypatch):
    monkeypatch.setattr(dynamic_chain, "AGENT_CAPABILITIES", AGENT_CAPABILITIES)


def test_build_fallback_chain_survives_groq_cascade_with_natural_spread(monkeypatch):
    """The plan's own acceptance line: reproduce the log's pre-run state
    and confirm build_fallback_chain("technical_writer") returns a
    chain with at least one non-Groq entry -- not the single-entry
    all-Groq chain that used to crash the moment its lone survivor
    also failed."""
    _patch_pool(monkeypatch)

    chain = dynamic_chain.build_fallback_chain("technical_writer", quota_status=QUOTA_STATUS)

    assert len(chain) >= 2
    providers = [step["provider"] for step in chain]
    assert providers[0] == "groq"          # the lone surviving Groq account, still preferred
    assert any(p != "groq" for p in providers), (
        f"chain never left Groq: {chain!r} -- this is exactly the "
        f"single-org chain the incident log describes"
    )


def test_build_fallback_chain_forces_second_org_when_max_steps_caps_the_natural_spread(monkeypatch):
    """The sharper reproduction: max_steps=1 means _rank_accounts()'s
    own loop only gets ONE round -- the literal "single-entry chain"
    shape from the log, where the natural provider-spread loop never
    even got a chance to reach a second provider. Only Patch C's
    post-pass can save this one; if it's missing or broken, this chain
    comes back as ["groq"] and the test fails exactly the way the
    original incident did."""
    _patch_pool(monkeypatch)

    chain = dynamic_chain.build_fallback_chain(
        "technical_writer", quota_status=QUOTA_STATUS, max_steps=1,
    )

    assert len(chain) == 2, (
        f"expected _rank_accounts()'s 1-round chain to be topped up by "
        f"exactly one Patch-C-forced entry, got {chain!r}"
    )
    assert chain[0]["provider"] == "groq"
    assert chain[0]["key_env"] == "GROQ_RESERVE_1"
    assert chain[1]["provider"] == "mistral"
    assert chain[1]["key_env"] == "MISTRAL_API_KEY"


def test_build_fallback_chain_orders_equal_quota_candidates_by_live_headroom(monkeypatch):
    """Patch B's companion case, exercised through the real public
    entrypoint: two candidates with EQUAL daily quota but different
    live headroom must come back from build_fallback_chain() itself in
    headroom order, not just from the lower-level helpers directly."""
    capabilities = {
        "GROQ_A": {"provider": "groq", "natural_roles": ["technical_writer"]},
        "GROQ_B": {"provider": "groq", "natural_roles": ["technical_writer"]},
        "MISTRAL_A": {"provider": "mistral", "natural_roles": ["technical_writer"]},
    }
    quota_status = {
        "GROQ_A": {"cooling_down": False, "pct": 0.4},
        "GROQ_B": {"cooling_down": False, "pct": 0.4},  # identical daily quota to GROQ_A
        "MISTRAL_A": {"cooling_down": False, "pct": 0.4},
    }
    monkeypatch.setattr(dynamic_chain, "AGENT_CAPABILITIES", capabilities)

    headroom_scores = {"GROQ_A": 0.9, "GROQ_B": 0.1, "MISTRAL_A": 0.5}
    monkeypatch.setattr(dynamic_chain, "_headroom_score", lambda key: headroom_scores[key])

    chain = dynamic_chain.build_fallback_chain("technical_writer", quota_status=quota_status)

    key_envs = [step["key_env"] for step in chain]
    # GROQ_B has the lowest (most-open) headroom score of the two
    # Groq candidates despite tying with GROQ_A on daily quota -- it
    # must be picked over GROQ_A on the strength of the live signal
    # alone.
    assert key_envs[0] == "GROQ_B"
    assert "GROQ_A" not in key_envs[:1]
