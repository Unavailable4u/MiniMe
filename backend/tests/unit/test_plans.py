"""eo/plans.py -- plan defaults and period windows (S01). No database."""
from datetime import datetime, timezone

import pytest

from eo import plans

UTC = timezone.utc


def dt(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=UTC)


def test_shipped_config_is_valid_and_ordered():
    plans.reload_config()
    assert plans.default_plan_key() == "free"
    pools = [plans.pool_for(k) for k in ("free", "basic", "moderate", "ultimate")]
    assert all(p > 0 for p in pools) and pools == sorted(pools)
    assert plans.pool_for("nope") is None
    assert plans.credits_per_usd() > 0


def test_calendar_month_window():
    assert plans.calendar_month_window(dt(2026, 10, 22, 13)) == (dt(2026, 10, 1), dt(2026, 11, 1))
    assert plans.calendar_month_window(dt(2026, 12, 31, 23)) == (dt(2026, 12, 1), dt(2027, 1, 1))


def test_add_months_clamps_and_measures_from_the_anchor():
    assert plans._add_months(dt(2026, 1, 31), 1) == dt(2026, 2, 28)
    assert plans._add_months(dt(2026, 1, 31), 2) == dt(2026, 3, 31)   # not Mar 28
    assert plans._add_months(dt(2028, 1, 31), 1) == dt(2028, 2, 29)   # leap year


def test_stored_window_is_kept_while_it_covers_now():
    s, e = dt(2026, 10, 5), dt(2026, 11, 5)
    assert plans.effective_window(s, e, dt(2026, 10, 22)) == (s, e)


def test_expired_window_rolls_forward_by_whole_months():
    s, e = dt(2026, 1, 5), dt(2026, 2, 5)
    assert plans.effective_window(s, e, dt(2026, 4, 20)) == (dt(2026, 4, 5), dt(2026, 5, 5))
    # exactly on a boundary belongs to the NEW period
    assert plans.effective_window(s, e, dt(2026, 4, 5)) == (dt(2026, 4, 5), dt(2026, 5, 5))
    assert plans.effective_window(s, e, dt(2026, 4, 4, 23)) == (dt(2026, 3, 5), dt(2026, 4, 5))


def test_rolled_window_respects_a_month_end_anchor():
    s, e = dt(2026, 1, 31), dt(2026, 2, 28)
    assert plans.effective_window(s, e, dt(2026, 3, 15)) == (dt(2026, 2, 28), dt(2026, 3, 31))


def test_naive_datetimes_are_treated_as_utc():
    naive = datetime(2026, 10, 5)
    s, e = plans.effective_window(naive, datetime(2026, 11, 5), dt(2026, 10, 22))
    assert s.tzinfo is not None and s == dt(2026, 10, 5)


def _row(**over):
    row = dict(plan_key="basic", credit_pool=1234, period_start=dt(2026, 10, 1),
               period_end=dt(2026, 11, 1), status="active")
    row.update(over)
    return row


def test_state_from_row_uses_the_rows_own_pool():
    st = plans.state_from_row("u", _row(), dt(2026, 10, 22))
    assert (st.plan_key, st.credit_pool, st.configured) == ("basic", 1234.0, True)


def test_canceled_plan_falls_back_to_the_default_pool():
    st = plans.state_from_row("u", _row(status="canceled"), dt(2026, 10, 22))
    assert st.plan_key == "free" and st.credit_pool == plans.pool_for("free")


def test_past_due_keeps_its_pool():
    st = plans.state_from_row("u", _row(status="past_due"), dt(2026, 10, 22))
    assert st.credit_pool == 1234.0


def test_default_state_for_an_account_with_no_row():
    st = plans.default_state("u", dt(2026, 10, 22))
    assert (st.plan_key, st.configured) == ("free", False)
    assert (st.period_start, st.period_end) == (dt(2026, 10, 1), dt(2026, 11, 1))
    assert st.credit_pool == plans.pool_for("free")


def test_resolve_plan_reads_the_row_through_db_cursor(monkeypatch):
    import contextlib

    class Cur:
        def execute(self, sql, params): self.params = params
        def fetchone(self): return _row(plan_key="moderate", credit_pool=7110)

    cur = Cur()

    @contextlib.contextmanager
    def fake_cursor(user_id=None, trusted=False):
        assert user_id == "u" and not trusted      # RLS identity is passed
        yield cur

    monkeypatch.setattr(plans.db, "cursor", fake_cursor)
    st = plans.resolve_plan("u", dt(2026, 10, 22))
    assert cur.params == ("u",) and st.plan_key == "moderate"


@pytest.fixture(autouse=True)
def _fresh_config():
    plans.reload_config()
    yield
    plans.reload_config()
