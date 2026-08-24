"""
tests/unit/test_eo_workspace_facts.py — Patch 7e (content/knowledge group).

eo/workspace_facts.py had zero test coverage before this. It's the
tier-3 (whole-project) layer of the three-tier memory model, and the
highest-value things to pin down are:

  - get_facts()/set_facts() always return/accept the FULL shape, so no
    caller anywhere has to defensively check for a missing key -- a
    regression here silently breaks every reader downstream.
  - record_section_entries()'s upsert-and-merge logic (_entry_key,
    _normalize_entry, _merge_entry, _coerce_section_bucket,
    _merge_sections), which is what keeps repeated mentions of the same
    fact merging into one row instead of piling up as duplicates.
  - The candidate accept/reject path, which the module's own comment
    calls out as a real prior bug fix (audit §9): candidates are
    addressed by a stable `candidate_id`, not list position, because
    two reviewers can be looking at the same pending list at once.
  - _invalidate_facts_cache()'s fail-open contract -- a write to facts
    must succeed even if cache invalidation itself blows up.

Isolation: workspace_facts.py does `from memory.bus import read, write`
(bound names in its own namespace), but unlike the smaller cache
modules in this batch, these tests lean on conftest's autouse
`fake_bus` fixture (a real in-memory Redis stand-in wired under
memory.bus.redis) rather than hand-mocking read/write directly -- the
module's own internal calls to read()/write() (e.g. get_facts() calling
read() inside record_section_entries()) round-trip through real
memory.bus logic this way, which is more faithful to production than
re-stubbing every internal call site by hand would be.
"""
import pytest

from eo import workspace_facts

# ---------------------------------------------------------------------
# _key / _slug
# ---------------------------------------------------------------------

def test_key_is_namespaced_by_workspace_id():
    assert workspace_facts._key("ws-1") == "workspace_facts:ws-1"


def test_slug_lowercases_and_replaces_non_alnum_with_underscores():
    assert workspace_facts._slug("Hello, World!") == "hello_world"


def test_slug_falls_back_to_item_when_nothing_alnum_remains():
    assert workspace_facts._slug("!!!") == "item"


def test_slug_truncates_to_max_len():
    assert len(workspace_facts._slug("a" * 100, max_len=10)) == 10


# ---------------------------------------------------------------------
# get_facts / set_facts — always return/accept the full shape
# ---------------------------------------------------------------------

def test_get_facts_with_no_workspace_id_returns_empty_facts():
    assert workspace_facts.get_facts(None) == workspace_facts.EMPTY_FACTS


def test_get_facts_empty_shape_does_not_alias_the_shared_empty_facts_constant():
    """Regression test: get_facts() for a never-written workspace used
    to return `dict(EMPTY_FACTS)`, a shallow copy that shares its
    nested `custom`/`sections`/`ledger` containers with the
    module-level EMPTY_FACTS constant. update_custom_fact() mutates
    `facts["custom"]` in place, so the FIRST call anywhere in the
    process (for any workspace) used to leak that custom fact into
    every OTHER never-written workspace's "empty" facts for the rest
    of the process's lifetime. Two unrelated, never-touched workspaces
    must never see each other's custom facts."""
    workspace_facts.update_custom_fact("ws-leaky-source", "deploy_target", "vercel")
    facts = workspace_facts.get_facts("ws-never-touched")
    assert facts["custom"] == {}
    assert "deploy_target" not in facts["custom"]


def test_get_facts_for_a_never_written_workspace_returns_the_full_empty_shape():
    facts = workspace_facts.get_facts("brand-new-ws")
    assert facts["brand_voice"] == ""
    assert facts["target_user"] == ""
    assert facts["tech_stack"] == []
    assert facts["custom"] == {}
    assert facts["sections"] == {}
    assert facts["ledger"] == []


def test_set_facts_then_get_facts_round_trips_simple_fields():
    workspace_facts.set_facts("ws-1", {"brand_voice": "playful", "target_user": "students"})
    facts = workspace_facts.get_facts("ws-1")
    assert facts["brand_voice"] == "playful"
    assert facts["target_user"] == "students"


def test_set_facts_requires_a_workspace_id():
    with pytest.raises(ValueError):
        workspace_facts.set_facts(None, {"brand_voice": "x"})


def test_set_facts_merges_custom_dict_rather_than_replacing_it():
    workspace_facts.set_facts("ws-1", {"custom": {"a": 1}})
    workspace_facts.set_facts("ws-1", {"custom": {"b": 2}})
    facts = workspace_facts.get_facts("ws-1")
    assert facts["custom"] == {"a": 1, "b": 2}


