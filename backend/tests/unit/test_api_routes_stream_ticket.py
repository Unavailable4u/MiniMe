"""Route-level tests for the `?token=<jwt>` -> `?ticket=` change.

Three entry points, all exercised end to end against the autouse FakeRedis
(tests/conftest.py), so what's asserted is the real chain -- mint a ticket
over HTTP, redeem it on the transport -- and not each half in isolation:

  * POST /api/stream-ticket                  (api/routes/stream_ticket.py)
  * GET  /api/task/{session_id}/stream (SSE) (api/routes/tasks.py)
  * WS   /ws/{session_id}                    (api/server.py)

The security properties under test: a ticket works once, only for the
transport and session it was minted for, and a raw access token is no
longer accepted anywhere in the URL (no fallback).
"""
import time

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api.routes.stream_ticket as ticket_routes
import api.routes.tasks as task_routes
from api.deps import require_auth
from api.stream_tickets import KIND_SSE, KIND_WS, TICKET_TTL_SECONDS, issue_ticket, redeem_ticket
from memory.bus import write

OWNER = "test-owner-id"
SESSION = "chat-1"
# Shaped like a real Supabase access token (three dot-separated segments).
FAKE_JWT = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9.c2lnbmF0dXJlLWJ5dGVzLWhlcmU"


def _make_every_jwt_valid(monkeypatch):
    """Make the Supabase JWT verifier accept ANY string, everywhere it
    could be called from. A fake JWT would fail real verification anyway,
    so on its own it can't show the routes stopped consulting the JWT
    path. With the verifier rigged to say yes, the only way `?token=` is
    still rejected is that nothing looks at it any more."""
    import api.deps
    import api.routes.tasks
    import api.server

    monkeypatch.setattr(api.deps, "_verify_supabase_jwt", lambda token: OWNER)
    for module in (api.routes.tasks, api.server):
        monkeypatch.setattr(module, "_verify_supabase_jwt", lambda token: OWNER, raising=False)


# ============================================================ POST /api/stream-ticket

@pytest.fixture
def mint_client(monkeypatch):
    app = FastAPI()
    app.include_router(ticket_routes.router)
    app.dependency_overrides[require_auth] = lambda: OWNER
    calls = []

    def fake_resolve(chat_id, user_id, require_edit=False):
        calls.append((chat_id, user_id, require_edit))
        return user_id

    monkeypatch.setattr(ticket_routes, "_resolve_chat_or_404", fake_resolve)
    client = TestClient(app)
    client.resolve_calls = calls
    return client


def test_mint_requires_a_real_authorization_header():
    app = FastAPI()
    app.include_router(ticket_routes.router)          # no dependency override
    resp = TestClient(app).post("/api/stream-ticket", json={"kind": "ws", "session_id": SESSION})
    assert resp.status_code == 401


def test_mint_ws_ticket_is_redeemable_exactly_once_by_its_owner(mint_client):
    resp = mint_client.post("/api/stream-ticket", json={"kind": "ws", "session_id": SESSION})
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in"] == TICKET_TTL_SECONDS
    assert resp.headers["cache-control"] == "no-store"

    assert redeem_ticket(body["ticket"], KIND_WS, SESSION) == OWNER
    assert redeem_ticket(body["ticket"], KIND_WS, SESSION) is None


def test_mint_ws_does_not_check_chat_ownership(mint_client):
    """/ws/{session_id} has never checked ownership; the ticket change
    must not silently change who can connect."""
    mint_client.post("/api/stream-ticket", json={"kind": "ws", "session_id": SESSION})
    assert mint_client.resolve_calls == []


def test_mint_sse_checks_read_access_to_the_chat(mint_client):
    resp = mint_client.post("/api/stream-ticket", json={"kind": "sse", "session_id": SESSION})
    assert resp.status_code == 200
    assert mint_client.resolve_calls == [(SESSION, OWNER, False)]   # read access, not edit
    assert redeem_ticket(resp.json()["ticket"], KIND_SSE, SESSION) == OWNER


def test_mint_sse_for_an_inaccessible_chat_404s_and_mints_nothing(mint_client, monkeypatch, fake_bus):
    def deny(*a, **k):
        raise HTTPException(status_code=404, detail="Unknown chat_id")

    monkeypatch.setattr(ticket_routes, "_resolve_chat_or_404", deny)
    resp = mint_client.post("/api/stream-ticket", json={"kind": "sse", "session_id": SESSION})
    assert resp.status_code == 404
    assert fake_bus._dump() == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "tcp", "session_id": SESSION},
        {"kind": "ws", "session_id": ""},
        {"kind": "ws", "session_id": "x" * 201},
        {"kind": "ws"},
        {"session_id": SESSION},
        {},
    ],
)
def test_mint_rejects_invalid_requests(mint_client, payload):
    assert mint_client.post("/api/stream-ticket", json=payload).status_code == 422


