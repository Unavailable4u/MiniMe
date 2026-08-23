"""
tests/unit/test_eo_node_summaries.py — Patch 7e-2.

eo/node_summaries.py had zero test coverage before this. The one
piece of behavior actually worth pinning down here is the MERGE
semantics of set_summaries(): a concept_linker run scoped to a few
sources must only refresh the nodes it actually read this pass, never
blank out every other node's summary in the workspace (see the
module's own set_summaries() docstring). A regression back to a plain
overwrite would silently wipe the Backlinks graph's node-click detail
for every node outside whatever scope happened to run last.

Isolation: this module reads/writes a real JSON file on disk
(SUMMARIES_PATH), not memory.bus -- tests monkeypatch SUMMARIES_PATH
itself to a path under tmp_path so nothing here ever touches the real
data/graph/_node_summaries.json.
"""
import json

import eo.node_summaries as node_summaries


def _use_tmp_summaries_path(monkeypatch, tmp_path):
    monkeypatch.setattr(node_summaries, "SUMMARIES_PATH", str(tmp_path / "_node_summaries.json"))


# ---------------------------------------------------------------------
# _read / _write
# ---------------------------------------------------------------------

def test_read_returns_empty_dict_when_file_does_not_exist(monkeypatch, tmp_path):
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    assert node_summaries._read() == {}


def test_write_creates_parent_directories_that_dont_exist_yet(monkeypatch, tmp_path):
    nested_path = tmp_path / "nested" / "dir" / "_node_summaries.json"
    monkeypatch.setattr(node_summaries, "SUMMARIES_PATH", str(nested_path))

    node_summaries._write({"ws_1": {"n1": "summary"}})

    assert nested_path.exists()
    assert json.loads(nested_path.read_text()) == {"ws_1": {"n1": "summary"}}


# ---------------------------------------------------------------------
# set_summaries
# ---------------------------------------------------------------------

def test_set_summaries_requires_workspace_id(monkeypatch, tmp_path):
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    try:
        node_summaries.set_summaries("", {"n1": "x"})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_summaries_writes_and_returns_the_full_merged_map(monkeypatch, tmp_path):
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    result = node_summaries.set_summaries("ws_1", {"n1": "first node summary"})
    assert result == {"n1": "first node summary"}
    assert node_summaries.get_summaries("ws_1") == {"n1": "first node summary"}


def test_set_summaries_merges_new_nodes_without_dropping_existing_ones(monkeypatch, tmp_path):
    """The core regression case: a second, scoped run touching only
    n2 must not erase n1's summary from the first run."""
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    node_summaries.set_summaries("ws_1", {"n1": "first summary"})

    result = node_summaries.set_summaries("ws_1", {"n2": "second summary"})

    assert result == {"n1": "first summary", "n2": "second summary"}


def test_set_summaries_overwrites_an_existing_node_when_reprocessed(monkeypatch, tmp_path):
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    node_summaries.set_summaries("ws_1", {"n1": "old summary"})

    result = node_summaries.set_summaries("ws_1", {"n1": "updated summary"})

    assert result == {"n1": "updated summary"}


def test_set_summaries_skips_falsy_summary_values(monkeypatch, tmp_path):
    """A node with an empty/None summary in the incoming batch must not
    overwrite (or create) an entry -- the node keeps its last real
    summary, per the module's own docstring."""
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    node_summaries.set_summaries("ws_1", {"n1": "real summary"})

    result = node_summaries.set_summaries("ws_1", {"n1": "", "n2": None})

    assert result == {"n1": "real summary"}


def test_set_summaries_skips_falsy_node_ids(monkeypatch, tmp_path):
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    result = node_summaries.set_summaries("ws_1", {"": "orphan summary"})
    assert result == {}


def test_set_summaries_with_none_summaries_arg_is_a_no_op_merge(monkeypatch, tmp_path):
    """summaries=None (via the `(summaries or {}).items()` guard) must
    behave like an empty batch, not raise on NoneType.items()."""
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    node_summaries.set_summaries("ws_1", {"n1": "kept summary"})

    result = node_summaries.set_summaries("ws_1", None)

    assert result == {"n1": "kept summary"}


def test_set_summaries_does_not_leak_across_different_workspaces(monkeypatch, tmp_path):
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    node_summaries.set_summaries("ws_1", {"n1": "ws1 summary"})
    node_summaries.set_summaries("ws_2", {"n1": "ws2 summary"})

    assert node_summaries.get_summaries("ws_1") == {"n1": "ws1 summary"}
    assert node_summaries.get_summaries("ws_2") == {"n1": "ws2 summary"}


# ---------------------------------------------------------------------
# get_summaries / get_summary
# ---------------------------------------------------------------------

def test_get_summaries_returns_empty_dict_for_unknown_workspace(monkeypatch, tmp_path):
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    assert node_summaries.get_summaries("nonexistent_ws") == {}


def test_get_summary_returns_none_for_unknown_node(monkeypatch, tmp_path):
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    node_summaries.set_summaries("ws_1", {"n1": "summary"})
    assert node_summaries.get_summary("ws_1", "n_missing") is None


def test_get_summary_returns_the_stored_summary_for_a_known_node(monkeypatch, tmp_path):
    _use_tmp_summaries_path(monkeypatch, tmp_path)
    node_summaries.set_summaries("ws_1", {"n1": "the actual summary text"})
    assert node_summaries.get_summary("ws_1", "n1") == "the actual summary text"
