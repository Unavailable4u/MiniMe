"""
eo/code_findings.py -- W8.3a (Build Workbench plan, backend half of the
Problems panel).

The tier-3 pipeline already produces two kinds of "something is wrong
with this file" data, each in its own shape, each keyed by module name
on the memory bus:

  KEYS["test_results"]          -- agents/sandbox_tester.py:
      {module: {"passed": bool, "stdout": str, "stderr": str,
                "error": str | None}}
      `error` is str() of an e2b ExecutionError, which is a dataclass,
      so it is the *repr* -- `ExecutionError(name='NameError',
      value="name 'x' is not defined", traceback='\\x1b[0;31m...')`,
      with the IPython traceback's ANSI colour codes and newlines
      still in escaped, literal-backslash form.
  KEYS["security_scan_results"] -- agents/security_scanner.py (after
      agents/security_aggregator.py):
      {module: {"findings": [{"severity": "critical|moderate|minor",
                              "description": str, ["source": str]}]}}
      The line number only exists inside the description text
      ("... at line 12: ..."), and `source` is only present on
      findings that came straight from a tool, not on ones the LLM
      summarising pass rewrote.

This module turns both into one flat list of
`{path, line, severity, message, source}` -- the shape the editor needs
to draw `@codemirror/lint` diagnostics and a bottom-panel list -- and
persists it per file (workspace_code_findings, migration 0011) so the
frontend can GET it back with the rest of the Code sub-tab's data.

Two places a finding's line number can be wrong, both handled here:

  1. The sandbox tester runs `code.rstrip() + "\\n\\n# --- Generated
     tests (Test Writer) ---\\n" + test_code`, so every traceback line
     number is a line of that COMBINED text. Anything past the last
     line of the module's own code is a line of the generated tests,
     which are never saved to the workspace -- pointing a diagnostic
     there would put it on a line that doesn't exist in the file (or,
     worse, on an unrelated one). Such frames are ignored; the next
     frame out that IS inside the module wins, and if none is, the
     finding is stored with line=None (a file-level problem).
  2. A line number quoted by a scan finding's free text (especially one
     the LLM rewrote) may be past the end of the file. Same rule: out
     of range means no line, not a wrong one.

Pure functions up top (no I/O -- unit-testable without a database),
then the two functions that touch Postgres: replace_findings() and
list_findings(). Neither is fail-open on its own; api/task_runner.py's
_write_code_files() wraps the write in its own try/except, since a
findings hiccup must never turn an already-saved set of files (or the
chat answer that produced them) into an error.
"""
import re

from eo import db

# @codemirror/lint's own severity names -- stored as-is so the frontend
# never has to translate the scanners' critical/moderate/minor.
SEVERITIES = ("error", "warning", "info")
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}

SOURCE_SANDBOX_TEST = "sandbox_test"
# Used when a scan finding has no `source` of its own (the LLM
# summarising pass in security_scanner.py doesn't carry it through).
SOURCE_SECURITY_SCAN = "security_scan"

_SCAN_SEVERITY = {"critical": "error", "moderate": "warning", "minor": "info"}

# Bounds, so one noisy scanner or a huge traceback can't bloat the
# table or the GET response. Findings beyond the per-file cap are
# dropped lowest-severity first (see build_findings()).
MAX_MESSAGE_LENGTH = 500
MAX_SOURCE_LENGTH = 40
MAX_FINDINGS_PER_FILE = 100

# sandbox_tester._run_one_module() results that say "the code was never
# actually run", not "the code is wrong" -- none of these is a finding
# about the file, so none produces one.
_NON_CODE_ERROR_PREFIXES = ("Sandbox failed to run:", "Unexpected module data shape")
_NO_CODE_STDERR = "No code found for this module."

# --- traceback / text parsing -------------------------------------------

# ANSI colour/cursor codes, both as real ESC bytes (a plain stderr
# string) and in the escaped literal-backslash spelling str(repr(...))
# leaves them in inside an ExecutionError's repr.
_ANSI_RE = re.compile(r"(?:\x1b|\\x1b|\\u001b|\\033)\[[0-9;]*[A-Za-z]")

