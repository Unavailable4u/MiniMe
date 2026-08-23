"""
tests/unit/test_eo_modes.py — Patch 7e-2.

eo/modes.py had zero test coverage before this. It's the last gate
between staff_task()'s candidate hire list and executor.py actually
spinning them up, so a wrong ceiling/truncation here either silently
over-hires (cost/quota impact) or silently drops hires a mode should
have kept.

Isolation: apply_mode() reads MODE_CEILINGS as a plain module-level
dict imported via `from eo.router import MODE_CEILINGS` (a bound name
in eo.modes's own namespace, same import shape tags.py's docstring
warns about for chat_workspace/chat_store) -- so tests that need to
control the ceiling table patch it on `modes.MODE_CEILINGS`, not on
`eo.router.MODE_CEILINGS`, or the patch would never be seen by
apply_mode().
"""
import eo.modes as modes


# ---------------------------------------------------------------------
# non-ceiling modes (expert / anything below its ceiling)
# ---------------------------------------------------------------------

def test_expert_mode_has_no_ceiling_returns_all_hires():
    hires = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l",
              "m", "n", "o", "p", "q"]  # 17, over every real ceiling but "expert"
    result = modes.apply_mode("expert", hires, assessed_max=4)
    assert result == {"hires": hires, "ceiling_hit": False, "action": None}


def test_mode_under_its_ceiling_returns_all_hires_unmodified(monkeypatch):
    monkeypatch.setattr(modes, "MODE_CEILINGS", {"auto": 16})
    hires = ["a", "b", "c"]
    result = modes.apply_mode("auto", hires, assessed_max=4)
    assert result == {"hires": hires, "ceiling_hit": False, "action": None}


def test_mode_exactly_at_its_ceiling_is_not_treated_as_hitting_it(monkeypatch):
    """len(hires) <= ceiling must NOT trigger the ceiling-hit branch --
    off-by-one here would wrongly zero out (simple/fast) or truncate
    (auto) a roster that's actually still within budget."""
    monkeypatch.setattr(modes, "MODE_CEILINGS", {"simple": 3})
    hires = ["a", "b", "c"]
    result = modes.apply_mode("simple", hires, assessed_max=2)
    assert result == {"hires": hires, "ceiling_hit": False, "action": None}


def test_mode_name_is_lowercased_before_lookup(monkeypatch):
    """Caller-supplied mode strings shouldn't have to be pre-normalized
    -- "Auto"/"AUTO" must resolve the same ceiling as "auto"."""
    monkeypatch.setattr(modes, "MODE_CEILINGS", {"auto": 16})
    hires = ["a", "b"]
    result = modes.apply_mode("AUTO", hires, assessed_max=4)
    assert result == {"hires": hires, "ceiling_hit": False, "action": None}


# ---------------------------------------------------------------------
# ceiling hit: auto vs simple/fast branch
# ---------------------------------------------------------------------

def test_auto_mode_over_ceiling_truncates_and_offers_beast_mode(monkeypatch):
    monkeypatch.setattr(modes, "MODE_CEILINGS", {"auto": 2})
    hires = ["a", "b", "c", "d"]
    result = modes.apply_mode("auto", hires, assessed_max=4)
    assert result == {"hires": ["a", "b"], "ceiling_hit": True,
                       "action": "offer_beast_mode"}


def test_simple_mode_over_ceiling_drops_all_hires_and_stops(monkeypatch):
    monkeypatch.setattr(modes, "MODE_CEILINGS", {"simple": 2})
    hires = ["a", "b", "c", "d"]
    result = modes.apply_mode("simple", hires, assessed_max=4)
    assert result == {"hires": [], "ceiling_hit": True,
                       "action": "stop_ask_beast_mode"}


def test_fast_mode_over_ceiling_drops_all_hires_and_stops(monkeypatch):
    """fast shares simple's branch (comment: "# simple / fast") --
    pinned as its own test so a future refactor that special-cases one
    but not the other gets caught."""
    monkeypatch.setattr(modes, "MODE_CEILINGS", {"fast": 1})
    hires = ["a", "b"]
    result = modes.apply_mode("fast", hires, assessed_max=4)
    assert result == {"hires": [], "ceiling_hit": True,
                       "action": "stop_ask_beast_mode"}


def test_unknown_mode_with_no_ceiling_entry_passes_through_untouched(monkeypatch):
    """MODE_CEILINGS.get(mode) returning None (mode not in the table at
    all, not even as an explicit None like "expert") must take the same
    no-ceiling path "expert" does, not raise or silently drop hires."""
    monkeypatch.setattr(modes, "MODE_CEILINGS", {})
    hires = ["a", "b", "c"]
    result = modes.apply_mode("some_future_mode", hires, assessed_max=4)
    assert result == {"hires": hires, "ceiling_hit": False, "action": None}


# ---------------------------------------------------------------------
# beast mode sizing
# ---------------------------------------------------------------------

def test_beast_mode_targets_2_5x_assessed_max_when_enough_hires_exist():
    hires = [f"h{i}" for i in range(20)]
    result = modes.apply_mode("beast", hires, assessed_max=4)
    # round(4 * 2.5) == 10
    assert result == {"hires": hires[:10], "ceiling_hit": False, "action": None}


def test_beast_mode_is_capped_by_the_actual_candidate_list_length():
    """If staff_task() only produced fewer candidates than 2.5x the
    assessed max, beast mode must take all of them, not pad the list
    or index past the end."""
    hires = ["a", "b", "c"]
    result = modes.apply_mode("beast", hires, assessed_max=4)
    # round(4 * 2.5) == 10, but only 3 hires exist
    assert result == {"hires": hires, "ceiling_hit": False, "action": None}


def test_beast_mode_with_empty_hires_list_uses_the_2_5x_target_as_len(monkeypatch):
    """Empty `hires` is falsy, so the `len(hires) if hires else ...`
    branch falls back to round(assessed_max * 2.5) as the slice bound
    -- pinned since slicing an empty list with any positive bound is a
    silent no-op either way (hires[:N] == [] regardless of N), so this
    only actually matters if apply_mode's caller ever changes to build
    hires up to that bound rather than slicing an existing list."""
    result = modes.apply_mode("beast", [], assessed_max=4)
    assert result == {"hires": [], "ceiling_hit": False, "action": None}


def test_beast_mode_ignores_mode_ceilings_entirely(monkeypatch):
    """MODE_CEILINGS["beast"] is documented as None ("sized as ~2.5x
    assessed max instead") -- beast must return before ever consulting
    the ceiling table, even if some future edit mistakenly gave it a
    numeric entry."""
    monkeypatch.setattr(modes, "MODE_CEILINGS", {"beast": 1})
    hires = ["a", "b", "c", "d", "e"]
    result = modes.apply_mode("beast", hires, assessed_max=2)
    # round(2 * 2.5) == 5, all 5 hires kept -- ceiling of 1 never applied
    assert result == {"hires": hires, "ceiling_hit": False, "action": None}
