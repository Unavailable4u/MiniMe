"""
api/routes/usage.py -- S01: the read surface over the shared usage ledger.

One route:

  GET /api/usage -- the signed-in account's credit pool for the current
  period: pool, used, remaining, when it resets, and a by-surface
  breakdown (web / m2c / menux). Credits only; users never see
  dollars. `used` is SUM(credits) over the account's ledger rows in the
  period window, so it always equals the rows -- see eo/usage_ledger.py.

Like api/routes/user_profile.py this is flat, not workspace-scoped: a
usage pool belongs to the account, not to any one project, and
require_auth already resolves straight to the account id.

Before migration 0012 is applied the underlying tables do not exist and
this route fails with the database's own error; apply the migration
first (see its header).
"""
from fastapi import APIRouter, Depends

from api.deps import require_auth
from eo import usage_ledger

router = APIRouter()


@router.get("/api/usage")
def get_usage(owner_id: str = Depends(require_auth)):
    """Always the full shape, so a usage meter can render straight from it
    with no defensive key checks."""
    return usage_ledger.usage_summary(owner_id)
