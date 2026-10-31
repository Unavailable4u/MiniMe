"""
m2c/tests/headless/headless_rig.py -- Patch C10: the shared rig of the headless-mode tests.

Why this is a plain module and not a conftest.py: tests/test_doctor.py does ``from conftest import ...``,
which resolves by module name, and a second conftest.py in tests/headless would shadow the real one (the
same reason tests/auth/auth_fixtures.py and tests/link/link_fakes.py exist).  Each test file imports what
it needs from here.

WHAT IS REUSED.  The fake device server of C04 (tests/link/link_fakes.py): a real ``websockets`` server on
127.0.0.1 whose per-connection scripts decide exactly what the "server" says.  This module adds, on top of it:

    ThreadedServer     runs that fake server in a thread of its own, so a test can call the SYNCHRONOUS
                       ``cli.main([...])`` / ``headless.run_headless(...)`` (which start their own event
                       loop with ``asyncio.run``) against it, with the real DeviceLink in between.
    Stream / scripted  a tiny model of the server's side of one run: it numbers the stream (seq 1, 2, 3...)
                       and says ``created`` after run.start and ``running`` after the user_message, the
                       order protocol/fixtures/*.jsonl pin; a test supplies only what happens next.
    call_headless      ``run_headless`` with StringIO streams and fast link options.
    run_cli            ``cli.main`` with capsys, signed in against a ThreadedServer.

Every client message the fake server receives is validated against the client's own copy of the schemas
when ``jsonschema`` is installed (``server.assert_clean()``); CI's m2c job installs only ``.[dev]``, so that
check is skipped there, exactly as for the link tests.

Fake secrets are built at run time (this repo's gitleaks scan flags anything shaped like a key): see
``link_fakes.make_credential``.
"""
from __future__ import annotations

import asyncio
import io
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

# The fake server lives in tests/link; pytest only puts the test file's own directory on sys.path.
_LINK_TESTS = str(Path(__file__).resolve().parents[1] / "link")
if _LINK_TESTS not in sys.path:
    sys.path.insert(0, _LINK_TESTS)

from link_fakes import *  # noqa: E402,F401,F403  (the autouse fixtures are re-exported on purpose)
from link_fakes import FakeServer, do_handshake, env, error_env, make_credential, run_async  # noqa: E402

from m2c import cli, headless  # noqa: E402
from m2c.auth import credentials  # noqa: E402

PROJECT_NAME = "my-project"


# --------------------------------------------------------------------------
# Fixtures (re-exported into each test module with ``from headless_rig import *``)
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch):
    """The OS keyring is replaced by "not available": a developer's real keychain is never touched."""
    monkeypatch.setattr(credentials, "_load_keyring", lambda: None)


@pytest.fixture(autouse=True)
def _clean_m2c_logger():
    yield
    logger = logging.getLogger("m2c")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def _no_installed_tool_hooks():
    """Tool hooks are module state: every test starts, and ends, with none installed."""
    headless.clear_tool_hooks()
    yield
    headless.clear_tool_hooks()


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """The working directory of the run: a folder whose NAME becomes the run's cwd_label."""
    folder = tmp_path / PROJECT_NAME
    folder.mkdir()
    monkeypatch.chdir(folder)
    return folder


# --------------------------------------------------------------------------
# The fake server in a thread
# --------------------------------------------------------------------------
class ThreadedServer:
    """``with ThreadedServer(script1, script2) as srv:`` -- a FakeServer whose event loop runs in a thread,
    so the code under test can run its own ``asyncio.run`` in the test's thread.  Read ``srv.url`` while it
    runs; inspect ``srv.fake`` (messages, connections, errors) after the block."""

    def __init__(self, *scripts, repeat_last: bool = False) -> None:
        self._scripts = scripts
        self._repeat_last = repeat_last
        self.fake: FakeServer | None = None
        self.url: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    def __enter__(self) -> "ThreadedServer":
        ready = threading.Event()

        async def main() -> None:
            async with FakeServer(*self._scripts, repeat_last=self._repeat_last) as fake:
                self.fake = fake
                self.url = fake.server_url
                self._loop = asyncio.get_running_loop()
                self._stop = asyncio.Event()
                ready.set()
                await self._stop.wait()

        def target() -> None:
            try:
                asyncio.run(main())
            except BaseException as exc:  # noqa: BLE001 - reported by __enter__/__exit__
                self._failure = exc
                ready.set()

        self._thread = threading.Thread(target=target, name="fake-m2c-server", daemon=True)
        self._thread.start()
        if not ready.wait(10) or self._failure is not None:
            raise RuntimeError(f"the fake server did not start: {self._failure!r}")
        return self

    def __exit__(self, *_exc) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(15)

    # -- what the server saw (valid after the ``with`` block) ------------------------------------------
    def messages(self, t: str | None = None) -> list[dict]:
        return self.fake.messages(None, t)

    def types(self) -> list[str]:
        return [m["t"] for m in self.messages() if m["t"] != "ping"]

    def assert_clean(self) -> None:
        self.fake.assert_clean()


