"""
tests/unit/test_eo_user_profile.py — Patch B1.

eo/user_profile.py is a new module (structural sibling of
eo/workspace_facts.py, but keyed by owner_id instead of workspace_id).
The highest-value things to pin down, mirroring
test_eo_workspace_facts.py's coverage priorities for its sibling:

  - get_profile() always returns the full shape, and a never-written
    account's empty profile must not alias (and therefore leak into)
    another account's — same class of bug workspace_facts.py's
    _empty_facts() docstring documents for its own module.
  - The evidence-count-gated confidence curve in _next_confidence():
    a single inferred observation must land at a LOW confidence, not
    calcify immediately; repeated corroborating observations climb the
    curve; an explicit statement always jumps straight to high
    confidence and is never eroded by a later inferred signal.
  - output_prefs' single-record setter reuses the same curve rather
    than having its own copy-pasted logic.
  - append_correction()/list_corrections() as a plain, independent
    audit log.

Isolation: same posture as test_eo_workspace_facts.py — leans on
conftest's autouse `fake_bus` fixture rather than hand-mocking
read/write, so the module's own internal read()/write() calls
round-trip through real memory.bus logic.
"""
import pytest

from eo import user_profile

# ---------------------------------------------------------------------
# _key
# ---------------------------------------------------------------------

def test_key_is_namespaced_by_owner_id():
    assert user_profile._key("user-1") == "user_profile:user-1"


# ---------------------------------------------------------------------
# get_profile — always returns/accepts the full shape
# ---------------------------------------------------------------------

def test_get_profile_with_no_owner_id_returns_empty_profile():
    assert user_profile.get_profile(None) == user_profile.EMPTY_PROFILE


def test_get_profile_for_a_never_written_account_returns_the_full_empty_shape():
    profile = user_profile.get_profile("brand-new-user")
    assert profile["domains"] == {}
    assert profile["likes"] == {}
    assert profile["dislikes"] == {}
    assert profile["error_patterns"] == {}
    assert profile["output_prefs"]["default_format"] is None
    assert profile["output_prefs"]["confidence"] == 0.0
    assert profile["corrections"] == []


def test_get_profile_empty_shape_does_not_alias_the_shared_empty_profile_constant():
    """Regression guard, same class of bug as
    workspace_facts.test_get_facts_empty_shape_does_not_alias_the_shared_empty_facts_constant:
    a never-written account's profile must never share nested
    dict/list containers with another account's."""
    user_profile.record_signal("user-leaky-source", "likes", "diagrams", explicit=True)
    profile = user_profile.get_profile("user-never-touched")
    assert profile["likes"] == {}
    assert "diagrams" not in profile["likes"]


# ---------------------------------------------------------------------
# record_signal — validation
# ---------------------------------------------------------------------

def test_record_signal_requires_owner_id_category_and_key():
    with pytest.raises(ValueError):
        user_profile.record_signal(None, "likes", "diagrams")
    with pytest.raises(ValueError):
        user_profile.record_signal("user-1", "", "diagrams")
    with pytest.raises(ValueError):
        user_profile.record_signal("user-1", "likes", "")


def test_record_signal_rejects_an_unknown_category():
    with pytest.raises(ValueError):
        user_profile.record_signal("user-1", "not_a_real_category", "x")


# ---------------------------------------------------------------------
# record_signal — evidence-count-gated confidence curve
# ---------------------------------------------------------------------

def test_a_single_inferred_observation_lands_at_the_low_starting_confidence():
    """The whole point of this module: one offhand comment must not
    calcify into a permanent trait."""
    profile = user_profile.record_signal("user-1", "dislikes", "Python", explicit=False)
    entry = profile["dislikes"]["Python"]
    assert entry["confidence"] == user_profile.INFERRED_STARTING_CONFIDENCE
    assert entry["evidence_count"] == 1
    assert entry["explicit"] is False


def test_repeated_corroborating_inferred_signals_raise_confidence_each_time():
    user_profile.record_signal("user-1", "domains", "React", explicit=False)
    profile = user_profile.record_signal("user-1", "domains", "React", explicit=False)
    entry = profile["domains"]["React"]
    assert entry["evidence_count"] == 2
    assert entry["confidence"] == pytest.approx(
        user_profile.INFERRED_STARTING_CONFIDENCE + user_profile.INFERRED_CONFIDENCE_STEP
    )


def test_inferred_confidence_never_exceeds_the_cap():
    for _ in range(50):
        profile = user_profile.record_signal("user-1", "domains", "React", explicit=False)
    assert profile["domains"]["React"]["confidence"] <= user_profile.INFERRED_CONFIDENCE_CAP


def test_inferred_confidence_never_reaches_explicit_confidence():
    for _ in range(50):
        profile = user_profile.record_signal("user-1", "domains", "React", explicit=False)
    assert profile["domains"]["React"]["confidence"] < user_profile.EXPLICIT_CONFIDENCE


def test_explicit_signal_jumps_straight_to_explicit_confidence():
    profile = user_profile.record_signal("user-1", "likes", "dark mode", explicit=True)
    entry = profile["likes"]["dark mode"]
    assert entry["confidence"] == user_profile.EXPLICIT_CONFIDENCE
    assert entry["evidence_count"] == 1
    assert entry["explicit"] is True


