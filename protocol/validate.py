#!/usr/bin/env python3
"""
Patch P00 (M2C Wave 1) -- validator for the M2C protocol and tool-schema contract.

protocol/v1/ is the ONE source of truth for the wire format between the device
client (m2c/) and the server (backend/codeagent/, WP S04/S05/S09). This script
is the reference reader of it:

    python protocol/validate.py                    # everything below, exit 0/1
    python protocol/validate.py --check-client-copy
    python protocol/validate.py --sync-client

Default run checks, in order:
  1. every schema under protocol/v1 is a well-formed Draft 2020-12 schema whose
     $id matches its path, every $ref resolves, every tool definition fits
     tool_definition.json, and every v0 tool has a tool_proposed branch;
  2. every golden transcript (protocol/fixtures/*.jsonl) validates line by line
     AND as a transcript (check_transcript: seq runs, call-id pairing, resume
     cursors, terminal states);
  3. every case in fixtures/invalid_examples.jsonl FAILS, and fails on the
     keyword and path it names (so a broken example cannot pass for the wrong
     reason);
  4. the client's synced copy of the schemas, if the m2c/ package exists yet,
     is byte-identical (see compare_client_copy).

--check-client-copy runs only step 4. It passes when m2c/src/m2c/protocol/v1/
does not exist yet: WP C01 creates the client package in parallel with this
patch, so on a tree without it there is nothing to compare.

--sync-client copies protocol/v1/**/*.json over the client copy (and removes
stale .json files there). It refuses, exit 2, when m2c/src/m2c/ does not exist:
this tool never creates the client package.

Import it from tests with importlib (protocol/ is not a Python package):
validate_message(), validate_tool_input(), check_transcript(), load_jsonl() and
compare_client_copy() are the supported entry points; the client and server
suites replay fixtures through the same functions.

Security stance: validation fails CLOSED. validate_message(tolerant=True) exists
for production receivers that must survive additive changes (README, "Versioning
rule"), and relaxes exactly one thing -- unknown *optional* properties outside
tool input. Missing fields, wrong types, bad enums and every byte of
model-produced tool input stay strict.

Dependency: jsonschema (see protocol/requirements.txt). Deliberately not in
backend/requirements.txt; a later patch may add it there.
"""
from __future__ import annotations

import argparse
import functools
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
except ImportError:  # pragma: no cover - exercised only without the dependency
    if __name__ == "__main__":
        sys.stderr.write(
            "protocol/validate.py needs jsonschema: "
            "pip install -r protocol/requirements.txt\n")
        raise SystemExit(2)
    raise

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_DIR = ROOT / "protocol"
V1_DIR = PROTOCOL_DIR / "v1"
TOOLS_DIR = V1_DIR / "tools"
FIXTURES_DIR = PROTOCOL_DIR / "fixtures"
INVALID_EXAMPLES = FIXTURES_DIR / "invalid_examples.jsonl"
REQUIRED_FIXTURES = ("01_plain_chat.jsonl", "02_edit_with_approval.jsonl",
                     "03_disconnect_resume.jsonl", "04_quota_error.jsonl")

# The client package is created by WP C01; its synced copy of the schemas lives
# here. Both paths are parameters of the functions below so tests can aim them
# at a temp directory.
CLIENT_PKG_DIR = ROOT / "m2c" / "src" / "m2c"
CLIENT_COPY_DIR = CLIENT_PKG_DIR / "protocol" / "v1"

BASE_URI = "https://m2c.invalid/protocol/v1/"   # identifiers only, never fetched
DIALECT = "https://json-schema.org/draft/2020-12/schema"
RUN_EVENT_UNITS_PREFIX = "run_event:"
TERMINAL_STATES = {"completed", "cancelled", "failed", "expired"}
SYNC_HINT = ("python protocol/validate.py --sync-client "
             "(protocol/v1 is the source; never edit the copy)")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list:
    """One JSON value per non-empty line; errors name file and line."""
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{n}: not valid JSON ({exc})") from exc
    return out


