"""
m2c/tests/test_repl.py -- Patch I01: the prompt loop, against a fake DeviceLink.

The real link is exercised end to end by the backend's tests/integration/test_hello_run_e2e.py;
here the link is a scripted fake so each REPL rule can be checked alone: what is printed, what is
stored, what is sent, and when the turn is over. Fake secrets and ids are built at run time.
"""
from __future__ import annotations

import asyncio
import functools
import os
import queue
import signal
import sys
import threading
import uuid

import pytest

from m2c import repl as repl_module
from m2c.link import LinkCallbacks, LinkDeviceRevokedError, LinkEvent, ServerError
from m2c.store import create_session, list_sessions, open_db, read_events


def run(coro):
    return asyncio.run(coro)


class FakeLink:
    """Records what the REPL sends; the test plays the server by calling `emit`/`fail` on the callbacks."""

    def __init__(self, store):
        self.store = store
        self.cb: LinkCallbacks | None = None
        self.started: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.failure = None
        self.state = "connected"
        self.confirm_cancel = True
        self._seq: dict[str, int] = {}

    async def start_run(self, **kw):
        self.started.append(kw)
        session = create_session(self.store, project_path=kw["project_path"], mode=kw["mode"])
        return session.id

    async def send_user_message(self, run_id, text):
        self.sent.append((run_id, text))

    async def send_cancel(self, run_id):
        self.cancelled.append(run_id)
        if self.confirm_cancel:
            asyncio.get_running_loop().call_soon(functools.partial(self.emit, run_id, "state_changed", state="cancelled", reason="cancel"))

    def emit(self, run_id, subtype, **payload):
        self._seq[run_id] = self._seq.get(run_id, 0) + 1
        self.cb.on_event(LinkEvent(run_id=run_id, seq=self._seq[run_id], id=str(uuid.uuid4()), ts="2026-10-28T00:00:00Z",
                                   type="run_event", payload={"type": subtype, **payload}))

    def error(self, code, run_id=None, **details):
        self.cb.on_error(ServerError(code=code, run_id=run_id, details=details or None))


class Rig:
    def __init__(self, tmp_path, lines=()):
        self.store = open_db({"M2C_HOME": str(tmp_path / "home")})
        self.out: list[str] = []
        self.lines: queue.Queue = queue.Queue()
        for line in lines:
            self.lines.put(line)
        self.repl = repl_module.Repl(self.store, project_path=str(tmp_path / "proj"), read_line=self._read,
                                     write=self.out.append, install_sigint=False)
        self.link = FakeLink(self.store)
        self.link.cb = self.repl.callbacks()

    def _read(self, prompt):
        item = self.lines.get()
        if item is EOFError:
            raise EOFError
        return item

    @property
    def text(self):
        return "".join(self.out)

    def say(self, line):
        self.lines.put(line)

    def finish_reply(self, run_id, text="ok", model="m"):
        self.link.emit(run_id, "text_delta", text=text)
        self.link.emit(run_id, "text_done", stop_reason="end_turn")
        self.link.emit(run_id, "usage", model=model, input_tokens=5, cached_input_tokens=1, output_tokens=2, credits=0.5)


async def until(pred, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out")


@pytest.fixture
def rig(tmp_path):
    (tmp_path / "proj").mkdir()
    r = Rig(tmp_path)
    yield r
    r.store.close()


# ---- the label ----------------------------------------------------------------------------------

@pytest.mark.parametrize("path,label", [("/home/me/my-project", "my-project"), ("/", "project"), ("", "project"),
                                        ("/a/" + "x" * 300, "x" * 128), ("/a/b/..", "project")])
def test_cwd_label_is_a_name_never_a_path(path, label):
    got = repl_module.cwd_label_for(path)
    assert got == label and "/" not in got and "\\" not in got


# ---- a conversation --------------------------------------------------------------------------------

def test_first_message_starts_a_run_then_streams_and_stores_the_reply(rig):
    async def scenario():
        rig.say("hello")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        (kw,) = rig.link.started
        assert (kw["profile"], kw["mode"], kw["cwd_label"]) == ("code", "ask", "proj")
        assert kw["project_path"].endswith("proj")
        run_id = rig.link.sent[0][0]
        rig.link.emit(run_id, "text_delta", text="Hel")
        rig.link.emit(run_id, "text_delta", text="lo")
        assert rig.text.endswith("Hello")                     # printed as it streams
        rig.link.emit(run_id, "text_done", stop_reason="end_turn")
        rig.link.emit(run_id, "usage", model="m1", input_tokens=5, cached_input_tokens=1, output_tokens=2, credits=0.5)
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        assert await task == 0
        return run_id

    run_id = run(scenario())
    assert rig.repl.replies == ["Hello"]
    assert "[m1 | in 5 (cached 1) | out 2 | 0.5 credits]" in rig.text
    events = list(read_events(rig.store, run_id))
    assert [e.type for e in events] == ["user.message", "assistant.message"]
    assert events[0].data == {"text": "hello"} and events[1].data == {"text": "Hello"}


def test_second_message_reuses_the_run(rig):
    async def scenario():
        rig.say("one")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: len(rig.link.sent) == 1)
        run_id = rig.link.sent[0][0]
        rig.finish_reply(run_id, "A")
        await until(lambda: rig.repl.idle)
        rig.say("two")
        await until(lambda: len(rig.link.sent) == 2)
        assert rig.link.sent[1][0] == run_id and len(rig.link.started) == 1
        rig.finish_reply(run_id, "B")
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        await task

    run(scenario())
    assert rig.repl.replies == ["A", "B"]


