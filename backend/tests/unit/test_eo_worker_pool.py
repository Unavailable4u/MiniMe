"""
tests/unit/test_eo_worker_pool.py — Patch 7e-S2.

eo/worker_pool.py had zero test coverage before this. It's the shared
selection logic behind any parallel fan-out pool (agents/code_writers.py
today, agents/content_adapter_pool.py per the module docstring): which
accounts are ELIGIBLE for a given role_tag (_eligible_pool), and which
`worker_count` of them actually get used (_select_workers) -- either a
Panel-driven key_override taken verbatim, or the least-used accounts by
today's live quota snapshot (the fairness rotation).

Style/isolation notes:
  - eo/registry.py's AGENT_CAPABILITIES is real production config. Per
    the same pattern tests/unit/test_eo_quota_sentinel.py already
    established for a sibling module, this is patched to a small fixed
    fake dict for every test rather than exercising the real registry.
  - Unlike quota_sentinel's functions (which import AGENT_CAPABILITIES
    with a DEFERRED import inside each function body), eo/worker_pool.py
    imports both AGENT_CAPABILITIES and get_quota_snapshot at module TOP
    LEVEL (`from eo.registry import AGENT_CAPABILITIES`, `from
    eo.quota_sentinel import get_quota_snapshot`) -- bound names in
    eo.worker_pool's own namespace. Per conftest.py's documented
    generate_text gotcha, these must be patched as
    eo.worker_pool.AGENT_CAPABILITIES / eo.worker_pool.get_quota_snapshot,
    NOT eo.registry.AGENT_CAPABILITIES / eo.quota_sentinel.get_quota_snapshot
    -- patching the latter would not reach the copies this module
    actually reads.
  - relay.emitter.emit_event is bound the same top-level way, so it's
    patched as eo.worker_pool.emit_event.
"""
from unittest.mock import MagicMock

import pytest

from eo import worker_pool
from eo.worker_pool import _eligible_pool, _select_workers

FAKE_CAPABILITIES = {
    "KEY_A": {"natural_roles": ["implementer"]},
    "KEY_B": {"natural_roles": ["implementer", "content_writer"]},
    "KEY_C": {"natural_roles": ["content_writer"]},
    "KEY_D": {"natural_roles": ["implementer"]},
    "KEY_E": {"natural_roles": []},
}


@pytest.fixture(autouse=True)
def patch_registry(monkeypatch):
    monkeypatch.setattr(worker_pool, "AGENT_CAPABILITIES", FAKE_CAPABILITIES)


