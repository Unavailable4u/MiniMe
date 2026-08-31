"""
eo/self_audit.py -- Phase 1 self-diagnosis tool for the self-modification
experiment (see MiniMe Self-Modification Experiment guide, section 6).

SAFETY CONTRACT -- read this before extending this file:

  This module NEVER writes, deletes, or moves anything on disk, and never
  executes target-file code directly. The ONLY code execution this file
  ever triggers is the target's *own* existing pytest test module (twice:
  once plain, once wrapped in an in-memory cProfile.Profile()), which is
  expected to already run against fakes/mocks -- fake_bus / mock_llm, per
  this repo's existing test fixtures. Profiling output is captured with
  `pstats.Stats(profiler, stream=io.StringIO())` -- an in-memory stream,
  never `Stats.dump_stats(file)` -- so profiling adds zero new disk I/O.
  There is no `open(..., "w")`, no `os.remove`, no `shutil.move`, anywhere
  in this file. If you're tempted to add write capability here, don't --
  that's Phase 2 (candidate-copy + test-gate + human diff review + manual
  promote), a deliberately separate, higher-trust tool. Keeping this file
  read/run-only *by construction* is what makes it safe to point at any
  file in AUDIT_ALLOWED_ROOTS without a human reviewing each invocation
  first.

  WHAT CHANGED FROM THE ORIGINAL NARROW VERSION, AND WHY IT'S STILL SAFE:
  The original tool only ever read the ONE target file. This version also
  builds a repo-wide import graph and symbol cross-reference index, so it
  can answer "how is this file wired to everything else" and "is anything
  here dead code". That means it now *reads* many more files than just the
  target. That widened read scope is still safe under the same contract
  because:
    - It is still 100% read-only. Nothing found during the repo-wide scan
      is ever written anywhere, only summarized into the JSON report.
    - AUDIT_DENY_PATTERNS is applied to EVERY file touched during the scan,
      not just the target -- a secrets/credentials file can never be read,
      quoted, or even have its path echoed into the report.
    - The scan is capped (MAX_SCAN_FILES, MAX_SCAN_BYTES) and only ever
      parses files with `ast.parse` (a pure parse, not an import/exec) or
      greps their text -- it never imports, executes, or evals anything it
      finds while scanning.
    - Reading (parsing text of) an orchestration-core file like
      eo/router.py to see whether it imports the target is NOT the same
      risk as writing to it or importing/executing it. Section 7 of the
      guide restricts *edits* to the orchestration core; it does not (and
      should not) restrict this tool from reading those files to build an
      accurate picture of what depends on what.

  Two-form guard, mirroring the CLI-internal-architecture plan's own
  pattern for exactly this reason (a code-level check can't be talked out
  of it the way a docstring instruction can):
    1. AUDIT_ALLOWED_ROOTS -- this tool will only ever treat a file as a
       valid *audit target* if it's under these directories. Anything else
       is refused before any file I/O on the target.
    2. AUDIT_DENY_PATTERNS -- even inside an allowed root, and even during
       the repo-wide cross-reference scan, filenames matching these
       patterns are refused outright (secrets, env files, credentials), so
       a report never reads or quotes the CONTENTS of a denied file.
       Known, accepted limitation: `import_graph.imports_from_target` is
       parsed from the (already-permitted) target file's own source, so if
       the target legitimately contains a line like
       `from secret_config import KEY`, that import statement's text will
       appear in the report -- same as it would to anyone reading the
       target file directly. What never happens is the tool opening
       secret_config.py itself; its contents (the actual key value) are
       never read, matched, or quoted anywhere.

Usage (manual, from repo root, inside the WSL2 experiment clone):

    python -m eo.self_audit backend/agents/some_agent.py
    python -m eo.self_audit backend/agents/some_agent.py --no-tests
    python -m eo.self_audit backend/agents/some_agent.py --no-profile
    python -m eo.self_audit backend/agents/some_agent.py --no-crossref
    python -m eo.self_audit backend/agents/some_agent.py --no-churn
    python -m eo.self_audit backend/agents/some_agent.py --llm-summary

Output is always a single JSON report on stdout (or `REFUSED: ...` on
stderr with exit code 2 if the path is rejected). Every finding is
diagnosis only -- this tool never proposes or applies a fix. Deciding
what, if anything, to act on is the human's job (see guide section 6).
"""

from __future__ import annotations

import ast
import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from eo.iteration_cap import check_and_increment, IterationCapExceeded
# --------------------------------------------------------------------------
# 1. Path allowlist / denylist -- checked before anything else touches disk.
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]  # adjust if this file moves
# Packages considered "local" for import-RESOLUTION purposes. Deliberately
# separate from AUDIT_ALLOWED_ROOTS, which gates what can be an audit
# TARGET (guide §7) -- a different concern. Reusing that list here was
# silently blinding the wiring check to imports from any local package
# that isn't currently an audit target (eo, memory, relay, graph, ...).
LOCAL_PACKAGE_NAMES = {
    p.name for p in REPO_ROOT.iterdir()
    if p.is_dir() and (p / "__init__.py").exists()
} | {"agents", "utils"}
# Start narrow, per the experiment guide (section 7): worker agents and
# plain utility modules only. Deliberately excludes the orchestration core
# (panel.py, executor.py, inspector.py, registry.py, dynamic_chain.py,
# router.py) as an *audit target* -- add those later, once Phase 1 has
# earned trust on non-critical files. Note: this restricts what can be
# audited, not what can be *read during cross-reference scanning* -- see
# the safety-contract note above.
AUDIT_ALLOWED_ROOTS = [
    REPO_ROOT / "agents",
    REPO_ROOT / "utils",
]

AUDIT_DENY_PATTERNS = [
    re.compile(r"\.env"),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"^\.git/"),
    re.compile(r"id_rsa|\.pem$|\.key$"),
]

# Directories never descended into during the repo-wide scan, regardless of
# AUDIT_ALLOWED_ROOTS -- these are either irrelevant, huge, or not source.
SCAN_SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".tox", ".idea", ".vscode",
}

# Hard caps on the repo-wide scan so this stays "cheap to run" (guide
# section 5's whole premise) even against a large, unfamiliar repo.
MAX_SCAN_FILES = 4000
MAX_SCAN_BYTES_PER_FILE = 1_500_000  # skip pathological single files


class AuditPathRejected(Exception):
    pass


def _validate_target_path(raw_path: str) -> Path:
    """Hard-coded gate. Refuses anything outside AUDIT_ALLOWED_ROOTS or
    matching AUDIT_DENY_PATTERNS, before any read happens."""
    target = Path(raw_path).resolve()

    if not target.is_file():
        raise AuditPathRejected(f"Not a file: {target}")

    if not any(_is_under(target, root) for root in AUDIT_ALLOWED_ROOTS):
        allowed = ", ".join(str(r) for r in AUDIT_ALLOWED_ROOTS)
        raise AuditPathRejected(
            f"{target} is outside the allowed audit roots ({allowed}). "
            f"Add its directory to AUDIT_ALLOWED_ROOTS deliberately if this "
            f"is intentional -- don't just widen it in passing."
        )

    _deny_check(target)
    return target


