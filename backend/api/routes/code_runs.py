"""
api/routes/code_runs.py -- Patch S10: "grant more" for an M2C run that stopped at its credit ceiling.

    POST /v1/code/runs/{run_id}/grant        Authorization: Bearer <device JWT>        (no body)
         -> 200 {state: "running", run_credit_ceiling, run_credits_spent, granted_credits}

WHO CALLS IT. The device, with the same short-lived device JWT it gets from POST /v1/code/devices/token (S03). The
terminal shows the user "this run reached its credit limit; grant more?", and on a yes it calls this route. The web
app's session tokens are not accepted here: only a token whose type is `device` and whose device is live (the
verification, eo.devices.verify_device_token, checks both and that the device was not revoked).

WHAT A GRANT IS. A run in `paused_budget` goes back to `running` with a raised per-run ceiling (one chunk more than it
had already spent: codeagent/metering.py's RunCeilingPolicy decides how big a chunk is) and its turn is restarted. It
does NOT raise the account's pool: the pool is the account's plan, shared with the web app, and is only ever read here.
A grant while the pool is empty is refused (`quota_exhausted`, with the reset time) and changes nothing.

THE ORDER (every step before the claim only reads; every step after it is undone if it fails)
    1  the token                      401 (503 when the device store cannot be asked: fail closed)
    2  the run is this account's AND this device's     404 run_not_found, the same body as a run that does not exist
    3  the run is in paused_budget    409 not_paused_budget {state}
    4  the device is connected        409 device_offline
    5  the pool has room              409 quota_exhausted {reset_at}; 503 usage_unavailable when it cannot be read
    6  the run's buffer exists        409 buffer_gone
    7  the numbers                    the run's spent credits and the ceiling in force; the new ceiling
    8  CLAIM: runs.transition(paused_budget -> running, expected=paused_budget, max_parallel=cap)
                                      409 not_paused_budget when someone else got there first; 429 rate_limited at the cap
    9  the raised ceiling is written to Redis (RunBudgetStore); run_guard.grant_more clears the breaker and cancel flag
    10 the emitter is bound again (the loop unbound it when it paused) and `state_changed running / budget_granted` is sent
    11 the on_resume hooks restart the turn, as a task (the response does not wait for the model)
  Steps 9 and 10 failing put the run back to paused_budget and restore the previous ceiling (503 grant_unavailable): the
  device can simply ask again. A failing hook costs the restart only (RunHandlers.call_hooks logs it); the run is
  `running`, announced, and the device can send a message to carry on.

WHY THE CLAIM IS STEP 8. Two requests for the same pause (a double tap, two terminals) must raise the ceiling once.
runs.transition is an atomic compare-and-set on the run's state: exactly one request moves the run, the other sees
`running` and is refused, and nothing else is written before the claim. A cheap check at step 3 refuses the usual case
early; the claim makes it exact.

WHY runs.transition AND NOT run_manager. run_manager.resume_run deliberately resumes only from paused_disconnected and
paused_error (RESUME_FROM): leaving paused_budget is this route's job, and run_manager has no call for it. paused_budget
-> running is a legal edge of runs.TRANSITIONS, and transition() applies the device's parallel-run cap itself.

RESPONSES CARRY NUMBERS AND STATE WORDS ONLY. Error bodies are {code, message[, state | reset_at | retry_after_s]}: the
message is a fixed text, the rest a state word, a timestamp or a number. Nothing the device sent (the token, a path)
and no exception text is ever echoed, logged or put in a body. Logs hold run, user and device ids, credit numbers and
state words (tests/static/test_metering_static.py reads this file).
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from codeagent import buffer, connections, metering
from codeagent import emitter as emitter_module
from codeagent import run_handlers, run_manager, runs
from eo import devices, run_guard, usage_ledger

logger = logging.getLogger(__name__)

router = APIRouter()

STATE_PAUSED = "paused_budget"
REASON_GRANTED = "budget_granted"             # state_changed.reason of the announcement (pattern ^[a-z][a-z0-9_]{0,63}$)
ERROR_QUOTA = "quota_exhausted"
RETRY_AFTER_SECONDS = 5                       # when the device's parallel-run cap was hit

MSG_UNAUTHORIZED = "A valid device token is required."
MSG_UNAVAILABLE = "Device sign-in is temporarily unavailable. Try again shortly."
MSG_RUN_NOT_FOUND = "Run not found."
MSG_NOT_PAUSED = "This run is not waiting for more credits."
MSG_OFFLINE = "This device is not connected right now."
MSG_POOL_EXHAUSTED = "Your credits for this period are used up."
MSG_USAGE_UNAVAILABLE = "Your credit balance cannot be read right now. Try again shortly."
MSG_BUFFER_GONE = "This run can no longer be continued."
MSG_TOO_MANY_RUNS = "Too many runs are active on this device."
MSG_GRANT_UNAVAILABLE = "The grant could not be applied. Nothing was changed; try again shortly."

_background: set = set()                      # restart tasks in flight (a task nobody holds can be collected mid-run)


# ---------------------------------------------------------------------
# What the route needs, swappable by tests
# ---------------------------------------------------------------------

@dataclass
class GrantServices:
    """The route's collaborators. `registry`, `handlers` and `emitter` are callables, looked up at each request (like the
    rest of the codebase does), so one swapped in later is the one used."""
    check: Callable                # (user_id) -> usage_ledger.CheckResult
    run_credits: Callable          # (user_id, run_id) -> credits the ledger holds for the run
    store: metering.RunBudgetStore
    policy: metering.RunCeilingPolicy
    registry: Callable             # () -> connections.ConnectionRegistry
    handlers: Callable             # () -> run_handlers.RunHandlers
    emitter: Callable              # () -> emitter.RunEmitter


def get_services() -> GrantServices:
    """The real collaborators (the dependency a test overrides)."""
    return GrantServices(
        check=lambda user_id: usage_ledger.check(user_id, now=metering._utcnow()),
        run_credits=metering.ledger_run_credits,
        store=metering.RunBudgetStore(),
        policy=metering.RunCeilingPolicy(),
        registry=connections.get_registry,
        handlers=run_handlers.get_handlers,
        emitter=emitter_module.get_emitter)


class GrantResponse(BaseModel):
    state: str
    run_credit_ceiling: float
    run_credits_spent: float
    granted_credits: float


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

def _fail(status: int, code: str, message: str, *, headers: "dict | None" = None, **numbers) -> HTTPException:
    """An error response: a code, a fixed message and (optionally) state words or numbers. Never anything else."""
    return HTTPException(status_code=status, detail={"code": code, "message": message, **numbers}, headers=headers)


async def _thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _spawn(work) -> None:
    task = asyncio.ensure_future(work)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _bearer(authorization: "str | None") -> "str | None":
    if not isinstance(authorization, str):
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


async def require_device(authorization: Optional[str] = Header(default=None)) -> "devices.DeviceIdentity":
    """The device behind a valid device JWT, else 401. A fault while checking (the devices store is down) is a 503:
    nothing is allowed on a guess."""
    unauthorized = _fail(401, "unauthorized", MSG_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    token = _bearer(authorization)
    if token is None:
        raise unauthorized
    try:
        return await _thread(devices.verify_device_token, token)
    except devices.DeviceAuthError:
        raise unauthorized from None
    except Exception as exc:
        logger.error("[code_runs] device token could not be checked error=%s", type(exc).__name__)
        raise _fail(503, "auth_unavailable", MSG_UNAVAILABLE) from None


# ---------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------

@router.post("/v1/code/runs/{run_id}/grant", response_model=GrantResponse)
async def grant_run_credits(run_id: str, device=Depends(require_device), services: GrantServices = Depends(get_services)):
    try:
        return await _grant(run_id, device, services)
    except HTTPException:
        raise
    except Exception as exc:                              # anything unforeseen before the claim: nothing was changed
        logger.error("[code_runs] grant failed run=%s user=%s device=%s error=%s",
                     run_id[:64], device.user_id, device.device_id, type(exc).__name__)
        raise _fail(503, "grant_unavailable", MSG_GRANT_UNAVAILABLE) from None


async def _own_run(user_id: str, device_id: str, run_id: str) -> "runs.Run":
    """The run, when it belongs to this account AND this device. A missing run, another account's, another device's and a
    malformed id are one and the same 404."""
    missing = _fail(404, "run_not_found", MSG_RUN_NOT_FOUND)
    try:
        run = await _thread(runs.get_run, user_id, run_id)
    except (runs.RunNotFound, runs.InvalidRun):
        raise missing from None
    except runs.RunStoreUnavailable:
        raise _fail(503, "grant_unavailable", MSG_GRANT_UNAVAILABLE) from None
    if str(run.device_id).strip().lower() != str(device_id).strip().lower():
        raise missing
    return run


async def _pool_with_room(services: GrantServices, user_id: str):
    """The ledger's check, which must be readable AND show room. Unlike metering (which fails open), a grant fails
    closed: nothing is raised on a pool that could not be read."""
    try:
        result = await _thread(services.check, user_id)
    except Exception as exc:
        logger.warning("[code_runs] pool check failed user=%s error=%s", user_id, type(exc).__name__)
        raise _fail(503, "usage_unavailable", MSG_USAGE_UNAVAILABLE) from None
    if result is None or getattr(result, "degraded", False):
        logger.warning("[code_runs] pool could not be read user=%s", user_id)
        raise _fail(503, "usage_unavailable", MSG_USAGE_UNAVAILABLE)
    if not result.allowed:
        reset_at = metering._timestamp(getattr(result, "resets_at", None))
        logger.info("[code_runs] grant refused, pool exhausted user=%s", user_id)
        raise _fail(409, ERROR_QUOTA, MSG_POOL_EXHAUSTED, **({"reset_at": reset_at} if reset_at else {}))
    return result


async def _grant(run_id: str, device, services: GrantServices) -> GrantResponse:
    user_id, device_id = device.user_id, device.device_id

    run = await _own_run(user_id, device_id, run_id)                                          # 2
    if run.state != STATE_PAUSED:                                                             # 3
        raise _fail(409, "not_paused_budget", MSG_NOT_PAUSED, state=run.state)
    conn = services.registry().get(user_id, device_id)                                        # 4
    if conn is None or getattr(conn, "closed", False):
        raise _fail(409, "device_offline", MSG_OFFLINE)
    pool = await _pool_with_room(services, user_id)                                           # 5
    if not await _thread(buffer.exists, run.id):                                              # 6
        raise _fail(409, "buffer_gone", MSG_BUFFER_GONE)

    # 7: the numbers. The ledger's figure counts when it is larger than the run row's counter (that counter is
    # informational); a ceiling the run already had is raised, never lowered.
    spent = max(metering._credits(run.credits), metering._credits(await _thread(services.run_credits, user_id, run.id)))
    previous = await _thread(services.store.ceiling, run.id)
    known = [v for v in (services.policy.base(pool.pool), previous) if v is not None]
    granted = services.policy.chunk(pool.pool)
    new_ceiling = round(max(max(known) if known else 0.0, spent) + granted, 6)

    handlers = services.handlers()
    try:                                                                                      # 8: the claim
        moved = await _thread(runs.transition, user_id, run.id, "running", expected=(STATE_PAUSED,),
                              max_parallel=handlers.max_parallel)
    except runs.IllegalTransition as exc:
        raise _fail(409, "not_paused_budget", MSG_NOT_PAUSED, state=exc.current) from None
    except runs.ParallelRunLimit:
        raise _fail(429, "rate_limited", MSG_TOO_MANY_RUNS, headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
                    retry_after_s=RETRY_AFTER_SECONDS) from None
    except (runs.RunNotFound, runs.InvalidRun):
        raise _fail(404, "run_not_found", MSG_RUN_NOT_FOUND) from None
    except runs.RunError:
        raise _fail(503, "grant_unavailable", MSG_GRANT_UNAVAILABLE) from None

    emitter = services.emitter()
    was_bound = emitter.is_bound(run.id)
    try:                                                                                      # 9, 10
        await _thread(services.store.set_ceiling, run.id, new_ceiling)
        await _thread(run_guard.grant_more, run.id)
        emitter.bind(run.id, user_id, device_id, last_seq=0)      # the counter is the buffer's: the first emit follows it
        await emitter.emit_state_changed(run.id, "running", REASON_GRANTED)
    except Exception as exc:
        logger.error("[code_runs] grant not applied, putting the run back run=%s user=%s device=%s error=%s",
                     run.id, user_id, device_id, type(exc).__name__)
        await _undo(services, emitter, run, previous, was_bound)
        raise _fail(503, "grant_unavailable", MSG_GRANT_UNAVAILABLE) from None

    logger.info("[code_runs] grant applied run=%s user=%s device=%s spent=%s ceiling=%s granted=%s",
                run.id, user_id, device_id, spent, new_ceiling, granted)
    _spawn(handlers.call_hooks(handlers.on_resume_hooks(), conn, moved, "grant"))             # 11
    return GrantResponse(state="running", run_credit_ceiling=new_ceiling, run_credits_spent=spent,
                         granted_credits=granted)


async def _undo(services: GrantServices, emitter, run: "runs.Run", previous: "float | None", was_bound: bool) -> None:
    """A grant failed after the claim: restore the ceiling, detach what this request attached, put the run back to
    paused_budget so the device can ask again. Every step is tried even if an earlier one failed."""
    try:
        await _thread(services.store.restore_ceiling, run.id, previous)
    except Exception as exc:
        logger.error("[code_runs] could not restore the ceiling run=%s error=%s", run.id, type(exc).__name__)
    if not was_bound:
        emitter.unbind(run.id)
    try:
        await _thread(run_manager.pause_run, run.user_id, run.id, STATE_PAUSED, error_code=ERROR_QUOTA)
    except Exception as exc:
        logger.error("[code_runs] could not put the run back to %s run=%s user=%s error=%s",
                     STATE_PAUSED, run.id, run.user_id, type(exc).__name__)
