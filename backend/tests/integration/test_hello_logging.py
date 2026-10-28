"""
Patch I01 -- content hygiene (O1): no message text, prompt or reply on the server's logs, in any
database stand-in row, in the ledger, or in plaintext in the (encrypted) buffer's Redis.

A canary string is the user's message and another is the model's reply; the scenarios run the
paths that log the most (a normal turn, quota exhausted, a cancelled turn, a model failure, a
ledger failure, a resumed reply) and then every record the server produced is searched.
"""
import asyncio
import logging
import threading

from codeagent import hello
from tests.integration import hello_testkit as k
from tests.integration.hello_testkit import Session, blocking_reply, stack  # noqa: F401
from tests.unit.test_codeagent_adapter import Error, text_reply

CANARIES = (k.USER_TEXT_CANARY, k.REPLY_TEXT_CANARY)


def go(coro):
    return asyncio.run(coro)


_CLIENT_LOGGERS = ("m2c", "websockets.client")     # the device's side of this one-process test, not the server


def _everything_logged(caplog):
    out = []
    for rec in caplog.records:
        if rec.name.startswith(_CLIENT_LOGGERS):
            continue
        out.append(rec.getMessage())
        out.append(str(rec.args))
        out.append(rec.exc_text or "")
        out.append(str(rec.__dict__.get("exc_info") or ""))
    return "\n".join(out)


def test_server_logs_and_rows_hold_no_message_or_reply_text(stack, caplog, capsys):
    # production-like (INFO) everywhere, DEBUG for the server's own loggers. The SDK is the trap: with
    # OPENAI_LOG=debug it would log whole requests, so put its logger at DEBUG first, as that would,
    # and let install() (idempotent) put it right again.
    caplog.set_level(logging.INFO)
    for name in ("codeagent", "eo"):
        caplog.set_level(logging.DEBUG, logger=name)
    logging.getLogger("openai").setLevel(logging.DEBUG)
    hello.install()
    assert logging.getLogger("openai").getEffectiveLevel() >= logging.INFO
    gate, started = threading.Event(), threading.Event()
    stack.provider.push(text_reply(k.REPLY_TEXT_CANARY))                       # a normal turn
    stack.provider.push(blocking_reply([k.REPLY_TEXT_CANARY], gate, started))  # a turn cancelled by Ctrl-C
    stack.provider.push(blocking_reply([k.REPLY_TEXT_CANARY], gate, started))  # a turn cut by a disconnect
    stack.provider.push(text_reply(k.REPLY_TEXT_CANARY))                       # ... then written again
    for _ in range(4):
        stack.provider.push(Error(500, {"error": {"message": k.USER_TEXT_CANARY}}))   # a provider that echoes

    async def scenario():
        async with Session(stack) as s:
            s.say(k.USER_TEXT_CANARY)
            await s.reply_count(1)
            s.say(k.USER_TEXT_CANARY)                                          # cancelled mid-reply
            await s.until(lambda: k.REPLY_TEXT_CANARY in s.text.split("credits]")[-1], what="streaming")
            s.repl.interrupt()
            await s.until(lambda: s.repl.run_id is None, what="the cancel")
            gate.set()
            s.say(k.USER_TEXT_CANARY)                                          # new run, cut by a disconnect
            await s.until(lambda: s.repl.run_id is not None and "restarting" not in s.text and s.text.count(k.REPLY_TEXT_CANARY) >= 3,
                          what="the third partial reply")
            run_id = s.repl.run_id
            gate.clear()
            s.drop_connection()
            await s.until(lambda: stack.run_row(run_id).state == "paused_disconnected", what="the pause")
            gate.set()
            s.allow_reconnect()
            await s.until(lambda: s.repl.restarts == 1 and s.repl.idle, what="the reply written again")
            stack.ledger.fail_commit = True                                    # a ledger failure
            stack.ledger.exhausted = True                                      # and a quota refusal
            s.say(k.USER_TEXT_CANARY)
            await s.until(lambda: s.errors and s.repl.run_id is None, what="the quota error")
            stack.ledger.exhausted = False
            s.say(k.USER_TEXT_CANARY)                                          # a model failure
            await s.until(lambda: len(s.errors) >= 2 and s.repl.run_id is None, what="the model failure")
            await s.leave()

    go(scenario())

    logged = _everything_logged(caplog)
    assert "[hello]" in logged                                                 # the module did log (ids, counts, codes)
    seen = capsys.readouterr()
    rows = repr(stack.world.store.__dict__) + repr(stack.ledger.rows) + repr(stack.rig.store.__dict__)
    redis = repr(stack.world.redis.__dict__)
    for canary in CANARIES:
        assert canary not in logged, f"{canary} reached the logs"
        assert canary not in seen.out + seen.err, f"{canary} reached the server's stdout/stderr"
        assert canary not in rows, f"{canary} reached a database stand-in or the ledger"
        assert canary not in redis, f"{canary} is in plaintext in the buffer's Redis"