def _deny_check(path: Path) -> None:
    """Applied to the target AND to every file touched during the
    repo-wide cross-reference scan (see _iter_repo_py_files)."""
    try:
        rel = str(path.relative_to(REPO_ROOT))
    except ValueError:
        rel = str(path)
    for pattern in AUDIT_DENY_PATTERNS:
        if pattern.search(rel):
            raise AuditPathRejected(
                f"{path} matches a deny pattern ({pattern.pattern}) -- refusing to read."
            )


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------
# 2. Findings model
# --------------------------------------------------------------------------

@dataclass
class Finding:
    category: str          # "bug_risk" | "security" | "performance" | "logic_quality" | "wiring"
    severity: str           # "low" | "medium" | "high"
    line: int | None
    description: str
    evidence: str = ""
    confidence: str = "high"  # "high" | "medium" | "low" -- see cross-ref caveats


@dataclass
class SymbolInfo:
    name: str
    kind: str               # "function" | "async_function" | "class"
    line: int
    loc: int
    cyclomatic_complexity: int
    max_nesting: int
    external_calls: list[str] = field(default_factory=list)
    has_try_around_external_call: bool = True
    reference_count_internal: int = 0
    reference_count_elsewhere: int = 0
    reference_examples: list[str] = field(default_factory=list)  # "file:line"
    dynamically_dispatched_hint: bool = False  # decorator suggests framework-invoked
    decorators: list[str] = field(default_factory=list)


@dataclass
class ImportGraph:
    target_module_guess: str
    imports_from_target: list[str] = field(default_factory=list)
    imported_by: list[dict] = field(default_factory=list)   # [{"file":..., "confidence":...}]
    unresolved_imports_in_target: list[dict] = field(default_factory=list)
    scan_truncated: bool = False
    files_scanned: int = 0


@dataclass
class ProfileHotspot:
    function: str
    file: str
    line: int
    ncalls: str
    tottime: float
    cumtime: float


@dataclass
class ExecutionHotspot:
    """Cross-referenced view: a function that is both expensive (per the
    profiler, running against the test suite's mocks/fakes) AND flagged by
    static analysis -- the combination is a much stronger root-cause signal
    than either alone."""
    function: str
    cumtime: float | None
    ncalls: str | None
    static_flags: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class ChurnSummary:
    ran: bool
    reason: str | None = None
    commit_count: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    last_modified: str | None = None


@dataclass
class AuditReport:
    target: str
    generated_at: str
    findings: list[Finding] = field(default_factory=list)
    symbols: list[SymbolInfo] = field(default_factory=list)
    dead_code_candidates: list[dict] = field(default_factory=list)
    import_graph: ImportGraph | None = None
    test_summary: dict | None = None
    profiling: dict | None = None
    execution_hotspots: list[ExecutionHotspot] = field(default_factory=list)
    churn: ChurnSummary | None = None
    llm_summary: str | None = None
    ai_review: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        def conv(o):
            if hasattr(o, "__dataclass_fields__"):
                return {k: conv(v) for k, v in asdict(o).items()}
            return o
        return json.dumps(
            {
                "target": self.target,
                "generated_at": self.generated_at,
                "findings": [asdict(f) for f in self.findings],
                "symbols": [asdict(s) for s in self.symbols],
                "dead_code_candidates": self.dead_code_candidates,
                "import_graph": asdict(self.import_graph) if self.import_graph else None,
                "test_summary": self.test_summary,
                "profiling": self.profiling,
                "execution_hotspots": [asdict(h) for h in self.execution_hotspots],
                "churn": asdict(self.churn) if self.churn else None,
                "llm_summary": self.llm_summary,
                "ai_review": self.ai_review,
                "notes": self.notes,
            },
            indent=2,
        )


# --------------------------------------------------------------------------
# 3. Static analysis (pure ast -- no execution)
# --------------------------------------------------------------------------

LONG_FUNCTION_LOC = 60
DEEP_NESTING = 4
HIGH_COMPLEXITY = 10
EXTERNAL_CALL_HINTS = ("generate_text", "requests.", "httpx.", ".post(", ".get(",
                       "subprocess.", "Groq(", "OpenAI(")
DYNAMIC_DISPATCH_DECORATOR_HINTS = (
    "route", "get", "post", "put", "delete", "patch",           # web frameworks
    "fixture", "pytest",                                          # pytest
    "task", "shared_task", "cron", "scheduled",                   # task queues/schedulers
    "register", "subscribe", "on_event", "listener", "handler",   # event/plugin systems
    "property", "cached_property", "staticmethod", "classmethod",
)


def _static_findings(source: str, path: Path) -> tuple[list[Finding], list[SymbolInfo], ast.AST | None]:
    findings: list[Finding] = []
    symbols: list[SymbolInfo] = []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return (
            [Finding("bug_risk", "high", e.lineno,
                      f"File does not parse: {e.msg}", evidence=str(e))],
            [],
            None,
        )

    lines = source.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            f_findings, sym = _check_function(node, lines)
            findings.extend(f_findings)
            symbols.append(sym)
        if isinstance(node, ast.ClassDef):
            symbols.append(SymbolInfo(
                name=node.name, kind="class", line=node.lineno,
                loc=(getattr(node, "end_lineno", node.lineno) - node.lineno + 1),
                cyclomatic_complexity=0, max_nesting=0,
                decorators=_decorator_names(node),
            ))
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            findings.append(Finding(
                "bug_risk", "medium", node.lineno,
                "Bare `except:` -- swallows everything including "
                "KeyboardInterrupt/SystemExit and hides real bugs.",
            ))

    findings.extend(_check_unused_imports(tree, source))
    findings.extend(_check_todo_fixme(lines))
    findings.extend(_check_unreachable_after_return(tree))

    return findings, symbols, tree


def _decorator_names(node) -> list[str]:
    out = []
    for d in getattr(node, "decorator_list", []):
        if isinstance(d, ast.Name):
            out.append(d.id)
        elif isinstance(d, ast.Attribute):
            out.append(d.attr)
        elif isinstance(d, ast.Call):
            f = d.func
            if isinstance(f, ast.Name):
                out.append(f.id)
            elif isinstance(f, ast.Attribute):
                out.append(f.attr)
    return out


