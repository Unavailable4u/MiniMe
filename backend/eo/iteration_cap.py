"""
eo/iteration_cap.py -- session-level attempt cap for the self-modification
experiment (guide §4). Tracks how many self-mod actions (audits, and later
Phase 2 edit attempts) have happened in this session, and refuses once a
hard cap is hit, even mid-task.

A "session" resets at local midnight, not per-process -- restarting the
script or opening a new terminal does NOT reset the count. This is
deliberate: the guide's cap is "N attempts per session," and a counter
that resets on every process start wouldn't actually cap anything.

The state file lives outside the repo (in your home directory), so it's
never accidentally committed, and a `git reset --hard` on the experiment
clone doesn't quietly reset your attempt count too.

Usage, from any self-mod script:

    from eo.iteration_cap import check_and_increment, IterationCapExceeded

    try:
        n = check_and_increment("audit")   # or "edit_attempt", your choice of label
    except IterationCapExceeded as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        sys.exit(3)
"""
from __future__ import annotations
import json
import time
from pathlib import Path

STATE_FILE = Path.home() / ".minime_self_mod_state.json"

# Start small per the guide's own advice (5-10). Raise this deliberately,
# by editing this line, once Phase 1's diagnoses have earned some trust --
# never by deleting the state file to dodge the count.
MAX_ATTEMPTS_PER_SESSION = 8


class IterationCapExceeded(Exception):
    pass


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load() -> dict:
    if not STATE_FILE.exists():
        return {"date": _today(), "count": 0, "kinds": {}}
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        # Corrupt or unreadable state file -- fail safe by starting a
        # fresh count rather than crashing every self-mod invocation.
        return {"date": _today(), "count": 0, "kinds": {}}
    if data.get("date") != _today():
        return {"date": _today(), "count": 0, "kinds": {}}
    return data


def _save(data: dict) -> None:
    STATE_FILE.write_text(json.dumps(data, indent=2))


def check_and_increment(kind: str = "attempt") -> int:
    """Call once per self-mod action, BEFORE doing the action.
    Raises IterationCapExceeded if today's session cap is already hit.
    Returns the new total count today on success."""
    data = _load()
    if data["count"] >= MAX_ATTEMPTS_PER_SESSION:
        raise IterationCapExceeded(
            f"Session cap of {MAX_ATTEMPTS_PER_SESSION} self-mod actions "
            f"already reached today ({data['date']}). Stop and review what "
            f"you've learned so far before continuing. To raise the cap, "
            f"edit MAX_ATTEMPTS_PER_SESSION in this file deliberately -- "
            f"don't delete {STATE_FILE} just to dodge the count."
        )
    data["count"] += 1
    data["kinds"][kind] = data["kinds"].get(kind, 0) + 1
    _save(data)
    return data["count"]


def current_count() -> int:
    return _load()["count"]


def remaining() -> int:
    return max(0, MAX_ATTEMPTS_PER_SESSION - current_count())
