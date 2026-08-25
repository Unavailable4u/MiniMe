"""
eo/concurrency_gate.py — Phase 4 of the reliability overhaul (see
reliability_overhaul_plan.md §PHASE 4, Patch B).

Patch A (utils/rate_ledger.py's reserve()/release_reservation()) closed
the race INSIDE a single gated LLM call: can_proceed() used to only read,
so two concurrent callers could both see "headroom free" before either
one's usage got booked. reserve() folds the check and the provisional
booking into one call, shrinking that race to one bus round trip.

This module is the layer above that: the fan-out sites themselves
(agents/hardware_speccer.py's _populate_prices(), eo/executor.py's
_run_concurrent_group()) don't currently ask the ledger anything before
deciding how many threads to start — they size a ThreadPoolExecutor to a
FIXED count (len(key_envs), len(group_roles), ...) and let every thread
race into its own call, each individually discovering via Patch A's
reserve() that there wasn't room after all, and each individually
sleeping/rerouting inside utils/llm_client.py's retry loop. That still
converges (Patch A prevents any of them from overshooting the ledger),
but it wastes a burst of started-then-blocked threads, adds pointless
per-thread sleep/retry latency, and gives the pool no way to adapt when
headroom frees up mid-run — the exact "7 parallel part_price_finder
workers all pile onto whichever accounts they were handed at t=0,
whether or not those accounts have room" scenario named in the incident
log this phase traces back to.

run_gated() is the fix: it looks at each task's designated ledger
candidate BEFORE starting its thread, only actually submits the ones
that currently have room (calling reserve() on their behalf, right here,
not deep inside llm_client.py), and re-evaluates the still-queued tasks
the instant an in-flight one finishes and releases its slot — so the
real concurrency in flight at any moment tracks live ledger headroom
instead of a number picked once at task start.

This deliberately does NOT replace Patch A. A task admitted here still
goes through its own normal utils/llm_client.py call chain, which still
calls rate_ledger.reserve()/release_reservation() per individual LLM
step the way Patch A wired it. The reservation this module makes is a
coarser, OUTER one — "is there room to let this candidate account start
ANOTHER concurrent attempt right now" — sized as exactly 1 unit per
task regardless of gating mode (see GatedTask's docstring), not an
attempt to pre-book the real token cost of whatever call the task ends
up making internally; that finer-grained accounting is already Patch
A's job, one layer down.
"""
import os
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock, Thread

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import rate_ledger

# Sane upper bound on real OS threads a single run_gated() call will ever
# spin up, independent of how many tasks are handed in. Actual PARALLELISM
# in flight is governed by reserve() succeeding per candidate, not by
# this number -- this just stops a pathologically large `tasks` list from
# creating one thread per item up front. Every existing call site this
# phase targets (7-worker price lookups, small role groups) sits well
# under this already.
_MAX_POOL_WORKERS = 32

# Admission-loop poll floor/ceiling. A just-released slot wakes the loop
# immediately via the Event below, so this timeout is a safety net for
# the "nothing is in flight to release anything, but a rate-limit window
# is about to roll over" case -- reserve()'s own suggested wait_seconds
# per blocked candidate drives the actual sleep, clamped to this range so
# the loop never busy-spins on a near-zero wait nor oversleeps past a
# short one.
_POLL_MIN_SECONDS = 0.25
_POLL_MAX_SECONDS = 5.0


class GatedTask:
    """One unit of work for run_gated().

    call: zero-argument callable that does the real work (build this as
        a closure/functools.partial over whatever the caller's own
        per-item function needs -- run_gated() never inspects or calls
        it with arguments, matching how each existing call site already
        closes over its own item/key_env/worker_id today).

    step: a generate_text()-shaped chain step dict -- {"provider":,
        "model":, "key_env": ...} or the cloudflare shape ({"provider":
        "cloudflare", "model":, "account_id_env":, "token_env": ...}),
        e.g. from eo/dynamic_chain.py's chain_step_for() or the first
        entry of a build_fallback_chain_excluding() result. This is
        WHICH candidate account/model this task's dispatch should be
        gated against. None means "nothing to gate on" (e.g. no tagged
        accounts resolved at all, agents/hardware_speccer.py's own
        existing degrade-to-ungated-fallback path) -- the task is
        admitted immediately, same as before this patch.

    label: optional, for logging only.
    """
    __slots__ = ("call", "label", "step")

    def __init__(self, call, step: "dict | None" = None, label: str = None):
        self.call = call
        self.step = step
        self.label = label


