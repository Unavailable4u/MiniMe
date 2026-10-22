"""eo/usage_context.py -- who is an LLM call for? (S01)

Pure in-process logic, no database. The thread-pool cases matter most:
contextvars do not cross into ThreadPoolExecutor workers, and ~20 modules
here fan LLM calls out through raw pools.
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from eo import usage_context as uc


def test_no_scope_resolves_to_system():
    who = uc.resolve("sess")
    assert (who.user_id, who.surface, who.session_id) == (None, "system", "sess")
    assert uc.current() is None


def test_scope_attributes_calls_on_the_same_thread():
    with uc.scope("u1", "web", "s1"):
        who = uc.resolve(None)
        assert (who.user_id, who.surface, who.session_id) == ("u1", "web", "s1")
    assert uc.resolve(None).surface == "system"


def test_scope_cleans_up_even_when_the_body_raises():
    with pytest.raises(RuntimeError):
        with uc.scope("u1", "web", "s1"):
            raise RuntimeError("boom")
    assert uc.resolve("s1").surface == "system"  # binding released too


def test_pool_worker_without_context_is_matched_by_session_id():
    with uc.scope("u1", "m2c", "s1"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            who = pool.submit(uc.resolve, "s1").result()
    assert (who.user_id, who.surface) == ("u1", "m2c")


def test_pool_worker_with_an_unknown_session_is_system():
    with uc.scope("u1", "web", "s1"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            who = pool.submit(uc.resolve, "someone-elses-session").result()
    assert who.surface == "system" and who.user_id is None


def test_pool_worker_with_no_session_id_needs_bind():
    with uc.scope("u1", "web", "s1"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            unbound = pool.submit(uc.resolve, None).result()
            bound = pool.submit(uc.bind(lambda: uc.resolve(None))).result()
    assert unbound.surface == "system"
    assert (bound.user_id, bound.surface) == ("u1", "web")


def test_bind_without_a_scope_returns_the_function_untouched():
    fn = lambda: 1  # noqa: E731
    assert uc.bind(fn) is fn


def test_two_users_sharing_a_session_never_get_each_other_charged():
    """Two partners run tasks at once in one shared chat. A pool-thread call
    that can only be matched by session_id is ambiguous -> 'system', never
    one of the two users. Calls on each scope's own thread stay exact."""
    import threading
    both_in, release, seen = threading.Barrier(3), threading.Event(), {}

    def run(user):
        with uc.scope(user, "web", "shared"):
            seen[user] = uc.resolve(None).user_id           # own thread: exact
            both_in.wait(timeout=5)
            release.wait(timeout=5)

    threads = [threading.Thread(target=run, args=(u,)) for u in ("alice", "bob")]
    for t in threads:
        t.start()
    both_in.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        ambiguous = pool.submit(uc.resolve, "shared").result()
    release.set()
    for t in threads:
        t.join()
    assert seen == {"alice": "alice", "bob": "bob"}
    assert ambiguous.user_id is None and ambiguous.surface == "system"


def test_same_user_twice_on_a_session_is_not_ambiguous():
    with uc.scope("u1", "web", "s1"), uc.scope("u1", "web", "s1"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            who = pool.submit(uc.resolve, "s1").result()
    assert who.user_id == "u1"


def test_missing_user_id_never_downgrades_an_outer_attribution():
    with uc.scope("u1", "m2c", "s1"):
        with uc.scope(None, "web", "s2"):
            who = uc.resolve(None)
    assert (who.user_id, who.surface, who.session_id) == ("u1", "m2c", "s2")


def test_missing_user_id_with_no_outer_scope_is_a_noop():
    with uc.scope(None, "web", "s1") as snap:
        assert snap is None
        assert uc.resolve("s1").surface == "system"


def test_attach_session_binds_an_id_minted_after_the_scope_opened():
    with uc.scope("u1", "web", None):
        uc.attach_session("minted-later")
        with ThreadPoolExecutor(max_workers=1) as pool:
            who = pool.submit(uc.resolve, "minted-later").result()
    assert who.user_id == "u1"
    assert uc.resolve("minted-later").surface == "system"  # released on exit


def test_attach_session_without_a_scope_is_a_noop():
    uc.attach_session("x")
    assert uc.resolve("x").surface == "system"


def test_attributed_reads_owner_and_session_from_the_call():
    seen = {}

    @uc.attributed("web")
    def entry(task_text, session_id=None, owner_id=None):
        seen["who"] = uc.resolve(None)

    entry("t", session_id="s9", owner_id="u9")
    assert (seen["who"].user_id, seen["who"].session_id) == ("u9", "s9")
    entry("t", session_id="s8")  # no owner_id -> left unattributed, never guessed
    assert seen["who"].surface == "system"


def test_attributed_passes_positional_arguments_through_unchanged():
    @uc.attributed("web")
    def entry(a, b, owner_id=None):
        return (a, b, owner_id)

    assert entry(1, 2, owner_id="u") == (1, 2, "u")


def test_invalid_surfaces_are_rejected():
    with pytest.raises(ValueError):
        with uc.scope("u1", "mobile"):
            pass
    with pytest.raises(ValueError):
        with uc.scope("u1", "system"):
            pass
    with pytest.raises(ValueError):
        uc.attributed("system")
