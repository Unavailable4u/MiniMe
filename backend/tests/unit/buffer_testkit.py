"""Shared fakes for the S06b run-buffer tests (not collected: no test_ prefix).

  Clock          a controllable clock; the buffer's `_now` and the fake Redis's TTLs
                 both read it, so an idle expiry, a hard cap and a refresh can be
                 crossed in a test without sleeping.
  FakeRunRedis   the thin interface codeagent.buffer talks to (`command(*args)` and
                 `atomic(commands)`, see buffer.RedisLike), backed by a small
                 interpreter for exactly the Redis commands the buffer uses:
                 HGET HMGET HSETNX HSET HINCRBY RPUSH RPUSHX LRANGE EXPIRE (with LT)
                 DEL EXISTS TTL. It behaves like Redis where it matters here:
                   - everything is stored as a string and returned as one (HINCRBY
                     and the list lengths are ints), a missing key reads as None / [];
                   - RPUSHX does nothing (returns 0) when the list does not exist;
                   - EXPIRE returns 1 / 0, and `EXPIRE ... LT` only ever shortens;
                   - TTLs run on the Clock; an expired key is gone for every command;
                   - `atomic` runs its commands under one lock, so nothing interleaves.
                 It also records every request (its commands and its size in bytes,
                 counted the way the REST body would be: the length of every
                 argument) so a test can assert that no single request, and no single
                 reply, comes near a request-size limit, and offers fault hooks:
                   fail_if(predicate)   raise ConnectionError (with a canary in its
                                        text) for every request the predicate accepts;
                   before_request(fn)   call fn(commands) before a request is applied
                                        (to delete the buffer "between" two steps);
                   keys() / ttl(key) / all_strings()   inspection for assertions.
"""
import threading

CANARY = "CANARY-redis-fault-text-do-not-log-5b2e"
SECRET = "unit-test-run-buffer-secret-0123456789-abcdefghij"            # 49 chars, test-only
OTHER_SECRET = "unit-test-other-buffer-secret-9876543210-zyxwvutsrq"    # 51 chars, test-only
RUN = "run-buffer-aaaaaaaa"
RUN_B = "run-buffer-bbbbbbbb"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def advance(self, seconds):
        self.t += seconds

    def time(self):
        return self.t


