"""
tests/unit/test_eo_project_registry.py — Patch 7e-2.

eo/project_registry.py had zero test coverage before this. Every
file operation against an external project is required to route
through resolve_project_root() first (see that function's own
docstring pointing at agents/file_manager.py's _confine_to_root()) --
a bug in the registry lookup/merge logic here is a real path-safety
issue, not just a bookkeeping inconvenience: it's one of the checks
standing between a tier-2 task and writing outside the project root
it was actually authorized for.

Isolation: same bound-name gotcha as the other cache modules --
project_registry.py does `from memory.bus import write, read`, so
tests patch `read`/`write` on the project_registry module object, not
on memory.bus. register_project() additionally does real filesystem
I/O (writes CONTROL_UNIT_FILENAME into the target root) -- tests use
pytest's tmp_path fixture for that, never a real project directory.
"""
import json
import os

from eo import project_registry

# ---------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------

def test_slugify_lowercases_and_replaces_non_alnum_with_underscore():
    assert project_registry._slugify("My Cool Project!!") == "my_cool_project"


def test_slugify_strips_leading_and_trailing_underscores():
    assert project_registry._slugify("  --weird-- ") == "weird"


def test_slugify_truncates_to_30_chars():
    long_name = "a" * 50
    result = project_registry._slugify(long_name)
    assert len(result) == 30
    assert result == "a" * 30


# ---------------------------------------------------------------------
# generate_control_unit
# ---------------------------------------------------------------------

def test_generate_control_unit_preserves_display_name_verbatim():
    result = project_registry.generate_control_unit("My Robot Arm")
    assert result["display_name"] == "My Robot Arm"


def test_generate_control_unit_unique_name_is_slug_plus_6_hex_suffix():
    result = project_registry.generate_control_unit("My Robot Arm")
    slug, _, suffix = result["unique_name"].rpartition("_")
    assert slug == "my_robot_arm"
    assert len(suffix) == 6
    int(suffix, 16)  # must be valid hex, raises ValueError otherwise


def test_generate_control_unit_produces_different_unique_names_each_call():
    """Two control units for the same display_name must not collide --
    that's the entire reason for the uuid suffix."""
    first = project_registry.generate_control_unit("My Robot Arm")
    second = project_registry.generate_control_unit("My Robot Arm")
    assert first["unique_name"] != second["unique_name"]


# ---------------------------------------------------------------------
# register_project
# ---------------------------------------------------------------------

def test_register_project_writes_marker_file_with_unique_name(tmp_path, monkeypatch):
    monkeypatch.setattr(project_registry, "read", lambda key, default=None: default)
    monkeypatch.setattr(project_registry, "write", lambda key, value: None)

    project_registry.register_project("robot_arm_ab12cd", str(tmp_path))

    marker_path = tmp_path / project_registry.CONTROL_UNIT_FILENAME
    assert marker_path.exists()
    assert json.loads(marker_path.read_text()) == {"unique_name": "robot_arm_ab12cd"}


def test_register_project_stores_abspath_not_the_raw_input_path(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(project_registry, "read", lambda key, default=None: {})
    monkeypatch.setattr(project_registry, "write",
                         lambda key, value: seen.update({"key": key, "value": value}))

    relative_style = str(tmp_path) + os.sep + "." + os.sep
    project_registry.register_project("robot_arm_ab12cd", relative_style)

    assert seen["value"]["robot_arm_ab12cd"]["root_path"] == os.path.abspath(relative_style)


def test_register_project_merges_into_existing_registry_without_dropping_others(tmp_path, monkeypatch):
    """A second registration must not wipe out an unrelated project
    already sitting in the shared, non-namespaced registry key."""
    existing = {"other_project_xy9988": {"root_path": "/some/other/path"}}
    seen = {}
    monkeypatch.setattr(project_registry, "read", lambda key, default=None: existing)
    monkeypatch.setattr(project_registry, "write",
                         lambda key, value: seen.update({"key": key, "value": value}))

    project_registry.register_project("robot_arm_ab12cd", str(tmp_path))

    assert seen["key"] == "project_registry"
    assert "other_project_xy9988" in seen["value"]
    assert seen["value"]["other_project_xy9988"] == {"root_path": "/some/other/path"}
    assert seen["value"]["robot_arm_ab12cd"]["root_path"] == os.path.abspath(str(tmp_path))


# ---------------------------------------------------------------------
# resolve_project_root
# ---------------------------------------------------------------------

def test_resolve_project_root_returns_root_path_for_known_unit(monkeypatch):
    registry = {"robot_arm_ab12cd": {"root_path": "/home/user/robot-arm"}}
    monkeypatch.setattr(project_registry, "read", lambda key, default=None: registry)

    assert project_registry.resolve_project_root("robot_arm_ab12cd") == "/home/user/robot-arm"


def test_resolve_project_root_raises_value_error_for_unknown_unit(monkeypatch):
    monkeypatch.setattr(project_registry, "read", lambda key, default=None: {})
    try:
        project_registry.resolve_project_root("nonexistent_unit")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "nonexistent_unit" in str(exc)


def test_resolve_project_root_raises_when_registry_is_empty_default(monkeypatch):
    """No stored registry at all (fresh install) must raise the same
    ValueError as an unknown unit against a populated registry, not a
    different error from indexing an empty dict."""
    monkeypatch.setattr(project_registry, "read", lambda key, default=None: default)
    try:
        project_registry.resolve_project_root("anything")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------

def test_list_projects_returns_empty_list_when_registry_is_empty(monkeypatch):
    monkeypatch.setattr(project_registry, "read", lambda key, default=None: default)
    assert project_registry.list_projects() == []


def test_list_projects_flattens_unique_name_into_each_entry(monkeypatch):
    registry = {
        "robot_arm_ab12cd": {"root_path": "/home/user/robot-arm"},
        "greenhouse_ef34gh": {"root_path": "/home/user/greenhouse"},
    }
    monkeypatch.setattr(project_registry, "read", lambda key, default=None: registry)

    result = project_registry.list_projects()

    assert {"unique_name": "robot_arm_ab12cd", "root_path": "/home/user/robot-arm"} in result
    assert {"unique_name": "greenhouse_ef34gh", "root_path": "/home/user/greenhouse"} in result
    assert len(result) == 2
