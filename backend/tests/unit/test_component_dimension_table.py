"""
tests/unit/test_component_dimension_table.py — J.4 coverage for J.1
(alias collision log + dimension_ambiguous flag), agents/
component_dimension_table.py.

Uses a throwaway JSON table (monkeypatched over the module's real
_TABLE_PATH) rather than the real data/component_dimensions_table.json,
so collision scenarios are deterministic and don't drift if someone
edits the real curated table later.
"""
import json

import pytest

import agents.component_dimension_table as cdt


@pytest.fixture(autouse=True)
def _reset_module_singletons():
    """component_dimension_table.py's _TABLE_BY_ID/_ALIAS_INDEX/
    _ALIAS_COLLISIONS are module-level singletons, lazily built on first
    use and never torn down (see the module's own comment: "lazy,
    sticky, in-memory"). Without resetting them, whichever test runs
    first "wins" and every later test silently reuses its table/
    collisions instead of the one this test just pointed _TABLE_PATH
    at."""
    cdt._TABLE_BY_ID = None
    cdt._ALIAS_INDEX = None
    cdt._ALIAS_COLLISIONS = None
    yield
    cdt._TABLE_BY_ID = None
    cdt._ALIAS_INDEX = None
    cdt._ALIAS_COLLISIONS = None


def _write_table(tmp_path, rows):
    path = tmp_path / "component_dimensions_table.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def test_no_collision_when_every_alias_is_unique(tmp_path, monkeypatch):
    rows = [
        {"id": "row_a", "generic_name": "Widget A", "aliases": "thing a",
         "dimensions_w_mm": 10, "dimensions_h_mm": 10, "dimensions_d_mm": None,
         "shape": "Box", "mount_type": None, "mount_spec": None,
         "dimension_confidence": "typical"},
        {"id": "row_b", "generic_name": "Widget B", "aliases": "thing b",
         "dimensions_w_mm": 20, "dimensions_h_mm": 20, "dimensions_d_mm": None,
         "shape": "Box", "mount_type": None, "mount_spec": None,
         "dimension_confidence": "typical"},
    ]
    monkeypatch.setattr(cdt, "_TABLE_PATH", _write_table(tmp_path, rows))

    assert cdt.get_alias_collisions() == []

    match = cdt.lookup_curated_dimensions("Widget A")
    assert match is not None
    assert match["dimension_ambiguous"] is False


def test_alias_collision_is_logged_first_row_wins(tmp_path, monkeypatch):
    # Both rows claim the alias "stepper" — a data-authoring bug per the
    # module's own docstring ("a real collision would be a data-
    # authoring bug, not an expected runtime case").
    rows = [
        {"id": "row_first", "generic_name": "First Stepper", "aliases": "stepper",
         "dimensions_w_mm": 28, "dimensions_h_mm": 19, "dimensions_d_mm": None,
         "shape": "Cylindrical", "mount_type": None, "mount_spec": None,
         "dimension_confidence": "typical"},
        {"id": "row_second", "generic_name": "Second Stepper", "aliases": "stepper",
         "dimensions_w_mm": 35, "dimensions_h_mm": 30, "dimensions_d_mm": None,
         "shape": "Cylindrical", "mount_type": None, "mount_spec": None,
         "dimension_confidence": "typical"},
    ]
    monkeypatch.setattr(cdt, "_TABLE_PATH", _write_table(tmp_path, rows))

    collisions = cdt.get_alias_collisions()
    assert collisions == [{"key": "stepper", "kept_id": "row_first", "dropped_id": "row_second"}]

    # "stepper" resolves to whichever row claimed it first.
    match = cdt.lookup_curated_dimensions("stepper")
    assert match["dimension_ref_id"] == "row_first"
    # ...and IS flagged ambiguous, even though it "won" the collision —
    # per _row_to_match()'s docstring, a row is ambiguous if it appears
    # on EITHER side of a recorded collision.
    assert match["dimension_ambiguous"] is True

    # The row that lost the contested alias is still reachable through
    # its own generic_name, and is flagged ambiguous too.
    match_by_generic_name = cdt.lookup_curated_dimensions("Second Stepper")
    assert match_by_generic_name["dimension_ref_id"] == "row_second"
    assert match_by_generic_name["dimension_ambiguous"] is True


def test_row_never_involved_in_a_collision_is_not_flagged(tmp_path, monkeypatch):
    rows = [
        {"id": "contested_a", "generic_name": "A", "aliases": "shared",
         "dimensions_w_mm": 1, "dimensions_h_mm": 1, "dimensions_d_mm": None,
         "shape": "Box", "mount_type": None, "mount_spec": None,
         "dimension_confidence": "typical"},
        {"id": "contested_b", "generic_name": "B", "aliases": "shared",
         "dimensions_w_mm": 2, "dimensions_h_mm": 2, "dimensions_d_mm": None,
         "shape": "Box", "mount_type": None, "mount_spec": None,
         "dimension_confidence": "typical"},
        {"id": "uninvolved", "generic_name": "C", "aliases": "unique alias",
         "dimensions_w_mm": 3, "dimensions_h_mm": 3, "dimensions_d_mm": None,
         "shape": "Box", "mount_type": None, "mount_spec": None,
         "dimension_confidence": "typical"},
    ]
    monkeypatch.setattr(cdt, "_TABLE_PATH", _write_table(tmp_path, rows))

    match = cdt.lookup_curated_dimensions("C")
    assert match["dimension_ref_id"] == "uninvolved"
    assert match["dimension_ambiguous"] is False


def test_missing_table_file_fails_safe_not_hard(tmp_path, monkeypatch):
    """Per _load_table()'s own docstring: G1a is a nice-to-have
    accelerant, never a hard dependency — a missing/malformed file must
    leave lookups returning None, not raise."""
    monkeypatch.setattr(cdt, "_TABLE_PATH", str(tmp_path / "does_not_exist.json"))

    assert cdt.get_alias_collisions() == []
    assert cdt.lookup_curated_dimensions("anything") is None