def iter_v1_documents() -> Iterator[tuple[str, dict]]:
    """(path relative to protocol/v1, parsed document), schemas then tools."""
    for p in sorted(V1_DIR.glob("*.json")):
        yield p.name, load_json(p)
    for p in sorted(TOOLS_DIR.glob("*.json")):
        yield f"tools/{p.name}", load_json(p)


def load_tools() -> dict[str, dict]:
    """The v0 tool catalog: name -> definition (name, description,
    input_schema, version, risk[, risk_max])."""
    return {p.stem: load_json(p) for p in sorted(TOOLS_DIR.glob("*.json"))}


@functools.lru_cache(maxsize=1)
def _registry() -> Registry:
    reg = Registry()
    for rel, doc in iter_v1_documents():
        reg = reg.with_resource(
            BASE_URI + rel,
            Resource.from_contents(doc, default_specification=DRAFT202012))
    return reg


@functools.lru_cache(maxsize=1)
def _envelope_validator() -> Draft202012Validator:
    return Draft202012Validator(load_json(V1_DIR / "envelope.json"),
                                registry=_registry())


# ---------------------------------------------------------------------------
# Message validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Issue:
    path: str        # "/"-joined location in the message, "" for the root
    validator: str   # the JSON Schema keyword that failed (enum, required, ...)
    message: str

    def __str__(self) -> str:
        return f"{self.path or '<root>'}: [{self.validator}] {self.message}"


def _issue(err) -> Issue:
    return Issue("/".join(str(p) for p in err.absolute_path), err.validator,
                 err.message[:300])


def validate_message(msg, *, tolerant: bool = False) -> list[Issue]:
    """Issues found in one envelope; [] means valid.

    tolerant=True drops ONLY `additionalProperties` errors, and never for model
    tool input (payload/input). It is what a production receiver uses so that
    an additive field from a newer peer does not break an older one.
    """
    if not isinstance(msg, dict):
        return [Issue("", "type", "a message must be a JSON object")]
    found = [_issue(e) for e in _envelope_validator().iter_errors(msg)]
    if tolerant:
        found = [i for i in found
                 if i.validator != "additionalProperties"
                 or i.path == "payload/input" or i.path.startswith("payload/input/")]
    return sorted(found, key=lambda i: (i.path, i.validator, i.message))


def validate_tool_input(tool: str, tool_input) -> list[Issue]:
    """Check model-produced arguments against the v0 catalog. A tool outside the
    catalog is an error: callers execute only what they can validate (fail
    closed)."""
    tools = load_tools()
    if tool not in tools:
        return [Issue("", "unknown-tool", f"unknown tool {tool!r} (fail closed)")]
    v = Draft202012Validator(tools[tool]["input_schema"])
    return sorted((_issue(e) for e in v.iter_errors(tool_input)),
                  key=lambda i: (i.path, i.validator))


def unit_of(msg: dict) -> str:
    """Data-classification unit of a message: its type, or run_event:<subtype>."""
    if msg.get("t") == "run_event":
        return RUN_EVENT_UNITS_PREFIX + str(msg.get("payload", {}).get("type"))
    return str(msg.get("t"))


# ---------------------------------------------------------------------------
# Schema introspection (used by the data-placement tests and by --check)
# ---------------------------------------------------------------------------

