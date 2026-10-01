"""
tests/unit/test_eo_code_proposals_deploy.py — W8.5.

Covers eo/code_proposals.py's create_deploy_config_proposal(): the
deploy_config_writer plan becoming an ordinary pending proposal, and
then behaving like every other proposal.

What matters most, worst-silent-failure first:

  1. Creating the proposal writes NOTHING to workspace_code_files — the
     whole point of the step. Only resolve_proposal()'s Keep does, via
     write_file(source="proposal", base_version=...).
  2. A re-propose rejects the earlier pending deploy proposal (any
     path — the writer picks ONE host per plan) but never touches a
     chat-edit proposal, and never rejects the new one.
  3. A plan with no usable file raises ValueError BEFORE any row is
     stored.
  4. The proposal is tagged model_meta.generator == "deploy_config_writer"
     (the frontend routes "Regenerate" off that tag) and its event
     payload is ids/status only (Pusher's 10,240-byte cap).

Isolation: a small in-memory stand-in for the workspace_code_proposals
table (_FakeProposalDb) behind code_proposals.db.cursor, so
create_proposal()/list_proposals()/resolve_proposal() all run for real
and the supersede path is exercised end to end rather than mocked.
workspace_code_files is faked at get_file()/write_file() (get_file keeps
the real _validate_file_path so a traversal path still fails loud).
"""
from datetime import UTC, datetime

import pytest

from eo import code_proposals, workspace_code_files
from eo.code_proposals import ProposalStaleError
from relay.emitter import EventType

WS = "ws_1"
USER = "user_1"
SESSION = "chat_1"

RENDER_PLAN = {
    "platform": "render",
    "config_filename": "render.yaml",
    "config_content": "services:\n  - type: web\n    name: app\n",
    "reason": "backend service detected",
}
FLY_PLAN = {
    "platform": "fly",
    "config_filename": "fly.toml",
    "config_content": 'app = "app"\n',
    "reason": "Dockerfile present",
}


class _FakeProposalDb:
    """Just enough of workspace_code_proposals for code_proposals.py's
    four statements (insert / select by status / select by id / update
    status). Rows are stored the way psycopg hands them back: jsonb
    columns as plain Python objects."""

    def __init__(self):
        self.rows = []
        self.cursor_calls = []

    def cursor(self, **kwargs):
        self.cursor_calls.append(kwargs)
        return _FakeCursorContext(_FakeCursor(self))


class _FakeCursorContext:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *exc_info):
        return False