# --------------------------------------------------------------------------
# The server's side of one run
# --------------------------------------------------------------------------
class Stream:
    """Numbers and sends the server's stream for one run (protocol/README.md section 1: ``seq`` starts at 1
    and grows by exactly 1)."""

    def __init__(self, conn, run_id: str) -> None:
        self.conn = conn
        self.run_id = run_id
        self.seq = 0

    async def _stream(self, t: str, payload: dict) -> None:
        self.seq += 1
        await self.conn.send(env(t, payload, run_id=self.run_id, seq=self.seq))

    async def event(self, **payload) -> None:
        await self._stream("run_event", payload)

    async def state(self, state: str, reason: str | None = None) -> None:
        await self.event(type="state_changed", state=state, **({"reason": reason} if reason else {}))

    async def text(self, *chunks: str) -> None:
        for chunk in chunks:
            await self.event(type="text_delta", text=chunk)

    async def done(self, stop_reason: str | None = "end_turn") -> None:
        await self.event(type="text_done", **({"stop_reason": stop_reason} if stop_reason else {}))

    async def usage(self, *, model: str = "fake-model", input_tokens: int = 10, cached_input_tokens: int = 2,
                    output_tokens: int = 5, credits: float = 0.5) -> None:
        await self.event(type="usage", model=model, input_tokens=input_tokens,
                         cached_input_tokens=cached_input_tokens, output_tokens=output_tokens, credits=credits)

    async def notice(self, code: str, message: str = "fixed text", severity: str = "info") -> None:
        await self.event(type="notice", code=code, severity=severity, message=message)

    async def proposed(self, call_id: str, tool: str, input: dict, risk: str = "R0") -> None:
        await self._stream("tool_proposed", {"call_id": call_id, "tool": tool, "input": input, "risk": risk})

    async def tool_finished(self, call_id: str, tool: str, status: str = "ok") -> None:
        await self.event(type="tool_finished", call_id=call_id, tool=tool, status=status)

    async def budget_warning(self, spent_pct: int = 80, scope: str = "account", reset_at: str | None = None) -> None:
        payload: dict = {"scope": scope, "spent_pct": spent_pct}
        if reset_at:
            payload["reset_at"] = reset_at
        await self._stream("budget_warning", payload)

    async def error(self, code: str, *, message: str = "fixed text", details: dict | None = None,
                    ref_id: str | None = None) -> None:
        """An ``error`` message: it carries no seq (it is not part of the replayable stream)."""
        await self.conn.send(error_env(code, message=message, run_id=self.run_id, ref_id=ref_id, details=details))

    async def reply(self, *chunks: str, stop_reason: str | None = "end_turn", usage: bool = True) -> None:
        """One complete model reply: deltas, text_done, usage."""
        await self.text(*chunks)
        await self.done(stop_reason)
        if usage:
            await self.usage()


async def expect(conn, t: str, timeout: float = 8.0) -> dict:
    """The next client message, which must be of type ``t`` (pings are answered on the way)."""
    while True:
        message = await conn.recv(timeout=timeout)
        if message["t"] == "ping":
            await conn.send(env("pong", {} if "nonce" not in message["payload"] else {"nonce": message["payload"]["nonce"]}))
            continue
        assert message["t"] == t, f"expected {t}, the client sent {message['t']}"
        return message


async def idle(conn, stream: Stream) -> None:
    """Stay connected until the client leaves: answer pings, and answer a ``cancel`` with the ``cancelled`` state."""
    while True:
        message = await conn.recv(timeout=None)
        if message["t"] == "ping":
            await conn.send(env("pong", {} if "nonce" not in message["payload"] else {"nonce": message["payload"]["nonce"]}))
        elif message["t"] == "cancel":
            await stream.state("cancelled", "cancel")


