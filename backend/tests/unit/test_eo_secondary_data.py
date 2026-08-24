"""
tests/unit/test_eo_secondary_data.py — Patch 7e-S4.

eo/secondary_data.py had zero test coverage before this. Priorities,
worst-silent-failure first:

  1. apply_patch()'s all-or-nothing contract: a bad op partway through
     a batch must never leave the on-disk document partially mutated —
     the whole point of validating against a working copy first.
  2. The three supported path shapes (/topics/<id>, the instances
     append, /connections/<idx|->) and their op restrictions (instances
     only ever "add", connections "add" only supports "-").
  3. get_secondary_data()'s dangling-connection filter (read-time only,
     never rewrites the stored doc) and get_secondary_data_scoped()'s
     scope="chat" filtering by originating session_id, including that
     an unattributable topic (no source_section_ids at all) never
     defaults to in-scope.

Isolation follows test_eo_node_summaries.py's convention: this module
reads/writes a real JSON file on disk (SECONDARY_DATA_PATH), so tests
monkeypatch that path to a location under tmp_path rather than ever
touching data/graph/_secondary_data.json.
"""
import json

import pytest

from eo import secondary_data


def _use_tmp_path(monkeypatch, tmp_path):
    monkeypatch.setattr(secondary_data, "SECONDARY_DATA_PATH",
                         str(tmp_path / "_secondary_data.json"))


def _seed(monkeypatch, tmp_path, workspace_id, doc):
    _use_tmp_path(monkeypatch, tmp_path)
    secondary_data._write({workspace_id: doc})


# ---------------------------------------------------------------------
# _read / _write
# ---------------------------------------------------------------------

