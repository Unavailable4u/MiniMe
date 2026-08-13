"""
agents/component_dimension_table.py — G1a: curated component-dimension
table lookup (Master Guide, "G1. Real component measurements", G1a).

Runs FIRST, before agents/component_spec_lookup.py's get_real_spec()
(G1b) -- this is a pure local dict lookup, no network, no cache needed
(nothing to expire; the table only changes when someone edits the
source data file). G1b is the gap-filler: it only runs on whatever a
part this module left without "dimensions_mm" (see
hardware_speccer.py's _populate_curated_dimensions()/
_populate_dimensions() call order).

Data source: backend/agents/data/component_dimensions_table.json, one
row per known component variant, keyed by a permanent, stable "id"
(this is the "dimension_ref_id" the Master Guide's G1a section refers
to). Each row carries the *whole* set of fields a match should merge
onto a part -- not just dimensions_w_mm/h_mm/d_mm, but also shape,
mount_type, mount_spec, dimension_confidence, and source metadata.
Nothing gets silently dropped, per the guide's own wording ("Merges
the whole matched row onto the part").

Matching uses the SAME generic_name/aliases vocabulary
hardware_speccer.py's _ensure_generic_names() already guarantees is
present (normalized, non-empty) on every part by the time G1a runs --
this is deliberate coordination with G2's future reference-design
search, which the Master Guide says should query using this exact
canonical vocabulary too, not ad-hoc LLM wording.
"""

import json
import os

_TABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "component_dimensions_table.json")

# Module-level singletons, built once on first use -- same "lazy,
# sticky, in-memory" shape as component_spec_lookup.py's own
# _digikey_token cache, just for a static table instead of a live
# token. _ALIAS_INDEX maps every normalized generic_name/alias string
# to the row's own "id" (dimension_ref_id), so a lookup is a single
# dict get, not a table scan.
_TABLE_BY_ID = None
_ALIAS_INDEX = None


def _normalize(text: str) -> str:
    """Case/whitespace-insensitive matching key. Deliberately simple
    (no stemming/fuzzy matching) -- the vocabulary on both sides
    (curated table's generic_name/aliases, part's generic_name/
    aliases) is already meant to be canonical, short, human-written
    names ("28BYJ-48 Stepper", "DS18B20"), not free text, so exact
    normalized matching is the right amount of matching, not too
    little.
    """
    return " ".join((text or "").strip().lower().split())


def _load_table() -> None:
    """Populates _TABLE_BY_ID/_ALIAS_INDEX from the JSON data file.
    Fail-safe: a missing/malformed data file leaves both as empty
    dicts rather than raising -- G1a is a nice-to-have accelerant for
    G1b/LLM estimation, never a hard dependency the rest of the spec
    pipeline should break on if the table file is absent.
    """
    global _TABLE_BY_ID, _ALIAS_INDEX

    _TABLE_BY_ID = {}
    _ALIAS_INDEX = {}

    try:
        with open(_TABLE_PATH, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception as e:
        print(f"  [component_dimension_table] failed to load {_TABLE_PATH}: {e}")
        return

    for row in rows:
        row_id = row.get("id")
        if not row_id:
            continue
        _TABLE_BY_ID[row_id] = row

        terms = [row.get("generic_name")]
        raw_aliases = row.get("aliases")
        if isinstance(raw_aliases, str):
            terms.extend(raw_aliases.split(","))
        elif isinstance(raw_aliases, list):
            terms.extend(raw_aliases)

        for term in terms:
            key = _normalize(term) if isinstance(term, str) else ""
            if not key:
                continue
            # First row to claim a given alias wins; the curated table
            # is small/hand-curated enough that a real collision would
            # be a data-authoring bug, not an expected runtime case --
            # log it instead of silently overwriting so it's visible.
            if key in _ALIAS_INDEX and _ALIAS_INDEX[key] != row_id:
                print(f"  [component_dimension_table] alias collision on "
                      f"\"{key}\": {_ALIAS_INDEX[key]!r} vs {row_id!r}, "
                      f"keeping {_ALIAS_INDEX[key]!r}")
                continue
            _ALIAS_INDEX[key] = row_id


def _row_to_match(row: dict) -> dict:
    """Shapes one curated-table row into what
    hardware_speccer._populate_curated_dimensions() merges onto a
    part. dimensions_mm only gets keys the row actually has a non-null
    value for -- per the data file's own legend, null means "not
    applicable to this shape" (e.g. a Cylindrical part's "d"), which
    should stay absent, not become a literal 0/None on the part.
    """
    dims = {}
    for src_key, out_key in (("dimensions_w_mm", "w"),
                              ("dimensions_h_mm", "h"),
                              ("dimensions_d_mm", "d")):
        value = row.get(src_key)
        if value is not None:
            dims[out_key] = value

    return {
        "dimension_ref_id": row.get("id"),
        "dimensions_mm": dims,
        "shape": row.get("shape"),
        "mount_type": row.get("mount_type"),
        "mount_spec": row.get("mount_spec"),
        "dimension_confidence": row.get("dimension_confidence"),
        "source": "curated_table",
    }


def lookup_curated_dimensions(generic_name: str, aliases: list = None) -> dict | None:
    """G1a's entry point. Looks up `generic_name` first, then each of
    `aliases` in order, against the curated table's own generic_name/
    aliases vocabulary -- returns the first hit's full row (reshaped
    per _row_to_match()), or None if nothing matches.

    No network call, no LLM call: a dict lookup, safe to call
    synchronously and cheaply for every part, unlike G1b's per-part
    DigiKey/Mouser round-trip.
    """
    if _TABLE_BY_ID is None:
        _load_table()

    if not _ALIAS_INDEX:
        return None

    candidates = [generic_name] + list(aliases or [])
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        key = _normalize(candidate)
        if not key:
            continue
        row_id = _ALIAS_INDEX.get(key)
        if row_id:
            return _row_to_match(_TABLE_BY_ID[row_id])

    return None
