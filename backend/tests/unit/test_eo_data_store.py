"""
tests/unit/test_eo_data_store.py — Patch C1.

eo/data_store.py is a small memory.bus-backed store, same shape family
as eo/scratchpad.py (B7) and eo/tool_budget.py (B6) — no mocking
needed beyond the autouse fake_bus fixture already used across this
suite. Priorities, worst-silent-failure first:

  1. write_section()'s versioning (1 for a new section, +1 on every
     subsequent write) and that sections/sessions don't bleed into
     each other.
  2. patch_section()'s fail-loud snippet matching (not found, not
     unique) and its optional expected_version optimistic-concurrency
     check — the one thing write_section()'s plain overwrite doesn't
     give you.
  3. list_sections()'s table-of-contents shape (no text) and
     search_sections()'s match cap and invalid-pattern handling.
  4. read_section()'s MAX_READ_BYTES truncation and its fail-loud
     (KeyError) behavior for an unknown section — deliberately
     different from eo/introspection.py's read_file(), which has no
     redaction concern here to justify a denial-shaped return instead.
"""
import pytest

from eo import data_store


def test_write_section_new_section_starts_at_version_1():
    result = data_store.write_section("s1", "intro", "hello", "writer_role")
    assert result == {
        "section_id": "intro",
        "written_by": "writer_role",
        "byte_len": len(b"hello"),
        "version": 1,
    }


def test_write_section_overwrite_bumps_version():
    data_store.write_section("s1", "intro", "v1 text", "writer_role")
    result = data_store.write_section("s1", "intro", "v2 text", "writer_role")
    assert result["version"] == 2
    assert data_store.read_section("s1", "intro") == "v2 text"


def test_sections_are_scoped_per_session():
    data_store.write_section("s1", "intro", "session one text", "role_a")
    data_store.write_section("s2", "intro", "session two text", "role_b")
    assert data_store.read_section("s1", "intro") == "session one text"
    assert data_store.read_section("s2", "intro") == "session two text"


def test_list_sections_empty_for_unknown_session():
    assert data_store.list_sections("never-written") == []


def test_list_sections_has_no_text_field():
    data_store.write_section("s1", "intro", "some content", "writer_role")
    [entry] = data_store.list_sections("s1")
    assert "text" not in entry
    assert entry["section_id"] == "intro"
    assert entry["written_by"] == "writer_role"
    assert entry["byte_len"] == len(b"some content")
    assert entry["version"] == 1


def test_read_section_unknown_section_raises_keyerror():
    data_store.write_section("s1", "intro", "text", "writer_role")
    with pytest.raises(KeyError):
        data_store.read_section("s1", "not_a_real_section")


def test_read_section_unknown_session_raises_keyerror():
    with pytest.raises(KeyError):
        data_store.read_section("never-written", "intro")


def test_read_section_truncates_at_max_read_bytes(monkeypatch):
    monkeypatch.setattr(data_store, "MAX_READ_BYTES", 10)
    data_store.write_section("s1", "big", "x" * 100, "writer_role")
    result = data_store.read_section("s1", "big")
    assert len(result.encode("utf-8")) <= 10


def test_search_sections_finds_matches_across_sections():
    data_store.write_section("s1", "a", "the quick fox\nsecond line", "role")
    data_store.write_section("s1", "b", "another quick line", "role")
    results = data_store.search_sections("s1", "quick")
    assert {"section_id": "a", "match": "the quick fox"} in results
    assert {"section_id": "b", "match": "another quick line"} in results
    assert len(results) == 2


def test_search_sections_no_matches_returns_empty_list():
    data_store.write_section("s1", "a", "nothing relevant here", "role")
    assert data_store.search_sections("s1", "zzz_not_present") == []


def test_search_sections_invalid_pattern_raises_valueerror():
    with pytest.raises(ValueError):
        data_store.search_sections("s1", "[unclosed")


def test_search_sections_caps_at_max_search_matches(monkeypatch):
    monkeypatch.setattr(data_store, "MAX_SEARCH_MATCHES", 3)
    text = "\n".join(f"hit {i}" for i in range(10))
    data_store.write_section("s1", "a", text, "role")
    results = data_store.search_sections("s1", "hit")
    assert len(results) == 3


def test_patch_section_replaces_unique_snippet_and_bumps_version():
    data_store.write_section("s1", "intro", "the quick brown fox", "writer_role")
    result = data_store.patch_section("s1", "intro", "brown", "red")
    assert result["version"] == 2
    assert data_store.read_section("s1", "intro") == "the quick red fox"


def test_patch_section_preserves_written_by():
    data_store.write_section("s1", "intro", "original text", "original_writer")
    result = data_store.patch_section("s1", "intro", "original", "revised")
    assert result["written_by"] == "original_writer"


def test_patch_section_unknown_section_raises_valueerror():
    with pytest.raises(ValueError):
        data_store.patch_section("s1", "not_a_real_section", "old", "new")


def test_patch_section_snippet_not_found_raises_valueerror():
    data_store.write_section("s1", "intro", "some text here", "role")
    with pytest.raises(ValueError):
        data_store.patch_section("s1", "intro", "not present anywhere", "x")


def test_patch_section_snippet_not_unique_raises_valueerror():
    data_store.write_section("s1", "intro", "dup dup dup", "role")
    with pytest.raises(ValueError):
        data_store.patch_section("s1", "intro", "dup", "single")


def test_patch_section_expected_version_mismatch_raises_version_conflict():
    data_store.write_section("s1", "intro", "text", "role")
    with pytest.raises(data_store.VersionConflict):
        data_store.patch_section("s1", "intro", "text", "new text", expected_version=99)


def test_patch_section_expected_version_match_succeeds():
    data_store.write_section("s1", "intro", "text", "role")  # version 1
    result = data_store.patch_section(
        "s1", "intro", "text", "new text", expected_version=1
    )
    assert result["version"] == 2


def test_patch_section_expected_version_none_skips_check():
    data_store.write_section("s1", "intro", "text", "role")
    # No expected_version passed — should succeed regardless of the
    # section's actual current version.
    result = data_store.patch_section("s1", "intro", "text", "new text")
    assert result["version"] == 2