def test_set_facts_preserves_unknown_top_level_keys():
    """Unknown keys must round-trip as-is, per the module's own
    docstring, so a future fact type doesn't need this module changed
    just to be stored."""
    workspace_facts.set_facts("ws-1", {"future_field": "some value"})
    facts = workspace_facts.get_facts("ws-1")
    assert facts["future_field"] == "some value"


def test_set_facts_ignores_none_values_for_known_fields():
    workspace_facts.set_facts("ws-1", {"brand_voice": "playful"})
    workspace_facts.set_facts("ws-1", {"brand_voice": None})
    facts = workspace_facts.get_facts("ws-1")
    assert facts["brand_voice"] == "playful"


# ---------------------------------------------------------------------
# update_custom_fact
# ---------------------------------------------------------------------

def test_update_custom_fact_sets_a_single_key_without_touching_others():
    workspace_facts.set_facts("ws-1", {"brand_voice": "playful", "custom": {"a": 1}})
    workspace_facts.update_custom_fact("ws-1", "deploy_target", "vercel")
    facts = workspace_facts.get_facts("ws-1")
    assert facts["custom"]["deploy_target"] == "vercel"
    assert facts["custom"]["a"] == 1
    assert facts["brand_voice"] == "playful"


def test_update_custom_fact_requires_workspace_id_and_key():
    with pytest.raises(ValueError):
        workspace_facts.update_custom_fact(None, "k", "v")
    with pytest.raises(ValueError):
        workspace_facts.update_custom_fact("ws-1", "", "v")


# ---------------------------------------------------------------------
# record_section_entries / _entry_key / _normalize_entry / _merge_entry
# ---------------------------------------------------------------------

def test_record_section_entries_requires_workspace_id_and_section():
    with pytest.raises(ValueError):
        workspace_facts.record_section_entries(None, "decisions", [{"title": "x"}])
    with pytest.raises(ValueError):
        workspace_facts.record_section_entries("ws-1", "", [{"title": "x"}])


def test_record_section_entries_adds_a_new_entry_under_the_section():
    facts = workspace_facts.record_section_entries(
        "ws-1", "decisions", [{"title": "Use TypeScript", "summary": "Always use TypeScript."}],
        source="fact_summarizer",
    )
    bucket = facts["sections"]["decisions"]
    assert len(bucket["order"]) == 1
    key = bucket["order"][0]
    assert bucket["entries"][key]["title"] == "Use TypeScript"
    assert bucket["entries"][key]["source"] == "fact_summarizer"
    assert bucket["entries"][key]["touch_count"] == 1


def test_record_section_entries_merges_repeated_entries_by_title_slug_instead_of_duplicating():
    """The whole point of _entry_key() falling back to a slugified
    title is so restating the same fact merges into the SAME row
    instead of piling up duplicates -- per fact_summarizer.py's own
    'title... so repeated statements... merge instead of piling up'
    comment."""
    workspace_facts.record_section_entries("ws-1", "decisions", [{"title": "Use TypeScript", "summary": "v1"}])
    facts = workspace_facts.record_section_entries("ws-1", "decisions", [{"title": "Use TypeScript", "summary": "v2"}])
    bucket = facts["sections"]["decisions"]
    assert len(bucket["order"]) == 1
    key = bucket["order"][0]
    assert bucket["entries"][key]["summary"] == "v2"
    assert bucket["entries"][key]["touch_count"] == 2


def test_record_section_entries_appends_a_ledger_entry_per_touch():
    workspace_facts.record_section_entries("ws-1", "decisions", [{"title": "A", "summary": "a"}])
    facts = workspace_facts.record_section_entries("ws-1", "decisions", [{"title": "A", "summary": "a2"}])
    assert len(facts["ledger"]) == 2
    assert all(entry["section"] == "decisions" for entry in facts["ledger"])


def test_record_section_entry_singular_is_equivalent_to_a_one_item_list():
    facts_a = workspace_facts.record_section_entry("ws-1", "ideas", {"title": "Idea A", "summary": "s"})
    facts_b = workspace_facts.record_section_entries("ws-2", "ideas", [{"title": "Idea A", "summary": "s"}])
    key_a = facts_a["sections"]["ideas"]["order"][0]
    key_b = facts_b["sections"]["ideas"]["order"][0]
    assert facts_a["sections"]["ideas"]["entries"][key_a]["title"] == facts_b["sections"]["ideas"]["entries"][key_b]["title"]


