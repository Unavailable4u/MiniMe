"""
tests/unit/test_price_outlier_flagging.py — Patch K.3/K.4 (pricing-audit):
covers eo/price_outliers.py's flag_price_outliers() -- the pricing-
summary flagging step wired into agents/hardware_speccer.py's
generate_hardware_spec() and api/routes/workspace_data.py's
refresh_part_prices(), both right after their own _populate_prices()-
equivalent pricing pass.

Per the guide's own "Done when": a dataset with one outlier price
(>=5x category median) and one asymmetric duplicate-part pair (one
priced, one not) both come back flagged; a dataset with no such
anomalies produces no flags.
"""
from eo.price_outliers import flag_price_outliers


def _part(id, name, category, price, **extra):
    return {"id": id, "name": name, "category": category,
            "estimated_price_bdt": price, "qty": 1, **extra}


# ---------------------------------------------------------------------------
# Outlier detection
# ---------------------------------------------------------------------------

def test_outlier_price_is_flagged():
    parts = [
        _part("r1", "10k Resistor", "module", 20),
        _part("r2", "10uF Capacitor", "module", 22),
        _part("r3", "Suspiciously Expensive Module", "module", 25000),  # >5x median (21)
    ]
    result = flag_price_outliers(parts)
    by_id = {p["id"]: p for p in result}

    assert by_id["r3"]["price_flagged"] is True
    assert "median" in by_id["r3"]["price_flag_reason"]
    assert by_id["r1"]["price_flagged"] is False
    assert by_id["r2"]["price_flagged"] is False


def test_price_exactly_at_threshold_is_not_flagged():
    # median of [10, 10] = 10; 5x = 50 exactly -- guide says "more than
    # ~5x", so exactly 5x must NOT flag (strictly greater-than).
    parts = [
        _part("a", "A", "module", 10),
        _part("b", "B", "module", 10),
        _part("c", "C", "module", 50),
    ]
    result = flag_price_outliers(parts)
    assert next(p for p in result if p["id"] == "c")["price_flagged"] is False


def test_price_just_over_threshold_is_flagged():
    parts = [
        _part("a", "A", "module", 10),
        _part("b", "B", "module", 10),
        _part("c", "C", "module", 50.01),
    ]
    result = flag_price_outliers(parts)
    assert next(p for p in result if p["id"] == "c")["price_flagged"] is True


def test_no_anomalies_produces_no_flags():
    parts = [
        _part("m1", "ESP32 DevKit", "mcu", 1200),
        _part("s1", "BME280", "sensor", 450),
        _part("s2", "DHT22", "sensor", 380),
        _part("h1", "Enclosure Housing", "3D_PRINT", 90, price_source="estimated_print_cost"),
        _part("l1", "Enclosure Lid", "3D_PRINT", 60, price_source="estimated_print_cost"),
    ]
    result = flag_price_outliers(parts)
    assert all(p["price_flagged"] is False for p in result)
    assert all(p["price_flag_reason"] is None for p in result)


def test_category_with_fewer_than_two_priced_items_never_flags():
    # Single priced item in "power" -- no median to compare against.
    parts = [_part("p1", "Battery Pack", "power", 5000)]
    result = flag_price_outliers(parts)
    assert result[0]["price_flagged"] is False


def test_different_categories_use_separate_medians():
    # 5000 would be a wild outlier among "module" prices, but this is
    # its own "power" category with its own (much higher) median -- must
    # not be flagged just because it's expensive in absolute terms.
    parts = [
        _part("mod1", "Relay Module", "module", 40),
        _part("mod2", "RTC Module", "module", 45),
        _part("pw1", "Lithium Battery", "power", 4800),
        _part("pw2", "Battery Charger", "power", 5200),
    ]
    result = flag_price_outliers(parts)
    assert all(p["price_flagged"] is False for p in result)


# ---------------------------------------------------------------------------
# Asymmetric duplicate-sibling detection
# ---------------------------------------------------------------------------

def test_asymmetric_duplicate_pair_flags_the_unpriced_one():
    parts = [
        _part("bracket_l", "Left Motor Mounting Bracket", "3D_PRINT", 0, price_source="estimated_print_cost"),
        _part("bracket_r", "Right Motor Mounting Bracket", "3D_PRINT", 120, price_source="estimated_print_cost"),
    ]
    # Simulate "no price found" as the real pipeline would represent it
    # (None, not 0) -- _part()'s helper takes a raw price value.
    parts[0]["estimated_price_bdt"] = None

    result = flag_price_outliers(parts)
    by_id = {p["id"]: p for p in result}

    assert by_id["bracket_l"]["price_flagged"] is True
    assert "Right Motor Mounting Bracket" in by_id["bracket_l"]["price_flag_reason"]
    # The priced sibling itself is NOT flagged just because its
    # counterpart lacks a price.
    assert by_id["bracket_r"]["price_flagged"] is False


