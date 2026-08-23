"""
daemon/tools.py — F2 Part 3 + Part 4. Part 3 shipped the two
read-only tool implementations (list_dir, read_file). Part 4 adds the
three mutating ones -- write_file, delete, execute_command -- in the
same module rather than a separate one, since they share _resolve()
and the same dispatch()/ToolError contract Part 3 already established;
splitting them out would just mean importing path_guard twice.

Every path this module touches is routed through
daemon/path_guard.py's assert_within_root -- see that module's own
docstring for why the containment check lives in its own file rather
than duplicated here. execute_command is the one exception that isn't
a *path* check: it still runs with cwd pinned to the allowed root (see
execute_command's own docstring for what that does and doesn't
protect against).

Read-only vs mutating is a daemon-side distinction only in the sense
that this module now contains both -- the actual "reads run freely,
writes/deletes/executes need a confirm" split is enforced entirely on
the backend side (eo/local_workspace_tools.py's propose/confirm store,
Part 4). By the time a write_file/delete/execute_command tool_call
reaches this module at all, the backend has already gotten an explicit
confirm for it; this module has no concept of "pending" and just runs
whatever tool_call it's handed, exactly like Part 3's read tools
always have. That's deliberate -- the daemon is the dumb, deterministic
end of this pipe on purpose (see eo/local_workspace.py's docstring),
and duplicating a confirm gate here would just be a second, redundant
place for that logic to drift out of sync with the real one.

Message shapes (also documented in eo/local_workspace.py on the
backend side, which is the other half of this protocol):

  backend -> daemon (tool_call):
    {"type": "tool_call", "request_id": "<uuid4>", "tool": "list_dir",
     "params": {"path": "."}}
    {"type": "tool_call", "request_id": "<uuid4>", "tool": "read_file",
     "params": {"path": "src/app.py"}}
    {"type": "tool_call", "request_id": "<uuid4>", "tool": "write_file",
     "params": {"path": "src/app.py", "content": "...", "create_dirs": true}}
    {"type": "tool_call", "request_id": "<uuid4>", "tool": "delete",
     "params": {"path": "old_file.py"}}
    {"type": "tool_call", "request_id": "<uuid4>", "tool": "execute_command",
     "params": {"command": "pytest -q", "timeout": 60}}

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

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict

from daemon.path_guard import PathGuardError, assert_within_root

# A multi-GB file requested by mistake (or by a confused agent) must
# come back as a clear truncation, not hang the single daemon
# connection everything else on this workspace shares while it's
# read/serialized/sent.
MAX_READ_FILE_BYTES = 1_000_000

# Same reasoning as MAX_READ_FILE_BYTES above, applied to the other
# direction -- a runaway write shouldn't be able to fill the disk (or
# just hang this connection serializing a huge request) any more than
# a runaway read should.
MAX_WRITE_FILE_BYTES = 1_000_000

# execute_command's default and hard ceiling. A caller can ask for
# less; nothing this module runs can ask for more -- an agent-proposed
# "run the test suite" with no explicit timeout must still eventually
# give the single daemon connection back, same reasoning as running
# disk IO in a thread on the backend side (see connection.py).
DEFAULT_EXECUTE_TIMEOUT_SECONDS = 30
MAX_EXECUTE_TIMEOUT_SECONDS = 300

# subprocess output is captured and sent back over the same JSON
# websocket message every other tool_result uses -- an unbounded
# amount of stdout/stderr from a runaway command would bloat that
# message the same way an unbounded read_file would, so it's
# truncated with the same shape (truncated: bool) read_file already
# uses below.
MAX_EXECUTE_OUTPUT_CHARS = 200_000


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


def write_file(root: Path, path: str, content: str, create_dirs: bool = False) -> Dict[str, Any]:
    """Part 4. Writes `content` (text, UTF-8) to `path`, overwriting
    whatever's there. `create_dirs` mirrors os.makedirs' own default of
    *not* silently creating parent directories -- a typo'd nested path
    should come back as a clear error, not quietly create a folder
    structure nobody asked for, unless the caller explicitly opts in.
    """
    if not isinstance(content, str):
        raise ToolError("write_file content must be a string")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_WRITE_FILE_BYTES:
        raise ToolError(
            f"content too large to write ({len(encoded)} bytes, limit is "
            f"{MAX_WRITE_FILE_BYTES})"
        )

    target = _resolve(root, path)
    if target.is_dir():
        raise ToolError(f"not a file (it's a directory): {path}")

    parent = target.parent
    parent_existed = parent.exists()
    if not parent_existed:
        if not create_dirs:
            raise ToolError(
                f"parent directory does not exist: {parent.relative_to(root)} "
                "(pass create_dirs to create it)"
            )
        # Still routed through the same containment check as the file
        # itself -- _resolve() above already proved `target` (and
        # therefore every ancestor up to `root`) resolves inside root,
        # so creating the missing intermediate directories here can't
        # itself escape the guard.
        parent.mkdir(parents=True, exist_ok=True)

    try:
        target.write_bytes(encoded)
    except OSError as exc:
        raise ToolError(f"could not write file: {exc}") from exc

    return {
        "path": str(target.relative_to(root)),
        "bytes_written": len(encoded),
        "created_dirs": create_dirs and not parent_existed,
    }


def delete(root: Path, path: str) -> Dict[str, Any]:
    """Part 4. Deletes a file or, recursively, a directory. Two
    guards beyond the usual containment check, since "delete" is the
    single most destructive read-adjacent-shaped tool this daemon
    exposes:
      - refuses to delete the allowed root itself (a caller wanting to
        empty the whole paired folder isn't a normal single-file/dir
        delete, and _resolve()'s containment check alone wouldn't
        catch it, since the root is trivially "within" itself).
      - refuses a path that doesn't exist, same as read_file/list_dir,
        so a caller can tell "already gone" apart from "deleted just
        now" rather than both looking like a silent no-op success.
    """
    target = _resolve(root, path)
    if target == root:
        raise ToolError("refusing to delete the daemon's configured root folder")
    if not target.exists():
        raise ToolError(f"path does not exist: {path}")

    was_dir = target.is_dir()
    try:
        if was_dir:
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise ToolError(f"could not delete: {exc}") from exc

    return {
        "path": str(target.relative_to(root)) if target != root else ".",
        "type": "dir" if was_dir else "file",
        "deleted": True,
    }


def execute_command(
    root: Path,
    command: str,
    timeout: int | None = None,
    on_chunk: "Callable[[str, str], None] | None" = None,
) -> Dict[str, Any]:
    """Part 4 (batch) + Part 7 (streaming). Runs `command` through the
    shell with cwd pinned to `root`. This is the one tool in this
    module that path_guard's containment check doesn't apply to
    (there's no single "path" argument to resolve) -- the actual
    safety property it provides is narrower and worth being explicit
    about: the *working directory* the command starts in is the
    allowed root, exactly like a person would get by `cd`-ing into the
    paired folder in their own terminal, and nothing more. It does not
    sandbox the command itself, does not stop `cd ..` or an absolute
    path inside the command from touching anything outside root, and
    does not restrict which binaries can run. That's why this tool --
    unlike list_dir/read_file -- always goes through Part 4's
    propose/confirm gate on the backend side
    (eo/local_workspace_tools.py) with no "run freely" path, the same
    as write_file/delete: the person pairing their machine is trusting
    themselves (or whoever they've paired with) with real shell access
    scoped only by "starts here," not a real sandbox boundary.

    `on_chunk`, new in Part 7: an optional `(stream, text) -> None`
    callback, called synchronously from this function (which itself
    always runs off the daemon's event loop -- see
    connection.py's `asyncio.to_thread(tools.dispatch, ...)`) once per
    line of output as the subprocess produces it, `stream` being
    `"stdout"` or `"stderr"`. This is what lets Part 7's terminal panel
    show output live instead of only after the whole command finishes.
    When `on_chunk` is None (every caller before Part 7, and any future
    caller that just wants the final result), behavior is byte-for-byte
    identical to the old `subprocess.run`-based implementation --
    same captured stdout/stderr, same truncation, same timeout
    handling. Streaming and truncation both apply to the exact same
    accumulated text; `on_chunk` never sees data that the returned
    `stdout`/`stderr` fields don't also (possibly truncated) contain.
    """
    if not command or not command.strip():
        raise ToolError("command must be a non-empty string")

    requested_timeout = timeout if timeout is not None else DEFAULT_EXECUTE_TIMEOUT_SECONDS
    try:
        requested_timeout = int(requested_timeout)
    except (TypeError, ValueError):
        raise ToolError(f"timeout must be a number of seconds, got {timeout!r}")
    if requested_timeout <= 0:
        raise ToolError("timeout must be positive")
    actual_timeout = min(requested_timeout, MAX_EXECUTE_TIMEOUT_SECONDS)

    try:
        # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
        # shell=True is required here, not incidental: `command` is an
        # arbitrary shell string (may contain pipes/redirects/etc.) that
        # this tool's whole purpose is to execute -- see the module and
        # execute_command() docstrings. It only ever reaches this call
        # after the backend's own explicit propose/confirm gate (Part 4),
        # so this isn't unsanitized input silently reaching a shell.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
    except OSError as exc:
        raise ToolError(f"could not run command: {exc}") from exc

    # Two reader threads (one per stream) rather than one thread
    # alternating between proc.stdout/proc.stderr -- a command that
    # writes a lot to one and nothing to the other must never have its
    # output delayed behind a blocking readline() on the quiet stream.
    # Each thread appends into its own accumulator list (thread-safe
    # enough for this: each thread only ever appends to its own list,
    # this function only reads them after both threads have been
    # joined) and, once truncation hasn't already kicked in, invokes
    # `on_chunk` per line -- the same per-line granularity a person
    # watching a real terminal would see, not one chunk per raw
    # OS-level read().
    import threading

    accumulated: Dict[str, list[str]] = {"stdout": [], "stderr": []}
    accumulated_chars: Dict[str, int] = {"stdout": 0, "stderr": 0}
    truncated_flags: Dict[str, bool] = {"stdout": False, "stderr": False}

    def _reader(stream_name: str, pipe) -> None:
        try:
            for raw_line in iter(pipe.readline, b""):
                line = raw_line.decode("utf-8", errors="replace")
                if truncated_flags[stream_name]:
                    continue  # already hit the cap for this stream -- keep draining the pipe, stop accumulating/emitting
                remaining = MAX_EXECUTE_OUTPUT_CHARS - accumulated_chars[stream_name]
                if len(line) > remaining:
                    line = line[:remaining]
                    truncated_flags[stream_name] = True
                accumulated[stream_name].append(line)
                accumulated_chars[stream_name] += len(line)
                if on_chunk is not None and line:
                    try:
                        on_chunk(stream_name, line)
                    except Exception:
                        # A broken/closed websocket on the backend side
                        # must never take down the command that's
                        # actually running -- the final tool_result
                        # below is still assembled and returned/sent
                        # normally either way.
                        pass
        finally:
            pipe.close()

    stdout_thread = threading.Thread(target=_reader, args=("stdout", proc.stdout), daemon=True)
    stderr_thread = threading.Thread(target=_reader, args=("stderr", proc.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    try:
        proc.wait(timeout=actual_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()

    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)

    if timed_out:
        raise ToolError(f"command timed out after {actual_timeout}s: {command!r}")

    stdout = "".join(accumulated["stdout"])
    stderr = "".join(accumulated["stderr"])

    return {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated_flags["stdout"] or truncated_flags["stderr"],
        "timed_out": False,
    }


# tool name -> (root, params) -> result dict. daemon/connection.py's
# message loop looks up the requested tool here and calls it -- one
# combined table covering both Part 3's read tools and Part 4's
# mutating ones, since by the time a tool_call reaches this module the
# backend has already handled the read-vs-confirm distinction (see
# module docstring). Kept as two named dicts (READ_ONLY_TOOLS,
# MUTATING_TOOLS) rather than one flat one purely so each half stays
# self-documenting about which risk class it's in -- dispatch() below
# still looks both up as one combined table.
READ_ONLY_TOOLS: Dict[str, Callable[[Path, Dict[str, Any]], Dict[str, Any]]] = {
    "list_dir": lambda root, params: list_dir(root, params.get("path", ".")),
    "read_file": lambda root, params: read_file(root, _require_param(params, "path")),
}

MUTATING_TOOLS: Dict[str, Callable[[Path, Dict[str, Any]], Dict[str, Any]]] = {
    "write_file": lambda root, params: write_file(
        root,
        _require_param(params, "path"),
        _require_param(params, "content"),
        bool(params.get("create_dirs", False)),
    ),
    "delete": lambda root, params: delete(root, _require_param(params, "path")),
    "execute_command": lambda root, params: execute_command(
        root,
        _require_param(params, "command"),
        params.get("timeout"),
    ),
}

ALL_TOOLS: Dict[str, Callable[[Path, Dict[str, Any]], Dict[str, Any]]] = {
    **READ_ONLY_TOOLS,
    **MUTATING_TOOLS,
}


def _require_param(params: Dict[str, Any], name: str) -> Any:
    if name not in params:
        raise ToolError(f"missing required param: {name!r}")
    return params[name]


def dispatch(root: Path, tool: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Raises ToolError for both an unknown tool name and any failure
    inside the tool itself, so daemon/connection.py's caller has one
    exception type to catch and turn into a tool_result."""
    handler = ALL_TOOLS.get(tool)
    if handler is None:
        raise ToolError(f"unknown or not-yet-implemented tool: {tool!r}")
    return handler(root, params or {})
