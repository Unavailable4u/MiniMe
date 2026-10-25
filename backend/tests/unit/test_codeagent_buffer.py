"""S06b: the ephemeral, encrypted run buffer (codeagent/buffer.py).

Everything runs against tests/unit/buffer_testkit.FakeRunRedis, an in-memory stand-in
behind the buffer's thin Redis interface, so Upstash is never needed. What is pinned:

  * fail closed: no secret, no buffer; a weak secret, a malformed rotation setting and a
    Redis fault are refusals with fixed text (never the secret, never a fault's text);
  * the content is unreadable without the key: nothing stored contains plaintext; a wrong
    key, a wrong run id, a swapped stream, a tampered or truncated record, a tampered
    creation stamp and an unknown key id are all refused (BufferCorrupt), never decoded;
  * a fresh nonce for every write; one key per run (HKDF); a key id on every record so the
    secret can be rotated;
  * a long conversation cannot exceed a request-size limit: the biggest request any test
    produces is a small fraction of the limit, whatever the item or conversation size;
  * TTLs: idle 2 hours refreshed by every read and write on EVERY key of the run, and a
    24 hour hard cap that holds even when the idle TTL is refreshed, and even when someone
    else extends the Redis TTL;
  * next_seq is atomic (threads), contiguous, and survives a process restart (it lives in
    Redis); a resumed buffer continues from the device's last_seq;
  * replay events come back verbatim, in seq order, only after the given seq, and are
    deleted with the buffer; a half-written item (a request failed part way) is never
    returned; a retried write that duplicated a record is returned once;
  * after delete_buffer nothing is left in Redis.
"""
import ast
import json
import pathlib
import threading

import pytest

from codeagent import buffer
from tests.unit.buffer_testkit import (
    CANARY, OTHER_SECRET, RUN, RUN_B, SECRET, Clock, FakeRunRedis,
)

REPO = pathlib.Path(__file__).resolve().parents[3]
PLAINTEXT_CANARY = "PLAINTEXT-canary-prompt-do-not-store-1c9d"


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    redis = FakeRunRedis(clock)
    monkeypatch.setattr(buffer, "_REDIS", redis)
    monkeypatch.setattr(buffer, "_now", clock.time)
    monkeypatch.setenv("RUN_BUFFER_SECRET", SECRET)
    monkeypatch.delenv("RUN_BUFFER_KEY_ID", raising=False)
    monkeypatch.delenv("RUN_BUFFER_PREVIOUS_SECRETS", raising=False)
    return type("Env", (), {"clock": clock, "redis": redis, "mp": monkeypatch})


STATE = {"profile": "code", "mode": "ask", "cwd_label": "proj", "instructions": "be helpful",
         "rules": [], "memory_index": "", "tool_catalog_version": "v0", "model_pref": "auto"}


def new(env, run=RUN, state=None, history=(), last_seq=0):
    buffer.create_buffer(run, state=dict(state or STATE), history=list(history), last_seq=last_seq)


def evt(seq, text="hi", run=RUN, t="run_event"):
    return {"v": 1, "t": t, "id": f"00000000-0000-4000-8000-{seq:012d}", "run_id": run, "seq": seq,
            "ts": "2026-11-02T09:00:00.250Z", "payload": {"type": "text_delta", "text": text}}


def remaining_hard(env, created):
    return created + buffer.HARD_CAP_SECONDS - env.clock.t


# ---------------------------------------------------------------------------
# Configuration: fail closed
# ---------------------------------------------------------------------------

def test_without_a_secret_no_buffer_is_created_and_redis_is_never_touched(env):
    env.mp.delenv("RUN_BUFFER_SECRET")
    with pytest.raises(buffer.BufferNotConfigured):
        new(env)
    assert env.redis.requests == [] and env.redis.keys() == []


def test_without_a_secret_nothing_can_be_read_or_written_but_deletion_still_works(env):
    new(env, history=[{"role": "user", "text": "hello"}])
    buffer.append_replay(RUN, evt(1))
    env.mp.delenv("RUN_BUFFER_SECRET")
    for call in (lambda: buffer.read_conversation(RUN), lambda: buffer.append_conversation(RUN, {"role": "user", "text": "x"}),
                 lambda: buffer.next_seq(RUN), lambda: buffer.replay_after(RUN, 0),
                 lambda: buffer.append_replay(RUN, evt(2)), lambda: buffer.touch(RUN)):
        with pytest.raises(buffer.BufferNotConfigured):
            call()
    assert buffer.delete_buffer(RUN) is True                       # no key needed to delete
    assert env.redis.keys() == []


@pytest.mark.parametrize("secret", ["", "short", "x" * 31, " " * 40, None])
def test_a_missing_empty_or_weak_secret_is_refused(env, secret):
    if secret is None:
        env.mp.delenv("RUN_BUFFER_SECRET")
    else:
        env.mp.setenv("RUN_BUFFER_SECRET", secret)
    with pytest.raises(buffer.BufferNotConfigured) as info:
        new(env)
    assert "short" not in str(info.value) or secret != "short"      # never echoes the value
    assert env.redis.requests == []


@pytest.mark.parametrize("kid", ["", "has space", "k" * 17, "a.b", "a/b", "é"])
def test_a_malformed_key_id_is_refused(env, kid):
    env.mp.setenv("RUN_BUFFER_KEY_ID", kid)
    with pytest.raises(buffer.BufferNotConfigured):
        new(env)


@pytest.mark.parametrize("previous", ["not json", "[]", '{"k0": 5}', '{"k0": "short"}', '{"bad id": "%s"}' % SECRET,
                                      '{"k1": "%s"}' % OTHER_SECRET])      # k1 is the current id
