"""Patch SX-21 -- /api/notes/import, /api/notes/pdf and /api/notes/voice enforce the upload cap.

Mounts just the notebooks router on a throwaway FastAPI app and overrides require_auth (same
pattern as tests/unit/test_api_routes_user_profile.py). process_upload is replaced by a
recorder, so no ingestor, model or database is involved. The caps are lowered with
monkeypatch so the tests only ever send a few bytes. Nothing here skips.
"""
import os
import tempfile

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.routes.notebooks as nb
from api.deps import require_auth

CAP = 100

# (url, upload file name, name of the cap constant in the route module, `kind` given to process_upload)
ROUTES = [
    pytest.param("/api/notes/import", "notes.md", "IMPORT_MAX_BYTES", "import", id="import"),
    pytest.param("/api/notes/pdf", "doc.pdf", "PDF_MAX_BYTES", "pdf", id="pdf"),
    pytest.param("/api/notes/voice", "memo.mp3", "VOICE_MAX_BYTES", "voice", id="voice"),
]


class _Recorder:
    """Stands in for process_upload. Records the call and what the temp file held at that moment."""

    def __init__(self):
        self.calls = []
        self.error = None

    def __call__(self, kind, payload, workspace_id, **kwargs):
        with open(payload, "rb") as fh:
            content = fh.read()
        self.calls.append({"kind": kind, "path": payload, "content": content, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return {"node_ids": ["n1"], "title": "a title"}


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(nb, "process_upload", rec)
    return rec


@pytest.fixture
def tmpdir_only(tmp_path, monkeypatch):
    """Point tempfile at an empty directory so a leftover temp file is visible."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(monkeypatch):
    for const in ("IMPORT_MAX_BYTES", "PDF_MAX_BYTES", "VOICE_MAX_BYTES"):
        monkeypatch.setattr(nb, const, CAP)
    app = FastAPI()
    app.include_router(nb.router)
    app.dependency_overrides[require_auth] = lambda: "test-owner-id"
    return TestClient(app)


def _post(client, url, filename, data):
    return client.post(
        url,
        data={"workspace_id": "ws-1"},
        files={"file": (filename, data, "application/octet-stream")},
    )


@pytest.mark.parametrize("url, filename, const, kind", ROUTES)
def test_oversize_upload_is_413_and_never_reaches_the_ingestor(client, recorder, tmpdir_only, url, filename, const, kind):
    resp = _post(client, url, filename, b"x" * (CAP + 1))
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"].lower()
    assert recorder.calls == []
    assert list(tmpdir_only.iterdir()) == []  # the temp file was removed


@pytest.mark.parametrize("url, filename, const, kind", ROUTES)
def test_upload_exactly_at_the_cap_is_accepted(client, recorder, tmpdir_only, url, filename, const, kind):
    data = b"y" * CAP
    resp = _post(client, url, filename, data)
    assert resp.status_code == 200
    assert resp.json() == {"node_ids": ["n1"], "title": "a title"}
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["kind"] == kind
    assert call["content"] == data  # the ingestor saw the whole upload
    assert call["kwargs"].get("created_by") == "user"
    assert not os.path.exists(call["path"])  # and the route removed the temp file afterwards
    assert list(tmpdir_only.iterdir()) == []


@pytest.mark.parametrize("url, filename, const, kind", ROUTES)
def test_an_ingestor_value_error_is_still_400_and_cleans_up(client, recorder, tmpdir_only, url, filename, const, kind):
    recorder.error = ValueError("cannot read this file")
    resp = _post(client, url, filename, b"z" * 10)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cannot read this file"
    assert list(tmpdir_only.iterdir()) == []


@pytest.mark.parametrize("url, filename, const, kind", ROUTES)
def test_the_cap_is_read_from_the_module_constant(monkeypatch, client, recorder, tmpdir_only, url, filename, const, kind):
    """Raising the cap lets the same upload through; that is how the env override takes effect."""
    assert _post(client, url, filename, b"x" * 150).status_code == 413
    monkeypatch.setattr(nb, const, 200)
    assert _post(client, url, filename, b"x" * 150).status_code == 200


def test_import_with_an_unsupported_extension_is_still_400_before_any_copy(client, recorder, tmpdir_only):
    resp = _post(client, "/api/notes/import", "notes.exe", b"x" * 10)
    assert resp.status_code == 400
    assert "Unsupported import format 'exe'" in resp.json()["detail"]
    assert recorder.calls == []
    assert list(tmpdir_only.iterdir()) == []


def test_voice_keeps_the_upload_extension_for_the_temp_file(client, recorder):
    assert _post(client, "/api/notes/voice", "memo.MP3", b"abc").status_code == 200
    assert recorder.calls[0]["path"].endswith(".MP3")


def test_voice_without_an_extension_uses_the_audio_fallback(client, recorder):
    assert _post(client, "/api/notes/voice", "memo", b"abc").status_code == 200
    assert recorder.calls[0]["path"].endswith(".audio")


def test_the_three_routes_no_longer_read_the_whole_upload_into_memory():
    """Tripwire: an unbounded `await file.read()` in a route would undo the cap."""
    with open(nb.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "await file.read()" not in source
