"""
m2c/tests/link/test_link_logging.py -- Patch C04: the link never logs message
content, prompts, file contents or the device credential (build plan 11.4, and
the C04 work package).  It logs message types, ids and error codes only.

Attacked from three sides, like C03's redaction test:

* a run with distinctive secret-looking strings in every place content can
  travel (what the user typed, what the model said, a file path and a search
  pattern in a tool call, tool output, instructions, a server error's text) is
  driven through every code path that logs -- connect, kill, resume, gap,
  unknown run, malformed event, rejected request, a store failure, a revoked
  device -- with the RAW log records captured; the records and every rendering
  of every exception are searched for those strings;
* the source is parsed: no ``logger.exception``/``exc_info``/``print``, logging
  calls take a literal or go through ``log_event`` with allowlisted keys, and
  the credential is revealed in exactly one function;
* repr() of the link shows nothing.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from link_fakes import *  # noqa: F401,F403  (autouse fixtures; see link_fakes.py)

import m2c.link
from m2c.link import LinkDeviceRevokedError, LinkStoreError
from m2c.logging_setup import SAFE_KEYS
from m2c.store import StoreError

LINK_SRC = Path(m2c.link.__file__).resolve().parent

CREDENTIAL = "m2c-test-cred-ZZTOPSECRET0123456789"
PROMPT = "SECRET-PROMPT-alpha-4417"
MODEL_TEXT = "SECRET-MODEL-OUTPUT-bravo-9120"
PATH = "/home/user/SECRET-PATH-charlie-7731/app.py"
PATTERN = "SECRET-PATTERN-delta-2208"
TOOL_OUTPUT = "SECRET-TOOL-OUTPUT-echo-5563"
INSTRUCTIONS = "SECRET-INSTRUCTIONS-foxtrot-8842"
SERVER_TEXT = "SECRET-SERVER-MESSAGE-golf-3319"
DISK_TEXT = "SECRET-DISK-hotel-6604"
SECRETS = (CREDENTIAL, PROMPT, MODEL_TEXT, PATH, PATTERN, TOOL_OUTPUT, INSTRUCTIONS, SERVER_TEXT, DISK_TEXT,
           "SECRET-PATH", "SECRET-MODEL", "SECRET-TOOL")
RUN = "run_logging_test01"


def assert_no_secrets(*texts):
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, f"{secret!r} leaked into: {text[:300]!r}"


def secret_stream():
    ev = synthetic_events(RUN, 9)
    ev[2]["payload"] = {"type": "text_delta", "text": MODEL_TEXT}
    ev[3] = env("tool_proposed", {"call_id": "call_1", "tool": "grep", "input": {"pattern": PATTERN, "path": PATH},
                                  "risk": "R0"}, run_id=RUN, seq=4)
    ev[4]["payload"] = {"type": "notice", "code": "info", "severity": "info", "message": SERVER_TEXT}
    return ev


def test_nothing_secret_reaches_a_log_in_any_path(m2c_logs):
    events = secret_stream()
    store = open_db()
    new_run(store, RUN)
    problems: list = []

    async def conn0(conn):
        await do_handshake(conn)
        await conn.recv(timeout=None)                                   # run.start
        for ev in events[:5]:                                           # 1..5: text, a tool proposal, a notice
            await conn.send(ev)
        await conn.send(env("error", {"code": "invalid_message", "message": SERVER_TEXT,
                                      "ref_id": str(uuid.uuid4())}, run_id=RUN))
        await conn.send(synthetic_events("run_unknown_00001", 4)[1])    # a run we never started
        await conn.send(env("run_event", {"type": "text_delta"}, run_id=RUN, seq=6))   # malformed, in order
        await conn.send(events[7])                                      # seq 7 skipped: a gap
        await conn.recv(timeout=5)                                      # the resume
        await asyncio.sleep(0.05)
        await conn.kill()

    async def conn1(conn):
        await do_handshake(conn)
        resume = await conn.recv(timeout=5)
        for ev in events[resume["payload"]["last_seq"]:]:
            await conn.send(ev)
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(conn0, conn1) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec, credential=make_credential(srv.server_url, CREDENTIAL))
            assert CREDENTIAL not in repr(link) and CREDENTIAL not in str(link)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, mode="ask", cwd_label="demo", instructions=INSTRUCTIONS)
            await link.send_user_message(RUN, PROMPT)
            await until(lambda: link.run_info(RUN).last_seq == 9, what="the stream")
            await link.send_tool_result(RUN, "call_1", ok=True, output=TOOL_OUTPUT, truncated=False,
                                        trust="local_project")
            await link.aclose()

    run_async(scenario())
    texts = log_texts(m2c_logs)
    assert_no_secrets(*texts)
    joined = "\n".join(r.getMessage() for r in m2c_logs.records)
    for expected in ("link.connected", "link.disconnected", "link.resume.sent", "link.event.gap", "link.event.invalid",
                     "link.event.unknown_run", "link.error.received"):
        assert expected in joined, f"{expected} was not logged: the test would pass vacuously"
    assert "code=invalid_message" in joined                              # error codes are logged
    assert f"run_id={RUN}" in joined                                     # ids are logged
    ours = [r for r in m2c_logs.records if r.name.startswith("m2c.link")]       # not the store's own "store.migrated"
    assert ours and all(r.getMessage().startswith("link.") for r in ours), [r.getMessage() for r in ours]


def test_a_revoked_device_leaves_no_secret_in_the_exception_or_its_traceback_locals(m2c_logs):
    store = open_db()

    async def scenario():
        async with FakeServer(reject_script("device_revoked", 4403, message=SERVER_TEXT)) as srv:
            link = make_link(store, srv, credential=make_credential(srv.server_url, CREDENTIAL))
            link.start()
            with pytest.raises(LinkDeviceRevokedError) as raised:
                await link.wait_closed()
            return raised.value, link.failure

    exc, failure = run_async(scenario())
    assert failure is exc
    assert_no_secrets(*exception_texts(exc), *log_texts(m2c_logs))
    assert "code=device_revoked" in " ".join(r.getMessage() for r in m2c_logs.records)


def test_a_store_failure_does_not_carry_the_stores_message_or_the_event(m2c_logs, monkeypatch):
    """The store's exception text can embed the row it was writing."""
    from m2c.link import client as link_client

    store = open_db()
    new_run(store, RUN)
    events = secret_stream()

    def failing_append(*args, **kwargs):
        raise StoreError(f"cannot write {MODEL_TEXT} {DISK_TEXT}")

    monkeypatch.setattr(link_client, "append_event", failing_append)
    replay = ReplayRun(events)

    async def scenario():
        async with FakeServer(replay.script) as srv:
            link = make_link(store, srv, credential=make_credential(srv.server_url, CREDENTIAL))
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, wait=False, mode="ask", cwd_label="demo", instructions=INSTRUCTIONS)
            with pytest.raises(LinkStoreError) as raised:
                await link.wait_closed()
            return raised.value

    exc = run_async(scenario())
    assert_no_secrets(*exception_texts(exc), *log_texts(m2c_logs))
    assert exc.__cause__ is None                       # raised outside the except block, with fixed text


