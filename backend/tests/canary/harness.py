"""
backend/tests/canary/harness.py -- Patch Q06: one scripted session of the REAL server and the REAL device, in process,
with a recorder at every place content could come to rest.

WHAT IS REAL AND WHAT IS FAKE
-----------------------------
Real, on the server: the device socket (codeagent/ws.py) served by uvicorn on 127.0.0.1, the message router, the run
handlers, the emitter, the tool broker, the agent loop (codeagent/loop.py) wired in by codeagent/wiring.py as
api/server.py wires it, the budgeted context assembler, metering, the run manager, the run store (see MODES) and the
encrypted run buffer (codeagent/buffer.py: real AES-GCM, real key derivation).

Real, on the device: the genuine C04 DeviceLink over a genuine TCP WebSocket, the tool runner, the policy engine, the
file and shell tools (real `bash` on a temporary project), checkpoints, the instruction loader (M2C.md goes into
run.start), the local SQLite store, and the terminal chat's session controller (m2c/tui/session.py) with a fake screen.

Fake: the model (loop_testkit.ScriptedAdapter: it streams text and proposes tool calls from a script), Redis (the repo's
in-memory twins, each wrapped so that every command is recorded), the person (an auto-approver of the approval prompts)
and, in fake mode only, Postgres (see MODES).

MODES (the two the repo's other database tests use; MINIME_TEST_DATABASE_URL decides)
-------------------------------------------------------------------------------------
  fake  No database URL.  The run store and the usage ledger are the repo's in-memory twins, wrapped by recorders that
        keep every call with its arguments and what it returned (those arguments are exactly what the real stores bind
        into their SQL), and `eo.db.cursor` is replaced by a TRAP: any SQL that reaches it from anywhere else is
        recorded with its statement and parameters, and the tests fail because a path the twins do not cover touched
        Postgres.  Scanned: all of that, plus the twins' own tables (runs, devices, the ledger).
  real  MINIME_TEST_DATABASE_URL is set (the unprivileged app role `minime_app`, a scratch database migrated through
        0014, as in the root README).  The run store is the real PgRunStore, the ledger is the real usage_ledger, every
        statement is recorded on its way to the database, and afterwards EVERY table of the database is scanned through
        information_schema on a second connection, MINIME_TEST_SCAN_DATABASE_URL (the table owner: the app role is held
        back by row-level security and would see none of the other users' rows, so a scan on it would be blind; the
        scan refuses to run if it would be).

Every canary and every fake secret here is made at run time (gitleaks flags literals shaped like keys).
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Fail closed: a missing dependency is an ERROR here, never a skip. A security check that skips itself when its
# tools are absent would let CI go green without having looked (backend/requirements.txt pins them; uvicorn and
# websockets are imported for real below, which fails the same way).
import jsonschema  # noqa: E402,F401  (the device socket validates every frame against protocol/v1)

REPO = Path(__file__).resolve().parents[3]
M2C_SRC = REPO / "m2c" / "src"
if str(M2C_SRC) not in sys.path:                       # CI installs m2c; a plain checkout works too
    sys.path.insert(0, str(M2C_SRC))

import uvicorn  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

from codeagent import broker as broker_module, buffer, emitter as emitter_module  # noqa: E402
from codeagent import loop as loop_module, metering, router as router_module  # noqa: E402
from codeagent import run_handlers, run_manager, runs, wiring  # noqa: E402
from codeagent.emitter import RunEmitter  # noqa: E402
from codeagent.loop import LoopConfig  # noqa: E402
from codeagent.router import MessageRouter  # noqa: E402
from codeagent.run_handlers import RunHandlers  # noqa: E402
from eo import db as eo_db, plans, run_guard, usage_ledger  # noqa: E402
from m2c.auth.credentials import Credential, Secret  # noqa: E402
from m2c.link import BackoffPolicy  # noqa: E402
from m2c.logging_setup import configure_logging  # noqa: E402
from m2c.store import open_db  # noqa: E402
from m2c.tui.session import Session  # noqa: E402
from tests.canary import scanners  # noqa: E402
from tests.unit.buffer_testkit import FakeRunRedis  # noqa: E402
from tests.unit.codeagent_testkit import Rig  # noqa: E402
from tests.unit.loop_testkit import HANG, STRONG, FakeBudget, Reply, call  # noqa: E402,F401
from tests.unit.loop_testkit import ScriptedAdapter  # noqa: E402
from tests.unit.metering_testkit import FakeLedger, priced  # noqa: E402
from tests.unit.run_manager_testkit import USER, FakeGuardRedis, World  # noqa: E402

DSN = os.environ.get("MINIME_TEST_DATABASE_URL")
SCAN_DSN = os.environ.get("MINIME_TEST_SCAN_DATABASE_URL") or DSN
MODE = "real" if DSN else "fake"

SERVER_NAMESPACES = ("codeagent", "eo", "api", "utils", "memory", "agents", "uvicorn", "websockets")


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

async def until(predicate, timeout=20.0, what="the condition"):
    end = time.monotonic() + timeout
    while True:
        if predicate():
            return
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def _flat(value: Any) -> Any:
    """A recordable copy of a call's argument or result (rows and results become dicts of plain values)."""
    if isinstance(value, dict):
        return {str(k): _flat(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_flat(v) for v in value]
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _flat(vars(value))
    if hasattr(value, "_asdict"):
        return _flat(value._asdict())
    return value if isinstance(value, (str, bytes, int, float, bool, type(None))) else str(value)


class StatementLog:
    """Every SQL statement (or store call, in fake mode) with its parameters; thread safe (the server runs on its own
    thread and loop)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[dict] = []

    def add(self, sql: Any, params: Any = None, **extra: Any) -> None:
        with self._lock:
            self.items.append({"sql": str(sql), "params": _flat(params), **{k: _flat(v) for k, v in extra.items()}})

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.items)


# ---------------------------------------------------------------------------------------------
# Redis recorders
# ---------------------------------------------------------------------------------------------

class RecordingRunRedis(FakeRunRedis):
    """The run buffer's Redis twin, plus a log of every command with its arguments (the twin only keeps names and
    sizes).  A value that was EVER written in plaintext is therefore visible even if it was deleted a moment later."""

    def __init__(self, clock=None) -> None:
        super().__init__(clock)
        self._log_lock = threading.Lock()
        self.commands: list[tuple] = []

    def _request(self, commands):
        with self._log_lock:
            self.commands.extend(tuple(str(a) for a in c) for c in commands)
        return super()._request(commands)

    def written_strings(self) -> list[str]:
        with self._log_lock:
            return [a for c in self.commands for a in c]


class RecordingGuardRedis(FakeGuardRedis):
    """run_guard's and the metering store's Redis twin (strings and counters) with a log of every write."""

    def __init__(self) -> None:
        super().__init__()
        self._log_lock = threading.Lock()
        self.written: list[tuple] = []

    def set(self, key, value, ex=None):
        with self._log_lock:
            self.written.append(("set", str(key), str(value)))
        return super().set(key, value, ex=ex)

    def incrby(self, key, n):
        with self._log_lock:
            self.written.append(("incrby", str(key), str(n)))
        return super().incrby(key, n)

    def all_strings(self) -> list[str]:
        return [str(k) for k in self.data] + [str(v) for v in self.data.values()]

    def written_strings(self) -> list[str]:
        with self._log_lock:
            return [str(a) for w in self.written for a in w]


# ---------------------------------------------------------------------------------------------
# Postgres recorders
# ---------------------------------------------------------------------------------------------

class RecordingRunStore:
    """The in-memory run store, recorded: each call with its arguments and what it returned."""

    def __init__(self, inner, log: StatementLog) -> None:
        self._inner, self._log = inner, log

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if not callable(target) or name.startswith("_"):
            return target

        def recorded(*args, **kwargs):
            result = target(*args, **kwargs)
            self._log.add(f"runs.{name}", {"args": args, "kwargs": kwargs}, returned=result)
            return result
        return recorded


class RecordingLedger:
    """metering_testkit.FakeLedger, recorded."""

    def __init__(self, inner: FakeLedger, log: StatementLog) -> None:
        self.inner, self._log = inner, log

    def check(self, user_id):
        result = self.inner.check(user_id)
        self._log.add("usage_ledger.check", {"user_id": user_id}, returned=result)
        return result

    def commit(self, **row):
        result = self.inner.commit(**row)
        self._log.add("usage_ledger.commit", row, returned=result)
        return result

    def run_credits(self, user_id, run_id):
        result = self.inner.run_credits(user_id, run_id)
        self._log.add("usage_ledger.run_credits", {"user_id": user_id, "run_id": run_id}, returned=result)
        return result


class _TrapCursor:
    def __init__(self, log: StatementLog) -> None:
        self._log, self.rowcount = log, 0

    def execute(self, sql, params=None, *a, **k):
        self._log.add(sql, params, trap=True)
        return self

    executemany = execute

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, *a):
        return []

    def __iter__(self):
        return iter(())


class _RecordingCursor:
    """A real cursor whose execute() records the statement and its parameters first."""

    def __init__(self, cur, log: StatementLog) -> None:
        self._cur, self._log = cur, log

    def execute(self, sql, params=None, *a, **k):
        self._log.add(sql, params)
        return self._cur.execute(sql, params, *a, **k)

    def executemany(self, sql, seq, *a, **k):
        seq = list(seq)
        self._log.add(sql, seq)
        return self._cur.executemany(sql, seq, *a, **k)

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        return iter(self._cur)


# ---------------------------------------------------------------------------------------------
# Logs and error reports
# ---------------------------------------------------------------------------------------------

class LogCollector(logging.Handler):
    """Every record of the process, as everything a log line could carry (see scanners.record_text)."""

    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self._lock = threading.Lock()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = scanners.record_text(record)
        except Exception:                                  # never lose a record because it formats badly
            text = str(record.msg)
        with self._lock:
            self.lines.append(text)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)


class SentryCapture:
    """The error-reporting SDK, initialised the way api/server.py initialises it (FastApiIntegration, the default
    integrations, every transaction sampled: stricter than production's 10%), with a transport that keeps every
    envelope instead of sending it.  `payloads()` is everything that would have left the process."""

    def __init__(self) -> None:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.transport import Transport

        capture = self
        self.envelopes: list[bytes] = []
        self.events: list[Any] = []
        self.error_events = 0              # items of type "event": what an error report is (not transactions, sessions)

        class _Transport(Transport):
            def capture_envelope(self, envelope):
                capture.envelopes.append(envelope.serialize())
                for item in envelope.items:
                    capture.error_events += item.type == "event"
                    capture.events.append(item.payload.json if item.payload.json is not None else
                                          item.payload.get_bytes())

        self._sdk = sentry_sdk
        dsn = "https://" + "k" * 32 + "@o0.ingest.invalid/1"           # built, not a literal; never contacted
        sentry_sdk.init(dsn=dsn, environment="canary-test", integrations=[FastApiIntegration()],
                        traces_sample_rate=1.0, transport=_Transport())

    def payloads(self) -> list[Any]:
        self._sdk.flush(timeout=2.0)
        return list(self.envelopes) + list(self.events)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._sdk.get_client().close(timeout=1.0)
        self._sdk.init()                                              # back to the disabled client


# ---------------------------------------------------------------------------------------------
# The canaries of one scenario, and the project they are planted in
# ---------------------------------------------------------------------------------------------

@dataclass
class Canaries:
    """(a) prompt, (b) file_text, (c) tool_output, (d) reply, (e) path and command (tool arguments), edit (an argument
    carrying content), (f) instructions.  Fresh random values for every scenario."""

    prompt: scanners.Canary = field(default_factory=lambda: scanners.make_canary("prompt", awkward=True))
    file_text: scanners.Canary = field(default_factory=lambda: scanners.make_canary("filetext", awkward=True))
    tool_output: scanners.Canary = field(default_factory=lambda: scanners.make_canary("toolout", awkward=True))
    reply: scanners.Canary = field(default_factory=lambda: scanners.make_canary("reply", awkward=True))
    reply2: scanners.Canary = field(default_factory=lambda: scanners.make_canary("replytwo", awkward=True))
    path: scanners.Canary = field(default_factory=lambda: scanners.make_canary("path"))
    command: scanners.Canary = field(default_factory=lambda: scanners.make_canary("command"))
    edit: scanners.Canary = field(default_factory=lambda: scanners.make_canary("edit", awkward=True))
    instructions: scanners.Canary = field(default_factory=lambda: scanners.make_canary("instr", awkward=True))
    fault: scanners.Canary = field(default_factory=lambda: scanners.make_canary("fault", awkward=True))

    def all(self) -> list[scanners.Canary]:
        return list(vars(self).values())

    def scanner(self) -> scanners.Scanner:
        return scanners.Scanner(self.all())

    # the file and the command every scenario shares
    @property
    def file_name(self) -> str:
        return f"notes_{self.path.token}.txt"

    @property
    def shell_command(self) -> str:
        return f"echo {self.command.value}; cat tool_output.txt"


def plant_project(root: Path, c: Canaries) -> None:
    """The project the model works on: a file whose content holds a canary, a file whose content the shell prints, and
    the instruction file that is loaded into run.start."""
    root.mkdir(parents=True, exist_ok=True)
    (root / c.file_name).write_text(f"line one\n{c.file_text.value}\n", encoding="utf-8")
    (root / "tool_output.txt").write_text(f"{c.tool_output.value}\n", encoding="utf-8")
    (root / "M2C.md").write_text(f"# Project notes\n\nAlways be careful. {c.instructions.value}\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# The model's scripts
# ---------------------------------------------------------------------------------------------

def read_call(c: Canaries, call_id="prov_read"):
    return call("read_file", {"path": c.file_name}, call_id=call_id)


def edit_call(c: Canaries, call_id="prov_edit"):
    return call("edit_file", {"path": c.file_name, "old_string": "line one", "new_string": f"edited {c.edit.value}"},
                call_id=call_id)


def bash_call(c: Canaries, call_id="prov_bash", command=None):
    return call("bash", {"command": command or c.shell_command, "reason": "look at the output"}, call_id=call_id)


def full_script(c: Canaries) -> list:
    """The normal session: read, edit, shell command, a final answer (four model calls)."""
    return [priced(f"Let me look. {c.reply.value}", [read_call(c)]),
            priced("", [edit_call(c)]),
            priced("", [bash_call(c)]),
            priced(f"All done. {c.reply2.value}")]


# ---------------------------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------------------------

class _LiveServer:
    """uvicorn on 127.0.0.1 in a thread, at the log level production uses (info)."""

    def __init__(self, app) -> None:
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="info", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, name="canary-server", daemon=True)
        self.thread.start()
        end = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > end:
                raise AssertionError("the server did not start")
            time.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)


class FakeScreen:
    """The terminal, for m2c/tui/session.py: it keeps what was shown."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.status: list[str] = []

    def show(self, blocks) -> None:
        self.chunks.extend(str(getattr(b, "text", "")) for b in blocks)

    def set_status(self, lines) -> None:
        self.status = list(lines)

    def set_prompt(self, label, text="") -> None:
        pass

    async def run(self, controller) -> None:
        await controller.exit_requested.wait()

    def text(self) -> str:
        return "".join(self.chunks)


@dataclass
class Result:
    """What one scenario left behind: everything the scanners look at."""

    mode: str
    canaries: Canaries
    user_id: str
    run_ids: list
    redis_after: list
    redis_written: list
    guard_after: list
    guard_written: list
    leftover_keys: list
    statements: list
    server_logs: list
    device_logs: str
    sentry: list
    ledger_rows: list
    local_store: list
    local_files: list
    twin_tables: dict
    db_scan: Any
    run_states: dict
    midrun: dict = field(default_factory=dict)


class Stack:
    def __init__(self, monkeypatch, tmp_path: Path, *, canaries: "Canaries | None" = None, quota: bool = False,
                 ledger_faults: Exception | None = None, script=None) -> None:
        """`script`: the model's replies (a list; default: full_script).  `ledger_faults`: an exception the ledger's
        commit raises (metering must fail open).  `quota`: the budget check refuses (a quota pause)."""
        self.mp, self.tmp = monkeypatch, tmp_path
        self.c = canaries or Canaries()
        self.mode = MODE
        self.user_id = USER if MODE == "fake" else f"canary_user_{uuid.uuid4().hex[:10]}"
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(key, "127.0.0.1,localhost")
        self.statements = StatementLog()

        self.world = World(monkeypatch)
        run_manager._protocol()                            # skips (ProtocolSchemasUnavailable) when protocol/ is absent
        self.redis = RecordingRunRedis(self.world.bt_clock)
        monkeypatch.setattr(buffer, "_REDIS", self.redis)
        self.guard = RecordingGuardRedis()
        monkeypatch.setattr(run_guard, "_redis", lambda: self.guard)
        self.world.guard = self.guard

        self.ledger_inner = FakeLedger(pool=1000.0)
        self.ledger = RecordingLedger(self.ledger_inner, self.statements)
        self._wire_postgres()

        self.rig = Rig(monkeypatch, revalidate_interval_s=0.2)
        self.emitter = RunEmitter()
        monkeypatch.setattr(emitter_module, "_EMITTER", self.emitter)
        self.handlers = RunHandlers(emitter=self.emitter, max_parallel=10, hook_timeout=5.0, stop_timeout=5.0,
                                    purge_delays=(0.01,))
        monkeypatch.setattr(run_handlers, "_HANDLERS", self.handlers)
        self.router = MessageRouter()
        monkeypatch.setattr(router_module, "_ROUTER", self.router)
        self.broker = broker_module.ToolBroker(emit=self.emitter.emit, timeout_s=10.0)
        monkeypatch.setattr(broker_module, "_BROKER", self.broker)
        monkeypatch.setattr(loop_module, "_LOOP", None)
        monkeypatch.setattr(wiring, "_BUILT", None)

        self.adapter = ScriptedAdapter(*(script if script is not None else full_script(self.c)))
        self.budget_store = metering.RunBudgetStore(redis=lambda: self.guard)
        kw: dict = {}
        if self.mode == "fake":
            kw.update(check=self.ledger.check, commit=self.ledger.commit, ledger_sum=self.ledger.run_credits)
        if ledger_faults is not None:                      # metering must fail open, and say nothing about the text
            if self.mode == "fake":
                self.ledger_inner.commit_raises = ledger_faults
            else:
                def failing_commit(**row):
                    raise ledger_faults
                monkeypatch.setattr(usage_ledger, "commit", failing_commit)
        if quota:
            kw["check"] = self._refusing_check
        self.loop = wiring.build_loop(
            environ={}, chain=[STRONG], adapter=self.adapter, store=self.budget_store,
            policy=metering.RunCeilingPolicy(fixed=1000.0, chunk_fixed=5.0), tool_budget=FakeBudget(),
            config=LoopConfig(cancel_poll_s=0.01, stop_timeout_s=2.0), **kw)
        run_handlers.install(self.router, handlers=self.handlers)
        wiring.install(loop=self.loop)
        broker_module.install(self.router, broker=self.broker, handlers=self.handlers)
        self.rig.use_handler(self.router)

        self.credential_text = "m2c_canary_cred_" + "x" * 30
        _, self.device_id = self.rig.store.add_device(user_id=self.user_id, credential=self.credential_text)

        # logs, before anything runs: the server's, and the device's through its own real configuration
        self.logs = LogCollector()
        self._saved_levels = {}
        root = logging.getLogger()
        root.addHandler(self.logs)
        for name in SERVER_NAMESPACES:
            logger = logging.getLogger(name)
            self._saved_levels[name] = logger.level
            logger.setLevel(logging.DEBUG if name != "websockets" else logging.INFO)
        self._root_level = root.level
        root.setLevel(logging.DEBUG)
        self.device_stream = io.StringIO()
        m2c_logger = logging.getLogger("m2c")
        self._saved_m2c = (list(m2c_logger.handlers), m2c_logger.level, m2c_logger.propagate)
        configure_logging(level=logging.DEBUG, stream=self.device_stream)

        self.sentry = SentryCapture()
        self.server = _LiveServer(self.rig.app)
        self.project = tmp_path / "project"
        plant_project(self.project, self.c)

    # -- postgres, by mode ------------------------------------------------------------------
    def _wire_postgres(self) -> None:
        mp = self.mp
        if self.mode == "fake":
            mp.setattr(runs, "_STORE", RecordingRunStore(self.world.store, self.statements))
            log = self.statements

            @contextlib.contextmanager
            def trap(user_id=None, trusted=False):
                yield _TrapCursor(log)

            def trap_pool(*a, **k):
                log.add("<connection pool requested>", None, trap=True)
                raise RuntimeError("canary harness: Postgres is not available in fake mode")

            mp.setattr(eo_db, "cursor", trap)
            mp.setattr(eo_db, "_get_pool", trap_pool)
            return
        import psycopg
        from psycopg.rows import dict_row

        log = self.statements

        @contextlib.contextmanager
        def cursor(user_id=None, trusted=False):
            conn = psycopg.connect(DSN, row_factory=dict_row)
            try:
                cur = conn.cursor()
                cur.execute("select set_config('app.current_user_id', %s, true), "
                            "set_config('app.trusted_internal', %s, true)",
                            (user_id or "", "true" if trusted else "false"))
                yield _RecordingCursor(cur, log)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        mp.setattr(eo_db, "cursor", cursor)
        mp.setattr(runs, "_STORE", runs.PgRunStore())
        plans.reload_config()

    def _refusing_check(self, user_id):
        return usage_ledger.CheckResult(status="quota_exhausted", allowed=False, pool=10.0, used=10.0, remaining=0.0,
                                        resets_at="2026-12-01T00:00:00+00:00")

    # -- the device -------------------------------------------------------------------------
    def credential(self) -> Credential:
        return Credential(device_credential=Secret(self.credential_text), device_id=self.device_id,
                          server_url=self.server.url)

    @contextlib.asynccontextmanager
    async def device(self):
        """The real device client on a real socket: yields a Device (session controller, screen, connection control)."""
        d = Device(self)
        await d.start()
        try:
            yield d
        finally:
            await d.stop()

    # -- bookkeeping ------------------------------------------------------------------------
    def run_row(self, run_id):
        return runs.get_run(self.user_id, run_id)

    def run_state(self, run_id):
        """The run's state, or None while its row does not exist yet.  The device learns the run id it minted itself
        before the server's insert has committed, so a poll that starts at once can come first (seen under load in
        real-database mode): absence is "not yet" here, and only `until`'s timeout turns it into a failure."""
        try:
            return runs.get_run(self.user_id, run_id).state
        except runs.RunNotFound:
            return None

    def keys_of(self, run_id) -> list:
        """Every Redis key of either twin that names this run."""
        names = list(self.redis.keys()) + [str(k) for k in self.guard.data]
        return [k for k in names if run_id in k]

    async def ended(self, run_id, timeout=20.0):
        def over():
            try:
                return runs.get_run(self.user_id, run_id).ended_at is not None
            except runs.RunNotFound:
                return False
        await until(over, timeout, "the run to reach a terminal state")

    def midrun_snapshot(self) -> dict:
        """What Redis holds right now (an active run), for the 'only sealed content while active' checks."""
        return {"strings": scanners.redis_strings(self.redis) + [str(k) for k in self.guard.data] +
                [str(v) for v in self.guard.data.values()],
                "unsealed": scanners.unsealed_values(self.redis),
                "ttls": {k: self.redis.ttl(k) for k in self.redis.keys()},
                "written": self.redis.written_strings() + self.guard.written_strings()}

    def collect(self, device: "Device | None", run_ids: list, midrun: dict | None = None) -> Result:
        """Everything the scanners look at.  `device` None: no device was run (the scanner self-test), so its own store
        and files are empty."""
        run_states = {r: self.run_row(r).state for r in run_ids}
        local_store = device.local_store_dump() if device is not None else []
        local_files = device.local_files() if device is not None else []
        from memory import bus
        guard_after = [str(k) for k in self.guard.data] + [str(v) for v in self.guard.data.values()]
        bus_strings = [str(k) for k in getattr(bus.redis, "_store", {})] + \
                      [str(v) for v in getattr(bus.redis, "_store", {}).values()]
        twin = {"devices": list(self.rig.store.devices.values())}
        if self.mode == "fake":
            twin["runs"] = [dict(r) for r in self.world.store.rows.values()]
            twin["usage_ledger"] = list(self.ledger_inner.rows)
        ledger_rows = list(self.ledger_inner.rows) if self.mode == "fake" else self._real_ledger_rows()
        return Result(
            mode=self.mode, canaries=self.c, user_id=self.user_id, run_ids=run_ids,
            redis_after=scanners.redis_strings(self.redis) + bus_strings, redis_written=self.redis.written_strings(),
            guard_after=guard_after, guard_written=self.guard.written_strings(),
            leftover_keys=[k for r in run_ids for k in self.keys_of(r)],
            statements=self.statements.snapshot(), server_logs=self.logs.snapshot(),
            device_logs=self.device_stream.getvalue(), sentry=self.sentry.payloads(), ledger_rows=ledger_rows,
            local_store=local_store, local_files=local_files, twin_tables=twin,
            db_scan=self._scan_real_database(), run_states=run_states, midrun=midrun or {})

    def _real_ledger_rows(self) -> list:
        with eo_db.cursor(user_id=self.user_id) as cur:
            cur.execute("select * from usage_ledger where user_id = %s", (self.user_id,))
            return [dict(r) for r in cur.fetchall()]

    def _scan_real_database(self):
        """(hits, rows scanned per table) over EVERY table, or None in fake mode."""
        if self.mode != "real":
            return None
        import psycopg
        with psycopg.connect(SCAN_DSN, autocommit=True) as conn:
            blind = scanners.rows_visible_check(conn)
            if blind:
                pytest.fail("the database scan would be blind: " + blind, pytrace=False)
            return scanners.scan_postgres(self.c.scanner(), conn)

    def close(self) -> None:
        self.sentry.close()
        self.server.stop()
        root = logging.getLogger()
        root.removeHandler(self.logs)
        root.setLevel(self._root_level)
        for name, level in self._saved_levels.items():
            logging.getLogger(name).setLevel(level)
        m2c_logger = logging.getLogger("m2c")
        m2c_logger.handlers[:] = self._saved_m2c[0]
        m2c_logger.setLevel(self._saved_m2c[1])
        m2c_logger.propagate = self._saved_m2c[2]
        self.world.teardown()


class Device:
    """The person's machine: the real session controller on a real DeviceLink, with the person's approvals automatic."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.screen = FakeScreen()
        self.store = open_db()                             # under the hermetic M2C_HOME
        self.conns: list = []
        self._may_connect = asyncio.Event()
        self._may_connect.set()
        self.approve = True
        self.session = Session(
            self.store, self.screen, project_path=str(stack.project), mode="ask",
            link_options={"credential_loader": stack.credential, "connector": self._connector,
                          "backoff": BackoffPolicy(base_s=0.05, cap_s=0.2, stable_after_s=0.1),
                          "keepalive_interval_s": None})
        self._approver: asyncio.Task | None = None

    def _connector(self, url, **options):
        async def go():
            await self._may_connect.wait()
            conn = await connect(url, **options)
            self.conns.append(conn)
            return conn
        return go()

    async def start(self) -> None:
        failed = await self.session.start(15.0)
        if failed is not None:
            raise AssertionError(f"the device could not connect (exit status {failed})")
        self._approver = asyncio.get_running_loop().create_task(self._approve_forever())

    async def _approve_forever(self) -> None:
        while True:
            if self.session.approval_active and self.approve:
                self.session.on_key("y")                   # allow once
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if self._approver is not None:
            self._approver.cancel()
            await asyncio.gather(self._approver, return_exceptions=True)
        await self.session.close()
        self.store.close()

    # -- what a person does -----------------------------------------------------------------
    def say(self, text: str) -> None:
        self.session.submit(text)

    @property
    def run_id(self):
        return self.session._run_id

    async def screen_has(self, text: str, timeout=20.0) -> None:
        await until(lambda: text in self.screen.text(), timeout, "text on the screen")

    def interrupt(self) -> None:
        self.session.on_interrupt()

    def drop_connection(self) -> None:
        """Lose the TCP connection abruptly; the reconnect is held until allow_reconnect()."""
        self._may_connect.clear()
        self.conns[-1].transport.abort()

    def allow_reconnect(self) -> None:
        self._may_connect.set()

    # -- what the device keeps (it MAY hold content) ----------------------------------------
    def local_store_dump(self) -> list:
        return scanners.sqlite_dump(self.store)

    def local_files(self) -> list:
        home = Path(os.environ["M2C_HOME"])
        out = []
        if home.exists():
            for p in home.rglob("*"):
                if p.is_file() and p.name != "state.db":
                    with contextlib.suppress(OSError):
                        out.append((str(p.relative_to(home)), p.read_bytes()))
        return out