def _check_function(node, lines: list[str]) -> tuple[list[Finding], SymbolInfo]:
    out = []
    start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
    loc = end - start + 1

    if loc > LONG_FUNCTION_LOC:
        out.append(Finding(
            "logic_quality", "medium", node.lineno,
            f"`{node.name}` is {loc} lines long (> {LONG_FUNCTION_LOC}). "
            f"Long functions are harder to reason about and to test in isolation "
            f"-- a candidate for splitting before adding more logic to it.",
        ))

    max_depth = _max_nesting_depth(node)
    if max_depth > DEEP_NESTING:
        out.append(Finding(
            "logic_quality", "medium", node.lineno,
            f"`{node.name}` has nesting depth {max_depth} (> {DEEP_NESTING}). "
            f"Deep nesting is a common source of missed edge cases.",
        ))

    complexity = _cyclomatic_complexity(node)
    if complexity > HIGH_COMPLEXITY:
        severity = "high" if complexity > HIGH_COMPLEXITY * 2 else "medium"
        out.append(Finding(
            "logic_quality", severity, node.lineno,
            f"`{node.name}` has approximate cyclomatic complexity {complexity} "
            f"(> {HIGH_COMPLEXITY}). This counts independent branches/paths "
            f"through the function -- high values correlate with both bug "
            f"risk and slow-to-write tests (many paths to cover).",
        ))

    body_text = "\n".join(lines[start - 1:end])
    external_calls = [hint.rstrip(".( ") for hint in EXTERNAL_CALL_HINTS if hint in body_text]
    has_external_call = bool(external_calls)
    has_try = any(isinstance(n, ast.Try) for n in ast.walk(node))
    if has_external_call and not has_try:
        out.append(Finding(
            "bug_risk", "high", node.lineno,
            f"`{node.name}` appears to make an external call "
            f"(network/subprocess/LLM) with no surrounding try/except in the "
            f"function body. Un-caught exceptions here will propagate up "
            f"uncontrolled.",
        ))

    decorators = _decorator_names(node)
    dyn_hint = any(
        any(h in d.lower() for h in DYNAMIC_DISPATCH_DECORATOR_HINTS)
        for d in decorators
    )

    sym = SymbolInfo(
        name=node.name,
        kind="async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
        line=node.lineno,
        loc=loc,
        cyclomatic_complexity=complexity,
        max_nesting=max_depth,
        external_calls=external_calls,
        has_try_around_external_call=has_try if has_external_call else True,
        dynamically_dispatched_hint=dyn_hint,
        decorators=decorators,
    )
    return out, sym


def _max_nesting_depth(node, depth=0) -> int:
    nesting_types = (ast.If, ast.For, ast.While, ast.With, ast.Try)
    max_d = depth
    for child in ast.iter_child_nodes(node):
        d = depth + 1 if isinstance(child, nesting_types) else depth
        max_d = max(max_d, _max_nesting_depth(child, d))
    return max_d


def _cyclomatic_complexity(node) -> int:
    """McCabe-style approximation: 1 (base path) + one per decision point.
    Counts If/For/While/Try-except/BoolOp-extra-operands/IfExp/Assert and
    comprehension generators/ifs. Approximate, not a certified metric --
    good enough for relative ranking within one file, which is all it's
    used for here."""
    complexity = 1
    for n in ast.walk(node):
        if isinstance(n, (ast.If, ast.IfExp, ast.While, ast.Assert)):
            complexity += 1
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            complexity += 1
        elif isinstance(n, ast.Try):
            complexity += len(n.handlers) or 1
        elif isinstance(n, ast.BoolOp):
            complexity += max(len(n.values) - 1, 0)
        elif isinstance(n, ast.comprehension):
            complexity += 1 + len(n.ifs)
    return complexity


def _check_unused_imports(tree, source: str) -> list[Finding]:
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                imported[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                if name != "*":
                    imported[name] = node.lineno

    used_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}

    findings = []
    for name, lineno in imported.items():
        if name in used_names:
            continue
        occurrences = len(re.findall(rf"\b{re.escape(name)}\b", source))
        if occurrences > 1:
            continue
        findings.append(Finding(
            "logic_quality", "low", lineno,
            f"`{name}` imported but doesn't appear to be used elsewhere in the file.",
        ))
    return findings


def _check_todo_fixme(lines: list[str]) -> list[Finding]:
    findings = []
    for i, line in enumerate(lines, start=1):
        if re.search(r"#\s*(TODO|FIXME|XXX|HACK)\b", line):
            findings.append(Finding(
                "logic_quality", "low", i,
                "Marked TODO/FIXME/HACK in source -- known incomplete or "
                "suspect logic, worth surfacing rather than leaving buried.",
                evidence=line.strip(),
            ))
    return findings


def _check_unreachable_after_return(tree) -> list[Finding]:
    """Flags statements that follow an unconditional return/raise/continue/
    break within the same block -- dead code by construction, not a guess."""
    findings = []
    terminators = (ast.Return, ast.Raise, ast.Continue, ast.Break)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body[:-1]):
            if isinstance(stmt, terminators):
                nxt = body[i + 1]
                findings.append(Finding(
                    "wiring", "medium", getattr(nxt, "lineno", None),
                    f"Unreachable code: this statement follows an unconditional "
                    f"`{type(stmt).__name__.lower()}` in the same block and can "
                    f"never execute.",
                ))
                break  # one finding per block is enough signal
    return findings


# --------------------------------------------------------------------------
# 4. Optional: semgrep (read-only, single file, if installed)
# --------------------------------------------------------------------------

