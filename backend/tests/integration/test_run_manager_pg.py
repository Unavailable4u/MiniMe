"""Real-Postgres checks for S06c (codeagent/run_manager.py).

The unit tests (tests/unit/test_codeagent_run_manager.py) use an in-memory twin of the run
store. This file runs the manager against the REAL `runs` table (migration 0014), so what
only Postgres can prove is proved here:

  * the sweep's one cross-account READ: the SQL of PgSweepStore, through the trusted
    internal path, returns only open, idle runs, in (updated_at, id) order, keyset-paged,
    and only ids and timestamps; a cursor with an acting user (not trusted) does not see
    other accounts' runs, so the sweep really does depend on that path;
  * expire_stale end to end: runs of two accounts are expired through the real conditional
    UPDATE, the trigger and the RLS policies (the write is made AS each run's owner);
  * the whole lifecycle -- start, pause, resume, end, cancel -- on real rows, and the
    cleanup rule (nothing left in Redis) after each ending;
  * a real race: eight threads ending one run at once -> exactly one winner.

Redis is the in-memory twin (tests/unit/buffer_testkit.py): this file is about Postgres.

Skipped unless MINIME_TEST_DATABASE_URL is set; see tests/integration/test_runs_pg.py for
how (a scratch database with migration 0014 applied, connecting AS minime_app).
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
def world(pg, monkeypatch):
    """Redis twins for the buffer and run_guard; the real run store."""
    from codeagent import buffer
    from eo import run_guard
    from tests.unit import buffer_testkit as bt
    from tests.unit.run_manager_testkit import FakeGuardRedis, World

    redis, guard = bt.FakeRunRedis(bt.Clock()), FakeGuardRedis()
    monkeypatch.setattr(buffer, "_REDIS", redis)
    monkeypatch.setattr(buffer, "_now", redis.clock.time)
    monkeypatch.setattr(run_guard, "_redis", lambda: guard)
    monkeypatch.setenv("RUN_BUFFER_SECRET", bt.SECRET)
    monkeypatch.delenv("RUN_BUFFER_KEY_ID", raising=False)
    monkeypatch.delenv("RUN_BUFFER_PREVIOUS_SECRETS", raising=False)
    World._reset_guard_cache()
    yield type("W", (), {"redis": redis, "guard": guard})
    World._reset_guard_cache()


@pytest.fixture
def user():
    return f"pg_user_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def device():
    return str(uuid.uuid4())


def rid():
    return "run-" + uuid.uuid4().hex[:20]


def start(user, device, run_id=None, **kw):
    from codeagent import run_manager
    from tests.unit.run_manager_testkit import history, start_payload
    return run_manager.start_run(user_id=user, device_id=device, run_id=run_id or rid(),
                                 payload=start_payload(history(2)), **kw)


def age(pg, user, run_id, seconds):
    """Make a run look idle: the app role may update updated_at (the trigger guards identity,
    terminal states and counters, not this column)."""
    with pg(user_id=user) as cur:
        cur.execute("update runs set updated_at = now() - make_interval(secs => %s) where id = %s and user_id = %s",
                    (seconds, run_id, user))


def state_of(pg, user, run_id):
    with pg(user_id=user) as cur:
        cur.execute("select state, error_code, ended_at from runs where id = %s and user_id = %s", (run_id, user))
        return cur.fetchone()


# ---------------------------------------------------------------------------
# The sweep read
# ---------------------------------------------------------------------------

def test_the_sweep_read_sees_open_idle_runs_of_every_account_and_nothing_else(pg, world, user, device):
    from datetime import datetime, timedelta, timezone

    from codeagent import run_manager
    other = f"pg_user_{uuid.uuid4().hex[:10]}"
    a, b, ended, fresh = rid(), rid(), rid(), rid()
    start(user, device, a)
    start(other, str(uuid.uuid4()), b)
    start(user, device, ended)
    start(user, device, fresh)
    from codeagent import runs
    runs.transition(user, ended, "running")
    run_manager.end_run(user, ended, "completed")
    for who, run_id, secs in ((user, a, 3600), (other, b, 1800), (user, ended, 7200)):
        age(pg, who, run_id, secs)

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=300)
    rows = run_manager.PgSweepStore().list_open(updated_before=cutoff, after=None, limit=1000)
    mine = [r for r in rows if r["id"] in (a, b, ended, fresh)]
    assert [r["id"] for r in mine] == [a, b]                               # oldest first; no ended run, no fresh run
    assert {k for r in mine for k in r} == {"id", "user_id", "updated_at"}   # ids and timestamps only
    assert {r["user_id"] for r in mine} == {user, other}


def test_the_sweep_read_is_keyset_paged(pg, world, user, device):
    from datetime import datetime, timedelta, timezone

    from codeagent import run_manager
    ids = [rid() for _ in range(5)]
    for i, run_id in enumerate(ids):
        start(user, device, run_id)
        age(pg, user, run_id, 5000 - i * 100)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=300)
    store = run_manager.PgSweepStore()
    seen, after = [], None
    while True:
        page = store.list_open(updated_before=cutoff, after=after, limit=2)
        seen += [r["id"] for r in page if r["id"] in ids]
        if len(page) < 2:
            break
        after = (page[-1]["updated_at"], page[-1]["id"])
    assert seen == ids                                                      # every run once, in order


def test_without_the_trusted_path_the_read_sees_nothing_of_other_accounts(pg, world, user, device):
    run_id = rid()
    start(user, device, run_id)
    with pg(user_id=f"pg_user_{uuid.uuid4().hex[:10]}") as cur:
        cur.execute("select id from runs where id = %s", (run_id,))
        assert cur.fetchall() == []
    with pg() as cur:                                                       # no acting user, not trusted
        cur.execute("select id from runs where id = %s", (run_id,))
        assert cur.fetchall() == []
    with pg(trusted=True) as cur:
        cur.execute("select id from runs where id = %s", (run_id,))
        assert [r["id"] for r in cur.fetchall()] == [run_id]


# ---------------------------------------------------------------------------
# expire_stale on real rows
# ---------------------------------------------------------------------------

def test_expire_stale_expires_idle_runs_without_a_buffer_across_accounts(pg, world, user, device):
    from codeagent import buffer, run_manager
    other, other_device = f"pg_user_{uuid.uuid4().hex[:10]}", str(uuid.uuid4())
    dead_a, dead_b, live, young = rid(), rid(), rid(), rid()
    start(user, device, dead_a)
    start(other, other_device, dead_b)
    start(user, device, live)
    start(user, device, young)
    for who, run_id in ((user, dead_a), (other, dead_b), (user, live), (user, young)):
        age(pg, who, run_id, 3600)
    age(pg, user, young, 10)                                                # recently active
    buffer.delete_buffer(dead_a)
    buffer.delete_buffer(dead_b)
    buffer.delete_buffer(young)                                             # no buffer, but not idle

    report = run_manager.expire_stale()
    assert report.expired >= 2 and report.unreadable == 0
    for who, run_id in ((user, dead_a), (other, dead_b)):
        row = state_of(pg, who, run_id)
        assert (row["state"], row["error_code"]) == ("expired", None) and row["ended_at"] is not None
    assert state_of(pg, user, live)["state"] == "created" and buffer.exists(live)
    assert state_of(pg, user, young)["state"] == "created"
    assert not [k for k in world.redis.keys() if dead_a in k or dead_b in k]


# ---------------------------------------------------------------------------
# The lifecycle on real rows
# ---------------------------------------------------------------------------

def test_the_lifecycle_start_pause_resume_end_leaves_nothing_in_redis(pg, world, user, device):
    from codeagent import buffer, run_manager, runs
    run_id = rid()
    run = start(user, device, run_id, max_parallel=2)
    assert run.state == "created" and len(world.redis.keys()) == 3
    runs.transition(user, run_id, "running")
    seq = buffer.next_seq(run_id)
    buffer.append_replay(run_id, {"v": 1, "t": "run_event", "id": "00000000-0000-4000-8000-000000000001",
                                  "run_id": run_id, "seq": seq, "ts": "2026-11-02T09:00:00.250Z",
                                  "payload": {"type": "text_delta", "text": "hi"}})
    run_manager.pause_run(user, run_id, "paused_disconnected")
    assert len(world.redis.keys()) == 3                                     # a pause keeps the buffer
    got = run_manager.resume_run(user_id=user, device_id=device, run_id=run_id, last_seq=0, max_parallel=2)
    assert got.run.state == "running" and [m["seq"] for m in got.replay] == [1]
    ended = run_manager.end_run(user, run_id, "completed")
    assert ended.state == "completed" and ended.ended_at is not None
    assert world.redis.keys() == [] and world.redis.all_strings() == []


def test_cancel_on_a_real_row_sets_the_flag_first_and_leaves_nothing(pg, world, user, device):
    from codeagent import run_manager, runs
    from eo import run_guard
    run_id = rid()
    start(user, device, run_id)
    runs.transition(user, run_id, "running")
    cancelled = run_manager.cancel_run(user, run_id)
    assert cancelled.state == "cancelled" and cancelled.ended_at is not None
    assert world.redis.keys() == [] and world.guard.data == {} and run_guard.cancel_reason(run_id) is None


def test_a_resume_from_a_device_that_is_full_is_refused_by_the_real_cap(pg, world, user, device):
    from codeagent import run_manager, runs
    held, other = rid(), rid()
    start(user, device, held)
    runs.transition(user, held, "running")
    run_manager.pause_run(user, held, "paused_disconnected")
    start(user, device, other, max_parallel=1)
    runs.transition(user, other, "running")
    with pytest.raises(runs.ParallelRunLimit):
        run_manager.resume_run(user_id=user, device_id=device, run_id=held, last_seq=0, max_parallel=1)
    assert state_of(pg, user, held)["state"] == "paused_disconnected"


def test_a_rebuilt_buffer_after_an_expiry_on_a_real_row(pg, world, user, device):
    from codeagent import buffer, run_manager, runs
    from tests.unit.run_manager_testkit import history, start_payload
    run_id = rid()
    start(user, device, run_id)
    runs.transition(user, run_id, "running")
    run_manager.pause_run(user, run_id, "paused_disconnected")
    world.redis.clock.advance(buffer.IDLE_TTL_SECONDS + 1)
    got = run_manager.resume_run(user_id=user, device_id=device, run_id=run_id, last_seq=7,
                                 state=start_payload(history(3, "device")))
    assert got.rebuilt and got.run.state == "running"
    assert buffer.read_conversation(run_id).items == history(3, "device") and buffer.next_seq(run_id) == 8


def test_eight_threads_ending_one_run_have_exactly_one_winner(pg, world, user, device):
    from codeagent import run_manager, runs
    run_id = rid()
    start(user, device, run_id)
    runs.transition(user, run_id, "running")
    outcomes, barrier = [], threading.Barrier(8)

    def go(to_state):
        barrier.wait()
        try:
            run_manager.end_run(user, run_id, to_state)
            outcomes.append("won")
        except runs.IllegalTransition:
            outcomes.append("lost")
        except runs.TransitionConflict:
            outcomes.append("lost")
    threads = [threading.Thread(target=go, args=(s,)) for s in ["completed", "failed", "expired", "completed"] * 2]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert outcomes.count("won") == 1 and len(outcomes) == 8
    assert state_of(pg, user, run_id)["state"] in ("completed", "failed", "expired")
    assert world.redis.keys() == []
