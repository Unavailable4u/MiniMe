"""Patch C26: the trust file is out of the model's reach, and nothing in the trust package can send anything anywhere.

C26 adds no protection of its own: the file lives under M2C's state directory, which C05's ToolContext already refuses
(``protected_dirs``) and which C11's sandbox profile never mounts.  These tests PROVE that for the real file this patch
writes, through the real file tools, so a later change to either guard cannot silently expose it.  The tests that need the
file tools build a project next to (not inside) the M2C home, and one where the M2C home is INSIDE the project (the
worst case: the path is then project-relative and only the denylist stands in the way).
"""
import ast
import hashlib
import os
import shutil
from pathlib import Path

import pytest

from m2c import paths, sandbox
from m2c.sandbox import profile
from m2c.tools import ToolContext, run_tool
from m2c.tools.paths import PathGuardError
from m2c.trust.store import TrustStore

REFUSED = {"outside_root", "protected_path", "invalid_path", "symlink_refused", "not_found"}


def fingerprint_text() -> str:
    return hashlib.sha256(b"protected-trust-test").hexdigest()


@pytest.fixture
def proj(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("hello\n")
    return root


@pytest.fixture
def trust_file(tmp_path) -> Path:
    """The real default trust file, with a real entry in it."""
    other = tmp_path / "some-other-project"
    other.mkdir()
    store = TrustStore.default()
    store.set(other, "trusted", fingerprint_text())
    assert store.path.is_file()
    return store.path


def assert_denied(result, trust_file: Path, before: bytes) -> None:
    assert not result.ok, result.output
    assert result.code in REFUSED, (result.code, result.output)
    assert fingerprint_text()[:16] not in result.output
    assert str(trust_file.parent) not in result.output
    assert trust_file.read_bytes() == before


def every_call(target: str) -> list[tuple[str, dict]]:
    return [
        ("read_file", {"path": target}),
        ("read_file", {"path": target, "start_line": 1, "end_line": 5}),
        ("write_file", {"path": target, "content": '{"version": 1, "folders": {}}'}),
        ("edit_file", {"path": target, "old_string": "trusted", "new_string": "untrusted"}),
        ("delete_path", {"path": target}),
    ]


# ---------------------------------------------------------------------------------------------------- the file tools
def test_the_trust_file_is_where_the_work_package_says(trust_file):
    assert trust_file == Path(os.environ["M2C_HOME"]) / "trust.json"
    assert paths.config_dir() == trust_file.parent


def test_the_file_tools_refuse_the_trust_file_by_absolute_path(proj, trust_file):
    before = trust_file.read_bytes()
    ctx = ToolContext(proj)
    for tool, args in every_call(str(trust_file)):
        assert_denied(run_tool(ctx, tool, args), trust_file, before)


def test_the_file_tools_refuse_it_by_a_relative_path(proj, trust_file):
    before = trust_file.read_bytes()
    ctx = ToolContext(proj)
    for tool, args in every_call(os.path.relpath(trust_file, proj)):
        assert_denied(run_tool(ctx, tool, args), trust_file, before)


def test_a_symlink_inside_the_project_does_not_open_the_way(proj, trust_file):
    before = trust_file.read_bytes()
    os.symlink(trust_file, proj / "link.json")
    os.symlink(trust_file.parent, proj / "cfg")
    ctx = ToolContext(proj)
    for target in ("link.json", "cfg/trust.json"):
        for tool, args in every_call(target):
            assert_denied(run_tool(ctx, tool, args), trust_file, before)
    assert trust_file.read_bytes() == before


def test_the_home_dot_m2c_directory_is_protected_too(proj, tmp_path):
    """Both state directories are protected: wherever M2C_HOME points, and ~/.m2c."""
    home_state = Path(os.environ["HOME"]) / ".m2c"
    home_state.mkdir(parents=True)
    (home_state / "trust.json").write_text("{}")
    before = (home_state / "trust.json").read_bytes()
    ctx = ToolContext(proj)
    for tool, args in every_call(str(home_state / "trust.json")):
        assert_denied(run_tool(ctx, tool, args), home_state / "trust.json", before)


def test_a_project_cannot_be_the_state_directory_or_inside_it(trust_file):
    with pytest.raises(PathGuardError):
        ToolContext(trust_file.parent)
    inner = trust_file.parent / "inner"
    inner.mkdir()
    with pytest.raises(PathGuardError):
        ToolContext(inner)


def test_when_the_state_directory_is_inside_the_project_the_denylist_still_refuses_it(proj, monkeypatch):
    """The worst case: M2C_HOME under the project, so the file is a project-relative path the model could name."""
    monkeypatch.setenv("M2C_HOME", str(proj / ".state"))
    store = TrustStore.default()
    store.set(proj, "trusted", fingerprint_text())
    trust_file = store.path
    before = trust_file.read_bytes()
    ctx = ToolContext(proj)
    for target in (".state/trust.json", str(trust_file), "./.state/./trust.json"):
        for tool, args in every_call(target):
            result = run_tool(ctx, tool, args)
            assert not result.ok and result.code in REFUSED, (tool, target, result.code)
            assert fingerprint_text()[:16] not in result.output
    assert trust_file.read_bytes() == before
    protected = run_tool(ctx, "read_file", {"path": ".state/trust.json"})
    assert protected.code == "protected_path"


def test_listing_and_searching_do_not_reveal_the_trust_file_inside_the_project(proj, monkeypatch):
    monkeypatch.setenv("M2C_HOME", str(proj / ".state"))
    store = TrustStore.default()
    store.set(proj, "trusted", fingerprint_text())
    ctx = ToolContext(proj)
    listing = run_tool(ctx, "list_dir", {"path": ".", "max_depth": 3})
    assert listing.ok and "trust.json" not in listing.output and ".state" not in listing.output
    found = run_tool(ctx, "glob", {"pattern": "**/*.json"})
    assert "trust.json" not in found.output
    direct = run_tool(ctx, "list_dir", {"path": ".state"})
    assert not direct.ok and direct.code in REFUSED


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_grep_does_not_read_the_trust_file_inside_the_project(proj, monkeypatch):
    monkeypatch.setenv("M2C_HOME", str(proj / ".state"))
    store = TrustStore.default()
    store.set(proj, "trusted", fingerprint_text())
    ctx = ToolContext(proj)
    control = run_tool(ctx, "grep", {"pattern": "hello"})          # the tool works and does find ordinary files
    assert control.ok and "a.txt" in control.output
    for path in (None, ".", ".state"):
        args = {"pattern": fingerprint_text()[:16]}
        if path:
            args["path"] = path
        result = run_tool(ctx, "grep", args)
        assert "trust.json" not in result.output, (path, result.output)


# ---------------------------------------------------------------------------------------------------- the sandbox
def test_the_sandbox_profile_never_mounts_the_m2c_directory():
    assert ".m2c" in profile.NEVER_MOUNT_HOME


def test_the_sandbox_denies_the_configured_m2c_directory_wherever_m2c_home_puts_it(trust_file):
    assert str(paths.config_dir()) in sandbox.default_denied()


# ---------------------------------------------------------------------------------------------------- nothing leaves the machine
FORBIDDEN_IMPORTS = {
    "socket", "ssl", "http", "urllib", "ftplib", "smtplib", "requests", "websockets", "asyncio", "subprocess",
    "m2c.link", "m2c.auth", "m2c.runner", "m2c.headless",
}


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    package = "m2c.trust"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                found.add(node.module or "")
            else:  # a relative import: resolve against m2c.trust
                base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                found.add(".".join(base + ([node.module] if node.module else [])))
    return found


def test_the_trust_package_has_no_way_to_send_anything():
    import m2c.trust as trust_pkg

    folder = Path(trust_pkg.__file__).parent
    modules = sorted(folder.glob("*.py"))
    assert {m.name for m in modules} >= {"__init__.py", "store.py", "fingerprint.py", "prompt.py"}
    for module in modules:
        for name in _imports_of(module):
            top = name.split(".")[0]
            assert top not in FORBIDDEN_IMPORTS and name not in FORBIDDEN_IMPORTS, (module.name, name)
            assert not name.startswith(("m2c.link", "m2c.auth")), (module.name, name)
