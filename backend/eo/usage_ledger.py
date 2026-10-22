"""
eo/usage_ledger.py -- S01: the shared usage ledger.

One row in `usage_ledger` (migration 0012) per real LLM call, attributed to
an account and a surface ('web', 'm2c', 'menux', or 'system' when
no account owns the call), plus the read side: how much of its credit pool
has an account used this period?

Rows hold NUMBERS AND OPAQUE IDS ONLY -- never prompt text, output text,
file paths, or error messages. The one free-text-shaped column,
`session_id`, is the same opaque task/chat id llm_call_log already
stores; `run_id` is for M2C's opaque run id.

The pieces, and who calls them:

  commit(...)        Insert one row. RAISES on failure. For callers that
                     need to know the write landed.
  record_call(...)   What utils/llm_client.py's hook calls, beside
                     _record_call_log(). Resolves who the call is for
                     (eo/usage_context.py), prices it, and calls commit().
                     FAIL-OPEN: it never raises -- a ledger problem must
                     never fail the user's call. A missing DATABASE_URL, a
                     pool timeout, or a migration that has not been applied
                     yet all degrade to one printed warning.
  check(...)         Pre-flight: would this account have credits left for
                     an estimated call? Returns a structured result
                     (status 'ok' or 'quota_exhausted') and leaves
                     presentation to the caller -- the web app and
                     M2C each decide how to show it. Nothing in
                     llm_client calls check(); S01 meters, it does not
                     block. Also fail-open.
  usage_summary(...) Pool, used, remaining, reset date and by-surface
                     breakdown for GET /api/usage.

"Used" is never a stored counter. It is SUM(credits) over the account's
rows in the current period window, computed in the same query that
produces the by-surface breakdown, so the total and the breakdown cannot
disagree with each other or with the rows.

Credits here are a deliberately simple placeholder, not S02's weighting:
cost_usd x credits_per_usd when the call is priced; tokens x the
configured unpriced rate when it is not; zero when nothing at all was
measured. See config/plans.json's _notes. S02 will replace
credits_for_call() with per-model prices including cached-read pricing;
callers should not depend on the exact numbers.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from eo import db, plans, usage_context

SURFACES = usage_context.SURFACES

# The surfaces an account's own breakdown reports. 'system' rows have no
# account (user_id NULL), so they can never appear in a user's summary.
USER_SURFACES = ("web", "m2c", "menux")

_CREDIT_DECIMALS = 6  # matches usage_ledger.credits numeric(18, 6)


def cached_tokens_from_usage(usage) -> "int | None":
    """Cached prompt tokens off a provider usage object or dict, or None if
    the provider did not report any. Tolerant of the shapes llm_client
    already handles for its other usage reads (OpenAI-compatible SDK
    objects, and Cloudflare's plain dicts). Kept here rather than added to
    llm_client's _usage_details_from_usage(), which feeds Langfuse and
    should not change shape for a billing feature."""
    if usage is None:
        return None

    def _get(obj, name):
        value = getattr(obj, name, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(name)
        return value

    details = _get(usage, "prompt_tokens_details")
    cached = _get(details, "cached_tokens") if details is not None else None
    if cached is None:
        cached = _get(usage, "cached_tokens")
    try:
        return int(cached) if cached is not None else None
    except (TypeError, ValueError):
        return None


def credits_for_call(cost_usd: "float | None", input_tokens: "int | None",
                     output_tokens: "int | None") -> float:
    """Credits to charge for one call. See the module docstring: a
    placeholder until S02. Never negative, never None."""
    if cost_usd is not None:
        return round(max(float(cost_usd), 0.0) * plans.credits_per_usd(), _CREDIT_DECIMALS)
    if input_tokens is None and output_tokens is None:
        return 0.0
    tokens = (input_tokens or 0) + (output_tokens or 0)
    usd = (tokens / 1_000_000) * plans.unpriced_usd_per_million_tokens()
    return round(usd * plans.credits_per_usd(), _CREDIT_DECIMALS)


def commit(*, user_id: "str | None", surface: str, provider: str,
           model: "str | None" = None, session_id: "str | None" = None,
           run_id: "str | None" = None, input_tokens: "int | None" = None,
           cached_input_tokens: "int | None" = None,
           output_tokens: "int | None" = None, cost_usd: "float | None" = None,
           credits: "float | None" = None) -> dict:
    """Insert one ledger row and return {"credits": float}. Raises on any
    failure, including a malformed (user_id, surface) pair -- use
    record_call() from anywhere a ledger problem must not propagate.

    `credits` defaults to credits_for_call(); pass it explicitly only if
    the caller has already priced the call (S02's weighting will).
    user_id must be None exactly when surface == 'system', which is also a
    CHECK constraint on the table.
    """
    if surface not in SURFACES:
        raise ValueError(f"surface must be one of {SURFACES}, got {surface!r}")
    if (surface == "system") != (user_id is None):
        raise ValueError("user_id must be None if and only if surface == 'system'")
    if credits is None:
        credits = credits_for_call(cost_usd, input_tokens, output_tokens)

    with db.cursor(trusted=True) as cur:
        cur.execute(
            """
            insert into usage_ledger
                (user_id, surface, session_id, run_id, provider, model,
                 input_tokens, cached_input_tokens, output_tokens, credits, cost_usd)
            values
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, surface, session_id, run_id, provider, model,
             input_tokens, cached_input_tokens, output_tokens, credits, cost_usd),
        )
    return {"credits": credits}