def test_empty_lines_and_unknown_slash_commands_send_nothing(rig):
    async def scenario():
        for line in ("", "   ", "/help"):
            rig.say(line)
        rig.say("/exit")
        assert await rig.repl.run(rig.link) == 0

    run(scenario())
    assert rig.link.sent == [] and rig.link.started == [] and "Unknown command" in rig.text


# ---- reply_restarted ----------------------------------------------------------------------------------

def test_reply_restarted_discards_the_partial_and_stores_the_final_reply_once(rig):
    async def scenario():
        rig.say("hi")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        run_id = rig.link.sent[0][0]
        rig.link.emit(run_id, "text_delta", text="Par")
        rig.link.emit(run_id, "text_delta", text="tial")
        rig.link.emit(run_id, "state_changed", state="paused_disconnected", reason="disconnect")   # not an end
        assert not rig.repl.idle
        rig.link.emit(run_id, "state_changed", state="running", reason="resumed")
        rig.link.emit(run_id, "notice", code="reply_restarted", severity="info", message="fixed text")
        assert "being written again" in rig.text and rig.repl.restarts == 1
        rig.link.emit(run_id, "text_delta", text="Whole reply")
        rig.link.emit(run_id, "text_done", stop_reason="end_turn")
        rig.link.emit(run_id, "usage", model="m", input_tokens=1, cached_input_tokens=0, output_tokens=1, credits=0)
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        await task
        return run_id

    run_id = run(scenario())
    assert rig.repl.replies == ["Whole reply"]                          # the partial is not part of the reply
    assert [e.data["text"] for e in read_events(rig.store, run_id) if e.type == "assistant.message"] == ["Whole reply"]


def test_other_notices_are_shown(rig):
    async def scenario():
        rig.say("hi")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        run_id = rig.link.sent[0][0]
        rig.link.emit(run_id, "notice", code="something_else", severity="warning", message="Careful")
        rig.finish_reply(run_id)
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        await task

    run(scenario())
    assert "[warning] Careful" in rig.text and rig.repl.restarts == 0


# ---- leaving ----------------------------------------------------------------------------------------------

def test_exit_sends_cancel_for_the_open_run_and_waits_for_the_confirmation(rig):
    async def scenario():
        rig.say("hi")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        run_id = rig.link.sent[0][0]
        rig.finish_reply(run_id)
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await task == 0
        assert loop.time() - started < repl_module.CANCEL_WAIT_S / 2     # the confirmation ended the wait early
        return run_id

    run_id = run(scenario())
    assert rig.link.cancelled == [run_id]


def test_exit_with_no_run_sends_no_cancel(rig):
    rig.say("/exit")
    assert run(rig.repl.run(rig.link)) == 0
    assert rig.link.cancelled == []


def test_end_of_input_leaves_like_exit(rig):
    async def scenario():
        rig.say("hi")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        rig.finish_reply(rig.link.sent[0][0])
        await until(lambda: rig.repl.idle)
        rig.say(EOFError)
        return await task

    assert run(scenario()) == 0 and len(rig.link.cancelled) == 1


def test_a_cancel_the_server_never_confirms_does_not_hang_the_exit(rig, monkeypatch):
    monkeypatch.setattr(repl_module, "CANCEL_WAIT_S", 0.1)
    rig.link.confirm_cancel = False

    async def scenario():
        rig.say("hi")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        rig.finish_reply(rig.link.sent[0][0])
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        return await asyncio.wait_for(task, 5)

    assert run(scenario()) == 0


# ---- Ctrl-C ---------------------------------------------------------------------------------------------------

def test_ctrl_c_while_streaming_cancels_and_the_next_message_starts_a_new_run(rig):
    async def scenario():
        rig.say("first")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        first = rig.link.sent[0][0]
        rig.link.emit(first, "text_delta", text="Thinking")
        rig.repl.interrupt()
        await until(lambda: rig.link.cancelled)
        await until(lambda: rig.repl.run_id is None)
        assert "your next message starts a new one" in rig.text
        rig.say("second")
        await until(lambda: len(rig.link.sent) == 2)
        second = rig.link.sent[1][0]
        assert second != first and len(rig.link.started) == 2           # a NEW run
        rig.link.emit(first, "text_delta", text="stale")                # a late event of the old run is ignored
        rig.finish_reply(second, "Fresh")
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        await task
        return first, second

    first, second = run(scenario())
    assert rig.repl.replies == ["Fresh"] and rig.link.cancelled == [first, second]
    assert "stale" not in rig.text and len(list_sessions(rig.store)) == 2


