"""
tests/unit/test_api_middleware.py — api/middleware.py.

The bug these pin: an UNHANDLED exception (a DB error, any bug) used to
be answered by Starlette's outermost ServerErrorMiddleware, i.e. from
outside CORSMiddleware, so the 500 carried no Access-Control-Allow-Origin
header and the browser surfaced it as an opaque "Failed to fetch"
instead of a readable 500 (this is what made the Build tab's move/rename
permission error look like a network failure).

These tests build a small app through add_cors_and_error_middleware() —
the exact function api/server.py calls — rather than importing
api.server (which boots the DB pool, Sentry, Laya, MCP...), so the real
middleware order is what's under test.
"""
import asyncio
import warnings

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.middleware import (
    INTERNAL_ERROR_DETAIL,
    UnhandledErrorMiddleware,
    add_cors_and_error_middleware,
)

warnings.filterwarnings("ignore", message=".*httpx.*", category=DeprecationWarning)

ORIGIN = "http://localhost:3000"


class _Saturated(Exception):
    """Stands in for DatabaseUnavailable & co: has its own handler."""


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(_Saturated)
    async def _saturated(request: Request, exc: _Saturated):
        return JSONResponse({"detail": "busy"}, status_code=503, headers={"Retry-After": "2"})

    add_cors_and_error_middleware(app, [ORIGIN])

    @app.get("/boom")
    def boom():
        raise RuntimeError("select * from secret_table failed: permission denied")

    @app.get("/http-error")
    def http_error():
        raise HTTPException(status_code=400, detail="bad path")

    @app.get("/saturated")
    def saturated():
        raise _Saturated()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    return app


@pytest.fixture
def client():
    # raise_server_exceptions=False: look at the response a browser would get.
    return TestClient(_build_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------
# The fix itself
# ---------------------------------------------------------------------

def test_unhandled_error_is_a_json_500_with_cors_headers(client):
    r = client.get("/boom", headers={"Origin": ORIGIN})
    assert r.status_code == 500
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert r.json() == {"detail": INTERNAL_ERROR_DETAIL}


def test_unhandled_error_does_not_leak_exception_text(client):
    r = client.get("/boom", headers={"Origin": ORIGIN})
    assert "secret_table" not in r.text
    assert "permission denied" not in r.text


def test_unhandled_error_is_still_raised_to_the_server():
    # The response is sent, but the exception must keep propagating so
    # uvicorn logs the traceback and Sentry's integration captures it.
    strict = TestClient(_build_app())  # raise_server_exceptions=True
    with pytest.raises(RuntimeError, match="secret_table"):
        strict.get("/boom", headers={"Origin": ORIGIN})


def test_cors_is_not_broadened_for_other_origins(client):
    r = client.get("/boom", headers={"Origin": "http://evil.example"})
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------
# Nothing that already worked changes
# ---------------------------------------------------------------------

def test_http_exception_is_untouched(client):
    r = client.get("/http-error", headers={"Origin": ORIGIN})
    assert r.status_code == 400
    assert r.json() == {"detail": "bad path"}
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_registered_exception_handlers_still_win(client):
    r = client.get("/saturated", headers={"Origin": ORIGIN})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"
    assert r.json() == {"detail": "busy"}


def test_successful_requests_are_untouched(client):
    r = client.get("/ok", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_preflight_still_answered(client):
    r = client.options("/boom", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert r.status_code == 200
    assert "POST" in r.headers["access-control-allow-methods"]


def test_premise_cors_alone_does_not_cover_unhandled_errors():
    """Documents WHY the middleware exists: with CORSMiddleware alone,
    Starlette's outermost layer answers an unhandled error without CORS
    headers. If a Starlette upgrade ever makes this fail, the middleware
    has become unnecessary and can be removed."""
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_methods=["GET"], allow_headers=["*"])

    @app.get("/boom")
    def boom():
        raise RuntimeError("x")

    r = TestClient(app, raise_server_exceptions=False).get("/boom", headers={"Origin": ORIGIN})
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------
# Raw-ASGI edge cases (driven directly; TestClient can't express these)
# ---------------------------------------------------------------------

def _run(middleware, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def go():
        await middleware(scope, receive, send)

    return sent, go


def test_error_after_response_started_is_not_answered_twice():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("died mid-stream")

    sent, go = _run(UnhandledErrorMiddleware(app), {"type": "http"})
    with pytest.raises(RuntimeError, match="died mid-stream"):
        asyncio.run(go())

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1 and starts[0]["status"] == 200


def test_failure_to_send_the_500_does_not_mask_the_original_error():
    async def app(scope, receive, send):
        raise ValueError("original")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def disconnected_send(message):
        raise OSError("client went away")

    mw = UnhandledErrorMiddleware(app)
    with pytest.raises(ValueError, match="original"):
        asyncio.run(mw({"type": "http"}, receive, disconnected_send))


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
def test_non_http_scopes_pass_straight_through(scope_type):
    async def app(scope, receive, send):
        raise RuntimeError("ws failure")

    sent, go = _run(UnhandledErrorMiddleware(app), {"type": scope_type})
    with pytest.raises(RuntimeError, match="ws failure"):
        asyncio.run(go())
    assert sent == []  # no HTTP 500 invented for a non-HTTP scope