class _FakeCursor:
    def __init__(self, db):
        self._db = db
        self._one = None
        self._all = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if q.startswith("insert into workspace_code_proposals"):
            (pid, ws, session, created_by, instruction, status,
             files, summary, refs, model_meta, created_at) = params
            row = {
                "id": pid, "workspace_id": ws, "session_id": session,
                "created_by": created_by, "instruction": instruction,
                "status": status, "files": files.obj, "summary": summary,
                "refs": refs.obj, "model_meta": model_meta.obj,
                "created_at": created_at, "resolved_at": None,
            }
            self._db.rows.append(row)
            self._one = row
        elif "from workspace_code_proposals where workspace_id = %s and status = %s" in q:
            ws, status = params
            self._all = [r for r in reversed(self._db.rows)
                         if r["workspace_id"] == ws and r["status"] == status]
        elif "from workspace_code_proposals where workspace_id = %s and id = %s" in q:
            ws, pid = params
            self._one = next((r for r in self._db.rows
                              if r["workspace_id"] == ws and r["id"] == pid), None)
        elif q.startswith("update workspace_code_proposals set status"):
            if "set status = %s, resolved_at = %s" in q:
                status, resolved_at, ws, pid = params
            else:
                (status, ws, pid), resolved_at = params, None
            row = next((r for r in self._db.rows
                        if r["workspace_id"] == ws and r["id"] == pid), None)
            if row is not None:
                row["status"] = status
                if resolved_at is not None:
                    row["resolved_at"] = resolved_at
            self._one = row
        else:  # pragma: no cover - a new statement means this fake needs updating
            raise AssertionError(f"unexpected SQL: {q}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


@pytest.fixture
def store(monkeypatch):
    """The fake table plus a fake workspace file store, all side effects
    (audit, events, writes) captured on the returned namespace."""
    db = _FakeProposalDb()
    monkeypatch.setattr(code_proposals.db, "cursor", db.cursor)

    class Env:
        pass

    env = Env()
    env.db = db
    env.files = {}  # path -> {"version", "content"}; absent = never saved
    env.writes = []
    env.deletes = []
    env.audits = []
    env.events = []

    def get_file(ws_id, path):
        workspace_code_files._validate_file_path(path)  # the real shape gate
        saved = env.files.get(path)
        if saved is None:
            return {"workspace_id": ws_id, "file_path": path, "content": "", "version": 0}
        return {"workspace_id": ws_id, "file_path": path, **saved}

    def write_file(ws_id, path, content, user_id, base_version=None, source="user", **kw):
        env.writes.append({"path": path, "content": content, "base_version": base_version,
                           "source": source})
        env.files[path] = {"version": (env.files.get(path, {}).get("version", 0)) + 1,
                           "content": content}

    monkeypatch.setattr(code_proposals.workspace_code_files, "get_file", get_file)
    monkeypatch.setattr(code_proposals.workspace_code_files, "write_file", write_file)
    monkeypatch.setattr(code_proposals.workspace_code_files, "delete_file",
                        lambda ws_id, path, user_id: env.deletes.append(path))
    monkeypatch.setattr(code_proposals, "write_audit",
                        lambda user_id, action, kind, ws_id, detail=None:
                        env.audits.append((action, detail)))
    monkeypatch.setattr(code_proposals, "emit_workspace_event",
                        lambda event, workspace_id=None, agent=None, payload=None:
                        env.events.append((event, payload)))
    return env


def _propose(plan=RENDER_PLAN):
    return code_proposals.create_deploy_config_proposal(WS, plan, SESSION, USER)


# ---------------------------------------------------------------------------
# 1. The plan becomes a pending proposal; nothing is written
# ---------------------------------------------------------------------------
class TestCreate:
    def test_new_file_is_a_pending_create_with_the_plans_content(self, store):
        proposal = _propose()
        assert proposal["status"] == "pending"
        assert proposal["session_id"] == SESSION
        assert proposal["summary"] == "Add render deploy config (render.yaml)"
        [f] = proposal["files"]
        assert f["path"] == "render.yaml"
        assert f["op"] == "create"
        assert f["base_version"] == 0
        assert f["original"] == ""
        assert f["proposed"] == RENDER_PLAN["config_content"]

    def test_creating_writes_nothing_to_the_workspace_files(self, store):
        _propose()
        assert store.writes == []
        assert store.deletes == []

    def test_existing_file_is_a_replace_against_what_was_saved(self, store):
        store.files["render.yaml"] = {"version": 3, "content": "old: yes\n"}
        [f] = _propose()["files"]
        assert f["op"] == "replace"
        assert f["base_version"] == 3
        assert f["original"] == "old: yes\n"
        assert f["proposed"] == RENDER_PLAN["config_content"]

    def test_summary_says_update_for_a_replace(self, store):
        store.files["render.yaml"] = {"version": 1, "content": "x"}
        assert _propose()["summary"] == "Update render deploy config (render.yaml)"

    def test_tagged_so_regenerate_can_find_the_deploy_writer(self, store):
        meta = _propose()["model_meta"]
        assert meta["generator"] == code_proposals.DEPLOY_CONFIG_GENERATOR == "deploy_config_writer"
        assert meta["platform"] == "render"
        assert meta["config_filename"] == "render.yaml"
        assert meta["reason"] == "backend service detected"

    def test_stored_ref_is_the_config_file(self, store):
        [ref] = _propose()["refs"]
        assert ref["kind"] == "file"
        assert ref["path"] == "render.yaml"

    def test_ready_event_and_audit_follow_the_normal_proposal_flow(self, store):
        proposal = _propose()
        assert store.audits == [("code_proposal.create",
                                 {"proposal_id": proposal["id"], "status": "pending",
                                  "file_count": 1})]
        [(event, payload)] = store.events
        assert event == EventType.CODE_PROPOSAL_READY
        # ids/status only — never file content (Pusher's byte cap)
        assert payload == {"workspace_id": WS, "proposal_id": proposal["id"], "status": "pending"}

    def test_long_reason_is_capped_in_the_stored_meta(self, store):
        plan = {**RENDER_PLAN, "reason": "x" * 5000}
        assert len(_propose(plan)["model_meta"]["reason"]) == code_proposals._DEPLOY_REASON_MAX_CHARS

    def test_non_string_reason_is_stored_as_none(self, store):
        assert _propose({**RENDER_PLAN, "reason": ["not", "text"]})["model_meta"]["reason"] is None

    def test_nested_workflow_path_is_accepted(self, store):
        plan = {"platform": "github_pages", "config_filename": ".github/workflows/deploy.yml",
                "config_content": "name: deploy\n"}
        assert _propose(plan)["files"][0]["path"] == ".github/workflows/deploy.yml"


# ---------------------------------------------------------------------------
# 2. A bad plan fails before any row exists
# ---------------------------------------------------------------------------
class TestBadPlans:
    @pytest.mark.parametrize("plan", [
        {},
        {"platform": "render", "config_content": "x"},
        {"platform": "render", "config_filename": "   ", "config_content": "x"},
        {"platform": "render", "config_filename": "render.yaml"},
        {"platform": "render", "config_filename": "render.yaml", "config_content": None},
        {"platform": "render", "config_filename": ["render.yaml"], "config_content": "x"},
    ])
    def test_unusable_plan_raises_and_stores_nothing(self, store, plan):
        with pytest.raises(ValueError):
            _propose(plan)
        assert store.db.rows == []
        assert store.events == []

    def test_non_dict_plan_raises(self, store):
        with pytest.raises(ValueError):
            _propose(["not", "a", "dict"])

    @pytest.mark.parametrize("path", ["../x.yaml", "a/../../b.yaml", "/etc/x.yaml", "bad\x00.yaml"])
    def test_unsafe_path_raises_and_stores_nothing(self, store, path):
        with pytest.raises(ValueError):
            _propose({**RENDER_PLAN, "config_filename": path})
        assert store.db.rows == []

    def test_empty_config_content_is_allowed(self, store):
        # An empty string is still a string; the review shows it and the
        # person can Undo it. Only a MISSING/non-string content is a plan bug.
        assert _propose({**RENDER_PLAN, "config_content": ""})["files"][0]["proposed"] == ""


# ---------------------------------------------------------------------------
# 3. Re-propose supersedes the earlier deploy proposal, and only that one
# ---------------------------------------------------------------------------
class TestSupersede:
    def test_second_propose_rejects_the_first(self, store):
        first = _propose(RENDER_PLAN)
        second = _propose(RENDER_PLAN)
        rows = {r["id"]: r for r in store.db.rows}
        assert rows[first["id"]]["status"] == "rejected"
        assert rows[first["id"]]["resolved_at"] is not None
        assert rows[second["id"]]["status"] == "pending"

    def test_switching_host_still_retires_the_old_proposal(self, store):
        first = _propose(RENDER_PLAN)   # render.yaml
        second = _propose(FLY_PLAN)     # fly.toml — different path
        rows = {r["id"]: r for r in store.db.rows}
        assert rows[first["id"]]["status"] == "rejected"
        assert rows[second["id"]]["status"] == "pending"

    def test_superseding_writes_no_files(self, store):
        _propose(RENDER_PLAN)
        _propose(FLY_PLAN)
        assert store.writes == []

    def test_superseded_rejection_is_audited_and_announced(self, store):
        first = _propose(RENDER_PLAN)
        store.audits.clear()
        store.events.clear()
        _propose(FLY_PLAN)
        assert ("code_proposal.reject", {"proposal_id": first["id"], "kept": [],
                                         "undone": ["render.yaml"]}) in store.audits
        resolved = [p for e, p in store.events if e == EventType.CODE_PROPOSAL_RESOLVED]
        assert resolved == [{"workspace_id": WS, "proposal_id": first["id"], "status": "rejected"}]

    def test_a_chat_edit_proposal_is_left_pending(self, store):
        # Same file, but a person asked for it — not the deploy writer's to retire.
        chat = code_proposals.create_proposal(
            WS, "tweak render.yaml", [{"kind": "file", "path": "render.yaml"}], SESSION, USER,
            generate_edit=lambda ws, instr, refs, cur: {
                "summary": "chat edit",
                "files": [{"path": "render.yaml", "op": "create", "content": "a: 1\n"}],
            },
        )
        deploy = _propose(RENDER_PLAN)
        rows = {r["id"]: r for r in store.db.rows}
        assert rows[chat["id"]]["status"] == "pending"
        assert rows[deploy["id"]]["status"] == "pending"

    def test_already_resolved_deploy_proposals_are_not_touched(self, store):
        first = _propose(RENDER_PLAN)
        code_proposals.resolve_proposal(WS, first["id"], [], USER)  # rejected by the person
        store.audits.clear()
        _propose(RENDER_PLAN)
        assert store.audits == [("code_proposal.create",
                                 {"proposal_id": store.db.rows[-1]["id"], "status": "pending",
                                  "file_count": 1})]

    def test_a_proposal_resolved_mid_supersede_is_skipped(self, store, monkeypatch):
        first = _propose(RENDER_PLAN)
        real_resolve = code_proposals.resolve_proposal

        def racing_resolve(ws_id, pid, decisions, user_id):
            real_resolve(ws_id, pid, decisions, user_id)   # someone else got there first…
            return real_resolve(ws_id, pid, decisions, user_id)  # …so this one raises ValueError

        monkeypatch.setattr(code_proposals, "resolve_proposal", racing_resolve)
        second = _propose(FLY_PLAN)  # must not raise
        assert second["status"] == "pending"
        assert {r["id"]: r["status"] for r in store.db.rows}[first["id"]] == "rejected"


# ---------------------------------------------------------------------------
# 4. Keep / Undo — the identical review: only Keep writes
# ---------------------------------------------------------------------------
class TestReview:
    def test_keep_writes_the_file_as_a_proposal_snapshot(self, store):
        proposal = _propose()
        resolved = code_proposals.resolve_proposal(
            WS, proposal["id"], [{"path": "render.yaml", "decision": "keep"}], USER)
        assert resolved["status"] == "accepted"
        assert store.writes == [{"path": "render.yaml", "content": RENDER_PLAN["config_content"],
                                 "base_version": 0, "source": "proposal"}]

    def test_keep_with_hand_edited_final_content_writes_that_text(self, store):
        proposal = _propose()
        code_proposals.resolve_proposal(
            WS, proposal["id"],
            [{"path": "render.yaml", "decision": "keep", "final_content": "edited: yes\n"}], USER)
        assert store.writes[0]["content"] == "edited: yes\n"

    def test_undo_writes_nothing_and_rejects(self, store):
        proposal = _propose()
        resolved = code_proposals.resolve_proposal(
            WS, proposal["id"], [{"path": "render.yaml", "decision": "undo"}], USER)
        assert resolved["status"] == "rejected"
        assert store.writes == []

    def test_keep_after_the_file_moved_on_is_stale_and_writes_nothing(self, store):
        proposal = _propose()
        store.files["render.yaml"] = {"version": 1, "content": "someone else's\n"}
        with pytest.raises(ProposalStaleError) as exc:
            code_proposals.resolve_proposal(
                WS, proposal["id"], [{"path": "render.yaml", "decision": "keep"}], USER)
        assert [f["path"] for f in exc.value.stale_files] == ["render.yaml"]
        assert store.writes == []

    def test_a_superseded_proposal_cannot_be_kept_any_more(self, store):
        first = _propose(RENDER_PLAN)
        _propose(FLY_PLAN)
        with pytest.raises(ValueError, match="already resolved"):
            code_proposals.resolve_proposal(
                WS, first["id"], [{"path": "render.yaml", "decision": "keep"}], USER)
        assert store.writes == []
