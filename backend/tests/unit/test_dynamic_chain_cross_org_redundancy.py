"""
tests/unit/test_dynamic_chain_cross_org_redundancy.py — Phase 5 of the
reliability overhaul (see reliability_overhaul_plan.md §PHASE 5, Patch C).

Covers eo/dynamic_chain.py's new _org_for() / _ensure_cross_org_redundancy()
post-pass: guaranteeing at least 2 chain entries from genuinely distinct
orgs whenever the pool allows it, and logging a loud
`chain_redundancy_gap` warning when it genuinely can't. The full
"most Groq keys in cooldown" acceptance scenario (Patch B + Patch C
together, against build_fallback_chain() end to end) is Patch D's job,
not this file's -- this file exercises _org_for()/
_ensure_cross_org_redundancy() directly and in isolation.
"""
import eo.dynamic_chain as dynamic_chain


def _patch_pool(monkeypatch, capabilities):
    monkeypatch.setattr(dynamic_chain, "AGENT_CAPABILITIES", capabilities)


def _patch_no_cooldowns(monkeypatch):
    import eo.panel as panel
    monkeypatch.setattr(panel, "_is_cooling_down", lambda key, quota_status: False)


def _patch_flat_headroom_and_usage(monkeypatch):
    """No preference among candidates -- isolates the redundancy logic
    from Patch B's ranking so ordering doesn't interfere with these
    assertions."""
    import eo.panel as panel
    monkeypatch.setattr(dynamic_chain, "_headroom_score", lambda key: 0.0)
    monkeypatch.setattr(panel, "_usage_fraction", lambda key_env, quota_status: 0.0)


# ---------------------------------------------------------------------------
# _org_for()
# ---------------------------------------------------------------------------

def test_org_for_openrouter_collapses_every_key_to_one_org():
    assert dynamic_chain._org_for("openrouter", "OPENROUTER_API_KEY_1") == "openrouter"
    assert dynamic_chain._org_for("openrouter", "OPENROUTER_API_KEY_2") == "openrouter"
    assert (dynamic_chain._org_for("openrouter", "OPENROUTER_API_KEY_1")
            == dynamic_chain._org_for("openrouter", "OPENROUTER_RESERVE_1"))


def test_org_for_other_providers_defaults_to_the_key_itself():
    assert dynamic_chain._org_for("groq", "GROQ_API_KEY_6") == "GROQ_API_KEY_6"
    assert dynamic_chain._org_for("cerebras", "CEREBRAS_API_KEY_1") == "CEREBRAS_API_KEY_1"
    # Two different Groq keys must NOT collapse to the same org --
    # only OpenRouter gets that treatment (pending OR-0).
    assert (dynamic_chain._org_for("groq", "GROQ_API_KEY_6")
            != dynamic_chain._org_for("groq", "GROQ_API_KEY_7"))


# ---------------------------------------------------------------------------
# _ensure_cross_org_redundancy()
# ---------------------------------------------------------------------------

def test_chain_already_spanning_two_orgs_is_left_untouched(monkeypatch):
    _patch_pool(monkeypatch, {
        "GROQ_A": {"provider": "groq"},
        "MISTRAL_A": {"provider": "mistral"},
    })
    chain = dynamic_chain._ensure_cross_org_redundancy(
        "technical_writer", ["GROQ_A", "MISTRAL_A"], quota_status={}, start_exclude=set(),
    )
    assert chain == ["GROQ_A", "MISTRAL_A"]


def test_empty_chain_is_left_untouched_no_log(monkeypatch, caplog):
    _patch_pool(monkeypatch, {"GROQ_A": {"provider": "groq"}})
    with caplog.at_level("WARNING", logger="eo.dynamic_chain"):
        chain = dynamic_chain._ensure_cross_org_redundancy(
            "technical_writer", [], quota_status={}, start_exclude=set(),
        )
    assert chain == []
    assert "chain_redundancy_gap" not in caplog.text


def test_single_entry_chain_force_appends_second_org_candidate(monkeypatch):
    """The Phase 5 log's actual scenario in miniature: chain came out of
    _rank_accounts() with just one Groq entry, but a non-Groq candidate
    (MISTRAL_A) exists elsewhere in the pool and isn't excluded/cooling
    -- it must be force-appended, even past whatever max_steps the
    caller originally used."""
    _patch_pool(monkeypatch, {
        "GROQ_A": {"provider": "groq", "natural_roles": ["technical_writer"]},
        "MISTRAL_A": {"provider": "mistral", "natural_roles": []},
    })
    _patch_no_cooldowns(monkeypatch)
    _patch_flat_headroom_and_usage(monkeypatch)

    chain = dynamic_chain._ensure_cross_org_redundancy(
        "technical_writer", ["GROQ_A"], quota_status={}, start_exclude=set(),
    )
    assert chain == ["GROQ_A", "MISTRAL_A"]


