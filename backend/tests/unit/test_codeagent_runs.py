"""S06a: the run record and its state machine (codeagent/runs.py).

Everything here runs against tests/unit/runs_testkit.FakeRunStore, an in-memory
twin of the Postgres store, so no database is needed. The SQL, the trigger and the
policies are exercised against a real database by tests/integration/test_runs_pg.py;
the migration text is checked by tests/static/test_runs_migration.py.

What is pinned:
  * the transition table, written out in full, and its shape (no self loops, terminal
    states final, every state reachable, every open state can still end);
  * every one of the 10 x 10 (from, to) pairs: legal exactly when the table says so;
  * the run record: id / device / profile / mode validation, collisions, per-user
    isolation, error codes (codes, never messages);
  * races: N callers cancelling at once -> exactly one winner; a state that changes
    between the read and the conditional update is re-read, not overwritten; churn
    that never settles ends in TransitionConflict, not in a lost update;
  * the parallel-run cap (welcome.limits.max_parallel_runs, ws.py), including under
    a race and for a resume from a paused state;
  * fail closed: a store fault surfaces as RunStoreUnavailable, with fixed text,
    no chained cause, and nothing but the exception's TYPE in the log.
"""
import json
import logging
import pathlib
import re
import threading

import pytest

from codeagent import runs
from tests.unit.runs_testkit import (
    CANARY, DEVICE, OTHER_DEVICE, OTHER_USER, USER, BoomStore, Clock, FakeRunStore, new_run_id,
)

REPO = pathlib.Path(__file__).resolve().parents[3]

# The table, written out a second time on purpose: changing the machine means editing
# runs.py AND this, and the diff shows both.
EXPECTED_TRANSITIONS = {
    "created": {"running", "cancelled", "failed", "expired"},
    "running": {"awaiting_approval", "completed", "paused_budget", "paused_disconnected",
                "paused_error", "cancelled", "failed", "expired"},
    "awaiting_approval": {"running", "paused_budget", "paused_disconnected", "paused_error",
                          "cancelled", "failed", "expired"},
    "paused_budget": {"running", "cancelled", "failed", "expired"},
    "paused_disconnected": {"running", "cancelled", "failed", "expired"},
    "paused_error": {"running", "cancelled", "failed", "expired"},
    "completed": set(),
    "cancelled": set(),
    "failed": set(),
    "expired": set(),
}


@pytest.fixture
def store(monkeypatch):
    s = FakeRunStore(Clock())
    monkeypatch.setattr(runs, "_STORE", s)
    return s


def make(run_id=None, *, user=USER, device=DEVICE, profile="code", mode="ask", max_parallel=None):
    return runs.create_run(user_id=user, device_id=device, run_id=run_id or new_run_id(),
                           profile=profile, mode=mode, max_parallel=max_parallel)


def at_state(state, **kw):
    """A run driven to `state` through legal moves only."""
    run = make(**kw)
    path = {
        "created": [], "running": ["running"],
        "awaiting_approval": ["running", "awaiting_approval"],
        "paused_budget": ["running", "paused_budget"],
        "paused_disconnected": ["running", "paused_disconnected"],
        "paused_error": ["running", "paused_error"],
        "completed": ["running", "completed"], "cancelled": ["cancelled"],
        "failed": ["failed"], "expired": ["expired"],
    }[state]
    for step in path:
        run = runs.transition(run.user_id, run.id, step)
    assert run.state == state
    return run


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def test_the_transition_table_is_exactly_the_documented_one():
    assert {k: set(v) for k, v in runs.TRANSITIONS.items()} == EXPECTED_TRANSITIONS


def test_the_table_is_read_only():
    with pytest.raises(TypeError):
        runs.TRANSITIONS["completed"] = frozenset({"running"})      # type: ignore[index]
    assert isinstance(runs.TRANSITIONS["running"], frozenset)


