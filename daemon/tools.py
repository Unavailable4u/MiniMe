"""
daemon/tools.py — F2 Part 3: the two read-only tool implementations
(list_dir, read_file) the daemon executes on request from the backend,
and the dispatch table daemon/connection.py's message loop uses to run
them. Every path this module touches is routed through
daemon/path_guard.py's assert_within_root -- see that module's own
docstring for why the containment check lives in its own file rather
than duplicated here.

Read-only by design (Part 3's whole scope, per the F2 plan): nothing
in this module ever writes, deletes, or executes anything on disk --
that's Part 4, which gets its own module plus a propose/confirm step
this one doesn't need.

Message shapes (also documented in eo/local_workspace.py on the
backend side, which is the other half of this protocol):

  backend -> daemon (tool_call):
    {"type": "tool_call", "request_id": "<uuid4>", "tool": "list_dir",
     "params": {"path": "."}}
    {"type": "tool_call", "request_id": "<uuid4>", "tool": "read_file",
     "params": {"path": "src/app.py"}}

  daemon -> backend (tool_result):
    {"type": "tool_result", "request_id": "<uuid4>", "ok": true,
     "result": {...}}
    {"type": "tool_result", "request_id": "<uuid4>", "ok": false,
     "error": "<message>"}

`path` is always relative to the daemon's configured allowed root.
Absolute paths are still safe to receive (not just rejected on
principle): _resolve() below routes every candidate through
path_guard.assert_within_root(), which resolves symlinks and checks
real containment regardless of how the candidate path was written --
an absolute path outside the root fails that check exactly like a
'../..' traversal would, and an absolute path that happens to point
back inside the root is accepted exactly like a relative one would be.

Place this file at: daemon/tools.py
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict

from daemon.path_guard import PathGuardError, assert_within_root

# A multi-GB file requested by mistake (or by a confused agent) must
# come back as a clear truncation, not hang the single daemon
# connection everything else on this workspace shares while it's
# read/serialized/sent.
MAX_READ_FILE_BYTES = 1_000_000


class ToolError(Exception):
    """Any read-tool failure that should become a
    {"ok": false, "error": ...} tool_result rather than crashing the
    connection loop -- a bad path, a missing file, a directory passed
    to read_file, an unknown tool name, etc. Always a message safe to
    show a user (never a raw traceback), since it's what the agent/
    frontend ultimately sees via eo.local_workspace.ToolCallError on
    the backend side."""


def _resolve(root: Path, relative_path: str) -> Path:
    try:
        return assert_within_root(root / relative_path, root)
    except PathGuardError as exc:
        raise ToolError(str(exc)) from exc


def list_dir(root: Path, path: str = ".") -> Dict[str, Any]:
    target = _resolve(root, path)
    if not target.exists():
        raise ToolError(f"path does not exist: {path}")
    if not target.is_dir():
        raise ToolError(f"not a directory: {path}")

    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            is_dir = child.is_dir()
            entries.append({
                "name": child.name,
                "type": "dir" if is_dir else "file",
                "size": None if is_dir else child.stat().st_size,
            })
        except OSError:
            # A single unreadable entry (permission error, broken
            # symlink) shouldn't fail the whole listing.
            entries.append({"name": child.name, "type": "unknown", "size": None})

    return {
        "path": "." if target == root else str(target.relative_to(root)),
        "entries": entries,
    }


def read_file(root: Path, path: str) -> Dict[str, Any]:
    target = _resolve(root, path)
    if not target.exists():
        raise ToolError(f"path does not exist: {path}")
    if target.is_dir():
        raise ToolError(f"not a file (it's a directory): {path}")

    try:
        size = target.stat().st_size
        raw = target.read_bytes()
    except OSError as exc:
        raise ToolError(f"could not read file: {exc}") from exc

    truncated = len(raw) > MAX_READ_FILE_BYTES
    if truncated:
        raw = raw[:MAX_READ_FILE_BYTES]

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(
            f"not a UTF-8 text file, can't display as text: {path}"
        ) from exc

    return {
        "path": str(target.relative_to(root)),
        "content": content,
        "size": size,
        "truncated": truncated,
    }


# tool name -> (root, params) -> result dict. daemon/connection.py's
# message loop looks up the requested tool here and calls it. Part 4
# adding write_file/delete/execute_command later means adding entries
# here (behind their own propose/confirm step), not branching
# connection.py's loop itself.
READ_ONLY_TOOLS: Dict[str, Callable[[Path, Dict[str, Any]], Dict[str, Any]]] = {
    "list_dir": lambda root, params: list_dir(root, params.get("path", ".")),
    "read_file": lambda root, params: read_file(root, _require_param(params, "path")),
}


def _require_param(params: Dict[str, Any], name: str) -> Any:
    if name not in params:
        raise ToolError(f"missing required param: {name!r}")
    return params[name]


def dispatch(root: Path, tool: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Raises ToolError for both an unknown tool name and any failure
    inside the tool itself, so daemon/connection.py's caller has one
    exception type to catch and turn into a tool_result."""
    handler = READ_ONLY_TOOLS.get(tool)
    if handler is None:
        raise ToolError(f"unknown or not-yet-implemented tool: {tool!r}")
    return handler(root, params or {})
