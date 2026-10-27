"""S14e: api/server.py's only change for S14 is one import and one call, placed next to the line
that includes the device socket's router.

Importing api.server in a test is slow and its global effects (the process-wide router, the
socket's handler) depend on test order, so this reads the file instead and checks the shape of
the change. tests/unit/test_codeagent_run_resume.py::test_install_* proves what the call does.
"""
import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[2]
SERVER = BACKEND / "api" / "server.py"
ALIAS = "codeagent_run_handlers"


def problems_in(source: str) -> list:
    tree = ast.parse(source)
    found = []

    imports = [n for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom) and any(a.name == "run_handlers" for a in n.names)]
    if len(imports) != 1:
        found.append(f"expected exactly one import of run_handlers, found {len(imports)}")
    elif (imports[0].module, [(a.name, a.asname) for a in imports[0].names]) != ("codeagent", [("run_handlers", ALIAS)]):
        found.append("the import must be `from codeagent import run_handlers as codeagent_run_handlers`")

    body = tree.body
    calls = [i for i, node in enumerate(body)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
             and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "install"
             and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == ALIAS]
    if len(calls) != 1:
        found.append(f"expected exactly one module-level {ALIAS}.install() statement, found {len(calls)}")
    else:
        call = body[calls[0]].value
        if call.args or call.keywords:
            found.append("the install call takes no arguments: it installs the process-wide handlers and router")
        includes = [i for i, node in enumerate(body)
                    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "include_router"
                    and node.value.args and isinstance(node.value.args[0], ast.Name)
                    and node.value.args[0].id == "codeagent_ws_router"]
        if len(includes) != 1:
            found.append("the device socket's router must be included exactly once")
        elif not 0 < calls[0] - includes[0] <= 2:
            found.append("the install call must come right after the line that includes the socket's router")

    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == ALIAS]
    if len(uses) != 1:
        found.append(f"{ALIAS} must be used only by the install call, found {len(uses)} uses")
    return found


def test_server_py_makes_the_one_install_call_next_to_the_socket_router_include():
    assert problems_in(SERVER.read_text(encoding="utf-8")) == []


def test_the_check_would_catch_a_missing_a_duplicated_or_a_misplaced_call():
    base = (
        "from codeagent import run_handlers as codeagent_run_handlers\n"
        "app.include_router(codeagent_ws_router)\n"
        "codeagent_run_handlers.install()\n"
    )
    assert problems_in(base) == []
    assert problems_in(base.replace("codeagent_run_handlers.install()\n", "")) != []
    assert problems_in(base + "codeagent_run_handlers.install()\n") != []
    assert problems_in(base.replace("install()", "install(handlers=None)")) != []
    assert problems_in("from codeagent import run_handlers as codeagent_run_handlers\n"
                       "codeagent_run_handlers.install()\napp.include_router(codeagent_ws_router)\n") != []
    near = base.replace("app.include_router(codeagent_ws_router)\n",
                                                               "app.include_router(codeagent_ws_router)\na = 1\n")
    assert problems_in(near) == []                                  # one statement in between is still "next to"
    far = ("from codeagent import run_handlers as codeagent_run_handlers\n"
           "app.include_router(codeagent_ws_router)\na = 1\nb = 2\nc = 3\ncodeagent_run_handlers.install()\n")
    assert problems_in(far) != []
    assert problems_in(base + "codeagent_run_handlers.get_handlers()\n") != []
