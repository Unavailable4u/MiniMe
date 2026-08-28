"""
tests/unit/test_eo_role_scope.py — Patch B3.

Covers the two new eo/capabilities.py functions Patch B3 adds
(get_role_scope(), capabilities_for_role()) and the additive
capability_tags field they read from eo/registry.py's per-role
metadata store. Uses the autouse `fake_bus` / `_reset_role_prompts_cache`
fixtures from tests/conftest.py, same as test_eo_registry_role_library.py.
"""
from eo import capabilities
from eo.registry import (
    add_role_prompt,
    get_role_metadata,
    record_role_hire,
    set_role_capability_tags,
    set_role_pinned,
)

# ---------------------------------------------------------------------------
# eo/registry.py — capability_tags is additive, defaults to []
# ---------------------------------------------------------------------------

def test_seeded_role_has_empty_capability_tags_by_default():
    entry = get_role_metadata("implementer")
    assert entry["capability_tags"] == []


def test_set_role_capability_tags_on_a_seeded_role():
    entry = set_role_capability_tags("researcher", ["frontend_capabilities"])
    assert entry["capability_tags"] == ["frontend_capabilities"]
    assert get_role_metadata("researcher")["capability_tags"] == ["frontend_capabilities"]


def test_set_role_capability_tags_on_a_role_never_briefed_creates_a_bare_entry():
    entry = set_role_capability_tags("brand_new_role", ["agent_roster"])
    assert entry["capability_tags"] == ["agent_roster"]
    assert entry["brief"] is None
    assert get_role_metadata("brand_new_role")["capability_tags"] == ["agent_roster"]


def test_set_role_capability_tags_replaces_wholesale_not_merges():
    set_role_capability_tags("writer", ["a", "b"])
    entry = set_role_capability_tags("writer", ["c"])
    assert entry["capability_tags"] == ["c"]


def test_capability_tags_survive_a_brief_rewrite():
    set_role_capability_tags("writer", ["frontend_capabilities"])
    add_role_prompt("writer", "A rewritten brief.", source="user_edited")
    assert get_role_metadata("writer")["capability_tags"] == ["frontend_capabilities"]


def test_capability_tags_survive_record_role_hire():
    set_role_capability_tags("writer", ["agent_roster"])
    record_role_hire("writer")
    entry = get_role_metadata("writer")
    assert entry["capability_tags"] == ["agent_roster"]
    assert entry["times_hired"] == 1


def test_capability_tags_survive_set_role_pinned():
    set_role_capability_tags("writer", ["mcp_capabilities"])
    set_role_pinned("writer", True)
    entry = get_role_metadata("writer")
    assert entry["capability_tags"] == ["mcp_capabilities"]
    assert entry["pinned"] is True


def test_legacy_dict_entry_missing_capability_tags_is_migrated_in_place(monkeypatch):
    # Simulate an entry written before Patch B3 -- a fully-migrated
    # Part 2 §2.2 dict shape, but with no capability_tags key at all.
    import eo.registry as registry_module

    key = registry_module._role_prompts_key()
    pre_b3_entry = {
        "brief": "An old brief.", "source": "user_edited",
        "updated_at": None, "times_hired": 3,
        "pinned": False, "pinned_at": None,
    }
    registry_module.write(key, {"legacy_role": pre_b3_entry})
    monkeypatch.setattr(registry_module, "_role_prompts_cache_ctx",
                         registry_module.contextvars.ContextVar("t", default=None))

    entry = get_role_metadata("legacy_role")
    assert entry["capability_tags"] == []
    assert entry["times_hired"] == 3  # untouched by the migration


# ---------------------------------------------------------------------------
# eo/capabilities.py::get_role_scope()
# ---------------------------------------------------------------------------

def test_get_role_scope_returns_configured_tags():
    set_role_capability_tags("researcher", ["frontend_capabilities", "agent_roster"])
    assert set(capabilities.get_role_scope("researcher")) == {
        "frontend_capabilities", "agent_roster",
    }


def test_get_role_scope_returns_empty_for_role_with_no_tags_set():
    assert capabilities.get_role_scope("implementer") == []


def test_get_role_scope_returns_empty_for_unknown_role():
    assert capabilities.get_role_scope("does_not_exist") == []


# ---------------------------------------------------------------------------
# eo/capabilities.py::capabilities_for_role()
# ---------------------------------------------------------------------------

def test_capabilities_for_role_returns_only_matching_tagged_entries():
    set_role_capability_tags("researcher", ["frontend_capabilities"])

    results = capabilities.capabilities_for_role("researcher")

    assert [e["entry_id"] for e in results] == ["frontend_renderable_tabs"]


def test_capabilities_for_role_union_across_multiple_tags():
    set_role_capability_tags("researcher", ["agent_roster", "mcp_capabilities"])

    results = capabilities.capabilities_for_role("researcher")

    assert {e["entry_id"] for e in results} == {"agent_roster", "mcp_capabilities"}


def test_capabilities_for_role_returns_empty_for_untagged_role():
    # No tags configured -- must NOT fall back to "see everything."
    assert capabilities.capabilities_for_role("implementer") == []


def test_capabilities_for_role_returns_empty_for_unknown_role():
    assert capabilities.capabilities_for_role("does_not_exist") == []


def test_capabilities_for_role_never_surfaces_redaction_entries():
    from eo import capability_entries

    capability_entries.write_capability_entry(
        title="Never read backend/.env",
        doc_text="Do not open the backend .env file.",
        tags=["frontend_capabilities"],
        entry_type="redaction",
    )
    set_role_capability_tags("researcher", ["frontend_capabilities"])

    results = capabilities.capabilities_for_role("researcher")

    assert all(e["entry_id"] != "never_read_backend_env" for e in results)
    assert [e["entry_id"] for e in results] == ["frontend_renderable_tabs"]
