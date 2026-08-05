"""
Shared fixtures for the whole tests/ tree (unit + integration).

Two problems this file exists to solve, both found during the B1 audit:

1. Several old test/dev scripts call memory.bus functions directly, which
   hit your REAL Upstash Redis (memory/bus.py's module-level `redis`
   client). At LEAST test_brief_writer.py, test_staff_task_e2e.py,
   test_seed_bootstrap.py, test_memory_bus.py, test_namespacing.py, and
   test_planner.py do this — one of them (test_seed_bootstrap.py) calls
   write(ROLE_PROMPTS_KEY, None), which wipes your real role-prompt
   registry if run against production Redis.

   Fix: `memory.bus` only ever touches Redis through the single
   module-level `redis` object (get/set/delete/mget — confirmed by
   auditing memory/bus.py directly). This autouse fixture swaps that one
   object for an in-memory FakeRedis before every test and restores it
   after, so nothing under tests/ can ever touch production data, with
   zero per-test-file changes required.

2. Most agents import `generate_text` directly into their own module
   namespace (`from utils.llm_client import generate_text`), so
   `unittest.mock.patch("utils.llm_client.generate_text")` does NOT mock
   the copy an agent actually calls — you have to patch it in every
   agent module individually, which is exactly the kind of thing that
   silently rots as new agents get added. `patch_generate_text()` below
   patches every module currently holding a bound reference to it, found
   by walking sys.modules, so new agents get covered automatically the
   next time they're imported.

Usage in a test:

    def test_something(fake_bus, mock_llm):
        mock_llm.set_response('{"summary": "ok"}')
        ...

or, if a test wants raw control:

    def test_something(fake_bus):
        with patch_generate_text(return_value="...") as mock:
            ...
"""
import sys
import json
import contextlib
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# 0. Import-order landmine (found while validating this file against the
#    real repo — see the B1 findings note handed back alongside this file).
#    eo/registry.py's `from agents import (...)` block (line ~1568) sits
#    AFTER AGENT_CAPABILITIES is defined, so importing eo.registry first
#    always succeeds. But several agents import back from eo/other agents
#    at module scope (agents/reviewer.py <- agents/generic_worker.py <-
#    eo/conversation_memory.py, and separately agents/code_writers.py +
#    agents/content_adapter_pool.py <- eo/worker_pool.py <- eo/registry.py)
#    — so importing ANY of those agent modules directly, before anything
#    else has pulled in eo.registry, raises ImportError: cannot import
#    name '...' from partially initialized module. This is a real bug in
#    the app, not a test artifact — flagged for a proper fix (see notes),
#    but tests need to run today, so: force the known-safe order once,
#    here, before pytest imports any test module that might import an
#    agent directly.
import eo.registry  # noqa: E402,F401


# ---------------------------------------------------------------------------
# 1. Fake Redis (in-memory stand-in for upstash_redis.Redis)
# ---------------------------------------------------------------------------

class FakeRedis:
    """Minimal in-memory stand-in for the upstash_redis.Redis surface that
    memory/bus.py actually uses: get, set, delete, mget. Values are stored
    as whatever bus.py hands us (bus.py itself does the json.dumps/loads —
    see write()/read() — so this stays a dumb string store, matching real
    Upstash REST semantics)."""

    def __init__(self):
        self._store = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, *args, **kwargs):
        self._store[key] = value
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                n += 1
        return n

    def mget(self, *keys):
        # upstash_redis.Redis.mget() is variadic (mget(*keys)) — see
        # memory/bus.py's read_many(), which calls it as redis.mget(*keys).
        # Also accept a single list/tuple arg for safety, in case any
        # caller ever passes one.
        if len(keys) == 1 and isinstance(keys[0], (list, tuple)):
            keys = keys[0]
        return [self._store.get(k) for k in keys]

    # Convenience for tests that want to inspect/seed state directly
    def _dump(self):
        return dict(self._store)


@pytest.fixture(autouse=True)
def fake_bus(monkeypatch):
    """Autouse: every test in tests/unit and tests/integration gets a
    clean, isolated FakeRedis in place of memory.bus.redis. tests/manual
    is excluded (see tests/manual/conftest.py) since those scripts are
    meant to hit real infra by hand."""
    from memory import bus as bus_module
    fake = FakeRedis()
    monkeypatch.setattr(bus_module, "redis", fake)
    yield fake


@pytest.fixture(autouse=True)
def _reset_role_prompts_cache():
    """Autouse: found while building B1 sector 2's brief-writer tests.

    eo/registry.py's _load_prompts() keeps a per-run role-prompt cache in
    a module-level contextvars.ContextVar (_role_prompts_cache_ctx),
    deliberately scoped to survive repeated calls WITHIN one request/CLI
    run (see that module's own comment above the ContextVar). pytest
    doesn't give each test function a fresh contextvars.Context the way a
    new incoming request does, so without this reset, the FIRST test that
    calls add_role_prompt()/get_role_prompt() for a given role poisons
    every later test in the same pytest process: fake_bus above already
    swaps in a brand-new empty FakeRedis per test, but a role added in
    test A would still resolve as a cache hit in test B even though
    test B's fake Redis has never heard of it -- zero LLM call, and a
    brief from a different test's fixture data. Resetting the ContextVar
    itself (not just the bus) before every test closes that gap."""
    import eo.registry as registry_module
    registry_module._role_prompts_cache_ctx.set(None)
    yield
    registry_module._role_prompts_cache_ctx.set(None)