def _candidate_identity(step: dict) -> "tuple[str, str, str]":
    """(provider, key, model) for a chain step dict, handling the
    cloudflare shape the same way utils/llm_client.py's own call sites
    already do -- key_id (account_id_env) for cloudflare, key_env for
    everything else. Mirrors _ledger_gate()'s own `key` convention so a
    reservation made here and one made moments later inside the task's
    own llm_client.py call chain address the same (provider, key_id,
    model) triple in rate_ledger, even though they're two separate
    reservations at two different layers (see module docstring)."""
    provider = step["provider"]
    model = step.get("model", "")
    key = step.get("account_id_env") or step.get("key_env")
    return provider, key, model


def _gating_mode_for_step(step: "dict | None") -> "str | None":
    """Best-effort gating-mode lookup for a chain step, shared by
    _outer_reservation_size() and _release() so both agree on whether a
    given step's outer reservation is "neurons" mode. Returns None (never
    raises) on step=None, an unrecognized step shape, or `rate_ledger`
    having been swapped for a test double that doesn't implement the
    private _gating_mode_for() this reaches into (see
    tests/integration/test_reliability_overhaul_gate.py's MockLedger,
    which stands in for the whole `rate_ledger` reference and only
    implements reserve()/release_reservation()) -- callers treat None
    the same as "tokens"/"requests" (the pre-existing, already-correct
    behavior for those modes), so a lookup failure degrades to exactly
    what this module did before this fix, never to something worse."""
    if step is None:
        return None
    try:
        provider, _key, model = _candidate_identity(step)
        return rate_ledger._gating_mode_for(provider, model)
    except Exception:
        return None


def _outer_reservation_size(step: dict) -> "tuple[int, int | None]":
    """Bug fix: what _admit() should actually reserve at this outer
    layer. Returns (estimated_units, max_output_tokens) for
    rate_ledger.reserve().

    For every mode except "neurons" this is (1, None) -- completely
    unchanged from before this fix; see _admit()'s docstring for why 1
    unit is the deliberately-approximate placeholder for those modes
    (Patch A's own reserve() call, one layer down inside the task's
    actual LLM call, books the real estimate).

    "neurons" mode has no such placeholder available. reserve()'s own
    docstring is explicit that `estimated_units` is the real INPUT-side
    token estimate and `max_output_tokens` the real output ceiling in
    THIS mode specifically -- there's no "cost of exactly 1" convention
    the way "requests" mode genuinely has. Reserving the same literal 1
    every other mode uses meant _estimate_neurons() was handed (1 input
    token, no output ceiling) on every single admission check,
    regardless of the step's real size -- producing a fraction-of-a-
    neuron estimate that reserve() could essentially never refuse, so
    this outer throttle provided NO real admission control for
    Cloudflare accounts, no matter how close to their real daily neuron
    budget they actually were (confirmed by tracing _estimate_neurons()
    and _reserve_neurons_mode() directly).

    Fix: for "neurons" mode only, resolve the step's real max_tokens
    ceiling via utils.llm_client._max_tokens_for() -- the exact same
    lookup the step's own eventual dispatch already uses (see
    llm_client.py's _remaining_chain_headroom(), same function) -- and
    reserve using that ceiling on both sides. This mirrors
    _estimate_neurons()'s own documented worst-case convention
    (deliberately book the ceiling, not a guess at the real length)
    instead of inventing a second, divergent heuristic here."""
    if _gating_mode_for_step(step) != "neurons":
        return 1, None
    try:
        from utils.llm_client import _max_tokens_for
        _provider, _key, model = _candidate_identity(step)
        ceiling = _max_tokens_for(model, step)
    except Exception:
        return 1, None
    if not ceiling:
        return 1, None
    return ceiling, ceiling


