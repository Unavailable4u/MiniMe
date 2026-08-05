"""
agents/static_scan.py — Static Security Scan (secrets + SAST). New this
pass, supporting Master Guide B2 ("Secrets scanning").

No LLM call — deterministic, same category as agents/review_aggregator.py
and agents/security_aggregator.py. Runs Gitleaks (secret detection) and
Semgrep (general static analysis: injection risks, unsafe deserialization,
missing validation, etc.) against one code module, inside a real E2B
sandbox — the exact same "run real tools instead of asking an LLM to
guess" pattern agents/sandbox_tester.py already established for test
execution.

Where this plugs in: agents/security_scanner.py's _scan_one() calls
run_static_scan() BEFORE its own LLM call, and passes the tool findings
into the prompt as ground truth to summarize/explain — not as a hint the
LLM re-derives from scratch. See that module's SYSTEM_PROMPT for the
updated framing ("real execution over LLM guessing", same principle
sandbox_tester.py's own docstring states).

Output shape matches security_scanner.py's own per-module findings shape,
so security_aggregator.py's de-dupe pass needs zero changes to consume
these findings alongside (or instead of) LLM-originated ones:
    {"findings": [{"severity": "critical|moderate|minor",
                    "description": "...", "source": "gitleaks"|"semgrep"}],
     "tool_error": str | None}

Setup cost note (be aware, don't optimize yet): neither tool is
pre-installed in a stock E2B sandbox, so every call here pays a cold-start
install cost (pip install semgrep + curl a gitleaks release binary) on top
of the scan itself — this is the same "accept a slower cold start for a
real tool" trade-off Phase G4's FreeCAD-in-E2B note makes for geometry
validation. If this cold start ends up dominating scan time in practice,
the fix is an E2B custom template with both tools pre-baked, not a
change to this module's logic.
"""
import json
import shlex

from e2b_code_interpreter import Sandbox

GITLEAKS_VERSION = "8.30.1"
GITLEAKS_URL = (
    f"https://github.com/gitleaks/gitleaks/releases/download/"
    f"v{GITLEAKS_VERSION}/gitleaks_{GITLEAKS_VERSION}_linux_x64.tar.gz"
)

# Best-effort setup: `|| true` on gitleaks' curl/tar/chmod chain and a
# redirected-and-swallowed semgrep install mean a network hiccup on ONE
# tool degrades to "that tool found nothing this run" (tool_error stays
# None, findings from the other tool still land) rather than failing the
# whole scan -- same tolerance _scan_one()'s broad except already has
# around a malformed LLM response.
_SETUP_CMD = (
    "pip install -q semgrep >/tmp/semgrep_install.log 2>&1 || true; "
    f"curl -sL {GITLEAKS_URL} -o /tmp/gitleaks.tar.gz "
    "&& tar -xzf /tmp/gitleaks.tar.gz -C /tmp gitleaks "
    "&& chmod +x /tmp/gitleaks || true"
)

_SEVERITY_FROM_SEMGREP = {"ERROR": "critical", "WARNING": "moderate", "INFO": "minor"}


def _run_gitleaks(sbx, file_path: str) -> list:
    out_path = "/tmp/gitleaks_out.json"
    cmd = (
        f"/tmp/gitleaks detect --no-git --source {shlex.quote(file_path)} "
        f"--report-format json --report-path {out_path} --exit-code 0 "
        f"2>/tmp/gitleaks_err.log || true"
    )
    sbx.commands.run(cmd, timeout=60)
    try:
        raw = sbx.files.read(out_path)
        rows = json.loads(raw) if raw and raw.strip() else []
    except Exception:
        rows = []
    findings = []
    for row in rows:
        rule = row.get("RuleID", "secret")
        line = row.get("StartLine", "?")
        match = (row.get("Match") or "")[:80]
        findings.append({
            "severity": "critical",  # a matched secret pattern is always critical -- never downgrade
            "description": f"Hardcoded secret detected ({rule}) at line {line}: {match}",
            "source": "gitleaks",
        })
    return findings


def _run_semgrep(sbx, file_path: str) -> list:
    out_path = "/tmp/semgrep_out.json"
    cmd = (
        f"semgrep --config=auto --json --quiet {shlex.quote(file_path)} "
        f"> {out_path} 2>/tmp/semgrep_err.log || true"
    )
    sbx.commands.run(cmd, timeout=90)
    try:
        raw = sbx.files.read(out_path)
        data = json.loads(raw) if raw and raw.strip() else {}
    except Exception:
        data = {}
    findings = []
    for result in data.get("results", []):
        extra = result.get("extra", {}) or {}
        severity = _SEVERITY_FROM_SEMGREP.get(extra.get("severity", "WARNING"), "moderate")
        message = extra.get("message", result.get("check_id", "issue"))
        line = (result.get("start", {}) or {}).get("line", "?")
        description = f"{result.get('check_id', 'semgrep')} at line {line}: {message}"
        findings.append({
            "severity": severity,
            "description": description[:300],
            "source": "semgrep",
        })
    return findings


def run_static_scan(module_name: str, code: str) -> dict:
    """Runs Gitleaks + Semgrep against one module's code inside a fresh
    E2B sandbox. Never raises -- a sandbox/tool failure degrades to an
    empty findings list plus a recorded tool_error, mirroring the
    tolerance security_scanner.py's own LLM call already has."""
    file_path = f"/tmp/{module_name.replace('/', '_').replace(chr(92), '_')}.py"
    try:
        with Sandbox.create() as sbx:
            sbx.files.write(file_path, code)
            sbx.commands.run(_SETUP_CMD, timeout=120)
            findings = _run_gitleaks(sbx, file_path) + _run_semgrep(sbx, file_path)
            return {"findings": findings, "tool_error": None}
    except Exception as exc:
        return {"findings": [], "tool_error": str(exc)}