# Frames of the module's own code, in the two spellings the sandbox
# produces: IPython's `Cell In[3], line 12` (also followed by
# `, in func(...)` for frames inside a function defined in the same
# cell) and the classic `File "<string>", line 12` for code run from a
# string. Frames in library files look different (`File /usr/...py:123,
# in ...` in IPython, `File "/usr/...py", line 123` classically) and are
# deliberately NOT matched: their line numbers belong to someone else's
# file.
_CELL_FRAME_RE = re.compile(r"\bCell\s+In\s*\[\d+\],\s*line\s+(\d+)")
_PSEUDO_FILE_FRAME_RE = re.compile(
    r'File\s+\\?"<(?:string|stdin|ipython-input[^>]*)>\\?",\s*line\s+(\d+)'
)

# `at line 12`, `on line 12`, `lines 12-15` (first number wins). A bare
# "line ?" (static_scan's placeholder when the tool gave no line) has no
# digits and so correctly doesn't match.
_LINE_IN_TEXT_RE = re.compile(r"\blines?\s+(\d+)", re.IGNORECASE)

# str(ExecutionError(...)) -- each field is quoted with whichever quote
# char repr() found cheapest, hence the backreferences.
_EXECUTION_ERROR_RE = re.compile(
    r"^ExecutionError\(name=(?P<nq>['\"])(?P<name>.*?)(?P=nq), "
    r"value=(?P<vq>['\"])(?P<value>.*?)(?P=vq), "
    r"traceback=(?P<tq>['\"])(?P<traceback>.*)(?P=tq)\)$",
    re.DOTALL,
)

_REPR_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "\\": "\\"}
_REPR_ESCAPE_RE = re.compile(r"\\([ntr'\"\\])")

_ERROR_LINE_RE = re.compile(r"\b\w*(?:Error|Exception|Warning)\b")


def _plain(text: str) -> str:
    """`text` with ANSI codes removed and the literal-backslash newline /
    tab escapes repr() leaves inside str(ExecutionError) turned back
    into real whitespace. The second half matters for more than looks:
    in the escaped spelling a frame's `Cell` directly follows a literal
    backslash-n, so without it there is no word boundary in front of
    `Cell` and the frame regex below silently finds nothing."""
    return (
        _ANSI_RE.sub("", text)
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .replace("\\t", "\t")
    )


def _unrepr(text: str) -> str:
    """Undo the handful of escapes repr() adds to a string, without the
    latin-1 round trip codecs' unicode_escape would mangle non-ASCII
    text with."""
    return _REPR_ESCAPE_RE.sub(lambda m: _REPR_ESCAPES[m.group(1)], text)


def _clean_message(text: str) -> str:
    """One-line, bounded display text."""
    collapsed = " ".join(_plain(text).split())
    if len(collapsed) > MAX_MESSAGE_LENGTH:
        collapsed = collapsed[: MAX_MESSAGE_LENGTH - 1].rstrip() + "\u2026"
    return collapsed


def _clean_source(value) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()[:MAX_SOURCE_LENGTH]
    return cleaned or None


def _code_line_count(code: str) -> int:
    """Number of lines in `code` as the sandbox saw it: it ran
    `code.rstrip()` followed by the generated tests, so lines past this
    count are the tests', and trailing blank lines don't count."""
    stripped = code.rstrip() if isinstance(code, str) else ""
    if not stripped:
        return 0
    return stripped.count("\n") + 1


def line_from_traceback(text: str, max_line: int) -> int | None:
    """The line of the module's own code a traceback ends in -- the
    innermost frame whose line is within 1..max_line -- or None.

    Frames are scanned innermost (last in the text) to outermost, since
    the innermost frame in the module's own code is where the failure
    actually surfaced. A frame past `max_line` is a line of the appended
    generated tests (see this module's docstring) and is skipped, so
    `test line 60 -> app line 12 -> raise` reports 12, and a failure
    that only ever happened inside the tests reports nothing."""
    if not isinstance(text, str) or max_line < 1:
        return None
    clean = _plain(text)
    frames = []
    for pattern in (_CELL_FRAME_RE, _PSEUDO_FILE_FRAME_RE):
        frames.extend((m.start(), int(m.group(1))) for m in pattern.finditer(clean))
    for _, line in sorted(frames, reverse=True):
        if 1 <= line <= max_line:
            return line
    return None