def test_a_malformed_rotation_setting_is_refused_and_never_echoed(env, previous):
    env.mp.setenv("RUN_BUFFER_PREVIOUS_SECRETS", previous)
    with pytest.raises(buffer.BufferNotConfigured) as info:
        new(env)
    assert OTHER_SECRET not in str(info.value) and SECRET not in str(info.value)


def test_the_error_text_never_contains_the_secret(env):
    env.mp.setenv("RUN_BUFFER_SECRET", "y" * 10)
    with pytest.raises(buffer.BufferNotConfigured) as info:
        new(env)
    assert "y" * 10 not in str(info.value) and "y" * 10 not in repr(info.value)


def test_configurable_constants_and_their_environment_overrides(env, monkeypatch):
    assert buffer.IDLE_TTL_SECONDS == 2 * 3600 and buffer.HARD_CAP_SECONDS == 24 * 3600
    monkeypatch.setenv("X_SECONDS", "90")
    assert buffer._env_seconds("X_SECONDS", 5) == 90
    for bad in ("", "0", "-4", "abc", "1.5", "99999999999"):
        monkeypatch.setenv("X_SECONDS", bad)
        assert buffer._env_seconds("X_SECONDS", 5) == 5
    monkeypatch.delenv("X_SECONDS")
    assert buffer._env_seconds("X_SECONDS", 5) == 5


# ---------------------------------------------------------------------------
# Round trips and keys
# ---------------------------------------------------------------------------

def test_the_conversation_round_trips_state_history_and_appended_items(env):
    history = [{"role": "user", "text": "first"}, {"role": "assistant", "text": "second"},
               {"role": "tool", "call_id": "c1", "ok": True, "output": "ünïcödé ✓ 日本語"}]
    new(env, history=history)
    buffer.append_conversation(RUN, {"role": "user", "text": "third"})
    buffer.append_conversation(RUN, {"role": "assistant", "text": "fourth"}, {"role": "user", "text": "fifth"})
    conv = buffer.read_conversation(RUN)
    assert conv.state == STATE
    assert conv.items == history + [{"role": "user", "text": "third"}, {"role": "assistant", "text": "fourth"},
                                    {"role": "user", "text": "fifth"}]


def test_the_keys_are_three_per_run_under_one_prefix_and_carry_only_the_run_id(env):
    new(env)
    assert env.redis.keys() == sorted(buffer.buffer_keys(RUN))
    assert len(buffer.buffer_keys(RUN)) == 3
    for key in buffer.buffer_keys(RUN):
        assert key.startswith("run_buffer:") and RUN in key
        assert not key.startswith("run_guard:")                      # run_guard's namespace is not shared


@pytest.mark.parametrize("bad", ["", "short", "x" * 65, "a:b:c:d:e:f:g:h", "has space abc", "../etc/passwd", "run-abcdefgh\n",
                                 None, 12345678, b"run-abcdefgh"])
def test_a_malformed_run_id_is_refused_before_redis_is_touched(env, bad):
    for call in (lambda: buffer.create_buffer(bad, state=STATE), lambda: buffer.read_conversation(bad),
                 lambda: buffer.next_seq(bad), lambda: buffer.delete_buffer(bad), lambda: buffer.exists(bad),
                 lambda: buffer.append_replay(bad, evt(1)), lambda: buffer.replay_after(bad, 0)):
        with pytest.raises(buffer.InvalidBufferInput):
            call()
    assert env.redis.requests == []


def test_the_run_id_pattern_equals_the_protocols_and_run_pys(env):
    common = REPO / "protocol" / "v1" / "common.json"
    if not common.exists():
        pytest.skip("protocol/ is not next to backend/ in this layout")
    assert buffer._RUN_ID_RE.pattern == json.loads(common.read_text())["$defs"]["run_id"]["pattern"]


def test_two_runs_are_independent(env):
    new(env, RUN, history=[{"role": "user", "text": "a"}])
    new(env, RUN_B, history=[{"role": "user", "text": "b"}])
    buffer.append_replay(RUN, evt(1, run=RUN))
    buffer.append_replay(RUN_B, evt(1, "other", run=RUN_B))
    assert buffer.read_conversation(RUN).items == [{"role": "user", "text": "a"}]
    assert buffer.delete_buffer(RUN) is True
    assert buffer.read_conversation(RUN_B).items == [{"role": "user", "text": "b"}]
    assert buffer.replay_after(RUN_B, 0)[0]["payload"]["text"] == "other"
    assert all(RUN_B in k for k in env.redis.keys())


# ---------------------------------------------------------------------------
# The content is unreadable without the key
# ---------------------------------------------------------------------------

def test_nothing_stored_in_redis_contains_the_plaintext(env):
    state = dict(STATE, instructions=PLAINTEXT_CANARY, cwd_label="proj")
    new(env, state=state, history=[{"role": "user", "text": PLAINTEXT_CANARY}])
    buffer.append_conversation(RUN, {"role": "tool", "call_id": "c1", "ok": True, "output": PLAINTEXT_CANARY})
    buffer.append_replay(RUN, evt(buffer.next_seq(RUN), PLAINTEXT_CANARY))
    stored = env.redis.all_strings()
    assert stored
    for s in stored:
        assert PLAINTEXT_CANARY not in s and "PLAINTEXT" not in s
        if s.count(".") == 2:                                   # a wire string: also look inside it
            raw = buffer._b64d(s.split(".", 2)[2])
            assert PLAINTEXT_CANARY.encode() not in raw and b"canary" not in raw
    # and the request log never carried it in the clear either
    assert all(PLAINTEXT_CANARY not in str(r) for r in env.redis.requests)