@pytest.fixture
def mock_emit(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(worker_pool, "emit_event", mock)
    return mock


@pytest.fixture
def mock_quota(monkeypatch):
    """Returns a setter the test calls with {key: pct} to control
    get_quota_snapshot()'s output, matching its real
    {agent_key: {"pct": float, ...}} shape."""
    def _set(pct_by_key: dict):
        snapshot = {k: {"pct": v} for k, v in pct_by_key.items()}
        monkeypatch.setattr(worker_pool, "get_quota_snapshot", lambda: snapshot)
    return _set


# ---------------------------------------------------------------------------
# _eligible_pool
# ---------------------------------------------------------------------------

def test_eligible_pool_filters_by_role_tag():
    assert sorted(_eligible_pool("implementer")) == ["KEY_A", "KEY_B", "KEY_D"]


def test_eligible_pool_a_role_tag_can_span_multiple_accounts():
    assert sorted(_eligible_pool("content_writer")) == ["KEY_B", "KEY_C"]


def test_eligible_pool_unknown_role_tag_returns_empty():
    assert _eligible_pool("nonexistent_tag") == []


def test_eligible_pool_account_with_no_natural_roles_never_matches():
    assert "KEY_E" not in _eligible_pool("implementer")
    assert "KEY_E" not in _eligible_pool("content_writer")


def test_eligible_pool_missing_natural_roles_key_treated_as_empty(monkeypatch):
    """info.get("natural_roles", []) -- an account dict missing the key
    entirely must not raise, just never match anything."""
    monkeypatch.setattr(worker_pool, "AGENT_CAPABILITIES", {"KEY_X": {}})
    assert _eligible_pool("implementer") == []


# ---------------------------------------------------------------------------
# _select_workers -- key_override path
# ---------------------------------------------------------------------------

def test_select_workers_key_override_string_wrapped_in_list(mock_quota):
    mock_quota({})
    result = _select_workers("implementer", worker_count=2, key_override="KEY_Z")
    assert result == ["KEY_Z"]


def test_select_workers_key_override_list_passed_through(mock_quota):
    mock_quota({})
    result = _select_workers("implementer", worker_count=2, key_override=["KEY_Z", "KEY_Y"])
    assert result == ["KEY_Z", "KEY_Y"]


def test_select_workers_key_override_does_not_emit_event(mock_quota, mock_emit):
    """Documented deliberately: key_override is the Panel's own explicit
    choice, not a rotation decision -- no worker_pool_selection event."""
    mock_quota({})
    _select_workers("implementer", worker_count=1, key_override="KEY_Z")
    mock_emit.assert_not_called()


def test_select_workers_key_override_ignores_eligible_pool(mock_quota):
    """A key_override wins outright even if it names an account that
    isn't tagged for this role_tag at all -- the Panel already made an
    informed choice, this function doesn't second-guess it."""
    mock_quota({})
    result = _select_workers("implementer", worker_count=1, key_override="KEY_C")
    assert result == ["KEY_C"]


# ---------------------------------------------------------------------------
# _select_workers -- quota-ranked fairness rotation
# ---------------------------------------------------------------------------

def test_select_workers_picks_least_used_accounts(mock_quota):
    mock_quota({"KEY_A": 0.9, "KEY_B": 0.1, "KEY_D": 0.5})
    result = _select_workers("implementer", worker_count=2)
    assert result == ["KEY_B", "KEY_D"]


def test_select_workers_worker_count_caps_selection(mock_quota):
    mock_quota({"KEY_A": 0.1, "KEY_B": 0.2, "KEY_D": 0.3})
    result = _select_workers("implementer", worker_count=1)
    assert result == ["KEY_A"]


def test_select_workers_worker_count_larger_than_pool_returns_whole_pool(mock_quota):
    mock_quota({"KEY_A": 0.1, "KEY_B": 0.2, "KEY_D": 0.3})
    result = _select_workers("implementer", worker_count=10)
    assert sorted(result) == ["KEY_A", "KEY_B", "KEY_D"]


def test_select_workers_missing_snapshot_entry_defaults_to_zero_pct(mock_quota):
    """(snapshot.get(k) or {}).get("pct") or 0.0 -- an account with no
    snapshot entry at all (e.g. never made a call today) ranks as 0%
    used, i.e. picked FIRST, not excluded or ranked last."""
    mock_quota({"KEY_A": 0.9})  # KEY_B, KEY_D have no entry at all
    result = _select_workers("implementer", worker_count=1)
    assert result[0] in ("KEY_B", "KEY_D")


def test_select_workers_none_pct_in_snapshot_defaults_to_zero(monkeypatch):
    """A snapshot entry that exists but has pct=None (e.g. no verified
    quota figure for that provider/model) must also fall back to 0.0,
    not raise a TypeError comparing None to float."""
    monkeypatch.setattr(worker_pool, "get_quota_snapshot",
                         lambda: {"KEY_A": {"pct": None}, "KEY_B": {"pct": 0.5}, "KEY_D": {"pct": 0.2}})
    result = _select_workers("implementer", worker_count=1)
    assert result == ["KEY_A"]


def test_select_workers_empty_pool_raises(mock_quota):
    mock_quota({})
    with pytest.raises(RuntimeError, match="nonexistent_tag"):
        _select_workers("nonexistent_tag", worker_count=2)


def test_select_workers_emits_selection_event(mock_quota, mock_emit):
    mock_quota({"KEY_A": 0.9, "KEY_B": 0.1, "KEY_D": 0.5})
    _select_workers("implementer", worker_count=2, session_id="sess-1", agent_name="code_writers")
    mock_emit.assert_called_once_with(
        "worker_pool_selection", session_id="sess-1", agent="code_writers",
        payload={"role_tag": "implementer", "worker_count": 2, "pool_size": 3,
                 "selected": ["KEY_B", "KEY_D"]},
    )


def test_select_workers_role_tag_can_overlap_accounts(mock_quota):
    """KEY_B is tagged for both implementer and content_writer -- ranking
    for one tag must not be affected by the other tag it also carries."""
    mock_quota({"KEY_B": 0.05, "KEY_C": 0.95})
    result = _select_workers("content_writer", worker_count=1)
    assert result == ["KEY_B"]
