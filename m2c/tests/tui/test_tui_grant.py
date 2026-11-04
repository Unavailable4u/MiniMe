"""Patch C09a: asking for more credits against a mock HTTP server (m2c.tui.grant)."""
import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from m2c.auth import AuthNetworkError, NotLoggedInError
from m2c.tui.grant import GrantResult, describe, request_grant

RUN = "run_abc-123"


def fake_token():
    return "t" * 8 + "." + "u" * 8      # not secret-shaped; only checked for being passed through


class Mock:
    def __init__(self):
        self.status, self.body, self.headers_out = 200, {}, {}
        self.requests = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                mock.requests.append((self.path, dict(self.headers)))
                payload = json.dumps(mock.body).encode()
                self.send_response(mock.status)
                for k, v in mock.headers_out.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    m = Mock()
    yield m
    m.close()


def ask(mock, token_getter=fake_token, run_id=RUN, **kw):
    return asyncio.run(request_grant(run_id, token_getter=token_getter, server_url=mock.url, timeout=5, **kw))


def error(code, **extra):
    return {"detail": {"code": code, "message": "free text the client must never show", **extra}}


def test_success_sends_the_bearer_token_to_the_run_grant_route_and_parses_the_numbers(mock):
    mock.body = {"state": "running", "run_credit_ceiling": 20, "run_credits_spent": 12.5, "granted_credits": 10}
    result = ask(mock)
    assert result == GrantResult("granted", "running", 20.0, 12.5, 10.0) and result.ok
    path, headers = mock.requests[0]
    assert path == f"/v1/code/runs/{RUN}/grant"
    assert headers["Authorization"] == "Bearer " + fake_token()
    assert "$" not in describe(result) and "12.5 of 20 credits" in describe(result)


def test_pool_exhausted_shows_the_reset_time(mock):
    mock.status, mock.body = 409, error("quota_exhausted", reset_at="2026-12-01T00:00:00Z")
    result = ask(mock)
    assert result.kind == "pool_exhausted" and result.reset_at == "2026-12-01T00:00:00Z"
    assert "resets 2026-12-01T00:00:00Z" in describe(result) and "used up" in describe(result)


def test_a_reset_time_that_is_not_a_timestamp_is_not_shown(mock):
    mock.status, mock.body = 409, error("quota_exhausted", reset_at="\x1b[31mevil [bold]")
    result = ask(mock)
    assert result.reset_at is None and "resets" not in describe(result)


def test_device_offline_is_said_plainly(mock):
    mock.status, mock.body = 409, error("device_offline")
    result = ask(mock)
    assert result.kind == "device_offline" and "offline" in describe(result)


@pytest.mark.parametrize("status,body,kind", [
    (409, error("not_paused_budget", state="running"), "not_paused"),
    (409, error("buffer_gone"), "run_gone"),
    (404, {}, "run_gone"),
    (429, error("rate_limited"), "rate_limited"),
    (401, {}, "unauthorized"),
    (503, error("grant_unavailable"), "unavailable"),
    (503, error("usage_unavailable"), "unavailable"),
    (500, {"oops": 1}, "error"),
])
def test_other_outcomes(mock, status, body, kind):
    mock.status, mock.body = status, body
    result = ask(mock)
    assert result.kind == kind and not result.ok and describe(result)
    assert "free text" not in describe(result)


def test_a_non_json_or_unexpected_success_body_is_an_error_not_a_crash(mock):
    mock.body = ["not", "a", "dict"]
    assert ask(mock).kind == "error"


def test_hostile_numbers_and_state_words_are_dropped(mock):
    mock.body = {"state": "\x1b[31mrunning", "run_credit_ceiling": "20", "run_credits_spent": -1, "granted_credits": True}
    result = ask(mock)
    assert result == GrantResult("granted")
    assert describe(result) == "More credits were granted; the run continues."


def test_no_network_is_reported_not_raised():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    result = asyncio.run(request_grant(RUN, token_getter=fake_token, server_url=f"http://127.0.0.1:{port}", timeout=3))
    assert result.kind == "network" and "Could not reach" in describe(result)


def test_a_redirect_is_not_followed_and_the_token_goes_nowhere_else(mock, monkeypatch):
    other = Mock()
    try:
        mock.status, mock.headers_out = 307, {"Location": other.url + "/stolen"}
        result = ask(mock)
        assert result.kind == "error" and other.requests == []
    finally:
        other.close()


def test_auth_problems_become_outcomes_and_never_reach_the_network(mock):
    def not_logged_in():
        raise NotLoggedInError("no")

    def offline():
        raise AuthNetworkError("down")

    assert ask(mock, not_logged_in).kind == "unauthorized"
    assert ask(mock, offline).kind == "network"
    assert ask(mock, lambda: "").kind == "error"
    assert mock.requests == []


def test_a_bad_run_id_is_refused_before_any_request(mock):
    for bad in ("", "../x", "a b", "x" * 100, None):
        assert ask(mock, run_id=bad).kind == "error"
    assert mock.requests == []


def test_a_plain_http_server_address_that_is_not_loopback_is_refused(mock):
    result = asyncio.run(request_grant(RUN, token_getter=fake_token, server_url="http://example.com", timeout=3))
    assert result.kind == "error"


def test_the_token_is_taken_from_a_secret_object_through_reveal(mock):
    from m2c.auth.credentials import Secret
    mock.body = {"state": "running"}
    assert ask(mock, token_getter=lambda: Secret(fake_token())).ok
    assert mock.requests[0][1]["Authorization"] == "Bearer " + fake_token()


def test_every_outcome_text_is_in_credits_and_has_no_dollars():
    for kind in ("granted", "pool_exhausted", "device_offline", "not_paused", "run_gone", "rate_limited",
                 "unavailable", "unauthorized", "network", "error"):
        text = describe(GrantResult(kind, ceiling=3.0, spent=1.0))
        assert text and "$" not in text and "USD" not in text and "dollar" not in text.lower()
