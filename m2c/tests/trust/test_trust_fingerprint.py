"""Patch C26: the folder fingerprint -- what it covers, what changes it, what makes a folder untrustworthy.

The fingerprint is the thing a trust decision is tied to, so these tests pin three properties:

* STABLE: the same listed files give the same digest, wherever the folder lives and in whatever order the files were made,
  and the digest is the documented SHA-256 construction (a reference implementation below), so a release can never silently
  change what an old stored decision means.
* SENSITIVE: a changed byte, a renamed, added or deleted listed file all change it; a file outside the listed places does not.
* SAFE: symlinks, over-large trees, odd files and odd names never raise, never hang, never follow a link out of the project,
  and make the folder "cannot be trusted automatically" instead of producing a digest.
"""
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from m2c.trust.fingerprint import (
    MAX_DEPTH, MAX_ENTRIES, MAX_FILES, MAX_TOTAL_BYTES, TRUST_ROOTS, compute_fingerprint, fingerprint_matches,
)

LISTED = [
    ".m2c/settings.toml",
    ".m2c/rules/style.md",
    ".m2c/rules/deep/more.md",
    ".m2c/hooks/pre.sh",
    ".m2c/skills/lint/SKILL.md",
    ".agents/skills/a/SKILL.md",
    ".claude/skills/c/SKILL.md",
]
UNLISTED = [
    "README.md", "src/app.py", ".m2c/notes.txt", ".m2c/checkpoints/blob", ".claude/settings.json",
    ".claude/commands/x.md", ".agents/other/x", ".git/config", "M2C.md", "AGENTS.md",
]


