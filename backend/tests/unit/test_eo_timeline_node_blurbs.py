"""
tests/unit/test_eo_timeline_node_blurbs.py — Patch 7e-2.

eo/timeline_node_blurbs.py had zero test coverage before this. Same
JSON-file-on-disk shape as eo/node_summaries.py, but with a different
merge contract worth its own coverage: DEFAULT_BLURBS must always be
present as a fallback (a fresh checkout has real content on first
request), stored overrides win over defaults, and a kind never
explicitly overridden still gets its default rather than disappearing
-- exactly the behavior the module's own _read() comment describes.

Isolation: same as node_summaries.py -- this reads/writes a real JSON
file (BLURBS_PATH), not memory.bus. Tests monkeypatch BLURBS_PATH to a
path under tmp_path so nothing here ever touches the real
data/timeline/_node_blurbs.json.
"""
import json

import eo.timeline_node_blurbs as timeline_node_blurbs


def _use_tmp_blurbs_path(monkeypatch, tmp_path):
    monkeypatch.setattr(timeline_node_blurbs, "BLURBS_PATH", str(tmp_path / "_node_blurbs.json"))


# ---------------------------------------------------------------------
# _read — defaults, no file yet
# ---------------------------------------------------------------------

def test_read_returns_default_blurbs_when_file_does_not_exist(monkeypatch, tmp_path):
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    assert timeline_node_blurbs._read() == timeline_node_blurbs.DEFAULT_BLURBS


# ---------------------------------------------------------------------
# get_blurbs / get_blurb
# ---------------------------------------------------------------------

def test_get_blurbs_on_a_fresh_checkout_has_real_default_content(monkeypatch, tmp_path):
    """A fresh checkout with no stored file yet must still return real
    blurb text for every default kind, not an empty store."""
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    result = timeline_node_blurbs.get_blurbs()
    assert result["cache_hit"] == timeline_node_blurbs.DEFAULT_BLURBS["cache_hit"]
    assert result["__input__"]
    assert result["__output__"]


def test_get_blurb_returns_none_for_an_unknown_kind(monkeypatch, tmp_path):
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    assert timeline_node_blurbs.get_blurb("some_unmapped_role") is None


def test_get_blurb_returns_the_default_for_a_default_kind(monkeypatch, tmp_path):
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    assert timeline_node_blurbs.get_blurb("worker_pool_selection") == \
        timeline_node_blurbs.DEFAULT_BLURBS["worker_pool_selection"]


# ---------------------------------------------------------------------
# set_blurb
# ---------------------------------------------------------------------

def test_set_blurb_requires_kind(monkeypatch, tmp_path):
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    try:
        timeline_node_blurbs.set_blurb("", "some blurb")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_blurb_adds_an_override_for_a_new_kind(monkeypatch, tmp_path):
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    result = timeline_node_blurbs.set_blurb("new_custom_role", "A custom step description.")
    assert result["new_custom_role"] == "A custom step description."
    assert timeline_node_blurbs.get_blurb("new_custom_role") == "A custom step description."


def test_set_blurb_overrides_a_default_blurb(monkeypatch, tmp_path):
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    timeline_node_blurbs.set_blurb("cache_hit", "Custom override text.")
    assert timeline_node_blurbs.get_blurb("cache_hit") == "Custom override text."


def test_set_blurb_a_stored_override_persists_across_separate_reads(monkeypatch, tmp_path):
    """Stored overrides must win over defaults on a fresh _read(), not
    just within the same in-memory call."""
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    timeline_node_blurbs.set_blurb("cache_hit", "Persisted override.")

    # A brand-new call path -- get_blurbs() -> _read() -> re-opens the file.
    assert timeline_node_blurbs.get_blurbs()["cache_hit"] == "Persisted override."


def test_set_blurb_falsy_blurb_reverts_a_default_kind_to_its_default(monkeypatch, tmp_path):
    """Regression coverage for the module's own set_blurb() docstring:
    clearing an override on a kind that IS a default must fall back to
    DEFAULT_BLURBS, not disappear or return an empty string."""
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    timeline_node_blurbs.set_blurb("cache_hit", "Custom override text.")

    timeline_node_blurbs.set_blurb("cache_hit", "")

    assert timeline_node_blurbs.get_blurb("cache_hit") == \
        timeline_node_blurbs.DEFAULT_BLURBS["cache_hit"]


def test_set_blurb_falsy_blurb_on_a_non_default_kind_removes_it_entirely(monkeypatch, tmp_path):
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    timeline_node_blurbs.set_blurb("custom_role", "Some text.")

    timeline_node_blurbs.set_blurb("custom_role", None)

    assert timeline_node_blurbs.get_blurb("custom_role") is None


def test_set_blurb_does_not_disturb_other_stored_overrides(monkeypatch, tmp_path):
    _use_tmp_blurbs_path(monkeypatch, tmp_path)
    timeline_node_blurbs.set_blurb("role_a", "Blurb A.")
    timeline_node_blurbs.set_blurb("role_b", "Blurb B.")

    timeline_node_blurbs.set_blurb("role_a", "Updated blurb A.")

    assert timeline_node_blurbs.get_blurb("role_a") == "Updated blurb A."
    assert timeline_node_blurbs.get_blurb("role_b") == "Blurb B."


# ---------------------------------------------------------------------
# _write
# ---------------------------------------------------------------------

def test_write_creates_parent_directories_that_dont_exist_yet(monkeypatch, tmp_path):
    nested_path = tmp_path / "nested" / "dir" / "_node_blurbs.json"
    monkeypatch.setattr(timeline_node_blurbs, "BLURBS_PATH", str(nested_path))

    timeline_node_blurbs._write({"cache_hit": "override"})

    assert nested_path.exists()
    assert json.loads(nested_path.read_text()) == {"cache_hit": "override"}
