"""Patch S10 -- tests for api/routes/code_runs.py: POST /v1/code/runs/{run_id}/grant ("grant more").

The route is called by the DEVICE with its device JWT. Everything is real except the stores: S03's FakeStore stands
in for the devices table (real JWTs are minted by eo.devices), run_manager_testkit.World for the runs table, the run
buffer and run_guard's Redis, and metering_testkit.FakeLedger for the usage ledger. The emitter, the run handlers
and the AgentLoop are the real objects. Requests go through httpx's ASGI transport inside the test's own event
loop, so the restart task the route starts lives long enough to be seen.

Every rejection is checked the same way: the run, the ceiling, run_guard's keys, the event stream and the hooks are
exactly as they were before the request.
"""
import asyncio
import logging
import uuid

import httpx
import jwt
import pytest
from fastapi import FastAPI

from api.routes import code_runs
from codeagent import buffer, connections, metering, run_manager, runs
from codeagent.emitter import RunEmitter
from codeagent.run_handlers import RunHandlers
from eo import devices, run_guard
from tests.unit.devices_testkit import SECRET, SUPABASE_SECRET, Clock, FakeStore, sha256
from tests.unit.loop_testkit import FakeConn, Rig, go, protocol_problems
from tests.unit.metering_testkit import CANARY, RESET_AT, FakeLedger, Harness, priced, tool_call
from tests.unit.run_manager_testkit import DEVICE, OTHER_DEVICE, OTHER_USER, USER, World, start_payload

pytest.importorskip("jsonschema", reason="the loop validates tool arguments with jsonschema")

RUN = "run-grant-000001"
NO_SUCH_RUN = "run-grant-nobody1"