def test_a_wrong_key_cannot_read_anything(env):
    new(env, state=dict(STATE, instructions=PLAINTEXT_CANARY), history=[{"role": "user", "text": "x"}])
    buffer.append_replay(RUN, evt(1, PLAINTEXT_CANARY))
    env.mp.setenv("RUN_BUFFER_SECRET", OTHER_SECRET)
    for call in (lambda: buffer.read_conversation(RUN), lambda: buffer.replay_after(RUN, 0),
                 lambda: buffer.next_seq(RUN), lambda: buffer.touch(RUN), lambda: buffer.exists(RUN),
                 lambda: buffer.append_replay(RUN, evt(2)), lambda: buffer.append_conversation(RUN, {"role": "user", "text": "y"})):
        with pytest.raises(buffer.BufferCorrupt) as info:
            call()
        assert SECRET not in str(info.value) and OTHER_SECRET not in str(info.value)
        assert PLAINTEXT_CANARY not in str(info.value)


def test_a_wrong_run_id_cannot_read_another_runs_data(env):
    new(env, RUN, state=dict(STATE, instructions=PLAINTEXT_CANARY))
    buffer.append_replay(RUN, evt(1, PLAINTEXT_CANARY))
    # an attacker (or a bug) copies run A's stored data under run B's keys
    a, b = buffer.buffer_keys(RUN), buffer.buffer_keys(RUN_B)
    for src, dst in zip(a, b):
        kind = env.redis.store[src][0]
        if kind == "hash":
            env.redis.store[dst] = ("hash", dict(env.redis.store[src][1]))
        else:
            env.redis.store[dst] = ("list", list(env.redis.store[src][1]))
        env.redis.expires[dst] = env.clock.t + 3600
    for call in (lambda: buffer.read_conversation(RUN_B), lambda: buffer.replay_after(RUN_B, 0),
                 lambda: buffer.next_seq(RUN_B), lambda: buffer.exists(RUN_B)):
        with pytest.raises(buffer.BufferCorrupt):
            call()
    assert buffer.read_conversation(RUN).state["instructions"] == PLAINTEXT_CANARY     # A is fine


def test_a_record_moved_between_streams_is_refused(env):
    new(env, history=[{"role": "user", "text": "x"}])
    buffer.append_replay(RUN, evt(1))
    conv_key, replay_key, _ = buffer.buffer_keys(RUN)
    env.redis.set_list(replay_key, env.redis.get_list(conv_key), ttl=3600)       # conv records posing as replay
    with pytest.raises(buffer.BufferCorrupt):
        buffer.replay_after(RUN, 0)


def _flip(wire):
    head, body = wire.rsplit(".", 1)
    i = len(body) // 2
    return head + "." + body[:i] + ("A" if body[i] != "A" else "B") + body[i + 1:]


@pytest.mark.parametrize("target", ["conv", "replay", "meta"])
def test_a_tampered_ciphertext_is_refused(env, target):
    new(env, history=[{"role": "user", "text": "x"}])
    buffer.append_replay(RUN, evt(1))
    conv_key, replay_key, stats_key = buffer.buffer_keys(RUN)
    if target == "meta":
        env.redis.set_hash_field(stats_key, "meta", _flip(env.redis.get_hash(stats_key)["meta"]))
    else:
        key = conv_key if target == "conv" else replay_key
        items = env.redis.get_list(key)
        items[-1] = _flip(items[-1])
        env.redis.set_list(key, items, ttl=3600)
    reader = {"conv": lambda: buffer.read_conversation(RUN), "replay": lambda: buffer.replay_after(RUN, 0),
              "meta": lambda: buffer.next_seq(RUN)}[target]
    with pytest.raises(buffer.BufferCorrupt):
        reader()


@pytest.mark.parametrize("mangle", [
    lambda w: w[:-8],                                  # truncated
    lambda w: w + "AAAA",                              # extended
    lambda w: "2." + w.split(".", 1)[1],               # unknown format version
    lambda w: w.split(".")[0] + ".zz." + w.split(".", 2)[2],     # unknown key id
    lambda w: "garbage",
    lambda w: "",
    lambda w: w.replace(".", ":"),
    lambda w: w.rsplit(".", 1)[0] + ".!!!!",           # not base64url
    lambda w: w.rsplit(".", 1)[0] + "." + "A" * 8,     # shorter than a nonce plus a tag
])
def test_malformed_records_are_refused_not_decoded(env, mangle):
    new(env, history=[{"role": "user", "text": "x"}])
    conv_key = buffer.buffer_keys(RUN)[0]
    items = env.redis.get_list(conv_key)
    items[-1] = mangle(items[-1])
    env.redis.set_list(conv_key, items, ttl=3600)
    with pytest.raises(buffer.BufferCorrupt):
        buffer.read_conversation(RUN)


def test_a_tampered_creation_stamp_cannot_extend_the_hard_cap(env):
    new(env)
    stats = buffer.buffer_keys(RUN)[2]
    forged = buffer._seal(RUN, "meta", json.dumps({"created": int((env.clock.t + 10 * 86400) * 1000)}).encode())
    # forged with the right key it would verify -- that needs the secret; with any other key it must not
    env.mp.setenv("RUN_BUFFER_SECRET", OTHER_SECRET)
    forged_other = buffer._seal(RUN, "meta", json.dumps({"created": int((env.clock.t + 10 * 86400) * 1000)}).encode())
    env.mp.setenv("RUN_BUFFER_SECRET", SECRET)
    env.redis.set_hash_field(stats, "meta", forged_other)
    with pytest.raises(buffer.BufferCorrupt):
        buffer.touch(RUN)
    assert forged != forged_other


