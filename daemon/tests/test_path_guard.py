"""
daemon/tests/test_path_guard.py — F2 Part 1: proves the containment
boundary before anything is wired to a network connection. Uses
pytest's tmp_path fixture, same as backend/tests/unit's convention of
never touching real disk locations outside a fixture-provided temp dir.

Run: pytest daemon/tests/test_path_guard.py -v
"""
import pytest

from daemon.path_guard import PathGuardError, assert_safe_root, assert_within_root, is_within_root


def test_assert_safe_root_accepts_a_real_directory(tmp_path):
    resolved = assert_safe_root(tmp_path)
    assert resolved == tmp_path.resolve()


def test_assert_safe_root_rejects_nonexistent_path(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(PathGuardError, match="does not exist"):
        assert_safe_root(missing)


def test_assert_safe_root_rejects_a_file_not_a_directory(tmp_path):
    a_file = tmp_path / "file.txt"
    a_file.write_text("hi")
    with pytest.raises(PathGuardError, match="not a directory"):
        assert_safe_root(a_file)


def test_assert_safe_root_rejects_filesystem_root():
    with pytest.raises(PathGuardError, match="too broad"):
        assert_safe_root("/")


def test_assert_safe_root_rejects_home_directory():
    with pytest.raises(PathGuardError, match="too broad"):
        assert_safe_root("~")


def test_is_within_root_true_for_direct_child(tmp_path):
    root = tmp_path.resolve()
    child = root / "file.txt"
    assert is_within_root(child, root) is True


def test_is_within_root_true_for_nested_child(tmp_path):
    root = tmp_path.resolve()
    nested = root / "sub" / "dir" / "file.txt"
    assert is_within_root(nested, root) is True


def test_is_within_root_true_for_root_itself(tmp_path):
    root = tmp_path.resolve()
    assert is_within_root(root, root) is True


def test_is_within_root_false_for_sibling_directory(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    sibling = tmp_path / "project-x" / "file.txt"
    assert is_within_root(sibling, root.resolve()) is False


def test_is_within_root_false_for_parent(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    assert is_within_root(tmp_path, root.resolve()) is False


def test_is_within_root_false_for_dotdot_traversal(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    traversal = root / ".." / ".." / "etc" / "passwd"
    assert is_within_root(traversal, root.resolve()) is False


def test_is_within_root_false_for_symlink_escaping_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside_target = tmp_path / "secret.txt"
    outside_target.write_text("do not read this")

    symlink = root / "innocent-looking-link.txt"
    symlink.symlink_to(outside_target)

    assert is_within_root(symlink, root.resolve()) is False


def test_assert_within_root_raises_with_useful_message(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "elsewhere"

    with pytest.raises(PathGuardError, match="escapes configured root"):
        assert_within_root(outside, root.resolve())


def test_assert_within_root_returns_resolved_path_on_success(tmp_path):
    root = tmp_path.resolve()
    child = root / "notes.md"

    result = assert_within_root(child, root)

    assert result == child.resolve()