def _semgrep_findings(path: Path) -> list[Finding]:
    if shutil.which("semgrep") is None:
        return []
    try:
        proc = subprocess.run(
            ["semgrep", "--config=auto", "--json", "--quiet", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        data = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []

    findings = []
    for result in data.get("results", []):
        findings.append(Finding(
            "security", result.get("extra", {}).get("severity", "medium").lower(),
            result.get("start", {}).get("line"),
            result.get("extra", {}).get("message", "semgrep finding"),
            evidence=result.get("check_id", ""),
        ))
    return findings


# --------------------------------------------------------------------------
# 5. Repo-wide, read-only cross-reference scan: import graph + dead code.
#    Every file this touches goes through _deny_check first. This never
#    executes or imports any of the scanned files -- ast.parse only.
# --------------------------------------------------------------------------

def _iter_repo_py_files(max_files: int = MAX_SCAN_FILES):
    count = 0
    truncated = False
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in SCAN_SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES_PER_FILE:
                continue
        except OSError:
            continue
        try:
            _deny_check(path)
        except AuditPathRejected:
            continue  # silently skip -- never surface denied paths, even as "skipped: X"
        if count >= max_files:
            truncated = True
            break
        count += 1
        yield path
    return truncated


def _module_dotted_path(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _build_import_graph(target: Path, target_tree: ast.AST) -> ImportGraph:
    target_module = _module_dotted_path(target)
    target_stem = target.stem

    imports_from_target = []
    unresolved = []
    for node in ast.walk(target_tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports_from_target.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for alias in node.names:
                imports_from_target.append(f"{mod}.{alias.name}" if mod else alias.name)

        # Best-effort resolution check: local (repo-relative) imports only.
    for imp in imports_from_target:
        clean = imp.lstrip(".")
        if not clean:
            continue
        candidate_rel = Path(*clean.split("."))
        hits = list(REPO_ROOT.glob(f"**/{candidate_rel}.py")) + \
               list(REPO_ROOT.glob(f"**/{candidate_rel}/__init__.py"))
        parts = clean.split(".")
        if len(parts) > 1:
            parent_rel = Path(*parts[:-1])
            hits += list(REPO_ROOT.glob(f"**/{parent_rel}.py")) + \
                    list(REPO_ROOT.glob(f"**/{parent_rel}/__init__.py"))
        hits = [h for h in hits if not any(p in SCAN_SKIP_DIRS for p in h.parts)]
        looks_local = clean.split(".")[0] in LOCAL_PACKAGE_NAMES or imp.startswith(".")
        if looks_local and not hits:
            unresolved.append({"import": imp, "note": "no matching local file found by name"})

    imported_by = []
    files_scanned = 0
    truncated = False
    for path in _iter_repo_py_files():
        files_scanned += 1
        if path == target:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(text, filename=str(path))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            confidence = None
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == target_module or node.module.endswith("." + target_stem):
                    confidence = "high" if node.module == target_module else "medium"
                elif node.module == target_stem:
                    confidence = "low"  # name-only match, could be a same-named module elsewhere
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == target_module:
                        confidence = "high"
                    elif alias.name.endswith("." + target_stem) or alias.name == target_stem:
                        confidence = confidence or "low"
            if confidence:
                imported_by.append({
                    "file": str(path.relative_to(REPO_ROOT)),
                    "line": node.lineno,
                    "confidence": confidence,
                })
                break

    return ImportGraph(
        target_module_guess=target_module,
        imports_from_target=sorted(set(imports_from_target)),
        imported_by=imported_by,
        unresolved_imports_in_target=unresolved,
        scan_truncated=files_scanned >= MAX_SCAN_FILES,
        files_scanned=files_scanned,
    )


def _internal_reference_counts(target_tree: ast.AST, names: dict[str, SymbolInfo]) -> dict[str, int]:
    """Count references to each symbol name WITHIN the target file itself
    (e.g. `process` calling `summarize_events` in the same module). This
    must run before anything is called "dead code" -- a same-file-only
    caller is a real caller, even though the repo-wide scan (which skips
    the target file) would never see it."""
    # A def/class's own name is a plain string on the FunctionDef/ClassDef
    # node, never an ast.Name/ast.Attribute node -- so this walk naturally
    # never counts a symbol's own header as a reference to itself.
    counts = {name: 0 for name in names}
    for node in ast.walk(target_tree):
        if isinstance(node, ast.Name) and node.id in counts:
            counts[node.id] += 1
        elif isinstance(node, ast.Attribute) and node.attr in counts:
            counts[node.attr] += 1
    return counts


def _cross_reference_symbols(target: Path, target_tree: ast.AST,
                              symbols: list[SymbolInfo]) -> list[dict]:
    """For each top-level def/class in the target, count how many OTHER
    files in the repo reference that name at all (substring/identifier
    match, not true call-graph resolution -- see caveat in the returned
    dict), AND how many times it's referenced from within the same file.
    A symbol is only a dead-code *candidate* if BOTH counts are zero --
    otherwise a same-file-only helper (like `process` calling
    `summarize_events` above it) would be wrongly flagged as unused."""
    names = {
        s.name: s for s in symbols
        if not (s.name.startswith("__") and s.name.endswith("__"))
        and s.name != "main"
    }
    if not names:
        return []

    internal_counts = _internal_reference_counts(target_tree, names)

    external_counts = {name: 0 for name in names}
    examples: dict[str, list[str]] = {name: [] for name in names}

    for path in _iter_repo_py_files():
        if path == target:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name in names:
            if re.search(rf"\b{re.escape(name)}\b", text):
                external_counts[name] += 1
                if len(examples[name]) < 5:
                    lineno = next(
                        (i for i, l in enumerate(text.splitlines(), 1)
                         if re.search(rf"\b{re.escape(name)}\b", l)),
                        None,
                    )
                    examples[name].append(f"{path.relative_to(REPO_ROOT)}:{lineno}")

    candidates = []
    for name, sym in names.items():
        sym.reference_count_internal = internal_counts[name]
        sym.reference_count_elsewhere = external_counts[name]
        sym.reference_examples = examples[name]
        if internal_counts[name] == 0 and external_counts[name] == 0:
            confidence = "low" if sym.dynamically_dispatched_hint else "medium"
            note = (
                "Decorator suggests this may be invoked dynamically by a "
                "framework/router/registry rather than called by name -- "
                "treat as a weak signal, verify manually."
                if sym.dynamically_dispatched_hint else
                "Not referenced within this file, and no other scanned file "
                "references this identifier by name either. Could still be "
                "reached via getattr/dynamic dispatch, string-based routing, "
                "or __init__.py re-exports this scan doesn't trace -- verify "
                "before assuming it's truly unused."
            )
            candidates.append({
                "name": sym.name,
                "kind": sym.kind,
                "line": sym.line,
                "confidence": confidence,
                "note": note,
            })
        elif internal_counts[name] > 0 and external_counts[name] == 0 and sym.kind != "class":
            candidates.append({
                "name": sym.name,
                "kind": sym.kind,
                "line": sym.line,
                "confidence": "info",
                "note": (
                    f"Used {internal_counts[name]}x within this file but never "
                    f"referenced elsewhere in the repo -- looks like a private "
                    f"helper, not dead code. Listed for completeness, not as a "
                    f"problem."
                ),
            })
    return candidates


# --------------------------------------------------------------------------
# 6. Run the file's OWN existing tests, with timing, and optionally with
#    an in-memory profiler wrapped around the same run.
# --------------------------------------------------------------------------

def _find_matching_test_file(target: Path) -> Path | None:
    stem = target.stem  # e.g. "calendar_agent"
    candidates = list(REPO_ROOT.glob(f"tests/**/test_{stem}.py"))
    candidates += list(REPO_ROOT.glob(f"tests/**/test_agent_{stem}.py"))
    return candidates[0] if candidates else None


def _run_tests(target: Path) -> dict:
    test_file = _find_matching_test_file(target)
    if test_file is None:
        return {"ran": False, "reason": "no matching test file found (by naming convention)"}

    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q",
         "--durations=0", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
    )
    elapsed = time.perf_counter() - start

    return {
        "ran": True,
        "test_file": str(test_file.relative_to(REPO_ROOT)),
        "passed": proc.returncode == 0,
        "elapsed_seconds": round(elapsed, 3),
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-40:]),
    }


_PROFILE_RUNNER = r"""
import cProfile, io, pstats, sys, pytest
pr = cProfile.Profile()
pr.enable()
exit_code = pytest.main([{test_file!r}, "-q", "--no-header", "-p", "no:cacheprovider"])
pr.disable()
stream = io.StringIO()
stats = pstats.Stats(pr, stream=stream)
stats.sort_stats("cumulative")
stats.print_stats(300)
print("===PROFILE_START===")
print(stream.getvalue())
print("===PROFILE_END===")
sys.exit(0)
"""

_PSTATS_LINE_RE = re.compile(
    r"^\s*(?P<ncalls>[\d/]+)\s+(?P<tottime>[\d.]+)\s+[\d.]+\s+"
    r"(?P<cumtime>[\d.]+)\s+[\d.]+\s+(?P<loc>.+)$"
)
_LOC_RE = re.compile(r"^(?P<file>.+):(?P<line>\d+)\((?P<func>.+)\)$")


def _profile_tests(target: Path, test_file: Path) -> dict | None:
    """Runs the SAME existing test file (nothing new executes) under an
    in-memory cProfile.Profile(). Never writes a .prof file to disk --
    stats are rendered to an io.StringIO() and only that text is captured
    from the subprocess's stdout. Filters results down to frames whose
    file matches the target module, so the report answers "which function
    IN THIS FILE is the actual time sink", not a whole-process profile
    dump."""
    code = _PROFILE_RUNNER.format(test_file=str(test_file))
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ran": False, "reason": "profiling run timed out (300s)"}

    out = proc.stdout
    if "===PROFILE_START===" not in out:
        return {"ran": False, "reason": "profiler produced no output",
                 "stderr_tail": "\n".join(proc.stderr.splitlines()[-20:])}

    block = out.split("===PROFILE_START===", 1)[1].split("===PROFILE_END===", 1)[0]
    target_name = target.name
    target_str = str(target)

    hotspots: list[ProfileHotspot] = []
    for line in block.splitlines():
        m = _PSTATS_LINE_RE.match(line)
        if not m:
            continue
        loc_m = _LOC_RE.match(m.group("loc").strip())
        if not loc_m:
            continue
        file_part = loc_m.group("file")
        if target_name not in file_part and target_str not in file_part:
            continue
        hotspots.append(ProfileHotspot(
            function=loc_m.group("func"),
            file=file_part,
            line=int(loc_m.group("line")) if loc_m.group("line").isdigit() else 0,
            ncalls=m.group("ncalls"),
            tottime=float(m.group("tottime")),
            cumtime=float(m.group("cumtime")),
        ))

    hotspots.sort(key=lambda h: h.cumtime, reverse=True)
    return {
        "ran": True,
        "caveat": (
            "This measures time inside the process while running the "
            "target's EXISTING test suite -- which, per this repo's "
            "convention, exercises this file against fakes/mocks "
            "(fake_bus / mock_llm), not real network/LLM latency. Treat "
            "this as a signal about this file's own CPU-bound logic, not "
            "as real-world production latency for any external call it "
            "makes -- see the guide's note on this before trusting a "
            "'slow' verdict for a function whose real cost is network-bound."
        ),
        "hotspots_in_target": [asdict(h) for h in hotspots[:20]],
    }


def _build_execution_hotspots(profiling: dict | None, symbols: list[SymbolInfo]) -> list[ExecutionHotspot]:
    if not profiling or not profiling.get("ran"):
        return []
    by_name = {s.name: s for s in symbols}
    out = []
    for h in profiling.get("hotspots_in_target", []):
        sym = by_name.get(h["function"])
        flags = []
        if sym:
            if sym.cyclomatic_complexity > HIGH_COMPLEXITY:
                flags.append(f"high cyclomatic complexity ({sym.cyclomatic_complexity})")
            if sym.max_nesting > DEEP_NESTING:
                flags.append(f"deep nesting ({sym.max_nesting})")
            if sym.loc > LONG_FUNCTION_LOC:
                flags.append(f"long function ({sym.loc} LOC)")
            if sym.external_calls:
                flags.append(f"external calls: {', '.join(sym.external_calls)}")
        if flags or h["cumtime"] > 0:
            note = (
                "Static and dynamic signals agree -- strong root-cause candidate."
                if flags else
                "Shows up as time-expensive in profiling but no static red flags -- "
                "may simply be doing real, necessary work; not automatically a problem."
            )
            out.append(ExecutionHotspot(
                function=h["function"], cumtime=h["cumtime"], ncalls=h["ncalls"],
                static_flags=flags, note=note,
            ))
    return out


# --------------------------------------------------------------------------
# 7. Optional: git churn (read-only `git log`, no working-tree changes)
# --------------------------------------------------------------------------

def _churn_summary(target: Path) -> ChurnSummary:
    if not (REPO_ROOT.parent / ".git").exists() or shutil.which("git") is None:
        return ChurnSummary(ran=False, reason="not a git repo, or git not installed")
    try:
        rel = str(target.relative_to(REPO_ROOT))
        log_proc = subprocess.run(
            ["git", "log", "--follow", "--format=%at", "--", rel],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30,
        )
        numstat_proc = subprocess.run(
            ["git", "log", "--follow", "--numstat", "--format=", "--", rel],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return ChurnSummary(ran=False, reason=f"git invocation failed: {e}")

    timestamps = [int(t) for t in log_proc.stdout.split() if t.isdigit()]
    if not timestamps:
        return ChurnSummary(ran=False, reason="no git history found for this file")

    added = removed = 0
    for line in numstat_proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            added += int(parts[0])
            removed += int(parts[1])

    return ChurnSummary(
        ran=True,
        commit_count=len(timestamps),
        lines_added=added,
        lines_removed=removed,
        last_modified=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(timestamps))),
    )