def test_a_fresh_random_nonce_is_used_for_every_write(env):
    new(env)
    for _ in range(5):
        buffer.append_conversation(RUN, {"role": "user", "text": "identical"})
    records = env.redis.get_list(buffer.buffer_keys(RUN)[0])[-5:]
    assert len(set(records)) == 5                                    # same plaintext, five different ciphertexts
    nonces = {buffer._b64d(r.split(".", 2)[2])[:12] for r in records}
    assert len(nonces) == 5


def test_each_run_has_its_own_key_derived_with_hkdf(env):
    k_a, k_b = buffer._derive_key(SECRET, RUN), buffer._derive_key(SECRET, RUN_B)
    assert len(k_a) == 32 and k_a != k_b and k_a == buffer._derive_key(SECRET, RUN)
    assert buffer._derive_key(OTHER_SECRET, RUN) != k_a
    assert SECRET.encode() not in k_a


def test_the_secret_can_be_rotated_with_a_key_id_on_every_record(env):
    new(env, history=[{"role": "user", "text": "written under k1"}])
    first = env.redis.get_list(buffer.buffer_keys(RUN)[0])[-1]
    assert first.split(".")[1] == "k1"
    # rotate: new current secret with id k2, the old one kept for reading
    env.mp.setenv("RUN_BUFFER_SECRET", OTHER_SECRET)
    env.mp.setenv("RUN_BUFFER_KEY_ID", "k2")
    env.mp.setenv("RUN_BUFFER_PREVIOUS_SECRETS", json.dumps({"k1": SECRET}))
    assert buffer.read_conversation(RUN).items == [{"role": "user", "text": "written under k1"}]
    buffer.append_conversation(RUN, {"role": "user", "text": "written under k2"})
    last = env.redis.get_list(buffer.buffer_keys(RUN)[0])[-1]
    assert last.split(".")[1] == "k2"
    assert [i["text"] for i in buffer.read_conversation(RUN).items] == ["written under k1", "written under k2"]
    # retire k1: its records can no longer be read
    env.mp.delenv("RUN_BUFFER_PREVIOUS_SECRETS")
    with pytest.raises(buffer.BufferCorrupt):
        buffer.read_conversation(RUN)


# ---------------------------------------------------------------------------
# Size: chunking and the request limit
# ---------------------------------------------------------------------------

LIMIT = 512 * 1024      # a conservative ceiling: Upstash documents 10 MB (an older page said 1 MB)


def test_a_huge_item_is_chunked_and_no_request_comes_near_the_limit(env):
    big = "x" * (3 * 1024 * 1024)
    new(env)
    buffer.append_conversation(RUN, {"role": "tool", "call_id": "c1", "ok": True, "output": big})
    assert buffer.read_conversation(RUN).items[-1]["output"] == big
    assert env.redis.max_request_bytes() < LIMIT
    assert env.redis.max_request_bytes() <= buffer.REQUEST_GUARD_BYTES
    assert len(env.redis.requests) > 10                              # it really was split up


def test_a_long_conversation_of_many_items_never_exceeds_the_limit(env):
    new(env)
    for i in range(300):
        buffer.append_conversation(RUN, {"role": "tool", "call_id": f"c{i}", "ok": True, "output": f"{i}:" + "y" * 20_000})
    conv = buffer.read_conversation(RUN)
    assert len(conv.items) == 300 and conv.items[299]["output"].startswith("299:")
    assert env.redis.max_request_bytes() < LIMIT
    assert env.redis.max_reply_bytes() < 6 * 1024 * 1024             # a read page stays small too


def test_a_big_batch_is_split_into_several_requests(env):
    new(env)
    before = len(env.redis.requests)
    buffer.append_conversation(RUN, *[{"role": "user", "text": "z" * 30_000} for _ in range(40)])
    assert len(env.redis.requests) - before >= 4
    assert env.redis.max_request_bytes() < LIMIT
    assert len(buffer.read_conversation(RUN).items) == 40


def test_the_chunk_and_request_budgets_leave_a_wide_margin_under_the_limits(env):
    assert buffer.CHUNK_BYTES <= 64 * 1024
    assert buffer.TX_BUDGET_BYTES <= 256 * 1024
    assert buffer.REQUEST_GUARD_BYTES <= LIMIT
    assert buffer.MAX_STREAM_BYTES * 2 < 100 * 1024 * 1024          # a list key stays far under the 100 MB record limit


@pytest.mark.parametrize("bad", ["not a dict", 5, None, [1], {"a": {1, 2}}, {"a": float("nan")}, {"a": object()}])
def test_an_item_that_is_not_a_json_object_is_refused_and_nothing_is_written(env, bad):
    new(env)
    before = len(env.redis.requests)
    with pytest.raises(buffer.InvalidBufferInput):
        buffer.append_conversation(RUN, bad)
    assert len(env.redis.requests) == before


def test_an_item_over_the_item_limit_is_refused_and_nothing_is_written(env):
    new(env)
    before = len(env.redis.requests)
    with pytest.raises(buffer.InvalidBufferInput):
        buffer.append_conversation(RUN, {"role": "user", "text": "x" * (buffer.MAX_ITEM_BYTES + 1)})
    assert len(env.redis.requests) == before
    with pytest.raises(buffer.InvalidBufferInput):
        buffer.append_conversation(RUN)                              # nothing to append


def test_a_stream_that_would_pass_its_cap_is_refused_whole(env):
    env.mp.setattr(buffer, "MAX_STREAM_BYTES", 120_000)
    new(env)
    for i in range(3):
        buffer.append_conversation(RUN, {"role": "user", "text": "a" * 30_000})
    with pytest.raises(buffer.BufferFull):
        buffer.append_conversation(RUN, {"role": "user", "text": "b" * 30_000}, {"role": "user", "text": "c" * 30_000})
    items = buffer.read_conversation(RUN).items
    assert len(items) == 3 and all(i["text"].startswith("a") for i in items)     # the refused batch left no trace
    buffer.append_conversation(RUN, {"role": "user", "text": "ok"})                 # small ones still fit
    with pytest.raises(buffer.BufferFull):
        buffer.append_replay(RUN, evt(1, "q" * 130_000))
    assert buffer.replay_after(RUN, 0) == []                                       # replay has its own counter