def test_explicit_signal_overrides_a_prior_weak_inferred_one_immediately():
    user_profile.record_signal("user-1", "dislikes", "Python", explicit=False)
    profile = user_profile.record_signal("user-1", "dislikes", "Python", explicit=True)
    entry = profile["dislikes"]["Python"]
    assert entry["confidence"] == user_profile.EXPLICIT_CONFIDENCE
    assert entry["explicit"] is True


def test_a_later_inferred_signal_does_not_erode_an_explicit_entrys_confidence():
    user_profile.record_signal("user-1", "likes", "dark mode", explicit=True)
    profile = user_profile.record_signal("user-1", "likes", "dark mode", explicit=False)
    entry = profile["likes"]["dark mode"]
    assert entry["confidence"] == user_profile.EXPLICIT_CONFIDENCE
    assert entry["explicit"] is True
    # bookkeeping still advances even though the visible confidence is pinned
    assert entry["evidence_count"] == 2


def test_record_signal_updates_last_seen_at_but_preserves_first_seen_at():
    first = user_profile.record_signal("user-1", "domains", "React", explicit=False)
    first_seen = first["domains"]["React"]["first_seen_at"]
    second = user_profile.record_signal("user-1", "domains", "React", explicit=False)
    assert second["domains"]["React"]["first_seen_at"] == first_seen
    assert second["domains"]["React"]["last_seen_at"] >= first_seen


def test_record_signal_stores_a_category_specific_value_payload():
    profile = user_profile.record_signal(
        "user-1", "domains", "React", value={"level": "intermediate"},
    )
    assert profile["domains"]["React"]["value"] == {"level": "intermediate"}


def test_record_signal_different_keys_in_the_same_category_do_not_collide():
    user_profile.record_signal("user-1", "likes", "diagrams", explicit=False)
    profile = user_profile.record_signal("user-1", "likes", "dark mode", explicit=False)
    assert set(profile["likes"].keys()) == {"diagrams", "dark mode"}
    assert profile["likes"]["diagrams"]["evidence_count"] == 1


# ---------------------------------------------------------------------
# set_output_pref
# ---------------------------------------------------------------------

def test_set_output_pref_requires_owner_id_and_default_format():
    with pytest.raises(ValueError):
        user_profile.set_output_pref(None, "markdown")
    with pytest.raises(ValueError):
        user_profile.set_output_pref("user-1", "")


def test_set_output_pref_inferred_starts_low_and_climbs_on_repetition():
    first = user_profile.set_output_pref("user-1", "diagram", explicit=False)
    assert first["output_prefs"]["confidence"] == user_profile.INFERRED_STARTING_CONFIDENCE
    second = user_profile.set_output_pref("user-1", "diagram", explicit=False)
    assert second["output_prefs"]["confidence"] == pytest.approx(
        user_profile.INFERRED_STARTING_CONFIDENCE + user_profile.INFERRED_CONFIDENCE_STEP
    )


def test_set_output_pref_switching_to_a_different_value_restarts_the_curve():
    user_profile.set_output_pref("user-1", "diagram", explicit=False)
    user_profile.set_output_pref("user-1", "diagram", explicit=False)
    profile = user_profile.set_output_pref("user-1", "markdown", explicit=False)
    assert profile["output_prefs"]["default_format"] == "markdown"
    assert profile["output_prefs"]["confidence"] == user_profile.INFERRED_STARTING_CONFIDENCE


def test_set_output_pref_explicit_sets_high_confidence_immediately():
    profile = user_profile.set_output_pref("user-1", "artifact", explicit=True)
    assert profile["output_prefs"]["confidence"] == user_profile.EXPLICIT_CONFIDENCE
    assert profile["output_prefs"]["explicit"] is True


# ---------------------------------------------------------------------
# append_correction / list_corrections
# ---------------------------------------------------------------------

def test_append_correction_requires_owner_id_and_field():
    with pytest.raises(ValueError):
        user_profile.append_correction(None, "likes", "diagrams", True, False)
    with pytest.raises(ValueError):
        user_profile.append_correction("user-1", "", "diagrams", True, False)


def test_append_correction_appends_an_entry_with_a_stable_id():
    profile = user_profile.append_correction(
        "user-1", "likes", "diagrams", old_value=True, new_value=False, reason="user said so",
    )
    assert len(profile["corrections"]) == 1
    entry = profile["corrections"][0]
    assert entry["correction_id"].startswith("corr_")
    assert entry["field"] == "likes"
    assert entry["key"] == "diagrams"
    assert entry["old_value"] is True
    assert entry["new_value"] is False
    assert entry["reason"] == "user said so"


def test_list_corrections_returns_empty_list_for_an_account_with_none_logged():
    assert user_profile.list_corrections("brand-new-user") == []


def test_corrections_log_is_append_only_across_multiple_calls():
    user_profile.append_correction("user-1", "likes", "diagrams", True, False)
    user_profile.append_correction("user-1", "output_prefs", None, "markdown", "diagram")
    assert len(user_profile.list_corrections("user-1")) == 2
