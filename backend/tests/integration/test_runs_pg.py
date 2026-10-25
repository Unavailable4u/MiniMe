"""Real-Postgres checks for S06 (migration 0014): the `runs` table, its CHECK
constraints, its trigger, its RLS policies, its grants -- and the atomicity the state
machine in codeagent/runs.py relies on (a conditional UPDATE; a per-device advisory
lock for the parallel-run cap).

The unit tests (tests/unit/test_codeagent_runs.py) use an in-memory twin of the store,
so none of this is exercised there. This file does.

Skipped unless MINIME_TEST_DATABASE_URL is set. Point it at a scratch database that
already has migration 0014 applied, connecting AS the app role (minime_app), not
postgres, so grants and RLS are enforced:

    MINIME_TEST_DATABASE_URL="postgresql://minime_app:pw@localhost/minime_test" \\
        pytest tests/integration/test_runs_pg.py

Every test uses its own random user, device and run ids, so runs do not collide and
need no cleanup (minime_app cannot delete from runs, by design).
"""
import contextlib
import os
import threading
import uuid

import pytest

DSN = os.environ.get("MINIME_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="MINIME_TEST_DATABASE_URL not set")


@pytest.fixture
def pg(monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    from codeagent import runs

    @contextlib.contextmanager
    def cursor(user_id=None, trusted=False):
        conn = psycopg.connect(DSN, row_factory=dict_row)
        try:
            cur = conn.cursor()
            cur.execute(
                "select set_config('app.current_user_id', %s, true), "
                "set_config('app.trusted_internal', %s, true)",
                (user_id or "", "true" if trusted else "false"),
            )
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(runs.db, "cursor", cursor)
    return cursor


@pytest.fixture
def user():
    return f"pg_user_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def device():
    return str(uuid.uuid4())


def rid():
    return "run-" + uuid.uuid4().hex[:20]


def make(user, device, **kw):
    from codeagent import runs
    return runs.create_run(user_id=user, device_id=device, run_id=kw.pop("run_id", rid()),
                           profile=kw.pop("profile", "code"), mode=kw.pop("mode", "ask"), **kw)


def insert_raw(pg, user, device, run_id=None, **cols):
    values = dict(id=run_id or rid(), user_id=user, device_id=device, profile="code", mode="ask")
    values.update(cols)
    names = ", ".join(values)
    with pg(user_id=user) as cur:
        cur.execute(f"insert into runs ({names}) values ({', '.join(['%s'] * len(values))}) returning id",
                    list(values.values()))
        return cur.fetchone()["id"]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def test_create_then_get_round_trips_with_database_types(pg, user, device):
    from datetime import datetime
    from codeagent import runs
    run_id = rid()
    run = make(user, device, run_id=run_id, mode="accept-edits")
    assert (run.id, run.user_id, run.device_id, run.state) == (run_id, user, device, "created")
    assert (run.profile, run.mode, run.steps, run.credits, run.ended_at, run.error_code) == \
        ("code", "accept-edits", 0, 0.0, None, None)
    assert isinstance(run.created_at, datetime) and run.created_at.tzinfo is not None
    assert isinstance(run.credits, float) and isinstance(run.device_id, str)
    assert runs.get_run(user, run_id) == run


def test_a_taken_id_is_a_collision_even_for_another_account_and_the_message_names_nothing(pg, user, device):
    from codeagent import runs
    other = f"pg_user_{uuid.uuid4().hex[:10]}"
    run_id = rid()
    make(user, device, run_id=run_id)
    for who in (user, other):
        with pytest.raises(runs.RunIdCollision) as info:
            make(who, str(uuid.uuid4()), run_id=run_id)
        assert run_id not in str(info.value)
    assert runs.get_run(user, run_id).user_id == user


def test_the_normal_life_and_the_error_code_round_trip(pg, user, device):
    from codeagent import runs
    run = make(user, device)
    run = runs.transition(user, run.id, "running")
    run = runs.transition(user, run.id, "awaiting_approval")
    run = runs.transition(user, run.id, "running")
    run = runs.transition(user, run.id, "paused_budget", error_code="quota_exhausted")
    assert (run.state, run.error_code, run.ended_at) == ("paused_budget", "quota_exhausted", None)
    run = runs.transition(user, run.id, "running")
    assert run.error_code is None
    done = runs.transition(user, run.id, "completed")
    assert done.state == "completed" and done.ended_at is not None and done.updated_at >= done.created_at


def test_progress_accumulates_exactly_and_still_lands_after_the_run_ended(pg, user, device):
    from codeagent import runs
    run = make(user, device)
    runs.add_progress(user, run.id, steps=1, credits=0.1)
    runs.add_progress(user, run.id, steps=2, credits=0.2)
    runs.transition(user, run.id, "cancelled")
    after = runs.add_progress(user, run.id, steps=1, credits=0.25)
    assert (after.state, after.steps) == ("cancelled", 4)
    assert after.credits == 0.55                           # numeric, so no 0.1 + 0.2 float noise


def test_list_open_runs_is_scoped_and_leaves_out_finished_runs(pg, user, device):
    from codeagent import runs
    a, b = make(user, device), make(user, device)
    done = make(user, device)
    runs.transition(user, done.id, "cancelled")
    make(user, str(uuid.uuid4()))
    make(f"pg_user_{uuid.uuid4().hex[:10]}", device)
    assert [r.id for r in runs.list_open_runs(user, device)] == [a.id, b.id]


# ---------------------------------------------------------------------------
# RLS
# ---------------------------------------------------------------------------

def test_another_account_cannot_read_or_move_a_run(pg, user, device):
    from codeagent import runs
    other = f"pg_user_{uuid.uuid4().hex[:10]}"
    run = make(user, device)
    with pytest.raises(runs.RunNotFound):
        runs.get_run(other, run.id)
    with pytest.raises(runs.RunNotFound):
        runs.transition(other, run.id, "cancelled")
    assert runs.get_run(user, run.id).state == "created"


def test_rls_blocks_raw_access_without_the_right_identity(pg, user, device):
    run = make(user, device)
    other = f"pg_user_{uuid.uuid4().hex[:10]}"
    for who, trusted in ((other, False), (None, False)):
        with pg(user_id=who, trusted=trusted) as cur:
            cur.execute("select id from runs where id = %s", (run.id,))
            assert cur.fetchall() == []
            cur.execute("update runs set steps = steps + 1 where id = %s returning id", (run.id,))
            assert cur.fetchall() == []                    # RLS hides the row from the UPDATE
    with pg(trusted=True) as cur:                          # the housekeeping path sees it
        cur.execute("select id from runs where id = %s", (run.id,))
        assert [r["id"] for r in cur.fetchall()] == [run.id]


def test_an_account_cannot_insert_a_run_for_another_account(pg, user, device):
    import psycopg
    other = f"pg_user_{uuid.uuid4().hex[:10]}"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg(user_id=other) as cur:
            cur.execute("insert into runs (id, user_id, device_id, profile, mode) values (%s, %s, %s, 'code', 'ask')",
                        (rid(), user, device))


# ---------------------------------------------------------------------------
# Grants, CHECKs and the trigger
# ---------------------------------------------------------------------------

def test_the_app_role_cannot_delete_a_run(pg, user, device):
    import psycopg
    run = make(user, device)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg(user_id=user) as cur:
            cur.execute("delete from runs where id = %s", (run.id,))


@pytest.mark.parametrize("cols", [
    dict(state="done"), dict(state="RUNNING"),
    dict(error_code="something went wrong in /home/me/app.py"), dict(error_code="Quota"),
    dict(error_code="x" * 65), dict(profile="has space"), dict(profile="/home/me/project"),
    dict(profile="x" * 33), dict(mode="Ask"), dict(mode=""), dict(id="short"), dict(id="has space abc"),
    dict(id="x" * 65), dict(steps=-1), dict(credits=-1),
    dict(state="completed"),                               # terminal without ended_at
])
def test_check_constraints_reject_content_shaped_and_inconsistent_values(pg, user, device, cols):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_raw(pg, user, device, **cols)


def test_ended_at_without_a_terminal_state_is_rejected(pg, user, device):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_raw(pg, user, device, state="running", ended_at="2026-01-01T00:00:00Z")


@pytest.mark.parametrize("term", ["completed", "cancelled", "failed", "expired"])
def test_a_terminal_run_is_frozen_by_the_trigger_even_for_raw_sql(pg, user, device, term):
    import psycopg
    from codeagent import runs
    run = make(user, device)
    run = runs.transition(user, run.id, "running") if term == "completed" else run
    done = runs.transition(user, run.id, term, error_code="internal" if term == "failed" else None)
    for sql, args in (
        ("update runs set state = 'running', ended_at = null where id = %s", (done.id,)),
        ("update runs set state = 'cancelled' where id = %s and state <> 'cancelled'", (done.id,)),
        ("update runs set ended_at = now() + interval '1 day' where id = %s", (done.id,)),
        ("update runs set error_code = 'internal' where id = %s and error_code is distinct from 'internal'", (done.id,)),
    ):
        try:
            with pg(user_id=user) as cur:
                cur.execute(sql, args)
                if cur.rowcount == 0:
                    continue                                # nothing matched: nothing changed
        except psycopg.errors.InsufficientPrivilege as exc:
            assert "terminal" in str(exc) and done.id not in str(exc)     # fixed text, no row value
        else:
            pytest.fail(f"a terminal run was changed by: {sql}")
    assert runs.get_run(user, done.id).state == term


def test_identity_columns_are_immutable_and_counters_cannot_decrease(pg, user, device):
    import psycopg
    run = make(user, device)
    with pg(user_id=user) as cur:
        cur.execute("update runs set steps = 5, credits = 2 where id = %s", (run.id,))
    for sql, args, text in (
        ("update runs set user_id = 'someone_else' where id = %s", (run.id,), "identity"),
        ("update runs set device_id = %s where id = %s", (str(uuid.uuid4()), run.id), "identity"),
        ("update runs set profile = 'mate' where id = %s", (run.id,), "identity"),
        ("update runs set created_at = now() - interval '1 day' where id = %s", (run.id,), "identity"),
        ("update runs set steps = steps - 1 where id = %s", (run.id,), "counters"),
        ("update runs set credits = credits - 0.5 where id = %s", (run.id,), "counters"),
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as info:
            with pg(user_id=user) as cur:
                cur.execute(sql, args)
        assert text in str(info.value)
    with pg(user_id=user) as cur:                           # raising a counter is fine
        cur.execute("update runs set steps = steps + 1 where id = %s", (run.id,))


def test_a_missing_table_would_fail_closed_not_open(pg, user, device, monkeypatch):
    from codeagent import runs
    import psycopg

    def broken(**_):
        raise psycopg.errors.UndefinedTable("relation \"runs\" does not exist")
    monkeypatch.setattr(runs._STORE, "get", broken)
    with pytest.raises(runs.RunStoreUnavailable):
        runs.get_run(user, rid())


# ---------------------------------------------------------------------------
# Atomicity (real concurrent connections)
# ---------------------------------------------------------------------------

def _first_read_waits(monkeypatch, parties):
    """Every racer READS the run before any of them WRITES: a one-shot barrier per
    thread in the store's get()."""
    from codeagent import runs
    barrier = threading.Barrier(parties)
    seen = threading.local()
    original = runs._STORE.get

    def get(**kw):
        row = original(**kw)
        # only the racing threads wait: the test's own get_run() calls (main thread)
        # after the race must not queue behind a barrier nobody else will reach
        if threading.current_thread() is not threading.main_thread() and not getattr(seen, "done", False):
            seen.done = True
            barrier.wait(timeout=20)
        return row
    monkeypatch.setattr(runs._STORE, "get", get)


def _run_threads(n, fn):
    results = [None] * n

    def worker(i):
        try:
            results[i] = ("ok", fn(i))
        except Exception as exc:                          # noqa: BLE001 - inspected by the test
            results[i] = ("err", exc)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert all(r is not None for r in results), "a racer hung"
    return results


def test_eight_connections_cancelling_the_same_run_produce_exactly_one_winner(pg, user, device, monkeypatch):
    from codeagent import runs
    run = make(user, device)
    run = runs.transition(user, run.id, "running")
    _first_read_waits(monkeypatch, 8)
    results = _run_threads(8, lambda i: runs.transition(user, run.id, "cancelled"))
    winners = [r for r in results if r[0] == "ok"]
    losers = [r[1] for r in results if r[0] == "err"]
    assert len(winners) == 1
    assert len(losers) == 7 and all(isinstance(e, runs.IllegalTransition) and e.current == "cancelled"
                                    for e in losers)
    final = runs.get_run(user, run.id)
    assert final.state == "cancelled" and final.ended_at == winners[0][1].ended_at


def test_a_pause_racing_a_completion_never_resurrects_or_overwrites(pg, user, device, monkeypatch):
    from codeagent import runs
    run = runs.transition(user, make(user, device).id, "running")
    _first_read_waits(monkeypatch, 2)
    results = _run_threads(2, lambda i: runs.transition(user, run.id, ("completed", "paused_disconnected")[i]))
    final = runs.get_run(user, run.id)
    assert any(r[0] == "ok" for r in results)
    for kind, value in results:
        if kind == "err":
            assert isinstance(value, runs.IllegalTransition)
    if final.state == "completed":
        assert final.ended_at is not None
    else:
        assert final.state == "paused_disconnected" and final.ended_at is None


def test_ten_simultaneous_creates_against_a_cap_of_three_let_exactly_three_in(pg, user, device):
    from codeagent import runs
    barrier = threading.Barrier(10)

    def create(i):
        barrier.wait(timeout=20)
        return make(user, device, max_parallel=3)
    results = _run_threads(10, create)
    assert sorted(r[0] for r in results) == ["err"] * 7 + ["ok"] * 3
    assert all(isinstance(r[1], runs.ParallelRunLimit) for r in results if r[0] == "err")
    assert len(runs.list_open_runs(user, device)) == 3


def test_simultaneous_resumes_cannot_overfill_a_device(pg, user, device):
    from codeagent import runs
    paused = []
    for _ in range(4):
        r = make(user, device)
        runs.transition(user, r.id, "running")
        paused.append(runs.transition(user, r.id, "paused_disconnected"))
    make(user, device)                                     # one slot already in use
    barrier = threading.Barrier(4)

    def resume(i):
        barrier.wait(timeout=20)
        return runs.transition(user, paused[i].id, "running", max_parallel=3)
    results = _run_threads(4, resume)
    assert sorted(r[0] for r in results) == ["err", "err", "ok", "ok"]    # 1 used + 2 resumed = 3
    assert all(isinstance(r[1], runs.ParallelRunLimit) for r in results if r[0] == "err")
    states = [runs.get_run(user, p.id).state for p in paused]
    assert states.count("running") == 2 and states.count("paused_disconnected") == 2


def test_the_cap_is_per_device_and_per_account(pg, user, device):
    other_device = str(uuid.uuid4())
    for _ in range(2):
        make(user, device, max_parallel=2)
    make(user, other_device, max_parallel=2)
    make(f"pg_user_{uuid.uuid4().hex[:10]}", device, max_parallel=2)


def test_a_paused_or_finished_run_frees_its_slot(pg, user, device):
    from codeagent import runs
    a = runs.transition(user, make(user, device, max_parallel=2).id, "running")
    make(user, device, max_parallel=2)
    with pytest.raises(runs.ParallelRunLimit):
        make(user, device, max_parallel=2)
    runs.transition(user, a.id, "paused_error", error_code="provider_unavailable")
    make(user, device, max_parallel=2)