@pytest.fixture(autouse=True)
def _reset_app_slug_context():
    """Autouse: same class of bug as _reset_role_prompts_cache above, and
    found the same way -- writing this test suite, not by inspection.
    memory/bus.py's _namespaced() prefixes every non-exempt bus key with
    whatever memory.bus._app_slug_ctx currently holds (a ContextVar, "Migration
    Part B"). write(KEYS["app_slug"], ...) and set_app_slug() both set it
    as a SIDE EFFECT of an ordinary-looking bus call, and -- exactly like
    _role_prompts_cache_ctx -- pytest does not give each test function a
    fresh contextvars.Context, so a slug set in test A silently changes
    which physical key test B's read()/write() calls land on, even though
    fake_bus above hands test B a brand-new, empty FakeRedis. Concretely:
    a test that seeds fixed_code, THEN sets app_slug, running right after
    a DIFFERENT test that left some other slug active, can write
    fixed_code under the wrong namespace and then read it back as
    missing under the new one -- confirmed by running this suite under
    pytest-randomly, see tests/integration/test_structure_architect.py's
    fixture ordering fix for the specific case this caught."""
    import memory.bus as bus_module
    bus_module._app_slug_ctx.set(None)
    yield
    bus_module._app_slug_ctx.set(None)


# ---------------------------------------------------------------------------
# 2. generate_text — patch every module that already imported it
# ---------------------------------------------------------------------------

def _modules_with_bound_generate_text():
    """Every currently-imported module whose namespace holds a reference
    named `generate_text` (i.e. did `from utils.llm_client import
    generate_text`). Only catches modules already in sys.modules at call
    time, which is why mock_llm patches lazily on first use inside a test
    rather than once at conftest-collection time — by then the agent
    module under test has actually been imported."""
    hits = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if getattr(mod, "generate_text", None) is not None and name != "utils.llm_client":
            hits.append(mod)
    return hits


class LLMPatcher:
    """Patches `generate_text` in-place across every module that imported
    it, records call args, and lets a test set canned responses."""

    def __init__(self):
        self._mock = MagicMock()
        self._patched_modules = []
        self._default_response = '{"result": "mocked"}'
        self._mock.side_effect = lambda *a, **k: self._default_response

    def set_response(self, text):
        """Next (and subsequent) calls return this raw string, exactly as
        generate_text() would — agents are responsible for their own
        json.loads(), same as with a real response."""
        self._default_response = text
        self._mock.side_effect = lambda *a, **k: self._default_response

    def set_json_response(self, obj):
        self.set_response(json.dumps(obj))

    def set_sequence(self, responses):
        """Return a different response on each successive call — useful
        for testing fallback-chain / retry logic."""
        it = iter(responses)
        self._mock.side_effect = lambda *a, **k: next(it)

    def raise_on_call(self, exc):
        self._mock.side_effect = exc

    @property
    def mock(self):
        return self._mock

    def _patch_all_now(self, monkeypatch, extra_modules=()):
        targets = _modules_with_bound_generate_text()
        for mod in list(extra_modules) + targets:
            if hasattr(mod, "generate_text"):
                monkeypatch.setattr(mod, "generate_text", self._mock, raising=False)
                self._patched_modules.append(mod)
        # utils.llm_client itself, so code that imports the module rather
        # than the name (`llm_client.generate_text(...)`) is covered too.
        try:
            import utils.llm_client as llm_client_mod
            monkeypatch.setattr(llm_client_mod, "generate_text", self._mock, raising=False)
        except ImportError:
            pass


@pytest.fixture
def mock_llm(monkeypatch):
    """Patches generate_text everywhere it's currently imported. IMPORTANT:
    import the agent module under test BEFORE relying on this fixture's
    patch reaching it — pytest fixture setup runs before your test body,
    but the agent must already be on sys.modules for the sweep to find it.
    Safest pattern:

        import agents.report_writer  # ensure it's imported
        def test_x(mock_llm):
            mock_llm.set_json_response({...})
            ...

    Since most test files already `import agents.<x>` at the top, this is
    rarely an issue in practice — call out to `mock_llm.mock` for
    call-count/call-args assertions same as any MagicMock.
    """
    patcher = LLMPatcher()
    patcher._patch_all_now(monkeypatch)
    yield patcher
    # re-sweep in case the test itself imported a new agent module after
    # fixture setup (e.g. a lazy `import` inside the test body)
    patcher._patch_all_now(monkeypatch)


@contextlib.contextmanager
def patch_generate_text(return_value=None, side_effect=None):
    """Non-fixture version for tests/scripts that need a `with` block
    instead of a pytest fixture (e.g. dev scripts kept outside pytest)."""
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    patcher = LLMPatcher()
    if side_effect is not None:
        patcher._mock.side_effect = side_effect
    elif return_value is not None:
        patcher.set_response(return_value)
    patcher._patch_all_now(mp)
    try:
        yield patcher
    finally:
        mp.undo()
