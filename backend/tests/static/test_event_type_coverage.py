"""
tests/static/test_event_type_coverage.py — PATCH-D: the drift guard.

PATCH-A introduced relay/emitter.py's EventType enum as the single
source of truth for event types, and PATCH-B repointed eo/notify.py's
VALID_KINDS at a curated subset of it (NOTIFY_KINDS), closing the two
places this codebase's event-type allowlist used to drift out of sync
with its own call sites. PATCH-C made an unrecognized type degrade
(logged + skipped) instead of raising, so that drift can no longer
crash a task run in production.

None of that stops drift from *happening* again -- it just stops it
from being catastrophic when it does. A call site can still pass a
plain string literal (nothing enforces "always use EventType.X"), and
if that literal is never added to EventType, the event just silently
never gets sent -- which is much better than crashing the run, but is
still a real, findable bug that's better caught at CI time than
discovered later by "huh, why did this notification/diagram/deploy
status never show up on the frontend."

This file is that catch: it statically walks every .py file under
agents/, eo/, and relay/ and asserts every string literal passed as an
event_type / kind argument to emit_event(), emit_user_event(), or
notify() resolves to a real EventType / NOTIFY_KINDS member. It does
NOT flag calls that already pass EventType.X directly (an Attribute
node, not a string Constant) -- those are already statically safe:
a typo there is an AttributeError at import time, long before this
test or anything else needs to catch it.

This is deliberately a plain AST walk, not an import-and-introspect
approach, so it works even against files that can't actually be
imported in this environment (heavy optional dependencies, missing
API keys, etc. -- see tests/conftest.py's own notes on how brittle
importing the full agent graph can be). Source text is all this test
needs.
"""
import ast
import pathlib
import sys

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import relay.emitter as emitter  # noqa: E402

VALID_EVENT_TYPE_VALUES = {e.value for e in emitter.EventType}
VALID_NOTIFY_KIND_VALUES = {k.value for k in emitter.NOTIFY_KINDS}

# Only scan the directories that actually call these functions today.
# Deliberately excludes tests/ itself -- test files intentionally pass
# bogus literals like "not_a_real_type" to exercise the reject path,
# and flagging those would defeat the point of those tests.
SCAN_DIRS = ("agents", "eo", "relay")

EMIT_FUNC_NAMES = {"emit_event", "emit_user_event"}
NOTIFY_FUNC_NAMES = {"notify"}


def _iter_py_files():
    for dirname in SCAN_DIRS:
        base = BACKEND_ROOT / dirname
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            yield path


def _parse(path: pathlib.Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        # Not this test's job to catch syntax errors -- some other
        # part of CI already will.
        return None


def _call_func_name(call_node: ast.Call):
    """The plain function name for a Call node's func, whether it's a
    bare `emit_event(...)` (ast.Name) or a qualified
    `emitter.emit_event(...)` (ast.Attribute)."""
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string_literal_or_none(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _find_emit_violations():
    """(file, lineno, literal) for every emit_event()/emit_user_event()
    call whose first positional argument is a string literal that is
    NOT a real EventType value."""
    violations = []
    for path in _iter_py_files():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(BACKEND_ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if _call_func_name(node) not in EMIT_FUNC_NAMES:
                continue
            literal = _string_literal_or_none(node.args[0])
            if literal is not None and literal not in VALID_EVENT_TYPE_VALUES:
                violations.append((str(rel), node.lineno, literal))
    return violations


def _find_notify_violations():
    """(file, lineno, literal) for every notify(session_id, kind, ...)
    call whose `kind` positional argument is a string literal that is
    NOT a real NOTIFY_KINDS value."""
    violations = []
    for path in _iter_py_files():
        tree = _parse(path)
        if tree is None:
            continue
        rel = path.relative_to(BACKEND_ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            if _call_func_name(node) not in NOTIFY_FUNC_NAMES:
                continue
            literal = _string_literal_or_none(node.args[1])
            if literal is not None and literal not in VALID_NOTIFY_KIND_VALUES:
                violations.append((str(rel), node.lineno, literal))
    return violations


def _format(violations, allowlist_hint):
    lines = [f"  {f}:{ln}: {lit!r}" for f, ln, lit in violations]
    return (
        f"Found {len(violations)} event-type literal(s) with no matching "
        f"entry in {allowlist_hint}:\n" + "\n".join(lines) +
        f"\n\nEither this is a real, intentional event that needs adding to "
        f"{allowlist_hint} in relay/emitter.py, or it's a typo at the call "
        f"site above. Either way, this would previously have only surfaced "
        f"as a runtime failure the first time that code path actually ran."
    )


def test_no_orphaned_emit_event_literals():
    """Regression guard for the incident that prompted this whole
    rewrite: nine string literals (deploy_config_proposed and eight
    siblings) were passed to emit_event() by real agent code but were
    never added to the allowlist, so every one of them failed the
    first time that code path executed in production. This test would
    have caught all nine before they ever shipped."""
    violations = _find_emit_violations()
    assert not violations, _format(violations, "EventType (relay/emitter.py)")


def test_no_orphaned_notify_kind_literals():
    """Same guard, for eo/notify.py's notify() call sites."""
    violations = _find_notify_violations()
    assert not violations, _format(violations, "NOTIFY_KINDS (relay/emitter.py)")


def test_event_type_has_no_duplicate_values():
    """A sanity check on the enum itself: two members that accidentally
    share the same string value would make one of them permanently
    unreachable by name-based lookup elsewhere, silently."""
    values = [e.value for e in emitter.EventType]
    assert len(values) == len(set(values)), (
        "EventType has duplicate values -- every member must be unique."
    )


def test_notify_kinds_is_a_subset_of_event_type():
    """NOTIFY_KINDS is defined as a curated subset of EventType members
    (see relay/emitter.py's own comment on it) -- confirm that
    invariant holds structurally, not just by convention."""
    assert VALID_NOTIFY_KIND_VALUES <= VALID_EVENT_TYPE_VALUES
