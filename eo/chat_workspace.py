"""
eo/chat_workspace.py — named containers of chats (UI label: "Projects").
Membership in a workspace automatically keeps its chats mutually linked
for memory sharing, same mechanism as eo/memory_batch.py's _sync_members,
reused here so a workspace behaves like an always-on batch.

Deliberately separate from eo/project_registry.py, which tracks external
codebase roots for Cross-Project File Control — unrelated concept, same
word collision risk, hence the different module/table name.

--- Part 8.2 migration notes -----------------------------------------------
Migrated from data/chats/_workspaces.json to Postgres, scoped by
owner_id. Every function now takes owner_id, slotted in right after any
existing identifier argument — same convention as chat_store.py.

Storage-shape note: the old JSON kept chat_ids as a list INSIDE each
workspace record. The Postgres schema instead puts workspace_id as a
column ON the chats table (see part8_schema.sql) — a workspace's
chat_ids is computed by querying which chats point at it, rather than
stored redundantly on the workspace row.
-----------------------------------------------------------------------------

--- Part 8.3 collaborator-sharing notes -------------------------------------
A workspace's chat_ids (as returned to any caller) NEVER includes a chat
that is_private=true, unless the caller is that chat's own owner_id —
this is what makes "private chat inside a shared workspace" work.
-----------------------------------------------------------------------------

--- Part 8.4 role hierarchy, owner voting, and attribution ------------------
FIVE roles now, not two. Low to high: viewer < editor < moderator <
partner <= owner. Owner and partner are functionally equal in power,
with one difference: "owner" is a single, named slot (workspaces.owner_id)
while "partner" is a workspace_members row — a workspace can have zero
or many partners but at most one owner at any moment, and can have NO
owner at all (see "joint state" below).

    viewer:     read only.
    editor:     read + edit content (rename workspace, add/remove chats
                they themselves own into/out of the workspace).
    moderator:  everything editor can, PLUS manage membership for
                viewer/editor/moderator-tier people (invite, change role,
                remove). Cannot touch a partner or the owner. Cannot
                toggle workspace-wide attribution visibility UNLESS the
                per-member can_toggle_attribution flag is set on them —
                that flag is granted only by an owner or partner.
    partner:    everything moderator can, PLUS manage other partners
                (add, remove), PLUS can forcibly remove the current
                owner (see remove_owner), PLUS always sees and can
                toggle attribution regardless of any flag.
    owner:      same power as partner while the slot is filled, and is
                the one identity workspaces.owner_id points to.

OWNERSHIP IS NOT PERMANENT. Two ways a workspace loses its owner:

    1. Forced removal — ANY partner calls remove_owner(). The current
       owner loses all access to the workspace outright (owner was
       never a workspace_members row, so nulling owner_id IS full
       removal — there's no membership row left over to demote them
       into). They get no successor-naming chance; that's the whole
       point of "forced." workspaces.owner_id becomes NULL — this is
       "joint state."
    2. Voluntary exit — the owner calls leave_workspace(). If they name
       a successor (must be a current partner), that partner becomes
       owner directly, no vote needed, and — same logic as forced
       removal — the outgoing owner has no membership row to keep, so
       they leave entirely. If they name no successor, owner_id also
       becomes NULL (joint state), and they still leave entirely.

JOINT STATE (owner_id IS NULL): every partner has full owner-equivalent
rights — there's no functional difference between "owner" and "partner"
in this state except that member_role() reports 'partner', not 'owner',
for everyone. The workspace stays in joint state indefinitely; there is
no forced resolution. A single owner only re-emerges if a vote resolves
one (see cast_vote).

VOTING: only meaningful in joint state (cast_vote raises if the
workspace currently has an owner — there's nothing to vote on). Only
partners vote, one vote each (re-casting overwrites their previous
vote, never accumulates). A vote is for a specific partner's user_id,
or for keeping things joint (vote_target = None). After EVERY cast, the
tally is checked immediately: if any specific candidate has a strict
majority of the CURRENT TOTAL PARTNER COUNT (not just of votes cast so
far), that candidate becomes owner immediately, their membership row is
removed (owner isn't tracked as a member), and every open ballot for
this workspace is cleared. A "joint" majority does nothing (joint is
already the resting state) — voting simply stays open so opinions can
keep shifting. There is no quorum deadline; the workspace runs jointly,
correctly, for as long as no candidate reaches majority.

ATTRIBUTION: every message a user sends carries an author_id (added by
the API layer into the message payload — chat_store.py's message
storage is untouched, this is purely a key inside the existing jsonb
message dict). workspaces.show_attribution controls whether
viewer/editor-tier collaborators are shown author_id on messages,
filtered at the API layer, not here. Owner/partner/moderator always see
it. Only owner/partner can flip show_attribution by default; a specific
moderator can too only if granted via can_toggle_attribution.

Deliberately NOT built in this pass: transferring a PRIVATE chat's
ownership as part of an owner/partner leaving — a departing owner's
own private chats stay theirs (they still own the chat_id row even
after losing workspace access; they just can no longer see it grouped
under this workspace's chat_ids list, same as any removed member).
Flagged here rather than silently decided.
-----------------------------------------------------------------------------
"""
import uuid
import json
from datetime import datetime, timezone
from eo import db
from eo import chat_store
from eo.audit_log import write_audit



