"""
tests/unit/test_pin_resolution_gate.py — Phase 3 (Master Guide gap #10,
"Pin resolution gate"): covers the three Patch 3.1/3.2 building blocks --

  - eo.mech_validator.find_unresolved_inferred_pins() (Patch 3.1): pure
    scan of wiring.edges for "_inferred": True entries whose still-null
    pin belongs to a part actually present in the final device
    (mech["mech"]["sections"]) -- not just any part sitting unused in
    mech["mech"]["placements"]
  - agents.hardware_speccer.resolve_inferred_pin() (Patch 3.2): single
    targeted resolution retry for one part/one pin, capped at one
    datasheet lookup + one LLM call by construction (no internal loop)
  - the combination the two are meant to support once Patch 3.3 wires
    them into run_hardware_speccer()'s finalize path: a resolvable pin
    gets resolved, an unresolvable one stays flagged rather than
    silently dropped, and a non-load-bearing inferred pin (its part
    isn't in any final section) is correctly ignored by the scanner so
    Patch 3.2 is never even called for it.

No real FreeCAD, no real datasheet download, no real LLM call anywhere
in this file -- get_datasheet_detail() and generate_text() are both
monkeypatched, same "fake out the network boundary, exercise this
module's own orchestration logic" approach test_mech_repair.py and
test_mech_validator.py already use.
"""
import eo.mech_validator as mv
import agents.hardware_speccer as hs


# ---------------------------------------------------------------------------
# find_unresolved_inferred_pins (Patch 3.1)
# ---------------------------------------------------------------------------

def test_no_wiring_returns_empty():
    assert mv.find_unresolved_inferred_pins({}) == []
    assert mv.find_unresolved_inferred_pins({"wiring": {}}) == []
    assert mv.find_unresolved_inferred_pins({"wiring": {"edges": []}}) == []


def test_no_sections_yet_returns_empty_even_with_inferred_edges():
    spec = {
        "wiring": {"edges": [
            {"from": "mcu_1", "to": "sensor_1", "kind": "data",
             "from_pin": "SCL", "to_pin": None, "_inferred": True},
        ]},
        "mech": {"placements": [], "sections": []},
    }
    assert mv.find_unresolved_inferred_pins(spec) == []


def test_finds_load_bearing_unresolved_pin():
    spec = {
        "wiring": {"edges": [
            {"from": "mcu_1", "to": "sensor_1", "kind": "data",
             "from_pin": "SCL", "to_pin": None, "_inferred": True},
        ]},
        "mech": {
            "placements": [
                {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0},
                {"part_id": "sensor_1", "x": 10, "y": 0, "z": 0},
            ],
            # Both mcu_1 and sensor_1 resolve into a checkable section --
            # realistic end-to-end shape.
            "sections": [
                {"section_id": "Compute", "subsection_ids": ["mcu_1"]},
                {"section_id": "Sensing", "subsection_ids": ["sensor_1"]},
            ],
        },
    }

    result = mv.find_unresolved_inferred_pins(spec)

    assert len(result) == 1
    entry = result[0]
    assert entry["part_id"] == "sensor_1"
    assert entry["pin_side"] == "to"
    assert entry["pin_hint"] == "SCL"
    assert entry["edge"] is spec["wiring"]["edges"][0]  # same object, not a copy


def test_non_load_bearing_part_is_ignored():
    """An inferred edge referencing a part that never made it into any
    final section (mech["sections"]) is not "load-bearing" -- the
    scanner must not report it, so Patch 3.2 never gets called for a
    part that isn't actually shipping."""
    spec = {
        "wiring": {"edges": [
            {"from": "mcu_1", "to": "orphan_sensor_1", "kind": "data",
             "from_pin": "SCL", "to_pin": None, "_inferred": True},
        ]},
        "mech": {
            "placements": [
                {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0},
                {"part_id": "orphan_sensor_1", "x": 10, "y": 0, "z": 0},
            ],
            # Only mcu_1 made it into a final section -- orphan_sensor_1
            # is discarded/unused.
            "sections": [{"section_id": "Compute", "subsection_ids": ["mcu_1"]}],
        },
    }

    assert mv.find_unresolved_inferred_pins(spec) == []


def test_non_inferred_edges_are_ignored():
    spec = {
        "wiring": {"edges": [
            {"from": "mcu_1", "to": "sensor_1", "kind": "data",
             "from_pin": "GPIO4", "to_pin": "SDA"},  # model-proposed, not inferred
        ]},
        "mech": {
            "placements": [
                {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0},
                {"part_id": "sensor_1", "x": 10, "y": 0, "z": 0},
            ],
            "sections": [
                {"section_id": "Compute", "subsection_ids": ["mcu_1"]},
                {"section_id": "Sensing", "subsection_ids": ["sensor_1"]},
            ],
        },
    }

    assert mv.find_unresolved_inferred_pins(spec) == []


