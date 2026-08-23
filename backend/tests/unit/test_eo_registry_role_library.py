"""
tests/unit/test_eo_registry_role_library.py

Unit coverage for the role-library / roster-bookkeeping half of
eo/registry.py: _role_prompts_key()'s per-user scoping decision,
update_role_prompt(), record_role_hire(), set_role_pinned(),
list_known_roles(), and list_role_metadata(). These sit directly
upstream of resolve_role()/REGISTRY in the same module and back the
Role Library UI's roster view (who's been hired, how often, what's
pinned) -- a coverage run showed every one of them had gaps or, in
set_role_pinned()'s case, zero coverage at all.

Uses the autouse `fake_bus` fixture (tests/conftest.py) so read/write
hit an in-memory FakeRedis, and the autouse `_reset_role_prompts_cache`
fixture so each test starts with a cold per-run cache -- both already
apply to every test under tests/unit without any per-file setup here.
"""
import pytest

import eo.registry as registry_module
from eo.registry import (
    _role_prompts_key,
    get_role_prompt,
    get_role_metadata,
    add_role_prompt,
    update_role_prompt,
    record_role_hire,
    set_role_pinned,
    list_known_roles,
    list_role_metadata,
    ROLE_PROMPTS_KEY,
)


# ---------------------------------------------------------------------------
# _role_prompts_key() -- global vs. per-user scoping
# ---------------------------------------------------------------------------

def test_default_global_scope_ignores_user_id():
    assert registry_module.ROLE_LIBRARY_SCOPE == "global"
    assert _role_prompts_key() == ROLE_PROMPTS_KEY
    assert _role_prompts_key(user_id="alice") == ROLE_PROMPTS_KEY


def test_per_user_scope_without_a_user_id_raises(monkeypatch):
    # A silent fallback to the shared key here would leak one caller's
    # read/write into every other user's library -- this must raise,
    # not degrade quietly.
    monkeypatch.setattr(registry_module, "ROLE_LIBRARY_SCOPE", "per_user")
    with pytest.raises(ValueError, match="per_user"):
        _role_prompts_key()
    with pytest.raises(ValueError):
        _role_prompts_key(user_id=None)


def test_per_user_scope_with_a_user_id_returns_a_namespaced_key(monkeypatch):
    monkeypatch.setattr(registry_module, "ROLE_LIBRARY_SCOPE", "per_user")
    assert _role_prompts_key(user_id="alice") == f"{ROLE_PROMPTS_KEY}:alice"
    assert _role_prompts_key(user_id="bob") == f"{ROLE_PROMPTS_KEY}:bob"


def test_per_user_scope_isolates_separate_users_stores(monkeypatch):
    monkeypatch.setattr(registry_module, "ROLE_LIBRARY_SCOPE", "per_user")
    add_role_prompt("brainstormer", "alice's version", user_id="alice")
    add_role_prompt("brainstormer", "bob's version", user_id="bob")
    assert get_role_prompt("brainstormer", user_id="alice") == "alice's version"
    assert get_role_prompt("brainstormer", user_id="bob") == "bob's version"


# ---------------------------------------------------------------------------
# update_role_prompt() -- thin wrapper over add_role_prompt()
# ---------------------------------------------------------------------------

def test_update_role_prompt_sets_source_to_user_edited_by_default():
    update_role_prompt("brainstormer", "a human wrote this brief")
    meta = get_role_metadata("brainstormer")
    assert meta["brief"] == "a human wrote this brief"
    assert meta["source"] == "user_edited"
    assert meta["updated_at"] is not None


def test_update_role_prompt_preserves_times_hired_and_pin_state():
    add_role_prompt("brainstormer", "first brief")
    record_role_hire("brainstormer")
    record_role_hire("brainstormer")
    set_role_pinned("brainstormer", True)

    update_role_prompt("brainstormer", "edited brief")

    meta = get_role_metadata("brainstormer")
    assert meta["brief"] == "edited brief"
    assert meta["times_hired"] == 2
    assert meta["pinned"] is True