def test_entry_key_prefers_an_explicit_key_over_a_derived_one():
    key = workspace_facts._entry_key("decisions", {"key": "explicit-key", "title": "Something"})
    assert key == "explicit-key"


def test_normalize_entry_deduplicates_identical_sources():
    normalized = workspace_facts._normalize_entry(
        "decisions",
        {"title": "A", "summary": "s", "sources": [{"source": "task_runner", "source_ref": "task-1"}]},
        source="task_runner", source_ref="task-1",
    )
    assert normalized["sources"] == [{"source": "task_runner", "source_ref": "task-1"}]


def test_merge_entry_increments_touch_count_and_keeps_first_seen_at():
    existing = workspace_facts._normalize_entry("decisions", {"title": "A", "summary": "v1"})
    incoming = workspace_facts._normalize_entry("decisions", {"title": "A", "summary": "v2"})
    merged = workspace_facts._merge_entry(existing, incoming)
    assert merged["summary"] == "v2"
    assert merged["touch_count"] == 2
    assert merged["first_seen_at"] == existing["first_seen_at"]


def test_merge_entry_combines_dict_data_fields_rather_than_replacing():
    existing = workspace_facts._normalize_entry("hardware", {"title": "MCU", "data": {"vendor": "Espressif"}})
    incoming = workspace_facts._normalize_entry("hardware", {"title": "MCU", "data": {"model": "ESP32"}})
    merged = workspace_facts._merge_entry(existing, incoming)
    assert merged["data"] == {"vendor": "Espressif", "model": "ESP32"}


# ---------------------------------------------------------------------
# _coerce_section_bucket — accepts list / {"entries": ...} / plain dict
# ---------------------------------------------------------------------

def test_coerce_section_bucket_handles_a_list_of_dicts():
    bucket = workspace_facts._coerce_section_bucket(
        [{"title": "A", "summary": "a"}, {"title": "B", "summary": "b"}], "decisions",
    )
    assert len(bucket["order"]) == 2


def test_coerce_section_bucket_handles_an_entries_dict_shape():
    bucket = workspace_facts._coerce_section_bucket(
        {"entries": {"k1": {"key": "k1", "title": "A"}}, "order": ["k1"]}, "decisions",
    )
    assert bucket["order"] == ["k1"]
    assert bucket["entries"]["k1"]["title"] == "A"


def test_coerce_section_bucket_handles_falsy_input_as_empty():
    assert workspace_facts._coerce_section_bucket(None, "decisions") == {"entries": {}, "order": []}
    assert workspace_facts._coerce_section_bucket({}, "decisions") == {"entries": {}, "order": []}


def test_coerce_section_bucket_preserves_explicit_order_over_insertion_order():
    bucket = workspace_facts._coerce_section_bucket(
        {"entries": {"k1": {"key": "k1", "title": "A"}, "k2": {"key": "k2", "title": "B"}},
         "order": ["k2", "k1"]},
        "decisions",
    )
    assert bucket["order"] == ["k2", "k1"]


# ---------------------------------------------------------------------
# format_facts_for_prompt
# ---------------------------------------------------------------------

def test_format_facts_for_prompt_returns_empty_string_for_a_blank_workspace():
    assert workspace_facts.format_facts_for_prompt("brand-new-ws") == ""


def test_format_facts_for_prompt_includes_brand_voice_and_target_user():
    workspace_facts.set_facts("ws-1", {"brand_voice": "playful", "target_user": "students"})
    rendered = workspace_facts.format_facts_for_prompt("ws-1")
    assert "Brand voice: playful" in rendered
    assert "Target user: students" in rendered


def test_format_facts_for_prompt_renders_sections_in_project_section_order():
    workspace_facts.record_section_entries("ws-1", "ideas", [{"title": "Idea A", "summary": "s"}])
    workspace_facts.record_section_entries("ws-1", "decisions", [{"title": "Decision A", "summary": "s"}])
    rendered = workspace_facts.format_facts_for_prompt("ws-1")
    assert rendered.index("[decisions]") < rendered.index("[ideas]")


def test_format_facts_for_prompt_includes_the_recent_ledger_tail():
    workspace_facts.record_section_entries("ws-1", "decisions", [{"title": "A", "summary": "s"}])
    rendered = workspace_facts.format_facts_for_prompt("ws-1")
    assert "Timeline:" in rendered


