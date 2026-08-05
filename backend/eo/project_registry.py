"""
eo/project_registry.py — Cross-Project File Control (blueprint §14).
Generates and tracks "control units" — uniquely-named markers a user
drops into an external project's root, making that project addressable
by name from MiniMe.

Requires memory/bus.py's _namespaced() to treat "project_registry" as a
non-namespaced, system-wide key (fixed alongside this file, Part 3 step
6) -- otherwise this registry would silently fragment per active
app_slug instead of tracking projects across the whole system.
"""
import os
import sys
import uuid
import json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, read

CONTROL_UNIT_FILENAME = ".minime_control"


def generate_control_unit(display_name: str) -> dict:
    """Creates a unique control unit record. Caller is responsible for
    writing the actual marker file into the target project's root (see
    register_project below) — this function only allocates the identity."""
    unique_name = f"{_slugify(display_name)}_{uuid.uuid4().hex[:6]}"
    return {"unique_name": unique_name, "display_name": display_name}


def register_project(unique_name: str, root_path: str) -> None:
    """Writes the marker file into root_path AND records the mapping in
    the shared registry (a non-namespaced key, since this tracks projects
    across the whole system, not one app_slug)."""
    root_path = os.path.abspath(root_path)
    marker_path = os.path.join(root_path, CONTROL_UNIT_FILENAME)
    with open(marker_path, "w") as f:
        json.dump({"unique_name": unique_name}, f)

    registry = read("project_registry", default={})
    registry[unique_name] = {"root_path": root_path}
    write("project_registry", registry)


def resolve_project_root(unique_name: str) -> str:
    """Returns the confirmed root path for a control unit name, or raises
    if unknown — every file operation against an external project MUST
    go through this before touching disk (see agents/file_manager.py's
    _confine_to_root())."""
    registry = read("project_registry", default={})
    entry = registry.get(unique_name)
    if not entry:
        raise ValueError(f"Unknown project control unit: {unique_name!r}")
    return entry["root_path"]


def list_projects() -> list:
    registry = read("project_registry", default={})
    return [{"unique_name": k, **v} for k, v in registry.items()]


def _slugify(text: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")[:30]