class WorkspaceAccessError(PermissionError):
    """Raised when the acting user has some access to a workspace (so it's
    not a 404) but not enough for the attempted operation. Callers
    (api/server.py) map this to HTTP 403."""
    pass

_STAGE_SEQUENCE = ["note", "research", "plan", "build", "test", "growth"]


def _next_stage(current: str) -> str | None:
    try:
        idx = _STAGE_SEQUENCE.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(_STAGE_SEQUENCE):
        return None
    return _STAGE_SEQUENCE[idx + 1]


def promote(ws_id: str, user_id: str, to_stage: str | None = None, mode: str = "complete") -> dict:
    """Advances a workspace along the fixed stage sequence
    (note -> research -> plan -> build -> test -> growth).
    Defaults to the immediate successor, but callers may explicitly
    choose any later stage in the same sequence. Requires edit-tier+
    access, same bar as rename_workspace (a stage move is a content
    edit, not a membership/ownership action).

    mode="complete" (default): today's unchanged behavior — the
    workspace leaves its old tab entirely. active_stages becomes
    [to_stage] and stage (primary) moves to to_stage.

    mode="partial": the workspace becomes active in to_stage's tab
    WHILE remaining active wherever it already was. to_stage is
    appended to active_stages; stage (primary) does not move, so
    _next_stage()'s default-promote target and stage_history's
    "primary" line are unaffected. A workspace can't be partially
    promoted into a stage it's already active in.
    """
    if mode not in ("complete", "partial"):
        raise ValueError(f"unknown promote mode {mode!r} — must be 'complete' or 'partial'")
    _require_edit_access(ws_id, user_id)
    with db.cursor() as cur:
        cur.execute("select stage, stage_history, active_stages from workspaces where id = %s", (ws_id,))
        row = cur.fetchone()
        if not row:
            raise FileNotFoundError(ws_id)
        current_stage = row["stage"]
        current_active = row.get("active_stages") or [current_stage]
        expected = _next_stage(current_stage)
        if expected is None:
            raise ValueError(f"workspace {ws_id} is already at its final stage ({current_stage!r})")
        if to_stage is None:
            to_stage = expected
        else:
            try:
                current_idx = _STAGE_SEQUENCE.index(current_stage)
                target_idx = _STAGE_SEQUENCE.index(to_stage)
            except ValueError:
                raise ValueError(f"unknown workspace stage {to_stage!r}")
            if target_idx <= current_idx:
                raise ValueError(
                    f"cannot promote workspace {ws_id} from {current_stage!r} to {to_stage!r} — "
                    f"the target stage must be later in the sequence"
                )
        if to_stage not in _STAGE_SEQUENCE:
            raise ValueError(
                f"cannot promote workspace {ws_id} from {current_stage!r} to {to_stage!r} — "
                f"the only valid target stages are: {', '.join(_STAGE_SEQUENCE[_STAGE_SEQUENCE.index(current_stage) + 1:])}"
            )
        if to_stage in current_active:
            raise ValueError(
                f"workspace {ws_id} is already active in {to_stage!r} — "
                f"it can't be promoted (complete or partial) into a tab it's already active in"
            )
        if mode == "complete":
            new_active = [to_stage]
            new_primary = to_stage
        else:  # partial
            new_active = current_active + [to_stage]
            new_primary = current_stage
        history = (row["stage_history"] or []) + [
            {"from": current_stage, "to": to_stage, "at": _iso(_now()), "by": user_id, "mode": mode}
        ]
        cur.execute(
            "update workspaces set stage = %s, active_stages = %s, stage_history = %s, updated_at = %s where id = %s",
            (new_primary, json.dumps(new_active), json.dumps(history), _now(), ws_id),
        )
    write_audit(user_id, "workspace.promote", "workspace", ws_id,
                {"from": current_stage, "to": to_stage, "mode": mode})
    return get_workspace(ws_id, user_id)

_VALID_ROLES = ("viewer", "editor", "moderator", "partner")
_ROLE_RANK = {"viewer": 0, "editor": 1, "moderator": 2, "partner": 3, "owner": 3}


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat() if value is not None else None


