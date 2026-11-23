"""Patch SX-20 -- api/uploads.py::save_upload_capped.

Self-contained: a fake UploadFile, a throw-away temp directory, no network, no database.
Nothing here skips.
"""
import asyncio
import importlib
import io
import os
import tempfile

import pytest
from fastapi import HTTPException

from api import uploads
from api.uploads import CHUNK_BYTES, save_upload_capped

MB = 1_048_576


def _run(coro):
    return asyncio.run(coro)


class _FakeUpload:
    """Serves `data` and records every read() so tests can see HOW it was read."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.sizes: list = []

    async def read(self, size=-1):
        self.sizes.append(size)
        return self._buf.read(size)


class _EndlessUpload:
    """An upload that never ends (like a hostile client). Never allocates more than asked."""

    def __init__(self):
        self.served = 0
        self.sizes: list = []

    async def read(self, size=-1):
        assert size is not None and size > 0, "an unbounded read() on an endless upload"
        self.sizes.append(size)
        self.served += size
        return b"\0" * size


class _FailingUpload:
    """Serves one chunk, then fails the way a dropped connection would."""

    def __init__(self):
        self.calls = 0

    async def read(self, size=-1):
        self.calls += 1
        if self.calls == 1:
            return b"a" * 1000
        raise RuntimeError("connection dropped")


@pytest.fixture
def tmpdir_only(tmp_path, monkeypatch):
    """Point tempfile at an empty directory so a leftover temp file is visible."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def test_under_the_cap_is_saved_whole_with_its_suffix(tmpdir_only):
    data = os.urandom(1000)
    path = _run(save_upload_capped(_FakeUpload(data), ".pdf", 5000))
    try:
        assert path.endswith(".pdf")
        assert os.path.dirname(path) == str(tmpdir_only)
        with open(path, "rb") as fh:
            assert fh.read() == data
    finally:
        os.remove(path)


def test_exactly_at_the_cap_is_accepted(tmpdir_only):
    data = b"x" * 5000
    path = _run(save_upload_capped(_FakeUpload(data), ".bin", 5000))
    try:
        assert os.path.getsize(path) == 5000
    finally:
        os.remove(path)


def test_one_byte_over_is_413_and_leaves_no_temp_file(tmpdir_only):
    with pytest.raises(HTTPException) as exc:
        _run(save_upload_capped(_FakeUpload(b"x" * 5001), ".bin", 5000))
    assert exc.value.status_code == 413
    assert list(tmpdir_only.iterdir()) == []


def test_empty_upload_is_saved_as_an_empty_file(tmpdir_only):
    path = _run(save_upload_capped(_FakeUpload(b""), ".txt", 5000))
    try:
        assert os.path.getsize(path) == 0
    finally:
        os.remove(path)


def test_a_multi_chunk_upload_is_read_in_bounded_chunks(tmpdir_only):
    data = os.urandom(3 * MB + 17)
    up = _FakeUpload(data)
    path = _run(save_upload_capped(up, ".bin", 10 * MB))
    try:
        with open(path, "rb") as fh:
            assert fh.read() == data
    finally:
        os.remove(path)
    assert len(up.sizes) > 1  # more than one read: it is not buffered in one go
    assert all(isinstance(s, int) and 0 < s <= CHUNK_BYTES for s in up.sizes)  # never read() / read(-1)


def test_an_endless_upload_is_cut_off_at_cap_plus_one_byte(tmpdir_only):
    cap = 2 * MB + 500  # crosses a chunk boundary
    up = _EndlessUpload()
    with pytest.raises(HTTPException) as exc:
        _run(save_upload_capped(up, ".bin", cap))
    assert exc.value.status_code == 413
    assert up.served == cap + 1  # stopped immediately, did not read on
    assert max(up.sizes) <= CHUNK_BYTES
    assert list(tmpdir_only.iterdir()) == []


def test_a_read_error_removes_the_temp_file_and_propagates(tmpdir_only):
    with pytest.raises(RuntimeError, match="connection dropped"):
        _run(save_upload_capped(_FailingUpload(), ".bin", 5000))
    assert list(tmpdir_only.iterdir()) == []


def test_the_413_message_names_the_limit_and_nothing_else(tmpdir_only):
    with pytest.raises(HTTPException) as exc:
        _run(save_upload_capped(_FakeUpload(b"x" * 20), ".bin", 10))
    assert exc.value.detail == "Upload too large: the limit is 10 bytes."


# ---- the caps ---------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    (None, 7),          # unset
    ("", 7),            # empty
    ("   ", 7),         # blank
    ("123", 123),       # valid
    (" 456 ", 456),     # surrounding spaces
    ("abc", 7),         # not a number
    ("12.5", 7),        # not an integer
    ("0", 7),           # not positive
    ("-5", 7),          # not positive
])
def test_env_int(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SX20_TEST_CAP", raising=False)
    else:
        monkeypatch.setenv("SX20_TEST_CAP", raw)
    assert uploads._env_int("SX20_TEST_CAP", 7) == expected


def test_placeholder_defaults_and_env_override(monkeypatch):
    names = ("IMPORT_MAX_BYTES", "PDF_MAX_BYTES", "VOICE_MAX_BYTES")
    try:
        for n in names:
            monkeypatch.delenv(n, raising=False)
        importlib.reload(uploads)
        assert uploads.IMPORT_MAX_BYTES == 10_000_000
        assert uploads.PDF_MAX_BYTES == 25_000_000
        assert uploads.VOICE_MAX_BYTES == 25_000_000

        monkeypatch.setenv("IMPORT_MAX_BYTES", "123")
        monkeypatch.setenv("PDF_MAX_BYTES", "not-a-number")
        importlib.reload(uploads)
        assert uploads.IMPORT_MAX_BYTES == 123
        assert uploads.PDF_MAX_BYTES == 25_000_000  # bad value ignored
    finally:
        monkeypatch.undo()
        importlib.reload(uploads)  # leave the module as the environment really defines it
