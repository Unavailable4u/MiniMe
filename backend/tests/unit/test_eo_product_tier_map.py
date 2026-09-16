"""
tests/unit/test_eo_product_tier_map.py

Coverage check: every role eo/registry.py actually knows about (its
AGENT_CAPABILITIES natural_roles tags, plus its REAL_ACTION_ROLES
keys) has a row in eo/product_tier_map.py's AGENT_PRODUCT_TIER_MAP,
and vice versa. This is the test that module's own docstring points
at re-running after adding a role to (or retiring one from) either of
those -- it's what keeps this table from silently drifting out of
sync with the live agent roster, the same failure mode Roadmap Part
3.2's own "why this matters" section warns three hand-maintained
tier forks would fall into at a larger scale.

Also asserts the skeleton's own documented invariants: Basic is fully
populated with the INHERIT sentinel (not real numbers -- see that
module's docstring point 3), and Moderate/Ultimate are still `None`
placeholders (Part 3.3's empirical testing hasn't run yet for
anything).
"""
import pytest

from eo import product_tier_map
from eo.registry import AGENT_CAPABILITIES, REAL_ACTION_ROLES


def _live_role_vocabulary():
    roles = set(REAL_ACTION_ROLES.keys())
    for info in AGENT_CAPABILITIES.values():
        roles.update(info.get("natural_roles", []))
    return roles


def test_every_live_role_has_a_row():
    missing = _live_role_vocabulary() - set(product_tier_map.AGENT_PRODUCT_TIER_MAP)
    assert not missing, f"roles missing from AGENT_PRODUCT_TIER_MAP: {sorted(missing)}"


def test_no_stale_rows_for_retired_roles():
    stale = set(product_tier_map.AGENT_PRODUCT_TIER_MAP) - _live_role_vocabulary()
    assert not stale, f"rows in AGENT_PRODUCT_TIER_MAP for roles no longer live: {sorted(stale)}"


def test_basic_column_fully_populated_with_inherit_sentinel():
    for role, columns in product_tier_map.AGENT_PRODUCT_TIER_MAP.items():
        basic = columns["basic"]
        assert basic is not None, f"{role}'s basic column must not be None"
        assert all(v == product_tier_map.INHERIT for v in basic.values()), (
            f"{role}'s basic column has a non-INHERIT value -- Part 3.2 says Basic "
            "describes today's live behavior unchanged; a real number here should "
            "come from Checklist item 1's cost-per-task logging, not be hand-typed"
        )


def test_moderate_and_ultimate_columns_are_still_placeholders():
    for role, columns in product_tier_map.AGENT_PRODUCT_TIER_MAP.items():
        assert columns["moderate"] is None, f"{role}'s moderate column should still be a placeholder"
        assert columns["ultimate"] is None, f"{role}'s ultimate column should still be a placeholder"


def test_basic_rows_are_independent_dicts_not_a_shared_reference():
    # Regression guard: a naive `{"basic": _basic_row()} for role in (...)`-
    # adjacent implementation could accidentally share one dict across every
    # role if `_basic_row()` were called once outside the comprehension.
    # Mutating one role's basic cell must never leak into another's.
    #
    # Exercised against fresh calls to the private row factory, not by
    # mutating the live module-level AGENT_PRODUCT_TIER_MAP in place --
    # that dict is a shared singleton other tests in this file (and any
    # future caller) read, so mutating one of its real cells here would
    # leak into whichever test happens to run afterward.
    first = product_tier_map._basic_row()
    second = product_tier_map._basic_row()
    assert first is not second
    first["model_class"] = "mutated-for-test"
    assert second["model_class"] == product_tier_map.INHERIT

    writer_basic = product_tier_map.AGENT_PRODUCT_TIER_MAP["writer"]["basic"]
    editor_basic = product_tier_map.AGENT_PRODUCT_TIER_MAP["editor"]["basic"]
    assert writer_basic is not editor_basic


def test_get_agent_product_tier_profile_rejects_unknown_product_tier():
    with pytest.raises(ValueError):
        product_tier_map.get_agent_product_tier_profile("writer", product_tier="premium")


def test_get_agent_product_tier_profile_rejects_unknown_role():
    with pytest.raises(KeyError):
        product_tier_map.get_agent_product_tier_profile("not_a_real_role")


def test_get_agent_product_tier_profile_returns_none_for_unpopulated_tier():
    assert product_tier_map.get_agent_product_tier_profile("writer", product_tier="moderate") is None
    assert product_tier_map.get_agent_product_tier_profile("writer", product_tier="ultimate") is None


def test_get_agent_product_tier_profile_returns_basic_row_by_default():
    profile = product_tier_map.get_agent_product_tier_profile("writer")
    assert profile["model_class"] == product_tier_map.INHERIT