class FakeRunRedis:
    def __init__(self, clock: Clock = None):
        self.clock = clock or Clock()
        self.lock = threading.RLock()
        self.store = {}              # key -> ("hash", dict) | ("list", list), all values str
        self.expires = {}            # key -> absolute time
        self.requests = []           # {"names": [...], "bytes": int, "reply_bytes": int}
        self._fail = None
        self._before = None

    # ---- the thin interface ----------------------------------------------
    def command(self, *args):
        return self._request([args])[0]

    def atomic(self, commands):
        return self._request([tuple(c) for c in commands])

    # ---- hooks -----------------------------------------------------------
    def fail_if(self, predicate):
        self._fail = predicate

    def before_request(self, fn):
        self._before = fn

    # ---- inspection ------------------------------------------------------
    def keys(self):
        with self.lock:
            self._purge()
            return sorted(self.store)

    def ttl(self, key):
        with self.lock:
            self._purge()
            if key not in self.store:
                return -2
            return int(round(self.expires[key] - self.clock.t)) if key in self.expires else -1

    def all_strings(self):
        """Every key, hash field, hash value and list element currently stored."""
        with self.lock:
            self._purge()
            out = list(self.store)
            for kind, value in self.store.values():
                if kind == "hash":
                    out += list(value) + list(value.values())
                else:
                    out += list(value)
            return out

    def get_hash(self, key):
        with self.lock:
            self._purge()
            return dict(self.store[key][1]) if key in self.store else None

    def get_list(self, key):
        with self.lock:
            self._purge()
            return list(self.store[key][1]) if key in self.store else None

    def set_hash_field(self, key, field, value):
        with self.lock:
            self.store[key][1][field] = value

    def set_list(self, key, items, ttl=None):
        with self.lock:
            self.store[key] = ("list", list(items))
            if ttl:
                self.expires[key] = self.clock.t + ttl

    def max_request_bytes(self):
        return max((r["bytes"] for r in self.requests), default=0)

    def max_reply_bytes(self):
        return max((r["reply_bytes"] for r in self.requests), default=0)

    # ---- the interpreter -------------------------------------------------
    def _request(self, commands):
        size = sum(len(str(a)) for c in commands for a in c)
        if self._before is not None:
            self._before(commands)
        with self.lock:
            if self._fail is not None and self._fail(commands):
                raise ConnectionError(f"connection to {CANARY} failed")
            self._purge()
            results = [self._do(c) for c in commands]
            reply = sum(len(str(x)) for r in results for x in (r if isinstance(r, list) else [r]))
            self.requests.append({"names": [str(c[0]).upper() for c in commands],
                                  "bytes": size, "reply_bytes": reply})
            return results

    def _purge(self):
        for key in [k for k, t in self.expires.items() if t <= self.clock.t]:
            self.store.pop(key, None)
            del self.expires[key]
        for key in [k for k in self.expires if k not in self.store]:
            del self.expires[key]

    def _hash(self, key, create=False):
        if key not in self.store:
            if not create:
                return None
            self.store[key] = ("hash", {})
        kind, value = self.store[key]
        assert kind == "hash", "WRONGTYPE"
        return value

    def _list(self, key, create=False):
        if key not in self.store:
            if not create:
                return None
            self.store[key] = ("list", [])
        kind, value = self.store[key]
        assert kind == "list", "WRONGTYPE"
        return value

    def _do(self, cmd):
        name, args = str(cmd[0]).upper(), [str(a) for a in cmd[1:]]
        if name == "HGET":
            h = self._hash(args[0])
            return None if h is None else h.get(args[1])
        if name == "HMGET":
            h = self._hash(args[0]) or {}
            return [h.get(f) for f in args[1:]]
        if name == "HSETNX":
            h = self._hash(args[0], create=True)
            if args[1] in h:
                return 0
            h[args[1]] = args[2]
            return 1
        if name == "HSET":
            h = self._hash(args[0], create=True)
            pairs = args[1:]
            assert len(pairs) % 2 == 0
            added = 0
            for f, v in zip(pairs[::2], pairs[1::2]):
                added += f not in h
                h[f] = v
            return added
        if name == "HINCRBY":
            h = self._hash(args[0], create=True)
            h[args[1]] = str(int(h.get(args[1], "0")) + int(args[2]))
            return int(h[args[1]])
        if name == "RPUSH":
            lst = self._list(args[0], create=True)
            lst.extend(args[1:])
            return len(lst)
        if name == "RPUSHX":
            lst = self._list(args[0])
            if lst is None:
                return 0
            lst.extend(args[1:])
            return len(lst)
        if name == "LRANGE":
            lst = self._list(args[0]) or []
            start, stop = int(args[1]), int(args[2])
            n = len(lst)
            start = max(n + start, 0) if start < 0 else start
            stop = n + stop if stop < 0 else stop
            return list(lst[start:stop + 1])
        if name == "EXPIRE":
            key, secs = args[0], int(args[1])
            flag = args[2].upper() if len(args) > 2 else None
            if key not in self.store:
                return 0
            new = self.clock.t + secs
            cur = self.expires.get(key)
            if flag == "LT" and cur is not None and not new < cur:
                return 0
            if flag == "GT" and (cur is None or not new > cur):
                return 0
            if flag == "NX" and cur is not None:
                return 0
            if flag == "XX" and cur is None:
                return 0
            self.expires[key] = new
            return 1
        if name == "DEL":
            n = 0
            for key in args:
                if key in self.store:
                    del self.store[key]
                    self.expires.pop(key, None)
                    n += 1
            return n
        if name == "EXISTS":
            return sum(1 for key in args if key in self.store)
        if name == "TTL":
            return self.ttl(args[0])
        raise AssertionError(f"FakeRunRedis does not implement {name}")
