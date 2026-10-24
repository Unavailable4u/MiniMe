"""
m2c/tests/auth/test_auth_credentials.py -- Patch C03: where the device
credential lives, and how it is protected there.

Covers the file backend (0600, atomic, fail closed on loose permissions), the
optional OS keyring (always faked here), and the redacting ``Secret`` and
``Credential`` types.  Redaction of whole flows is in test_auth_redaction.py.
"""
from __future__ import annotations

from auth_fixtures import *  # noqa: F401,F403  (fixtures; see auth_fixtures.py)

import dataclasses
import json
import os
import shlex
import stat
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

import m2c
from m2c.auth import credentials as creds
from m2c.auth.credentials import (
    AuthError,
    Credential,
    CredentialStoreError,
    InsecureCredentialFileError,
    NotLoggedInError,
    Secret,
)
from m2c.paths import ConfigDirError
from mock_server import CREDENTIAL, DEVICE_ID, FakeKeyring, exception_texts

# Captured at import time: the autouse fixture later replaces the module's
# attribute with "no keyring", and one test needs the real function.
_REAL_LOAD_KEYRING = creds._load_keyring

SERVER = "https://api.example.test"


def make_cred(server: str = SERVER, secret: str = CREDENTIAL, device_id: str = DEVICE_ID) -> Credential:
    return Credential(device_credential=Secret(secret), device_id=device_id, server_url=server)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def write_creds_file(content: str | bytes, mode: int = 0o600) -> Path:
    path = creds.credentials_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    os.chmod(path, mode)
    return path


def record(**overrides) -> dict:
    base = {"version": 1, "server_url": SERVER, "device_id": DEVICE_ID, "device_credential": CREDENTIAL}
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Secret and Credential never print
# --------------------------------------------------------------------------
def test_secret_prints_as_redacted_in_every_formatting_route():
    s = Secret(CREDENTIAL)
    for text in (repr(s), str(s), f"{s}", "%s" % s, "%r" % s, format(s), repr([s]), repr({"k": s}), repr((s,))):
        assert CREDENTIAL not in text
        assert "redacted" in text
    assert s.reveal() == CREDENTIAL


def test_secret_equality_is_by_value_and_never_matches_a_plain_string():
    assert Secret("a" * 30) == Secret("a" * 30)
    assert Secret("a" * 30) != Secret("b" * 30)
    assert Secret("a" * 30) != "a" * 30


def test_credential_repr_and_str_hide_the_secret_but_show_the_device():
    cred = make_cred()
    for text in (repr(cred), str(cred), f"{cred}", repr([cred])):
        assert CREDENTIAL not in text
        assert DEVICE_ID in text
        assert "redacted" in text


def test_credential_survives_a_locals_capturing_traceback():
    """Error trackers and ``pytest --showlocals`` print every local variable's
    repr; a Credential held in a frame must not give the secret away."""

    def explode():
        held = make_cred()  # noqa: F841 (the point is that it is a local)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError) as info:
        explode()
    text = "".join(traceback.TracebackException.from_exception(info.value, capture_locals=True).format())
    assert "held" in text and CREDENTIAL not in text


def test_credential_is_immutable():
    cred = make_cred()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cred.device_id = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(secret="short"),
        dict(secret="has a space " + "x" * 30),
        dict(secret="line\nbreak" + "x" * 30),
        dict(device_id="not-a-uuid"),
        dict(server="http://api.example.test"),  # cleartext to a non-loopback host
        dict(server="https://user:pw@api.example.test"),
    ],
)
def test_credential_rejects_malformed_parts_without_echoing_them(kwargs):
    with pytest.raises(ValueError) as info:
        make_cred(**kwargs)
    for value in kwargs.values():
        assert value not in str(info.value)


def test_credential_normalises_the_server_url():
    assert make_cred(server="https://API.Example.test/").server_url == "https://api.example.test"