def make(root: Path, names, data: dict[str, bytes] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((data or {}).get(name, ("content of " + name).encode()))
    return root


def reference_digest(root: Path, names) -> str:
    """The documented construction, written out independently of the module under test."""
    h = hashlib.sha256()
    h.update(b"m2c-trust-fingerprint-v1\n")
    for rel in sorted(names, key=lambda n: n.encode("utf-8", "surrogateescape")):
        rb = rel.encode("utf-8", "surrogateescape")
        h.update(len(rb).to_bytes(4, "big"))
        h.update(rb)
        h.update(hashlib.sha256((root / rel).read_bytes()).digest())
    return h.hexdigest()


@pytest.fixture
def proj(tmp_path) -> Path:
    return make(tmp_path / "proj", LISTED + UNLISTED)


# ---------------------------------------------------------------------------------------------------- what is covered
def test_the_listed_places_are_the_ones_in_the_work_package():
    assert set(TRUST_ROOTS) == {
        ".m2c/settings.toml", ".m2c/rules", ".m2c/hooks", ".m2c/skills", ".agents/skills", ".claude/skills",
    }


def test_every_listed_kind_of_file_is_found_and_nothing_else(proj):
    result = compute_fingerprint(proj)
    assert result.problem is None
    assert result.files == tuple(sorted(LISTED))


def test_a_folder_with_nothing_listed_has_no_files_and_still_a_digest(tmp_path):
    root = make(tmp_path / "p", UNLISTED)
    result = compute_fingerprint(root)
    assert result.files == () and result.problem is None and isinstance(result.digest, str)


def test_files_are_relative_posix_paths_and_never_absolute(proj):
    for rel in compute_fingerprint(proj).files:
        assert not rel.startswith("/") and ".." not in rel.split("/") and str(proj) not in rel and "\\" not in rel


def test_the_digest_is_the_documented_sha256_construction(proj):
    result = compute_fingerprint(proj)
    assert result.digest == reference_digest(proj, LISTED)
    assert len(result.digest) == 64 and result.digest == result.digest.lower()
    int(result.digest, 16)


def test_the_digest_of_nothing_is_the_construction_over_zero_files(tmp_path):
    root = make(tmp_path / "p", UNLISTED)
    assert compute_fingerprint(root).digest == reference_digest(root, [])


# ---------------------------------------------------------------------------------------------------- stability
def test_two_calls_agree(proj):
    assert compute_fingerprint(proj).digest == compute_fingerprint(proj).digest


def test_the_same_files_in_another_folder_have_the_same_digest(tmp_path, proj):
    other = make(tmp_path / "somewhere" / "else" / "copy", LISTED)
    assert compute_fingerprint(other).digest == compute_fingerprint(proj).digest


def test_creation_order_does_not_matter(tmp_path):
    a = make(tmp_path / "a", LISTED)
    b = make(tmp_path / "b", list(reversed(LISTED)))
    assert compute_fingerprint(a).digest == compute_fingerprint(b).digest


# ---------------------------------------------------------------------------------------------------- sensitivity
def test_a_changed_byte_changes_the_digest(proj):
    before = compute_fingerprint(proj).digest
    path = proj / ".m2c" / "settings.toml"
    data = bytearray(path.read_bytes())
    data[0] ^= 1
    path.write_bytes(bytes(data))
    assert compute_fingerprint(proj).digest != before


@pytest.mark.parametrize("name", LISTED)
def test_deleting_any_listed_file_changes_the_digest(proj, name):
    before = compute_fingerprint(proj).digest
    (proj / name).unlink()
    after = compute_fingerprint(proj)
    assert after.digest != before and name not in after.files


@pytest.mark.parametrize("name", LISTED)
def test_renaming_any_listed_file_changes_the_digest(proj, name):
    before = compute_fingerprint(proj).digest
    target = proj / (name + "2")
    (proj / name).rename(target)
    after = compute_fingerprint(proj)
    assert after.digest != before and name not in after.files
    if name != ".m2c/settings.toml":          # the settings file is covered only under its own name
        assert (name + "2") in after.files


@pytest.mark.parametrize("name", [
    ".m2c/rules/new.md", ".m2c/hooks/new.sh", ".m2c/skills/new/SKILL.md", ".agents/skills/new/SKILL.md",
    ".claude/skills/new/SKILL.md", ".m2c/rules/deeper/still/new.md",
])
def test_adding_a_file_in_any_listed_place_changes_the_digest(proj, name):
    before = compute_fingerprint(proj).digest
    make(proj, [name])
    after = compute_fingerprint(proj)
    assert after.digest != before and name in after.files


def test_adding_the_settings_file_to_a_folder_that_had_none_changes_the_digest(tmp_path):
    root = make(tmp_path / "p", UNLISTED)
    before = compute_fingerprint(root).digest
    make(root, [".m2c/settings.toml"])
    assert compute_fingerprint(root).digest != before


def test_an_empty_file_is_not_the_same_as_no_file(proj):
    (proj / ".m2c" / "hooks" / "pre.sh").write_bytes(b"")
    with_empty = compute_fingerprint(proj).digest
    (proj / ".m2c" / "hooks" / "pre.sh").unlink()
    assert compute_fingerprint(proj).digest != with_empty


def test_moving_content_between_two_files_changes_the_digest(tmp_path):
    a = make(tmp_path / "a", [".m2c/rules/a", ".m2c/rules/b"], {".m2c/rules/a": b"1", ".m2c/rules/b": b"2"})
    b = make(tmp_path / "b", [".m2c/rules/a", ".m2c/rules/b"], {".m2c/rules/a": b"2", ".m2c/rules/b": b"1"})
    c = make(tmp_path / "c", [".m2c/rules/a", ".m2c/rules/b"], {".m2c/rules/a": b"12", ".m2c/rules/b": b""})
    assert len({compute_fingerprint(x).digest for x in (a, b, c)}) == 3


def test_a_path_and_its_content_cannot_be_confused(tmp_path):
    """Length-prefixed framing: 'ab' with content X must not equal 'a' + 'b...' shifted."""
    one = make(tmp_path / "one", [".m2c/rules/ab"], {".m2c/rules/ab": b"x"})
    two = make(tmp_path / "two", [".m2c/rules/a", ".m2c/rules/b"], {".m2c/rules/a": b"x", ".m2c/rules/b": b"x"})
    assert compute_fingerprint(one).digest != compute_fingerprint(two).digest


def test_files_outside_the_listed_places_change_nothing(proj):
    before = compute_fingerprint(proj)
    for name in UNLISTED:
        (proj / name).write_bytes(b"changed")
    make(proj, ["brand/new/file.txt", ".m2c/other/more.txt", ".claude/other/x", ".agents/skillz/x"])
    (proj / "README.md").unlink()
    after = compute_fingerprint(proj)
    assert after.digest == before.digest and after.files == before.files


def test_a_symlink_outside_the_listed_places_is_never_followed_and_changes_nothing(proj, tmp_path):
    before = compute_fingerprint(proj)
    outside = make(tmp_path / "outside", ["secret.txt"])
    os.symlink(outside, proj / "src" / "escape")
    os.symlink(outside / "secret.txt", proj / ".m2c" / "notes-link")
    after = compute_fingerprint(proj)
    assert after.problem is None and after.digest == before.digest


# ---------------------------------------------------------------------------------------------------- fingerprint_matches
def test_fingerprint_matches_is_true_only_for_the_same_files(proj):
    digest = compute_fingerprint(proj).digest
    assert fingerprint_matches(proj, digest) is True
    (proj / ".m2c" / "settings.toml").write_bytes(b"[permissions]\nallow = []\n")
    assert fingerprint_matches(proj, digest) is False


def test_fingerprint_matches_is_false_when_the_folder_became_untrustworthy(proj, tmp_path):
    digest = compute_fingerprint(proj).digest
    os.symlink(tmp_path, proj / ".m2c" / "rules" / "link")
    assert fingerprint_matches(proj, digest) is False


@pytest.mark.parametrize("digest", [None, "", "x", 5, b"abc"])
def test_fingerprint_matches_is_false_for_a_digest_that_is_not_one(proj, digest):
    assert fingerprint_matches(proj, digest) is False


# ---------------------------------------------------------------------------------------------------- symlinks
def _file_link_inside(proj, tmp_path):
    os.symlink(proj / "README.md", proj / ".m2c" / "skills" / "lint" / "inside")


def _file_link_outside(proj, tmp_path):
    outside = make(tmp_path / "outside", ["f.txt"])
    os.symlink(outside / "f.txt", proj / ".m2c" / "hooks" / "outside")


def _dangling(proj, tmp_path):
    os.symlink(tmp_path / "does-not-exist", proj / ".m2c" / "rules" / "dangling")


def _dir_link_outside(proj, tmp_path):
    outside = make(tmp_path / "outside", ["secret.txt"])
    os.symlink(outside, proj / ".agents" / "skills" / "linked")


def _settings_is_a_link(proj, tmp_path):
    outside = make(tmp_path / "outside", ["s.toml"])
    (proj / ".m2c" / "settings.toml").unlink()
    os.symlink(outside / "s.toml", proj / ".m2c" / "settings.toml")


def _m2c_is_a_link(proj, tmp_path):
    outside = make(tmp_path / "outside", ["settings.toml"])
    import shutil
    shutil.rmtree(proj / ".m2c")
    os.symlink(outside, proj / ".m2c")


def _claude_is_a_link(proj, tmp_path):
    import shutil
    outside = make(tmp_path / "outside", ["skills/x/SKILL.md"])
    shutil.rmtree(proj / ".claude")
    os.symlink(outside, proj / ".claude")


def _agents_is_a_link(proj, tmp_path):
    import shutil
    outside = make(tmp_path / "outside", ["skills/x/SKILL.md"])
    shutil.rmtree(proj / ".agents")
    os.symlink(outside, proj / ".agents")


def _skills_dir_is_a_link(proj, tmp_path):
    import shutil
    outside = make(tmp_path / "outside", ["x/SKILL.md"])
    shutil.rmtree(proj / ".claude" / "skills")
    os.symlink(outside, proj / ".claude" / "skills")


def _loop(proj, tmp_path):
    os.symlink(proj / ".m2c" / "rules", proj / ".m2c" / "rules" / "loop")


@pytest.mark.parametrize("build", [
    _file_link_inside, _file_link_outside, _dangling, _dir_link_outside, _settings_is_a_link, _m2c_is_a_link,
    _claude_is_a_link, _agents_is_a_link, _skills_dir_is_a_link, _loop,
])
def test_a_symlink_in_a_trust_relevant_place_makes_the_folder_untrustworthy(proj, tmp_path, build):
    build(proj, tmp_path)
    result = compute_fingerprint(proj)
    assert result.problem == "symlink" and result.digest is None


def test_a_link_to_a_directory_outside_is_not_read(proj, tmp_path):
    _dir_link_outside(proj, tmp_path)
    result = compute_fingerprint(proj)
    assert result.problem == "symlink"
    assert not any("secret.txt" in name for name in result.files)


def test_fingerprint_matches_never_follows_the_link_either(proj, tmp_path):
    digest = compute_fingerprint(proj).digest
    _dir_link_outside(proj, tmp_path)
    assert fingerprint_matches(proj, digest) is False


# ---------------------------------------------------------------------------------------------------- caps
def test_the_default_caps_are_sane():
    assert MAX_FILES >= 100 and MAX_TOTAL_BYTES >= 1_000_000 and MAX_ENTRIES >= MAX_FILES and MAX_DEPTH >= 6


def test_more_files_than_the_cap_is_too_large(tmp_path):
    root = make(tmp_path / "p", [f".m2c/rules/r{i}.md" for i in range(4)])
    assert compute_fingerprint(root, max_files=3).problem == "too_large"
    assert compute_fingerprint(root, max_files=3).digest is None


def test_exactly_the_cap_is_fine(tmp_path):
    root = make(tmp_path / "p", [f".m2c/rules/r{i}.md" for i in range(3)])
    assert compute_fingerprint(root, max_files=3).problem is None


def test_more_bytes_than_the_cap_is_too_large(tmp_path):
    root = make(tmp_path / "p", [".m2c/hooks/a", ".m2c/hooks/b"], {".m2c/hooks/a": b"x" * 6, ".m2c/hooks/b": b"y" * 5})
    assert compute_fingerprint(root, max_total_bytes=10).problem == "too_large"
    assert compute_fingerprint(root, max_total_bytes=11).problem is None


def test_a_huge_sparse_file_is_refused_by_its_size_without_reading_it(tmp_path):
    root = make(tmp_path / "p", [".m2c/settings.toml"])
    big = root / ".m2c" / "hooks" / "big"
    big.parent.mkdir(parents=True)
    with open(big, "wb") as handle:
        handle.truncate(200 * 1024 * 1024)
    started = time.monotonic()
    result = compute_fingerprint(root)
    assert result.problem == "too_large" and time.monotonic() - started < 5


def test_a_tree_nested_deeper_than_the_cap_is_too_large(tmp_path):
    root = make(tmp_path / "p", [".m2c/rules/" + "/".join(["d"] * 20) + "/f.md"])
    assert compute_fingerprint(root).problem == "too_large"
    assert compute_fingerprint(root, max_depth=40).problem is None


def test_many_empty_directories_count_against_the_cap(tmp_path):
    root = tmp_path / "p"
    for i in range(10):
        (root / ".m2c" / "rules" / f"d{i}").mkdir(parents=True)
    assert compute_fingerprint(root, max_entries=5).problem == "too_large"


# ---------------------------------------------------------------------------------------------------- odd files, odd names
def test_a_fifo_does_not_hang_and_is_unreadable(proj):
    os.mkfifo(proj / ".m2c" / "hooks" / "pipe")
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("r", compute_fingerprint(proj)), daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "reading a FIFO hung"
    assert box["r"].problem == "unreadable" and box["r"].digest is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a file whatever its mode")
def test_an_unreadable_file_is_unreadable(proj):
    path = proj / ".m2c" / "settings.toml"
    path.chmod(0)
    try:
        assert compute_fingerprint(proj).problem == "unreadable"
    finally:
        path.chmod(0o600)


def test_a_missing_folder_and_a_file_instead_of_a_folder_are_unreadable(tmp_path):
    assert compute_fingerprint(tmp_path / "nope").problem == "unreadable"
    (tmp_path / "afile").write_text("x")
    assert compute_fingerprint(tmp_path / "afile").problem == "unreadable"


def test_odd_names_never_raise(tmp_path):
    root = tmp_path / "p"
    base = os.fsencode(str(root))
    names = [b"line\nbreak.md", b"bad\xff.md", b"tab\there.md", b"esc\x1b[31mred.md", b"sp ace.md", b"u\xc3\xa9.md",
             b"n" * 200]
    for raw in names:
        full = os.path.join(base, b".m2c", b"rules", raw)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(b"x")
    first = compute_fingerprint(root)
    assert first.problem is None and len(first.files) == len(names)
    assert compute_fingerprint(root).digest == first.digest


def test_the_empty_project_is_fine(tmp_path):
    (tmp_path / "p").mkdir()
    result = compute_fingerprint(tmp_path / "p")
    assert result.problem is None and result.files == ()


def test_the_result_is_immutable(proj):
    result = compute_fingerprint(proj)
    with pytest.raises(Exception):
        result.digest = "x"  # type: ignore[misc]
