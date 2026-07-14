"""
eo/audit_log.py — Part 8.6: append-only audit trail.

Deliberately thin. chat_store.py and the memory bus already record almost
everything that happens (chat content, workspace state, message history) —
this module doesn't duplicate any of that. It exists purely to answer two
questions an admin/security review eventually needs answered that nothing
else in this system currently tracks: "what did this user do" and "what
happened to this target," with a timestamp, independent of the current
state of the thing itself (which may have since been deleted, renamed, or
transferred to someone else).

Call write_audit() at the same moments the mutating functions in
chat_workspace.py / chat_store.py already touch the database — a thin
logging call added at each existing write point, not a parallel tracking
system trying to reconstruct history after the fact (see Part 8 guide
§8.6). This module has no opinion about WHERE it's called from; it's
wired into chat_workspace.py's mutating functions as of this pass. Wiring
eo/registry.py's update_role_prompt() (the other example the guide names)
is not done in this pass — that file wasn't in scope this session.

Defensive by design: write_audit() never raises. An audit-logging failure
must never break the actual operation it's describing — same posture
eo/knowledge_graph.py already takes toward its own Vector-store failures
(degrade silently, print, move on). Losing one audit row to a transient
DB hiccup is an acceptable tradeoff against "the entire feature this audit
call was attached to just went down because logging it failed."
"""
from eo import db


def write_audit(user_id: str, action: str, target_type: str, target_id: str,
                 detail: dict | None = None) -> None:
    """Fire-and-forget. Never raises — a failed audit write must never
    take down the operation it's describing. Caller doesn't need to (and
    shouldn't) wrap this in its own try/except."""
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                insert into audit_log (user_id, action, target_type, target_id, detail)
                values (%s, %s, %s, %s, %s)
                """,
                (user_id, action, target_type, target_id, db.Json(detail or {})),
            )
    except Exception as exc:
        # Never let audit logging break the real operation it's attached to.
        print(f"  [audit_log] write failed (action={action!r}, target={target_id!r}): {exc}")


def list_for_target(target_type: str, target_id: str, limit: int = 100) -> list[dict]:
    """Everything that happened to one target (a workspace, a chat, ...),
    most recent first. No access check here — same discipline as every
    other read function in this codebase: callers (api/server.py) decide
    who's allowed to ask, this module just answers the query."""
    with db.cursor() as cur:
        cur.execute(
            """
            select id, user_id, action, target_type, target_id, detail, created_at
            from audit_log
            where target_type = %s and target_id = %s
            order by created_at desc
            limit %s
            """,
            (target_type, target_id, limit),
        )
        rows = cur.fetchall()
    return [_row_to_entry(r) for r in rows]


def list_for_user(user_id: str, limit: int = 100) -> list[dict]:
    """Everything one user did, most recent first — "what have I done"
    rather than "what happened to this thing." Distinct query, same
    table, since the two access patterns have different index needs
    (see the migration's two indexes)."""
    with db.cursor() as cur:
        cur.execute(
            """
            select id, user_id, action, target_type, target_id, detail, created_at
            from audit_log
            where user_id = %s
            order by created_at desc
            limit %s
            """,
            (user_id, limit),
        )
        rows = cur.fetchall()
    return [_row_to_entry(r) for r in rows]


def _row_to_entry(row: dict) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "action": row["action"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "detail": row["detail"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }