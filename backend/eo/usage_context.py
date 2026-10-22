"""
eo/usage_context.py -- S01: who is this LLM call for, and on which surface?

utils/llm_client.py's ledger hook runs deep inside the provider fallback
chain, where no user id is in scope and none is passed down (see
migration 0008's header and _record_call_log()'s docstring for the same
constraint). This module is how that hook finds out. Entry points that DO
know the account -- api/task_runner.py's run_task()/preview_task()/
confirm_task()/run_task_from_template(), api/routes/tasks.py's resume --
open a `scope(user_id, surface, session_id)`; the hook calls `resolve()`.

Attribution is resolved in this order, first match wins:

  1. The ContextVar scope of the calling thread/task. Free, exact, and
     the common case: most LLM calls run on the same thread as the entry
     point that opened the scope.
  2. A session binding, looked up by the session_id the llm_client call
     already carries. This exists because contextvars do NOT cross into
     `concurrent.futures.ThreadPoolExecutor` workers -- a worker thread
     starts with an empty context (eo/executor.py's _open_member_span()
     documents the same blind spot for OTEL) -- and ~20 modules here fan
     LLM calls out through raw pools. Without this fallback every one of
     those calls would be metered as 'system'. A binding lasts exactly as
     long as the scope that created it.
  3. 'system': no owning account in scope. The ledger stores such a call
     with user_id NULL and surface 'system' rather than guessing a user.

Known limit, deliberately conservative: if two live scopes share a
session_id but belong to different users or surfaces (two partners
running tasks concurrently in one shared chat), a pool-thread call that
can only be matched by session_id is ambiguous. It is attributed to
'system' (and a warning is printed once) rather than risk charging the
wrong person. Calls on the scope's own thread are unaffected -- rule 1
resolves those exactly.

Nothing here touches the database or imports anything from this
codebase, so it is safe to import from utils/llm_client.py.
"""
import contextlib
import contextvars
import functools
import inspect
import threading
from dataclasses import dataclass

SURFACES = ("web", "m2c", "menux", "system")


@dataclass(frozen=True)
class UsageContext:
    """Immutable answer to "who is this call for". user_id is None if and
    only if surface == 'system' (the same invariant the usage_ledger table
    enforces with a CHECK constraint)."""
    user_id: "str | None"
    surface: str
    session_id: "str | None" = None


class _Scope:
    """One live scope. Mutable only so attach_session() can add the session
    id of a run that generated its own after the scope was already open."""
    __slots__ = ("user_id", "surface", "session_id", "bound_sessions")

    def __init__(self, user_id: str, surface: str, session_id: "str | None"):
        self.user_id = user_id
        self.surface = surface
        self.session_id = session_id
        self.bound_sessions: list = []

    def snapshot(self) -> UsageContext:
        return UsageContext(self.user_id, self.surface, self.session_id)


_current: "contextvars.ContextVar[_Scope | None]" = contextvars.ContextVar(
    "usage_context_scope", default=None)

# session_id -> live scopes bound to it. Guarded by _lock: it is read and
# written from many worker threads at once.
_bindings: "dict[str, list[_Scope]]" = {}
_lock = threading.Lock()

_warned_ambiguous: set = set()
_MAX_WARNED = 1000


def _bind(session_id: str, scope: _Scope) -> None:
    with _lock:
        _bindings.setdefault(session_id, []).append(scope)
        scope.bound_sessions.append(session_id)


def _unbind_all(scope: _Scope) -> None:
    with _lock:
        for session_id in scope.bound_sessions:
            live = _bindings.get(session_id)
            if not live:
                continue
            try:
                live.remove(scope)
            except ValueError:
                pass
            if not live:
                _bindings.pop(session_id, None)
        scope.bound_sessions.clear()


def current() -> "UsageContext | None":
    """The calling thread/task's own scope (rule 1 only), or None."""
    scope = _current.get()
    return scope.snapshot() if scope is not None else None


