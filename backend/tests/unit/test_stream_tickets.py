"""Unit tests for api/stream_tickets.py -- the short-lived, single-use
tickets that replace `?token=<jwt>` on the /ws and SSE routes.

Each test targets a property the design depends on: a ticket works once,
only for the kind/session it was minted for, only as long as its TTL,
never stores the raw ticket, and the store failing means "denied", never
"allowed". FakeRedis (tests/conftest.py, autouse via fake_bus) stands in
for Upstash."""
import json
import threading

import pytest

from api import stream_tickets
from api.stream_tickets import (
    KIND_SSE,
    KIND_WS,
    TICKET_TTL_SECONDS,
    TicketStoreUnavailable,
    issue_ticket,
    redeem_ticket,
)

USER = "user-123"
SESSION = "chat-abc"


# --------------------------------------------------------------- minting

def test_issue_returns_high_entropy_urlsafe_ticket():
    t1 = issue_ticket(USER, KIND_WS, SESSION)
    t2 = issue_ticket(USER, KIND_WS, SESSION)
    assert t1 != t2
    assert len(t1) >= 43                       # token_urlsafe(32) -> 43 chars
    assert set(t1) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_issue_stores_only_a_hash_never_the_raw_ticket(fake_bus):
    ticket = issue_ticket(USER, KIND_WS, SESSION)
    dump = fake_bus._dump()
    assert len(dump) == 1
    (key, value), = dump.items()
    assert key.startswith("stream_ticket:")
    assert ticket not in key
    assert ticket not in value
    assert json.loads(value) == {"u": USER, "k": KIND_WS, "s": SESSION}


def test_issue_sets_a_short_ttl(fake_bus, monkeypatch):
    seen = {}
    real_set = fake_bus.set

    def recording_set(key, value, *args, **kwargs):
        seen.update(kwargs)
        return real_set(key, value, *args, **kwargs)

    monkeypatch.setattr(fake_bus, "set", recording_set)
    issue_ticket(USER, KIND_SSE, SESSION)
    assert seen.get("ex") == TICKET_TTL_SECONDS
    assert TICKET_TTL_SECONDS <= 60            # "short-lived" is the point


def test_ticket_is_not_namespaced_by_app_slug(fake_bus):
    """Minted in one request, redeemed in another -- the active app_slug
    can differ between them, so the key must not depend on it."""
    import memory.bus as bus_module

    bus_module._app_slug_ctx.set("slug-at-mint-time")
    ticket = issue_ticket(USER, KIND_WS, SESSION)
    bus_module._app_slug_ctx.set("a-different-slug-at-redeem-time")
    assert redeem_ticket(ticket, KIND_WS, SESSION) == USER


@pytest.mark.parametrize(
    "user_id,kind,session_id",
    [(USER, "tcp", SESSION), (USER, "", SESSION), ("", KIND_WS, SESSION), (USER, KIND_WS, "")],
)
def test_issue_rejects_bad_arguments(user_id, kind, session_id):
    with pytest.raises(ValueError):
        issue_ticket(user_id, kind, session_id)


def test_issue_raises_when_store_is_down(fake_bus, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("upstash unreachable")

    monkeypatch.setattr(fake_bus, "set", boom)
    with pytest.raises(TicketStoreUnavailable):
        issue_ticket(USER, KIND_WS, SESSION)


# ------------------------------------------------------------ redemption

def test_redeem_returns_the_user_it_was_minted_for():
    ticket = issue_ticket(USER, KIND_WS, SESSION)
    assert redeem_ticket(ticket, KIND_WS, SESSION) == USER


def test_ticket_is_single_use():
    ticket = issue_ticket(USER, KIND_SSE, SESSION)
    assert redeem_ticket(ticket, KIND_SSE, SESSION) == USER
    assert redeem_ticket(ticket, KIND_SSE, SESSION) is None


def test_ticket_for_one_kind_cannot_open_the_other():
    ticket = issue_ticket(USER, KIND_WS, SESSION)
    assert redeem_ticket(ticket, KIND_SSE, SESSION) is None


def test_ticket_for_one_session_cannot_open_another():
    ticket = issue_ticket(USER, KIND_WS, SESSION)
    assert redeem_ticket(ticket, KIND_WS, "someone-elses-chat") is None


def test_misused_ticket_is_burned_even_when_rejected():
    """A ticket presented for the wrong session/kind is consumed by the
    attempt, so an attacker can't probe with it and then retry correctly."""
    ticket = issue_ticket(USER, KIND_WS, SESSION)
    assert redeem_ticket(ticket, KIND_WS, "wrong-session") is None
    assert redeem_ticket(ticket, KIND_WS, SESSION) is None


def test_unknown_ticket_is_rejected():
    assert redeem_ticket("A" * 43, KIND_WS, SESSION) is None


@pytest.mark.parametrize(
    "junk",
    [
        None,
        "",
        "short",
        "x" * 129,
        "has spaces in it, which are not url-safe chars",
        # A real-looking JWT: dots aren't in the ticket alphabet, so a
        # client that mistakenly (or maliciously) sends its access token
        # as ?ticket= is rejected before it even reaches Redis.
        "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9.c2lnbmF0dXJlLWJ5dGVzLWhlcmU",
        123,
    ],
)
def test_malformed_tickets_are_rejected_without_touching_the_store(junk, fake_bus, monkeypatch):
    def must_not_be_called(*a, **k):
        raise AssertionError("malformed ticket should never reach Redis")

    monkeypatch.setattr(fake_bus, "getdel", must_not_be_called)
    assert redeem_ticket(junk, KIND_WS, SESSION) is None


def test_redeem_fails_closed_when_store_is_down(fake_bus, monkeypatch):
    ticket = issue_ticket(USER, KIND_WS, SESSION)

    def boom(*a, **k):
        raise ConnectionError("upstash unreachable")

    monkeypatch.setattr(fake_bus, "getdel", boom)
    assert redeem_ticket(ticket, KIND_WS, SESSION) is None


@pytest.mark.parametrize(
    "stored",
    ["not json", "[]", "null", '"a string"', json.dumps({"k": KIND_WS, "s": SESSION}),
     json.dumps({"u": "", "k": KIND_WS, "s": SESSION}),
     json.dumps({"u": 42, "k": KIND_WS, "s": SESSION})],
)
def test_corrupt_stored_payload_is_rejected(stored, fake_bus):
    ticket = "B" * 43
    fake_bus.set(stream_tickets._key(ticket), stored)
    assert redeem_ticket(ticket, KIND_WS, SESSION) is None


def test_expired_ticket_is_rejected(fake_bus):
    """Expiry is Redis's job (we pass ex=TTL, asserted above). When the
    key has expired it is simply gone, which must read as 'denied'."""
    ticket = issue_ticket(USER, KIND_WS, SESSION)
    fake_bus._store.clear()                    # what TTL expiry does to the key
    assert redeem_ticket(ticket, KIND_WS, SESSION) is None


def test_concurrent_redemptions_admit_exactly_one():
    """The reason for GETDEL over get()+delete(): many racing redeemers,
    exactly one wins."""
    ticket = issue_ticket(USER, KIND_WS, SESSION)
    results, barrier = [], threading.Barrier(16)

    def attempt():
        barrier.wait()
        results.append(redeem_ticket(ticket, KIND_WS, SESSION))

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(USER) == 1
    assert results.count(None) == 15