def test_table_shape_no_self_loops_terminal_final_everything_reachable_everything_ends():
    states = set(runs.RUN_STATES)
    assert set(runs.TRANSITIONS) == states
    for src, targets in runs.TRANSITIONS.items():
        assert src not in targets, f"self loop on {src}"
        assert targets <= states
    for term in runs.TERMINAL_STATES:
        assert runs.TRANSITIONS[term] == frozenset()
    assert runs.TERMINAL_STATES == {"completed", "cancelled", "failed", "expired"}

    def reach(start):
        seen, todo = {start}, [start]
        while todo:
            for nxt in runs.TRANSITIONS[todo.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    assert reach("created") == states                      # nothing is unreachable
    for s in states - runs.TERMINAL_STATES:
        assert reach(s) & runs.TERMINAL_STATES, f"{s} can never end"
    assert not any("created" in t for t in runs.TRANSITIONS.values())    # nothing goes back to created


def test_state_and_error_code_vocabularies_equal_the_protocols():
    common = REPO / "protocol" / "v1" / "common.json"
    if not common.exists():
        pytest.skip("protocol/ is not next to backend/ in this layout")
    defs = json.loads(common.read_text())["$defs"]
    assert set(runs.RUN_STATES) == set(defs["run_state"]["enum"])
    assert set(runs.ERROR_CODES) == set(defs["error_code"]["enum"])
    pattern = defs["run_id"]["pattern"]
    assert re.fullmatch(pattern, "run-abcdefgh") and not re.fullmatch(pattern, "short")
    assert runs._RUN_ID_RE.pattern == pattern


def test_the_slot_states_are_the_ones_the_sql_counts():
    assert runs.SLOT_STATES == {"created", "running", "awaiting_approval"}
    assert not (runs.SLOT_STATES & runs.PAUSED_STATES) and not (runs.SLOT_STATES & runs.TERMINAL_STATES)
    for sql in (runs._INSERT_COUNT_SQL, runs._MOVE_COUNT_SQL):
        listed = set(re.findall(r"'([a-z_]+)'", sql))
        assert listed == set(runs.SLOT_STATES)


@pytest.mark.parametrize("src", runs.RUN_STATES)
@pytest.mark.parametrize("dst", runs.RUN_STATES)
def test_every_pair_is_legal_exactly_when_the_table_says_so(store, src, dst):
    assert runs.can_transition(src, dst) == (dst in EXPECTED_TRANSITIONS[src])
    run = at_state(src)
    if dst in EXPECTED_TRANSITIONS[src]:
        moved = runs.transition(USER, run.id, dst)
        assert moved.state == dst
        assert (moved.ended_at is not None) == (dst in runs.TERMINAL_STATES)
    else:
        with pytest.raises(runs.IllegalTransition) as info:
            runs.transition(USER, run.id, dst)
        assert (info.value.current, info.value.target) == (src, dst)
        assert runs.get_run(USER, run.id).state == src          # nothing changed


# ---------------------------------------------------------------------------
# The run record
# ---------------------------------------------------------------------------

def test_create_starts_in_created_with_zero_counters_and_no_end(store):
    run = make("run-abcdefgh", profile="code", mode="accept-edits")
    assert (run.id, run.user_id, run.device_id) == ("run-abcdefgh", USER, DEVICE)
    assert (run.profile, run.mode, run.state) == ("code", "accept-edits", "created")
    assert (run.steps, run.credits, run.ended_at, run.error_code) == (0, 0.0, None, None)
    assert run.created_at == run.updated_at and not run.terminal
    assert runs.get_run(USER, "run-abcdefgh") == run


def test_the_run_manager_does_not_hard_code_the_code_profile(store):
    # build plan O8: Menux adds profiles on the same engine
    assert make(profile="mate").profile == "mate"


@pytest.mark.parametrize("bad", ["", "short", "x" * 65, "has space abc", "a/b/c/d/e/f/g/h", "../etc/passwd",
                                 "run-abcdefgh\n", "run-abcdéfgh", None, 12345678, b"run-abcdefgh"])
def test_a_malformed_run_id_is_refused_before_the_store_is_touched(store, bad):
    with pytest.raises(runs.InvalidRun):
        runs.create_run(user_id=USER, device_id=DEVICE, run_id=bad, profile="code", mode="ask")
    with pytest.raises(runs.InvalidRun):
        runs.get_run(USER, bad)
    assert store.insert_calls == 0 and not store.rows


@pytest.mark.parametrize("field,bad", [
    ("profile", "Code"), ("profile", "has space"), ("profile", "/home/me/project"), ("profile", ""),
    ("profile", "x" * 33), ("profile", "refactor the auth module"), ("profile", "code\n"),
    ("mode", "Ask"), ("mode", "a b"), ("mode", "/etc"), ("mode", ""), ("mode", None),
    ("device_id", "not-a-uuid"), ("device_id", ""), ("device_id", None), ("device_id", "../../x"),
    ("user_id", ""), ("user_id", None), ("user_id", "x" * 200), ("user_id", "a\nb"),
])
def test_other_create_arguments_are_validated(store, field, bad):
    args = dict(user_id=USER, device_id=DEVICE, run_id=new_run_id(), profile="code", mode="ask")
    args[field] = bad
    with pytest.raises(runs.InvalidRun):
        runs.create_run(**args)
    assert store.insert_calls == 0


def test_a_device_id_is_normalised_to_lower_case_canonical_form(store):
    run = make(device=DEVICE.upper())
    assert run.device_id == DEVICE


def test_a_second_run_with_the_same_id_is_a_collision_even_for_another_account(store):
    make("run-collide01")
    with pytest.raises(runs.RunIdCollision):
        make("run-collide01")
    with pytest.raises(runs.RunIdCollision):
        make("run-collide01", user=OTHER_USER, device=OTHER_DEVICE)
    assert runs.get_run(USER, "run-collide01").user_id == USER       # the original is untouched


def test_another_account_cannot_see_or_change_a_run(store):
    run = make()
    with pytest.raises(runs.RunNotFound):
        runs.get_run(OTHER_USER, run.id)
    with pytest.raises(runs.RunNotFound):
        runs.transition(OTHER_USER, run.id, "running")
    with pytest.raises(runs.RunNotFound):
        runs.add_progress(OTHER_USER, run.id, steps=1)
    assert runs.get_run(USER, run.id).state == "created"
    assert runs.list_open_runs(OTHER_USER, DEVICE) == []


def test_an_unknown_run_is_run_not_found_and_the_message_names_nothing(store):
    with pytest.raises(runs.RunNotFound) as info:
        runs.get_run(USER, "run-doesnotexist")
    assert "doesnotexist" not in str(info.value) and str(info.value) == "no such run"


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def test_the_normal_life_of_a_run(store):
    run = make()
    created_at = run.created_at
    store.clock.advance(5)
    run = runs.transition(USER, run.id, "running")
    assert run.state == "running" and run.ended_at is None and run.updated_at > created_at
    run = runs.transition(USER, run.id, "awaiting_approval")
    run = runs.transition(USER, run.id, "running")
    store.clock.advance(5)
    run = runs.transition(USER, run.id, "completed")
    assert run.state == "completed" and run.terminal
    assert run.ended_at == run.updated_at and run.created_at == created_at


def test_pausing_records_the_error_code_and_resuming_clears_it(store):
    run = at_state("running")
    run = runs.transition(USER, run.id, "paused_budget", error_code="quota_exhausted")
    assert (run.state, run.error_code, run.ended_at) == ("paused_budget", "quota_exhausted", None)
    run = runs.transition(USER, run.id, "running")                       # "grant more"
    assert (run.state, run.error_code) == ("running", None)
    run = runs.transition(USER, run.id, "paused_error", error_code="provider_unavailable")
    run = runs.transition(USER, run.id, "running")                       # retry
    run = runs.transition(USER, run.id, "paused_disconnected")
    run = runs.transition(USER, run.id, "running")                       # reconnect
    assert run.state == "running" and run.error_code is None


def test_a_failed_run_keeps_its_code_and_ends(store):
    run = runs.transition(USER, at_state("running").id, "failed", error_code="internal")
    assert (run.state, run.error_code) == ("failed", "internal") and run.ended_at is not None


@pytest.mark.parametrize("term", sorted(runs.TERMINAL_STATES))
def test_a_terminal_run_never_changes_state_again(store, term):
    run = at_state(term)
    for target in runs.RUN_STATES:
        with pytest.raises(runs.IllegalTransition):
            runs.transition(USER, run.id, target)
    after = runs.get_run(USER, run.id)
    assert after.state == term and after.ended_at == run.ended_at


@pytest.mark.parametrize("code", ["something went wrong in /home/me/app.py", "Quota_Exhausted", "",
                                  "not_a_protocol_code", "quota exhausted", "x" * 65, 7, b"internal"])
def test_an_error_code_must_be_a_protocol_code_never_a_message(store, code):
    run = at_state("running")
    with pytest.raises(runs.InvalidRun):
        runs.transition(USER, run.id, "paused_error", error_code=code)
    assert runs.get_run(USER, run.id).state == "running"


@pytest.mark.parametrize("code", sorted(runs.ERROR_CODES))
def test_every_protocol_error_code_is_accepted_when_pausing(store, code):
    assert runs.transition(USER, at_state("running").id, "paused_error", error_code=code).error_code == code


@pytest.mark.parametrize("target", ["running", "awaiting_approval", "completed"])
def test_a_code_cannot_be_attached_to_a_healthy_or_successful_state(store, target):
    run = at_state("running") if target != "running" else at_state("paused_error")
    with pytest.raises(runs.InvalidRun):
        runs.transition(USER, run.id, target, error_code="internal")


def test_unknown_target_state_is_invalid_not_illegal(store):
    run = make()
    for bad in ("RUNNING", "done", "", None, 3):
        with pytest.raises(runs.InvalidRun):
            runs.transition(USER, run.id, bad)


def test_expected_pins_the_state_the_caller_believes_the_run_is_in(store):
    run = at_state("paused_disconnected")
    with pytest.raises(runs.IllegalTransition) as info:
        runs.transition(USER, run.id, "running", expected={"paused_budget"})
    assert info.value.current == "paused_disconnected"
    assert runs.get_run(USER, run.id).state == "paused_disconnected"
    ok = runs.transition(USER, run.id, "running", expected=("paused_disconnected", "paused_error"))
    assert ok.state == "running"


# ---------------------------------------------------------------------------
# Races
# ---------------------------------------------------------------------------

def _race(store, run_id, targets):
    """Start one thread per target. Every thread READS the run before any of them
    WRITES (a one-shot barrier per thread sits in the store's `get`), then they all
    try to move; a loser that re-reads does not wait again."""
    barrier = threading.Barrier(len(targets))
    seen = threading.local()

    def first_read_waits(*_):
        if not getattr(seen, "done", False):
            seen.done = True
            barrier.wait(timeout=10)
    store.after_get = first_read_waits
    results = [None] * len(targets)

    def worker(i, target):
        try:
            results[i] = ("ok", runs.transition(USER, run_id, target))
        except Exception as exc:                      # noqa: BLE001 - the test inspects the type
            results[i] = ("err", exc)

    threads = [threading.Thread(target=worker, args=(i, t)) for i, t in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    store.after_get = None
    assert all(r is not None for r in results), "a racer hung"
    return results


def test_eight_callers_cancelling_at_once_produce_exactly_one_winner(store):
    run = at_state("running")
    results = _race(store, run.id, ["cancelled"] * 8)
    winners = [r for r in results if r[0] == "ok"]
    losers = [r[1] for r in results if r[0] == "err"]
    assert len(winners) == 1
    assert len(losers) == 7 and all(isinstance(e, runs.IllegalTransition) for e in losers)
    assert all(e.current == "cancelled" for e in losers)          # they saw the winner's state
    final = runs.get_run(USER, run.id)
    assert final.state == "cancelled" and final.ended_at == winners[0][1].ended_at


@pytest.mark.parametrize("a,b", [("completed", "paused_disconnected"), ("completed", "cancelled"),
                                 ("failed", "completed"), ("expired", "paused_error"),
                                 ("cancelled", "failed")])
def test_two_different_moves_from_the_same_state_never_both_end_the_run(store, a, b):
    run = at_state("running")
    results = _race(store, run.id, [a, b])
    ok = [r[1].state for r in results if r[0] == "ok"]
    errors = [r[1] for r in results if r[0] == "err"]
    final = runs.get_run(USER, run.id)
    assert all(isinstance(e, runs.IllegalTransition) for e in errors)
    assert ok, "somebody must win"
    # at most one of them ended the run, and the row agrees with whoever did
    ended = [s for s in ok if s in runs.TERMINAL_STATES]
    assert len(ended) <= 1
    if ended:
        assert final.state == ended[0] and final.ended_at is not None
    else:
        assert final.state == ok[-1] and final.ended_at is None
    # every loser lost to a state that really forbids its move
    for e in errors:
        assert not runs.can_transition(e.current, e.target)


def test_a_state_that_changed_after_the_read_is_re_read_and_the_move_is_judged_again(store):
    # Reader saw paused_error and wants "cancelled". Another process moved it to running
    # first. running -> cancelled is also legal, so the retry succeeds from the NEW state.
    run = at_state("paused_error")
    before = store.move_calls
    fired = []

    def other_process_wins(call):
        if not fired:
            fired.append(1)
            store.force_state(call["run_id"], "running")
    store.before_move = other_process_wins
    moved = runs.transition(USER, run.id, "cancelled")
    assert moved.state == "cancelled"
    assert store.move_calls - before == 2                              # first lost, second won


def test_a_state_that_changed_to_terminal_after_the_read_is_illegal_not_overwritten(store):
    run = at_state("running")

    def other_process_completes(call):
        store.force_state(call["run_id"], "completed")
    store.before_move = other_process_completes
    with pytest.raises(runs.IllegalTransition) as info:
        runs.transition(USER, run.id, "paused_disconnected")
    assert info.value.current == "completed"
    assert runs.get_run(USER, run.id).state == "completed"


def test_churn_that_never_settles_ends_in_transition_conflict_after_a_bounded_number_of_tries(store):
    run = at_state("paused_error")
    before = store.move_calls
    flip = {"to": "paused_budget"}

    def churn(call):
        store.force_state(call["run_id"], flip["to"])
        flip["to"] = "paused_error" if flip["to"] == "paused_budget" else "paused_budget"
    store.before_move = churn
    with pytest.raises(runs.TransitionConflict):
        runs.transition(USER, run.id, "cancelled")
    assert store.move_calls - before == runs.MAX_TRANSITION_ATTEMPTS
    assert runs.get_run(USER, run.id).state in {"paused_error", "paused_budget"}    # never half-moved


def test_a_run_that_disappears_between_read_and_move_is_run_not_found(store):
    run = at_state("running")
    store.before_move = lambda call: store.rows.pop(call["run_id"])
    with pytest.raises(runs.RunNotFound):
        runs.transition(USER, run.id, "cancelled")


# ---------------------------------------------------------------------------
# The parallel-run cap
# ---------------------------------------------------------------------------

def test_the_cap_counts_created_running_and_awaiting_runs_of_the_same_device(store):
    for _ in range(3):
        make(max_parallel=3)
    with pytest.raises(runs.ParallelRunLimit) as info:
        make(max_parallel=3)
    assert info.value.limit == 3
    make(max_parallel=3, device=OTHER_DEVICE)                 # another device has its own slots
    make(max_parallel=3, user=OTHER_USER)                     # and so does another account


def test_without_a_cap_nothing_is_counted(store):
    for _ in range(12):
        make()


@pytest.mark.parametrize("ending", ["completed", "cancelled", "failed", "expired",
                                    "paused_budget", "paused_disconnected", "paused_error"])
def test_a_paused_or_finished_run_frees_its_slot(store, ending):
    a = at_state("running")
    make(max_parallel=2)
    with pytest.raises(runs.ParallelRunLimit):
        make(max_parallel=2)
    runs.transition(USER, a.id, ending)
    make(max_parallel=2)                                       # a slot opened


def test_resuming_a_paused_run_needs_a_slot_and_is_refused_when_none_is_free(store):
    paused = at_state("paused_disconnected")
    for _ in range(2):
        make(max_parallel=2)
    with pytest.raises(runs.ParallelRunLimit):
        runs.transition(USER, paused.id, "running", max_parallel=2)
    assert runs.get_run(USER, paused.id).state == "paused_disconnected"
    runs.transition(USER, paused.id, "running", max_parallel=3)        # room: it goes
    assert runs.get_run(USER, paused.id).state == "running"


def test_moves_between_slot_states_never_need_a_slot(store):
    a, b = make(max_parallel=2), make(max_parallel=2)         # device is exactly full
    for run in (a, b):
        runs.transition(USER, run.id, "running", max_parallel=2)
        runs.transition(USER, run.id, "awaiting_approval", max_parallel=2)
        runs.transition(USER, run.id, "running", max_parallel=2)
    assert store.move_calls == 6


def test_a_cap_below_one_is_invalid(store):
    for bad in (0, -1, 1.5, "3", True):
        with pytest.raises(runs.InvalidRun):
            make(max_parallel=bad)


def test_ten_simultaneous_creates_against_a_cap_of_three_let_exactly_three_in(store):
    results = [None] * 10
    barrier = threading.Barrier(10)

    def worker(i):
        barrier.wait(timeout=10)
        try:
            results[i] = ("ok", make(max_parallel=3))
        except runs.ParallelRunLimit as exc:
            results[i] = ("full", exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]
    assert sorted(r[0] for r in results) == ["full"] * 7 + ["ok"] * 3
    assert len(runs.list_open_runs(USER, DEVICE)) == 3


# ---------------------------------------------------------------------------
# Counters and listing
# ---------------------------------------------------------------------------

def test_progress_accumulates(store):
    run = at_state("running")
    store.clock.advance(3)
    run = runs.add_progress(USER, run.id, steps=1, credits=0.5)
    run = runs.add_progress(USER, run.id, steps=2, credits=0.1)
    run = runs.add_progress(USER, run.id, credits=0.2)
    assert (run.steps, run.credits) == (3, 0.8)
    assert run.updated_at > run.created_at


def test_progress_still_lands_after_the_run_has_ended(store):
    # a model call that was in flight when the run was cancelled still gets recorded
    run = at_state("cancelled")
    after = runs.add_progress(USER, run.id, steps=1, credits=0.25)
    assert (after.state, after.steps, after.credits) == ("cancelled", 1, 0.25)


@pytest.mark.parametrize("kw", [dict(steps=-1), dict(credits=-0.01), dict(steps=1.5), dict(steps="2"),
                                dict(credits="1"), dict(steps=True), dict(credits=float("nan")),
                                dict(credits=float("inf"))])
def test_progress_rejects_negative_or_non_numeric_values(store, kw):
    run = at_state("running")
    with pytest.raises(runs.InvalidRun):
        runs.add_progress(USER, run.id, **kw)
    assert runs.get_run(USER, run.id).steps == 0


def test_progress_on_an_unknown_run_is_run_not_found(store):
    with pytest.raises(runs.RunNotFound):
        runs.add_progress(USER, "run-doesnotexist", steps=1)


def test_list_open_runs_is_scoped_to_account_and_device_and_leaves_out_finished_runs(store):
    a, b = make(), make()
    done = at_state("completed")
    paused = at_state("paused_error")
    make(device=OTHER_DEVICE)
    make(user=OTHER_USER)
    ids = [r.id for r in runs.list_open_runs(USER, DEVICE)]
    assert set(ids) == {a.id, b.id, paused.id} and done.id not in ids
    assert all(isinstance(r, runs.Run) for r in runs.list_open_runs(USER, DEVICE))


def test_list_open_runs_validates_its_arguments(store):
    with pytest.raises(runs.InvalidRun):
        runs.list_open_runs(USER, "nope")
    with pytest.raises(runs.InvalidRun):
        runs.list_open_runs("", DEVICE)


# ---------------------------------------------------------------------------
# Fail closed, and never log what a store fault says
# ---------------------------------------------------------------------------

CALLS = {
    "create": lambda: make(),
    "get": lambda: runs.get_run(USER, "run-abcdefgh"),
    "transition": lambda: runs.transition(USER, "run-abcdefgh", "running"),
    "progress": lambda: runs.add_progress(USER, "run-abcdefgh", steps=1),
    "list": lambda: runs.list_open_runs(USER, DEVICE),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_store_fault_is_run_store_unavailable_with_fixed_text_and_no_cause(monkeypatch, caplog, name):
    monkeypatch.setattr(runs, "_STORE", BoomStore())
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(runs.RunStoreUnavailable) as info:
            CALLS[name]()
    assert CANARY not in str(info.value) and CANARY not in repr(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert CANARY not in caplog.text
    assert "RuntimeError" in caplog.text                      # the TYPE is what gets logged


def test_a_database_pool_timeout_surfaces_with_its_retry_hint(monkeypatch):
    from eo import db

    class Exhausted:
        def get(self, **_):
            raise db.DatabaseUnavailable(retry_after=2.0)
    monkeypatch.setattr(runs, "_STORE", Exhausted())
    with pytest.raises(runs.RunStoreUnavailable) as info:
        runs.get_run(USER, "run-abcdefgh")
    assert info.value.retry_after == 2.0


def test_domain_errors_are_not_swallowed_into_store_unavailable(store):
    with pytest.raises(runs.RunNotFound):
        runs.get_run(USER, "run-doesnotexist")
    make("run-collide02")
    with pytest.raises(runs.RunIdCollision):
        make("run-collide02")


def test_the_module_logs_ids_and_states_but_never_a_store_fault_text(store, caplog):
    with caplog.at_level(logging.DEBUG):
        run = make()
        runs.transition(USER, run.id, "running")
        runs.transition(USER, run.id, "paused_error", error_code="internal")
    assert run.id in caplog.text and "running" in caplog.text
    assert "paused_error" in caplog.text