def test_inferred_edge_with_both_pins_named_contributes_nothing():
    """Shouldn't happen given today's two synthesis cases, but if a
    future one ever leaves both sides named, there's no unresolved pin
    left to chase even though the edge is still tagged _inferred."""
    spec = {
        "wiring": {"edges": [
            {"from": "batt_1", "to": "reg_1", "kind": "power",
             "from_pin": "OUT", "to_pin": "VIN", "_inferred": True},
        ]},
        "mech": {
            "placements": [
                {"part_id": "batt_1", "x": 0, "y": 0, "z": 0},
                {"part_id": "reg_1", "x": 10, "y": 0, "z": 0},
            ],
            "sections": [{"section_id": "Power", "subsection_ids": ["batt_1", "reg_1"]}],
        },
    }
    assert mv.find_unresolved_inferred_pins(spec) == []


# ---------------------------------------------------------------------------
# resolve_inferred_pin (Patch 3.2)
# ---------------------------------------------------------------------------

def test_resolve_inferred_pin_returns_none_without_datasheet_url():
    part = {"id": "sensor_1", "name": "Generic Sensor"}
    assert hs.resolve_inferred_pin(part, "SCL", "data", chain=[]) is None


def test_resolve_inferred_pin_returns_none_when_datasheet_lookup_fails(monkeypatch):
    def _boom(url):
        raise RuntimeError("network down")
    monkeypatch.setattr("agents.component_spec_lookup.get_datasheet_detail", _boom)

    part = {"id": "sensor_1", "name": "Generic Sensor", "datasheet_url": "https://example.com/ds.pdf"}
    assert hs.resolve_inferred_pin(part, "SCL", "data", chain=[]) is None


def test_resolve_inferred_pin_returns_none_on_empty_detail(monkeypatch):
    monkeypatch.setattr("agents.component_spec_lookup.get_datasheet_detail", lambda url: None)

    part = {"id": "sensor_1", "name": "Generic Sensor", "datasheet_url": "https://example.com/ds.pdf"}
    assert hs.resolve_inferred_pin(part, "SCL", "data", chain=[]) is None


def test_resolve_inferred_pin_success(monkeypatch):
    monkeypatch.setattr(
        "agents.component_spec_lookup.get_datasheet_detail",
        lambda url: {"title": "Sensor Datasheet", "content": "Pin table: SCL -> GPIO22", "page_count": 3},
    )
    monkeypatch.setattr(hs, "generate_text", lambda *a, **k: "GPIO22")

    part = {"id": "sensor_1", "name": "Generic Sensor", "datasheet_url": "https://example.com/ds.pdf"}
    result = hs.resolve_inferred_pin(part, "SCL", "data", chain=[])

    assert result == "GPIO22"


def test_resolve_inferred_pin_honest_unknown(monkeypatch):
    monkeypatch.setattr(
        "agents.component_spec_lookup.get_datasheet_detail",
        lambda url: {"title": "t", "content": "no pinout table here", "page_count": 1},
    )
    monkeypatch.setattr(hs, "generate_text", lambda *a, **k: "UNKNOWN")

    part = {"id": "sensor_1", "datasheet_url": "https://example.com/ds.pdf"}
    assert hs.resolve_inferred_pin(part, "SCL", "data", chain=[]) is None


def test_resolve_inferred_pin_rejects_sentence_shaped_answer(monkeypatch):
    """A hedged/explained answer isn't a clean pin label -- treated the
    same as "still unresolved", not returned as-is."""
    monkeypatch.setattr(
        "agents.component_spec_lookup.get_datasheet_detail",
        lambda url: {"title": "t", "content": "ambiguous pinout", "page_count": 1},
    )
    monkeypatch.setattr(
        hs, "generate_text",
        lambda *a, **k: "It's probably GPIO22 but the datasheet is unclear about this",
    )

    part = {"id": "sensor_1", "datasheet_url": "https://example.com/ds.pdf"}
    assert hs.resolve_inferred_pin(part, "SCL", "data", chain=[]) is None


