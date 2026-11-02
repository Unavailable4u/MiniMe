"""Patch S10: the structural half of "metering and the grant route never leak content".

tests/unit/test_codeagent_metering.py and tests/unit/test_api_routes_code_runs.py prove at run time (caplog) that a whole
run's logs and responses hold numbers and state words only. This file reads the source:

  1. every logging call in codeagent/metering.py and api/routes/code_runs.py names only safe values (the same scan the
     S04 and S07 files get: no tracebacks, no exception text, no names that carry content or a credential);
  2. the one SQL statement metering.py runs (the run's credit sum) only needs privileges the migrations grant the app
     role (the same ledger tests/static/test_sql_grants.py derives; that file is not edited, its SQL_MODULES list is
     the place for a later patch that wants metering.py under the permanent guard);
  3. api/server.py's only change is one import and one include_router line, placed before the device socket's router
     (so S14's install() call keeps its "right after the socket router" position).

Importing api.server here would be slow and order-dependent, so the file is read, not run.
"""
import ast
import pathlib

import pytest

from tests.static.test_codeagent_logging import FORBIDDEN_NAMES, _logging_calls, _names_outside_type_calls
from tests.static.test_loop_logging import EXTRA_FORBIDDEN
from tests.static.test_sql_grants import check_module_source, derive_grants, extract_sql

BACKEND = pathlib.Path(__file__).resolve().parents[2]
FILES = ["codeagent/metering.py", "api/routes/code_runs.py"]
SERVER = BACKEND / "api" / "server.py"
# `spent`, `ceiling` and `granted` are credit numbers: allowed. `usage` (the model's Usage object), `notice` and the rest of
# EXTRA_FORBIDDEN are not.
FORBIDDEN = FORBIDDEN_NAMES | EXTRA_FORBIDDEN


def scan(tree, rel):
    problems = []
    for call in _logging_calls(tree):
        where = f"{rel}:{call.lineno}"
        if call.func.attr == "exception":
            problems.append(f"{where}: logger.exception() prints a traceback with the exception's text")
        for kw in call.keywords:
            if kw.arg in ("exc_info", "stack_info"):
                problems.append(f"{where}: {kw.arg}= prints the exception's text")
        for arg in list(call.args) + [kw.value for kw in call.keywords]:
            bad = sorted(set(_names_outside_type_calls(arg)) & FORBIDDEN)
            if bad:
                problems.append(f"{where}: logs {', '.join(bad)}")
    return problems


@pytest.mark.parametrize("rel", FILES)
def test_logging_calls_name_only_safe_values(rel):
    path = BACKEND / rel
    assert path.exists(), f"{rel} is part of S10"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert len(list(_logging_calls(tree))) >= 5                   # it does log, so the scan has teeth
    assert scan(tree, rel) == []


def test_the_scan_would_catch_a_violation():
    bad = ast.parse(
        "logger.info('x %s', usage)\n"
        "logger.warning('y %s', exc)\n"
        "logger.exception('z')\n"
        "logger.error('w', exc_info=True)\n"
        "logger.info('v %s', payload)\n")
    assert len(scan(bad, "x.py")) == 5
    good = ast.parse("logger.warning('ok run=%s error=%s', run_id, type(exc).__name__)\n")
    assert scan(good, "x.py") == []


def test_metering_runs_the_one_read_only_statement_the_migrations_allow():
    source = (BACKEND / "codeagent" / "metering.py").read_text(encoding="utf-8")
    statements = extract_sql(source, "codeagent/metering.py")
    assert statements, "the extractor found no SQL in metering.py: it is broken or the statement moved"
    ledger = derive_grants()
    assert check_module_source(source, "codeagent/metering.py", ledger) == []
    for stmt in statements:
        assert stmt.sql.lstrip().lower().startswith("select"), "metering reads the ledger; it never writes it"


def test_the_grant_route_has_no_sql_of_its_own():
    source = (BACKEND / "api" / "routes" / "code_runs.py").read_text(encoding="utf-8")
    assert extract_sql(source, "api/routes/code_runs.py") == []


def server_problems(source: str) -> list:
    tree = ast.parse(source)
    found = []
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "api.routes.code_runs"]
    if len(imports) != 1 or [(a.name, a.asname) for a in imports[0].names] != [("router", "code_runs_router")]:
        found.append("expected exactly one `from api.routes.code_runs import router as code_runs_router`")

    def include(name):
        return [i for i, node in enumerate(tree.body)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "include_router"
                and node.value.args and isinstance(node.value.args[0], ast.Name) and node.value.args[0].id == name]

    mine, sockets = include("code_runs_router"), include("codeagent_ws_router")
    if len(mine) != 1:
        found.append(f"expected exactly one include_router(code_runs_router), found {len(mine)}")
    elif len(sockets) == 1 and not mine[0] < sockets[0]:
        found.append("the grant router must be included before the device socket's router (S14's install() call goes right after that one)")
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "code_runs_router"]
    if len(uses) != 1:
        found.append(f"code_runs_router must be used only by the include, found {len(uses)} uses")
    return found


def test_server_py_makes_the_one_import_and_the_one_include():
    assert server_problems(SERVER.read_text(encoding="utf-8")) == []


def test_the_server_check_would_catch_a_missing_duplicated_or_misplaced_include():
    base = ("from api.routes.code_runs import router as code_runs_router\n"
            "app.include_router(code_runs_router)\n"
            "app.include_router(codeagent_ws_router)\n")
    assert server_problems(base) == []
    assert server_problems(base.replace("app.include_router(code_runs_router)\n", ""))
    assert server_problems(base + "app.include_router(code_runs_router)\n")
    assert server_problems("from api.routes.code_runs import router as code_runs_router\n"
                           "app.include_router(codeagent_ws_router)\napp.include_router(code_runs_router)\n")