# --------------------------------------------------------------------------
# 8. Optional: LLM synthesis over the raw findings (opt-in, cheap chain only)
# --------------------------------------------------------------------------

def _llm_summary(target: Path, findings: list[Finding], test_summary: dict | None,
                  execution_hotspots: list[ExecutionHotspot],
                  dead_code_candidates: list[dict], chain: list) -> str | None:
    try:
        from utils.llm_client import generate_text  # local import: optional dependency
    except ImportError:
        return None

    findings_text = "\n".join(
        f"- [{f.severity}/{f.category}/{f.confidence}] line {f.line}: {f.description}"
        for f in findings
    ) or "(no static findings)"
    hotspot_text = "\n".join(
        f"- {h.function}: cumtime={h.cumtime}s, flags={h.static_flags}"
        for h in execution_hotspots
    ) or "(no execution hotspots)"
    dead_text = "\n".join(
        f"- {d['name']} ({d['confidence']} confidence): {d['note']}"
        for d in dead_code_candidates
    ) or "(no dead-code candidates)"
    test_text = json.dumps(test_summary, indent=2) if test_summary else "(tests not run)"

    system_prompt = (
        "You are a terse code-review assistant. You are given static-analysis "
        "findings, execution-hotspot data, dead-code candidates, and test-run "
        "output for ONE file. Summarize, in plain language, which findings are "
        "most worth a human's attention and why, in under 250 words. Do not "
        "invent findings not present in the input. Do not suggest edits -- "
        "this is diagnosis only, not a fix. Be explicit about confidence "
        "levels where given; don't state a low-confidence finding as fact."
    )
    user_content = (
        f"File: {target.relative_to(REPO_ROOT)}\n\n"
        f"Static findings:\n{findings_text}\n\n"
        f"Execution hotspots:\n{hotspot_text}\n\n"
        f"Dead-code candidates:\n{dead_text}\n\n"
        f"Test run:\n{test_text}"
    )

    try:
        text, *_ = generate_text(system_prompt, user_content, chain,
                                  agent_name="self_audit")
        return text
    except Exception as e:  # noqa: BLE001 -- diagnostic tool, never raise on this
        return f"(LLM summary unavailable: {e})"


