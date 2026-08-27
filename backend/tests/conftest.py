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
import contextlib
import json
import sys
from types import ModuleType
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
import eo.registry  # noqa: F401

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
    module under test has actually been imported.

    getattr(mod, ..., None) is NOT a safe way to probe an arbitrary
    module here: it only swallows AttributeError, but some third-party
    packages install lazy-import module proxies (e.g. yt_dlp's
    dependencies.Cryptodome, via compat_utils.passthrough_module) whose
    __getattr__ raises ModuleNotFoundError/ImportError instead for any
    attribute that isn't the one thing it's a shim for. Once such a
    module lands in sys.modules (pulled in transitively by an unrelated
    test), this sweep would crash on it and take down every OTHER test
    that depends on mock_llm in the same session. Skip modules that
    raise on attribute access rather than letting the sweep die."""
    hits = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if name == "utils.llm_client":
            continue
        try:
            has_it = getattr(mod, "generate_text", None) is not None
        except Exception:
            continue
        if has_it:
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


# ---------------------------------------------------------------------------
# 3. run_static_scan (B2) — same sweep-and-patch shape as generate_text
#    above, for the exact same reason: agents/security_scanner.py does
#    `from agents.static_scan import run_static_scan`, a bound name in its
#    own module namespace, so patching agents.static_scan.run_static_scan
#    directly would NOT reach the copy security_scanner.py actually calls.
#    Without this, every security_scanner test would try to spin up a real
#    E2B sandbox (network call, needs E2B_API_KEY) on every run.
# ---------------------------------------------------------------------------

def _modules_with_bound_static_scan():
    """Same lazy-import-proxy hazard as _modules_with_bound_generate_text()
    above — getattr(..., None) alone isn't enough, since some modules'
    __getattr__ raises something other than AttributeError. See that
    function's docstring for the concrete case (yt_dlp's Cryptodome
    shim)."""
    hits = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if name == "agents.static_scan":
            continue
        try:
            has_it = getattr(mod, "run_static_scan", None) is not None
        except Exception:
            continue
        if has_it:
            hits.append(mod)
    return hits


class StaticScanPatcher:
    """Patches run_static_scan in-place across every module that imported
    it. Defaults to 'tools found nothing' (empty findings, no error) —
    the safe default that also means the LLM summarization call doesn't
    fire unless a test explicitly gives the tools something to find, via
    set_findings()."""

    def __init__(self):
        self._mock = MagicMock()
        self._patched_modules = []
        self.set_findings([])

    def set_findings(self, findings, tool_error=None):
        result = {"findings": findings, "tool_error": tool_error}
        self._mock.side_effect = lambda *a, **k: result

    def raise_via_tool_error(self, message):
        """Simulates a sandbox/tool failure — run_static_scan itself never
        raises (see its own docstring), it degrades to this shape."""
        self.set_findings([], tool_error=message)

    @property
    def mock(self):
        return self._mock

    def _patch_all_now(self, monkeypatch):
        for mod in _modules_with_bound_static_scan():
            monkeypatch.setattr(mod, "run_static_scan", self._mock, raising=False)
            self._patched_modules.append(mod)
        try:
            import agents.static_scan as static_scan_mod
            monkeypatch.setattr(static_scan_mod, "run_static_scan", self._mock, raising=False)
        except ImportError:
            pass


@pytest.fixture
def mock_static_scan(monkeypatch):
    """Same usage shape/caveat as mock_llm: import the agent module under
    test before relying on this fixture's sweep to reach it.

        import agents.security_scanner
        def test_x(mock_static_scan, mock_llm):
            mock_static_scan.set_findings([{"severity": "critical", ...}])
            mock_llm.set_json_response({...})
            ...
    """
    patcher = StaticScanPatcher()
    patcher._patch_all_now(monkeypatch)
    yield patcher
    patcher._patch_all_now(monkeypatch)


# ---------------------------------------------------------------------------
# 4. web_search (utils/web_search.py) — autouse, no fixture request needed
#
#    Found via a Windows test-run audit: tests/integration/test_gatekeeper.py
#    and test_resume_graph.py drive agents/web_researcher.py through the real
#    executor/gatekeeper code, which calls utils.web_search.search() with no
#    mocking anywhere in this file. On a machine with a real TAVILY_API_KEY
#    in backend/.env (loaded transitively the same way LANGFUSE_* is above --
#    see test_eo_tracing.py's fix), that made REAL calls to Tavily on every
#    test run. Once that key was already rate-limited, Tavily's retry/
#    fallback path (per-result-URL classification calls, provider-chain
#    fallback to Exa) made extra generate_text() calls the tests never
#    accounted for, draining mock_llm's finite set_sequence() queues
#    (StopIteration) and inflating call counts.
#
#    This is exactly the class of bug fake_bus (above) already exists to
#    prevent for Redis: tests/unit and tests/integration must never depend
#    on -- or burn quota against -- real external services, regardless of
#    what happens to be sitting in a contributor's local .env. Same
#    lazy-import hazard as generate_text (agents/web_researcher.py and
#    agents/part_price_finder.py both do
#    `from utils.web_search import search as web_search`, a bound name in
#    their own module namespace), so this uses the same sweep-and-patch
#    shape as _modules_with_bound_generate_text() above.
#
#    tests/manual/ is excluded the same way it already is for fake_bus --
#    see tests/manual/conftest.py's override of this fixture.
# ---------------------------------------------------------------------------

def _modules_with_bound_web_search():
    """Same lazy-import-proxy hazard noted on _modules_with_bound_generate_text()
    above -- getattr(..., None) alone isn't safe against modules whose
    __getattr__ raises something other than AttributeError.

    Extra hazard specific to this name (found the hard way -- see the git
    log for this line): "web_search" is both the bound-function-name
    convention (`from utils.web_search import search as web_search`, used
    by agents/web_researcher.py and agents/part_price_finder.py) AND,
    completely coincidentally, the exact name of the utils.web_search
    submodule itself. Python's import system auto-sets `web_search` as an
    attribute on the parent `utils` PACKAGE module the first time
    `utils.web_search` is imported anywhere (that's what makes
    `import utils.web_search as x` work at all). Without the isinstance
    check below, this sweep matches the `utils` package too, and patching
    it replaces that submodule attribute with the mock -- which silently
    breaks every `import utils.web_search as web_search` statement
    everywhere else (including in test files that patch the module
    directly), since that import resolves through the package attribute,
    not sys.modules, once the submodule has been auto-attached. Skipping
    module-type hits keeps this sweep matching only genuine bound
    function references."""
    hits = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if name == "utils.web_search":
            continue
        try:
            candidate = getattr(mod, "web_search", None)
        except Exception:
            continue
        if candidate is None or isinstance(candidate, ModuleType):
            continue
        hits.append(mod)
    return hits


class WebSearchPatcher:
    """Patches utils.web_search.search in-place across every module that
    imported it (bound as `web_search` or otherwise), plus the module
    itself so deferred/local `from utils.web_search import search` imports
    (e.g. agents/component_spec_lookup.py, which imports inside the
    function body on every call) pick up the patched version too.

    Defaults to "no results" ([]) -- the same safe, non-fatal outcome
    utils/web_search.py's own docstring says every current caller already
    treats a real empty response as, so this never changes control flow
    for a test that doesn't care about search results. Call
    set_results()/set_sequence() when a test needs specific results."""

    def __init__(self):
        self._mock = MagicMock()
        self._patched_modules = []
        self._mock.side_effect = lambda *a, **k: []

    def set_results(self, results):
        self._mock.side_effect = lambda *a, **k: results

    def set_sequence(self, results_list):
        it = iter(results_list)
        self._mock.side_effect = lambda *a, **k: next(it)

    @property
    def mock(self):
        return self._mock

    def _patch_all_now(self, monkeypatch):
        for mod in _modules_with_bound_web_search():
            monkeypatch.setattr(mod, "web_search", self._mock, raising=False)
            self._patched_modules.append(mod)
        try:
            import utils.web_search as web_search_mod
            monkeypatch.setattr(web_search_mod, "search", self._mock, raising=False)
        except ImportError:
            pass


@pytest.fixture(autouse=True)
def _no_real_web_search(monkeypatch):
    """Autouse (unlike mock_llm/mock_static_scan): unlike an LLM call, a
    test doesn't have to be *about* search for the code under test to
    reach it incidentally (gatekeeper/executor/resume_graph all can, via
    web_researcher), so opting in per-test isn't good enough here -- the
    whole point is that nothing under tests/unit or tests/integration
    should ever be able to make a real Tavily/Exa call, full stop. A test
    that specifically wants to assert on search behavior can still pull
    `_web_search_patcher` and call set_results()/set_sequence()."""
    patcher = WebSearchPatcher()
    patcher._patch_all_now(monkeypatch)
    yield patcher
    patcher._patch_all_now(monkeypatch)


@pytest.fixture
def _web_search_patcher(_no_real_web_search):
    """Named alias for tests that want to configure search results --
    same object _no_real_web_search already installed, just a clearer name
    to request when a test cares about it specifically."""
    return _no_real_web_search


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
