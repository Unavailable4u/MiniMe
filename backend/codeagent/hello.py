"""
codeagent/hello.py -- the walking skeleton's turn runner (patch I01, "hello run").

WHAT THIS IS. The smallest thing that joins the pieces built apart: a text-only turn
runner for S14's seam, so that one real message makes one real, streamed, metered
reply. No tools are offered or executed here; S07 replaces this module's `run_turn`
with the agent loop and keeps the same seam (`set_turn_runner`, then `replace=True`).

    device                      S14 (router, run handlers)            this module
    ------                      --------------------------            -----------
    run.start            --->   run_manager.start_run (S06)
    user_message         --->   queue, one worker task per run  --->  run_turn(conn, run, msg)
                                                                        1. usage_ledger.check      (S01)
                                                                        2. buffer.append_conversation (S06)
                                                                        3. adapter.stream          (S05)
                                                                        4. emitter.emit            (S14)
                                                                        5. credits_for + commit    (S02, S01)
    cancel / disconnect  --->   cancels the worker task          --->  CancelledError in run_turn
    run.resume           --->   replay, `running`, on_resume hooks --> _on_resume (restart, below)

THE TURN, STEP BY STEP
  1. Quota. `usage_ledger.check(user_id)` (surface 'm2c' is the ledger's concern, the pool is
     shared). Exhausted: the run moves to `paused_budget` (error_code quota_exhausted), the
     device gets the `quota_exhausted` error (ref_id = its message, details.reset_at when the
     ledger knows it) and then the `state_changed paused_budget` event, in the order
     protocol/fixtures/04_quota_error.jsonl pins. The model is not called and the message is
     not stored. The check fails open (a ledger that cannot be read allows the call).
  2. The user's message is appended to the run's encrypted conversation (the buffer is the
     only place the conversation lives; it is deleted when the run ends).
  3. The conversation is sent to the S05 adapter. Every TextDelta becomes a `text_delta`
     run_event as it arrives.
  4. The reply completes: `text_done` (stop_reason end_turn, or max_tokens when the provider
     says so), then the metering below, then `usage`. The finished reply is appended to the
     conversation. The run stays `running` for the next message.
  5. Metering. credits = S02's `credits_for(provider, model, input, cached, output)`; ONE
     ledger row via `usage_ledger.commit(surface='m2c', run_id=...)`, numbers only. A ledger
     write that raises is logged by exception type only and the turn finishes anyway (metering
     fails open). `usage_ledger` is the metering truth; `runs.add_progress` only keeps the
     informational per-run counter that feeds `run_credits_total`.

CANCELLATION. S14 cancels the turn's task when the device disconnects or sends `cancel`. The
turn sees asyncio.CancelledError at whatever await it is on; it closes the model stream (the
S05 generator's own cleanup closes the provider connection), drops its partial reply and
re-raises. A cancelled or interrupted turn writes NO ledger row: usage is only known at the end
of a completed model call. KNOWN LIMIT: the tokens a cancelled call already consumed are not
charged; S10 (budget and reservations) removes this later. (If the cancel arrives after the model
call completed and while the ledger write is in its thread, the row is still written: the call is
complete and its usage is known.)

RESUME MID-REPLY. A disconnect cancels the turn and S14 pauses the run. The partial text the
device already received stays in the event log. On `run.resume` S14 calls `_on_resume`: if the
run's conversation ends with a user message that has no assistant reply, a turn was interrupted.
Then the hook emits `notice reply_restarted` (info, fixed text) and runs the turn again WITHOUT
appending the user message a second time, so the reply is written again from the start; the
client discards the partial text when it sees the notice. A conversation that ends with an
assistant reply is left alone.

S14 has no public call that enqueues a turn from a hook (proposed follow-up, see the patch
description), so the restarted turn is a task this module tracks itself, by run id. It is
cancelled by `_on_cancel` and `_on_run_disconnect`, and it never runs beside a normal turn: both
paths hold one per-run lock (`_turn_lock`), so a message typed right after the resume waits for the
restarted reply and then sees it in the conversation.

FAILURES. A model call that fails (every step of S05's chain refused, or no provider key is set)
answers `provider_unavailable` with a fixed text, moves the run to `paused_error` and announces the
state, like the quota path; nothing is metered. The user message is already in the conversation, so
a later `run.resume` of that run restarts the turn (the same rule as an interrupted one). A ledger or
run-counter write that fails never fails the turn.

CONTENT HYGIENE (O1). No message text, prompt, reply or path is ever written to Postgres, to the
ledger, to a log or to an error message from here. Logs carry run id, device id, user id, message
type, token counts, error codes and exception TYPES only; the texts the device sees are fixed
constants below. The conversation lives only in the encrypted buffer. The openai SDK
would log whole requests at DEBUG (it turns DEBUG on itself when OPENAI_LOG=debug is set), so
`install()` holds the SDK's logger at INFO or above.

CONFIGURATION. The adapter is built once, from the provider key environment variables S05 and
utils/llm_client already use (GROQ_API_KEY[_N], MISTRAL_API_KEY[_N], OPENROUTER_API_KEY[_N]: the
first one set wins). `M2C_HELLO_MODEL` optionally names the model (the provider's default is used
otherwise). With no key set the turn answers `provider_unavailable` and pauses the run
(paused_error); tests inject an adapter with `set_adapter`.
"""
import asyncio
import logging
import os
import weakref