# --------------------------------------------------------------------------
# 9. Entry point
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 8a. Multi-LLM review (opt-in, 2026-08-30 -- see SELF_MOD_LOG.md).
# Unlike _llm_summary() above (which only sees pre-computed findings),
# this gives each of 3 INDEPENDENT reviewers the target's actual source,
# so they can reason about wiring/data-flow correctness and spot bugs
# static analysis wouldn't catch -- the whole point of adding this was
# that a purely static + mocked-test tool wasn't meaningfully different
# from CI/lint/semgrep the repo already has. Deliberately different
# providers, not the same model 3x (diversity of judgment > throughput
# here) -- Groq and Mistral reuse the existing shared, low-volume,
# no-number keys already used by several other agents (see
# agents/documentation_agent.py, agents/dataset_analyst.py, etc.).
# Gemini has no such shared key -- every numbered GEMINI_API_KEY_N in
# eo/registry.py's AGENT_CAPABILITIES is already claimed by a tagged
# role or a dedicated agent's own CHAIN (see
# agents/performance_reviewer.py's docstring for _13/_14 specifically)
# -- so _select_gemini_review_chain() below picks whichever tagged key
# currently has the most quota headroom instead, same ranking approach
# agents/reviewer.py's own _select_workers() already uses.
# --------------------------------------------------------------------------

GROQ_REVIEW_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"},
]
MISTRAL_REVIEW_CHAIN = [
    {"provider": "mistral", "model": "mistral-medium-latest", "key_env": "MISTRAL_API_KEY"},
]


def _select_gemini_review_chain() -> list:
    """Picks a currently-usable tagged Gemini key: filters out anything
    eo/quota_sentinel.py's cooldown tracking marks as cooling_down (a
    REAL prior rate-limit hit, not a guess) FIRST, then ranks what's
    left by today's usage pct, lowest first -- same two-step approach
    agents/reviewer.py's own _select_workers() uses. Deliberately does
    NOT hardcode any specific "these key numbers are bad" list: a
    manual key-health check run on 2026-08-30 showed 12 of 17 Gemini
    keys mid-rate-limit at that moment (see SELF_MOD_LOG.md) -- that
    list is a snapshot, not a fact, and would already be wrong by the
    next time this runs. Falls through to an empty chain (Gemini
    skipped, not crashed) if every tagged key is currently cooling
    down -- the two other reviewers still run either way."""
    try:
        from eo.registry import AGENT_CAPABILITIES
        from eo.quota_sentinel import get_quota_snapshot
    except ImportError:
        return []
    pool = [k for k, info in AGENT_CAPABILITIES.items() if info.get("provider") == "gemini"]
    if not pool:
        return []
    snapshot = get_quota_snapshot()
    usable = [k for k in pool if not (snapshot.get(k) or {}).get("cooling_down", False)]
    if not usable:
        return []  # every tagged Gemini key is currently cooling down -- skip, don't guess
    ranked = sorted(usable, key=lambda k: (snapshot.get(k) or {}).get("pct") or 0.0)
    return [{"provider": "gemini", "model": "gemini-3.6-flash", "key_env": ranked[0]}]


# Reserved output budget used ONLY when planning how many chars of
# SOURCE fit in a chunk (a conservative worst-case reservation so chunk
# boundaries stay stable regardless of which provider ends up using
# them). NOT a per-call output cap on its own -- see
# _review_output_budget() below, which sizes the ACTUAL cap per call
# from each call's real estimated input and each provider's real tpm.
# Bug fix (2026-09-01 v2): an earlier version of this fix used this
# same flat 1500 figure as the literal max_tokens for every review
# call, groq and mistral alike. That starved mistral (25000 tpm --
# plenty of headroom) down to the same tiny budget sized for groq's
# worst case (8000 tpm), truncating both providers' reviews mid-output
# (finish_reason=length) even though mistral had 3x the room to spare.
_REVIEW_CHUNK_OUTPUT_TOKENS = 1500

# Bug fix (2026-09-01 v2): upper bound on the ADAPTIVE per-call output
# cap below. A review has little real use for more than this, and an
# unbounded cap would just re-invite the original "can never fit"
# tpm-ceiling failure on a borderline input by claiming more output
# room than a low-tpm provider can actually spare.
_REVIEW_OUTPUT_CEILING_TOKENS = 4096


def _review_output_budget(system_prompt: str, user_content: str,
                           tpm_limit: "int | None") -> "int | None":
    """Sizes a per-call max_tokens override to THIS call's real
    estimated input, instead of reusing one flat constant for every
    provider regardless of how much headroom it actually has. Returns
    None when tpm_limit is unknown for this provider/model -- same
    "don't fabricate a number, fall back to the flat model-family
    default" posture the rest of this budget-aware code already
    follows (see _max_tokens_for()'s own docstring).

    Floors at _REVIEW_CHUNK_OUTPUT_TOKENS: chunk boundaries were chosen
    assuming this much output room would be reserved, so this is always
    safe to grant, never enough to blow the tpm ceiling that sizing
    already accounted for. Ceilings at _REVIEW_OUTPUT_CEILING_TOKENS:
    a high-tpm provider like mistral (25000 tpm) shouldn't be allowed
    to claim a near-unbounded amount just because a small file's
    estimated input leaves a lot of arithmetic headroom.
    """
    if tpm_limit is None:
        return None
    from utils.llm_client import _estimate_tokens_for_call, _MAX_TOKENS_SAFETY_MARGIN
    estimated = _estimate_tokens_for_call(system_prompt, user_content)
    available = tpm_limit - _MAX_TOKENS_SAFETY_MARGIN - estimated
    return max(_REVIEW_CHUNK_OUTPUT_TOKENS, min(_REVIEW_OUTPUT_CEILING_TOKENS, available))


