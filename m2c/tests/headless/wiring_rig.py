"""
m2c/tests/headless/wiring_rig.py -- Patch C10b: what the "headless runs really use tools" tests share.

A plain module (not a conftest) for the same reason headless_rig.py is one: other test directories import from it.
It adds to the C10 rig (``headless_rig``: the fake server in a thread, ``run_cli``, ``call_headless``):

``steps_tail``      a server script that proposes tool calls one after the other, the way the real agent loop does,
                    and says what it expects back for each (a ``tool_result`` when the device ran it, an ``approval``
                    when the device refused it), then gives a final reply when every call was answered.
``network_guard``   a fixture that fails the test if anything connects anywhere but the loopback fake server.
``make_setup``      the ``tool_setup`` argument of ``run_headless`` for a project folder, built the way the CLI builds it.
``tool_results``    the ``tool_result`` payloads the server received, by call id.
``checkpoint_rows`` the rows of the store's ``checkpoints`` table, for one session.

No literal in this file looks like a credential; tests that need a fake secret build it at run time.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from headless_rig import *  # noqa: E402,F401,F403  (the autouse fixtures are re-exported on purpose)
from headless_rig import expect  # noqa: E402

TASK = "make the change"


def steps_tail(*steps, final: str = "All done."):
    """``steps`` are ``(call_id, tool, input, risk, expect_back)`` with ``expect_back`` one of
    ``"tool_result"`` (the device ran it) and ``"approval"`` (the device refused it).

    After a refusal the script stops proposing and waits, as the real server does: the device cancels the run.  When
    every call was answered by a result the script ends the turn with ``final``."""

    async def tail(conn, stream, _start, _user):
        await stream.text("working")
        await stream.done("tool_use")
        await stream.usage()
        for call_id, tool, tool_input, risk, back in steps:
            await stream.proposed(call_id, tool, tool_input, risk)
            message = await expect(conn, back)
            if back == "approval":
                return
            await stream.tool_finished(call_id, tool, "ok" if message["payload"].get("ok") else "error")
        await stream.reply(final)

    return tail


def tool_results(srv) -> dict[str, dict]:
    """The ``tool_result`` payloads the server received, keyed by call id."""
    return {m["payload"]["call_id"]: m["payload"] for m in srv.messages("tool_result")}


def make_setup(project, *, allow=(), deny=(), sandbox="auto", environ=None):
    """``run_headless(..., tool_setup=make_setup(project))``: the preparation the CLI does, for ``project``."""
    from m2c import session_setup

    def setup():
        return session_setup.headless_tool_setup(cwd=str(project), allow=tuple(allow), deny=tuple(deny),
                                                 sandbox=sandbox, environ=environ)

    return setup


def checkpoint_rows(session_id: str) -> list[tuple]:
    """``(step, path, blob_hash)`` rows of the checkpoints table for one session, from the (hermetic) store."""
    from m2c.store import open_db

    conn = open_db()
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT step, path, blob_hash FROM checkpoints WHERE session_id = ? ORDER BY step, path", (session_id,))]
    finally:
        conn.close()


@pytest.fixture
def network_guard(monkeypatch):
    """Any connection to an address that is not loopback fails the test: the run needs no network but the fake server."""
    attempts: list[str] = []
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else None
        if host is not None and host not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(str(host))
            raise OSError("network access is not allowed in this test")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield attempts
    assert attempts == [], f"something tried to reach the network: {attempts}"
