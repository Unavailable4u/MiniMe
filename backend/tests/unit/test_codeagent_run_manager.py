"""S06c: the run manager (codeagent/run_manager.py) -- what makes the run row (S06a) and
the encrypted buffer (S06b) behave as one run.

Everything runs against in-memory twins behind the real interfaces (see
tests/unit/run_manager_testkit.py): the run store, the sweep read, the Redis buffer and
eo/run_guard's Redis, all on one clock, so an idle expiry or the 24 h cap is crossed
without sleeping. The protocol/v1 validation is the REAL one (the same validator the
socket uses). The SQL of the sweep read and the end-to-end behaviour against Postgres are
in tests/integration/test_run_manager_pg.py.

What is pinned:
  * start: the payload is validated first (nothing is written for a bad one, and the
    error never quotes it); the row and the buffer are created together; a buffer that
    cannot be created leaves no `created` zombie; a missing secret is caught before the
    row is written; the parallel-run cap and id collisions leave no buffer behind;
  * the cleanup rule: completed / failed / expired / cancelled leave NOTHING in Redis
    (no buffer key, no run_guard rail); the paused states keep the buffer; two racing
    ends have exactly one winner;
  * cancel: the run_guard flag is set before the row moves, only for the owner's open
    run, it survives a store outage (the user's intent), and nothing is left behind;
  * resume: events after last_seq come back verbatim; an expired, missing or unreadable
    buffer is rebuilt from the device's validated history (counter continuing from
    last_seq); no buffer and no state is a clear error; another device's or account's run
    is "not found"; an ended run is not resumable; a resume that loses a race with the
    sweep does not leave a buffer behind;
  * expire_stale: only idle runs whose buffer is gone, never a live one, never one it
    cannot judge (Redis down, secret missing, unreadable); the 24 h cap through it;
    keyset paging; a run that is moved by someone else in between is "raced";
  * fail closed, and nothing but a type name in the log.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("jsonschema")

from codeagent import buffer, run_manager, runs                      # noqa: E402
from eo import run_guard                                             # noqa: E402
from tests.unit import buffer_testkit as bt                          # noqa: E402
from tests.unit import runs_testkit as rt                            # noqa: E402
from tests.unit.run_manager_testkit import (                         # noqa: E402
    CANARY, DEVICE, OTHER_DEVICE, OTHER_USER, USER, World, history, start_payload,
)


@pytest.fixture
def w(monkeypatch):
    world = World(monkeypatch)
    try:
        run_manager._protocol()
    except run_manager.ProtocolSchemasUnavailable:
        pytest.skip("protocol/ or jsonschema is not available in this layout")
    yield world
    world.teardown()


def evt(seq, run_id, text="hi", t="run_event"):
    return {"v": 1, "t": t, "id": f"00000000-0000-4000-8000-{seq:012d}", "run_id": run_id, "seq": seq,
            "ts": "2026-11-02T09:00:00.250Z", "payload": {"type": "text_delta", "text": text}}


def store_events(run_id, n):
    for _ in range(n):
        seq = buffer.next_seq(run_id)
        buffer.append_replay(run_id, evt(seq, run_id, f"delta {seq}"))


def row(w, run_id):
    return w.store.rows[run_id]


def nothing_left(w, run_id):
    assert w.redis.keys() == [] and w.redis.all_strings() == []
    assert not [k for k in w.guard.data if run_id in k]
    assert run_guard.cancel_reason(run_id) is None


# ---------------------------------------------------------------------------
# start_run
# ---------------------------------------------------------------------------

def test_start_records_the_run_and_creates_its_buffer(w):
    h = history(2)
    run = w.start("run-start-0001", payload=start_payload(h))
    assert (run.state, run.profile, run.mode, run.user_id, run.device_id) == ("created", "code", "ask", USER, DEVICE)
    conv = buffer.read_conversation(run.id)
    assert conv.items == h
    assert conv.state == start_payload() and "history" not in conv.state
    assert buffer.next_seq(run.id) == 1                                  # the stream starts at 1
    assert len(w.redis.keys()) == 3


def test_a_run_may_start_without_history(w):
    run = w.start("run-start-0002", payload=start_payload())
    assert buffer.read_conversation(run.id).items == []


def test_a_bad_payload_writes_nothing_and_the_error_never_quotes_it(w):
    bad = start_payload(cwd_label="/etc/" + CANARY, mode="wizard")
    with pytest.raises(run_manager.InvalidRunState) as info:
        w.start("run-start-0003", payload=bad)
    assert CANARY not in str(info.value) and CANARY not in repr(info.value)
    assert all(CANARY not in i for i in info.value.issues) and info.value.issues
    assert w.store.rows == {} and w.redis.requests == []


@pytest.mark.parametrize("payload", ["text", None, [], 5, {}, start_payload(history=[{"role": "wizard"}]),
                                     start_payload(history="x"), start_payload(tool_catalog_version="v9")])
def test_payloads_that_are_not_a_run_start_are_refused(w, payload):
    with pytest.raises(run_manager.InvalidRunState):
        w.start("run-start-0004", payload=payload)
    assert w.store.rows == {} and w.redis.requests == []


def test_an_additive_field_from_a_newer_client_is_accepted_like_the_socket_does(w):
    run = w.start("run-start-0005", payload=start_payload(future_field=1))
    assert buffer.read_conversation(run.id).state["future_field"] == 1


def test_without_a_secret_no_row_is_written(w):
    w.mp.delenv("RUN_BUFFER_SECRET")
    with pytest.raises(buffer.BufferNotConfigured):
        w.start("run-start-0006")
    assert w.store.rows == {} and w.redis.requests == []


def test_a_buffer_that_cannot_be_created_leaves_a_failed_run_not_a_created_zombie(w):
    w.redis.fail_if(lambda commands: True)
    with pytest.raises(buffer.BufferUnavailable):
        w.start("run-start-0007", max_parallel=1)
    w.redis.fail_if(None)
    r = row(w, "run-start-0007")
    assert (r["state"], r["error_code"]) == ("failed", "internal") and r["ended_at"] is not None
    w.start("run-start-0008", max_parallel=1)                           # the slot is free again


def test_a_creation_that_fails_part_way_leaves_no_buffer_and_a_failed_run(w):
    calls = {"n": 0}

    def third_request_fails(commands):
        calls["n"] += 1
        return calls["n"] == 3
    w.redis.fail_if(third_request_fails)
    with pytest.raises(buffer.BufferUnavailable):
        w.start("run-start-0009", payload=start_payload([{"role": "user", "text": "h" * 190_000}, {"role": "user", "text": "i" * 190_000}]))
    w.redis.fail_if(None)
    assert w.redis.keys() == [] and row(w, "run-start-0009")["state"] == "failed"


def test_a_duplicate_run_id_is_refused_and_the_first_buffer_is_untouched(w):
    w.start("run-start-0010", payload=start_payload(history(1, "original")))
    with pytest.raises(runs.RunIdCollision):
        w.start("run-start-0010", user=OTHER_USER, payload=start_payload(history(1, "intruder")))
    assert buffer.read_conversation("run-start-0010").items[0]["text"].startswith("original")


def test_the_parallel_run_cap_leaves_no_buffer_for_the_refused_run(w):
    w.start("run-start-0011", max_parallel=1)
    keys = w.redis.keys()
    with pytest.raises(runs.ParallelRunLimit):
        w.start("run-start-0012", max_parallel=1)
    assert w.redis.keys() == keys and "run-start-0012" not in w.store.rows


def test_an_unavailable_protocol_validator_fails_closed_before_anything_is_written(w):
    def boom():
        raise run_manager.ProtocolSchemasUnavailable()
    w.mp.setattr(run_manager, "_protocol", boom)
    with pytest.raises(run_manager.ProtocolSchemasUnavailable):
        w.start("run-start-0013")
    assert w.store.rows == {} and w.redis.requests == []


def test_the_real_protocol_loader_failing_is_reported_as_unavailable(w, caplog):
    from codeagent import ws

    def broken():
        raise RuntimeError("secret detail " + CANARY)
    w.mp.setattr(ws, "load_protocol", broken)
    with caplog.at_level("DEBUG"):
        with pytest.raises(run_manager.ProtocolSchemasUnavailable) as info:
            w.start("run-start-0014")
    assert CANARY not in str(info.value) and info.value.__cause__ is None
    assert CANARY not in caplog.text and "RuntimeError" in caplog.text


# ---------------------------------------------------------------------------
# Ending a run: nothing remains
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("to_state,code", [("completed", None), ("failed", "provider_unavailable"), ("expired", None)])
def test_ending_a_run_deletes_the_buffer_and_the_run_guard_rails(w, to_state, code):
    run = w.run_at("running", "run-end-0001")
    store_events(run.id, 4)
    w.guard_seed(run.id)
    assert w.redis.keys() and w.guard.data
    ended = run_manager.end_run(USER, run.id, to_state, error_code=code)
    assert (ended.state, ended.error_code) == (to_state, code) and ended.ended_at is not None
    nothing_left(w, run.id)
    with pytest.raises(buffer.BufferMissing):
        buffer.replay_after(run.id, 0)


def test_other_runs_buffers_and_rails_are_not_touched(w):
    a, b = w.run_at("running", "run-end-0002"), w.run_at("running", "run-end-0003")
    w.guard_seed(a.id), w.guard_seed(b.id)
    run_manager.end_run(USER, a.id, "completed")
    assert all("run-end-0003" in k for k in w.redis.keys()) and len(w.redis.keys()) == 3
    assert [k for k in w.guard.data if "run-end-0003" in k] and not [k for k in w.guard.data if "run-end-0002" in k]
    assert buffer.read_conversation(b.id).items


@pytest.mark.parametrize("to_state", ["cancelled", "running", "paused_error", "created", "bogus", None])
def test_end_run_only_ends_a_run_as_completed_failed_or_expired(w, to_state):
    run = w.run_at("running", "run-end-0004")
    with pytest.raises(runs.InvalidRun):
        run_manager.end_run(USER, run.id, to_state)
    assert row(w, run.id)["state"] == "running" and w.redis.keys()


@pytest.mark.parametrize("state", ["paused_budget", "paused_disconnected", "paused_error"])
def test_the_paused_states_keep_the_buffer(w, state):
    run = w.run_at("running", "run-pause-0001")
    store_events(run.id, 2)
    code = {"paused_budget": "quota_exhausted", "paused_error": "provider_unavailable"}.get(state)
    paused = run_manager.pause_run(USER, run.id, state, error_code=code)
    assert paused.state == state and paused.error_code == code
    assert len(w.redis.keys()) == 3 and len(buffer.replay_after(run.id, 0)) == 2
    w.advance(3600)
    assert buffer.exists(run.id)                                         # kept until its own TTL


def test_pause_run_only_pauses(w):
    run = w.run_at("running", "run-pause-0002")
    for bad in ("completed", "cancelled", "running", "bogus"):
        with pytest.raises(runs.InvalidRun):
            run_manager.pause_run(USER, run.id, bad)


def test_ending_an_ended_run_is_refused_and_does_not_resurrect_anything(w):
    run = w.run_at("running", "run-end-0005")
    run_manager.end_run(USER, run.id, "completed")
    with pytest.raises(runs.IllegalTransition):
        run_manager.end_run(USER, run.id, "failed", error_code="internal")
    nothing_left(w, run.id)


def test_racing_ends_have_exactly_one_winner_and_nothing_is_left(w):
    run = w.run_at("running", "run-end-0006")
    store_events(run.id, 3)
    outcomes, barrier = [], threading.Barrier(8)

    def go(state):
        barrier.wait()
        try:
            run_manager.end_run(USER, run.id, state)
            outcomes.append("won")
        except runs.IllegalTransition:
            outcomes.append("lost")
    threads = [threading.Thread(target=go, args=(s,)) for s in ["completed", "failed", "expired", "completed"] * 2]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(outcomes) == ["lost"] * 7 + ["won"]
    nothing_left(w, run.id)


def test_a_buffer_that_cannot_be_deleted_is_reported_but_the_run_is_over_and_purge_finishes_it(w):
    run = w.run_at("running", "run-end-0007")
    w.guard_seed(run.id)
    w.redis.fail_if(lambda commands: commands[0][0] == "DEL")
    with pytest.raises(run_manager.CleanupIncomplete) as info:
        run_manager.end_run(USER, run.id, "completed")
    assert info.value.run.state == "completed" and info.value.__cause__ is None
    assert row(w, run.id)["state"] == "completed"
    assert not [k for k in w.guard.data if run.id in k]                  # the rails are cleared regardless
    assert w.redis.keys()
    w.redis.fail_if(None)
    run_manager.purge_run(USER, run.id)
    nothing_left(w, run.id)
    run_manager.purge_run(USER, run.id)                                  # idempotent


def test_purge_refuses_an_open_run_and_someone_elses(w):
    run = w.run_at("running", "run-end-0008")
    with pytest.raises(run_manager.RunNotEnded) as info:
        run_manager.purge_run(USER, run.id)
    assert info.value.state == "running" and len(w.redis.keys()) == 3
    with pytest.raises(runs.RunNotFound):
        run_manager.purge_run(OTHER_USER, run.id)


# ---------------------------------------------------------------------------
# cancel_run
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["created", "running", "awaiting_approval", "paused_budget", "paused_disconnected", "paused_error"])
def test_cancel_moves_every_open_state_to_cancelled_and_leaves_nothing(w, state):
    run = w.run_at(state, "run-cancel-0001")
    store_events(run.id, 2)
    w.guard_seed(run.id)
    cancelled = run_manager.cancel_run(USER, run.id)
    assert cancelled.state == "cancelled" and cancelled.ended_at is not None
    nothing_left(w, run.id)


def test_the_cancel_flag_is_set_before_the_run_moves(w):
    run = w.run_at("running", "run-cancel-0002")
    seen = []
    w.store.before_move = lambda call: seen.append((call["to_state"], run_guard.cancel_reason(run.id)))
    run_manager.cancel_run(USER, run.id)
    assert seen == [("cancelled", "cancelled")]                          # a worker polling now would stop
    assert run_guard.cancel_reason(run.id) is None                       # and the rail is cleaned up after


def test_a_cancel_may_carry_an_error_code(w):
    run = w.run_at("running", "run-cancel-0003")
    assert run_manager.cancel_run(USER, run.id, error_code="device_revoked").error_code == "device_revoked"


def test_cancelling_someone_elses_run_sets_no_flag(w):
    run = w.run_at("running", "run-cancel-0004")
    with pytest.raises(runs.RunNotFound):
        run_manager.cancel_run(OTHER_USER, run.id)
    assert w.guard.data == {} and run_guard.cancel_reason(run.id) is None
    assert row(w, run.id)["state"] == "running" and len(w.redis.keys()) == 3


def test_cancelling_an_ended_run_is_refused_and_leaves_no_flag(w):
    run = w.run_at("running", "run-cancel-0005")
    run_manager.end_run(USER, run.id, "completed")
    with pytest.raises(runs.IllegalTransition):
        run_manager.cancel_run(USER, run.id)
    assert w.guard.data == {} and run_guard.cancel_reason(run.id) is None


def test_a_second_cancel_is_refused_and_leaves_nothing(w):
    run = w.run_at("running", "run-cancel-0006")
    run_manager.cancel_run(USER, run.id)
    with pytest.raises(runs.IllegalTransition):
        run_manager.cancel_run(USER, run.id)
    nothing_left(w, run.id)


def test_a_run_ended_by_someone_else_between_the_read_and_the_move_leaves_no_flag(w):
    run = w.run_at("running", "run-cancel-0007")
    w.store.before_move = lambda call: call["store"].force_state(run.id, "completed")
    with pytest.raises(runs.IllegalTransition):
        run_manager.cancel_run(USER, run.id)
    assert w.guard.data == {} and run_guard.cancel_reason(run.id) is None


def test_when_the_store_is_down_the_cancel_flag_stays_and_the_buffer_is_untouched(w):
    run = w.run_at("running", "run-cancel-0008")

    def boom(**kwargs):
        raise RuntimeError("connection to " + CANARY)
    w.store.move = boom
    with pytest.raises(runs.RunStoreUnavailable):
        run_manager.cancel_run(USER, run.id)
    assert run_guard.cancel_reason(run.id) == "cancelled"                # the user's intent is honoured
    assert len(w.redis.keys()) == 3 and row(w, run.id)["state"] == "running"


# ---------------------------------------------------------------------------
# resume_run
# ---------------------------------------------------------------------------

def resume(w, run_id, last_seq=0, *, state=None, user=USER, device=DEVICE, max_parallel=None):
    return run_manager.resume_run(user_id=user, device_id=device, run_id=run_id, last_seq=last_seq,
                                  state=state, max_parallel=max_parallel)


def test_a_live_buffer_replays_the_events_after_last_seq_verbatim_and_the_run_runs_again(w):
    run = w.run_at("paused_disconnected", "run-resume-0001")
    store_events(run.id, 5)
    sent = buffer.replay_after(run.id, 0)
    got = resume(w, run.id, 2)
    assert (got.rebuilt, got.run.state) == (False, "running")
    assert got.replay == sent[2:] and [m["seq"] for m in got.replay] == [3, 4, 5]
    assert row(w, run.id)["state"] == "running"
    assert [m["seq"] for m in resume(w, run.id, 5).replay] == []         # nothing new


def test_resume_refreshes_the_buffers_idle_ttl(w):
    run = w.run_at("paused_disconnected", "run-resume-0002")
    w.advance(3600)
    resume(w, run.id)
    for key in buffer.buffer_keys(run.id):
        assert w.redis.ttl(key) == buffer.IDLE_TTL_SECONDS


def test_a_live_buffer_wins_over_the_state_the_device_sent(w):
    run = w.run_at("paused_disconnected", "run-resume-0003", payload=start_payload(history(1, "server")))
    got = resume(w, run.id, 0, state=start_payload(history(5, "device")))
    assert got.rebuilt is False
    assert [i["text"] for i in buffer.read_conversation(run.id).items][0].startswith("server")
    assert len(buffer.read_conversation(run.id).items) == 2


def test_an_expired_buffer_is_rebuilt_from_the_devices_history_and_the_run_continues(w):
    run = w.run_at("paused_disconnected", "run-resume-0004")
    store_events(run.id, 5)
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    assert w.redis.keys() == []
    device_history = history(3, "from the device log")
    got = resume(w, run.id, 5, state=start_payload(device_history))
    assert (got.rebuilt, got.replay, got.run.state) == (True, [], "running")
    conv = buffer.read_conversation(run.id)
    assert conv.items == device_history and conv.state == start_payload()
    assert buffer.next_seq(run.id) == 6                                   # continues from the device's last_seq
    assert len(w.redis.keys()) == 3


def test_a_buffer_past_the_hard_cap_is_rebuilt_too(w):
    run = w.run_at("paused_disconnected", "run-resume-0005")
    for _ in range(23):
        w.advance(3600)
        buffer.touch(run.id)                                              # kept alive by use ...
    w.advance(3600 + 1)
    got = resume(w, run.id, 0, state=start_payload(history(1)))          # ... until the cap
    assert got.rebuilt and buffer.read_conversation(run.id).items == history(1)


def test_a_missing_buffer_without_state_is_a_clear_error_and_changes_nothing(w):
    run = w.run_at("paused_disconnected", "run-resume-0006")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    with pytest.raises(run_manager.RunGone):
        resume(w, run.id, 3)
    assert row(w, run.id)["state"] == "paused_disconnected" and w.redis.keys() == []


def test_an_unreadable_buffer_is_rebuilt_only_when_a_valid_state_is_in_hand(w):
    run = w.run_at("paused_disconnected", "run-resume-0007", payload=start_payload(history(1, "old")))
    w.mp.setenv("RUN_BUFFER_SECRET", bt.OTHER_SECRET)                    # the deployed secret changed
    keys_before = w.redis.keys()
    with pytest.raises(run_manager.RunGone):                             # no state: leave it alone
        resume(w, run.id, 0)
    assert w.redis.keys() == keys_before
    with pytest.raises(run_manager.InvalidRunState):                     # bad state: leave it alone
        resume(w, run.id, 0, state=start_payload(history=[{"role": "wizard"}]))
    assert w.redis.keys() == keys_before and row(w, run.id)["state"] == "paused_disconnected"
    got = resume(w, run.id, 0, state=start_payload(history(2, "new")))   # valid state: rebuilt under the new key
    assert got.rebuilt and buffer.read_conversation(run.id).items == history(2, "new")


def test_the_state_is_validated_before_anything_is_touched_and_never_quoted(w):
    run = w.run_at("paused_disconnected", "run-resume-0008")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    bad = start_payload(history=[{"role": "tool", "call_id": "c1", "ok": "yes", "output": CANARY}])
    with pytest.raises(run_manager.InvalidRunState) as info:
        resume(w, run.id, 0, state=bad)
    assert CANARY not in str(info.value) and all(CANARY not in i for i in info.value.issues) and info.value.issues
    assert w.redis.keys() == [] and row(w, run.id)["state"] == "paused_disconnected"


def test_a_state_for_another_profile_is_refused(w):
    run = w.run_at("paused_disconnected", "run-resume-0009")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    run_manager_state = start_payload(history(1))
    w.store.rows[run.id]["profile"] = "menux"                            # the row says another profile
    with pytest.raises(run_manager.InvalidRunState):
        resume(w, run.id, 0, state=run_manager_state)
    assert w.redis.keys() == []


@pytest.mark.parametrize("bad", [-1, 1.5, "3", True, None])
def test_a_bad_last_seq_is_refused(w, bad):
    run = w.run_at("paused_disconnected", "run-resume-0010")
    with pytest.raises(runs.InvalidRun):
        resume(w, run.id, bad)
    assert row(w, run.id)["state"] == "paused_disconnected"


def test_another_device_or_another_account_cannot_resume_a_run(w):
    run = w.run_at("paused_disconnected", "run-resume-0011")
    for kw in ({"device": OTHER_DEVICE}, {"user": OTHER_USER}):
        with pytest.raises(runs.RunNotFound):
            resume(w, run.id, 0, **kw)
    with pytest.raises(runs.RunNotFound):
        resume(w, "run-resume-9999", 0)
    assert row(w, run.id)["state"] == "paused_disconnected"


def test_the_device_id_is_compared_case_insensitively(w):
    run = w.run_at("paused_disconnected", "run-resume-0012")
    assert resume(w, run.id, 0, device=DEVICE.upper()).run.state == "running"


@pytest.mark.parametrize("final", ["completed", "cancelled", "failed", "expired"])
def test_an_ended_run_cannot_be_resumed_and_gets_no_buffer(w, final):
    run = w.run_at("running", "run-resume-0013")
    if final == "cancelled":
        run_manager.cancel_run(USER, run.id)
    else:
        run_manager.end_run(USER, run.id, final)
    with pytest.raises(run_manager.RunNotResumable) as info:
        resume(w, run.id, 0, state=start_payload(history(1)))
    assert info.value.state == final
    nothing_left(w, run.id)


def test_a_run_that_stopped_for_budget_is_reattached_but_stays_paused(w):
    run = w.run_at("paused_budget", "run-resume-0014")
    store_events(run.id, 2)
    got = resume(w, run.id, 1)
    assert got.run.state == "paused_budget" and [m["seq"] for m in got.replay] == [2]


def test_a_paused_error_run_is_resumed(w):
    run = w.run_at("paused_error", "run-resume-0015")
    assert resume(w, run.id, 0).run.state == "running"


@pytest.mark.parametrize("state", ["created", "running", "awaiting_approval"])
def test_a_run_the_server_still_thinks_is_live_is_reattached_as_it_is(w, state):
    run = w.run_at(state, "run-resume-0016")
    store_events(run.id, 2)
    got = resume(w, run.id, 0)
    assert got.run.state == state and len(got.replay) == 2


def test_a_second_resume_while_running_is_harmless(w):
    run = w.run_at("paused_disconnected", "run-resume-0017")
    assert resume(w, run.id, 0).run.state == "running"
    assert resume(w, run.id, 0).run.state == "running"


def test_a_full_device_keeps_the_run_paused_and_the_buffer_alive(w):
    held = w.run_at("paused_disconnected", "run-resume-0018", max_parallel=1)
    w.run_at("running", "run-resume-0019", max_parallel=1)               # the device's only slot
    with pytest.raises(runs.ParallelRunLimit):
        resume(w, held.id, 0, max_parallel=1)
    assert row(w, held.id)["state"] == "paused_disconnected" and buffer.exists(held.id)


def test_a_resume_that_loses_a_race_with_the_sweep_leaves_no_buffer_behind(w):
    run = w.run_at("paused_disconnected", "run-resume-0020")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    w.store.before_move = lambda call: (call["store"].force_state(run.id, "expired")
                                        if call["to_state"] == "running" else None)
    with pytest.raises(runs.IllegalTransition):
        resume(w, run.id, 0, state=start_payload(history(1)))
    assert w.redis.keys() == [] and row(w, run.id)["state"] == "expired"


def test_resume_fails_closed_when_redis_is_down_and_does_not_move_the_run(w):
    run = w.run_at("paused_disconnected", "run-resume-0021")
    w.redis.fail_if(lambda commands: True)
    with pytest.raises(buffer.BufferUnavailable):
        resume(w, run.id, 0, state=start_payload(history(1)))
    assert row(w, run.id)["state"] == "paused_disconnected"


def test_resume_fails_closed_without_a_secret_and_does_not_move_the_run(w):
    run = w.run_at("paused_disconnected", "run-resume-0022")
    w.mp.delenv("RUN_BUFFER_SECRET")
    with pytest.raises(buffer.BufferNotConfigured):
        resume(w, run.id, 0, state=start_payload(history(1)))
    assert row(w, run.id)["state"] == "paused_disconnected"


def test_a_rebuild_that_fails_part_way_leaves_nothing_and_the_run_paused(w):
    run = w.run_at("paused_disconnected", "run-resume-0023")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    calls = {"n": 0}

    def third_request_fails(commands):
        calls["n"] += 1
        return calls["n"] == 4
    w.redis.fail_if(third_request_fails)
    with pytest.raises(buffer.BufferUnavailable):
        resume(w, run.id, 0, state=start_payload([{"role": "user", "text": "h" * 190_000}, {"role": "user", "text": "i" * 190_000}]))
    w.redis.fail_if(None)
    assert w.redis.keys() == [] and row(w, run.id)["state"] == "paused_disconnected"


# ---------------------------------------------------------------------------
# expire_stale
# ---------------------------------------------------------------------------

def sweep(w, **kw):
    return run_manager.expire_stale(w.now, **kw)


def test_an_idle_run_whose_buffer_expired_is_moved_to_expired_and_cleaned_up(w):
    run = w.run_at("paused_disconnected", "run-sweep-0001")
    w.guard_seed(run.id)
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    report = sweep(w)
    assert (report.examined, report.expired, report.kept) == (1, 1, 0)
    r = row(w, run.id)
    assert (r["state"], r["error_code"]) == ("expired", None) and r["ended_at"] is not None
    nothing_left(w, run.id)


@pytest.mark.parametrize("state", ["created", "running", "awaiting_approval", "paused_budget", "paused_disconnected", "paused_error"])
def test_every_open_state_can_be_swept(w, state):
    run = w.run_at(state, "run-sweep-0002")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    assert sweep(w).expired == 1 and row(w, run.id)["state"] == "expired"


def test_a_run_with_a_live_buffer_is_never_expired(w):
    run = w.run_at("paused_disconnected", "run-sweep-0003")
    w.advance(3600)
    report = sweep(w)
    assert (report.examined, report.expired, report.kept) == (1, 0, 1)
    assert row(w, run.id)["state"] == "paused_disconnected" and len(w.redis.keys()) == 3


def test_a_run_that_is_not_idle_yet_is_not_looked_at(w):
    w.run_at("running", "run-sweep-0004")
    w.redis.store.clear()                                                # no buffer ... but it was just created
    w.advance(run_manager.SWEEP_MIN_IDLE_SECONDS - 10)
    assert sweep(w).examined == 0
    w.advance(20)
    assert sweep(w).expired == 1


def test_a_heartbeat_keeps_a_run_out_of_the_sweep(w):
    run = w.run_at("running", "run-sweep-0005")
    w.redis.store.clear()
    w.advance(run_manager.SWEEP_MIN_IDLE_SECONDS - 10)
    runs.add_progress(USER, run.id)                                      # a heartbeat refreshes updated_at
    w.advance(100)
    assert sweep(w).examined == 0


def test_ended_runs_are_not_looked_at(w):
    run = w.run_at("running", "run-sweep-0006")
    run_manager.end_run(USER, run.id, "completed")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    assert sweep(w).examined == 0 and row(w, run.id)["state"] == "completed"


def test_the_24_hour_cap_is_applied_through_the_sweep(w):
    run = w.run_at("paused_disconnected", "run-sweep-0007")
    for _ in range(23):
        w.advance(3600)
        buffer.touch(run.id)                                             # the idle TTL is refreshed every hour
    assert sweep(w).expired == 0 and buffer.exists(run.id)               # hour 23: still inside the cap
    w.advance(3600)
    report = sweep(w)
    assert report.expired == 1 and row(w, run.id)["state"] == "expired"
    nothing_left(w, run.id)


def test_a_rebuilt_run_older_than_a_day_is_not_expired_by_its_rows_age(w):
    run = w.run_at("paused_disconnected", "run-sweep-0008")
    w.advance(30 * 3600)                                                 # the buffer is long gone
    resume(w, run.id, 0, state=start_payload(history(1)))                # the device rebuilt it
    runs.transition(USER, run.id, "paused_disconnected")
    w.advance(run_manager.SWEEP_MIN_IDLE_SECONDS + 1)
    report = sweep(w)
    assert (report.expired, report.kept) == (0, 1) and row(w, run.id)["state"] == "paused_disconnected"


def test_a_buffer_that_cannot_be_read_is_left_alone_and_counted(w):
    run = w.run_at("paused_disconnected", "run-sweep-0009")
    w.advance(run_manager.SWEEP_MIN_IDLE_SECONDS + 1)
    w.mp.setenv("RUN_BUFFER_SECRET", bt.OTHER_SECRET)
    report = sweep(w)
    assert (report.unreadable, report.expired) == (1, 0)
    assert row(w, run.id)["state"] == "paused_disconnected" and len(w.redis.keys()) == 3


def test_when_redis_is_down_nothing_is_expired(w):
    run = w.run_at("paused_disconnected", "run-sweep-0010")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    w.redis.fail_if(lambda commands: True)
    with pytest.raises(buffer.BufferUnavailable):
        sweep(w)
    assert row(w, run.id)["state"] == "paused_disconnected"


def test_without_a_secret_nothing_is_expired(w):
    run = w.run_at("paused_disconnected", "run-sweep-0011")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    w.mp.delenv("RUN_BUFFER_SECRET")
    with pytest.raises(buffer.BufferNotConfigured):
        sweep(w)
    assert row(w, run.id)["state"] == "paused_disconnected"


def test_a_run_ended_by_someone_else_between_the_read_and_the_move_is_counted_as_raced(w):
    run = w.run_at("paused_disconnected", "run-sweep-0012")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    w.sweep.after_list = lambda rows: w.store.force_state(run.id, "completed")
    report = sweep(w)
    assert (report.raced, report.expired) == (1, 0) and row(w, run.id)["state"] == "completed"


def test_a_buffer_that_cannot_be_deleted_is_counted_but_the_run_is_expired(w):
    run = w.run_at("paused_disconnected", "run-sweep-0013")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    w.redis.fail_if(lambda commands: commands[0][0] == "DEL")
    report = sweep(w)
    assert (report.expired, report.cleanup_failed) == (1, 1) and row(w, run.id)["state"] == "expired"


def test_healthy_old_runs_at_the_front_do_not_starve_the_ones_behind_them(w):
    for i in range(5):
        w.run_at("paused_disconnected", f"run-sweep-live{i}")
        w.advance(1)
    target = w.run_at("paused_disconnected", "run-sweep-dead1")
    buffer.delete_buffer(target.id)                                      # only this one lost its buffer
    w.advance(run_manager.SWEEP_MIN_IDLE_SECONDS + 1)
    report = sweep(w, page_size=2)
    assert (report.examined, report.kept, report.expired) == (6, 5, 1)
    assert w.sweep.calls >= 3 and row(w, target.id)["state"] == "expired"


def test_one_sweep_looks_at_no_more_than_max_runs(w):
    for i in range(6):
        w.run_at("paused_disconnected", f"run-sweep-many{i}")
    w.advance(run_manager.SWEEP_MIN_IDLE_SECONDS + 1)
    assert sweep(w, max_runs=4, page_size=3).examined == 4
    assert sweep(w, max_runs=0).examined == 0


def test_now_may_be_an_aware_or_naive_datetime_or_a_unix_time(w):
    w.run_at("paused_disconnected", "run-sweep-0014")
    w.advance(buffer.IDLE_TTL_SECONDS + 1)
    t = w.rt_clock.t
    for now in (w.now, datetime.fromtimestamp(t, tz=timezone.utc).replace(tzinfo=None), t):
        assert run_manager.expire_stale(now).examined in (0, 1)
    assert row(w, "run-sweep-0014")["state"] == "expired"


@pytest.mark.parametrize("kw", [{"min_idle_seconds": -1}, {"max_runs": -1}, {"page_size": 0}, {"page_size": True},
                                {"max_runs": 1.5}])
def test_bad_sweep_limits_are_refused(w, kw):
    with pytest.raises(runs.InvalidRun):
        sweep(w, **kw)


@pytest.mark.parametrize("now", ["now", [], object()])
def test_a_bad_now_is_refused(w, now):
    with pytest.raises(runs.InvalidRun):
        run_manager.expire_stale(now)


def test_a_store_fault_in_the_sweep_is_runstoreunavailable_with_fixed_text_and_no_cause(w, caplog):
    class Boom:
        def list_open(self, **kwargs):
            raise RuntimeError("connection to " + CANARY)
    w.mp.setattr(run_manager, "_SWEEP", Boom())
    with caplog.at_level("DEBUG"):
        with pytest.raises(runs.RunStoreUnavailable) as info:
            sweep(w)
    assert CANARY not in str(info.value) and CANARY not in repr(info.value) and info.value.__cause__ is None
    assert CANARY not in caplog.text and "RuntimeError" in caplog.text


# ---------------------------------------------------------------------------
# Logging and content
# ---------------------------------------------------------------------------

def test_nothing_but_ids_state_names_and_error_types_reaches_the_log(w, caplog):
    secret_history = [{"role": "user", "text": CANARY}, {"role": "assistant", "text": CANARY}]
    with caplog.at_level("DEBUG"):
        run = w.start("run-log-0001", payload=start_payload(secret_history, instructions=CANARY))
        store_events(run.id, 2)
        runs.transition(USER, run.id, "running")
        run_manager.pause_run(USER, run.id, "paused_disconnected")
        resume(w, run.id, 0)
        w.advance(buffer.IDLE_TTL_SECONDS + 1)
        resume(w, run.id, 1, state=start_payload(secret_history, instructions=CANARY))
        w.redis.fail_if(lambda commands: commands[0][0] == "DEL")
        with pytest.raises(run_manager.CleanupIncomplete):
            run_manager.end_run(USER, run.id, "completed")
        w.redis.fail_if(None)
        run_manager.purge_run(USER, run.id)
        sweep(w)
    assert CANARY not in caplog.text and bt.SECRET not in caplog.text
    assert "run_buffer:" not in caplog.text


def test_no_content_is_stored_in_redis_in_the_clear_by_a_run(w):
    w.start("run-log-0002", payload=start_payload([{"role": "user", "text": CANARY}], instructions=CANARY))
    assert all(CANARY not in s for s in w.redis.all_strings())
    assert all(CANARY not in str(r) for r in w.redis.requests)
    assert all(CANARY not in str(v) for r in w.store.rows.values() for v in r.values())    # nor in the run row


def test_the_error_classes_carry_no_content():
    for exc in (run_manager.RunNotResumable("completed"), run_manager.RunGone(), run_manager.InvalidRunState(["a: b"]),
                run_manager.ProtocolSchemasUnavailable(), run_manager.RunNotEnded("running")):
        assert str(exc) and CANARY not in str(exc)
    assert run_manager.InvalidRunState(["x"] * 50).issues == ["x"] * 20


def test_importing_the_module_does_not_import_fastapi_or_the_bus():
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(run_manager.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top for a in n.names} | {n.module for n in top if isinstance(n, ast.ImportFrom)}
    assert not any(str(x).startswith(("fastapi", "starlette", "memory", "psycopg")) for x in names if x)
    assert "codeagent.ws" not in {f"codeagent.{a.name}" for n in top if isinstance(n, ast.ImportFrom)
                                  and n.module == "codeagent" for a in n.names}


def test_require_configured_touches_nothing_and_matches_the_buffers_rules(w):
    buffer.require_configured()
    w.mp.setenv("RUN_BUFFER_SECRET", "short")
    with pytest.raises(buffer.BufferNotConfigured):
        buffer.require_configured()
    assert w.redis.requests == []