def test_multiple_openrouter_only_entries_still_count_as_one_org_and_get_topped_up(monkeypatch):
    """Two OPENROUTER_* keys in the chain (2 entries) must NOT read as
    "already redundant" -- _org_for() collapses them to one org, so
    this is exactly the gap the OR-0/OR-5 ASSUMPTION FLAGGED note
    exists to catch."""
    _patch_pool(monkeypatch, {
        "OPENROUTER_API_KEY_1": {"provider": "openrouter", "natural_roles": ["implementer"]},
        "OPENROUTER_API_KEY_2": {"provider": "openrouter", "natural_roles": ["implementer"]},
        "MISTRAL_A": {"provider": "mistral", "natural_roles": []},
    })
    _patch_no_cooldowns(monkeypatch)
    _patch_flat_headroom_and_usage(monkeypatch)

    chain = dynamic_chain._ensure_cross_org_redundancy(
        "implementer", ["OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2"],
        quota_status={}, start_exclude=set(),
    )
    assert chain == ["OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2", "MISTRAL_A"]


def test_no_second_org_candidate_anywhere_logs_and_returns_chain_unchanged(monkeypatch, caplog):
    """Every other org is excluded/cooling/not provisioned for this
    role -- the chain is returned as-is (not silently dropped, not
    crashed on), and a chain_redundancy_gap warning is logged."""
    _patch_pool(monkeypatch, {
        "GROQ_A": {"provider": "groq", "natural_roles": ["technical_writer"]},
        "GROQ_B": {"provider": "groq", "natural_roles": ["technical_writer"]},
    })
    _patch_no_cooldowns(monkeypatch)
    _patch_flat_headroom_and_usage(monkeypatch)

    with caplog.at_level("WARNING", logger="eo.dynamic_chain"):
        chain = dynamic_chain._ensure_cross_org_redundancy(
            "technical_writer", ["GROQ_A"], quota_status={}, start_exclude={"GROQ_B"},
        )
    assert chain == ["GROQ_A"]  # unchanged, not silently mutated or emptied
    assert "chain_redundancy_gap" in caplog.text
    assert "technical_writer" in caplog.text


def test_start_exclude_is_respected_when_searching_for_a_second_org(monkeypatch):
    """A candidate that's in start_exclude (e.g. a sibling worker
    thread's already-claimed account, per build_fallback_chain_excluding())
    must not be force-appended even if it belongs to a different org."""
    _patch_pool(monkeypatch, {
        "GROQ_A": {"provider": "groq", "natural_roles": ["technical_writer"]},
        "MISTRAL_A": {"provider": "mistral", "natural_roles": []},
    })
    _patch_no_cooldowns(monkeypatch)
    _patch_flat_headroom_and_usage(monkeypatch)

    chain = dynamic_chain._ensure_cross_org_redundancy(
        "technical_writer", ["GROQ_A"], quota_status={}, start_exclude={"MISTRAL_A"},
    )
    assert chain == ["GROQ_A"]  # no candidate left to top up with


def test_forced_second_org_candidate_can_exceed_max_steps(monkeypatch):
    """build_fallback_chain()'s own max_steps cap is a round-count
    heuristic for _rank_accounts()'s loop, not a hard ceiling on the
    final chain -- the redundancy guarantee is allowed to push one
    entry past it. Two OPENROUTER_* keys collapse to ONE org under
    _org_for(), so this two-entry chain still needs topping up --
    unlike two distinct Groq keys, which _org_for() already counts as
    two separate orgs (see test_org_for_other_providers_defaults_to_
    the_key_itself above)."""
    _patch_pool(monkeypatch, {
        "OPENROUTER_API_KEY_1": {"provider": "openrouter", "natural_roles": ["technical_writer"]},
        "OPENROUTER_API_KEY_2": {"provider": "openrouter", "natural_roles": ["technical_writer"]},
        "MISTRAL_A": {"provider": "mistral", "natural_roles": []},
    })
    _patch_no_cooldowns(monkeypatch)
    _patch_flat_headroom_and_usage(monkeypatch)

    # Simulate a _rank_accounts() result that already used up a
    # max_steps=2 budget on two same-org (per _org_for()) entries.
    chain = dynamic_chain._ensure_cross_org_redundancy(
        "technical_writer", ["OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2"],
        quota_status={}, start_exclude=set(),
    )
    assert chain == ["OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2", "MISTRAL_A"]