def test_symmetric_pair_both_priced_is_not_flagged():
    parts = [
        _part("bracket_l", "Left Motor Mounting Bracket", "3D_PRINT", 110, price_source="estimated_print_cost"),
        _part("bracket_r", "Right Motor Mounting Bracket", "3D_PRINT", 120, price_source="estimated_print_cost"),
    ]
    result = flag_price_outliers(parts)
    assert all(p["price_flagged"] is False for p in result)


def test_symmetric_pair_both_unpriced_is_not_flagged():
    parts = [
        _part("bracket_l", "Left Motor Mounting Bracket", "3D_PRINT", None),
        _part("bracket_r", "Right Motor Mounting Bracket", "3D_PRINT", None),
    ]
    result = flag_price_outliers(parts)
    assert all(p["price_flagged"] is False for p in result)


def test_different_categories_never_match_as_siblings():
    # Same normalized name, different category -- must not be treated as
    # duplicate-part siblings of each other.
    parts = [
        _part("x1", "Left Sensor Mount", "3D_PRINT", 80, price_source="estimated_print_cost"),
        _part("x2", "Right Sensor Mount", "MISC", None),
    ]
    result = flag_price_outliers(parts)
    assert all(p["price_flagged"] is False for p in result)


def test_leftover_is_not_mistaken_for_a_side_word():
    # Whole-word match only -- "Leftover" must not be treated as
    # containing "left".
    parts = [
        _part("a", "Leftover Bracket Stock", "MISC", 30),
        _part("b", "Right Bracket Stock", "MISC", None),
    ]
    result = flag_price_outliers(parts)
    # Different normalized names ("leftover bracket stock" vs "bracket
    # stock") -- not treated as siblings, so the unpriced one stays
    # unflagged.
    assert all(p["price_flagged"] is False for p in result)


def test_falls_back_to_generic_name_when_name_missing():
    parts = [
        {"id": "a", "category": "3D_PRINT", "generic_name": "Left Wheel Mount",
         "estimated_price_bdt": 60, "qty": 1, "price_source": "estimated_print_cost"},
        {"id": "b", "category": "3D_PRINT", "generic_name": "Right Wheel Mount",
         "estimated_price_bdt": None, "qty": 1},
    ]
    result = flag_price_outliers(parts)
    by_id = {p["id"]: p for p in result}
    assert by_id["b"]["price_flagged"] is True


# ---------------------------------------------------------------------------
# Stale-flag clearing + fail-safe input handling
# ---------------------------------------------------------------------------

def test_stale_flag_is_cleared_on_reevaluation():
    # A part that was flagged in a PREVIOUS run (stale field already on
    # it) but no longer qualifies must have both fields overwritten, not
    # left stuck true from before -- per the module's own docstring.
    parts = [
        _part("a", "A", "module", 10, price_flagged=True, price_flag_reason="stale"),
        _part("b", "B", "module", 12),
    ]
    result = flag_price_outliers(parts)
    assert result[0]["price_flagged"] is False
    assert result[0]["price_flag_reason"] is None


def test_non_list_input_returned_unchanged():
    assert flag_price_outliers(None) is None
    assert flag_price_outliers("not-a-list") == "not-a-list"


def test_non_dict_entries_are_skipped_without_raising():
    parts = [_part("a", "A", "module", 10), "garbage-entry", None]
    result = flag_price_outliers(parts)
    assert result[0]["price_flagged"] is False  # unaffected
    assert result[1] == "garbage-entry"
    assert result[2] is None


def test_zero_and_negative_prices_treated_as_unpriced():
    parts = [
        _part("a", "Left Widget", "MISC", 0),
        _part("b", "Right Widget", "MISC", -5),
        _part("c", "Center Widget", "MISC", 40),
    ]
    result = flag_price_outliers(parts)
    # None of these should crash or produce a nonsensical median/ratio;
    # 0/negative both count as "no price" so no duplicate-sibling match
    # exists between a and b (neither is "priced").
    assert all(isinstance(p["price_flagged"], bool) for p in result)
