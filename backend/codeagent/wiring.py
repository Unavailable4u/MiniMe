"""
codeagent/wiring.py -- Patch S15: switch the server from hello.py's turn runner to the real agent loop.

WHAT THIS IS. Everything the loop needs was built apart and nothing joined it: the loop (S07), the budgeted context
assembler (S08), the tool broker (S09), metering and the grant route (S10), the router, emitter and run handlers (S14).
This module builds the process loop from those real parts and installs it where hello.py's runner was. api/server.py
makes one call, `codeagent_wiring.install()`, in place of `codeagent_hello.install()`.

    build_loop()   AgentLoop(chain = loop.default_chain(), broker = the process broker, emit / emit_final / unbind =
                   the S14 emitter (looked up at every call, like build_default_loop does), assembler = S08's
                   BudgetedContextAssembler, metering_factory = S10's make_metering_factory fed with the real check,
                   commit, credits and emit functions, offer_tools = conn_offers_tools)
    install()      loop mode (the default): set_loop(), the loop's turn runner (replace=True, so it takes hello's place)
                   and its resume, cancel and disconnect hooks, plus one small hook of this module (below).
                   hello mode (M2C_LOOP=hello, the kill switch): hello.py's runner and hooks instead.
                   Either way the OTHER side's runner and hooks are not left registered. Safe to call twice.

WHAT hello.py DID, AND WHERE EACH PIECE IS NOW (the work package's parity list)
    quota check before the model call     S10 RunMetering.before_call: usage_ledger.check, then BudgetPause. The device
                                          gets the quota_exhausted error, then state_changed paused_budget (fixture 04).
    ONE ledger row per model call         S10 RunMetering.after_call: credits_for (S02) and usage_ledger.commit,
                                          numbers only. A call that is cut (cancel, disconnect) writes none.
    the `usage` event                     the same place, with run_credits_total.
    provider failure -> provider_unavailable, paused_error      the loop's own chain walk and pause.
    restart of an interrupted reply       the loop's on_resume hook restarts the turn. It does NOT say so: its
                                          `reply_restarted` notice is only for a provider fallback in the middle of a
                                          reply. hello's resume hook sent the notice, so `_announce_restart` below does
                                          the same for the loop, and only when partial text was really sent.
    cancellation without leaked tasks     the loop's on_cancel / on_run_disconnect hooks stop the continuation task it
                                          started; S14 cancels the worker task of an ordinary turn.
    the openai SDK logger held at INFO    `_keep_prompts_out_of_sdk_logs` below (hello.install did it; the loop path
                                          would otherwise lose it and O1 forbids prompt text in any log).

TOOLS ONLY FOR CLIENTS THAT CAN ANSWER THEM. The device lists what it can do in hello.capabilities and S04 keeps that on
the connection (`conn.capabilities`). `conn_offers_tools(conn)` is True only when the list contains "tools" (C09 declares
it). The loop asks it once per turn: a connection without it, such as I01's plain prompt loop, gets a turn with no tools
at all and is never sent a tool_proposed. See loop.AgentLoop's `offer_tools`.

CONFIGURATION (what the real path needs, and what happens when it is missing). install() NEVER raises for a missing
setting, because the web app lives in the same process and must still boot; it logs one warning per problem, by NAME
(configuration_problems()), and each feature fails closed by itself:
    RUN_BUFFER_SECRET            >= 32 chars. Missing: run.start is refused before any row is written (device gets
                                 `internal`, fixed text). No content is ever held without it.
    DEVICE_JWT_SECRET            >= 32 chars and different from SUPABASE_JWT_SECRET. Missing: device pairing, device
                                 tokens and the grant route answer 503 (eo.devices.NotConfigured). Never defaulted.
    GROQ_API_KEY[_N] | MISTRAL_API_KEY[_N] | OPENROUTER_API_KEY[_N]   at least one (the first provider set wins, as in
                                 hello.build_chain). Missing: the chain is empty and every turn answers
                                 `provider_unavailable`, pauses the run (paused_error) and sends no content.
    M2C_HELLO_MODEL              optional: names the model of that chain. Unset: the provider's default.
    M2C_RUN_CREDIT_CEILING       optional: the per-run ceiling in credits. Unset: 25% of the pool, floor 1 (S10).
    M2C_CONTEXT_WINDOW_TOKENS    optional: the model window the assembler budgets against. Unset or not a sane whole
                                 number: 100 000 (S08). It is not a secret, so a default is fine.
    M2C_LOOP                     optional: `hello` is the kill switch. Anything else is the loop.
    REDIS (Upstash) and the database settings the rest of the app already needs (the buffer, run_guard, the ledger).
Nothing else is read here. The usage ledger and the run counters fail OPEN (metering); everything above fails CLOSED.

LOGGING (build plan 11.4, O1). Run id, user id, setting NAMES (never values), mode words and exception TYPES. Never a
prompt, a reply or a tool argument (tests/static/test_wiring_logging.py reads this file).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool

from codeagent import broker as broker_module
from codeagent import buffer, context, hello, loop as loop_module, metering, run_handlers
from codeagent import emitter as emitter_module
from eo import credits as credits_module
from eo import devices, usage_ledger

logger = logging.getLogger(__name__)

ENV_MODE = "M2C_LOOP"
MODE_LOOP = "loop"
MODE_HELLO = "hello"
CAPABILITY_TOOLS = "tools"

NOTICE_REPLY_RESTARTED = loop_module.NOTICE_REPLY_RESTARTED
MSG_REPLY_RESTARTED = loop_module.MSG_REPLY_RESTARTED

# What configuration_problems() says, by setting. Fixed texts: no value ever goes into one.
_PROVIDER_SETTING = "GROQ_API_KEY / MISTRAL_API_KEY / OPENROUTER_API_KEY"
REASON_MISSING_OR_WEAK = "is not set, or is shorter than 32 characters"
REASON_SAME_AS_SUPABASE = "must differ from the Supabase JWT secret"
REASON_NO_PROVIDER = "no model provider key is set, so no model can answer"

# The run handlers' private hook lists, by the public accessor that reads them (see _discard).
_HOOK_ATTRS = (("on_cancel", "_on_cancel", "on_cancel_hooks"),
               ("on_resume", "_on_resume", "on_resume_hooks"),
               ("on_run_disconnect", "_on_run_disconnect", "on_run_disconnect_hooks"))

_BUILT: "loop_module.AgentLoop | None" = None      # the loop this module has installed (None: none, or hello mode)


class WiringError(RuntimeError):
    """The run handlers still hold a hook this module was asked to remove. Fixed text: no value in it."""


# ---------------------------------------------------------------------
# Small rules
# ---------------------------------------------------------------------

def selected_mode(environ=None) -> str:
    """`hello` when M2C_LOOP says so (case and surrounding spaces ignored), else `loop`: the default, and what an
    unknown value means (a typo must not silently select the walking skeleton, which has no tools)."""
    environ = os.environ if environ is None else environ
    value = environ.get(ENV_MODE)
    return MODE_HELLO if isinstance(value, str) and value.strip().lower() == MODE_HELLO else MODE_LOOP


def conn_offers_tools(conn) -> bool:
    """True only when the connection's hello declared the exact capability "tools". Anything else (no attribute, None,
    a string, another list) is False: a client is offered tools only when it said it can answer them."""
    capabilities = getattr(conn, "capabilities", None)
    if not isinstance(capabilities, (tuple, list, set, frozenset)):
        return False
    return any(isinstance(c, str) and c == CAPABILITY_TOOLS for c in capabilities)


def _strong(value) -> bool:
    return isinstance(value, str) and len(value.strip()) >= buffer.MIN_SECRET_CHARS


def configuration_problems(environ=None) -> "list[tuple[str, str]]":
    """[(setting name, fixed reason)] for every REQUIRED setting that is missing or unusable. Names and fixed texts
    only: a value, even a wrong one, is never put in the result. Empty when the real path is fully configured."""
    environ = os.environ if environ is None else environ
    problems = []
    if not _strong(environ.get("RUN_BUFFER_SECRET")):
        problems.append(("RUN_BUFFER_SECRET", REASON_MISSING_OR_WEAK))
    device = environ.get("DEVICE_JWT_SECRET")
    if not isinstance(device, str) or len(device) < devices.MIN_SECRET_LENGTH:
        problems.append(("DEVICE_JWT_SECRET", REASON_MISSING_OR_WEAK))
    elif device == environ.get("SUPABASE_JWT_SECRET"):
        problems.append(("DEVICE_JWT_SECRET", REASON_SAME_AS_SUPABASE))
    if not hello.build_chain(environ):
        problems.append((_PROVIDER_SETTING, REASON_NO_PROVIDER))
    return problems


def _keep_prompts_out_of_sdk_logs() -> None:
    """hello.install() held the openai SDK's logger at INFO or above, because its DEBUG line carries whole requests and
    the SDK turns DEBUG on by itself when OPENAI_LOG=debug is set. The loop path must keep that (O1). Never lowered."""
    sdk = logging.getLogger("openai")
    if sdk.getEffectiveLevel() < logging.INFO:
        sdk.setLevel(logging.INFO)


# ---------------------------------------------------------------------
# The loop, built from the real parts
# ---------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_loop(*, environ=None, chain=None, assembler=None, config=None, check=None, commit=None, credits=None,
               progress=None, ledger_sum=None, store=None, policy=None, **loop_kw) -> "loop_module.AgentLoop":
    """The process loop. Every argument is an override for a test; with none the parts are the real ones. Nothing is
    installed or remembered here (install() does that).

    environ      where the model chain, the window and the run ceiling are read (default: the process environment)
    chain        the model chain (default: loop.default_chain(environ))
    assembler    S08's BudgetedContextAssembler (default: one, window from M2C_CONTEXT_WINDOW_TOKENS)
    check, commit, credits, progress, ledger_sum, store, policy      S10's make_metering_factory collaborators.
                 The defaults look the real function up AT CALL TIME (usage_ledger.check, usage_ledger.commit,
                 credits_for), so a replaced one is the one used; the emit function is the process emitter's, also late.
    loop_kw      anything else AgentLoop takes (adapter, config, tool_budget, ...)"""
    environ = os.environ if environ is None else environ

    async def emit(run_id, t, payload):
        return await emitter_module.get_emitter().emit(run_id, t, payload)

    async def emit_final(run_id, to_state, why=None):
        return await emitter_module.get_emitter().emit_final(run_id, to_state, why)

    def unbind(run_id):
        emitter_module.get_emitter().unbind(run_id)

    def real_check(user_id):
        return usage_ledger.check(user_id, now=_utcnow())

    def real_commit(**row):
        return usage_ledger.commit(**row)

    def real_credits(provider, model, tokens_in, tokens_cached, tokens_out):
        return credits_module.credits_for(provider, model, tokens_in, tokens_cached, tokens_out)

    factory = metering.make_metering_factory(
        emit=emit, check=check or real_check, commit=commit or real_commit, credits=credits or real_credits,
        progress=progress, ledger_sum=ledger_sum, store=store,
        policy=policy or metering.RunCeilingPolicy(environ=environ))
    return loop_module.AgentLoop(
        chain=loop_module.default_chain(environ) if chain is None else chain,
        broker=broker_module.get_broker(), emit=emit, emit_final=emit_final, unbind=unbind,
        assembler=assembler if assembler is not None else context.BudgetedContextAssembler(environ=environ),
        metering_factory=factory, offer_tools=conn_offers_tools,
        **({"config": config} if config is not None else {}), **loop_kw)


# ---------------------------------------------------------------------
# The restart notice (what hello's resume hook said, and the loop's does not)
# ---------------------------------------------------------------------

async def _partial_text_was_sent(run_id: str) -> bool:
    """True when the run's replay shows text streamed that no text_done closed: the last text event, going backwards,
    is a text_delta. State changes, usage and tool events in between do not count either way."""
    events = await run_in_threadpool(buffer.replay_after, run_id, 0)
    for message in reversed(events):
        payload = message.get("payload") or {}
        kind = payload.get("type") if message.get("t") == "run_event" else None
        if kind == "text_delta":
            return True
        if kind == "text_done":
            return False
    return False


async def _announce_restart(conn, run) -> None:
    """run_handlers on_resume hook, registered BEFORE the loop's: if the conversation ends where the model must answer
    next (a user message or a tool result) and a reply was left half written, tell the device to drop the partial text.
    Hooks run in order and the loop's only starts a task, so the notice is stored before the new reply's first delta.
    Fails quietly: a missing notice costs the client a doubled sentence, never the resume."""
    try:
        conversation = await run_in_threadpool(buffer.read_conversation, run.id)
        items = conversation.items
        if not items or items[-1].get("role") not in ("user", "tool"):
            return
        if not await _partial_text_was_sent(run.id):
            return
        await emitter_module.get_emitter().emit(run.id, "run_event", {
            "type": "notice", "code": NOTICE_REPLY_RESTARTED, "severity": "info", "message": MSG_REPLY_RESTARTED})
    except Exception as exc:
        logger.warning("[wiring] restart notice not sent run=%s user=%s error=%s", run.id, run.user_id,
                       type(exc).__name__)


# ---------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------

def _discard(handlers, pairs) -> None:
    """Take hooks out of the run handlers. RunHandlers has a public add_* and read accessors but no remove, and this
    patch does not edit run_handlers.py, so the private lists are edited here, by identity, and the result is checked
    through the PUBLIC accessors: a hook that is still there raises WiringError instead of running twice."""
    for _name, attr, accessor in _HOOK_ATTRS:
        for hook_name, fn in pairs:
            if hook_name != _name or fn is None:
                continue
            hooks = getattr(handlers, attr, None)
            if isinstance(hooks, list):
                hooks[:] = [h for h in hooks if h is not fn]
            if any(h is fn for h in getattr(handlers, accessor)()):
                raise WiringError("a hook of the other turn runner is still registered")


def _hello_pairs() -> list:
    return [("on_cancel", hello._on_cancel), ("on_resume", hello._on_resume),
            ("on_run_disconnect", hello._on_run_disconnect)]


def _loop_pairs(active) -> list:
    if active is None:
        return []
    return [(name, fn) for name, fn in active.hooks.items()] + [("on_resume", _announce_restart)]


def install(*, loop: "loop_module.AgentLoop | None" = None):
    """Install the turn runner the settings select, on the process run handlers. Returns the loop (loop mode) or None
    (hello mode). Idempotent: a second call changes nothing. An explicit `loop` (tests) becomes the process loop.
    Never raises for a missing setting (module docstring, CONFIGURATION)."""
    global _BUILT
    handlers = run_handlers.get_handlers()
    environ = os.environ
    raw = environ.get(ENV_MODE)
    mode = selected_mode(environ)
    if isinstance(raw, str) and raw.strip() and raw.strip().lower() not in (MODE_LOOP, MODE_HELLO):
        logger.warning("[wiring] %s has an unknown value, using the %s", ENV_MODE, MODE_LOOP)
    for name, reason in configuration_problems(environ):
        logger.warning("[wiring] setting %s: %s", name, reason)
    _keep_prompts_out_of_sdk_logs()

    stale = [_BUILT, loop_module._LOOP]                      # whichever loops may have hooks registered
    if mode == MODE_HELLO:
        for old in stale:
            _discard(handlers, _loop_pairs(old))
        _BUILT = None
        handlers.set_turn_runner(hello.run_turn, replace=True)
        handlers.add_on_cancel(hello._on_cancel)
        handlers.add_on_resume(hello._on_resume)
        handlers.add_on_run_disconnect(hello._on_run_disconnect)
        logger.info("[wiring] turn runner installed: %s", MODE_HELLO)
        return None

    active = loop if loop is not None else (_BUILT if _BUILT is not None else build_loop())
    for old in stale:
        if old is not None and old is not active:
            _discard(handlers, _loop_pairs(old))
    _discard(handlers, _hello_pairs())
    _BUILT = active
    loop_module.set_loop(active)
    handlers.add_on_resume(_announce_restart)                # before the loop's hook: the notice precedes the reply
    loop_module.install(replace=True, handlers=handlers, loop=active)
    logger.info("[wiring] turn runner installed: %s", MODE_LOOP)
    return active