def _deref(node, resolver, depth=0):
    if depth > 40:
        raise RecursionError("schema $ref nesting too deep (cycle?)")
    if isinstance(node, dict):
        if "$ref" in node:
            res = resolver.lookup(node["$ref"])
            target = _deref(res.contents, res.resolver, depth + 1)
            rest = {k: _deref(v, resolver, depth + 1) for k, v in node.items() if k != "$ref"}
            return {**target, **rest}
        return {k: _deref(v, resolver, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_deref(v, resolver, depth + 1) for v in node]
    return node


def deref_payload_schema(msg_type: str) -> dict:
    """protocol/v1/<msg_type>.json with every $ref inlined."""
    resolver = _registry().resolver(BASE_URI + f"{msg_type}.json")
    return _deref(resolver.lookup(BASE_URI + f"{msg_type}.json").contents, resolver)


def message_types() -> list[str]:
    types = load_json(V1_DIR / "envelope.json")["x-m2c-types"]
    return types["client_to_server"] + types["server_to_client"]


def units() -> list[str]:
    """Every classifiable unit: each message type, with run_event split into its
    subtypes."""
    out = []
    for t in message_types():
        if t == "run_event":
            out += [RUN_EVENT_UNITS_PREFIX + s
                    for s in load_json(V1_DIR / "run_event.json")["$defs"]]
        else:
            out.append(t)
    return out


def deref_unit(unit: str) -> dict:
    if unit.startswith(RUN_EVENT_UNITS_PREFIX):
        sub = unit[len(RUN_EVENT_UNITS_PREFIX):]
        resolver = _registry().resolver(BASE_URI + "run_event.json")
        res = resolver.lookup(f"#/$defs/{sub}")
        return _deref(res.contents, res.resolver)
    return deref_payload_schema(unit)


def data_class_of(unit: str):
    return deref_unit(unit).get("x-m2c-data-class")


def data_class_units(cls: str) -> list[str]:
    """Units declared content (in flight only) or metadata (may be persisted)."""
    return [u for u in units() if data_class_of(u) == cls]


def secret_fields() -> dict[str, list[str]]:
    """unit -> top-level properties marked x-m2c-secret (never logged)."""
    out = {}
    for u in units():
        names = [n for n, s in deref_unit(u).get("properties", {}).items()
                 if isinstance(s, dict) and s.get("x-m2c-secret")]
        if names:
            out[u] = names
    return out


_CHILD_MAPS = ("properties", "$defs")
_CHILD_SCHEMAS = ("items", "if", "then", "else", "not", "additionalProperties")
_CHILD_LISTS = ("allOf", "anyOf", "oneOf")


def walk(schema, where: str = "") -> Iterator[tuple[str, dict]]:
    """Every sub-schema (dict) of a schema with a readable location. Walks
    schema positions only, so a property *named* "type" is not mistaken for a
    keyword."""
    if not isinstance(schema, dict):
        return
    yield where or "<root>", schema
    for key in _CHILD_MAPS:
        for name, sub in (schema.get(key) or {}).items():
            yield from walk(sub, f"{where}/{key}/{name}")
    for key in _CHILD_SCHEMAS:
        yield from walk(schema.get(key), f"{where}/{key}")
    for key in _CHILD_LISTS:
        for i, sub in enumerate(schema.get(key) or []):
            yield from walk(sub, f"{where}/{key}/{i}")


def property_names(schema) -> Iterator[str]:
    for _, node in walk(schema):
        yield from (node.get("properties") or {}).keys()


def iter_strings(obj) -> Iterator[str]:
    """Every string *value* in a JSON document (keys are not yielded)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)


_PATHISH = re.compile(
    r"(?:^|[\s\"'=(])(?:~|\.{1,2})?/[^\s/]"      # /abs, ~/x, ./x, ../x
    r"|(?:^|\s)\.{1,2}\\"                        # ..\x
    r"|[A-Za-z]:\\"                              # C:\x
    r"|^\\\\")                                   # \\server\share


def looks_like_path(s: str) -> bool:
    """Heuristic tripwire for fixtures, not a security control: does this string
    look like a filesystem path? Model ids such as openai/gpt-oss-120b do not."""
    return bool(_PATHISH.search(s))


# ---------------------------------------------------------------------------
# Static checks of the schema tree
# ---------------------------------------------------------------------------

_RISK_ORDER = ["R0", "R1", "R2", "R3", "R4"]


def check_tool_definition(doc: dict) -> list[str]:
    """Problems with one tools/*.json definition ([] when fine)."""
    problems = []
    meta = Draft202012Validator(load_json(V1_DIR / "tool_definition.json"),
                                registry=_registry())
    problems += [f"tool definition: {_issue(e)}" for e in meta.iter_errors(doc)]
    if problems:
        return problems
    try:
        Draft202012Validator.check_schema(doc["input_schema"])
    except Exception as exc:  # jsonschema.SchemaError
        problems.append(f"input_schema is not a valid schema: {exc}")
    if doc.get("risk_max") and \
            _RISK_ORDER.index(doc["risk_max"]) <= _RISK_ORDER.index(doc["risk"]):
        problems.append("risk_max must be higher than risk (omit it otherwise)")
    return problems


def check_schemas() -> list[str]:
    """Well-formedness of everything under protocol/v1. [] means fine."""
    problems: list[str] = []
    for rel, doc in iter_v1_documents():
        path = V1_DIR / rel
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            problems.append(f"{rel}: has a UTF-8 BOM")
        if not raw.endswith(b"\n"):
            problems.append(f"{rel}: must end with a newline")
        if rel.startswith("tools/"):
            if doc.get("name") != Path(rel).stem:
                problems.append(f"{rel}: name must equal the file name")
            problems += [f"{rel}: {p}" for p in check_tool_definition(doc)]
            continue
        if doc.get("$schema") != DIALECT:
            problems.append(f"{rel}: $schema must be {DIALECT}")
        if doc.get("$id") != BASE_URI + rel:
            problems.append(f"{rel}: $id must be {BASE_URI + rel}")
        try:
            Draft202012Validator.check_schema(doc)
        except Exception as exc:
            problems.append(f"{rel}: not a valid Draft 2020-12 schema: {exc}")

    types = message_types()
    for t in types:
        if not (V1_DIR / f"{t}.json").is_file():
            problems.append(f"envelope lists {t!r} but v1/{t}.json is missing")
    env = load_json(V1_DIR / "envelope.json")
    routed = {b["if"]["properties"]["t"]["const"] for b in env["allOf"]
              if "const" in b.get("if", {}).get("properties", {}).get("t", {})}
    for t in types:
        if t not in routed:
            problems.append(f"envelope has no payload dispatch for {t!r}")

    tp = load_json(V1_DIR / "tool_proposed.json")
    covered = {b["if"]["properties"]["tool"]["const"] for b in tp["allOf"]}
    for name in load_tools():
        if name not in covered:
            problems.append(f"tool_proposed.json does not check input for tool {name!r}")

    try:  # every $ref resolves; every unit declares a data class
        for u in units():
            if data_class_of(u) not in ("content", "metadata"):
                problems.append(f"{u}: x-m2c-data-class must be 'content' or 'metadata'")
        for name in load_tools():
            _deref({"$ref": f"tools/{name}.json#/input_schema"},
                   _registry().resolver(BASE_URI + "tool_proposed.json"))
    except Exception as exc:
        problems.append(f"unresolvable $ref: {exc!r}")
    return problems


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

def check_transcript(msgs: list) -> list[str]:
    """Coherence of a whole transcript (beyond per-line validity).

    A transcript is the ordered record, interleaving both directions, of the
    messages DELIVERED on one device's connections; a `hello` starts a new
    connection. Rules checked: ids unique and timestamps non-decreasing;
    connections open with hello and are answered by welcome or error; each
    run's server stream numbers seq 1,2,3... without gaps; nothing follows a
    terminal state; run.start once per run; run.resume's last_seq equals what
    the transcript had delivered; approvals and results name a proposed call,
    a call finishes exactly once, and nothing arrives for a finished call.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    last_ts = None
    last_seq: dict[str, int] = {}
    started: set[str] = set()
    terminal: set[str] = set()
    proposed: dict[tuple[str, str], str] = {}       # (run, call) -> tool
    finished: set[tuple[str, str]] = set()
    conn_open = False
    expecting_welcome = False

    for n, m in enumerate(msgs, 1):
        def bad(text, n=n, m=m):
            problems.append(f"line {n} ({m.get('t')}): {text}")
        t, run = m.get("t"), m.get("run_id")
        p = m.get("payload", {})
        if m.get("id") in seen_ids:
            bad("duplicate message id")
        seen_ids.add(m.get("id"))
        if last_ts is not None and m.get("ts", "") < last_ts:
            bad("timestamp goes backwards")
        last_ts = m.get("ts", last_ts)

        if t == "hello":
            if expecting_welcome:
                bad("new hello before the previous one was answered")
            conn_open, expecting_welcome = False, True
            continue
        if expecting_welcome:
            if t == "welcome":
                conn_open, expecting_welcome = True, False
                continue
            if t == "error":
                expecting_welcome = False
                continue
            bad("a hello must be answered by welcome or error")
            expecting_welcome = False
        if not conn_open and t not in ("error",):
            bad("message outside an open connection (no hello/welcome before it)")

        if t == "run.start":
            if run in started:
                bad("run.start for a run that already exists")
            started.add(run)
        elif t == "run.resume":
            if run not in started:
                bad("run.resume for an unknown run")
            elif p.get("last_seq") != last_seq.get(run, 0):
                bad(f"last_seq {p.get('last_seq')} but the transcript delivered "
                    f"up to {last_seq.get(run, 0)}")
        elif t in ("user_message", "cancel", "tool_result", "tool_stream", "approval"):
            if run not in started:
                bad("run-scoped message before run.start")

        if "seq" in m:
            if run not in started:
                bad("server stream event for a run that was never started")
            want = last_seq.get(run, 0) + 1
            if m["seq"] != want:
                bad(f"seq {m['seq']} but expected {want}")
            last_seq[run] = m["seq"]
            if run in terminal:
                bad("event after the run reached a terminal state")

        if t == "tool_proposed":
            if (run, p["call_id"]) in proposed:
                bad(f"call_id {p['call_id']} proposed twice")
            proposed[(run, p["call_id"])] = p["tool"]
        elif t in ("tool_result", "tool_stream", "approval"):
            key = (run, p.get("call_id"))
            if key not in proposed:
                bad(f"call_id {p.get('call_id')} was never proposed")
            elif key in finished:
                bad(f"call_id {p.get('call_id')} already finished")
        elif t == "run_event":
            sub = p.get("type")
            if sub in ("tool_started", "tool_finished"):
                key = (run, p.get("call_id"))
                if key not in proposed:
                    bad(f"{sub} for call {p.get('call_id')} that was never proposed")
                elif proposed[key] != p.get("tool"):
                    bad(f"{sub} names tool {p.get('tool')!r}, proposed as {proposed[key]!r}")
                if sub == "tool_finished":
                    if key in finished:
                        bad(f"call {p.get('call_id')} finished twice")
                    finished.add(key)
            elif sub == "state_changed" and p.get("state") in TERMINAL_STATES:
                terminal.add(run)
    open_calls = sorted(c for (r, c) in proposed if (r, c) not in finished and r in terminal)
    if open_calls:
        problems.append(f"run ended with unfinished calls: {open_calls}")
    return problems


def check_fixtures() -> list[str]:
    problems: list[str] = []
    for name in REQUIRED_FIXTURES:
        if not (FIXTURES_DIR / name).is_file():
            problems.append(f"fixtures/{name} is missing")
    for path in sorted(FIXTURES_DIR.glob("*.jsonl")):
        if path.name == INVALID_EXAMPLES.name:
            continue
        try:
            msgs = load_jsonl(path)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if not msgs:
            problems.append(f"fixtures/{path.name}: empty")
        for n, m in enumerate(msgs, 1):
            problems += [f"fixtures/{path.name}:{n}: {i}" for i in validate_message(m)]
        problems += [f"fixtures/{path.name}: {p}" for p in check_transcript(msgs)]
    return problems


def check_invalid_examples() -> list[str]:
    """Each case must be rejected, on the keyword and path it declares."""
    problems: list[str] = []
    if not INVALID_EXAMPLES.is_file():
        return [f"{INVALID_EXAMPLES.name} is missing"]
    for case in load_jsonl(INVALID_EXAMPLES):
        name = case.get("case", "<unnamed>")
        found = validate_message(case.get("message"))
        want = case.get("expect", {})
        if not found:
            problems.append(f"invalid example {name!r} was ACCEPTED")
        elif not any(i.validator == want.get("validator") and i.path == want.get("path")
                     for i in found):
            problems.append(
                f"invalid example {name!r} failed for the wrong reason: wanted "
                f"[{want.get('validator')}] at {want.get('path')!r}, got "
                f"{[str(i) for i in found]}")
    return problems


# ---------------------------------------------------------------------------
# Client copy
# ---------------------------------------------------------------------------

def _json_files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*.json"))}


def compare_client_copy(client_dir: Path = CLIENT_COPY_DIR,
                        source_dir: Path = V1_DIR) -> list[str]:
    """Byte-for-byte comparison of the client's synced copy with protocol/v1.

    [] when the copy does not exist yet (C01 has not landed) or is identical.
    Only *.json files are compared; a packager may add other files (for example
    __init__.py) next to them. A missing, extra or different file is a problem.
    """
    client_dir, source_dir = Path(client_dir), Path(source_dir)
    if not client_dir.exists():
        return []
    src, dst = _json_files(source_dir), _json_files(client_dir)
    problems = []
    for rel in sorted(src.keys() - dst.keys()):
        problems.append(f"client copy is missing {rel}")
    for rel in sorted(dst.keys() - src.keys()):
        problems.append(f"client copy has {rel}, which is not in protocol/v1")
    for rel in sorted(src.keys() & dst.keys()):
        if src[rel].read_bytes() != dst[rel].read_bytes():
            problems.append(f"client copy differs from protocol/v1: {rel}")
    return problems


def copy_tree(src: Path, dst: Path) -> list[str]:
    """Copy every *.json under src to dst, byte for byte. Returns the relative
    paths written."""
    written = []
    for rel, path in _json_files(Path(src)).items():
        target = Path(dst) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        written.append(rel)
    return written


def sync_client(client_pkg_dir: Path = CLIENT_PKG_DIR,
                source_dir: Path = V1_DIR) -> list[str]:
    """Make <client_pkg_dir>/protocol/v1 a byte-identical mirror of the schemas.

    Raises FileNotFoundError if the client package directory does not exist:
    creating m2c/ is WP C01's job, not this tool's. Stale .json files in the
    copy are deleted. Returns the relative paths written.
    """
    client_pkg_dir = Path(client_pkg_dir)
    if not client_pkg_dir.is_dir():
        raise FileNotFoundError(
            f"{client_pkg_dir} does not exist; the client package comes from WP C01")
    dst = client_pkg_dir / "protocol" / "v1"
    if dst.exists():
        keep = set(_json_files(Path(source_dir)))
        for rel, path in _json_files(dst).items():
            if rel not in keep:
                path.unlink()
    return copy_tree(source_dir, dst)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report(title: str, problems: list[str], hint: str = "") -> bool:
    if problems:
        print(f"FAIL  {title}")
        for p in problems:
            print(f"        - {p}")
        if hint:
            print(f"        fix: {hint}")
        return False
    print(f"ok    {title}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate the M2C protocol contract (protocol/v1).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check-client-copy", action="store_true",
                   help="only check m2c/src/m2c/protocol/v1 against protocol/v1 "
                        "(passes if the client package has no copy yet)")
    g.add_argument("--sync-client", action="store_true",
                   help="copy protocol/v1/**/*.json into the client package")
    args = ap.parse_args(argv)

    if args.sync_client:
        try:
            written = sync_client()
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"synced {len(written)} files to {CLIENT_COPY_DIR.relative_to(ROOT)}")
        return 0

    if args.check_client_copy:
        absent = not CLIENT_COPY_DIR.exists()
        ok = _report("client copy" + (" (not created yet: nothing to compare)"
                                      if absent else " is byte-identical"),
                     compare_client_copy(), hint=SYNC_HINT)
        return 0 if ok else 1

    ok = True
    ok &= _report(f"schemas: {sum(1 for _ in iter_v1_documents())} files "
                  f"({len(load_tools())} tools, {len(message_types())} message types)",
                  check_schemas())
    if ok:   # the remaining checks assume loadable schemas
        n_fix = len([p for p in FIXTURES_DIR.glob('*.jsonl') if p.name != INVALID_EXAMPLES.name])
        ok &= _report(f"fixtures: {n_fix} golden transcripts validate and cohere",
                      check_fixtures())
        n_bad = len(load_jsonl(INVALID_EXAMPLES)) if INVALID_EXAMPLES.is_file() else 0
        ok &= _report(f"invalid examples: {n_bad} cases all rejected for the intended reason",
                      check_invalid_examples())
        ok &= _report("client copy" + (" (not created yet: nothing to compare)"
                                       if not CLIENT_COPY_DIR.exists() else " is byte-identical"),
                      compare_client_copy(), hint=SYNC_HINT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