def resolve(session_id: "str | None" = None) -> UsageContext:
    """Who is a call with this session_id for? Never raises, never returns
    None. See the module docstring for the resolution order."""
    scope = _current.get()
    if scope is not None:
        return UsageContext(scope.user_id, scope.surface, scope.session_id or session_id)

    if session_id:
        with _lock:
            live = list(_bindings.get(session_id, ()))
        if live:
            identities = {(s.user_id, s.surface) for s in live}
            if len(identities) == 1:
                user_id, surface = next(iter(identities))
                return UsageContext(user_id, surface, session_id)
            if session_id not in _warned_ambiguous and len(_warned_ambiguous) < _MAX_WARNED:
                _warned_ambiguous.add(session_id)
                print(f"  [usage_context] session {session_id!r} has live scopes for "
                      f"{len(identities)} different users/surfaces; attributing a "
                      f"thread-pool call to 'system' rather than guess (non-fatal).")
    return UsageContext(None, "system", session_id)


@contextlib.contextmanager
def scope(user_id: "str | None", surface: str = "web", session_id: "str | None" = None):
    """Attribute every LLM call made inside this block (and on pool threads
    that carry the same session_id) to `user_id` on `surface`.

    A missing user_id never downgrades an attribution that already exists:
    with an outer scope open, scope(None, ...) keeps the outer user and
    surface and only adds session_id. With no outer scope it is a no-op --
    the calls simply resolve to 'system'. That keeps a non-HTTP caller (a
    script, a test) that passes no owner_id from being treated as an
    account.

    Must be entered and exited in the same context (same thread, or same
    asyncio task), like any contextvar token.
    """
    if surface not in SURFACES:
        raise ValueError(f"surface must be one of {SURFACES}, got {surface!r}")
    outer = _current.get()
    if not user_id:
        if outer is None:
            yield None
            return
        user_id, surface = outer.user_id, outer.surface
    elif surface == "system":
        raise ValueError("a 'system' scope cannot carry a user_id")

    new = _Scope(user_id, surface, session_id or (outer.session_id if outer else None))
    token = _current.set(new)
    if new.session_id:
        _bind(new.session_id, new)
    try:
        yield new.snapshot()
    finally:
        _current.reset(token)
        _unbind_all(new)


def attach_session(session_id: "str | None") -> None:
    """Add a session id to the scope already open on this thread. For entry
    points that generate their own (task_runner's `session_id or
    uuid4()`), where the id does not exist yet when the scope opens.
    No-op without an open scope or without an id."""
    scope_ = _current.get()
    if scope_ is None or not session_id or session_id in scope_.bound_sessions:
        return
    scope_.session_id = session_id
    _bind(session_id, scope_)


def bind(fn):
    """Wrap `fn` so that, when it later runs on ANOTHER thread, it runs
    under the usage scope that is open right now on this one:

        executor.submit(usage_context.bind(worker), arg)

    For thread-pool fan-out whose LLM calls carry no session_id and so
    cannot be matched by rule 2. Carries only this module's scope -- not
    the whole contextvars.Context -- so it does not change what OTEL or
    any other contextvar-based machinery sees in the worker. With no
    scope open it returns `fn` untouched.
    """
    snap = current()
    if snap is None:
        return fn

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        with scope(snap.user_id, snap.surface, snap.session_id):
            return fn(*args, **kwargs)
    return _wrapped


def attributed(surface: str = "web"):
    """Decorator for entry points that take `owner_id` and `session_id`:
    runs the function inside scope(owner_id, surface, session_id), reading
    both from the call's own arguments. A call with no owner_id is left
    unattributed (see scope()). Adds nothing to the signature, so it is a
    one-line change per entry point instead of re-indenting a long body.
    """
    if surface not in SURFACES or surface == "system":
        raise ValueError(f"attributed() surface must be a user surface, got {surface!r}")

    def _decorate(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            try:
                bound = sig.bind_partial(*args, **kwargs).arguments
            except TypeError:
                bound = {}
            with scope(bound.get("owner_id"), surface, bound.get("session_id")):
                return fn(*args, **kwargs)
        return _wrapper
    return _decorate