# ---------------------------------------------------------------------------
# record_role_hire()
# ---------------------------------------------------------------------------

def test_record_role_hire_increments_an_existing_entry():
    add_role_prompt("verifier_helper", "brief")
    record_role_hire("verifier_helper")
    record_role_hire("verifier_helper")
    record_role_hire("verifier_helper")
    assert get_role_metadata("verifier_helper")["times_hired"] == 3


def test_record_role_hire_creates_a_bare_entry_for_an_unbriefed_role():
    # A hire can in principle race a first-ever brief write -- must not
    # raise for a role with no existing entry.
    assert get_role_metadata("brand_new_role") is None
    record_role_hire("brand_new_role")
    meta = get_role_metadata("brand_new_role")
    assert meta["times_hired"] == 1
    assert meta["brief"] is None
    assert meta["pinned"] is False


# ---------------------------------------------------------------------------
# set_role_pinned()
# ---------------------------------------------------------------------------

def test_set_role_pinned_true_sets_pinned_and_pinned_at():
    add_role_prompt("outliner", "brief")
    entry = set_role_pinned("outliner", True)
    assert entry["pinned"] is True
    assert entry["pinned_at"] is not None
    # Returned entry reflects the persisted state.
    assert get_role_metadata("outliner")["pinned"] is True


def test_set_role_pinned_false_clears_pinned_at():
    add_role_prompt("outliner", "brief")
    set_role_pinned("outliner", True)
    entry = set_role_pinned("outliner", False)
    assert entry["pinned"] is False
    assert entry["pinned_at"] is None


def test_set_role_pinned_creates_a_bare_entry_for_an_unbriefed_role():
    # A role can be pinned from a picker before it's ever been
    # hired/briefed.
    assert get_role_metadata("never_briefed_role") is None
    entry = set_role_pinned("never_briefed_role", True)
    assert entry["brief"] is None
    assert entry["pinned"] is True
    assert get_role_metadata("never_briefed_role")["pinned"] is True


def test_set_role_pinned_coerces_a_truthy_non_bool_value():
    add_role_prompt("editor", "brief")
    entry = set_role_pinned("editor", 1)  # truthy, not a real bool
    assert entry["pinned"] is True


# ---------------------------------------------------------------------------
# list_known_roles() / list_role_metadata()
# ---------------------------------------------------------------------------

def test_list_known_roles_returns_a_sorted_list_of_role_names():
    add_role_prompt("zeta_role", "z")
    add_role_prompt("alpha_role", "a")
    roles = list_known_roles()
    assert "zeta_role" in roles
    assert "alpha_role" in roles
    assert roles == sorted(roles)


def test_list_known_roles_includes_seeded_roles_on_first_touch():
    # _load_prompts() bootstraps from ROLE_PROMPTS_SEED on the very
    # first call for a fresh store.
    roles = list_known_roles()
    assert len(roles) > 0
    assert set(registry_module.ROLE_PROMPTS_SEED.keys()).issubset(set(roles))


def test_list_role_metadata_returns_one_entry_per_role_sorted_by_name():
    add_role_prompt("zeta_role", "z")
    add_role_prompt("alpha_role", "a")
    entries = list_role_metadata()
    names = [e["role"] for e in entries]
    assert names == sorted(names)
    zeta_entry = next(e for e in entries if e["role"] == "zeta_role")
    assert zeta_entry["brief"] == "z"


def test_list_role_metadata_entry_shape_includes_role_key_plus_full_metadata():
    add_role_prompt("solo_role", "brief text", source="panel_brief_writer")
    entries = list_role_metadata()
    entry = next(e for e in entries if e["role"] == "solo_role")
    assert entry["brief"] == "brief text"
    assert entry["source"] == "panel_brief_writer"
    assert "times_hired" in entry
    assert "pinned" in entry
