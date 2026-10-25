"""
m2c/tests/link/test_link_reconnect.py -- Patch C04: backoff policy and error
classification (m2c/link/reconnect.py), with no network at all.

The clock, the sleep and the random source are injected everywhere, so these
tests are instant and exact.
"""
from __future__ import annotations

import random

import pytest
from websockets.exceptions import ConnectionClosedError

from link_fakes import *  # noqa: F401,F403  (autouse fixtures; see link_fakes.py)

from m2c.auth.credentials import InsecureCredentialFileError, NotLoggedInError, ServerMismatchError
from m2c.link import (
    Backoff,
    BackoffPolicy,
    LinkClosedError,
    LinkConnectionError,
    LinkDeviceRevokedError,
    LinkError,
    LinkProtocolUnsupportedError,
    LinkRateLimitedError,
    LinkStoreError,
    LinkSupersededError,
    LinkUnauthorizedError,
    classify,
    retry_after_of,
)


# --------------------------------------------------------------------------
# The policy
# --------------------------------------------------------------------------
def test_default_policy_values():
    p = BackoffPolicy()
    assert (p.base_s, p.factor, p.cap_s, p.stable_after_s) == (1.0, 2.0, 30.0, 10.0)


def test_ceiling_doubles_then_is_capped():
    b = Backoff(BackoffPolicy(), rng=lambda: 0.0)
    ceilings = []
    for _ in range(9):
        ceilings.append(b.ceiling())
        b.next_delay()
    assert ceilings == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]


def test_full_jitter_spans_zero_to_the_ceiling():
    """Full jitter: the delay is uniform in [0, ceiling), not ceiling plus a
    little noise."""
    low = Backoff(BackoffPolicy(), rng=lambda: 0.0)
    mid = Backoff(BackoffPolicy(), rng=lambda: 0.5)
    high = Backoff(BackoffPolicy(), rng=lambda: 0.999)
    assert low.next_delay() == 0.0
    assert mid.next_delay() == 0.5
    assert high.next_delay() == pytest.approx(0.999)
    # second step: ceiling 2
    assert low.next_delay() == 0.0
    assert mid.next_delay() == 1.0
    assert high.next_delay() == pytest.approx(1.998)


def test_uses_the_injected_random_source_deterministically():
    first = Backoff(BackoffPolicy(), rng=random.Random(7).random)
    second = Backoff(BackoffPolicy(), rng=random.Random(7).random)
    a = [first.next_delay() for _ in range(12)]
    b = [second.next_delay() for _ in range(12)]
    assert a == b
    ceilings = [1, 2, 4, 8, 16, 30, 30, 30, 30, 30, 30, 30]
    assert all(0.0 <= d < c for d, c in zip(a, ceilings))
    assert len(set(a)) > 6                      # it really is random, not a constant


def test_a_stable_connection_resets_the_sequence():
    policy = BackoffPolicy(stable_after_s=10.0)
    b = Backoff(policy, rng=lambda: 0.5)
    assert [b.next_delay() for _ in range(3)] == [0.5, 1.0, 2.0]
    assert b.record_connection(up_for_s=10.0) is True
    assert b.next_delay() == 0.5                 # back to the first step


def test_a_short_connection_does_not_reset():
    """A server that accepts and immediately drops must not keep the delay at
    its minimum: that would be a tight retry loop."""
    b = Backoff(BackoffPolicy(stable_after_s=10.0), rng=lambda: 0.5)
    assert [b.next_delay() for _ in range(3)] == [0.5, 1.0, 2.0]
    assert b.record_connection(up_for_s=9.999) is False
    assert b.next_delay() == 4.0


def test_retry_after_is_a_floor_and_is_clamped():
    b = Backoff(BackoffPolicy(max_retry_after_s=300.0), rng=lambda: 0.5)
    assert b.next_delay(retry_after_s=7) == 7                 # server asked for longer than the jitter
    assert b.next_delay(retry_after_s=0.1) == 1.0             # jitter (ceiling 2 * 0.5) is already longer
    assert b.next_delay(retry_after_s=10**9) == 300.0         # a hostile value cannot stall the client forever


def test_a_very_long_outage_does_not_overflow():
    b = Backoff(BackoffPolicy(), rng=lambda: 0.5)
    for _ in range(5000):
        b.next_delay()
    assert b.ceiling() == 30.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_s": 0}, {"base_s": -1}, {"factor": 0.5}, {"cap_s": 0.5}, {"stable_after_s": -1},
        {"max_retry_after_s": -1}, {"base_s": float("nan")}, {"cap_s": float("inf")},
    ],
)
def test_nonsense_policies_are_refused(kwargs):
    with pytest.raises(ValueError):
        BackoffPolicy(**kwargs)


# --------------------------------------------------------------------------
# The errors a caller sees
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cls, code",
    [
        (LinkUnauthorizedError, "unauthorized"),
        (LinkDeviceRevokedError, "device_revoked"),
        (LinkProtocolUnsupportedError, "protocol_unsupported"),
    ],
)
def test_the_three_terminal_errors_carry_the_protocol_code_and_a_clear_message(cls, code):
    exc = cls() if cls is not LinkProtocolUnsupportedError else cls(min_protocol=2, max_protocol=1)
    assert exc.code == code
    assert exc.retryable is False
    text = str(exc)
    assert len(text) > 30
    if cls is LinkProtocolUnsupportedError:
        assert "update" in text.lower() and "2" in text and "1" in text
    else:
        assert "m2c login" in text


def test_protocol_unsupported_keeps_the_numbers():
    exc = LinkProtocolUnsupportedError(min_protocol=3, max_protocol=1)
    assert (exc.min_protocol, exc.max_protocol) == (3, 1)


def test_rate_limited_is_retryable_and_remembers_how_long():
    exc = LinkRateLimitedError(retry_after_s=12)
    assert exc.retryable is True and exc.retry_after_s == 12 and exc.code == "rate_limited"
    assert retry_after_of(exc) == 12


def test_retry_after_of_other_errors_is_none():
    assert retry_after_of(LinkConnectionError("x")) is None
    assert retry_after_of(OSError()) is None


# --------------------------------------------------------------------------
# What ends the reconnect loop and what does not
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc",
    [
        LinkUnauthorizedError(),
        LinkDeviceRevokedError(),
        LinkProtocolUnsupportedError(min_protocol=2, max_protocol=1),
        LinkSupersededError(),
        LinkStoreError(),
        NotLoggedInError("x"),
        InsecureCredentialFileError("x"),
        ServerMismatchError("x"),
    ],
)
def test_these_end_the_loop(exc):
    assert classify(exc) == "fatal"


@pytest.mark.parametrize(
    "exc",
    [
        LinkConnectionError("lost"),
        LinkRateLimitedError(retry_after_s=3),
        ConnectionRefusedError(),
        OSError("network is unreachable"),
        TimeoutError(),
        ConnectionClosedError(None, None),
    ],
)
def test_these_are_retried_with_backoff(exc):
    assert classify(exc) == "retry"


def test_a_programming_error_is_neither_swallowed_nor_retried_forever():
    assert classify(TypeError("a bug")) == "unexpected"
    assert classify(KeyError("a bug")) == "unexpected"


def test_all_link_errors_share_a_base_and_closed_is_final():
    for cls in (LinkUnauthorizedError, LinkDeviceRevokedError, LinkSupersededError, LinkStoreError, LinkClosedError):
        assert issubclass(cls, LinkError)
    assert classify(LinkClosedError()) == "fatal"