def line_from_description(text: str, max_line: int) -> int | None:
    """The first `line N` mentioned in a finding's description, if it is
    a real line of the file (1..max_line); otherwise None."""
    if not isinstance(text, str) or max_line < 1:
        return None
    match = _LINE_IN_TEXT_RE.search(_plain(text))
    if not match:
        return None
    line = int(match.group(1))
    return line if 1 <= line <= max_line else None


def _parse_execution_error(text: str):
    """(name, value, traceback) from str(ExecutionError(...)), or None
    when `text` isn't in that shape."""
    match = _EXECUTION_ERROR_RE.match(text.strip())
    if not match:
        return None
    return (
        _unrepr(match.group("name")),
        _unrepr(match.group("value")),
        match.group("traceback"),
    )


def _headline(text: str) -> str:
    """The most informative single line of a block of stderr: the last
    line that names an Error/Exception/Warning (a traceback's final
    line, a warning's category line), falling back to the last
    non-empty line."""
    lines = [ln.strip() for ln in _plain(text).splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _ERROR_LINE_RE.search(ln):
            return ln
    return lines[-1] if lines else ""


# --- producers: one module's result -> list of finding dicts ----------------

def findings_from_test_result(result, code: str) -> list[dict]:
    """Findings (without `path`) for one sandbox_tester result.

    A passing result, or one that records the sandbox/infrastructure
    failing rather than the code (see _NON_CODE_ERROR_PREFIXES), yields
    nothing. A raised error is severity "error"; a run that "failed"
    only because something wrote to stderr (a warning, say) is "warning".
    """
    if not isinstance(result, dict) or result.get("passed"):
        return []
    error = result.get("error")
    stderr = result.get("stderr")
    error_text = error.strip() if isinstance(error, str) else ""
    stderr_text = stderr if isinstance(stderr, str) else ""
    if error_text.startswith(_NON_CODE_ERROR_PREFIXES):
        return []
    if not error_text and stderr_text.strip() == _NO_CODE_STDERR:
        return []

    max_line = _code_line_count(code)
    if error_text:
        parsed = _parse_execution_error(error_text)
        if parsed:
            name, value, traceback = parsed
            message = f"{name}: {value}" if value else name
        else:
            traceback = error_text
            message = _headline(error_text) or error_text
        line = line_from_traceback(traceback, max_line)
        if line is None:
            line = line_from_traceback(stderr_text, max_line)
        severity = "error"
    elif stderr_text.strip():
        message = _headline(stderr_text)
        line = line_from_traceback(stderr_text, max_line)
        severity = "warning"
    else:
        return []

    message = _clean_message(message)
    if not message:
        return []
    return [{"line": line, "severity": severity, "message": message, "source": SOURCE_SANDBOX_TEST}]


def findings_from_scan_result(result, code: str) -> list[dict]:
    """Findings (without `path`) for one security_scanner result.

    Severity maps critical/moderate/minor -> error/warning/info (an
    unrecognised severity is "warning" rather than being dropped -- a
    real tool finding is never silently lost, same rule
    agents/static_scan.py's own severity map follows). `tool_error`
    (the scan tools themselves failing) says nothing about the code and
    produces nothing."""
    if not isinstance(result, dict):
        return []
    raw_findings = result.get("findings")
    if not isinstance(raw_findings, list):
        return []
    max_line = _code_line_count(code)
    out = []
    for raw in raw_findings:
        if not isinstance(raw, dict):
            continue
        description = raw.get("description")
        if not isinstance(description, str):
            continue
        message = _clean_message(description)
        if not message:
            continue
        severity = _SCAN_SEVERITY.get(str(raw.get("severity", "")).strip().lower(), "warning")
        out.append({
            "line": line_from_description(description, max_line),
            "severity": severity,
            "message": message,
            "source": _clean_source(raw.get("source")) or SOURCE_SECURITY_SCAN,
        })
    return out


def build_findings(modules: dict, test_results, scan_results) -> list[dict]:
    """Every finding for the given modules, as
    `{path, line, severity, message, source}` dicts.

    `modules`: {module_name: {"path": <workspace file path>, "code":
    <the module's code as it was tested/scanned>}} -- exactly the
    modules _write_code_files() just saved. `test_results` /
    `scan_results` are the raw bus values (either may be None/empty); a
    module missing from either simply contributes nothing from it.

    Per file: exact duplicates (same line, message and source) are
    collapsed, findings are ordered most severe first, and the list is
    capped at MAX_FINDINGS_PER_FILE -- so the cap only ever drops
    "info" before "warning" before "error".
    """
    tests = test_results if isinstance(test_results, dict) else {}
    scans = scan_results if isinstance(scan_results, dict) else {}
    out = []
    for module_name, info in modules.items():
        path = info["path"]
        code = info.get("code") or ""
        found = findings_from_test_result(tests.get(module_name), code)
        found += findings_from_scan_result(scans.get(module_name), code)

        seen = set()
        unique = []
        for f in found:
            key = (f["line"], f["message"], f["source"])
            if key not in seen:
                seen.add(key)
                unique.append(f)
        unique.sort(key=lambda f: _SEVERITY_RANK[f["severity"]])  # stable: ties keep producer order
        out.extend({"path": path, **f} for f in unique[:MAX_FINDINGS_PER_FILE])
    return out


# --- storage ------------------------------------------------------------------

def replace_findings(ws_id: str, paths: list[str], findings: list[dict], user_id: str,
                     file_versions: dict | None = None) -> int:
    """Makes `findings` the complete set of stored findings for every
    path in `paths`, in one transaction: delete whatever those files had,
    insert the new rows. A path in `paths` with no entry in `findings`
    ends up with none -- that is how a file that was just rewritten and
    is now clean stops showing its old problems. Findings for any path
    NOT in `paths` are ignored, so a caller can never touch a file it
    didn't say it was replacing.

    `file_versions`: {path: workspace_code_files.version just written},
    recorded per row (see migration 0011's `file_version` note).

    Returns the number of rows inserted. Does nothing, and never opens a
    connection, when `paths` is empty. Rows with an unknown severity, an
    empty message, or a line below 1 are skipped rather than allowed to
    fail the whole transaction on a CHECK constraint.
    """
    unique_paths = list(dict.fromkeys(paths or []))
    if not unique_paths:
        return 0
    allowed = set(unique_paths)
    versions = file_versions or {}

    rows = []
    next_seq = {}
    for f in findings or []:
        path = f.get("path")
        line = f.get("line")
        if path not in allowed or f.get("severity") not in SEVERITIES or not f.get("message"):
            continue
        if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
            continue
        seq = next_seq.get(path, 0)
        next_seq[path] = seq + 1
        rows.append((ws_id, path, seq, line, f["severity"], f["message"], f["source"], versions.get(path)))

    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            "delete from workspace_code_findings where workspace_id = %s and file_path = any(%s)",
            (ws_id, unique_paths),
        )
        if rows:
            placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(rows))
            cur.execute(
                "insert into workspace_code_findings "
                "(workspace_id, file_path, seq, line, severity, message, source, file_version) "
                f"values {placeholders}",
                [value for row in rows for value in row],
            )
    return len(rows)


def list_findings(ws_id: str) -> list[dict]:
    """Every stored finding for a workspace, ordered by file, then line
    (file-level findings, line=None, last within their file), then the
    order they were stored in. Runs trusted=True: the route layer has
    already confirmed the caller can see `ws_id` (same reasoning
    workspace_code_files.list_files() documents for itself).

    `file_version` is included so the frontend can tell a finding made
    against an older version of the file (its line may have shifted)
    from one made against the live version.
    """
    with db.cursor(trusted=True) as cur:
        cur.execute(
            "select file_path, line, severity, message, source, file_version "
            "from workspace_code_findings where workspace_id = %s "
            "order by file_path, line nulls last, seq",
            (ws_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "path": r["file_path"],
            "line": r["line"],
            "severity": r["severity"],
            "message": r["message"],
            "source": r["source"],
            "file_version": r["file_version"],
        }
        for r in rows
    ]