def test_read_returns_empty_dict_when_file_does_not_exist(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    assert secondary_data._read() == {}


def test_write_creates_parent_directories(monkeypatch, tmp_path):
    nested = tmp_path / "nested" / "dir" / "_secondary_data.json"
    monkeypatch.setattr(secondary_data, "SECONDARY_DATA_PATH", str(nested))
    secondary_data._write({"ws_1": {"topics": {}, "connections": []}})
    assert nested.exists()
    assert json.loads(nested.read_text())["ws_1"]["topics"] == {}


# ---------------------------------------------------------------------
# get_secondary_data
# ---------------------------------------------------------------------

def test_get_secondary_data_requires_workspace_id(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.get_secondary_data("")


def test_get_secondary_data_returns_empty_skeleton_for_unknown_workspace(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    result = secondary_data.get_secondary_data("ws_missing")
    assert result == {"topics": {}, "connections": []}


def test_get_secondary_data_empty_skeleton_is_not_the_shared_module_constant(monkeypatch, tmp_path):
    """A caller mutating the returned dict must never corrupt the next
    empty-workspace read."""
    _use_tmp_path(monkeypatch, tmp_path)
    result = secondary_data.get_secondary_data("ws_missing")
    result["topics"]["polluted"] = {"name": "should not leak"}
    result2 = secondary_data.get_secondary_data("ws_missing")
    assert result2 == {"topics": {}, "connections": []}


def test_get_secondary_data_returns_saved_doc(monkeypatch, tmp_path):
    doc = {"topics": {"t1": {"name": "Topic 1"}}, "connections": []}
    _seed(monkeypatch, tmp_path, "ws_1", doc)
    result = secondary_data.get_secondary_data("ws_1")
    assert result["topics"]["t1"]["name"] == "Topic 1"


def test_get_secondary_data_drops_connections_referencing_missing_topics(monkeypatch, tmp_path):
    doc = {
        "topics": {"t1": {"name": "Topic 1"}},
        "connections": [
            {"from_topic": "t1", "to_topic": "t_ghost", "relation": "prerequisite-of"},
            {"from_topic": "t_ghost", "to_topic": "t1", "relation": "elaborates-on"},
        ],
    }
    _seed(monkeypatch, tmp_path, "ws_1", doc)
    result = secondary_data.get_secondary_data("ws_1")
    assert result["connections"] == []


def test_get_secondary_data_keeps_connections_whose_endpoints_both_resolve(monkeypatch, tmp_path):
    doc = {
        "topics": {"t1": {"name": "T1"}, "t2": {"name": "T2"}},
        "connections": [{"from_topic": "t1", "to_topic": "t2", "relation": "elaborates-on"}],
    }
    _seed(monkeypatch, tmp_path, "ws_1", doc)
    result = secondary_data.get_secondary_data("ws_1")
    assert len(result["connections"]) == 1


def test_get_secondary_data_dangling_filter_never_rewrites_the_stored_file(monkeypatch, tmp_path):
    doc = {
        "topics": {"t1": {"name": "T1"}},
        "connections": [{"from_topic": "t1", "to_topic": "t_ghost", "relation": "x"}],
    }
    _seed(monkeypatch, tmp_path, "ws_1", doc)
    secondary_data.get_secondary_data("ws_1")  # triggers the filter on read
    raw = secondary_data._read()
    assert raw["ws_1"]["connections"] == [
        {"from_topic": "t1", "to_topic": "t_ghost", "relation": "x"}
    ]  # untouched on disk


# ---------------------------------------------------------------------
# get_secondary_data_scoped
# ---------------------------------------------------------------------

def test_get_secondary_data_scoped_project_is_unfiltered(monkeypatch, tmp_path):
    doc = {"topics": {"t1": {"name": "T1"}}, "connections": []}
    _seed(monkeypatch, tmp_path, "ws_1", doc)
    result = secondary_data.get_secondary_data_scoped("ws_1", "project")
    assert "t1" in result["topics"]


def test_get_secondary_data_scoped_rejects_unknown_scope(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.get_secondary_data_scoped("ws_1", "galaxy")


def test_get_secondary_data_scoped_chat_requires_session_id(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.get_secondary_data_scoped("ws_1", "chat")


def test_get_secondary_data_scoped_chat_filters_by_originating_session(monkeypatch, tmp_path):
    doc = {
        "topics": {
            "t1": {"name": "T1", "source_section_ids": ["sec_a"]},
            "t2": {"name": "T2", "source_section_ids": ["sec_b"]},
        },
        "connections": [{"from_topic": "t1", "to_topic": "t2", "relation": "elaborates-on"}],
    }
    _seed(monkeypatch, tmp_path, "ws_1", doc)
    monkeypatch.setattr(
        "eo.knowledge_graph.list_nodes",
        lambda ws_id: [
            {"node_id": "sec_a", "session_id": "sess_1"},
            {"node_id": "sec_b", "session_id": "sess_2"},
        ],
    )

    result = secondary_data.get_secondary_data_scoped("ws_1", "chat", session_id="sess_1")

    assert set(result["topics"].keys()) == {"t1"}
    assert result["connections"] == []  # t2 endpoint out of scope


def test_get_secondary_data_scoped_chat_topic_with_no_sections_is_never_in_scope(monkeypatch, tmp_path):
    doc = {
        "topics": {"t1": {"name": "T1", "source_section_ids": []}},
        "connections": [],
    }
    _seed(monkeypatch, tmp_path, "ws_1", doc)
    monkeypatch.setattr("eo.knowledge_graph.list_nodes", lambda ws_id: [])

    result = secondary_data.get_secondary_data_scoped("ws_1", "chat", session_id="sess_1")

    assert result["topics"] == {}


def test_get_secondary_data_scoped_chat_keeps_connection_only_when_both_endpoints_in_scope(monkeypatch, tmp_path):
    doc = {
        "topics": {
            "t1": {"name": "T1", "source_section_ids": ["sec_a"]},
            "t2": {"name": "T2", "source_section_ids": ["sec_a"]},
        },
        "connections": [{"from_topic": "t1", "to_topic": "t2", "relation": "elaborates-on"}],
    }
    _seed(monkeypatch, tmp_path, "ws_1", doc)
    monkeypatch.setattr(
        "eo.knowledge_graph.list_nodes",
        lambda ws_id: [{"node_id": "sec_a", "session_id": "sess_1"}],
    )

    result = secondary_data.get_secondary_data_scoped("ws_1", "chat", session_id="sess_1")

    assert len(result["connections"]) == 1


# ---------------------------------------------------------------------
# apply_patch — path parsing / op validation
# ---------------------------------------------------------------------

def test_apply_patch_requires_workspace_id(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.apply_patch("", [{"op": "add", "path": "/topics/t1", "value": {}}])


def test_apply_patch_requires_non_empty_ops(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [])


def test_apply_patch_rejects_unsupported_op_type(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [{"op": "move", "path": "/topics/t1"}])


def test_apply_patch_rejects_missing_path(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [{"op": "add", "value": {}}])


def test_apply_patch_rejects_unrecognized_top_level_collection(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [{"op": "add", "path": "/bogus/x", "value": {}}])


# ---------------------------------------------------------------------
# apply_patch — /topics/<id>
# ---------------------------------------------------------------------

def test_apply_patch_add_topic_creates_a_new_entry(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    result = secondary_data.apply_patch("ws_1", [
        {"op": "add", "path": "/topics/t1", "value": {"name": "Topic 1"}},
    ])
    assert result["topics"]["t1"]["name"] == "Topic 1"


def test_apply_patch_add_topic_requires_value(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [{"op": "add", "path": "/topics/t1"}])


def test_apply_patch_replace_topic_overwrites_existing_entry(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1",
          {"topics": {"t1": {"name": "Old"}}, "connections": []})
    result = secondary_data.apply_patch("ws_1", [
        {"op": "replace", "path": "/topics/t1", "value": {"name": "New"}},
    ])
    assert result["topics"]["t1"]["name"] == "New"


def test_apply_patch_remove_topic_is_a_noop_when_key_missing(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    result = secondary_data.apply_patch("ws_1", [
        {"op": "remove", "path": "/topics/t_ghost"},
    ])
    assert result["topics"] == {}


def test_apply_patch_remove_topic_deletes_existing_entry(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1",
          {"topics": {"t1": {"name": "T1"}}, "connections": []})
    result = secondary_data.apply_patch("ws_1", [{"op": "remove", "path": "/topics/t1"}])
    assert result["topics"] == {}


# ---------------------------------------------------------------------
# apply_patch — /topics/<id>/instances/-
# ---------------------------------------------------------------------

def test_apply_patch_instance_append_requires_add_op(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1",
          {"topics": {"t1": {"name": "T1"}}, "connections": []})
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [
            {"op": "replace", "path": "/topics/t1/instances/-", "value": {}},
        ])


def test_apply_patch_instance_append_requires_value(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1",
          {"topics": {"t1": {"name": "T1"}}, "connections": []})
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [{"op": "add", "path": "/topics/t1/instances/-"}])


def test_apply_patch_instance_append_raises_when_topic_missing(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [
            {"op": "add", "path": "/topics/t_ghost/instances/-",
             "value": {"verbatim": "x", "confidence": 0.9}},
        ])


def test_apply_patch_instance_append_adds_to_existing_list(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1",
          {"topics": {"t1": {"name": "T1", "instances": [{"verbatim": "first"}]}},
           "connections": []})
    result = secondary_data.apply_patch("ws_1", [
        {"op": "add", "path": "/topics/t1/instances/-", "value": {"verbatim": "second"}},
    ])
    assert [i["verbatim"] for i in result["topics"]["t1"]["instances"]] == ["first", "second"]


def test_apply_patch_instance_append_creates_list_when_absent(monkeypatch, tmp_path):
    """An old topic dict written before `instances` existed has no key
    for it at all -- append must not KeyError."""
    _seed(monkeypatch, tmp_path, "ws_1",
          {"topics": {"t1": {"name": "T1"}}, "connections": []})
    result = secondary_data.apply_patch("ws_1", [
        {"op": "add", "path": "/topics/t1/instances/-", "value": {"verbatim": "only"}},
    ])
    assert result["topics"]["t1"]["instances"] == [{"verbatim": "only"}]


# ---------------------------------------------------------------------
# apply_patch — /connections/...
# ---------------------------------------------------------------------

def test_apply_patch_connections_add_only_supports_dash_append(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [
            {"op": "add", "path": "/connections/0", "value": {}},
        ])


def test_apply_patch_connections_add_requires_value(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [{"op": "add", "path": "/connections/-"}])


def test_apply_patch_connections_add_appends(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    conn = {"from_topic": "t1", "to_topic": "t2", "relation": "prerequisite-of"}
    result = secondary_data.apply_patch("ws_1", [{"op": "add", "path": "/connections/-", "value": conn}])
    assert result["connections"] == [conn]


def test_apply_patch_connections_remove_by_index(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1", {
        "topics": {}, "connections": [{"relation": "a"}, {"relation": "b"}],
    })
    result = secondary_data.apply_patch("ws_1", [{"op": "remove", "path": "/connections/0"}])
    assert result["connections"] == [{"relation": "b"}]


def test_apply_patch_connections_replace_by_index(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1", {
        "topics": {}, "connections": [{"relation": "a"}],
    })
    result = secondary_data.apply_patch("ws_1", [
        {"op": "replace", "path": "/connections/0", "value": {"relation": "replaced"}},
    ])
    assert result["connections"] == [{"relation": "replaced"}]


def test_apply_patch_connections_replace_requires_value(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1", {"topics": {}, "connections": [{"relation": "a"}]})
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [{"op": "replace", "path": "/connections/0"}])


def test_apply_patch_connections_index_out_of_range_raises(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1", {"topics": {}, "connections": []})
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [{"op": "remove", "path": "/connections/0"}])


def test_apply_patch_connections_non_integer_index_raises(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "ws_1", {"topics": {}, "connections": [{"relation": "a"}]})
    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [{"op": "remove", "path": "/connections/bogus"}])


# ---------------------------------------------------------------------
# apply_patch — batch semantics
# ---------------------------------------------------------------------

def test_apply_patch_applies_multiple_ops_in_one_batch(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    result = secondary_data.apply_patch("ws_1", [
        {"op": "add", "path": "/topics/t1", "value": {"name": "T1"}},
        {"op": "add", "path": "/topics/t2", "value": {"name": "T2"}},
        {"op": "add", "path": "/connections/-",
         "value": {"from_topic": "t1", "to_topic": "t2", "relation": "elaborates-on"}},
    ])
    assert set(result["topics"].keys()) == {"t1", "t2"}
    assert len(result["connections"]) == 1


def test_apply_patch_all_or_nothing_a_bad_op_touches_nothing_on_disk(monkeypatch, tmp_path):
    """The core regression case this module's docstring calls out:
    validated against a working copy first, so a bad op partway through
    a batch never partially writes."""
    _seed(monkeypatch, tmp_path, "ws_1",
          {"topics": {"t1": {"name": "Original"}}, "connections": []})

    with pytest.raises(ValueError):
        secondary_data.apply_patch("ws_1", [
            {"op": "add", "path": "/topics/t2", "value": {"name": "T2"}},
            {"op": "remove", "path": "/connections/0"},  # out of range -- batch fails here
        ])

    on_disk = secondary_data._read()
    assert on_disk["ws_1"]["topics"] == {"t1": {"name": "Original"}}


def test_apply_patch_persists_across_calls(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    secondary_data.apply_patch("ws_1", [{"op": "add", "path": "/topics/t1", "value": {"name": "T1"}}])
    result = secondary_data.apply_patch("ws_1", [{"op": "add", "path": "/topics/t2", "value": {"name": "T2"}}])
    assert set(result["topics"].keys()) == {"t1", "t2"}


def test_apply_patch_does_not_mutate_other_workspaces(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    secondary_data._write({"ws_other": {"topics": {"z": {"name": "Z"}}, "connections": []}})

    secondary_data.apply_patch("ws_1", [{"op": "add", "path": "/topics/t1", "value": {"name": "T1"}}])

    on_disk = secondary_data._read()
    assert on_disk["ws_other"]["topics"] == {"z": {"name": "Z"}}