def scripted(tail=None, *, started: asyncio.Event | None = None):
    """A server script for one connection: handshake, ``run.start`` -> ``created``, ``user_message`` ->
    ``running``, then ``await tail(conn, stream, run_start, user_message)``, then idle (answering a cancel).

    ``tail`` is an async function; ``None`` means "say nothing more"."""

    async def script(conn) -> None:
        await do_handshake(conn)
        run_start = await expect(conn, "run.start")
        stream = Stream(conn, run_start["run_id"])
        await stream.state("created")
        user_message = await expect(conn, "user_message")
        await stream.state("running")
        if tail is not None:
            await tail(conn, stream, run_start, user_message)
        if started is not None:
            started.set()
        await idle(conn, stream)

    return script


def replying(*chunks: str, **kwargs):
    """The ordinary script: one reply, then idle."""

    async def tail(_conn, stream, _start, _user) -> None:
        await stream.reply(*chunks, **kwargs)

    return scripted(tail)


# --------------------------------------------------------------------------
# Calling the code under test
# --------------------------------------------------------------------------
class Capture(io.StringIO):
    """An output stream that is not a terminal."""

    def isatty(self) -> bool:
        return False


@dataclass
class Outcome:
    code: int
    out: str
    err: str
    server: ThreadedServer | None = None

    def json(self) -> dict:
        """stdout parsed as the ONE JSON object json mode promises (and nothing else)."""
        import json

        assert self.out.endswith("\n") and self.out.count("\n") == 1, f"stdout is not exactly one line: {self.out!r}"
        return json.loads(self.out)


def fast_link_options(cred) -> dict:
    """Options for the real DeviceLink so that tests are quick and deterministic."""
    from m2c.link import BackoffPolicy

    return dict(
        credential_loader=lambda: cred,
        keepalive_interval_s=None,
        handshake_timeout_s=3.0,
        request_timeout_s=5.0,
        close_timeout_s=1.0,
        backoff=BackoffPolicy(base_s=0.01, factor=1.0, cap_s=0.01, stable_after_s=0.0),
    )


def call_headless(srv: ThreadedServer | None, task: str = "do the thing", *, mode: str = "ask",
                  output_format: str = "text", stdin=None, url: str | None = None, **kwargs) -> Outcome:
    """``headless.run_headless`` against ``srv`` with captured streams and fast, deterministic timing."""
    cred = make_credential(url or srv.url)
    out, err = Capture(), Capture()
    defaults = dict(
        stdout=out,
        stderr=err,
        stdin=stdin,
        credential_loader=lambda: cred,
        link_options=fast_link_options(cred),
        connect_timeout_s=5.0,
        settle_s=0.0,
        cancel_wait_s=2.0,
        reconnect_give_up_s=5.0,
        project_path=str(Path.cwd()),
    )
    defaults.update(kwargs)
    code = headless.run_headless(headless.HeadlessOptions(task=task, mode=mode, output_format=output_format), **defaults)
    return Outcome(code, out.getvalue(), err.getvalue(), srv)


# --------------------------------------------------------------------------
# A real child process (signals, "no terminal at all")
# --------------------------------------------------------------------------
# The child is the real ``m2c`` entry point with the OS keyring switched off (a developer's real keychain must
# never be touched, and a child cannot be monkeypatched from outside).
LAUNCHER = (
    "import sys\n"
    "from m2c.auth import credentials\n"
    "credentials._load_keyring = lambda: None\n"
    "from m2c import cli\n"
    "sys.exit(cli.main(sys.argv[1:]))\n"
)


def child_env(tmp_path: Path) -> dict[str, str]:
    """A minimal environment for the child: its own hermetic HOME and M2C_HOME, no proxies."""
    import os

    home = tmp_path / "child-home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "M2C_HOME": str(tmp_path / "child-m2c-home"),
        "PYTHONPATH": str(PKG_DIR.parent),
        "NO_PROXY": "*",
        "no_proxy": "*",
    }


def sign_in_for(env_vars: dict[str, str], url: str) -> None:
    """Store a device credential for ``url`` where the child will look for it."""
    credentials.save_credential(make_credential(url), environ=env_vars)


def sign_in(url: str) -> None:
    """Store a device credential for ``url`` in the (hermetic) M2C_HOME, as ``m2c login`` would."""
    credentials.save_credential(make_credential(url))


def run_cli(capsys, args: list[str], *scripts, signed_in: bool = True, repeat_last: bool = False) -> Outcome:
    """``cli.main(args)`` against a ThreadedServer with those scripts, signed in; the exit code and both streams."""
    with ThreadedServer(*scripts, repeat_last=repeat_last) as srv:
        if signed_in:
            sign_in(srv.url)
        code = cli.main(args)
    captured = capsys.readouterr()
    return Outcome(code, captured.out, captured.err, srv)


__all__ = [name for name in dir() if not name.startswith("__") and name not in {"annotations"}]
