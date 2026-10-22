"""
eo/plans.py -- S01: which plan is an account on, how big is its credit
pool, and what period does that pool cover right now?

Two sources, in this order:

  1. The account's row in `user_plans` (migration 0012), if it has one.
     The row is authoritative for its own pool: `credit_pool` is stored
     per account so a support top-up or grant changes one account without
     changing the plan definition, and `plan_key` is only the label.
  2. config/plans.json's defaults, for an account with no row ("Free and
     unconfigured accounts follow plans.json defaults"): the default
     plan's pool over a calendar-month window (UTC).

There is no billing job in this codebase yet, so a stored period can end
without anything advancing it. Rather than letting an account's pool
silently stay "used up" forever (or reset to nothing), the effective
window is rolled forward by whole months from the stored period_start --
the billing-anniversary rule from Rulebook section 7.2 -- without writing
anything back. When billing eventually does write the new period, the
stored value and the rolled value agree.

A 'canceled' plan falls back to the default plan's pool: the account is
still usable, it is just no longer entitled to what it stopped paying
for. 'past_due' keeps its pool; whether to cut it off is a billing
decision, not something to infer here.

Reads go through eo/db.py's cursor(user_id=...), so the migration's RLS
policy (own row only) applies as defense in depth on top of the explicit
WHERE clause, same as every other store module here.
"""
import functools
import json
import os
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from eo import db

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "plans.json"


@functools.lru_cache(maxsize=1)
def _config() -> dict:
    path = Path(os.getenv("PLANS_CONFIG_PATH") or _DEFAULT_CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if cfg["default_plan"] not in cfg["plans"]:
        raise ValueError(f"plans.json default_plan {cfg['default_plan']!r} is not in plans")
    if not cfg["credits_per_usd"] > 0:
        raise ValueError("plans.json credits_per_usd must be positive")
    return cfg


def reload_config() -> None:
    """Drop the cached config (tests, or after editing plans.json in a
    long-running process)."""
    _config.cache_clear()


def default_plan_key() -> str:
    return _config()["default_plan"]


def credits_per_usd() -> float:
    return float(_config()["credits_per_usd"])


def unpriced_usd_per_million_tokens() -> float:
    return float(_config()["unpriced_usd_per_million_tokens"])


def pool_for(plan_key: str) -> "float | None":
    """The configured pool for a plan key, or None if plans.json does not
    define it (callers decide what an unknown plan means)."""
    plan = _config()["plans"].get(plan_key)
    return float(plan["credit_pool"]) if plan else None


@dataclass(frozen=True)
class PlanState:
    user_id: str
    plan_key: str
    status: str
    credit_pool: float
    period_start: datetime
    period_end: datetime
    configured: bool  # False = no user_plans row, plans.json defaults in effect


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _add_months(anchor: datetime, months: int) -> datetime:
    """`anchor` shifted by whole calendar months, always measured from the
    original anchor (Jan 31 + 1 month = Feb 28/29, Jan 31 + 2 months =
    Mar 31 -- not Mar 28) and with the day clamped to the target month's
    length."""
    index = anchor.year * 12 + (anchor.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(anchor.day, monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def calendar_month_window(now: datetime) -> "tuple[datetime, datetime]":
    """[first instant of this UTC month, first instant of the next)."""
    now = _as_utc(now)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, _add_months(start, 1)


def effective_window(period_start: datetime, period_end: datetime,
                     now: datetime) -> "tuple[datetime, datetime]":
    """The period that covers `now`. The stored window if it already does
    (or if it lies in the future); otherwise the stored window rolled
    forward by whole months from period_start until it does."""
    period_start, period_end, now = _as_utc(period_start), _as_utc(period_end), _as_utc(now)
    if now < period_end:
        return period_start, period_end
    months = (now.year - period_start.year) * 12 + (now.month - period_start.month)
    months = max(months, 1)
    while _add_months(period_start, months) > now:
        months -= 1
    while _add_months(period_start, months + 1) <= now:
        months += 1
    return _add_months(period_start, months), _add_months(period_start, months + 1)


def default_state(user_id: str, now: "datetime | None" = None) -> PlanState:
    """What an account with no user_plans row is on."""
    now = now or _utcnow()
    plan_key = default_plan_key()
    start, end = calendar_month_window(now)
    return PlanState(user_id, plan_key, "active", pool_for(plan_key), start, end, configured=False)


def state_from_row(user_id: str, row: dict, now: "datetime | None" = None) -> PlanState:
    """Pure: a user_plans row (dict with the table's columns) -> PlanState."""
    now = now or _utcnow()
    start, end = effective_window(row["period_start"], row["period_end"], now)
    status = row["status"]
    if status == "canceled":
        plan_key = default_plan_key()
        pool = pool_for(plan_key)
    else:
        plan_key = row["plan_key"]
        pool = float(row["credit_pool"])
    return PlanState(user_id, plan_key, status, pool, start, end, configured=True)


def resolve_plan(user_id: str, now: "datetime | None" = None) -> PlanState:
    """The account's current plan state. Raises on a database failure --
    callers choose their own failure posture (GET /api/usage surfaces it as
    a 503; usage_ledger.check() fails open)."""
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            "select plan_key, credit_pool, period_start, period_end, status "
            "from user_plans where user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    return state_from_row(user_id, row, now) if row else default_state(user_id, now)
