"""
tests/unit/test_eo_user_profile_b5.py — Patch B5 (Output-Format Routing).

Covers eo/user_profile.py's default_format_hint() — the read side B5
adds on top of B1's existing output_prefs store. Priorities, mirroring
test_eo_user_profile.py's own coverage style:

  - No owner_id, no stored profile, or a low-confidence single inferred
    observation must all yield "" — the "don't calcify on one offhand
    comment" rule applies to what gets fed into a prompt just as much
    as to what gets stored (test_eo_user_profile.py already covers the
    storage side of that same rule).
  - An explicit statement clears the threshold immediately; two
    corroborating inferred observations clear it too.
  - Every recognized default_format key produces a non-empty, distinct
    clause; an unrecognized key degrades to "" rather than crashing.
  - The hint is always an ADDITION (a "\\n\\n"-prefixed clause) so
    callers that do `SYSTEM_PROMPT + default_format_hint(owner_id)` get
    back exactly SYSTEM_PROMPT, unchanged, when there's nothing to add.

Isolation: same conftest `fake_bus` fixture every other eo/user_profile
test file already leans on.
"""
from eo import user_profile


def test_no_owner_id_returns_empty_string():
    assert user_profile.default_format_hint(None) == ""
    assert user_profile.default_format_hint("") == ""


def test_never_written_account_returns_empty_string():
    assert user_profile.default_format_hint("brand-new-user") == ""


def test_single_inferred_observation_is_below_threshold_and_returns_empty_string():
    user_profile.set_output_pref("user-1", "diagram", explicit=False)
    # One inferred observation lands at INFERRED_STARTING_CONFIDENCE
    # (0.2), below FORMAT_HINT_CONFIDENCE_THRESHOLD (0.35) — must not
    # steer generation off a single offhand pattern.
    assert user_profile.default_format_hint("user-1") == ""


def test_two_corroborating_inferred_observations_clear_the_threshold():
    user_profile.set_output_pref("user-1", "diagram", explicit=False)
    user_profile.set_output_pref("user-1", "diagram", explicit=False)
    hint = user_profile.default_format_hint("user-1")
    assert hint != ""
    assert hint.startswith("\n\n")
    assert "mermaid" in hint


def test_explicit_statement_clears_the_threshold_immediately():
    user_profile.set_output_pref("user-1", "artifact", explicit=True)
    hint = user_profile.default_format_hint("user-1")
    assert hint != ""
    assert "artifact" in hint.lower()


def test_every_recognized_format_key_produces_a_distinct_nonempty_clause():
    hints = {}
    for fmt in user_profile._FORMAT_HINT_CLAUSES:
        user_profile.set_output_pref(f"user-{fmt}", fmt, explicit=True)
        hint = user_profile.default_format_hint(f"user-{fmt}")
        assert hint != "", f"expected a non-empty hint for {fmt!r}"
        hints[fmt] = hint
    # No two recognized formats should collapse to the same instruction —
    # that would defeat the point of a per-format hint.
    assert len(set(hints.values())) == len(hints)


def test_unrecognized_stored_format_degrades_to_empty_string():
    # A future writer using a format name this module doesn't know yet
    # (e.g. a value B2's classifier starts emitting before this map is
    # updated) must never crash generation — silently no-op instead.
    user_profile.set_output_pref("user-1", "some_future_format", explicit=True)
    assert user_profile.default_format_hint("user-1") == ""


def test_hint_is_a_pure_addition_not_a_replacement():
    user_profile.set_output_pref("user-1", "table", explicit=True)
    base_prompt = "You are a helpful assistant. Format your answer in Markdown."
    combined = base_prompt + user_profile.default_format_hint("user-1")
    assert combined.startswith(base_prompt)
    assert combined != base_prompt

    # And the true no-op case: an account with nothing confident stored
    # yields back exactly the base prompt, byte for byte.
    untouched = base_prompt + user_profile.default_format_hint("never-seen-user")
    assert untouched == base_prompt