# --------------------------------------------------------------------------
# File backend: location, mode, atomicity
# --------------------------------------------------------------------------
def test_save_writes_a_0600_file_inside_a_0700_directory():
    where = creds.save_credential(make_cred())
    path = creds.credentials_path()
    assert where == "file"
    assert path == Path(os.environ["M2C_HOME"]) / "credentials.json"
    assert mode_of(path) == 0o600
    assert mode_of(path.parent) == 0o700
    assert json.loads(path.read_text()) == record()


def test_load_returns_what_was_saved():
    cred = make_cred()
    creds.save_credential(cred)
    loaded = creds.load_credential()
    assert loaded == cred
    assert loaded.device_credential.reveal() == CREDENTIAL
    assert loaded.device_id == DEVICE_ID
    assert loaded.server_url == SERVER


def test_file_is_0600_whatever_the_umask():
    previous = os.umask(0o000)
    try:
        creds.save_credential(make_cred())
    finally:
        os.umask(previous)
    assert mode_of(creds.credentials_path()) == 0o600


def test_overwriting_a_world_readable_file_leaves_a_0600_file():
    write_creds_file(json.dumps(record(device_credential="OLD-" + "x" * 30)), mode=0o644)
    creds.save_credential(make_cred())
    path = creds.credentials_path()
    assert mode_of(path) == 0o600
    assert json.loads(path.read_text())["device_credential"] == CREDENTIAL


def test_the_file_is_complete_and_0600_before_it_becomes_visible(monkeypatch):
    """Atomic write: the credential is written to a private temp file, and the
    final name only ever points at a finished 0600 file."""
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        seen["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        seen["content"] = json.loads(Path(src).read_text())
        seen["dst_existed"] = Path(dst).exists()
        return real_replace(src, dst)

    monkeypatch.setattr(creds.os, "replace", spy)
    creds.save_credential(make_cred())
    assert seen["mode"] == 0o600
    assert seen["content"] == record()
    assert seen["dst_existed"] is False


def test_a_failed_replace_keeps_the_old_file_and_leaves_no_temp_files(monkeypatch):
    old = json.dumps(record(device_credential="OLD-" + "y" * 30))
    path = write_creds_file(old)

    def boom(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(creds.os, "replace", boom)
    with pytest.raises(CredentialStoreError):
        creds.save_credential(make_cred())
    assert path.read_text() == old
    assert sorted(p.name for p in path.parent.iterdir()) == ["credentials.json"]


def test_save_fails_closed_when_the_config_directory_is_unusable(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("M2C_HOME", str(blocker))
    with pytest.raises(ConfigDirError):
        creds.save_credential(make_cred())


# --------------------------------------------------------------------------
# Reading fails closed
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604, 0o666, 0o610, 0o700, 0o601])
def test_a_file_looser_than_0600_is_refused_with_the_fix(mode):
    path = write_creds_file(json.dumps(record()), mode=mode)
    with pytest.raises(InsecureCredentialFileError) as info:
        creds.load_credential()
    message = str(info.value)
    assert f"chmod 600 {shlex.quote(str(path))}" in message
    assert CREDENTIAL not in "\n".join(exception_texts(info.value))
    assert mode_of(path) == mode  # reading never "helpfully" changes it
    assert isinstance(info.value, CredentialStoreError) and isinstance(info.value, AuthError)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_a_file_at_0600_or_stricter_is_accepted(mode):
    write_creds_file(json.dumps(record()), mode=mode)
    assert creds.load_credential().device_credential.reveal() == CREDENTIAL


def test_a_symlinked_credentials_file_is_refused(tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(record()))
    os.chmod(real, 0o600)
    path = creds.credentials_path()
    path.parent.mkdir(mode=0o700, parents=True)
    path.symlink_to(real)
    with pytest.raises(CredentialStoreError) as info:
        creds.load_credential()
    assert "regular file" in str(info.value)


def test_a_directory_in_place_of_the_file_is_refused():
    path = creds.credentials_path()
    path.mkdir(mode=0o700, parents=True)
    with pytest.raises(CredentialStoreError):
        creds.load_credential()


def test_a_file_owned_by_someone_else_is_refused(monkeypatch):
    write_creds_file(json.dumps(record()))
    monkeypatch.setattr(creds, "_current_uid", lambda: os.getuid() + 1)
    with pytest.raises(CredentialStoreError) as info:
        creds.load_credential()
    assert "another user" in str(info.value)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json at all SENTINEL-DO-NOT-ECHO",
        b"\xff\xfe\x00 binary SENTINEL-DO-NOT-ECHO",
        b"[]",
        b'"SENTINEL-DO-NOT-ECHO"',
        json.dumps(record(version=2)).encode(),
        json.dumps({k: v for k, v in record().items() if k != "device_id"}).encode(),
        json.dumps(record(device_credential="has a space SENTINEL-DO-NOT-ECHO" + "x" * 30)).encode(),
        json.dumps(record(device_credential="short")).encode(),
        json.dumps(record(device_id="SENTINEL-DO-NOT-ECHO")).encode(),
        json.dumps(record(server_url="http://api.example.test")).encode(),
        json.dumps(record(device_credential=12345)).encode(),
        b"{" + b'"x":"SENTINEL-DO-NOT-ECHO",' * 5000 + b'"y":1}',  # far larger than any real file
    ],
)
def test_a_damaged_file_is_refused_without_echoing_its_content(content):
    write_creds_file(content)
    with pytest.raises(CredentialStoreError) as info:
        creds.load_credential()
    text = "\n".join(exception_texts(info.value))
    assert "SENTINEL-DO-NOT-ECHO" not in text
    assert CREDENTIAL not in text
    assert "m2c logout" in str(info.value) or "m2c login" in str(info.value)


