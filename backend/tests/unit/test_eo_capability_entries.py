"""
tests/unit/test_eo_capability_entries.py — Patch B1.

eo/capability_entries.py is a second, separate store from
eo/skill_library.py's (see that module's own docstring for why) — no
Vector/embedding involved, so unlike test_eo_skill_library.py this
file needs no embed_text/vector_index patching. `read`/`write` route
through fake_bus (conftest, autouse) transparently, same as every
other registry:-prefixed store's tests in this suite.
"""
from eo import capability_entries


def test_seed_bootstraps_on_first_read():
    entries = capability_entries._load_entries()
    assert set(entries) == set(capability_entries.CAPABILITY_SEED)
    for entry_id in capability_entries.CAPABILITY_SEED:
        assert entries[entry_id]["updated_at"] is None


def test_list_capability_entries_defaults_to_capability_type():
    results = capability_entries.list_capability_entries()
    assert len(results) == len(capability_entries.CAPABILITY_SEED)
    assert all(e["entry_type"] == "capability" for e in results)


def test_list_capability_entries_filters_by_tag():
    results = capability_entries.list_capability_entries(tags=["agent_roster"])
    assert [e["entry_id"] for e in results] == ["agent_roster"]


def test_list_capability_entries_unknown_tag_returns_empty():
    assert capability_entries.list_capability_entries(tags=["not_a_real_tag"]) == []


def test_list_capability_entries_multiple_tags_is_union_not_intersection():
    results = capability_entries.list_capability_entries(
        tags=["agent_roster", "mcp_capabilities"]
    )
    assert {e["entry_id"] for e in results} == {"agent_roster", "mcp_capabilities"}


def test_get_capability_entry_returns_full_record():
    entry = capability_entries.get_capability_entry("frontend_renderable_tabs")
    assert entry["entry_id"] == "frontend_renderable_tabs"
    assert entry["title"] == "What the frontend can render"
    assert "frontend_capabilities" in entry["tags"]


def test_get_capability_entry_missing_id_returns_none():
    assert capability_entries.get_capability_entry("does-not-exist") is None


def test_write_capability_entry_is_readable_afterward():
    entry_id = capability_entries.write_capability_entry(
        title="A brand new capability",
        doc_text="Describes something new the system can do.",
        tags=["frontend_capabilities"],
    )
    entry = capability_entries.get_capability_entry(entry_id)
    assert entry["title"] == "A brand new capability"
    assert entry["tags"] == ["frontend_capabilities"]
    assert entry["entry_type"] == "capability"
    assert entry["updated_at"] is not None


def test_write_capability_entry_same_title_updates_in_place():
    id1 = capability_entries.write_capability_entry(
        title="Same Title", doc_text="First version.", tags=["a"]
    )
    id2 = capability_entries.write_capability_entry(
        title="Same Title", doc_text="Second version.", tags=["b"]
    )
    assert id1 == id2
    entry = capability_entries.get_capability_entry(id1)
    assert entry["doc"] == "Second version."
    assert entry["tags"] == ["b"]


def test_write_capability_entry_supports_other_entry_types():
    # Patch B2 reuses this same store/write path for "redaction" entries
    # — pin that the entry_type is stored and filtered on correctly,
    # since B2 depends on this behavior existing already.
    entry_id = capability_entries.write_capability_entry(
        title="Never read credentials.json",
        doc_text="Do not open or summarize any credentials file.",
        tags=["secrets"],
        entry_type="redaction",
        source="hand_written",
    )
    assert capability_entries.get_capability_entry(entry_id)["entry_type"] == "redaction"
    # Default list_capability_entries() call must NOT surface it.
    capability_ids = {e["entry_id"] for e in capability_entries.list_capability_entries()}
    assert entry_id not in capability_ids
    # But an explicit entry_type="redaction" lookup does.
    redaction_ids = {
        e["entry_id"]
        for e in capability_entries.list_capability_entries(entry_type="redaction")
    }
    assert entry_id in redaction_ids
