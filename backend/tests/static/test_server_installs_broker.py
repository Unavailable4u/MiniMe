"""S09: api/server.py's only change for the tool broker is one import and one call, placed right
after the walking skeleton's install (which is right after run_handlers' install, pinned by
test_server_installs_run_handlers.py and tests/unit/test_codeagent_hello.py).

Importing api.server in a test is slow and its global effects (the process-wide router, the
socket's handler) depend on test order, so this reads the file instead and checks the shape of
the change. tests/unit/test_codeagent_broker.py::test_install_* proves what the call does.
"""
import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[2]
SERVER = BACKEND / "api" / "server.py"
ALIAS = "codeagent_broker"


def _install_calls(body, alias):
    return [i for i, node in enumerate(body)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "install"
            and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == alias]


def problems_in(source: str) -> list:
    tree = ast.parse(source)
    found = []

    imports = [n for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom) and any(a.name == "broker" for a in n.names)]
    if len(imports) != 1:
        found.append(f"expected exactly one import of broker, found {len(imports)}")
    elif (imports[0].module, [(a.name, a.asname) for a in imports[0].names]) != ("codeagent", [("broker", ALIAS)]):
        found.append("the import must be `from codeagent import broker as codeagent_broker`")

    body = tree.body
    calls = _install_calls(body, ALIAS)
    if len(calls) != 1:
        found.append(f"expected exactly one module-level {ALIAS}.install() statement, found {len(calls)}")
    else:
        call = body[calls[0]].value
        if call.args or call.keywords:
            found.append("the install call takes no arguments: it installs the process-wide broker")
        hello_calls = _install_calls(body, "codeagent_hello")
        handler_calls = _install_calls(body, "codeagent_run_handlers")
        if len(hello_calls) != 1 or len(handler_calls) != 1:
            found.append("the run handlers' and the walking skeleton's install calls must be there")
        elif not handler_calls[0] < hello_calls[0] < calls[0] and calls[0] - hello_calls[0] <= 2:
            found.append("the broker's install call must come after the run handlers' and the walking skeleton's")
        elif calls[0] - hello_calls[0] > 2:
            found.append("the broker's install call must come right after the walking skeleton's")

    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == ALIAS]
    if len(uses) != 1:
        found.append(f"{ALIAS} must be used only by the install call, found {len(uses)} uses")
    return found


def test_server_py_installs_the_broker_right_after_the_walking_skeleton():
    assert problems_in(SERVER.read_text(encoding="utf-8")) == []


def test_the_existing_install_lines_are_unchanged_in_shape():
    """The broker line is added; nothing else about the run handlers' and hello's lines moves."""
    lines = [ln for ln in SERVER.read_text(encoding="utf-8").splitlines() if ln.strip()]
    i = next(n for n, ln in enumerate(lines) if ln.startswith("codeagent_run_handlers.install()"))
    assert lines[i + 1].startswith("codeagent_hello.install()")
    assert lines[i + 2].startswith("codeagent_broker.install()")


def test_the_check_would_catch_a_missing_a_duplicated_or_a_misplaced_call():
    base = (
        "from codeagent import broker as codeagent_broker\n"
        "codeagent_run_handlers.install()\n"
        "codeagent_hello.install()\n"
        "codeagent_broker.install()\n"
    )
    assert problems_in(base) == []
    assert problems_in(base.replace("codeagent_broker.install()\n", "")) != []
    assert problems_in(base + "codeagent_broker.install()\n") != []
    assert problems_in(base.replace("install()", "install(None)", 3)) != []
    assert problems_in(base.replace("from codeagent import broker as codeagent_broker\n", "")) != []
    before = ("from codeagent import broker as codeagent_broker\n"
              "codeagent_broker.install()\ncodeagent_run_handlers.install()\ncodeagent_hello.install()\n")
    assert problems_in(before) != []
    far = base.replace("codeagent_hello.install()\n", "codeagent_hello.install()\na = 1\nb = 2\nc = 3\n")
    assert problems_in(far) != []
    assert problems_in(base + "codeagent_broker.get_broker()\n") != []