def _row_to_workspace(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "owner_id": row.get("owner_id"),
        "is_joint": row.get("owner_id") is None,
        "show_attribution": row.get("show_attribution", True),
        "stage": row.get("stage", "note"),
        "active_stages": row.get("active_stages") or [row.get("stage", "note")],
        "stage_history": row.get("stage_history") or [],
        "chat_ids": row.get("chat_ids") or [],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _chat_ids_sql():
    """A workspace's chat_ids: every chat pointing at it, EXCEPT another
    user's private chat. %s placeholders (in order): viewer_id, viewer_id
    again."""
    return """
        coalesce(array_agg(c.id) filter (
            where c.id is not null
              and (c.is_private = false or c.owner_id = %s)
        ), '{}') as chat_ids
    """


# --- role resolution ---------------------------------------------------

def member_role(ws_id: str, user_id: str) -> str | None:
    """Returns 'owner', 'partner', 'moderator', 'editor', 'viewer', or
    None (no access at all). Single source of truth for workspace
    access — every other function in this module calls this first."""
    with db.cursor() as cur:
        cur.execute("select owner_id from workspaces where id = %s", (ws_id,))
        ws = cur.fetchone()
        if not ws:
            return None
        if ws["owner_id"] is not None and ws["owner_id"] == user_id:
            return "owner"
        cur.execute(
            "select role from workspace_members where workspace_id = %s and user_id = %s",
            (ws_id, user_id),
        )
        member = cur.fetchone()
        return member["role"] if member else None


def _rank(role: str | None) -> int:
    return _ROLE_RANK.get(role, -1)


def _require_access(ws_id: str, user_id: str) -> str:
    role = member_role(ws_id, user_id)
    if role is None:
        raise FileNotFoundError(ws_id)
    return role


def _require_edit_access(ws_id: str, user_id: str) -> str:
    """editor tier or above — content edits (rename, add/remove own chats)."""
    role = _require_access(ws_id, user_id)
    if _rank(role) < _rank("editor"):
        raise WorkspaceAccessError(f"user {user_id} has viewer-only access to workspace {ws_id}")
    return role


def _require_membership_manage_access(ws_id: str, user_id: str) -> str:
    """moderator tier or above — can manage viewer/editor/moderator members."""
    role = _require_access(ws_id, user_id)
    if _rank(role) < _rank("moderator"):
        raise WorkspaceAccessError(
            f"user {user_id} does not have membership-management access to workspace {ws_id}"
        )
    return role


def _require_owner_or_partner(ws_id: str, user_id: str) -> str:
    """partner tier or above — the only tier that can touch other
    partners, remove/replace the owner, or toggle attribution by
    default."""
    role = _require_access(ws_id, user_id)
    if role not in ("owner", "partner"):
        raise WorkspaceAccessError(
            f"user {user_id} must be an owner or partner of workspace {ws_id} for this action"
        )
    return role


def _sync(chat_ids: list, owner_id: str):
    """Every member links to every OTHER member — mutual linking for
    memory sharing, unchanged logic from before."""
    for cid in chat_ids:
        others = [m for m in chat_ids if m != cid]
        chat_store.set_linked_chats(cid, owner_id, others)


def _sync_by_owner(chat_ids: list):
    """Groups chat_ids by their real owner_id and mutually links each
    owner's own subset."""
    if not chat_ids:
        return
    with db.cursor() as cur:
        cur.execute("select id, owner_id from chats where id = any(%s)", (chat_ids,))
        rows = cur.fetchall()
    by_owner: dict[str, list[str]] = {}
    for r in rows:
        by_owner.setdefault(r["owner_id"], []).append(r["id"])
    for owner_id, ids in by_owner.items():
        _sync(ids, owner_id)


# --- workspace CRUD ------------------------------------------------------

def list_workspaces(user_id: str) -> list:
    """Every workspace this user can see: owned, or where they're a
    member (any of the four workspace_members roles)."""
    with db.cursor() as cur:
        cur.execute(
            f"""
            select w.id, w.name, w.owner_id, w.show_attribution, w.stage, w.active_stages, w.stage_history, w.created_at, w.updated_at,
                   {_chat_ids_sql()}
            from workspaces w
            left join chats c on c.workspace_id = w.id
            where w.owner_id = %s
               or w.id in (select workspace_id from workspace_members where user_id = %s)
            group by w.id
            order by w.updated_at desc
            """,
            (user_id, user_id, user_id),
        )
        rows = cur.fetchall()
    return [_row_to_workspace(r) for r in rows]


def get_workspace(ws_id: str, user_id: str) -> dict:
    _require_access(ws_id, user_id)  # raises FileNotFoundError if no access at all
    with db.cursor() as cur:
        cur.execute(
            f"""
            select w.id, w.name, w.owner_id, w.show_attribution, w.stage, w.active_stages, w.stage_history, w.created_at, w.updated_at,
                   {_chat_ids_sql()}
            from workspaces w
            left join chats c on c.workspace_id = w.id
            where w.id = %s
            group by w.id
            """,
            (user_id, ws_id),
        )
        row = cur.fetchone()
    if not row:
        raise FileNotFoundError(ws_id)
    return _row_to_workspace(row)


def create_workspace(owner_id: str, name: str, stage: str = "note") -> dict:
    # NEW — item #10 / B0: previously this always inserted with no stage
    # column at all, so every native-created workspace silently defaulted
    # to "note" regardless of which tab created it (e.g. a workspace
    # created from Research would show up under Notebooks instead). Any
    # tab that wants to create its own project now passes its stage in.
    ws_id = f"ws_{uuid.uuid4().hex[:10]}"
    clean_name = name.strip() or "Untitled project"
    clean_stage = stage if stage in _STAGE_SEQUENCE else "note"
    with db.cursor() as cur:
        cur.execute(
            "insert into workspaces (id, name, owner_id, stage, active_stages) values (%s, %s, %s, %s, %s) "
            "returning id, name, owner_id, show_attribution, stage, active_stages, created_at, updated_at",
            (ws_id, clean_name, owner_id, clean_stage, [clean_stage]),
        )
        row = cur.fetchone()
    row = dict(row)
    row["chat_ids"] = []
    write_audit(owner_id, "workspace.create", "workspace", ws_id, {"name": clean_name, "stage": clean_stage})
    return _row_to_workspace(row)


def rename_workspace(ws_id: str, user_id: str, name: str) -> dict:
    _require_edit_access(ws_id, user_id)  # editor tier or above
    clean_name = name.strip()[:80]
    with db.cursor() as cur:
        if clean_name:
            cur.execute(
                "update workspaces set name = %s, updated_at = %s where id = %s returning id",
                (clean_name, _now(), ws_id),
            )
        else:
            cur.execute(
                "update workspaces set updated_at = %s where id = %s returning id",
                (_now(), ws_id),
            )
        if not cur.fetchone():
            raise FileNotFoundError(ws_id)
    write_audit(user_id, "workspace.rename", "workspace", ws_id, {"name": clean_name})
    return get_workspace(ws_id, user_id)


def add_chat(ws_id: str, user_id: str, chat_id: str) -> dict:
    """user_id must have edit access to the WORKSPACE, and must OWN the
    chat_id being added."""
    _require_edit_access(ws_id, user_id)
    with db.cursor() as cur:
        cur.execute(
            "update chats set workspace_id = %s, updated_at = %s where id = %s and owner_id = %s",
            (ws_id, _now(), chat_id, user_id),
        )
        cur.execute("update workspaces set updated_at = %s where id = %s", (_now(), ws_id))

    ws = get_workspace(ws_id, user_id)
    _sync_by_owner(ws["chat_ids"])
    write_audit(user_id, "workspace.chat_add", "workspace", ws_id, {"chat_id": chat_id})
    return ws


def create_chat_in_workspace(ws_id: str, user_id: str, title: str = "New Chat") -> dict:
    """One-step create+attach — see add_chat's docstring for the access
    rules this reuses unchanged."""
    _require_edit_access(ws_id, user_id)
    chat = chat_store.create_chat(user_id, title=title)
    return add_chat(ws_id, user_id, chat["id"])


def remove_chat(ws_id: str, user_id: str, chat_id: str, delete_chat: bool = False) -> dict:
    _require_edit_access(ws_id, user_id)

    with db.cursor() as cur:
        if not delete_chat:
            cur.execute(
                "update chats set workspace_id = null, updated_at = %s "
                "where id = %s and workspace_id = %s",
                (_now(), chat_id, ws_id),
            )
        cur.execute("update workspaces set updated_at = %s where id = %s", (_now(), ws_id))

    if delete_chat:
        chat_store.delete_chat(chat_id, user_id)  # no-ops silently if user_id doesn't own it
    elif chat_store.chat_exists(chat_id, user_id):
        chat_store.set_linked_chats(chat_id, user_id, [])

    ws = get_workspace(ws_id, user_id)
    _sync_by_owner(ws["chat_ids"])
    write_audit(user_id, "workspace.chat_remove", "workspace", ws_id,
                {"chat_id": chat_id, "deleted": delete_chat})
    return ws


def delete_workspace(ws_id: str, user_id: str) -> None:
    """Owner OR partner can delete — both tiers are full-power. A
    moderator cannot, regardless of how long they've been trusted with
    membership management."""
    role = _require_owner_or_partner(ws_id, user_id)
    ws = get_workspace(ws_id, user_id)
    with db.cursor() as cur:
        cur.execute("delete from workspaces where id = %s", (ws_id,))
    write_audit(user_id, "workspace.delete", "workspace", ws_id, {"name": ws["name"]})
    for cid in ws["chat_ids"]:
        if chat_store.chat_exists(cid, user_id):
            chat_store.set_linked_chats(cid, user_id, [])


def workspace_for_chat(chat_id: str, owner_id: str) -> dict | None:
    with db.cursor() as cur:
        cur.execute(
            "select workspace_id from chats where id = %s and owner_id = %s",
            (chat_id, owner_id),
        )
        row = cur.fetchone()
    if not row or not row["workspace_id"]:
        return None
    try:
        return get_workspace(row["workspace_id"], owner_id)
    except FileNotFoundError:
        return None


# --- membership management (moderator+ for viewer/editor/moderator,
#     owner/partner-only for partner-tier changes) --------------------------

def list_notify_targets(ws_id: str) -> list[str]:
    """Part 8.4: every user_id who should hear about a workspace-wide
    event (e.g. the note-taking agent proposing a note) — the owner plus
    every workspace_members row. Deliberately auth-free, unlike every
    other function in this module: this is called from internal system
    code reacting to an agent's own action, not in response to a
    specific user's request, so there's no actor_id to check access
    against. Never call this from an API route — routes always have a
    real caller to scope to; use list_members(ws_id, user_id) there."""
    with db.cursor() as cur:
        cur.execute("select owner_id from workspaces where id = %s", (ws_id,))
        row = cur.fetchone()
        if not row:
            return []
        targets = {row["owner_id"]}
        cur.execute(
            "select user_id from workspace_members where workspace_id = %s",
            (ws_id,),
        )
        targets.update(r["user_id"] for r in cur.fetchall())
    return [t for t in targets if t]


def list_members(ws_id: str, user_id: str) -> list:
    """Any member (viewer and up) can see the roster — unlike Part 8.3,
    this is no longer owner-only, since a shared workspace with a real
    hierarchy benefits from everyone knowing who else has access. Admin
    actions (add/change/remove) are still gated separately below."""
    _require_access(ws_id, user_id)
    with db.cursor() as cur:
        cur.execute(
            "select user_id, role, can_toggle_attribution, added_at from workspace_members "
            "where workspace_id = %s order by added_at",
            (ws_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "user_id": r["user_id"],
            "role": r["role"],
            "can_toggle_attribution": r["can_toggle_attribution"],
            "added_at": _iso(r["added_at"]),
        }
        for r in rows
    ]


def add_member(ws_id: str, actor_id: str, target_user_id: str, role: str = "viewer") -> dict:
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {_VALID_ROLES}, got {role!r}")
    actor_role = _require_membership_manage_access(ws_id, actor_id)  # moderator+
    if role == "partner" and actor_role not in ("owner", "partner"):
        raise WorkspaceAccessError(
            f"user {actor_id} (role={actor_role!r}) cannot grant partner-tier access"
        )
    with db.cursor() as cur:
        cur.execute("select owner_id from workspaces where id = %s", (ws_id,))
        ws = cur.fetchone()
        if not ws:
            raise FileNotFoundError(ws_id)
        if ws["owner_id"] == target_user_id:
            raise ValueError("workspace owner cannot also be added as a member")
        cur.execute(
            """
            insert into workspace_members (workspace_id, user_id, role)
            values (%s, %s, %s)
            on conflict (workspace_id, user_id) do update set role = excluded.role
            returning user_id, role, can_toggle_attribution, added_at
            """,
            (ws_id, target_user_id, role),
        )
        row = cur.fetchone()
    write_audit(actor_id, "workspace.member_add", "workspace", ws_id,
                {"target_user_id": target_user_id, "role": role})
    return {
        "user_id": row["user_id"],
        "role": row["role"],
        "can_toggle_attribution": row["can_toggle_attribution"],
        "added_at": _iso(row["added_at"]),
    }


def update_member_role(ws_id: str, actor_id: str, target_user_id: str, role: str) -> dict:
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {_VALID_ROLES}, got {role!r}")
    actor_role = _require_membership_manage_access(ws_id, actor_id)  # moderator+

    with db.cursor() as cur:
        cur.execute(
            "select role from workspace_members where workspace_id = %s and user_id = %s",
            (ws_id, target_user_id),
        )
        current = cur.fetchone()
        if not current:
            raise FileNotFoundError(f"{target_user_id} is not a member of workspace {ws_id}")

        touching_partner_tier = role == "partner" or current["role"] == "partner"
        if touching_partner_tier and actor_role not in ("owner", "partner"):
            raise WorkspaceAccessError(
                f"user {actor_id} (role={actor_role!r}) cannot change partner-tier membership"
            )

        cur.execute(
            "update workspace_members set role = %s where workspace_id = %s and user_id = %s "
            "returning user_id, role, can_toggle_attribution, added_at",
            (role, ws_id, target_user_id),
        )
        row = cur.fetchone()
    write_audit(actor_id, "workspace.member_role_change", "workspace", ws_id,
                {"target_user_id": target_user_id, "previous_role": current["role"], "new_role": role})
    return {
        "user_id": row["user_id"],
        "role": row["role"],
        "can_toggle_attribution": row["can_toggle_attribution"],
        "added_at": _iso(row["added_at"]),
    }


def remove_member(ws_id: str, actor_id: str, target_user_id: str) -> None:
    """Removes a viewer/editor/moderator/partner member. NOT for the
    owner — the owner isn't a workspace_members row at all; use
    remove_owner() for that, which has a genuinely different meaning
    (forced removal, no successor choice) and different eligibility
    (partner-only, not moderator)."""
    actor_role = _require_membership_manage_access(ws_id, actor_id)  # moderator+

    with db.cursor() as cur:
        cur.execute(
            "select role from workspace_members where workspace_id = %s and user_id = %s",
            (ws_id, target_user_id),
        )
        target = cur.fetchone()
        if not target:
            raise FileNotFoundError(f"{target_user_id} is not a member of workspace {ws_id}")

        if target["role"] == "partner" and actor_role not in ("owner", "partner"):
            raise WorkspaceAccessError(
                f"user {actor_id} (role={actor_role!r}) cannot remove a partner"
            )

        cur.execute(
            "delete from workspace_members where workspace_id = %s and user_id = %s",
            (ws_id, target_user_id),
        )
    write_audit(actor_id, "workspace.member_remove", "workspace", ws_id,
                {"target_user_id": target_user_id, "previous_role": target["role"]})


# --- Part 8.4: owner transitions -----------------------------------------

def remove_owner(ws_id: str, partner_id: str) -> dict:
    """Forced removal: any partner can eject the current owner. The
    outgoing owner gets no successor choice — that's the defining
    difference from leave_workspace(). Since the owner was never a
    workspace_members row, nulling owner_id IS their complete removal;
    there's no leftover row to demote them into. Puts the workspace
    into joint state and clears any stale open ballot so voting starts
    fresh."""
    role = member_role(ws_id, partner_id)
    if role != "partner":
        raise WorkspaceAccessError(
            f"user {partner_id} must be a partner to force-remove the owner "
            f"(the owner cannot be removed by a moderator, and the owner "
            f"cannot remove themself this way — see leave_workspace)"
        )
    with db.cursor() as cur:
        cur.execute("select owner_id from workspaces where id = %s", (ws_id,))
        ws = cur.fetchone()
        if not ws:
            raise FileNotFoundError(ws_id)
        if ws["owner_id"] is None:
            raise ValueError(f"workspace {ws_id} already has no owner (already joint)")
        cur.execute(
            "update workspaces set owner_id = null, updated_at = %s where id = %s",
            (_now(), ws_id),
        )
        cur.execute("delete from workspace_owner_votes where workspace_id = %s", (ws_id,))
    write_audit(partner_id, "workspace.owner_force_removed", "workspace", ws_id,
                {"removed_owner_id": ws["owner_id"]})
    return get_workspace(ws_id, partner_id)


def leave_workspace(ws_id: str, user_id: str, successor_id: str | None = None) -> dict | None:
    """Any member can leave voluntarily. For the OWNER specifically:
      - naming a successor (must be a current partner) hands ownership
        to them directly, no vote — the successor's membership row is
        removed since owner isn't tracked as a member.
      - naming no successor puts the workspace into joint state
        (owner_id -> NULL), same as a forced removal, except the owner
        chose it.
    Either way the outgoing owner has no membership row to clean up —
    they leave with zero remaining access, same as remove_owner.

    For a non-owner member (partner/moderator/editor/viewer): just
    deletes their own membership row. Always allowed — nobody needs
    permission to remove themselves.

    Returns the updated workspace dict, or None if the leaving user had
    no remaining relationship to look it up by (kept simple: we always
    have access to look it up via the pre-leave role, so this just
    returns the fresh state for whoever's left)."""
    role = member_role(ws_id, user_id)
    if role is None:
        raise FileNotFoundError(ws_id)

    if role == "owner":
        with db.cursor() as cur:
            if successor_id:
                succ_role = member_role(ws_id, successor_id)
                if succ_role != "partner":
                    raise ValueError(
                        f"successor {successor_id} must be a current partner of workspace {ws_id}"
                    )
                cur.execute(
                    "update workspaces set owner_id = %s, updated_at = %s where id = %s",
                    (successor_id, _now(), ws_id),
                )
                cur.execute(
                    "delete from workspace_members where workspace_id = %s and user_id = %s",
                    (ws_id, successor_id),
                )
            else:
                cur.execute(
                    "update workspaces set owner_id = null, updated_at = %s where id = %s",
                    (_now(), ws_id),
                )
            cur.execute("delete from workspace_owner_votes where workspace_id = %s", (ws_id,))
        write_audit(user_id, "workspace.owner_left", "workspace", ws_id,
                    {"successor_id": successor_id})
        # the outgoing owner has nothing left to fetch the workspace with —
        # nothing to return on their behalf.
        return None

    with db.cursor() as cur:
        cur.execute(
            "delete from workspace_members where workspace_id = %s and user_id = %s",
            (ws_id, user_id),
        )
    write_audit(user_id, "workspace.member_left", "workspace", ws_id, {"previous_role": role})
    return None


# --- Part 8.4: owner voting (joint-state only) ----------------------------

def get_vote_status(ws_id: str, user_id: str) -> dict:
    """Any member can see the current ballot — transparency about who's
    voted for whom is the point, not a secret."""
    _require_access(ws_id, user_id)
    with db.cursor() as cur:
        cur.execute("select owner_id from workspaces where id = %s", (ws_id,))
        ws = cur.fetchone()
        if not ws:
            raise FileNotFoundError(ws_id)
        cur.execute(
            "select count(*) as n from workspace_members where workspace_id = %s and role = 'partner'",
            (ws_id,),
        )
        total_partners = cur.fetchone()["n"]
        cur.execute(
            "select voter_id, vote_target, cast_at from workspace_owner_votes "
            "where workspace_id = %s order by cast_at",
            (ws_id,),
        )
        votes = cur.fetchall()
    return {
        "workspace_id": ws_id,
        "is_joint": ws["owner_id"] is None,
        "total_partners": total_partners,
        "votes": [
            {"voter_id": v["voter_id"], "vote_target": v["vote_target"], "cast_at": _iso(v["cast_at"])}
            for v in votes
        ],
    }


def cast_vote(ws_id: str, voter_id: str, vote_target: str | None) -> dict:
    """voter_id must be a current partner. vote_target is another
    partner's user_id, or None for 'stay joint.' Only meaningful while
    the workspace has no owner — raises if it already has one.

    After recording the vote, tallies immediately: any candidate with a
    strict majority of the CURRENT TOTAL PARTNER COUNT wins on the
    spot, becomes owner, loses their membership row (owner isn't a
    member row), and the ballot is cleared. A 'joint' majority is a
    no-op — joint is already the resting state, so voting just stays
    open for opinions to keep shifting."""
    role = member_role(ws_id, voter_id)
    if role != "partner":
        raise WorkspaceAccessError(f"user {voter_id} must be a partner of workspace {ws_id} to vote")

    with db.cursor() as cur:
        cur.execute("select owner_id from workspaces where id = %s", (ws_id,))
        ws = cur.fetchone()
        if not ws:
            raise FileNotFoundError(ws_id)
        if ws["owner_id"] is not None:
            raise ValueError(f"workspace {ws_id} already has an owner — nothing to vote on")

        if vote_target is not None:
            target_role = member_role(ws_id, vote_target)
            if target_role != "partner":
                raise ValueError(f"vote_target {vote_target} must be a current partner")

        cur.execute(
            """
            insert into workspace_owner_votes (workspace_id, voter_id, vote_target, cast_at)
            values (%s, %s, %s, %s)
            on conflict (workspace_id, voter_id)
            do update set vote_target = excluded.vote_target, cast_at = excluded.cast_at
            """,
            (ws_id, voter_id, vote_target, _now()),
        )

        cur.execute(
            "select count(*) as n from workspace_members where workspace_id = %s and role = 'partner'",
            (ws_id,),
        )
        total_partners = cur.fetchone()["n"]

        cur.execute(
            "select vote_target, count(*) as n from workspace_owner_votes "
            "where workspace_id = %s and vote_target is not null "
            "group by vote_target order by n desc limit 1",
            (ws_id,),
        )
        top = cur.fetchone()

        if top and total_partners > 0 and top["n"] * 2 > total_partners:
            winner = top["vote_target"]
            cur.execute(
                "update workspaces set owner_id = %s, updated_at = %s where id = %s",
                (winner, _now(), ws_id),
            )
            cur.execute(
                "delete from workspace_members where workspace_id = %s and user_id = %s",
                (ws_id, winner),
            )
            cur.execute("delete from workspace_owner_votes where workspace_id = %s", (ws_id,))
            write_audit(voter_id, "workspace.owner_elected", "workspace", ws_id,
                        {"winner": winner, "votes_for_winner": top["n"], "total_partners": total_partners})

    return get_vote_status(ws_id, voter_id)


# --- Part 8.4: attribution -------------------------------------------------

def set_show_attribution(ws_id: str, actor_id: str, show: bool) -> dict:
    """Owner/partner always can. A moderator can only if their
    workspace_members row has can_toggle_attribution = true."""
    role = member_role(ws_id, actor_id)
    if role is None:
        raise FileNotFoundError(ws_id)
    if role not in ("owner", "partner"):
        if role != "moderator":
            raise WorkspaceAccessError(f"user {actor_id} cannot toggle attribution visibility")
        with db.cursor() as cur:
            cur.execute(
                "select can_toggle_attribution from workspace_members "
                "where workspace_id = %s and user_id = %s",
                (ws_id, actor_id),
            )
            row = cur.fetchone()
        if not row or not row["can_toggle_attribution"]:
            raise WorkspaceAccessError(
                f"moderator {actor_id} has not been granted attribution-toggle rights"
            )
    with db.cursor() as cur:
        cur.execute(
            "update workspaces set show_attribution = %s, updated_at = %s where id = %s",
            (show, _now(), ws_id),
        )
    write_audit(actor_id, "workspace.attribution_toggle", "workspace", ws_id, {"show": show})
    return get_workspace(ws_id, actor_id)


def set_moderator_attribution_grant(ws_id: str, actor_id: str, moderator_user_id: str, can_toggle: bool) -> dict:
    """Owner/partner only — grants or revokes a specific moderator's
    right to toggle attribution visibility."""
    _require_owner_or_partner(ws_id, actor_id)
    with db.cursor() as cur:
        cur.execute(
            "select role from workspace_members where workspace_id = %s and user_id = %s",
            (ws_id, moderator_user_id),
        )
        target = cur.fetchone()
        if not target:
            raise FileNotFoundError(f"{moderator_user_id} is not a member of workspace {ws_id}")
        if target["role"] != "moderator":
            raise ValueError(
                f"{moderator_user_id} is role={target['role']!r}, not moderator — "
                f"this grant only applies to moderators (partners/owners already always can)"
            )
        cur.execute(
            "update workspace_members set can_toggle_attribution = %s "
            "where workspace_id = %s and user_id = %s "
            "returning user_id, role, can_toggle_attribution, added_at",
            (can_toggle, ws_id, moderator_user_id),
        )
        row = cur.fetchone()
    write_audit(actor_id, "workspace.attribution_grant", "workspace", ws_id,
                {"moderator_user_id": moderator_user_id, "can_toggle": can_toggle})
    return {
        "user_id": row["user_id"],
        "role": row["role"],
        "can_toggle_attribution": row["can_toggle_attribution"],
        "added_at": _iso(row["added_at"]),
    }


def can_see_attribution(ws_id: str, user_id: str) -> bool:
    """Used by api/server.py's get_chat route to decide whether to
    strip author_id from messages before returning them. Owner/partner/
    moderator: always true. viewer/editor: true only if the
    workspace's show_attribution flag is on."""
    role = member_role(ws_id, user_id)
    if role in ("owner", "partner", "moderator"):
        return True
    if role in ("viewer", "editor"):
        with db.cursor() as cur:
            cur.execute("select show_attribution from workspaces where id = %s", (ws_id,))
            row = cur.fetchone()
        return bool(row and row["show_attribution"])
    return False


# --- Part 8.7: backup / restore --------------------------------------------

def export_workspace_data(ws_id: str, user_id: str) -> dict:
    """Any current member can export — this is 'give me a portable copy
    of MY data in this workspace,' not an admin-only action like the
    audit log. Correctly scoped by owner_id even though ws['chat_ids']
    may include collaborators' chats too (chat_store.export_chats()
    filters to user_id internally, same discipline as every other
    owner_id-scoped read in this codebase) — a user's export must never
    leak a collaborator's chat content."""
    ws = get_workspace(ws_id, user_id)  # raises FileNotFoundError if no access
    role = member_role(ws_id, user_id)
    chats = chat_store.export_chats(user_id, ws["chat_ids"])
    manifest = {
        "export_version": 1,
        "exported_at": _iso(_now()),
        "exported_by": user_id,
        "workspace": {
            "id": ws["id"],
            "name": ws["name"],
            "your_role": role,
        },
        "chats": chats,
    }
    write_audit(user_id, "workspace.export", "workspace", ws_id,
                {"chat_count": len(chats)})
    return manifest


def import_workspace_data(ws_id: str, user_id: str, manifest: dict) -> dict:
    """Restores a previously-exported manifest's chats as brand-new
    chats owned by the CALLER (not whoever originally exported them —
    there's no cross-account identity to preserve, and preserving the
    original owner_id would be a privilege-escalation hole: anyone could
    hand-craft a manifest claiming to be someone else's data), attached
    to ws_id. Requires edit-tier+ access to ws_id, same bar as adding
    any other chat to a workspace (add_chat already requires this;
    import is conceptually 'add N chats,' not a separate action)."""
    _require_edit_access(ws_id, user_id)
    chats = manifest.get("chats", [])
    restored = chat_store.restore_chats(user_id, chats, workspace_id=ws_id)
    write_audit(user_id, "workspace.import", "workspace", ws_id,
                {"chat_count": len(restored)})
    return {"restored_chat_ids": [c["id"] for c in restored], "count": len(restored)}