def test_resolve_inferred_pin_swallows_llm_failure(monkeypatch):
    monkeypatch.setattr(
        "agents.component_spec_lookup.get_datasheet_detail",
        lambda url: {"title": "t", "content": "some content", "page_count": 1},
    )

    def _boom(*a, **k):
        raise RuntimeError("provider chain exhausted")
    monkeypatch.setattr(hs, "generate_text", _boom)

    part = {"id": "sensor_1", "datasheet_url": "https://example.com/ds.pdf"}
    assert hs.resolve_inferred_pin(part, "SCL", "data", chain=[]) is None


# ---------------------------------------------------------------------------
# Combined behavior the two functions are meant to support (Patch 3.3's
# eventual finalize-path wiring) -- exercised directly here since 3.3
# only threads these two together, it introduces no new logic of its own
# to unit-test in isolation.
# ---------------------------------------------------------------------------

def test_resolvable_pin_gets_resolved_end_to_end(monkeypatch):
    spec = {
        "wiring": {"edges": [
            {"from": "mcu_1", "to": "sensor_1", "kind": "data",
             "from_pin": "SCL", "to_pin": None, "_inferred": True},
        ]},
        "parts": [
            {"id": "sensor_1", "name": "Generic Sensor", "datasheet_url": "https://example.com/ds.pdf"},
        ],
        "mech": {
            "placements": [
                {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0},
                {"part_id": "sensor_1", "x": 10, "y": 0, "z": 0},
            ],
            "sections": [
                {"section_id": "Compute", "subsection_ids": ["mcu_1"]},
                {"section_id": "Sensing", "subsection_ids": ["sensor_1"]},
            ],
        },
    }
    monkeypatch.setattr(
        "agents.component_spec_lookup.get_datasheet_detail",
        lambda url: {"title": "t", "content": "SCL -> GPIO21", "page_count": 1},
    )
    monkeypatch.setattr(hs, "generate_text", lambda *a, **k: "GPIO21")

    unresolved = mv.find_unresolved_inferred_pins(spec)
    assert len(unresolved) == 1
    pin = unresolved[0]
    parts_by_id = {p["id"]: p for p in spec["parts"]}
    resolved = hs.resolve_inferred_pin(
        parts_by_id.get(pin["part_id"]), pin["pin_hint"], pin["edge"].get("kind"), chain=[],
    )
    assert resolved == "GPIO21"

    edge = pin["edge"]
    if pin["pin_side"] == "from":
        edge["from_pin"] = resolved
    else:
        edge["to_pin"] = resolved

    assert spec["wiring"]["edges"][0]["to_pin"] == "GPIO21"
    assert spec["wiring"]["edges"][0]["_inferred"] is True  # resolving the pin doesn't un-infer the edge


def test_unresolvable_pin_is_flagged_not_dropped(monkeypatch):
    spec = {
        "wiring": {"edges": [
            {"from": "mcu_1", "to": "sensor_1", "kind": "data",
             "from_pin": "SCL", "to_pin": None, "_inferred": True},
        ]},
        "parts": [
            {"id": "sensor_1", "name": "Generic Sensor", "datasheet_url": "https://example.com/ds.pdf"},
        ],
        "mech": {
            "placements": [
                {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0},
                {"part_id": "sensor_1", "x": 10, "y": 0, "z": 0},
            ],
            "sections": [
                {"section_id": "Compute", "subsection_ids": ["mcu_1"]},
                {"section_id": "Sensing", "subsection_ids": ["sensor_1"]},
            ],
        },
    }
    monkeypatch.setattr(
        "agents.component_spec_lookup.get_datasheet_detail",
        lambda url: {"title": "t", "content": "no pinout info", "page_count": 1},
    )
    monkeypatch.setattr(hs, "generate_text", lambda *a, **k: "UNKNOWN")

    unresolved = mv.find_unresolved_inferred_pins(spec)
    assert len(unresolved) == 1
    pin = unresolved[0]
    parts_by_id = {p["id"]: p for p in spec["parts"]}
    resolved = hs.resolve_inferred_pin(
        parts_by_id.get(pin["part_id"]), pin["pin_hint"], pin["edge"].get("kind"), chain=[],
    )
    assert resolved is None

    # Simulate Patch 3.3's own "still unresolved -> flag loudly" branch.
    still_unresolved = [] if resolved else [{
        "part_id": pin["part_id"], "pin_side": pin["pin_side"],
        "kind": pin["edge"].get("kind"),
        "from": pin["edge"].get("from"), "to": pin["edge"].get("to"),
    }]
    assert still_unresolved == [{
        "part_id": "sensor_1", "pin_side": "to",
        "kind": "data", "from": "mcu_1", "to": "sensor_1",
    }]
    # The pin stays null on the edge itself -- flagged separately, never
    # silently dropped or guessed.
    assert spec["wiring"]["edges"][0]["to_pin"] is None