def test_not_logged_in_when_nothing_is_stored():
    with pytest.raises(NotLoggedInError) as info:
        creds.load_credential()
    assert "m2c login" in str(info.value)
    assert not Path(os.environ["M2C_HOME"]).exists()  # reading creates nothing


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------
def test_delete_removes_the_file_and_reports_it():
    creds.save_credential(make_cred())
    sibling = creds.credentials_path().parent / "state.db"
    sibling.write_text("keep me")
    assert creds.delete_credential() is True
    assert not creds.credentials_path().exists()
    assert sibling.read_text() == "keep me"
    assert creds.delete_credential() is False


def test_delete_does_not_create_the_config_directory():
    assert creds.delete_credential() is False
    assert not Path(os.environ["M2C_HOME"]).exists()


def test_delete_works_on_a_file_with_loose_permissions():
    """Logout must be able to clean up a file that load refuses to read."""
    write_creds_file(json.dumps(record()), mode=0o644)
    assert creds.delete_credential() is True
    assert not creds.credentials_path().exists()


# --------------------------------------------------------------------------
# keyring (always a fake)
# --------------------------------------------------------------------------
def test_keyring_is_used_when_available_and_no_file_is_written(fake_keyring):
    assert creds.save_credential(make_cred()) == "keyring"
    assert not creds.credentials_path().exists()
    ((service, username), value), = fake_keyring.store.items()
    assert service == "m2c"
    assert username.startswith("device:")
    assert json.loads(value) == record()
    assert creds.load_credential() == make_cred()


def test_keyring_entries_are_per_profile(fake_keyring, tmp_path, monkeypatch):
    creds.save_credential(make_cred())
    monkeypatch.setenv("M2C_HOME", str(tmp_path / "second-profile"))
    creds.save_credential(make_cred())
    usernames = {username for (_service, username) in fake_keyring.store}
    assert len(usernames) == 2


def test_saving_to_the_keyring_removes_a_stale_credentials_file(fake_keyring, monkeypatch):
    monkeypatch.setattr(creds, "_load_keyring", lambda: None)
    assert creds.save_credential(make_cred(secret="OLD-" + "z" * 30)) == "file"
    monkeypatch.setattr(creds, "_load_keyring", lambda: fake_keyring)
    assert creds.save_credential(make_cred()) == "keyring"
    assert not creds.credentials_path().exists()
    assert creds.load_credential().device_credential.reveal() == CREDENTIAL


