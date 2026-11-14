"""Patch C26: the trust store (~/.m2c/trust.json) -- permissions, atomic writes, malformed entries, refusal to read a loose file.

The file records, per canonical project root, a decision and the fingerprint it was made for.  It is a security file
(a forged "trusted" entry would switch on a folder's hooks), so reading it fails closed in the same way C03's credential
file does, and an entry that is not exactly well-formed is ignored rather than believed.
"""
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from m2c.trust import store as store_mod
from m2c.trust.store import TrustEntry, TrustStore, TrustStoreError


def fp(label: str) -> str:
    """A well-formed fingerprint made at run time (no hash literals in the repository)."""
    return hashlib.sha256(label.encode()).hexdigest()


def good(label="a", decision="trusted", when=1_700_000_000) -> dict:
    return {"decision": decision, "fingerprint": fp(label), "time": when}


def raw_file(path: Path, folders, *, version=1, mode=0o600, text: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text if text is not None else json.dumps({"version": version, "folders": folders})
    path.write_text(body)
    path.chmod(mode)


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "store-home"


@pytest.fixture
def path(home) -> Path:
    return home / "trust.json"


@pytest.fixture
def store(path) -> TrustStore:
    return TrustStore(path, clock=lambda: 1_700_000_123)


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "project"
    r.mkdir()
    return r.resolve()


# ---------------------------------------------------------------------------------------------------- location
def test_the_default_store_lives_in_the_m2c_home_and_touches_nothing():
    default = TrustStore.default()
    assert default.path == Path(os.environ["M2C_HOME"]) / "trust.json"
    assert not default.path.exists() and not default.path.parent.exists()


def test_the_default_store_follows_an_explicit_environment(tmp_path):
    default = TrustStore.default({"M2C_HOME": str(tmp_path / "other")})
    assert default.path == tmp_path / "other" / "trust.json"


def test_reading_a_store_that_does_not_exist_is_empty_not_an_error(store, root, path):
    assert store.get(root) is None and store.items() == []
    assert not path.exists()


# ---------------------------------------------------------------------------------------------------- writing
def test_set_creates_the_file_0600_and_the_directory_0700(store, root, path):
    store.set(root, "trusted", fp("one"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_the_default_store_tightens_an_existing_loose_config_directory(tmp_path, root):
    home = Path(os.environ["M2C_HOME"])
    home.mkdir()
    home.chmod(0o755)
    TrustStore.default().set(root, "trusted", fp("one"))
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_the_file_has_the_documented_shape(store, root, path):
    store.set(root, "untrusted", fp("shape"))
    data = json.loads(path.read_text())
    assert data == {"version": 1, "folders": {str(root): {"decision": "untrusted", "fingerprint": fp("shape"), "time": 1_700_000_123}}}


def test_get_returns_what_was_set(store, root):
    store.set(root, "trusted", fp("one"))
    assert store.get(root) == TrustEntry("trusted", fp("one"), 1_700_000_123)


def test_a_new_decision_for_the_same_folder_replaces_the_old_one(store, root):
    store.set(root, "trusted", fp("one"))
    store.set(root, "untrusted", fp("two"))
    assert store.get(root) == TrustEntry("untrusted", fp("two"), 1_700_000_123)
    assert len(store.items()) == 1


def test_other_folders_are_kept(store, root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    store.set(root, "trusted", fp("one"))
    store.set(other, "untrusted", fp("two"))
    assert store.get(root).decision == "trusted" and store.get(other).decision == "untrusted"


def test_the_key_is_the_real_path_so_a_symlinked_path_is_the_same_folder(store, root, tmp_path):
    link = tmp_path / "link-to-project"
    os.symlink(root, link)
    store.set(link, "trusted", fp("one"))
    assert store.get(root) is not None and store.get(link) is not None
    assert list(json.loads(store.path.read_text())["folders"]) == [str(root)]


def test_a_relative_path_is_made_absolute_not_stored_as_given(store, root, monkeypatch):
    monkeypatch.chdir(root)
    store.set(".", "trusted", fp("one"))
    assert list(json.loads(store.path.read_text())["folders"]) == [str(root)]


@pytest.mark.parametrize("decision", ["yes", "Trusted", "", None, 1, True])
def test_set_refuses_a_decision_that_is_not_one(store, root, decision):
    with pytest.raises(ValueError):
        store.set(root, decision, fp("one"))


@pytest.mark.parametrize("bad", ["abc", "", None, 5, "Z" * 64, fp("x").upper(), fp("x")[:-1], fp("x") + "0"])
def test_set_refuses_a_fingerprint_that_is_not_one(store, root, bad):
    with pytest.raises(ValueError):
        store.set(root, "trusted", bad)


def test_the_write_goes_through_a_private_temp_file_fsync_and_rename(store, root, path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", stat.S_IMODE(os.stat(src).st_mode), Path(src).parent == path.parent))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    store.set(root, "trusted", fp("one"))
    assert events[0] == "fsync"                       # the data was flushed before the rename
    assert ("replace", 0o600, True) in events          # temp file: private, in the same directory
    assert events.index("fsync") < events.index(("replace", 0o600, True))


def test_a_failed_write_leaves_the_old_file_untouched_and_no_temp_file(store, root, path, monkeypatch, tmp_path):
    store.set(root, "trusted", fp("one"))
    before = path.read_bytes()
    other = tmp_path / "other"
    other.mkdir()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(TrustStoreError) as err:
        store.set(other, "trusted", fp("two"))
    assert "disk full" not in str(err.value)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert sorted(os.listdir(path.parent)) == ["trust.json"]


def test_the_error_of_a_failed_write_says_what_to_check(store, root, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(TrustStoreError) as err:
        store.set(root, "trusted", fp("one"))
    assert "trust.json" in str(err.value)


def test_set_never_overwrites_a_file_it_refuses_to_read(store, root, path):
    raw_file(path, {}, mode=0o644)
    with pytest.raises(TrustStoreError):
        store.set(root, "trusted", fp("one"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and json.loads(path.read_text())["folders"] == {}


def test_the_store_is_bounded_and_the_oldest_decisions_go_first(path, tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "MAX_ENTRIES", 3)
    times = iter(range(100, 200))
    store = TrustStore(path, clock=lambda: next(times))
    roots = []
    for i in range(5):
        r = tmp_path / f"p{i}"
        r.mkdir()
        roots.append(r)
        store.set(r, "trusted", fp(str(i)))
    assert [store.get(r) is not None for r in roots] == [False, False, True, True, True]


# ---------------------------------------------------------------------------------------------------- forget and list
def test_forget_removes_one_decision_and_reports_whether_there_was_one(store, root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    store.set(root, "trusted", fp("one"))
    store.set(other, "trusted", fp("two"))
    assert store.forget(root) is True and store.get(root) is None and store.get(other) is not None
    assert store.forget(root) is False


def test_forget_works_for_a_folder_that_no_longer_exists(store, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    store.set(gone, "trusted", fp("one"))
    gone.rmdir()
    assert store.forget(gone) is True and store.items() == []


def test_forget_on_a_missing_store_creates_nothing(store, root, path):
    assert store.forget(root) is False
    assert not path.exists() and not path.parent.exists()


def test_items_lists_every_valid_decision_sorted(store, tmp_path):
    for name in ("b", "a", "c"):
        r = tmp_path / name
        r.mkdir()
        store.set(r, "trusted" if name != "c" else "untrusted", fp(name))
    listed = store.items()
    assert [p for p, _ in listed] == sorted(p for p, _ in listed) and len(listed) == 3
    assert {e.decision for _, e in listed} == {"trusted", "untrusted"}


# ---------------------------------------------------------------------------------------------------- malformed entries
BAD_ENTRIES = {
    "/p/not-an-object": "trusted",
    "/p/none": None,
    "/p/bad-decision": {"decision": "yes", "fingerprint": fp("x"), "time": 1},
    "/p/case": {"decision": "Trusted", "fingerprint": fp("x"), "time": 1},
    "/p/list-decision": {"decision": ["trusted"], "fingerprint": fp("x"), "time": 1},
    "/p/short-fp": {"decision": "trusted", "fingerprint": "abc", "time": 1},
    "/p/upper-fp": {"decision": "trusted", "fingerprint": fp("x").upper(), "time": 1},
    "/p/no-time": {"decision": "trusted", "fingerprint": fp("x")},
    "/p/negative-time": {"decision": "trusted", "fingerprint": fp("x"), "time": -1},
    "/p/float-time": {"decision": "trusted", "fingerprint": fp("x"), "time": 1.5},
    "/p/bool-time": {"decision": "trusted", "fingerprint": fp("x"), "time": True},
    "/p/str-time": {"decision": "trusted", "fingerprint": fp("x"), "time": "1"},
    "relative/path": good(),
    "": good(),
    "/p/with\u0000nul": good(),
}


def test_malformed_entries_are_ignored_and_never_trusted(path, tmp_path):
    folders = dict(BAD_ENTRIES)
    folders["/p/good"] = good("g")
    raw_file(path, folders)
    store = TrustStore(path)
    assert [p for p, _ in store.items()] == ["/p/good"]
    for name in BAD_ENTRIES:
        if name.startswith("/"):
            assert store.get(name) is None, name
    assert store.get("/p/good") == TrustEntry("trusted", fp("g"), 1_700_000_000)
    assert store.ignored_entries == len(BAD_ENTRIES)


def test_extra_keys_in_an_entry_are_tolerated(path):
    entry = good()
    entry["note"] = "from a newer release"
    raw_file(path, {"/p/x": entry})
    assert TrustStore(path).get("/p/x") is not None


def test_a_set_after_malformed_entries_drops_them_and_keeps_the_good_ones(path, root):
    raw_file(path, {"/p/good": good("g"), "/p/bad": "trusted"})
    store = TrustStore(path)
    store.set(root, "trusted", fp("new"))
    assert set(json.loads(path.read_text())["folders"]) == {"/p/good", str(root)}


# ---------------------------------------------------------------------------------------------------- a damaged file
MARKER = "marker-of-the-contents"


@pytest.mark.parametrize("body", [
    "", "not json " + MARKER, "[]", json.dumps({"folders": {}}), json.dumps({"version": 1}),
    json.dumps({"version": 1, "folders": []}), json.dumps({"version": 1, "folders": "x"}),
    json.dumps({"version": True, "folders": {}}), json.dumps([MARKER]),
])
def test_a_damaged_file_is_refused_with_a_fix_and_without_echoing_it(path, body):
    raw_file(path, None, text=body)
    with pytest.raises(TrustStoreError) as err:
        TrustStore(path).get("/p/x")
    text = str(err.value)
    assert str(path) in text and "damaged" in text and MARKER not in text


def test_a_file_from_a_newer_release_is_refused_not_misread(path):
    raw_file(path, {}, version=2)
    with pytest.raises(TrustStoreError) as err:
        TrustStore(path).items()
    assert "newer" in str(err.value) and str(path) in str(err.value)


def test_a_file_that_is_not_utf8_is_refused(path):
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00{")
    path.chmod(0o600)
    with pytest.raises(TrustStoreError):
        TrustStore(path).items()


def test_an_oversize_file_is_refused(path, monkeypatch):
    raw_file(path, {"/p/x": good()})
    monkeypatch.setattr(store_mod, "MAX_FILE_BYTES", 20)
    with pytest.raises(TrustStoreError):
        TrustStore(path).items()


# ---------------------------------------------------------------------------------------------------- loose permissions
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o620, 0o602, 0o700, 0o601, 0o777])
def test_a_file_anyone_else_can_touch_is_refused_with_the_fix(path, mode):
    raw_file(path, {"/p/x": good()}, mode=mode)
    with pytest.raises(TrustStoreError) as err:
        TrustStore(path).get("/p/x")
    text = str(err.value)
    assert f"chmod 600 {path}" in text and f"{mode:04o}" in text


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_owner_only_modes_are_accepted(path, mode):
    raw_file(path, {"/p/x": good()}, mode=mode)
    assert TrustStore(path).get("/p/x") is not None


def test_reading_never_changes_the_mode_of_a_loose_file(path):
    raw_file(path, {"/p/x": good()}, mode=0o644)
    with pytest.raises(TrustStoreError):
        TrustStore(path).get("/p/x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_a_file_owned_by_someone_else_is_refused(path, monkeypatch):
    raw_file(path, {"/p/x": good()})
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(TrustStoreError) as err:
        TrustStore(path).get("/p/x")
    assert "another user" in str(err.value)


def test_a_symlink_in_place_of_the_file_is_refused(path, tmp_path):
    real = tmp_path / "real.json"
    raw_file(real, {"/p/x": good()})
    path.parent.mkdir(parents=True)
    os.symlink(real, path)
    with pytest.raises(TrustStoreError) as err:
        TrustStore(path).get("/p/x")
    assert "not a regular file" in str(err.value)


def test_a_directory_in_place_of_the_file_is_refused(path):
    path.mkdir(parents=True)
    with pytest.raises(TrustStoreError):
        TrustStore(path).items()


def test_a_forged_trusted_entry_in_a_loose_file_is_never_believed(path, root):
    """The attack the permission check exists for: someone who can write the file adds 'trusted' for a folder."""
    raw_file(path, {str(root): good("forged")}, mode=0o666)
    with pytest.raises(TrustStoreError):
        TrustStore(path).get(root)


def test_error_texts_are_fixed_text_fit_to_print(path):
    raw_file(path, {}, mode=0o644)
    with pytest.raises(TrustStoreError) as err:
        TrustStore(path).items()
    assert "\x1b" not in str(err.value) and "\n" not in str(err.value)