def test_ctrl_c_at_the_prompt_leaves(rig):
    async def scenario():
        task = asyncio.create_task(rig.repl.run(rig.link))
        await asyncio.sleep(0.05)
        rig.repl.interrupt()
        return await asyncio.wait_for(task, 5)

    assert run(scenario()) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_a_real_sigint_is_routed_to_the_repl_not_to_python(tmp_path):
    (tmp_path / "proj").mkdir()
    r = Rig(tmp_path)
    r.repl = repl_module.Repl(r.store, project_path=str(tmp_path / "proj"), read_line=r._read, write=r.out.append)
    r.link.cb = r.repl.callbacks()
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers need the main thread")

    async def scenario():
        r.say("first")
        task = asyncio.create_task(r.repl.run(r.link))
        await until(lambda: r.link.sent)
        assert r.repl.sigint_installed
        r.link.emit(r.link.sent[0][0], "text_delta", text="x")
        os.kill(os.getpid(), signal.SIGINT)                           # the real thing; no KeyboardInterrupt
        await until(lambda: r.link.cancelled)
        r.say("/exit")
        await task
        assert not r.repl.sigint_installed                            # the handler is removed on the way out

    try:
        run(scenario())
    finally:
        r.store.close()


# ---- errors and pauses ---------------------------------------------------------------------------------------------

def test_quota_exhausted_shows_the_reset_time_and_ends_the_parked_run(rig, monkeypatch):
    monkeypatch.setattr(repl_module, "ERROR_STATE_GRACE_S", 5.0)

    async def scenario():
        rig.say("hi")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        run_id = rig.link.sent[0][0]
        rig.link.error("quota_exhausted", run_id=run_id, reset_at="2026-11-01T00:00:00+00:00")
        assert not rig.repl.idle                                      # the turn ends on the state event that follows
        rig.link.emit(run_id, "state_changed", state="paused_budget", reason="quota_exhausted")
        await until(lambda: rig.link.cancelled)                       # a paused run takes no more messages
        assert "credit pool is used up" in rig.text and "2026-11-01" in rig.text
        await until(lambda: rig.repl.run_id is None)
        rig.say("again")
        await until(lambda: len(rig.link.started) == 2)               # the next message is a new run
        await until(lambda: len(rig.link.sent) == 2)
        rig.finish_reply(rig.link.sent[1][0], "second run")
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        await task

    run(scenario())


def test_if_the_state_event_never_follows_the_error_the_prompt_comes_back(rig, monkeypatch):
    monkeypatch.setattr(repl_module, "ERROR_STATE_GRACE_S", 0.05)

    async def scenario():
        rig.say("hi")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        rig.link.error("provider_unavailable", run_id=rig.link.sent[0][0])
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        await task

    run(scenario())
    assert "No model could answer" in rig.text


def test_errors_for_other_runs_and_unknown_codes_are_handled(rig):
    async def scenario():
        rig.say("hi")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        run_id = rig.link.sent[0][0]
        rig.link.error("quota_exhausted", run_id=str(uuid.uuid4()))      # someone else's run: ignored
        assert rig.repl.last_error is None
        rig.link.error("internal", run_id=run_id)
        assert "Something went wrong on the server." in rig.text
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        await task

    run(scenario())


def test_a_dead_link_ends_the_loop_with_the_revoked_status(rig):
    async def scenario():
        task = asyncio.create_task(rig.repl.run(rig.link))
        await asyncio.sleep(0.05)
        rig.link.failure = LinkDeviceRevokedError("revoked")
        rig.link.state = "failed"
        rig.link.cb.on_state("failed")
        return await asyncio.wait_for(task, 5)

    assert run(scenario()) == 4 and "revoked" in rig.text


def test_a_link_that_is_already_dead_does_not_prompt(rig):
    rig.link.failure = LinkDeviceRevokedError("revoked")
    rig.link.state = "closed"
    assert run(rig.repl.run(rig.link)) == 4 and rig.link.sent == []


def test_a_local_log_failure_does_not_kill_the_prompt(rig, monkeypatch):
    monkeypatch.setattr(repl_module, "append_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))

    async def scenario():
        rig.say("hi")
        task = asyncio.create_task(rig.repl.run(rig.link))
        await until(lambda: rig.link.sent)
        rig.finish_reply(rig.link.sent[0][0], "fine")
        await until(lambda: rig.repl.idle)
        rig.say("/exit")
        await task

    run(scenario())
    assert rig.repl.replies == ["fine"] and "could not write to the local log" in rig.text