def test_a_backend_with_no_priority_is_treated_as_no_keyring(monkeypatch):
    """keyring's "fail" backend (what WSL2 usually has) reports priority 0.
    Goes through the REAL viability check, not the fixture that bypasses it."""
    fake = FakeKeyring(priority=0)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setattr(creds, "_load_keyring", _REAL_LOAD_KEYRING)
    assert creds.save_credential(make_cred()) == "file"
    assert creds.load_credential() == make_cred()
    assert creds.delete_credential() is True
    assert fake.calls == []  # a non-viable keyring is never even asked


def test_a_viable_backend_is_used_through_the_real_viability_check(monkeypatch):
    fake = FakeKeyring(priority=5)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setattr(creds, "_load_keyring", _REAL_LOAD_KEYRING)
    assert creds.save_credential(make_cred()) == "keyring"
    assert creds.load_credential() == make_cred()


def test_a_failing_keyring_falls_back_to_the_file(fake_keyring):
    fake_keyring.fail_set = RuntimeError(f"keychain is locked {CREDENTIAL}")
    assert creds.save_credential(make_cred()) == "file"
    assert mode_of(creds.credentials_path()) == 0o600
    fake_keyring.fail_get = RuntimeError("dbus went away")
    assert creds.load_credential() == make_cred()  # a broken keyring does not hide the file


def test_load_prefers_the_keyring_then_looks_at_the_file(fake_keyring):
    write_creds_file(json.dumps(record(device_credential="FILE-" + "f" * 30)))
    assert creds.load_credential().device_credential.reveal().startswith("FILE-")
    fake_keyring.store[("m2c", creds._keyring_username())] = json.dumps(record())
    assert creds.load_credential().device_credential.reveal() == CREDENTIAL


def test_delete_clears_both_places(fake_keyring):
    write_creds_file(json.dumps(record()))
    fake_keyring.store[("m2c", creds._keyring_username())] = json.dumps(record())
    assert creds.delete_credential() is True
    assert fake_keyring.store == {}
    assert not creds.credentials_path().exists()
    assert creds.delete_credential() is False


def test_a_keyring_that_cannot_delete_is_reported_not_ignored(fake_keyring):
    """Logout must never claim success while the credential is still there."""
    write_creds_file(json.dumps(record()))
    fake_keyring.store[("m2c", creds._keyring_username())] = json.dumps(record())
    fake_keyring.fail_delete = RuntimeError(f"nope {CREDENTIAL}")
    with pytest.raises(CredentialStoreError) as info:
        creds.delete_credential()
    assert CREDENTIAL not in "\n".join(exception_texts(info.value))
    assert not creds.credentials_path().exists()  # the file was still removed


def test_nothing_to_delete_in_the_keyring_means_no_delete_call(fake_keyring):
    assert creds.delete_credential() is False
    assert [c for c in fake_keyring.calls if c[0] == "delete"] == []


# --------------------------------------------------------------------------
# the real _load_keyring: lazy, optional, never required
# --------------------------------------------------------------------------
def test_load_keyring_returns_a_viable_module(monkeypatch):
    good = FakeKeyring(priority=5)
    monkeypatch.setitem(sys.modules, "keyring", good)
    assert _REAL_LOAD_KEYRING() is good


def test_load_keyring_is_none_when_the_backend_is_not_viable(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring(priority=0))
    assert _REAL_LOAD_KEYRING() is None


def test_load_keyring_is_none_when_the_package_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)  # makes `import keyring` raise ImportError
    assert _REAL_LOAD_KEYRING() is None


def test_load_keyring_is_none_when_the_package_is_broken(monkeypatch):
    broken = FakeKeyring()
    broken.get_keyring = lambda: (_ for _ in ()).throw(RuntimeError("backend init failed"))  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "keyring", broken)
    assert _REAL_LOAD_KEYRING() is None


def test_importing_the_auth_package_never_imports_keyring():
    code = (
        "import sys, m2c.auth, m2c.auth.login, m2c.auth.credentials\n"
        "assert 'keyring' not in sys.modules, 'keyring was imported eagerly'\n"
    )
    src = str(Path(m2c.__file__).resolve().parent.parent)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={"PYTHONPATH": src, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