def test_a_request_that_fails_part_way_leaves_no_half_item_and_later_writes_work(env):
    new(env, history=[{"role": "user", "text": "before"}])
    calls = {"n": 0}

    def second_push_fails(commands):
        if commands[0][0] == "RPUSHX":
            calls["n"] += 1
            return calls["n"] == 2
        return False
    env.redis.fail_if(second_push_fails)
    big = {"role": "tool", "call_id": "c1", "ok": True, "output": "p" * 300_000}      # needs several requests
    with pytest.raises(buffer.BufferUnavailable):
        buffer.append_conversation(RUN, big)
    env.redis.fail_if(None)
    assert [i.get("text") for i in buffer.read_conversation(RUN).items] == ["before"]     # the partial item is invisible
    buffer.append_conversation(RUN, {"role": "user", "text": "after"})
    assert [i.get("text") for i in buffer.read_conversation(RUN).items] == ["before", "after"]


def test_a_retried_write_that_duplicated_records_is_read_once(env):
    # upstash_redis retries a request once when a reply is lost, so a push can land twice
    new(env, history=[{"role": "user", "text": "one"}])
    buffer.append_conversation(RUN, {"role": "user", "text": "two"})
    key = buffer.buffer_keys(RUN)[0]
    records = env.redis.get_list(key)
    env.redis.set_list(key, records + records[-1:], ttl=3600)                 # the last item's record, twice
    assert [i.get("text") for i in buffer.read_conversation(RUN).items] == ["one", "two"]
    big = {"role": "user", "text": "m" * 150_000}                              # a multi-record item, duplicated whole
    buffer.append_conversation(RUN, big)
    records = env.redis.get_list(key)
    n = len([r for r in records]) - len(records[:-3])
    env.redis.set_list(key, records + records[-3:], ttl=3600)
    texts = [len(i.get("text", "")) for i in buffer.read_conversation(RUN).items]
    assert texts == [3, 3, 150_000]


# ---------------------------------------------------------------------------
# TTLs: idle, hard cap
# ---------------------------------------------------------------------------

def test_every_key_gets_the_idle_ttl_at_creation(env):
    new(env, history=[{"role": "user", "text": "x" * 200_000}])         # several creation requests
    for key in buffer.buffer_keys(RUN):
        assert env.redis.ttl(key) == buffer.IDLE_TTL_SECONDS


@pytest.mark.parametrize("op", [
    lambda: buffer.append_conversation(RUN, {"role": "user", "text": "w"}),
    lambda: buffer.read_conversation(RUN),
    lambda: buffer.next_seq(RUN),
    lambda: buffer.append_replay(RUN, evt(1)),
    lambda: buffer.replay_after(RUN, 0),
    lambda: buffer.touch(RUN),
])
def test_every_read_and_write_refreshes_every_key_of_the_run(env, op):
    new(env)
    env.clock.advance(3600)
    for key in buffer.buffer_keys(RUN):
        assert env.redis.ttl(key) == buffer.IDLE_TTL_SECONDS - 3600
    op()
    for key in buffer.buffer_keys(RUN):
        assert env.redis.ttl(key) == buffer.IDLE_TTL_SECONDS


def test_an_idle_buffer_disappears_from_redis_after_two_hours_and_reports_missing(env):
    new(env, history=[{"role": "user", "text": PLAINTEXT_CANARY}])
    env.clock.advance(buffer.IDLE_TTL_SECONDS + 1)
    assert env.redis.keys() == []
    assert buffer.exists(RUN) is False
    for call in (lambda: buffer.read_conversation(RUN), lambda: buffer.next_seq(RUN), lambda: buffer.touch(RUN),
                 lambda: buffer.append_replay(RUN, evt(1)), lambda: buffer.replay_after(RUN, 0),
                 lambda: buffer.append_conversation(RUN, {"role": "user", "text": "x"})):
        with pytest.raises(buffer.BufferMissing):
            call()
    assert env.redis.keys() == []                                  # a refused write left nothing behind


def test_just_inside_the_idle_window_the_buffer_is_alive(env):
    new(env)
    env.clock.advance(buffer.IDLE_TTL_SECONDS - 5)
    assert buffer.exists(RUN) is True
    buffer.touch(RUN)


def test_the_hard_cap_holds_even_though_every_hour_refreshes_the_idle_ttl(env):
    new(env)
    created = env.clock.t
    for hour in range(1, 24):
        env.clock.advance(3600)
        buffer.touch(RUN)                                          # the idle TTL is refreshed every hour
        for key in buffer.buffer_keys(RUN):
            ttl = env.redis.ttl(key)
            assert 0 < ttl <= buffer.IDLE_TTL_SECONDS
            assert ttl <= remaining_hard(env, created) + 1         # never beyond the hard cap
    assert env.redis.ttl(buffer.buffer_keys(RUN)[0]) == 3600       # hour 23: only an hour of life left
    env.clock.advance(3600 - 1)
    buffer.touch(RUN)                                              # one second before the cap: still alive
    env.clock.advance(2)
    with pytest.raises(buffer.BufferExpired):
        buffer.touch(RUN)
    assert env.redis.keys() == []                                  # and the cap deleted the content