# --------------------------------------------------------------------------
# The source
# --------------------------------------------------------------------------
def link_modules():
    return sorted(LINK_SRC.glob("*.py"))


def test_the_link_package_exists_with_the_three_modules():
    assert {p.name for p in link_modules()} >= {"__init__.py", "client.py", "reconnect.py"}


def test_no_traceback_logging_and_no_print_in_the_link_source():
    offenders = []
    for path in link_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in ("exception", "print_exc", "print_exception", "format_exc", "print", "pprint"):
                    offenders.append(f"{path.name}:{node.lineno} {name}")
                if any(k.arg == "exc_info" or k.arg == "stack_info" for k in node.keywords):
                    offenders.append(f"{path.name}:{node.lineno} exc_info")
    assert offenders == []


def test_logging_calls_are_literals_or_log_event_with_allowlisted_keys():
    offenders = []
    levels = {"debug", "info", "warning", "error", "critical", "log"}
    for path in link_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in levels and getattr(func.value, "id", "") in ("logger", "log"):
                literal_only = (len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                                and isinstance(node.args[0].value, str) and not node.keywords)
                if not literal_only:
                    offenders.append(f"{path.name}:{node.lineno} logger.{func.attr} with data")
            if getattr(func, "id", "") == "log_event":
                if len(node.args) < 3 or not (isinstance(node.args[2], ast.Constant) and isinstance(node.args[2].value, str)):
                    offenders.append(f"{path.name}:{node.lineno} log_event name is not a literal")
                for kw in node.keywords:
                    if kw.arg is None or kw.arg not in SAFE_KEYS:
                        offenders.append(f"{path.name}:{node.lineno} log_event key {kw.arg!r}")
    assert offenders == []


def test_the_credential_is_revealed_in_exactly_one_function():
    """C03's discipline: a raw secret in a local variable shows up in tracebacks
    and error trackers.  The link reveals it only while building and sending
    the hello, in one function, and never binds the text to a local."""
    sites = []
    for path in link_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(func):
                if isinstance(node, ast.Attribute) and node.attr == "reveal":
                    sites.append(func.name)
    assert sites and set(sites) == {"_send_hello"}, sites