def test_mint_returns_503_when_the_ticket_store_is_down(mint_client, fake_bus, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("upstash unreachable")

    monkeypatch.setattr(fake_bus, "set", boom)
    resp = mint_client.post("/api/stream-ticket", json={"kind": "ws", "session_id": SESSION})
    assert resp.status_code == 503
    assert "ticket" not in resp.json()


# ============================================================ GET /api/task/{id}/stream

@pytest.fixture
def sse_client(monkeypatch):
    app = FastAPI()
    app.include_router(task_routes.router)
    resolve_calls = []

    def fake_resolve(chat_id, user_id, require_edit=False):
        resolve_calls.append((chat_id, user_id, require_edit))
        return user_id

    monkeypatch.setattr(task_routes, "_resolve_chat_or_404", fake_resolve)
    write(
        f"pending_synthesis:{SESSION}",
        {"answer": "one two three four five six seven eight", "dedup_notes": {"k": "v"}},
    )
    client = TestClient(app)
    client.resolve_calls = resolve_calls
    return client


def _stream_url(session_id=SESSION):
    return f"/api/task/{session_id}/stream"


def test_sse_streams_with_a_valid_ticket_and_authorizes_as_the_ticket_owner(sse_client):
    ticket = issue_ticket("owner-of-ticket", KIND_SSE, SESSION)
    resp = sse_client.get(_stream_url(), params={"ticket": ticket})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "[DONE]" in resp.text
    assert '"chunk"' in resp.text
    # Access is decided for the user the ticket was minted for, read-only.
    assert sse_client.resolve_calls == [(SESSION, "owner-of-ticket", False)]


def test_sse_ticket_cannot_be_replayed(sse_client):
    ticket = issue_ticket(OWNER, KIND_SSE, SESSION)
    assert sse_client.get(_stream_url(), params={"ticket": ticket}).status_code == 200
    replay = sse_client.get(_stream_url(), params={"ticket": ticket})
    assert replay.status_code == 401


def test_sse_rejects_a_ws_ticket(sse_client):
    ticket = issue_ticket(OWNER, KIND_WS, SESSION)
    assert sse_client.get(_stream_url(), params={"ticket": ticket}).status_code == 401


def test_sse_rejects_a_ticket_minted_for_another_session(sse_client):
    ticket = issue_ticket(OWNER, KIND_SSE, "some-other-chat")
    assert sse_client.get(_stream_url(), params={"ticket": ticket}).status_code == 401


def test_sse_requires_a_ticket(sse_client):
    resp = sse_client.get(_stream_url())
    assert resp.status_code == 401
    assert "ticket" in resp.json()["detail"].lower()


def test_sse_no_longer_accepts_a_raw_access_token_in_the_url(sse_client, monkeypatch):
    """The whole point of the change: no ?token= fallback -- even when the
    JWT verifier would have said yes."""
    _make_every_jwt_valid(monkeypatch)
    resp = sse_client.get(_stream_url(), params={"token": FAKE_JWT})
    assert resp.status_code == 401
    # ...and a JWT smuggled into ?ticket= is rejected too.
    resp = sse_client.get(_stream_url(), params={"ticket": FAKE_JWT})
    assert resp.status_code == 401
    assert sse_client.resolve_calls == []


def test_sse_with_a_valid_ticket_but_no_pending_snapshot_404s(monkeypatch):
    app = FastAPI()
    app.include_router(task_routes.router)
    monkeypatch.setattr(task_routes, "_resolve_chat_or_404", lambda c, u, require_edit=False: u)
    ticket = issue_ticket(OWNER, KIND_SSE, "chat-with-no-snapshot")
    resp = TestClient(app).get(_stream_url("chat-with-no-snapshot"), params={"ticket": ticket})
    assert resp.status_code == 404


# ============================================================ WS /ws/{session_id}

@pytest.fixture
def ws_client():
    from api.server import app          # heavy import, so only when a WS test runs
    return TestClient(app)


def _assert_closed_with_4401(client, url):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url):
            pass
    assert exc.value.code == 4401


def _wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_ws_connects_with_a_valid_ticket_and_registers_the_socket(ws_client):
    from eo import ws_registry

    ticket = issue_ticket(OWNER, KIND_WS, SESSION)
    with ws_client.websocket_connect(f"/ws/{SESSION}?ticket={ticket}"):
        assert _wait_until(lambda: bool(ws_registry._connections.get(SESSION)))
    assert _wait_until(lambda: not ws_registry._connections.get(SESSION))   # unregistered on close


def test_ws_ticket_cannot_be_replayed(ws_client):
    ticket = issue_ticket(OWNER, KIND_WS, SESSION)
    with ws_client.websocket_connect(f"/ws/{SESSION}?ticket={ticket}"):
        pass
    _assert_closed_with_4401(ws_client, f"/ws/{SESSION}?ticket={ticket}")


def test_ws_requires_a_ticket(ws_client):
    _assert_closed_with_4401(ws_client, f"/ws/{SESSION}")


def test_ws_rejects_an_sse_ticket(ws_client):
    ticket = issue_ticket(OWNER, KIND_SSE, SESSION)
    _assert_closed_with_4401(ws_client, f"/ws/{SESSION}?ticket={ticket}")


def test_ws_rejects_a_ticket_minted_for_another_session(ws_client):
    ticket = issue_ticket(OWNER, KIND_WS, "some-other-chat")
    _assert_closed_with_4401(ws_client, f"/ws/{SESSION}?ticket={ticket}")


def test_ws_no_longer_accepts_a_raw_access_token_in_the_url(ws_client, monkeypatch):
    """The whole point of the change: no ?token= fallback -- even when the
    JWT verifier would have said yes."""
    _make_every_jwt_valid(monkeypatch)
    _assert_closed_with_4401(ws_client, f"/ws/{SESSION}?token={FAKE_JWT}")
    _assert_closed_with_4401(ws_client, f"/ws/{SESSION}?ticket={FAKE_JWT}")