def test_the_hard_cap_holds_even_if_someone_extends_the_redis_ttl_directly(env):
    new(env, history=[{"role": "user", "text": PLAINTEXT_CANARY}])
    env.clock.advance(buffer.HARD_CAP_SECONDS + 10)
    for key in buffer.buffer_keys(RUN):
        env.redis.expires[key] = env.clock.t + 10 ** 6             # an operator (or a bug) refreshed the TTLs
    assert env.redis.keys() != []
    for call in (lambda: buffer.read_conversation(RUN), lambda: buffer.next_seq(RUN),
                 lambda: buffer.append_replay(RUN, evt(1)), lambda: buffer.replay_after(RUN, 0)):
        new_env_keys = env.redis.keys()
        if not new_env_keys:                                       # the first refusal already purged it
            with pytest.raises(buffer.BufferMissing):
                call()
            continue
        with pytest.raises(buffer.BufferExpired):
            call()
    assert env.redis.keys() == []


def test_exists_applies_the_hard_cap_too_and_does_not_refresh_the_ttl(env):
    new(env)
    env.clock.advance(600)
    assert buffer.exists(RUN) is True
    assert env.redis.ttl(buffer.buffer_keys(RUN)[0]) == buffer.IDLE_TTL_SECONDS - 600     # a status check is not use
    env.clock.advance(buffer.HARD_CAP_SECONDS)
    for key in buffer.buffer_keys(RUN):
        env.redis.expires[key] = env.clock.t + 10 ** 6
    assert buffer.exists(RUN) is False
    assert env.redis.keys() == []


def test_the_ttls_follow_the_configurable_constants(env):
    env.mp.setattr(buffer, "IDLE_TTL_SECONDS", 60)
    env.mp.setattr(buffer, "HARD_CAP_SECONDS", 150)
    new(env)
    assert env.redis.ttl(buffer.buffer_keys(RUN)[1]) == 60
    env.clock.advance(50)
    buffer.touch(RUN)
    assert env.redis.ttl(buffer.buffer_keys(RUN)[1]) == 60
    env.clock.advance(50)
    buffer.touch(RUN)
    assert env.redis.ttl(buffer.buffer_keys(RUN)[1]) == 50          # 150 - 100 left of the hard cap
    env.clock.advance(51)
    with pytest.raises(buffer.BufferExpired):
        buffer.touch(RUN)


# ---------------------------------------------------------------------------
# next_seq
# ---------------------------------------------------------------------------

def test_next_seq_starts_at_one_and_goes_up_by_exactly_one(env):
    new(env)
    assert [buffer.next_seq(RUN) for _ in range(5)] == [1, 2, 3, 4, 5]


def test_next_seq_is_atomic_across_threads(env):
    new(env)
    got, lock = [], threading.Lock()

    def worker():
        for _ in range(25):
            n = buffer.next_seq(RUN)
            with lock:
                got.append(n)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(got) == list(range(1, 201))


def test_the_counter_lives_in_redis_so_it_survives_a_restart(env):
    new(env)
    assert [buffer.next_seq(RUN) for _ in range(3)] == [1, 2, 3]
    import importlib
    importlib.reload(buffer)                                        # "the process restarted": no in-memory state
    env.mp.setattr(buffer, "_REDIS", env.redis)
    env.mp.setattr(buffer, "_now", env.clock.time)
    assert buffer.next_seq(RUN) == 4


def test_a_rebuilt_buffer_continues_from_the_devices_last_seq(env):
    new(env, last_seq=41)
    assert buffer.next_seq(RUN) == 42
    assert buffer.next_seq(RUN) == 43


@pytest.mark.parametrize("bad", [-1, 1.5, "3", True, None])
def test_a_bad_last_seq_is_refused(env, bad):
    with pytest.raises(buffer.InvalidBufferInput):
        buffer.create_buffer(RUN, state=STATE, last_seq=bad)
    new(env)
    with pytest.raises(buffer.InvalidBufferInput):
        buffer.replay_after(RUN, bad)


def test_next_seq_on_a_missing_buffer_is_refused_and_creates_nothing(env):
    with pytest.raises(buffer.BufferMissing):
        buffer.next_seq(RUN)
    assert env.redis.keys() == []


def test_a_buffer_deleted_between_the_check_and_the_increment_leaves_nothing_behind(env):
    new(env)

    def delete_before_increment(commands):
        if commands[0][0] == "HINCRBY":
            with env.redis.lock:
                env.redis.store.clear()
                env.redis.expires.clear()
    env.redis.before_request(delete_before_increment)
    with pytest.raises(buffer.BufferMissing):
        buffer.next_seq(RUN)
    env.redis.before_request(None)
    assert env.redis.keys() == []                                   # the resurrected counter was removed again


# ---------------------------------------------------------------------------
# Replay events
# ---------------------------------------------------------------------------

def test_replay_returns_only_events_after_the_given_seq_in_seq_order_verbatim(env):
    new(env)
    sent = []
    for i in range(1, 9):
        seq = buffer.next_seq(RUN)
        message = evt(seq, f"delta {i} ünï ✓")
        sent.append(message)
        buffer.append_replay(RUN, message)
    assert buffer.replay_after(RUN, 0) == sent
    assert buffer.replay_after(RUN, 3) == sent[3:]
    assert buffer.replay_after(RUN, 7) == sent[7:]
    assert buffer.replay_after(RUN, 8) == [] and buffer.replay_after(RUN, 50) == []
    for got, want in zip(buffer.replay_after(RUN, 0), sent):
        assert (got["id"], got["seq"], got["ts"], got["payload"]) == (want["id"], want["seq"], want["ts"], want["payload"])


