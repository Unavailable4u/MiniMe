"""
m2c/tests/policy/test_policy_modes.py -- Patch C06: modes, the tool catalog, risk.

The mode table is checked exhaustively (every mode x tool class x risk x
inside/outside x protected), and the catalog copy in modes.py is checked
against the P00 schemas so that it cannot drift.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

import m2c
from m2c.policy.modes import (
    MODES, RISKS, TOOLS, class_ceiling, effective_risk, is_valid_mode, max_risk, mode_default, risk_rank,
)

A, K, D = "allow", "ask", "deny"
CLASSES = ["read", "edit", "delete", "shell", "plan_exit", "nonsense"]


def table():
    for mode, cls, risk, inside, prot in itertools.product(MODES, CLASSES, RISKS, (True, False), (True, False)):
        yield mode, cls, risk, inside, prot, mode_default(mode, cls, risk, inside_project=inside, protected=prot)


def test_exactly_these_combinations_are_allowed_by_a_mode():
    allowed = {(m, c, r, i, p) for m, c, r, i, p, d in table() if d.action == A}
    expected = set()
    for mode in MODES:
        for risk in ("R0",):
            expected.add((mode, "read", risk, True, False))
            expected.add((mode, "read", risk, True, True))   # reading a protected file is fine
    for risk in ("R0", "R1"):
        expected.add(("accept-edits", "edit", risk, True, False))
    assert allowed == expected


def test_a_mode_never_allows_a_shell_command_or_a_delete_or_plan_exit():
    for m, c, r, i, p, d in table():
        if c in ("shell", "delete", "plan_exit", "nonsense"):
            assert d.action != A, (m, c, r)


def test_a_mode_never_allows_anything_above_its_class_ceiling():
    for m, c, r, i, p, d in table():
        if d.action == A:
            assert risk_rank(r) <= risk_rank(class_ceiling(c))
    assert class_ceiling("shell") is None and class_ceiling("delete") is None


def test_plan_mode_denies_every_mutation_and_command():
    for r, i, p in itertools.product(RISKS, (True, False), (True, False)):
        for cls in ("edit", "delete", "shell"):
            assert mode_default("plan", cls, r, inside_project=i, protected=p).action == D


def test_protected_paths_are_denied_for_mutations_in_every_mode():
    for mode, cls, r, i in itertools.product(MODES, ("edit", "delete", "shell"), RISKS, (True, False)):
        assert mode_default(mode, cls, r, inside_project=i, protected=True).action == D


def test_ask_mode_asks_for_edits_deletes_and_shell():
    for cls in ("edit", "delete", "shell"):
        assert mode_default("ask", cls, "R1", inside_project=True).action == K
    assert mode_default("accept-edits", "delete", "R1", inside_project=True).action == K
    assert mode_default("accept-edits", "shell", "R2", inside_project=True).action == K
    assert mode_default("accept-edits", "edit", "R1", inside_project=False).action == K
    assert mode_default("accept-edits", "edit", "R2", inside_project=True).action == K


def test_reads_outside_the_project_or_above_r0_ask():
    assert mode_default("ask", "read", "R0", inside_project=False).action == K
    assert mode_default("ask", "read", "R1", inside_project=True).action == K
    assert mode_default("plan", "read", "R3", inside_project=True).action == K


def test_plan_exit_always_asks():
    for mode in MODES:
        assert mode_default(mode, "plan_exit", "R0", inside_project=True).action == K


@pytest.mark.parametrize("mode", ["bypass", "auto", "", None, "Ask", 5, ["ask"]])
def test_unknown_mode_is_denied(mode):
    assert mode_default(mode, "read", "R0", inside_project=True).action == D
    assert not is_valid_mode(mode)


@pytest.mark.parametrize("risk", ["R5", "", None, "r0", 0, ["R0"]])
def test_unknown_risk_is_denied(risk):
    assert mode_default("ask", "read", risk, inside_project=True).action == D


def test_every_decision_has_a_reason():
    assert all(d.reason for *_, d in table())


# --- risk -------------------------------------------------------------------
def test_effective_risk_is_the_higher_of_the_two():
    for a, b in itertools.product(RISKS, RISKS):
        e = effective_risk(a, b)
        assert risk_rank(e) == max(risk_rank(a), risk_rank(b))
        assert e == effective_risk(b, a)
    assert max_risk("R1", "R3") == "R3"


def test_risk_rank_rejects_unknown_levels():
    for bad in ("R5", None, 1, "r1"):
        with pytest.raises(ValueError):
            risk_rank(bad)


# --- the catalog copy matches P00 ------------------------------------------
TOOLS_DIR = Path(m2c.__file__).parent / "protocol" / "v1" / "tools"
PY = {"string": str, "integer": int, "boolean": bool}


def test_catalog_has_exactly_the_nine_p00_tools():
    files = sorted(p.stem for p in TOOLS_DIR.glob("*.json"))
    assert files == sorted(TOOLS)
    assert len(TOOLS) == 9


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_catalog_matches_the_schema(name):
    schema = json.loads((TOOLS_DIR / f"{name}.json").read_text())
    spec = TOOLS[name]
    assert spec.risk == schema["risk"]
    assert spec.risk_max == schema.get("risk_max")
    inp = schema["input_schema"]
    assert set(spec.required) == set(inp["required"])
    assert {k: PY[v["type"]] for k, v in inp["properties"].items()} == spec.arg_types
    assert inp["additionalProperties"] is False


def test_tool_classes():
    cls = {n: t.tool_class for n, t in TOOLS.items()}
    assert cls == {
        "read_file": "read", "list_dir": "read", "glob": "read", "grep": "read",
        "edit_file": "edit", "write_file": "edit", "delete_path": "delete", "bash": "shell", "plan_exit": "plan_exit",
    }