def record_call(*, provider: str, model: "str | None", session_id: "str | None",
                input_tokens: "int | None", output_tokens: "int | None",
                cost_usd: "float | None", usage=None,
                agent_name: str = "Agent", run_id: "str | None" = None) -> None:
    """The hook entry point. Works out who the call is for, then commits one
    row. NEVER RAISES (see the module docstring)."""
    try:
        who = usage_context.resolve(session_id)
        commit(
            user_id=who.user_id,
            surface=who.surface,
            provider=provider,
            model=model,
            session_id=who.session_id or session_id,
            run_id=run_id,
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens_from_usage(usage),
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
    except Exception as exc:
        print(f"  [{agent_name}] usage-ledger write failed (non-fatal): {exc}")


# ---------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------

def _round(value) -> float:
    return round(float(value or 0), _CREDIT_DECIMALS)


def _usage_by_surface(user_id: str, period_start: datetime, period_end: datetime) -> dict:
    """{surface: credits} for the account's rows inside [start, end). The
    single source for both the total and the breakdown."""
    with db.cursor(user_id=user_id) as cur:
        cur.execute(
            """
            select surface, coalesce(sum(credits), 0) as credits
            from usage_ledger
            where user_id = %s and created_at >= %s and created_at < %s
            group by surface
            """,
            (user_id, period_start, period_end),
        )
        rows = cur.fetchall()
    return {row["surface"]: _round(row["credits"]) for row in rows}


def usage_summary(user_id: str, now: "datetime | None" = None) -> dict:
    """What GET /api/usage returns. Credits only -- users never see dollars
    (Rulebook section 7.2). Raises on a database failure."""
    now = now or datetime.now(timezone.utc)
    plan = plans.resolve_plan(user_id, now)
    by_surface = _usage_by_surface(user_id, plan.period_start, plan.period_end)
    used = _round(sum(by_surface.values()))
    pool = _round(plan.credit_pool)
    return {
        "plan_key": plan.plan_key,
        "status": plan.status,
        "pool": pool,
        "used": used,
        "remaining": _round(max(pool - used, 0.0)),
        "period_start": plan.period_start.isoformat(),
        "resets_at": plan.period_end.isoformat(),
        "by_surface": {surface: by_surface.get(surface, 0.0) for surface in USER_SURFACES},
    }


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a pre-flight check. `status` is 'ok' or 'quota_exhausted'.
    `degraded` is True when the check could not be performed (the ledger or
    database was unavailable) and the call was allowed anyway -- fail-open,
    the same posture as record_call()."""
    status: str
    allowed: bool
    pool: "float | None" = None
    used: "float | None" = None
    remaining: "float | None" = None
    resets_at: "str | None" = None
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "status": self.status, "allowed": self.allowed, "pool": self.pool,
            "used": self.used, "remaining": self.remaining,
            "resets_at": self.resets_at, "degraded": self.degraded,
        }


def check(user_id: "str | None", estimate: float = 0.0,
          now: "datetime | None" = None) -> CheckResult:
    """Pre-flight: may this account make a call expected to cost `estimate`
    credits? Allowed iff something remains in the pool and the estimate
    fits in it. A call with no owning account (user_id None) is not
    metered against any pool and is always allowed. Never raises."""
    if user_id is None:
        return CheckResult(status="ok", allowed=True)
    try:
        summary = usage_summary(user_id, now)
    except Exception as exc:
        print(f"  [usage_ledger] check() could not read usage for {user_id!r}; "
              f"allowing the call (non-fatal): {exc}")
        return CheckResult(status="ok", allowed=True, degraded=True)

    remaining = summary["remaining"]
    allowed = remaining > 0 and max(float(estimate or 0), 0.0) <= remaining
    return CheckResult(
        status="ok" if allowed else "quota_exhausted",
        allowed=allowed,
        pool=summary["pool"],
        used=summary["used"],
        remaining=remaining,
        resets_at=summary["resets_at"],
    )
