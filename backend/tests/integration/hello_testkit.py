"""
tests/integration/hello_testkit.py -- Patch I01: the harness for the walking-skeleton tests.

One `Stack` joins the real pieces and fakes only what sits at a module boundary:

    real    codeagent.ws (S04) + router + run_handlers (S14) + emitter + hello (I01)
            run_manager / runs / buffer (S06) over their in-memory twins (World)
            the S05 adapter, talking HTTP to S05's own FakeProvider
            eo.credits (S02)
            m2c.link.DeviceLink (C04), m2c.repl (I01), the m2c local store (C02)
    fake    the database calls: devices (Rig's WsFakeStore), runs (FakeRunStore),
            the usage ledger (FakeLedger below), Redis (FakeRunRedis)

The device socket is served by uvicorn on 127.0.0.1 in a thread, so the genuine C04 client uses a
genuine TCP WebSocket (handshake, frames, abrupt connection loss). The client's source is
imported from the repo (m2c/src) because the backend's CI job does not pip-install m2c; its only
third-party dependency, `websockets`, is in the backend requirements.

Fake secrets are built at run time (gitleaks flags anything shaped like a key).
"""
import asyncio
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("jsonschema", reason="the device socket tests need jsonschema (protocol/requirements.txt)")
pytest.importorskip("uvicorn", reason="the walking-skeleton tests serve the app with uvicorn")
pytest.importorskip("websockets", reason="the walking-skeleton tests use the real C04 client")

REPO = Path(__file__).resolve().parents[3]
M2C_SRC = REPO / "m2c" / "src"
if str(M2C_SRC) not in sys.path:
    sys.path.insert(0, str(M2C_SRC))

import uvicorn  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

from codeagent import emitter as emitter_module  # noqa: E402
from codeagent import hello, router as router_module, run_handlers, run_manager, runs  # noqa: E402
from codeagent.adapters.openai_compat import OpenAICompatAdapter  # noqa: E402
from codeagent.emitter import RunEmitter  # noqa: E402
from codeagent.router import MessageRouter  # noqa: E402
from codeagent.run_handlers import RunHandlers  # noqa: E402
from eo import usage_ledger  # noqa: E402
from m2c import repl as m2c_repl  # noqa: E402
from m2c.auth.credentials import Credential, Secret  # noqa: E402
from m2c.link import BackoffPolicy, DeviceLink, LinkCallbacks  # noqa: E402
from m2c.store import open_db  # noqa: E402
from tests.unit.codeagent_testkit import Rig  # noqa: E402
from tests.unit.run_manager_testkit import World  # noqa: E402
from tests.unit.test_codeagent_adapter import FakeProvider, Stream, chunk, usage_chunk  # noqa: E402

USER = "user-hello-1"
PROVIDER = "groq"
MODEL = "openai/gpt-oss-120b"
USER_TEXT_CANARY = "CANARY-user-text-do-not-log-3e9a"
REPLY_TEXT_CANARY = "CANARY-reply-text-do-not-log-71c4"


def wait_for(predicate, timeout=15.0, what="condition"):
    """Poll from a plain thread (the server runs on its own loop)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def await_for(predicate, timeout=15.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class FakeLedger:
    """usage_ledger.check/commit with the pool in a list. `exhausted` makes check refuse;
    `fail_commit` makes commit raise (metering must fail open)."""

    def __init__(self):
        self.rows = []
        self.checks = []
        self.exhausted = False
        self.resets_at = "2026-11-01T00:00:00+00:00"
        self.fail_commit = False
        self._lock = threading.Lock()

    def check(self, user_id, estimate=0.0, now=None):
        with self._lock:
            self.checks.append(user_id)
        if self.exhausted:
            return usage_ledger.CheckResult(status="quota_exhausted", allowed=False, pool=10.0, used=10.0,
                                            remaining=0.0, resets_at=self.resets_at)
        return usage_ledger.CheckResult(status="ok", allowed=True, pool=10.0, used=1.0, remaining=9.0)

    def commit(self, **kw):
        if self.fail_commit:
            raise RuntimeError("ledger is down")
        with self._lock:
            self.rows.append(dict(kw))
        return {"credits": kw.get("credits")}


class LiveServer:
    """uvicorn on 127.0.0.1:<free port> in a thread."""

    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                                    lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, name="live-server", daemon=True)
        self.thread.start()
        wait_for(lambda: self.server.started, 15, "the server to start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(15)


class Stack:
    """`db_cursor`: when given (the real-database test), `eo.db.cursor` is replaced by it, the run store
    is the real PgRunStore and the ledger is the real usage_ledger; `user_id` is then a per-test user."""

    def __init__(self, monkeypatch, tmp_path, *, db_cursor=None, user_id=USER):
        self.mp = monkeypatch
        self.tmp = tmp_path
        self.user_id = user_id
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(key, "127.0.0.1,localhost")      # the sandbox proxy must not capture 127.0.0.1
        self.world = World(monkeypatch)
        run_manager._protocol()                                  # skips via ProtocolSchemasUnavailable otherwise
        self.rig = Rig(monkeypatch, revalidate_interval_s=0.2)

        # a fresh emitter, handlers and router, installed the way server.py does it
        self.emitter = RunEmitter()
        monkeypatch.setattr(emitter_module, "_EMITTER", self.emitter)
        self.handlers = RunHandlers(emitter=self.emitter, max_parallel=10, hook_timeout=5.0, stop_timeout=5.0,
                                    purge_delays=(0.01,))
        monkeypatch.setattr(run_handlers, "_HANDLERS", self.handlers)
        self.router = MessageRouter()
        monkeypatch.setattr(router_module, "_ROUTER", self.router)
        self.handlers.register_on(self.router)
        self.rig.use_handler(self.router)
        hello.install()

        self.ledger = FakeLedger()
        if db_cursor is None:
            monkeypatch.setattr(usage_ledger, "check", self.ledger.check)
            monkeypatch.setattr(usage_ledger, "commit", self.ledger.commit)
        else:
            from eo import db as eo_db, plans
            monkeypatch.setattr(eo_db, "cursor", db_cursor)
            monkeypatch.setattr(runs, "_STORE", runs.PgRunStore())
            plans.reload_config()

        self.provider = FakeProvider()
        self.adapter = OpenAICompatAdapter.single(MODEL, client=self.provider.client(), provider=PROVIDER)
        monkeypatch.setattr(hello, "_ADAPTER", self.adapter)

        self.credential_text = "m2c_test_cred_" + "x" * 30
        _, self.device_id = self.rig.store.add_device(user_id=user_id, credential=self.credential_text)
        self.server = LiveServer(self.rig.app)
        self.cwd = tmp_path / "my-project"
        self.cwd.mkdir()

    def close(self):
        self.server.stop()
        self.provider.close()
        self.world.teardown()

    # -- the client side ------------------------------------------------
    def credential(self):
        return Credential(device_credential=Secret(self.credential_text), device_id=self.device_id,
                          server_url=self.server.url)

    def open_store(self, name="home"):
        return open_db({"M2C_HOME": str(self.tmp / name)})

    def revoke_device(self):
        with self.rig.store.lock:
            self.rig.store.devices[self.device_id]["revoked_at"] = self.rig.store_clock.t

    # -- server-side facts, read from the test thread ---------------------
    def run_row(self, run_id):
        return runs.get_run(self.user_id, run_id)

    def idle(self):
        """No turn, no restarted turn, no tracked lock holder left on the server."""
        return self.handlers.turn_runs() == () and hello._RESTARTS == {}


class Session:
    """One REPL on one real DeviceLink, with scripted input. Use inside `async with`."""

    def __init__(self, stack, *, store_name="home", project_path=None):
        self.stack = stack
        self.store = stack.open_store(store_name)
        self.out = []
        self.lines = queue.Queue()
        self.conns = []
        self.repl = m2c_repl.Repl(self.store, project_path=str(project_path or stack.cwd), read_line=self._read,
                                  write=self.out.append, install_sigint=False)
        self.errors = []
        inner = self.repl.callbacks()

        def on_error(error):
            self.errors.append(error)
            inner.on_error(error)

        self.link = DeviceLink(
            self.store, credential_loader=stack.credential,
            callbacks=LinkCallbacks(on_event=inner.on_event, on_error=on_error, on_state=inner.on_state),
            connector=self._connector, backoff=BackoffPolicy(base_s=0.05, cap_s=0.2, stable_after_s=0.1),
            keepalive_interval_s=None)
        self.task = None
        self.status = None
        # The link reconnects within ~50 ms of a drop, so a test could never reliably observe the
        # paused_disconnected state in between. drop_connection() therefore HOLDS the reconnect until
        # allow_reconnect() is called: the pause is then a stable state the test can inspect.
        self._may_connect = asyncio.Event()
        self._may_connect.set()

    def _read(self, prompt):
        item = self.lines.get()
        if item is EOFError:
            raise EOFError
        return item

    def _connector(self, url, **options):
        async def go():
            await self._may_connect.wait()
            conn = await connect(url, **options)
            self.conns.append(conn)
            return conn
        return go()

    async def __aenter__(self):
        self.link.start()
        await self.link.wait_connected(15)
        self.task = asyncio.create_task(self.repl.run(self.link))
        return self

    async def __aexit__(self, *exc):
        if self.task is not None and not self.task.done():
            self.lines.put("/exit")
            try:
                await asyncio.wait_for(self.task, 20)
            except (asyncio.TimeoutError, Exception):
                self.task.cancel()
        await self.link.aclose()
        self.store.close()

    @property
    def text(self):
        return "".join(self.out)

    def say(self, line):
        self.lines.put(line)

    async def until(self, predicate, timeout=15.0, what="condition"):
        await await_for(predicate, timeout, what)

    async def reply_count(self, n):
        await self.until(lambda: len(self.repl.replies) >= n and self.repl.idle,
                         what=f"{n} complete repl(y/ies) and the turn's last event; saw {self.text!r}")

    async def leave(self):
        self.say("/exit")
        self.status = await asyncio.wait_for(self.task, 20)
        return self.status

    def drop_connection(self):
        """Abruptly lose the TCP connection (a laptop lid closing, wifi going away). The reconnect is held
        until allow_reconnect()."""
        self._may_connect.clear()
        self.conns[-1].transport.abort()

    def allow_reconnect(self):
        self._may_connect.set()


def blocking_reply(first, gate, started, *, prompt=30, completion=5):
    """A reply that sends `first` (a list of text parts), tells the test (`started`), then stalls on `gate`
    and, once released, writes many small chunks (so a hung-up client is noticed by the fake server)."""
    return Stream([chunk("", finish=None), *[chunk(p) for p in first], ("set", started), ("wait", gate),
                   *[chunk("x") for _ in range(30)], chunk(finish="stop"), usage_chunk(prompt, completion)])


def new_stack(monkeypatch, tmp_path, **kw):
    return Stack(monkeypatch, tmp_path, **kw)


@pytest.fixture
def stack(monkeypatch, tmp_path):
    s = new_stack(monkeypatch, tmp_path)
    try:
        yield s
    finally:
        s.close()


def local_events(store, run_id):
    from m2c.store import read_events
    return list(read_events(store, run_id))


def server_events(run_id):
    from codeagent import buffer
    return buffer.replay_after(run_id, 0)