# ---------------------------------------------------------------------
# propose_fact / list_candidates / accept_candidate / reject_candidate
# ---------------------------------------------------------------------

def test_propose_fact_requires_workspace_id_and_key():
    with pytest.raises(ValueError):
        workspace_facts.propose_fact(None, "k", "v", "some_agent")
    with pytest.raises(ValueError):
        workspace_facts.propose_fact("ws-1", "", "v", "some_agent")


def test_propose_fact_appends_a_candidate_with_a_stable_id():
    candidates = workspace_facts.propose_fact("ws-1", "deploy_target", "vercel", "some_agent")
    assert len(candidates) == 1
    assert candidates[0]["key"] == "deploy_target"
    assert candidates[0]["value"] == "vercel"
    assert candidates[0]["candidate_id"].startswith("fact_")


def test_list_candidates_returns_empty_list_for_a_workspace_with_none_proposed():
    assert workspace_facts.list_candidates("brand-new-ws") == []


def test_accept_candidate_is_addressed_by_candidate_id_not_list_position():
    """Regression pin for bug audit §9: accept/reject must resolve a
    candidate by its stable candidate_id, not its index in the list --
    otherwise a second reviewer's accept/reject can silently act on a
    DIFFERENT candidate than the one they actually looked at, if the
    list has changed since they loaded it (e.g. another reviewer
    already resolved an earlier entry)."""
    workspace_facts.propose_fact("ws-1", "key_a", "value_a", "agent_1")
    candidates = workspace_facts.propose_fact("ws-1", "key_b", "value_b", "agent_1")
    target_id = candidates[1]["candidate_id"]

    # Simulate a concurrent reviewer removing the first candidate from
    # the pending list before this accept_candidate() call resolves --
    # position-based addressing would now act on the wrong item.
    workspace_facts.reject_candidate("ws-1", candidates[0]["candidate_id"])

    facts = workspace_facts.accept_candidate("ws-1", target_id)
    assert facts["custom"]["key_b"] == "value_b"
    assert "key_a" not in facts["custom"]


def test_accept_candidate_removes_it_from_the_pending_list_either_way():
    candidates = workspace_facts.propose_fact("ws-1", "key_a", "value_a", "agent_1")
    workspace_facts.accept_candidate("ws-1", candidates[0]["candidate_id"])
    assert workspace_facts.list_candidates("ws-1") == []


def test_accept_candidate_raises_for_an_unknown_candidate_id():
    with pytest.raises(FileNotFoundError):
        workspace_facts.accept_candidate("ws-1", "does-not-exist")


def test_reject_candidate_removes_it_without_writing_to_custom():
    candidates = workspace_facts.propose_fact("ws-1", "key_a", "value_a", "agent_1")
    workspace_facts.reject_candidate("ws-1", candidates[0]["candidate_id"])
    assert workspace_facts.list_candidates("ws-1") == []
    facts = workspace_facts.get_facts("ws-1")
    assert "key_a" not in facts["custom"]


def test_reject_candidate_raises_for_an_unknown_candidate_id():
    with pytest.raises(FileNotFoundError):
        workspace_facts.reject_candidate("ws-1", "does-not-exist")


# ---------------------------------------------------------------------
# _invalidate_facts_cache — fail-open
# ---------------------------------------------------------------------

def test_set_facts_still_succeeds_when_cache_invalidation_raises(monkeypatch):
    """A fact write must never be blocked by cache-invalidation failing
    -- fire-and-forget, per the module's own docstring for
    _invalidate_facts_cache()."""
    from eo import semantic_cache

    def boom(text, workspace_id=None):
        raise RuntimeError("Vector unavailable")

    monkeypatch.setattr(semantic_cache, "invalidate_cache", boom)

    workspace_facts.set_facts("ws-1", {"brand_voice": "playful"})
    facts = workspace_facts.get_facts("ws-1")
    assert facts["brand_voice"] == "playful"


def test_update_custom_fact_still_succeeds_when_cache_invalidation_raises(monkeypatch):
    from eo import semantic_cache

    def boom(text, workspace_id=None):
        raise RuntimeError("Vector unavailable")

    monkeypatch.setattr(semantic_cache, "invalidate_cache", boom)

    workspace_facts.update_custom_fact("ws-1", "deploy_target", "vercel")
    facts = workspace_facts.get_facts("ws-1")
    assert facts["custom"]["deploy_target"] == "vercel"