def test_replay_is_in_seq_order_even_if_it_was_stored_out_of_order_and_duplicates_collapse(env):
    new(env)
    for seq in (3, 1, 2, 2, 5, 4):
        buffer.append_replay(RUN, evt(seq, f"d{seq}"))
    got = buffer.replay_after(RUN, 0)
    assert [m["seq"] for m in got] == [1, 2, 3, 4, 5]
    assert [m["payload"]["text"] for m in got] == ["d1", "d2", "d3", "d4", "d5"]


def test_append_replay_many_stores_a_batch_in_one_go(env):
    new(env)
    before = len(env.redis.requests)
    buffer.append_replay_many(RUN, [evt(i) for i in range(1, 11)])
    assert len(env.redis.requests) - before <= 3                    # one check, one write
    assert [m["seq"] for m in buffer.replay_after(RUN, 0)] == list(range(1, 11))


def test_a_large_replay_event_round_trips_under_the_request_limit(env):
    new(env)
    text = "é" * 400_000                                             # about 800 KB of UTF-8, like a big tool output
    buffer.append_replay(RUN, evt(1, text, t="tool_proposed"))
    got = buffer.replay_after(RUN, 0)
    assert got[0]["payload"]["text"] == text and got[0]["t"] == "tool_proposed"
    assert env.redis.max_request_bytes() < LIMIT


@pytest.mark.parametrize("message", [
    "text", None, [], {}, {"t": "run_event"},                                     # not a stream message
    {"v": 1, "t": "error", "id": "x", "run_id": RUN, "seq": 1, "payload": {}},    # not a replayable type
    {"v": 1, "t": "run_event", "id": "x", "run_id": RUN_B, "seq": 1, "payload": {}},    # another run's
    {"v": 1, "t": "run_event", "id": "x", "run_id": RUN, "payload": {}},                # no seq
    {"v": 1, "t": "run_event", "id": "x", "run_id": RUN, "seq": 0, "payload": {}},
    {"v": 1, "t": "run_event", "id": "x", "run_id": RUN, "seq": True, "payload": {}},
    {"v": 1, "t": "run_event", "id": "x", "run_id": RUN, "seq": "1", "payload": {}},
    {"v": 1, "t": "run_event", "run_id": RUN, "seq": 1, "payload": {}},                 # no id
])
def test_only_stream_messages_for_this_run_can_be_stored_for_replay(env, message):
    new(env)
    before = len(env.redis.requests)
    with pytest.raises(buffer.InvalidBufferInput):
        buffer.append_replay(RUN, message)
    assert len(env.redis.requests) == before
    assert buffer.replay_after(RUN, 0) == []


@pytest.mark.parametrize("t", ["run_event", "tool_proposed", "budget_warning"])
def test_the_three_stream_types_are_replayable(env, t):
    new(env)
    buffer.append_replay(RUN, evt(1, t=t))
    assert buffer.replay_after(RUN, 0)[0]["t"] == t


# ---------------------------------------------------------------------------
# Lifecycle: create, delete, nothing left
# ---------------------------------------------------------------------------

def test_creating_a_buffer_twice_is_refused_and_does_not_disturb_or_extend_the_first(env):
    new(env, history=[{"role": "user", "text": "original"}])
    created = env.clock.t
    env.clock.advance(3600)
    with pytest.raises(buffer.BufferExists):
        new(env, history=[{"role": "user", "text": "intruder"}])
    assert [i["text"] for i in buffer.read_conversation(RUN).items] == ["original"]
    for _ in range(22):                                                     # the run stays in use (idle TTL is 2 h) ...
        env.clock.advance(3600)
        buffer.touch(RUN)
    env.clock.advance(3600 - 100)                                           # ... until 100 s before the hard cap
    buffer.touch(RUN)
    with pytest.raises(buffer.BufferExists):
        new(env)
    for key in buffer.buffer_keys(RUN):
        assert env.redis.ttl(key) <= remaining_hard(env, created) + 1       # a second create cannot lengthen the life


def test_a_creation_that_fails_part_way_cleans_up_after_itself(env):
    calls = {"n": 0}

    def second_request_fails(commands):
        calls["n"] += 1
        return calls["n"] == 3
    env.redis.fail_if(second_request_fails)
    with pytest.raises(buffer.BufferUnavailable):
        new(env, history=[{"role": "user", "text": "h" * 300_000}])
    env.redis.fail_if(None)
    assert env.redis.keys() == []
    new(env)                                                          # and the run id is usable again
    assert buffer.exists(RUN)


def test_a_creation_that_dies_before_it_can_clean_up_still_leaves_only_keys_with_a_ttl(env):
    calls = {"n": 0}

    def everything_after_the_second_request_fails(commands):
        calls["n"] += 1
        return calls["n"] >= 3                                        # even the cleanup request fails
    env.redis.fail_if(everything_after_the_second_request_fails)
    with pytest.raises(buffer.BufferUnavailable):
        new(env, history=[{"role": "user", "text": "h" * 300_000}])
    env.redis.fail_if(None)
    for key in env.redis.keys():
        assert 0 < env.redis.ttl(key) <= buffer.IDLE_TTL_SECONDS        # nothing is immortal
    env.clock.advance(buffer.IDLE_TTL_SECONDS + 1)
    assert env.redis.keys() == []


def test_delete_removes_everything_and_is_idempotent(env):
    new(env, history=[{"role": "user", "text": PLAINTEXT_CANARY}])
    buffer.append_conversation(RUN, {"role": "user", "text": "more"})
    for i in range(1, 6):
        buffer.append_replay(RUN, evt(buffer.next_seq(RUN), "d"))
    assert env.redis.keys() != []
    assert buffer.delete_buffer(RUN) is True
    assert env.redis.keys() == [] and env.redis.all_strings() == []     # nothing remains: keys, fields, records
    assert buffer.delete_buffer(RUN) is False
    assert buffer.exists(RUN) is False
    for call in (lambda: buffer.read_conversation(RUN), lambda: buffer.replay_after(RUN, 0)):
        with pytest.raises(buffer.BufferMissing):
            call()