def _split_source_by_symbols(source: str, symbols: list[SymbolInfo],
                              budget_chars: int) -> list[str]:
    """Splits `source` into coherent chunks -- whole functions/classes,
    never one cut in half -- sized to fit under `budget_chars`. Reuses
    the symbol table self_audit already computed (line/loc per symbol)
    as the boundary, same "one small unit per call" shape as
    agents/code_writer_lean.py's one-module-spec-per-call pattern,
    applied to review instead of generation. Falls back to a flat
    line-count split only if no symbols were found at all (e.g. a file
    the symbol pass couldn't parse) -- still chunked, just without
    function-aware boundaries.
    """
    lines = source.split("\n")
    if not symbols:
        chunks, current, current_len = [], [], 0
        for line in lines:
            current.append(line)
            current_len += len(line) + 1
            if current_len >= budget_chars:
                chunks.append("\n".join(current))
                current, current_len = [], 0
        if current:
            chunks.append("\n".join(current))
        return chunks or [source]

    ordered = sorted(symbols, key=lambda s: s.line)
    # Preamble = everything before the first symbol (imports, module
    # docstring, module-level constants) -- travels with the first chunk
    # rather than getting its own, since it's rarely useful reviewed alone.
    preamble = "\n".join(lines[:ordered[0].line - 1]).strip()

    chunks: list[str] = []
    current_parts = [preamble] if preamble else []
    current_len = len(preamble)

    for sym in ordered:
        start, end = sym.line - 1, sym.line - 1 + sym.loc
        sym_source = "\n".join(lines[start:end])
        if current_parts and current_len + len(sym_source) > budget_chars:
            chunks.append("\n\n".join(current_parts))
            current_parts, current_len = [], 0
        current_parts.append(sym_source)
        current_len += len(sym_source)

    if current_parts:
        chunks.append("\n\n".join(current_parts))
    return chunks or [source]


def _multi_llm_review(target: Path, source: str, findings: list[Finding],
                       symbols: list[SymbolInfo], import_graph: ImportGraph | None,
                       test_summary: dict | None) -> dict:
    """Runs 2-3 independent reviewers over the target's real source.
    Every reviewer's raw output is kept SEPARATE (no merging/voting) --
    this is a diagnosis tool for a human to read and verify, not a
    production gate that needs one verdict (contrast
    agents/reviewer.py's own aggregate_reviews(), which DOES merge,
    because it gates a task). Never raises: a failed reviewer gets an
    "error" field instead of tanking the whole audit.

    Chunking (added after the 2026-08-31 tpm-ceiling finding): a
    whole-file prompt was the only call site in the codebase that
    didn't follow the rest of the system's small-context-per-call
    convention (contrast code_writer_lean.py's one-module-spec-per-call).
    For a low-tpm reviewer (Groq's models here run 6000-8000 tpm), that
    guaranteed a pre-flight rejection on anything but a tiny file,
    independent of prompt quality. Each provider now gets its own
    per-call budget computed from its real verified tpm figure
    (rate_ledger._tpm_limit_for, the same source _max_tokens_for already
    trusts) minus the safety margin and this call's reserved output
    budget; if the whole file fits, one call goes out exactly as
    before. If it doesn't, source is split along symbol boundaries
    (_split_source_by_symbols) and reviewed in sequential passes, each
    chunk labeled with which lines it covers and reminded that static
    context (findings/symbols/imports/tests) already describes the
    whole file, not just this slice -- individual chunk reviews are
    concatenated under one "review" string per provider, in order, so
    nothing about the result shape changes for callers."""
    try:
        from utils.llm_client import (generate_text, _estimate_tokens_for_call,
                                       _MAX_TOKENS_SAFETY_MARGIN)
        from utils import rate_ledger
    except ImportError:
        return {"error": "utils.llm_client not importable -- skipping AI review"}

    findings_text = "\n".join(
        f"- [{f.severity}/{f.category}/{f.confidence}] line {f.line}: {f.description}"
        for f in findings
    ) or "(no static findings)"
    symbols_text = "\n".join(
        f"- {s.name} ({s.kind}, line {s.line}): complexity={s.cyclomatic_complexity}, "
        f"external_calls={s.external_calls}, refs_elsewhere={s.reference_count_elsewhere} "
        f"(name-only match, treat with skepticism on common names)"
        for s in symbols
    ) or "(no symbols)"
    imports_text = json.dumps(import_graph.imports_from_target if import_graph else [], indent=2)
    test_text = json.dumps(test_summary, indent=2) if test_summary else "(tests not run)"

    system_prompt = (
        "You are one of several INDEPENDENT reviewers examining the same Python file -- "
        "your job is to reason carefully, not to agree with anything else. You will not "
        "see any other reviewer's output. Given the file's source (possibly only part of "
        "it -- see below) plus static analysis context covering the WHOLE file, identify:\n"
        "1. WIRING / DATA-FLOW issues: does data returned from a call actually get used "
        "correctly by the caller? Are error paths handled, or silently swallowed? Does "
        "anything write to one key/variable but read from a different one?\n"
        "2. BUGS static analysis likely missed: logic errors, off-by-one, wrong "
        "condition, anything that would only be caught by actually reading the code.\n"
        "3. EFFICIENCY / COMPLEXITY: any clearly wasteful pattern (repeated work, "
        "unnecessary nesting, an obviously better data structure) -- note the static "
        "complexity numbers given as context, don't just restate them.\n\n"
        "Cite line numbers where you can. Be explicit about your own confidence -- say "
        "so plainly if you're not sure. Do not suggest a fix or rewrite -- this is "
        "diagnosis only. If you find nothing in a category, say so briefly rather than "
        "inventing something to fill the section."
    )
    static_context = (
        f"=== Static findings for the WHOLE file (already known, don't just repeat these) ===\n"
        f"{findings_text}\n\n"
        f"=== Symbols for the WHOLE file ===\n{symbols_text}\n\n"
        f"=== Imports ===\n{imports_text}\n\n"
        f"=== Test run ===\n{test_text}"
    )

    reviewers = {
        "groq": GROQ_REVIEW_CHAIN,
        "mistral": MISTRAL_REVIEW_CHAIN,
        "gemini": _select_gemini_review_chain(),
    }
    result: dict = {}
    for name, chain in reviewers.items():
        if not chain:
            result[name] = {"error": f"no chain available for {name}"}
            continue

        model = chain[0]["model"]
        tpm_limit = rate_ledger._tpm_limit_for(chain[0]["provider"], model)
        fixed_tokens = _estimate_tokens_for_call(
            system_prompt, f"File: {target.relative_to(REPO_ROOT)}\n\n{static_context}"
        )

        if tpm_limit is None:
            # No verified tpm figure for this provider/model -- same
            # "don't fabricate a number" posture _max_tokens_for() takes;
            # send the whole file in one call as before.
            source_budget_chars = None
        else:
            available = tpm_limit - _MAX_TOKENS_SAFETY_MARGIN - fixed_tokens - _REVIEW_CHUNK_OUTPUT_TOKENS
            source_budget_chars = max(available, 200) * 4  # 200-token floor: always make forward progress

        whole_file_fits = (
            source_budget_chars is None
            or len(source) <= source_budget_chars
        )

        # Bug fix (2026-09-01): max_tokens was only ever capped down to
        # _REVIEW_CHUNK_OUTPUT_TOKENS on the chunked branch below. A file
        # small enough to fit whole (whole_file_fits=True) still went out
        # with NO override, so _max_tokens_for() fell back to its flat
        # model-family default (capped only at tpm_limit - safety margin,
        # e.g. 6500 for groq's 8000-tpm model) regardless of how small the
        # actual input was. For a low-tpm model, input + that flat default
        # can still exceed the whole per-minute budget on its own -- the
        # exact "can never fit" failure this chunking system was meant to
        # prevent. v2 (below): the cap itself is now computed per call via
        # _review_output_budget() rather than reusing one flat constant
        # for every provider -- see that function's docstring for why a
        # single shared number under-served a higher-tpm provider like
        # mistral once groq's fix exposed the same gap on its side too.

        try:
            if whole_file_fits:
                user_content = (
                    f"File: {target.relative_to(REPO_ROOT)}\n\n"
                    f"=== SOURCE (complete file) ===\n{source}\n\n{static_context}"
                )
                output_budget = _review_output_budget(system_prompt, user_content, tpm_limit)
                call_chain = (
                    [dict(step, max_tokens=output_budget) for step in chain]
                    if output_budget is not None else chain
                )
                text = generate_text(system_prompt, user_content, call_chain,
                                      agent_name=f"self_audit-{name}-reviewer")
                result[name] = {"model": model, "review": text}
            else:
                chunks = _split_source_by_symbols(source, symbols, source_budget_chars)
                reviews = []
                for i, chunk_source in enumerate(chunks, start=1):
                    user_content = (
                        f"File: {target.relative_to(REPO_ROOT)}\n\n"
                        f"=== SOURCE (chunk {i}/{len(chunks)} of this file -- NOT the "
                        f"whole file, the static context below IS for the whole file) ===\n"
                        f"{chunk_source}\n\n{static_context}"
                    )
                    output_budget = _review_output_budget(system_prompt, user_content, tpm_limit)
                    chunk_chain = (
                        [dict(step, max_tokens=output_budget) for step in chain]
                        if output_budget is not None else chain
                    )
                    chunk_text = generate_text(system_prompt, user_content, chunk_chain,
                                                agent_name=f"self_audit-{name}-reviewer-chunk{i}")
                    reviews.append(f"--- Chunk {i}/{len(chunks)} ---\n{chunk_text}")
                result[name] = {
                    "model": model,
                    "review": "\n\n".join(reviews),
                    "chunked": True,
                    "chunk_count": len(chunks),
                }
        except Exception as e:  # noqa: BLE001 -- one bad reviewer shouldn't tank the others
            result[name] = {"model": model, "error": str(e)}
    return result


