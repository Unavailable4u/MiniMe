"""
m2c/tests/link/test_link_shutdown.py -- Patch C04: graceful shutdown.

``aclose()`` must close the socket properly, cancel every task the link
started, settle every waiter, and leave the store consistent: nothing
half-written, and the in-memory cursor equal to what the log says.
"""
from __future__ import annotations

import pytest

from link_fakes import *  # noqa: F401,F403  (autouse fixtures; see link_fakes.py)

from m2c.link import LinkClosedError, recover_last_seq

RUN = "run_shutdown_001"


def start_kwargs():
    return dict(mode="ask", cwd_label="demo-project", instructions="")


def test_aclose_sends_a_normal_close_frame_and_leaves_no_tasks_behind():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv, keepalive_interval_s=0.05)
            link.start()
            await link.wait_connected(5)
            await asyncio.sleep(0.12)                                 # let the keepalive task run
            await link.aclose()
            await asyncio.wait_for(srv.connections[0].closed.wait(), 3)
            return srv, link

    srv, link = run_async(scenario())
    assert srv.connections[0].close_code == 1000
    assert link.state == "closed"
    assert link.active_tasks == 0           # the run loop, reader and keepalive are all finished


def test_aclose_is_idempotent_and_works_on_a_link_that_never_started():
    store = open_db()

    async def scenario():
        link = make_link(store, "http://127.0.0.1:9")
        await link.aclose()
        await link.aclose()
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            await link.aclose()
            await link.aclose()
            return link.state

    assert run_async(scenario()) == "closed"


def test_a_pending_request_is_settled_with_a_clear_error_when_the_link_closes():
    store = open_db()

    async def deaf(conn):
        await do_handshake(conn)
        while True:
            await conn.recv(timeout=None)

    async def scenario():
        async with FakeServer(deaf) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            waiting = asyncio.ensure_future(link.ping(timeout=30))
            await until(lambda: link.pending_count == 1)
            await link.aclose()
            with pytest.raises(LinkClosedError):
                await waiting
            return link.pending_count

    assert run_async(scenario()) == 0


def test_aclose_during_a_backoff_sleep_returns_promptly():
    store = open_db()

    async def scenario():
        async def refuse(*args, **kwargs):
            raise ConnectionRefusedError()

        entered = asyncio.Event()

        async def long_sleep(delay):
            entered.set()
            await asyncio.sleep(3600)

        link = make_link(store, "http://127.0.0.1:9", connector=refuse, sleep=long_sleep)
        link.start()
        await asyncio.wait_for(entered.wait(), 5)
        loop = asyncio.get_running_loop()
        began = loop.time()
        await link.aclose()
        return loop.time() - began, link.state

    took, state = run_async(scenario())
    assert took < 2 and state == "closed"


def test_aclose_mid_stream_leaves_a_consistent_log_that_matches_the_cursor():
    events = synthetic_events(RUN, 400)
    store = open_db()
    new_run(store, RUN)
    replay = ReplayRun(events, delay_s=0.002)

    async def scenario():
        async with FakeServer(replay.script) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, mode="ask", cwd_label="demo", instructions="")
            await until(lambda: link.run_info(RUN).last_seq >= 30)
            await link.aclose()
            return link, rec

    link, rec = run_async(scenario())
    seqs = stored_server_seqs(store, RUN)
    assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) >= 30        # contiguous, nothing partial
    assert link.run_info(RUN).last_seq == seqs[-1] == recover_last_seq(store, RUN)[0]
    assert rec.seqs(RUN) == seqs[: len(rec.seqs(RUN))]
    assert store.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [e.seq for e in read_events(store, RUN)] == list(range(1, len(seqs) + 1))


def test_cancelling_the_run_task_from_outside_is_clean_too():
    events = synthetic_events(RUN, 400)
    store = open_db()
    new_run(store, RUN)
    replay = ReplayRun(events, delay_s=0.002)

    async def scenario():
        async with FakeServer(replay.script) as srv:
            link = make_link(store, srv)
            task = asyncio.ensure_future(link.run())
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, mode="ask", cwd_label="demo", instructions="")
            await until(lambda: link.run_info(RUN).last_seq >= 20)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(srv.connections[0].closed.wait(), 3)
            return link

    link = run_async(scenario())
    seqs = stored_server_seqs(store, RUN)
    assert seqs == list(range(1, len(seqs) + 1))
    assert link.run_info(RUN).last_seq == seqs[-1]


def test_the_async_context_manager_starts_and_closes_the_link():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            async with make_link(store, srv) as link:
                await link.wait_connected(5)
                assert link.state == "connected"
            return link.state, srv

    state, srv = run_async(scenario())
    assert state == "closed" and srv.connections[0].close_code == 1000


def test_wait_closed_returns_quietly_after_a_normal_close():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            await link.aclose()
            await link.wait_closed()                                  # no exception: nothing went wrong
            return link.failure

    assert run_async(scenario()) is None
