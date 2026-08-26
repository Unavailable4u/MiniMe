"""
tests/unit/test_hardware_speccer_pricing.py — Patch K.2 (pricing-audit):
covers agents/hardware_speccer.py's _populate_prices() dispatch split --
a `category == "3D_PRINT"` part must never reach
agents/part_price_finder.py's find_price() (no real-world retail
listing exists for it to find), and must instead receive a
deterministic eo/mech_material.py estimate_print_cost_bdt() price
marked `price_source: "estimated_print_cost"`. Every other category is
asserted unaffected -- still dispatched through the existing
worker-pool/find_price() path, unchanged.

_select_workers is monkeypatched to force the "no tagged accounts"
fallback branch (RuntimeError) -- the worker-pool/account-selection
machinery itself is out of scope here (covered elsewhere); this file
only cares about the category split that happens BEFORE any of that
dispatch machinery runs.
"""
import agents.hardware_speccer as hs


def _no_workers(*args, **kwargs):
    raise RuntimeError("no accounts tagged for this role in this test")


def test_3d_print_parts_never_call_find_price(monkeypatch):
    calls = []

    def _fake_find_price(name, **kwargs):
        calls.append(name)
        return {"listings": [{"price_bdt": 500, "vendor": "Some Vendor", "url": "https://x"}],
                "checked_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr("agents.part_price_finder.find_price", _fake_find_price)
    monkeypatch.setattr("eo.worker_pool._select_workers", _no_workers)

    parts = [
        {"id": "housing_1", "name": "Enclosure Housing", "category": "3D_PRINT",
         "dimensions_mm": {"w": 100, "h": 60, "d": 30}, "qty": 1},
        {"id": "lid_1", "name": "Enclosure Lid", "category": "3D_PRINT", "qty": 1},
    ]

    result = hs._populate_prices(parts, session_id="test-session")

    assert calls == []  # find_price never invoked for either part
    for part in result:
        assert part["price_source"] == "estimated_print_cost"
        assert part["vendor_name"] is None
        assert part["vendor_url"] is None
        assert isinstance(part["estimated_price_bdt"], float)
        assert part["estimated_price_bdt"] > 0


def test_non_3d_print_parts_still_go_through_market_search(monkeypatch):
    calls = []

    def _fake_find_price(name, **kwargs):
        calls.append(name)
        return {"listings": [{"price_bdt": 750, "vendor": "DigiKey", "url": "https://digikey.example/x"}],
                "checked_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr("agents.part_price_finder.find_price", _fake_find_price)
    monkeypatch.setattr("eo.worker_pool._select_workers", _no_workers)

    parts = [{"id": "mcu_1", "name": "ESP32 DevKit", "category": "mcu", "qty": 1}]
    result = hs._populate_prices(parts, session_id="test-session")

    assert calls == ["ESP32 DevKit"]
    assert result[0]["estimated_price_bdt"] == 750
    assert result[0]["vendor_name"] == "DigiKey"
    assert result[0]["price_source"] == "market_listing"


def test_mixed_bom_only_electrical_parts_hit_market_search(monkeypatch):
    calls = []

    def _fake_find_price(name, **kwargs):
        calls.append(name)
        return {"listings": [], "checked_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr("agents.part_price_finder.find_price", _fake_find_price)
    monkeypatch.setattr("eo.worker_pool._select_workers", _no_workers)

    parts = [
        {"id": "mcu_1", "name": "ESP32 DevKit", "category": "mcu", "qty": 1},
        {"id": "housing_1", "name": "Enclosure Housing", "category": "3D_PRINT",
         "dimensions_mm": {"w": 50, "h": 50, "d": 20}, "qty": 1},
        {"id": "sensor_1", "name": "BME280", "category": "sensor", "qty": 1},
    ]
    result = hs._populate_prices(parts, session_id="test-session")

    assert sorted(calls) == ["BME280", "ESP32 DevKit"]  # housing never queried

    by_id = {p["id"]: p for p in result}
    assert by_id["housing_1"]["price_source"] == "estimated_print_cost"
    assert "price_source" not in by_id["mcu_1"] or by_id["mcu_1"].get("estimated_price_bdt") is None


def test_original_part_order_is_preserved(monkeypatch):
    monkeypatch.setattr("agents.part_price_finder.find_price",
                         lambda name, **k: {"listings": [], "checked_at": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr("eo.worker_pool._select_workers", _no_workers)

    parts = [
        {"id": "a", "name": "A", "category": "mcu", "qty": 1},
        {"id": "b", "name": "B", "category": "3D_PRINT", "qty": 1},
        {"id": "c", "name": "C", "category": "sensor", "qty": 1},
        {"id": "d", "name": "D", "category": "3D_PRINT", "qty": 1},
    ]
    result = hs._populate_prices(parts, session_id="test-session")
    assert [p["id"] for p in result] == ["a", "b", "c", "d"]


def test_wearable_archetype_resolves_strap_to_tpu_material(monkeypatch):
    """archetype forwarding: a strap-flavored 3D_PRINT part on a wearable
    archetype should price against tpu_flexible, not the pla_rigid
    default -- exercised end-to-end through _populate_prices(), not just
    at resolve_material()'s own unit level (see test_mech_material.py)."""
    from eo.enclosure_spec import MATERIAL_PROPERTIES

    monkeypatch.setattr("eo.worker_pool._select_workers", _no_workers)
    patched = dict(MATERIAL_PROPERTIES)
    patched["tpu_flexible"] = dict(MATERIAL_PROPERTIES["tpu_flexible"])
    patched["tpu_flexible"]["cost_per_gram_bdt"] = 999.0
    monkeypatch.setattr("eo.mech_material.MATERIAL_PROPERTIES", patched)

    strap = {"id": "strap_1", "name": "Wrist Strap", "category": "3D_PRINT",
             "generic_name": "Wrist Strap", "dimensions_mm": {"w": 40, "h": 10, "d": 5}, "qty": 1}
    wearable_archetype = {"enclosure_mode": "full", "mobility_type": "wearable"}

    result = hs._populate_prices([strap], session_id="test-session", archetype=wearable_archetype)
    assert result[0]["estimated_price_bdt"] > 100  # 999 BDT/g dwarfs the 3.5 BDT/g default