def audit_file(raw_path: str, run_tests: bool = True, run_profile: bool = True,
                run_crossref: bool = True, run_churn: bool = True,
                use_semgrep: bool = True, llm_chain: list | None = None,
                run_ai_review: bool = False) -> AuditReport:
    target = _validate_target_path(raw_path)
    source = target.read_text(encoding="utf-8")  # read-only

    findings, symbols, tree = _static_findings(source, target)
    if use_semgrep:
        findings.extend(_semgrep_findings(target))

    notes: list[str] = []

    import_graph = None
    dead_code_candidates: list[dict] = []
    if run_crossref and tree is not None:
        import_graph = _build_import_graph(target, tree)
        if import_graph.scan_truncated:
            notes.append(
                f"Repo-wide scan hit MAX_SCAN_FILES={MAX_SCAN_FILES} and stopped early -- "
                f"import_graph.imported_by and dead_code_candidates may be incomplete."
            )
        for u in import_graph.unresolved_imports_in_target:
            findings.append(Finding(
                "wiring", "medium", None,
                f"Import `{u['import']}` in this file looks local but no matching "
                f"file was found by name -- possibly a renamed/moved/deleted module.",
                confidence="low",
            ))
        dead_code_candidates = _cross_reference_symbols(target, tree, symbols)
        if not import_graph.imported_by:
            findings.append(Finding(
                "wiring", "medium", None,
                "No other file in the repo appears to import this module. Either "
                "it's an entry point (invoked directly, e.g. via `python -m`), "
                "or nothing currently wires it into the running system.",
                confidence="medium",
            ))

    test_summary = _run_tests(target) if run_tests else None

    profiling = None
    execution_hotspots: list[ExecutionHotspot] = []
    if run_profile:
        test_file = _find_matching_test_file(target)
        if test_file is None:
            profiling = {"ran": False, "reason": "no matching test file found -- can't profile"}
        else:
            profiling = _profile_tests(target, test_file)
            execution_hotspots = _build_execution_hotspots(profiling, symbols)

    churn = _churn_summary(target) if run_churn else None

    summary = None
    if llm_chain:
        summary = _llm_summary(target, findings, test_summary, execution_hotspots,
                                dead_code_candidates, llm_chain)

    ai_review = None
    if run_ai_review:
        ai_review = _multi_llm_review(target, source, findings, symbols,
                                       import_graph, test_summary)

    return AuditReport(
        target=str(target.relative_to(REPO_ROOT)),
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        findings=findings,
        symbols=symbols,
        dead_code_candidates=dead_code_candidates,
        import_graph=import_graph,
        test_summary=test_summary,
        profiling=profiling,
        execution_hotspots=execution_hotspots,
        churn=churn,
        llm_summary=summary,
        ai_review=ai_review,
        notes=notes,
    )


def _main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="File to audit (must be under AUDIT_ALLOWED_ROOTS)")
    parser.add_argument("--no-tests", action="store_true", help="Skip running the matching test file")
    parser.add_argument("--no-profile", action="store_true", help="Skip in-memory cProfile run")
    parser.add_argument("--no-crossref", action="store_true",
                         help="Skip the repo-wide import graph / dead-code scan")
    parser.add_argument("--no-churn", action="store_true", help="Skip git log churn summary")
    parser.add_argument("--no-semgrep", action="store_true", help="Skip semgrep even if installed")
    parser.add_argument("--llm-summary", action="store_true",
                         help="Also synthesize a plain-language summary via generate_text "
                              "(requires a chain to be wired in -- see EXAMPLE_CHAIN below)")
    parser.add_argument("--ai-review", action="store_true",
                         help="Run 3 independent LLM reviewers (Groq/Mistral/Gemini) over "
                              "the real source -- up to 3 real LLM calls per invocation, "
                              "counts as one iteration-cap action either way")
    args = parser.parse_args()

    # Wire a real cheap chain here once you've picked Basic's model shortlist
    # (Tech-B). Left empty by default so this tool works with zero LLM
    # config out of the box.
    EXAMPLE_CHAIN: list = []
    try:
        n = check_and_increment("audit")
        print(f"[iteration cap] self-mod action {n}/8 today", file=sys.stderr)
    except IterationCapExceeded as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        sys.exit(3)
    try:
        report = audit_file(
            args.path,
            run_tests=not args.no_tests,
            run_profile=not args.no_profile,
            run_crossref=not args.no_crossref,
            run_churn=not args.no_churn,
            use_semgrep=not args.no_semgrep,
            llm_chain=EXAMPLE_CHAIN if args.llm_summary else None,
            run_ai_review=args.ai_review,
        )
    except AuditPathRejected as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        sys.exit(2)

    print(report.to_json())


if __name__ == "__main__":
    _main()