class CapturingConn(FakeConn):
    """What the device would receive."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sent = []

    async def send_json(self, envelope):
        await super().send_json(envelope)
        self.sent.append(envelope)


class GrantRig:
    """The route over fakes. `account_pool` / `external_used` set the pool; `fixed` and `chunk` the ceiling policy."""

    def __init__(self, monkeypatch, *, world=None, ledger=None, store=None, policy=None, emitter=None,
                 registry=None, conn=None, max_parallel=3, paused=True, run_id=RUN):
        monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
        monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)
        self.world = world or World(monkeypatch)
        self.clock = Clock()
        self.devices = FakeStore(self.clock)
        monkeypatch.setattr(devices, "_STORE", self.devices)
        self.run_id = run_id
        self.credential = {}
        self.credential[(USER, DEVICE)] = self.add_device(DEVICE, USER)
        self.credential[(OTHER_USER, OTHER_DEVICE)] = self.add_device(OTHER_DEVICE, OTHER_USER)

        self.registry = registry or connections.InMemoryRegistry()
        self.conn = conn or CapturingConn()
        if conn is None or registry is None:
            self.registry.add(self.conn)
        self.emitter = emitter or RunEmitter(registry=lambda: self.registry)
        self.handlers = RunHandlers(emitter=self.emitter, max_parallel=max_parallel)
        self.hook_calls = []

        async def hook(conn, run):
            self.hook_calls.append((run.id, run.state))
        self.handlers.add_on_resume(hook)

        self.ledger = ledger or FakeLedger(pool=1000.0)
        self.budget = store or metering.RunBudgetStore(redis=lambda: self.world.guard)
        self.policy = policy or metering.RunCeilingPolicy(fixed=2.0, chunk_fixed=5.0)
        self.services = code_runs.GrantServices(
            check=self.ledger.check, run_credits=self.ledger.run_credits, store=self.budget, policy=self.policy,
            registry=lambda: self.registry, handlers=lambda: self.handlers, emitter=lambda: self.emitter)
        self.app = FastAPI()
        self.app.include_router(code_runs.router)
        self.app.dependency_overrides[code_runs.get_services] = lambda: self.services
        if paused and world is None:
            self.world.run_at("paused_budget", run_id)

    # -- devices and tokens ----------------------------------------------------------------------
    def add_device(self, device_id, user_id):
        credential = "cred_" + uuid.uuid4().hex + uuid.uuid4().hex[:8]       # built at run time, never a literal
        self.devices.devices[device_id] = {
            "id": device_id, "user_id": user_id, "device_name": "test", "os": "Linux", "client_version": "0.0.1",
            "credential_hash": sha256(credential), "created_at": self.clock.t, "last_seen_at": None,
            "revoked_at": None}
        return credential

    def token(self, user=USER, device=DEVICE):
        return devices.issue_device_token(self.credential[(user, device)])["jwt"]

    def revoke(self, device=DEVICE):
        self.devices.devices[device]["revoked_at"] = self.clock.t

    # -- requests ----------------------------------------------------------------------------------
    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def grant(self, run_id=None, token="default", headers=None):
        if token == "default":
            token = self.token()
        hdrs = dict(headers or {})
        if token is not None:
            hdrs.setdefault("Authorization", f"Bearer {token}")
        async with self.client() as client:
            return await client.post(f"/v1/code/runs/{run_id or self.run_id}/grant", headers=hdrs)

    # -- what "changes nothing" means --------------------------------------------------------------
    def snapshot(self):
        run = runs.get_run(USER, self.run_id)
        return {"state": run.state, "error_code": run.error_code, "credits": run.credits,
                "ceiling": self.budget.ceiling(self.run_id), "guard": dict(self.world.guard.data),
                "sent": len(getattr(self.conn, "sent", [])), "hooks": list(self.hook_calls),
                "bound": self.emitter.is_bound(self.run_id), "pool": self.ledger.pool,
                "rows": len(self.ledger.rows)}


@pytest.fixture
def rig(monkeypatch):
    made = GrantRig(monkeypatch)
    yield made
    made.world.teardown()


def detail(resp):
    return resp.json()["detail"]


# =============================================================================================
# A successful grant
# =============================================================================================

def test_a_grant_raises_the_run_ceiling_by_one_chunk_moves_the_run_and_restarts_it(rig):
    runs.add_progress(USER, RUN, steps=2, credits=2.0)                        # the run stopped at its ceiling of 2
    rig.world.guard_seed(RUN)                                                 # a tripped breaker, a cancel flag ...

    async def scenario():
        resp = await rig.grant()
        await asyncio.sleep(0.05)                                             # the restart is a task
        return resp

    resp = go(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"state": "running", "run_credit_ceiling": 7.0, "run_credits_spent": 2.0,
                    "granted_credits": 5.0}                                   # max(ceiling 2, spent 2) + chunk 5
    run = runs.get_run(USER, RUN)
    assert run.state == "running" and run.error_code is None
    assert rig.budget.ceiling(RUN) == 7.0
    assert run_guard._k("tripped", RUN) not in rig.world.guard.data           # run_guard.grant_more ran: a fresh chance
    assert run_guard._k("cancel", RUN) not in rig.world.guard.data

    (event,) = rig.conn.sent                                                  # the device is told, with a seq
    assert event["t"] == "run_event" and event["seq"] == 1
    assert event["payload"] == {"type": "state_changed", "state": "running", "reason": "budget_granted"}
    assert protocol_problems(rig.conn.sent) == []
    assert rig.hook_calls == [(RUN, "running")]                               # the on_resume hooks restarted the turn
    assert rig.emitter.is_bound(RUN)                                          # a running run is attached again


def test_the_pool_is_never_raised_by_a_grant(rig):
    pool_before = rig.ledger.pool
    go(rig.grant())
    assert rig.ledger.pool == pool_before and rig.ledger.rows == []
    assert rig.ledger.check_calls == 1                                        # the pool was asked, not changed


def test_a_second_grant_for_the_same_pause_is_refused_and_changes_nothing(rig):
    assert go(rig.grant()).status_code == 200
    after_first = rig.snapshot()
    again = go(rig.grant())
    assert again.status_code == 409 and detail(again)["code"] == "not_paused_budget"
    assert rig.snapshot() == after_first


def test_two_simultaneous_grants_raise_the_ceiling_once(rig):
    async def scenario():
        return await asyncio.gather(rig.grant(), rig.grant())

    first, second = go(scenario())
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    assert rig.budget.ceiling(RUN) == 7.0                                     # one chunk, not two
    assert len(rig.hook_calls) == 1


def test_a_run_that_overshot_its_ceiling_still_gets_a_full_chunk(rig):
    runs.add_progress(USER, RUN, steps=3, credits=2.75)                       # the last call took it past the ceiling of 2
    resp = go(rig.grant())
    assert resp.json()["run_credit_ceiling"] == 7.75                          # 2.75 + 5, not 2 + 5


def test_the_ledgers_figure_counts_when_it_is_larger_than_the_run_rows(rig):
    rig.ledger.commit(user_id=USER, run_id=RUN, credits=4.0)                  # the run row's counter missed a write
    resp = go(rig.grant())
    assert resp.json()["run_credits_spent"] == 4.0 and resp.json()["run_credit_ceiling"] == 9.0


def test_a_second_pause_and_grant_stacks_on_the_raised_ceiling(rig):
    go(rig.grant())
    runs.add_progress(USER, RUN, steps=5, credits=7.5)
    run_manager.pause_run(USER, RUN, "paused_budget", error_code="quota_exhausted")
    resp = go(rig.grant())
    assert resp.status_code == 200 and resp.json()["run_credit_ceiling"] == 12.5     # max(7, 7.5) + 5


def test_the_response_and_the_logs_carry_numbers_and_state_words_only(rig, caplog):
    caplog.set_level(logging.DEBUG)
    token = rig.token()
    resp = go(rig.grant(token=token))
    assert set(resp.json()) == {"state", "run_credit_ceiling", "run_credits_spent", "granted_credits"}
    assert all(isinstance(v, (int, float)) or v == "running" for v in resp.json().values())
    for secret in (token, rig.credential[(USER, DEVICE)], SECRET):
        assert secret not in caplog.text and secret not in resp.text
    assert any("grant" in r.getMessage() for r in caplog.records)


# =============================================================================================
# Every rejection changes nothing
# =============================================================================================

def refused(rig, status, code=None, **kw):
    before = rig.snapshot()
    resp = go(rig.grant(**kw))
    assert resp.status_code == status, resp.text
    if code is not None:
        assert detail(resp)["code"] == code
    assert rig.snapshot() == before
    assert rig.budget.ceiling(RUN) is None
    return resp


def test_a_request_without_a_device_token_is_refused(rig):
    refused(rig, 401, token=None)
    refused(rig, 401, token="not-a-jwt")
    refused(rig, 401, headers={"Authorization": "Basic abc"}, token=None)


def test_only_a_device_token_is_accepted(rig):
    now = 1_900_000_000
    not_device = jwt.encode({"sub": USER, "device_id": DEVICE, "token_type": "access", "aud": "m2c-device",
                             "iat": now - 10, "exp": now + 900}, SECRET, algorithm="HS256")
    wrong_key = jwt.encode({"sub": USER, "device_id": DEVICE, "token_type": "device", "aud": "m2c-device",
                            "iat": now - 10, "exp": now + 900}, "k" * 40, algorithm="HS256")
    web_user = jwt.encode({"sub": USER, "aud": "authenticated", "iat": now - 10, "exp": now + 900},
                          SUPABASE_SECRET, algorithm="HS256")
    for token in (not_device, wrong_key, web_user):
        refused(rig, 401, token=token)


def test_an_expired_device_token_is_refused(rig):
    now = 1_000_000_000
    expired = jwt.encode({"sub": USER, "device_id": DEVICE, "token_type": "device", "aud": "m2c-device",
                          "iat": now - 2000, "exp": now - 1000}, SECRET, algorithm="HS256")
    refused(rig, 401, token=expired)


def test_a_revoked_device_is_refused_even_with_a_token_that_has_not_expired(rig):
    token = rig.token()
    rig.revoke()
    refused(rig, 401, token=token)


def test_a_store_fault_while_checking_the_device_fails_closed(rig, monkeypatch):
    token = rig.token()

    def boom(**kw):
        raise RuntimeError("database down " + CANARY)

    monkeypatch.setattr(rig.devices, "is_active", boom)
    resp = refused(rig, 503, token=token)
    assert CANARY not in resp.text


def test_another_accounts_run_is_a_404_that_matches_a_missing_run(rig):
    missing = go(rig.grant(run_id=NO_SUCH_RUN))
    other = refused(rig, 404, "run_not_found", token=rig.token(OTHER_USER, OTHER_DEVICE))
    assert (other.status_code, other.json()) == (missing.status_code, missing.json())


def test_another_devices_run_is_a_404_that_matches_a_missing_run(rig):
    second = rig.add_device(str(uuid.uuid4()), USER)                          # the same account, a different device
    token = devices.issue_device_token(second)["jwt"]
    missing = go(rig.grant(run_id=NO_SUCH_RUN))
    wrong = refused(rig, 404, "run_not_found", token=token)
    assert (wrong.status_code, wrong.json()) == (missing.status_code, missing.json())


def test_a_malformed_run_id_is_the_same_404(rig):
    missing = go(rig.grant(run_id=NO_SUCH_RUN))
    for bad in ("x", "has space", "a" * 80, "ünïcode-run-id"):
        resp = go(rig.grant(run_id=bad))
        assert (resp.status_code, resp.json()) == (missing.status_code, missing.json())


@pytest.mark.parametrize("state", ["created", "running", "awaiting_approval", "paused_disconnected", "paused_error"])
def test_a_run_that_is_not_paused_for_budget_is_refused(monkeypatch, state):
    world = World(monkeypatch)
    world.run_at(state, RUN)
    rig = GrantRig(monkeypatch, world=world)
    try:
        resp = refused_other(rig, 409)
        assert detail(resp)["code"] == "not_paused_budget" and detail(resp)["state"] == state
    finally:
        world.teardown()


def test_an_ended_run_is_refused(monkeypatch):
    world = World(monkeypatch)
    world.run_at("paused_budget", RUN)
    run_manager.cancel_run(USER, RUN)
    rig = GrantRig(monkeypatch, world=world)
    try:
        resp = go(rig.grant())
        assert resp.status_code == 409 and detail(resp)["state"] == "cancelled"
    finally:
        world.teardown()


def refused_other(rig, status):
    """refused() for a run whose state is not paused_budget (the ceiling must still be untouched)."""
    before = rig.snapshot()
    resp = go(rig.grant())
    assert resp.status_code == status, resp.text
    assert rig.snapshot() == before and rig.budget.ceiling(RUN) is None
    return resp


def test_a_run_that_moves_between_the_check_and_the_claim_is_not_resumed_by_the_grant(monkeypatch):
    """The claim only moves a run out of paused_budget: a run another path put into paused_disconnected a moment after
    the route read it must stay there (paused_disconnected -> running is a legal edge; `expected` is what refuses it)."""
    import dataclasses
    world = World(monkeypatch)
    world.run_at("paused_disconnected", RUN)
    rig = GrantRig(monkeypatch, world=world)
    real_get, seen = runs.get_run, []

    def stale(user_id, run_id):
        run = real_get(user_id, run_id)
        if not seen:
            seen.append(run_id)
            return dataclasses.replace(run, state="paused_budget")       # what the route read a moment ago
        return run

    try:
        monkeypatch.setattr(runs, "get_run", stale)
        resp = go(rig.grant())
        monkeypatch.setattr(runs, "get_run", real_get)
        assert resp.status_code == 409 and detail(resp)["code"] == "not_paused_budget"
        assert detail(resp)["state"] == "paused_disconnected"
        assert runs.get_run(USER, RUN).state == "paused_disconnected"
        assert rig.budget.ceiling(RUN) is None and rig.hook_calls == []
    finally:
        world.teardown()


def test_an_offline_device_is_a_409_and_nothing_changes(rig):
    rig.registry.remove(rig.conn)
    resp = refused(rig, 409, "device_offline")
    assert "reset_at" not in detail(resp)


def test_a_connection_that_is_closing_counts_as_offline(rig):
    rig.conn.closed = True
    refused(rig, 409, "device_offline")


def test_an_exhausted_pool_is_a_409_with_the_reset_time_and_changes_nothing(rig):
    rig.ledger.external_used = rig.ledger.pool
    resp = refused(rig, 409, "quota_exhausted")
    assert detail(resp)["reset_at"] == RESET_AT


def test_a_pool_that_cannot_be_read_does_not_authorise_a_grant(rig):
    rig.ledger.degraded = True
    refused(rig, 503, "usage_unavailable")
    rig.ledger.degraded = False
    rig.ledger.check_raises = RuntimeError(CANARY)
    resp = refused(rig, 503, "usage_unavailable")
    assert CANARY not in resp.text


def test_a_run_whose_buffer_is_gone_is_refused_and_unchanged(rig):
    buffer.delete_buffer(RUN)
    refused(rig, 409, "buffer_gone")


def test_a_device_at_its_parallel_run_cap_is_refused_and_the_run_stays_paused(monkeypatch):
    world = World(monkeypatch)
    world.run_at("running", "run-other-000001")                               # takes the device's only slot
    world.run_at("paused_budget", RUN)
    rig = GrantRig(monkeypatch, world=world, max_parallel=1)
    try:
        resp = refused(rig, 429, "rate_limited")
        assert resp.status_code == 429
    finally:
        world.teardown()


# =============================================================================================
# Failures after the run was moved are undone
# =============================================================================================

def test_a_ceiling_that_cannot_be_written_puts_the_run_back(rig, monkeypatch):
    before = rig.snapshot()

    def boom(run_id, value):
        raise RuntimeError("redis down " + CANARY)

    monkeypatch.setattr(rig.budget, "set_ceiling", boom)
    resp = go(rig.grant())
    assert resp.status_code == 503 and detail(resp)["code"] == "grant_unavailable" and CANARY not in resp.text
    after = rig.snapshot()
    assert after["state"] == "paused_budget" and after["error_code"] == "quota_exhausted"
    assert (after["sent"], after["hooks"], after["bound"]) == (before["sent"], before["hooks"], before["bound"])


def test_an_announcement_that_fails_puts_the_run_and_the_ceiling_back(monkeypatch):
    world = World(monkeypatch)
    world.run_at("paused_budget", RUN)
    rig = GrantRig(monkeypatch, world=world)
    rig.budget.set_ceiling(RUN, 3.0)                                          # an earlier grant
    before = rig.snapshot()

    def boom(*a, **k):
        raise RuntimeError("emitter down " + CANARY)

    real_bind = rig.emitter.bind
    monkeypatch.setattr(rig.emitter, "bind", boom)
    try:
        resp = go(rig.grant())
        assert resp.status_code == 503 and CANARY not in resp.text
        after = rig.snapshot()
        assert after["state"] == "paused_budget" and after["error_code"] == "quota_exhausted"
        assert after["ceiling"] == 3.0                                        # restored, not left at the raised value
        assert (after["sent"], after["hooks"], after["bound"]) == (before["sent"], before["hooks"], before["bound"])
        monkeypatch.setattr(rig.emitter, "bind", real_bind)                   # the retry can simply be sent again
        retry = go(rig.grant())
        assert retry.status_code == 200 and retry.json()["run_credit_ceiling"] == 8.0
    finally:
        world.teardown()


def test_a_failing_hook_does_not_fail_the_grant(rig):
    async def broken(conn, run):
        raise RuntimeError(CANARY)

    rig.handlers.add_on_resume(broken)

    async def scenario():
        resp = await rig.grant()
        await asyncio.sleep(0.05)
        return resp

    assert go(scenario()).status_code == 200
    assert rig.hook_calls == [(RUN, "running")]                               # the other hook still ran


# =============================================================================================
# The whole thing: the real loop pauses at its ceiling, the device grants, the loop carries on
# =============================================================================================

def test_end_to_end_the_run_pauses_at_the_ceiling_and_continues_after_a_grant(monkeypatch):
    registry = connections.InMemoryRegistry()
    conn = CapturingConn()
    registry.add(conn)
    emitter = RunEmitter(registry=lambda: registry)
    h = Harness(emit=emitter.emit, flat_credits=1.0, policy=metering.RunCeilingPolicy(fixed=2.0, chunk_fixed=5.0), real_progress=True)
    loop_rig = Rig(monkeypatch, priced("one", [tool_call(1)]), priced("two", [tool_call(2)]), priced("all done"),
                   metering_factory=h.factory, emit=emitter.emit, emit_final=emitter.emit_final,
                   unbind=emitter.unbind)
    loop_rig.conn = conn
    emitter.bind(loop_rig.run_id, USER, loop_rig.run.device_id, last_seq=0)
    rig = GrantRig(monkeypatch, world=loop_rig.world, ledger=h.ledger, store=h.store, policy=h.policy,
                   emitter=emitter, registry=registry, conn=conn, run_id=loop_rig.run_id)
    rig.handlers.add_on_resume(loop_rig.loop.hooks["on_resume"])              # what loop.install() does
    try:
        async def scenario():
            first = await loop_rig.turn("please fix it")
            assert (first.outcome, first.reason) == ("paused_budget", "quota_exhausted")
            assert loop_rig.adapter.opened == 2 and not emitter.is_bound(loop_rig.run_id)
            refused_now = await rig.grant(token=None)                         # no token: nothing happens
            assert refused_now.status_code == 401
            resp = await rig.grant()
            await asyncio.sleep(0.05)
            task = loop_rig.loop._tasks.get(loop_rig.run_id)
            if task is not None:
                await asyncio.wait_for(task, 5)
            return resp

        resp = go(scenario())
        assert resp.status_code == 200 and resp.json()["run_credit_ceiling"] == 7.0
        assert loop_rig.adapter.opened == 3                                   # the interrupted turn went on
        assert h.ledger.total(loop_rig.run_id) == 3.0 and len(h.ledger.rows) == 3
        assert runs.get_run(USER, loop_rig.run_id).state == "running"

        states = [(e["payload"]["state"], e["payload"].get("reason")) for e in conn.sent
                  if e["payload"].get("type") == "state_changed"]
        assert states == [("paused_budget", "quota_exhausted"), ("running", "budget_granted")]
        assert [e["seq"] for e in conn.sent] == list(range(1, len(conn.sent) + 1))      # no gap, no repeat
        assert [c["code"] for c in conn.errors] == ["quota_exhausted"]
        order = [e["payload"].get("type", e["t"]) for e in conn.sent]
        assert order.index("notice") < order.index("state_changed")
        assert [p["run_credits_total"] for p in (e["payload"] for e in conn.sent
                                                 if e["payload"].get("type") == "usage")] == [1.0, 2.0, 3.0]
        assert protocol_problems(conn.sent) == []
    finally:
        rig.world.teardown()
