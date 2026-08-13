"""
eo/local_workspace_tools.py — F2 Part 3: the local-workspace tool set
an agent (or a route) can call, built on top of eo/local_workspace.py's
call_daemon() request/response bridge.

Only list_dir/read_file exist here -- Part 3's whole scope is the
read-only path. Per the F2 plan, reads run freely with no confirm
step, so this is the safe half to prove the backend<->daemon pipe
works end-to-end before Part 4 adds write_file/delete/execute_command
behind a propose/confirm step.

Two things live here:
  - list_workspace_dir()/read_workspace_file(): the actual async
    functions a caller runs to execute a tool for real (see
    api/routes/local_workspace.py, Part 3's other new file, for the
    HTTP surface over these).
  - local_workspace_tools(): the OpenAI-tool-schema builder, same
    shape/convention as utils/capability_tools.py's
    study_progress_tools() (a hand-written, non-manifest tool list,
    per that module's own docstring on why generation and
    non-generation tools stay separate) -- for wiring into
    api/routes/notebooks.py's classify_intent() `tools` list alongside
    manifest_to_tools()/study_progress_tools(), once a caller wants an
    agent to be able to reach for a local-workspace read the same way
    it already reaches for "mark as done". Not wired into that route
    in this part -- left as a self-contained builder so that wiring
    (and its own review) can happen independently of the read-path
    plumbing this part is actually about.

Place this file at: eo/local_workspace_tools.py
"""
from __future__ import annotations

from typing import Any, Dict, List

from eo.local_workspace import ToolCallError, call_daemon  # noqa: F401 -- re-exported for callers

__all__ = [
    "ToolCallError",
    "list_workspace_dir",
    "read_workspace_file",
    "local_workspace_tools",
]


async def list_workspace_dir(workspace_id: str, path: str = ".") -> Dict[str, Any]:
    """Raises eo.local_workspace.ToolCallError if no daemon is
    connected for this workspace, the call times out, or the daemon
    reports a failure (bad path, not a directory, etc.) -- callers
    (api/routes/local_workspace.py) turn that into the HTTP error
    shape rather than handling it here, so this stays a thin,
    transport-agnostic wrapper any future caller (a route, an agent
    step) can reuse as-is."""
    return await call_daemon(workspace_id, "list_dir", {"path": path})


async def read_workspace_file(workspace_id: str, path: str) -> Dict[str, Any]:
    """Same error contract as list_workspace_dir() above."""
    return await call_daemon(workspace_id, "read_file", {"path": path})


def local_workspace_tools() -> List[Dict[str, Any]]:
    """Hand-written, not manifest-driven -- same reasoning
    utils/capability_tools.py's study_progress_tools() docstring
    already gives for its own non-manifest tools: this isn't a
    generation target, it's a read action against a specific machine's
    paired folder, so it doesn't belong in CAPABILITIES_MANIFEST."""
    return [
        {
            "type": "function",
            "function": {
                "name": "list_local_dir",
                "description": (
                    "List files and folders inside the user's locally "
                    "paired project folder (the F2 local daemon feature). "
                    "Only works if a daemon is currently connected for "
                    "this workspace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path relative to the paired folder's root. "
                                "Omit, or use '.', for the root itself."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_local_file",
                "description": (
                    "Read the text contents of one file inside the user's "
                    "locally paired project folder (the F2 local daemon "
                    "feature). Only works if a daemon is currently "
                    "connected for this workspace, and only for UTF-8 "
                    "text files under the read-size limit -- binary or "
                    "oversized files come back as an error, not partial "
                    "or garbled content."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the paired folder's root.",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
    ]