from starlette.concurrency import run_in_threadpool

from codeagent import buffer, run_handlers, run_manager, runs
from codeagent.adapters.openai_compat import OpenAICompatAdapter
from codeagent.emitter import get_emitter
from codeagent.model_adapter import Message, ModelAdapterError, ModelRequest, TextDelta, Usage
from eo import credits as credits_module
from eo import usage_ledger

logger = logging.getLogger(__name__)

SURFACE = "m2c"

# The system prompt is a constant of this module, never anything the person wrote.
SYSTEM_PROMPT = (
    "You are M2C, a coding assistant in the person's terminal. Answer briefly and plainly. "
    "You cannot read files or run commands yet; if asked to, say so."
)

# Fixed texts (error.json / notice: server-authored, no content, no exception text).
MSG_QUOTA_EXHAUSTED = "Your credit pool is used up for now."
MSG_PROVIDER_UNAVAILABLE = "No model could answer right now. You can retry."
NOTICE_REPLY_RESTARTED = "reply_restarted"
MSG_REPLY_RESTARTED = "The connection dropped while the reply was being written, so it is being written again."

# provider default chain: (provider, model, base environment variable). First one with a key wins.
_PROVIDERS = (
    ("groq", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    ("mistral", "mistral-medium-latest", "MISTRAL_API_KEY"),
    ("openrouter", "openrouter/free", "OPENROUTER_API_KEY"),
)
_MAX_KEY_SUFFIX = 20

_ADAPTER = None                                 # built on first use, or set by set_adapter()
_RESTARTS: "dict[str, asyncio.Task]" = {}       # run id -> the restarted-turn task
# One lock per run. Weak values: a lock lives only while a turn holds or awaits it, so the
# registry cannot grow with the number of runs.
_LOCKS: "weakref.WeakValueDictionary" = weakref.WeakValueDictionary()


# ---------------------------------------------------------------------
# Adapter configuration
# ---------------------------------------------------------------------

def build_chain(environ=None) -> list:
    """The CHAIN (S05's step shape) for the first provider whose key variable is set; an empty
    list when none is. Names variables and models only: never a key's value."""
    environ = os.environ if environ is None else environ
    for provider, model, base in _PROVIDERS:
        names = [base] + [f"{base}_{n}" for n in range(1, _MAX_KEY_SUFFIX + 1)]
        for name in names:
            if environ.get(name):
                model = environ.get("M2C_HELLO_MODEL") or model
                return [{"provider": provider, "model": model, "key_env": name}]
    return []


def set_adapter(adapter) -> None:
    """Install the model adapter (tests, and later the real wiring); None forgets it."""
    global _ADAPTER
    _ADAPTER = adapter


def get_adapter():
    """The adapter, built from the environment the first time. None when no provider key is set."""
    global _ADAPTER
    if _ADAPTER is None:
        chain = build_chain()
        if chain:
            _ADAPTER = OpenAICompatAdapter.from_chain(chain)
    return _ADAPTER


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def _turn_lock(run_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(run_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[run_id] = lock
    return lock


def _to_messages(items) -> tuple:
    """The conversation items as S05 messages. Only user and assistant text exists in this patch;
    anything else (a tool item from a later patch) is skipped rather than guessed at."""
    out = [Message.system(SYSTEM_PROMPT)]
    for item in items:
        role, text = item.get("role"), item.get("text")
        if role == "user" and isinstance(text, str):
            out.append(Message.user(text))
        elif role == "assistant" and isinstance(text, str) and text:
            out.append(Message.assistant(text))
    return tuple(out)


async def _close_stream(gen) -> None:
    """Close the adapter's async generator (which closes the provider connection). Waits for the
    close even when this task is being cancelled, then lets the cancellation through."""
    close = getattr(gen, "aclose", None)
    if close is None:
        return
    work = asyncio.ensure_future(close())
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        if not work.done():
            await asyncio.wait({work})
        if not work.cancelled():
            work.exception()                    # looked at: nobody else will
        raise
    except Exception as exc:
        logger.warning("[hello] stream close failed error=%s", type(exc).__name__)


async def _emit(run_id: str, t: str, payload: dict) -> dict:
    return await get_emitter().emit(run_id, t, payload)


def _provider_failure_ids(exc) -> str:
    return f"bucket={getattr(exc.bucket, 'name', '?')} code={exc.code} status={exc.status_code}"


# ---------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------

async def run_turn(conn, run, msg) -> None:
    """S14's turn runner: one turn for one `user_message`. `msg` is the validated envelope; the
    text is in msg["payload"]["text"]. Owns the run's state (see the module docstring)."""
    text = msg["payload"]["text"]
    async with _turn_lock(run.id):
        await _turn(conn, run, msg, text)


async def _turn(conn, run, msg, text) -> None:
    """The turn proper. `text` is the new user message, or None for a restarted turn (the user
    message is already in the conversation)."""
    user_id, run_id = run.user_id, run.id

    allowed, resets_at = await _quota(user_id)
    if not allowed:
        await _quota_exhausted(conn, run, msg, resets_at)
        return

    if text is not None:
        await run_in_threadpool(buffer.append_conversation, run_id, {"role": "user", "text": text})
    conversation = await run_in_threadpool(buffer.read_conversation, run_id)

    adapter = get_adapter()
    if adapter is None:
        logger.error("[hello] no model provider key is configured run=%s user=%s", run_id, user_id)
        await _provider_unavailable(conn, run, msg)
        return

    parts, usage = [], None
    gen = adapter.stream(ModelRequest(messages=_to_messages(conversation.items)))
    try:
        async for event in gen:
            if isinstance(event, TextDelta):
                parts.append(event.text)
                await _emit(run_id, "run_event", {"type": "text_delta", "text": event.text})
            elif isinstance(event, Usage):
                usage = event
            # A ToolCall cannot come back: none was offered.
    except ModelAdapterError as exc:
        logger.warning("[hello] model call failed run=%s device=%s user=%s %s emitted=%s", run_id,
                       conn.device_id, user_id, _provider_failure_ids(exc), exc.emitted_output)
        await _provider_unavailable(conn, run, msg)
        return
    finally:
        await _close_stream(gen)                # also on CancelledError: no open provider stream is left

    reply = "".join(parts)
    stop = usage.stop_reason if usage is not None and usage.stop_reason in ("end_turn", "max_tokens") else "end_turn"
    await _emit(run_id, "run_event", {"type": "text_done", "stop_reason": stop})

    numbers = await _meter(run, usage)
    await _emit(run_id, "run_event", dict(numbers["event"]))
    await run_in_threadpool(buffer.append_conversation, run_id, {"role": "assistant", "text": reply})
    logger.info("[hello] turn done run=%s device=%s user=%s input=%s cached=%s output=%s credits=%s",
                run_id, conn.device_id, user_id, numbers["input"], numbers["cached"], numbers["output"],
                numbers["credits"])


async def _quota(user_id: str):
    """(allowed, resets_at). usage_ledger.check never raises and fails open; if it somehow does,
    so do we (metering fails open)."""
    try:
        result = await run_in_threadpool(usage_ledger.check, user_id)
    except Exception as exc:
        logger.warning("[hello] quota check failed, allowing the call user=%s error=%s", user_id, type(exc).__name__)
        return True, None
    return bool(result.allowed), getattr(result, "resets_at", None)


async def _quota_exhausted(conn, run, msg, resets_at) -> None:
    """quota_exhausted: park the run in paused_budget, tell the device, announce the state."""
    user_id, run_id = run.user_id, run.id
    try:
        await run_in_threadpool(run_manager.pause_run, user_id, run_id, "paused_budget",
                                error_code="quota_exhausted")
    except Exception as exc:
        logger.error("[hello] could not pause the run for quota run=%s user=%s error=%s",
                     run_id, user_id, type(exc).__name__)
    details = {"reset_at": resets_at} if resets_at else None
    await run_handlers.get_handlers()._tell(conn, msg, "quota_exhausted", MSG_QUOTA_EXHAUSTED, details)
    await _announce_pause(run, "paused_budget", "quota_exhausted")
    logger.info("[hello] quota exhausted run=%s device=%s user=%s", run_id, conn.device_id, user_id)


async def _provider_unavailable(conn, run, msg) -> None:
    """The model could not answer: tell the device and park the run in paused_error (retry)."""
    try:
        await run_in_threadpool(run_manager.pause_run, run.user_id, run.id, "paused_error",
                                error_code="provider_unavailable")
    except Exception as exc:
        logger.error("[hello] could not pause the run after a model failure run=%s user=%s error=%s",
                     run.id, run.user_id, type(exc).__name__)
    await run_handlers.get_handlers()._tell(conn, msg, "provider_unavailable", MSG_PROVIDER_UNAVAILABLE)
    await _announce_pause(run, "paused_error", "provider_unavailable")


async def _announce_pause(run, state_name: str, reason: str) -> None:
    """Put the pause in the run's event stream and release the emitter (a paused run holds no
    emitter state, as after a disconnect)."""
    em = get_emitter()
    try:
        await em.emit_state_changed(run.id, state_name, reason)
    except Exception as exc:
        logger.error("[hello] pause not stored in the replay run=%s user=%s error=%s",
                     run.id, run.user_id, type(exc).__name__)
    finally:
        em.unbind(run.id)


async def _meter(run, usage) -> dict:
    """Price the finished call, write ONE ledger row, bump the run's informational counter, and
    return the numbers for the `usage` event. Never raises: metering fails open."""
    u = usage if usage is not None else Usage(reported=False)
    provider = u.provider or "unknown"
    model = u.model or "unknown"
    credits = credits_module.credits_for(provider, model, u.input, u.cached_input, u.output)
    try:
        await run_in_threadpool(
            usage_ledger.commit, user_id=run.user_id, surface=SURFACE, run_id=run.id, provider=provider,
            model=model, input_tokens=u.input, cached_input_tokens=u.cached_input,
            output_tokens=u.output, credits=credits)
    except Exception as exc:
        logger.error("[hello] ledger write failed run=%s user=%s error=%s", run.id, run.user_id, type(exc).__name__)
    event = {"type": "usage", "model": model if _model_ok(model) else "unknown",
             "input_tokens": u.input, "cached_input_tokens": u.cached_input,
             "output_tokens": u.output, "credits": credits}
    try:
        updated = await run_in_threadpool(runs.add_progress, run.user_id, run.id, credits=credits)
        event["run_credits_total"] = updated.credits
    except Exception as exc:
        logger.warning("[hello] run counter not updated run=%s user=%s error=%s", run.id, run.user_id, type(exc).__name__)
    return {"event": event, "input": u.input, "cached": u.cached_input, "output": u.output, "credits": credits}


def _model_ok(model: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model))


# ---------------------------------------------------------------------
# Hooks (S14 calls these; each is async and quick)
# ---------------------------------------------------------------------

def _forget_restart(run_id: str, task=None) -> None:
    if task is None or _RESTARTS.get(run_id) is task:
        _RESTARTS.pop(run_id, None)


async def _stop_restart(run_id: str) -> None:
    """Cancel the run's restarted turn, if any, and wait for it to unwind."""
    task = _RESTARTS.pop(run_id, None)
    if task is None or task.done() or task is asyncio.current_task():
        return
    task.cancel()
    await asyncio.wait({task}, timeout=run_handlers.get_handlers().stop_timeout)


async def _on_cancel(conn, run) -> None:
    await _stop_restart(run.id)


async def _on_run_disconnect(conn, run) -> None:
    await _stop_restart(run.id)


async def _on_resume(conn, run) -> None:
    """After a run.resume that took the run out of a pause: if a turn was interrupted (the
    conversation ends with a user message), say so and run it again, as a tracked task."""
    try:
        conversation = await run_in_threadpool(buffer.read_conversation, run.id)
    except Exception as exc:
        logger.error("[hello] could not read the conversation on resume run=%s user=%s error=%s",
                     run.id, run.user_id, type(exc).__name__)
        return
    items = conversation.items
    if not items or items[-1].get("role") != "user":
        return                                  # nothing was interrupted
    await _stop_restart(run.id)                 # at most one restarted turn per run
    task = asyncio.create_task(_restart(conn, run))
    _RESTARTS[run.id] = task
    task.add_done_callback(lambda t, rid=run.id: _forget_restart(rid, t))


async def _restart(conn, run) -> None:
    """The restarted turn: the notice, then the same turn without a second user message."""
    try:
        async with _turn_lock(run.id):
            await _emit(run.id, "run_event", {"type": "notice", "code": NOTICE_REPLY_RESTARTED,
                                              "severity": "info", "message": MSG_REPLY_RESTARTED})
            await _turn(conn, run, {}, None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("[hello] restarted turn failed run=%s device=%s user=%s error=%s",
                     run.id, conn.device_id, run.user_id, type(exc).__name__)


# ---------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------

def _keep_prompts_out_of_sdk_logs() -> None:
    """The openai SDK's DEBUG log line "Request options" carries the whole request, messages
    included, and the SDK turns DEBUG on by itself when OPENAI_LOG=debug is set. O1 forbids message
    text in any log, so the SDK's logger is held at INFO or above (never lowered if already higher)."""
    sdk = logging.getLogger("openai")
    if sdk.getEffectiveLevel() < logging.INFO:
        sdk.setLevel(logging.INFO)


def install() -> None:
    """Wire the walking skeleton into S14: the turn runner and the three hooks. Safe to call
    twice (S14 keeps one copy of a hook, and accepts the same runner again). Does not need
    `replace=True`: no other turn runner is installed in this repo yet."""
    _keep_prompts_out_of_sdk_logs()
    run_handlers.set_turn_runner(run_turn)
    run_handlers.add_on_cancel(_on_cancel)
    run_handlers.add_on_resume(_on_resume)
    run_handlers.add_on_run_disconnect(_on_run_disconnect)