def _admit(task: GatedTask) -> "tuple[bool, float, str | None]":
    """Tries to reserve this task's outer dispatch slot. See
    _outer_reservation_size() for what's actually booked and why it
    differs for "neurons" mode. A step of None always admits immediately
    with no reservation (nothing to release later either)."""
    if task.step is None:
        return True, 0.0, None
    provider, key, model = _candidate_identity(task.step)
    estimated_units, max_output_tokens = _outer_reservation_size(task.step)
    if max_output_tokens is None:
        return rate_ledger.reserve(provider, key, model, estimated_units)
    return rate_ledger.reserve(provider, key, model, estimated_units,
                                max_output_tokens=max_output_tokens)


def _release(task: GatedTask, reservation_id: "str | None") -> None:
    """Settles (not rolls back) the outer slot once the task's call()
    has finished, success or exception -- the dispatch genuinely
    happened (a real attempt against this account was made and is now
    done), so this confirms the reservation as final rather than a full
    rollback, which is reserved for a step that was never dispatched in
    the first place (see _run_one()'s cancelled-future branch below).

    Bug fix, "neurons" mode: every other mode's outer reservation is a
    deliberately-approximate placeholder that settling correctly leaves
    (at most) negligibly booked (1 real token/request, matching
    _outer_reservation_size()'s own docstring). "neurons" mode's outer
    reservation is NOT negligible -- it now books a real, worst-case-
    ceiling-sized estimate (see above) purely so admission control
    actually works. That estimate was never meant to be a second,
    independent record of real usage: the task's own dispatch, one
    layer down inside utils/llm_client.py, already makes its OWN
    reserve()/release_reservation() call against the same (provider,
    key_id) neuron budget and trues THAT one up against real usage (see
    release_reservation()'s own "neurons" branch). Settling this outer
    reservation the way every other mode's placeholder settles would
    leave its full worst-case estimate permanently booked ON TOP OF the
    inner layer's real, trued-up figure -- double-counting every single
    dispatched Cloudflare call against the account's real daily budget,
    exhausting it roughly twice as fast as reality. Rolling it back to
    zero instead leaves the inner layer's own reservation as the one
    real record, exactly matching this module's own stated intent (the
    outer layer governs admission only; Patch A does real accounting).

    Falls back to the pre-fix, no-`dispatched`-kwarg call on TypeError
    so a `rate_ledger` stand-in that only implements the older
    reserve()/release_reservation(reservation_id, actual_units) shape
    (see tests/integration/test_reliability_overhaul_gate.py's
    MockLedger) still works unchanged."""
    if reservation_id is None:
        return
    if _gating_mode_for_step(task.step) == "neurons":
        try:
            rate_ledger.release_reservation(reservation_id, dispatched=False)
        except TypeError:
            rate_ledger.release_reservation(reservation_id)
        return
    rate_ledger.release_reservation(reservation_id, actual_units=1)