def test_delete_works_on_a_corrupt_or_foreign_buffer(env):
    new(env)
    env.mp.setenv("RUN_BUFFER_SECRET", OTHER_SECRET)
    with pytest.raises(buffer.BufferCorrupt):
        buffer.read_conversation(RUN)
    assert buffer.delete_buffer(RUN) is True                        # S06c deletes then rebuilds from the device's history
    assert env.redis.keys() == []
    new(env)                                                          # under the new key
    assert buffer.read_conversation(RUN).state == STATE


# ---------------------------------------------------------------------------
# Fail closed on Redis faults, and never log or re-raise a fault's text
# ---------------------------------------------------------------------------

CALLS = {
    "create": lambda: buffer.create_buffer(RUN_B, state=STATE),
    "append": lambda: buffer.append_conversation(RUN, {"role": "user", "text": "x"}),
    "read": lambda: buffer.read_conversation(RUN),
    "seq": lambda: buffer.next_seq(RUN),
    "replay_append": lambda: buffer.append_replay(RUN, evt(1)),
    "replay_read": lambda: buffer.replay_after(RUN, 0),
    "touch": lambda: buffer.touch(RUN),
    "exists": lambda: buffer.exists(RUN),
    "delete": lambda: buffer.delete_buffer(RUN),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_redis_fault_is_buffer_unavailable_with_fixed_text_no_cause_and_nothing_logged_but_the_type(env, caplog, name):
    new(env)
    env.redis.fail_if(lambda commands: True)
    with caplog.at_level("DEBUG"):
        with pytest.raises(buffer.BufferUnavailable) as info:
            CALLS[name]()
    assert CANARY not in str(info.value) and CANARY not in repr(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert CANARY not in caplog.text and SECRET not in caplog.text
    assert "ConnectionError" in caplog.text


def test_domain_errors_are_not_reported_as_unavailable(env):
    with pytest.raises(buffer.BufferMissing):
        buffer.read_conversation(RUN)
    new(env)
    with pytest.raises(buffer.BufferExists):
        new(env)


def test_the_module_never_logs_content_keys_or_the_secret(env, caplog):
    with caplog.at_level("DEBUG"):
        new(env, state=dict(STATE, instructions=PLAINTEXT_CANARY), history=[{"role": "user", "text": PLAINTEXT_CANARY}])
        buffer.append_replay(RUN, evt(buffer.next_seq(RUN), PLAINTEXT_CANARY))
        buffer.read_conversation(RUN)
        buffer.replay_after(RUN, 0)
        buffer.delete_buffer(RUN)
    assert PLAINTEXT_CANARY not in caplog.text and SECRET not in caplog.text
    assert "run_buffer:" not in caplog.text


# ---------------------------------------------------------------------------
# The production adapter (over memory.bus.redis)
# ---------------------------------------------------------------------------

class StubPipeline:
    def __init__(self, results):
        self.sent, self._results = [], results

    def execute(self, command):
        self.sent.append(list(command))
        return self

    def exec(self):
        return self._results[:len(self.sent)]


class StubUpstash:
    def __init__(self):
        self.executed, self.pipelines = [], []

    def execute(self, command):
        self.executed.append(list(command))
        return True if command[0] == "EXPIRE" else 7

    def multi(self):
        p = StubPipeline([5, True, False, 0, 1])
        self.pipelines.append(p)
        return p

    def pipeline(self):                       # must NOT be used: it is not a transaction
        raise AssertionError("the buffer's atomic steps must use multi(), not pipeline()")


def test_the_adapter_maps_one_command_to_execute_and_normalises_booleans():
    stub = StubUpstash()
    adapter = buffer._UpstashAdapter(lambda: stub)
    assert adapter.command("HGET", "k", "f") == 7
    assert adapter.command("EXPIRE", "k", 5, "LT") == 1 and adapter.command("EXPIRE", "k", 5) == 1
    assert stub.executed[0] == ["HGET", "k", "f"] and stub.executed[1] == ["EXPIRE", "k", "5", "LT"]


def test_the_adapter_runs_atomic_steps_as_one_multi_exec_request_and_normalises_results():
    stub = StubUpstash()
    adapter = buffer._UpstashAdapter(lambda: stub)
    out = adapter.atomic([("RPUSHX", "k", "a", "b"), ("EXPIRE", "k", 9), ("EXPIRE", "j", 9), ("EXPIRE", "i", 9), ("DEL", "x")])
    assert out == [5, 1, 0, 0, 1]                                    # True -> 1, False -> 0
    assert stub.pipelines[0].sent[0] == ["RPUSHX", "k", "a", "b"]
    assert len(stub.pipelines) == 1 and stub.executed == []


def test_the_default_client_is_the_bus_redis_resolved_lazily(env, monkeypatch):
    import memory.bus
    stub = StubUpstash()
    monkeypatch.setattr(memory.bus, "redis", stub)
    monkeypatch.setattr(buffer, "_REDIS", None)
    assert buffer.delete_buffer(RUN) is True                         # the stub answers DEL with a truthy int
    assert stub.executed and stub.executed[0][0] == "DEL"
    assert stub.executed[0][1:] == list(buffer.buffer_keys(RUN))


def test_importing_the_module_does_not_import_the_bus_or_the_database():
    tree = ast.parse((pathlib.Path(buffer.__file__)).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top for a in n.names} | {n.module for n in top if isinstance(n, ast.ImportFrom)}
    assert not any(str(x).startswith(("memory", "eo", "psycopg")) for x in names if x)
