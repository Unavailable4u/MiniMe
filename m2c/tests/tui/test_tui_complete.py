"""Patch C09a: @ file mentions (m2c.tui.complete)."""
import os
from pathlib import Path

import pytest

from m2c.tools.paths import Denylist
from m2c.tui.complete import MAX_CANDIDATES, candidates, mention_at, scan_project


@pytest.fixture
def project(tmp_path):
    root = tmp_path.resolve() / "proj"
    root.mkdir()
    return root


def touch(root, rel, text="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def scan(root, **kw):
    return scan_project(root, Denylist(root=root), **kw)


def test_project_files_are_listed_relative_and_sorted_by_depth(project):
    for rel in ("README.md", "src/a.py", "src/pkg/b.py", "docs/x.md"):
        touch(project, rel)
    s = scan(project)
    assert s.complete and set(s.files) == {"README.md", "src/a.py", "src/pkg/b.py", "docs/x.md"}
    assert s.files[0] == "README.md"                       # breadth first: shallow files come first


def test_denylisted_files_and_directories_are_never_offered(project):
    touch(project, ".env", "SECRET=1")
    touch(project, "id_rsa", "key")
    touch(project, "src/ok.py")
    touch(project, ".ssh/config")
    touch(project, "sub/.env")
    files = scan(project).files
    assert files == ("src/ok.py",)


def test_user_patterns_of_the_denylist_are_honored(project):
    touch(project, "secrets/a.txt")
    touch(project, "pub/a.txt")
    s = scan_project(project, Denylist(["secrets/**"], root=project))
    assert "pub/a.txt" in s.files and not any(f.startswith("secrets") for f in s.files)


def test_m2c_own_state_directory_is_protected(project, tmp_path):
    state = project / ".m2cstate"
    touch(project, ".m2cstate/credentials", "x")
    touch(project, "a.txt")
    s = scan_project(project, Denylist(root=project, protected_dirs=[state]))
    assert s.files == ("a.txt",)


def test_dot_git_and_noise_directories_are_skipped(project):
    touch(project, ".git/config")
    touch(project, "node_modules/x/index.js")
    touch(project, "__pycache__/a.pyc")
    touch(project, "a.py")
    assert scan(project).files == ("a.py",)


def test_symlinks_are_never_offered_or_followed(project, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    touch(project, "real.py")
    (project / "link_out.txt").symlink_to(outside)
    (project / "link_in.py").symlink_to(project / "real.py")
    (project / "dirlink").symlink_to(tmp_path)
    (project / "env_link").symlink_to(touch(project, ".env"))
    assert scan(project).files == ("real.py",)


def test_names_with_control_or_format_characters_are_not_offered(project):
    touch(project, "ok.txt")
    for name in ("bad\nname.txt", "esc\x1b[31m.txt", "rtl\u202e.txt"):
        try:
            touch(project, name)
        except (OSError, ValueError):
            pytest.skip("this filesystem refuses that file name")
    assert scan(project).files == ("ok.txt",)


def test_the_number_of_files_is_capped(project):
    for i in range(40):
        touch(project, f"d/f{i:02}.txt")
    s = scan(project, max_files=10)
    assert len(s.files) == 10 and not s.complete


def test_the_scan_time_is_capped(project):
    for i in range(30):
        touch(project, f"d{i}/f.txt")
    ticks = iter(range(10_000))
    s = scan(project, time_budget_s=3, clock=lambda: float(next(ticks)))
    assert not s.complete and len(s.files) < 30


def test_an_unreadable_directory_is_skipped(project):
    touch(project, "ok/a.txt")
    locked = project / "locked"
    locked.mkdir()
    touch(project, "locked/b.txt")
    locked.chmod(0)
    try:
        s = scan(project)
    finally:
        locked.chmod(0o755)
    assert "ok/a.txt" in s.files


def test_mention_at_finds_the_token_before_the_cursor():
    assert mention_at("look at @src/a") == (9, "src/a")
    assert mention_at("@") == (1, "") and mention_at("a @b @c") == (6, "c")
    assert mention_at("mail me@example.com") is None and mention_at("no mention") is None
    assert mention_at("@done and more") is None


def test_candidates_rank_path_start_then_name_start_then_anywhere_and_are_capped():
    files = ("src/app.py", "app.py", "tests/test_app.py", "docs/notes.md", "lib/application.py")
    got = candidates(files, "app")
    assert got[0] == "app.py" and "docs/notes.md" not in got and "tests/test_app.py" in got
    many = tuple(f"f{i}.txt" for i in range(500))
    assert len(candidates(many, "f")) == MAX_CANDIDATES
    assert candidates(files, "APP")[0] == "app.py"


def test_candidates_are_only_ever_paths_from_the_scan(project):
    touch(project, "a.py")
    files = scan(project).files
    assert candidates(files, "\x1b[31m../../etc") == []