def run_gated(tasks: "list[GatedTask]", session_id: str = None) -> "list[Future]":
    """Drop-in replacement for a raw `with ThreadPoolExecutor(...) as
    executor: futures = [executor.submit(...) for ...]` block. Returns a
    list of concurrent.futures.Future objects, one per task, in the SAME
    order as `tasks` -- ordinary Future objects, so an existing call
    site's `as_completed(futures)` / `future.result()` pattern keeps
    working completely unchanged; only how each future gets STARTED
    changes. Like executor.submit(), this returns immediately -- the
    actual admission/dispatch work happens on a background thread, so a
    caller iterating as_completed(futures) can start consuming
    early-finishing results while later, still-throttled tasks are
    still waiting on the ledger, exactly as it would with a real
    ThreadPoolExecutor.

    Unlike executor.submit(), which starts a task's thread immediately,
    the futures returned here start running only once the background
    admission loop has confirmed (via rate_ledger.reserve(), see
    _admit()) that their designated candidate (task.step) currently has
    room for one more concurrent attempt. Tasks with step=None skip
    that check entirely and are admitted as soon as a pool thread is
    free, same as a plain ThreadPoolExecutor would.

    The admission loop re-scans the still-queued tasks every time an
    in-flight one finishes (its done-callback both releases its outer
    reservation via _release() and wakes the loop), so a slot freed by
    an early finisher is immediately available to a still-queued task
    instead of waiting for a fixed poll interval -- "computes how many
    can dispatch right now... re-checks as slots free up" (Patch B's
    own spec) rather than sizing the pool once at task start.

    session_id is accepted for parity with other role-tag-driven
    functions in this codebase (worker_pool._select_workers(),
    dynamic_chain.build_fallback_chain()) and reserved for future
    observability (an emit_event() call here, matching
    worker_pool_selection) -- not otherwise used yet.
    """
    if not tasks:
        return []

    futures: list[Future] = [Future() for _ in tasks]
    pending = list(range(len(tasks)))  # indices into tasks/futures still waiting on admission
    pending_lock = Lock()
    wake = Event()
    pool = ThreadPoolExecutor(max_workers=min(max(len(tasks), 1), _MAX_POOL_WORKERS))

    def _run_one(idx: int, reservation_id: "str | None") -> None:
        fut = futures[idx]
        if not fut.set_running_or_notify_cancel():
            # Caller cancelled this future before it ever ran -- the
            # task genuinely never dispatched, so this is a full
            # rollback, not _release()'s "settle" case, which is for a
            # task that DID run (successfully or not) and so DID use its
            # reserved slot.
            #
            # Bug fix: this used to call release_reservation(reservation_id)
            # with no other args, which meant "full rollback" back when
            # release_reservation()'s only rollback signal was
            # actual_units=None. That's no longer true -- it now defaults
            # to dispatched=True (settle), so this call was silently
            # SETTLING a cancelled task's reservation instead of rolling
            # it back, leaving it permanently occupying ledger budget it
            # never used. dispatched=False is the real, current rollback
            # signal (see release_reservation()'s own "Bug fix" docstring
            # section). Falls back to the old no-kwarg call on TypeError
            # for a `rate_ledger` stand-in that only implements the older
            # shape (see MockLedger in
            # tests/integration/test_reliability_overhaul_gate.py).
            try:
                rate_ledger.release_reservation(reservation_id, dispatched=False)
            except TypeError:
                rate_ledger.release_reservation(reservation_id)
            return
        try:
            result = tasks[idx].call()
        except BaseException as exc:  # noqa: BLE001 -- mirror Future's own "capture any exception" contract
            _release(tasks[idx], reservation_id)
            fut.set_exception(exc)
        else:
            _release(tasks[idx], reservation_id)
            fut.set_result(result)
        finally:
            wake.set()  # a slot just freed -- let the admission loop re-scan immediately

    def _admission_loop() -> None:
        try:
            while True:
                with pending_lock:
                    if not pending:
                        return
                    still_waiting = []
                    soonest_wait = _POLL_MAX_SECONDS
                    admitted_any = False
                    for idx in pending:
                        ok, wait, reservation_id = _admit(tasks[idx])
                        if ok:
                            admitted_any = True
                            pool.submit(_run_one, idx, reservation_id)
                        else:
                            still_waiting.append(idx)
                            soonest_wait = min(soonest_wait, wait)
                    pending[:] = still_waiting
                    if not pending:
                        return
                if not admitted_any:
                    wake.clear()
                    wake.wait(timeout=max(_POLL_MIN_SECONDS, min(soonest_wait, _POLL_MAX_SECONDS)))
        finally:
            # Don't wait for in-flight tasks here (shutdown(wait=False))
            # -- the caller already blocks on the returned futures
            # itself (via as_completed()/future.result()). The pool
            # object has no other referrers once every task has been
            # submitted, so its threads simply finish their current
            # task and exit; not waiting here just keeps this
            # background thread from lingering past the point its own
            # job (admitting everything) is done.
            pool.shutdown(wait=False)

    Thread(target=_admission_loop, name="concurrency_gate-admission", daemon=True).start()
    return futures
