"""
tests/unit/test_eo_chat_store.py — Patch 7e (content/knowledge group).

eo/chat_store.py had zero test coverage before this. It's the UI's
durable, uncapped record of a chat — every message, title, tag, and
cross-chat link — and per its own docstring, ownership scoping
(`WHERE ... AND owner_id = %s`) IS the access control until Part 8.3's
RLS policies land. That makes three things worth pinning down hard
here, in order of how expensive a silent regression would be:

  1. Ownership scoping: every read/write function must filter by
     owner_id in the query itself, and a chat that exists but belongs
     to someone else must look IDENTICAL to one that doesn't exist at
     all (FileNotFoundError either way) -- never a different error
     that would confirm the id belongs to another user.
  2. append_message()'s create-on-first-write + auto-title + seq
     bookkeeping, since it's the hottest path in the module and the
     one most recently rewritten (perf audit item #2 cut it over from
     an O(n) JSONB read-modify-write to small fixed-size columns plus
     one chat_messages insert).
  3. resolve_chat_access()'s collaborator-vs-owner branching, since
     getting that backwards either locks a legitimate collaborator out
     or -- worse -- leaks a private chat.

Isolation: chat_store.py does `from eo import db` (a module import), so
db.cursor()/db.Json are reached through that same module object.
db.cursor() is a @contextmanager; FakeCursor/FakeCursorContext below is
a minimal stand-in that queues canned fetchone()/fetchall() results in
call order and records every execute() (query, params) pair, without
touching a real Postgres connection. db.Json is left as the real
psycopg2.extras.Json wrapper (available in this environment) so
`.adapted` can be asserted on directly, same convention as
test_eo_audit_log.py.

write_audit (imported via `from eo.audit_log import write_audit` --
i.e. bound directly into chat_store's own namespace) is patched as
`chat_store.write_audit` for the same reason: patching `eo.audit_log.
write_audit` would not reach the name chat_store already resolved at
import time.
"""
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from eo import chat_store

# ---------------------------------------------------------------------
# Fake db.cursor() harness
# ---------------------------------------------------------------------

class FakeCursor:
    """Records every execute() call; hands back queued fetchone()/
    fetchall() results in the order they're consumed, so a test can
    script a whole multi-statement transaction (e.g. append_message's
    select-for-update -> insert/update -> insert into chat_messages)."""

    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.executed = []
        self._fetchone_queue = list(fetchone_results or [])
        self._fetchall_queue = list(fetchall_results or [])

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        if not self._fetchone_queue:
            return None
        return self._fetchone_queue.pop(0)

    def fetchall(self):
        if not self._fetchall_queue:
            return []
        return self._fetchall_queue.pop(0)


class FakeCursorContext:
    """Stand-in for db.cursor()'s @contextmanager -- records the
    kwargs it was entered with (user_id / trusted) so tests can assert
    which access posture a given function used, same pattern as
    test_eo_audit_log.py."""

    def __init__(self, cursor, calls_log, **kwargs):
        self.cursor = cursor
        self.calls_log = calls_log
        self.kwargs = kwargs

    def __enter__(self):
        self.calls_log.append(self.kwargs)
        return self.cursor

    def __exit__(self, *exc_info):
        return False


def _install_fake_cursor(monkeypatch, cursor, calls_log=None):
    calls_log = calls_log if calls_log is not None else []
    monkeypatch.setattr(
        chat_store.db, "cursor",
        lambda **kwargs: FakeCursorContext(cursor, calls_log, **kwargs),
    )
    return calls_log


def _row(id="chat_1", title="New Chat", tags=None, linked=None, template_id=None,
         workspace_id=None, message_count=None, last_status=None, is_private=None,
         owner_id=None):
    created = datetime(2026, 1, 1, tzinfo=UTC)
    updated = datetime(2026, 1, 2, tzinfo=UTC)
    row = {
        "id": id, "title": title, "created_at": created, "updated_at": updated,
        "linked_chat_ids": linked or [], "tags": tags or [], "template_id": template_id,
        "workspace_id": workspace_id,
    }
    if message_count is not None:
        row["message_count"] = message_count
    if last_status is not None:
        row["last_status"] = last_status
    if is_private is not None:
        row["is_private"] = is_private
    if owner_id is not None:
        row["owner_id"] = owner_id
    return row


@pytest.fixture(autouse=True)
def _no_real_audit(monkeypatch):
    """write_audit is exercised for its OWN call-site/argument contract
    below; default it to a no-op mock everywhere else so unrelated
    tests don't depend on eo.audit_log's real DB-touching behavior."""
    monkeypatch.setattr(chat_store, "write_audit", MagicMock())


# ---------------------------------------------------------------------
# _clean_tags / new_chat_id / _row_to_chat
# ---------------------------------------------------------------------

def test_clean_tags_strips_dedupes_and_caps_at_25():
    tags = [f" tag{i} " for i in range(30)] + ["tag0"]  # dup + whitespace
    cleaned = chat_store._clean_tags(tags)
    assert cleaned[0] == "tag0"
    assert len(cleaned) == 25
    assert len(cleaned) == len(set(cleaned))


def test_clean_tags_drops_blank_entries():
    assert chat_store._clean_tags(["", "  ", "real"]) == ["real"]


def test_clean_tags_handles_none_and_empty():
    assert chat_store._clean_tags(None) == []
    assert chat_store._clean_tags([]) == []


def test_new_chat_id_has_expected_prefix_and_is_unique():
    a, b = chat_store.new_chat_id(), chat_store.new_chat_id()
    assert a.startswith("chat_")
    assert a != b


def test_row_to_chat_includes_messages_by_default():
    out = chat_store._row_to_chat(_row())
    assert out["messages"] == []
    assert out["id"] == "chat_1"
    assert out["created_at"] == "2026-01-01T00:00:00+00:00"


def test_row_to_chat_can_omit_messages():
    out = chat_store._row_to_chat(_row(), include_messages=False)
    assert "messages" not in out


def test_row_to_chat_surfaces_message_count_and_last_status_when_present():
    out = chat_store._row_to_chat(
        _row(message_count=3, last_status="paused"), include_messages=False)
    assert out["message_count"] == 3
    assert out["last_status"] == "paused"


def test_row_to_chat_omits_message_count_and_last_status_when_absent():
    out = chat_store._row_to_chat(_row(), include_messages=False)
    assert "message_count" not in out
    assert "last_status" not in out


# ---------------------------------------------------------------------
# create_chat
# ---------------------------------------------------------------------

def test_create_chat_scopes_cursor_by_owner_id_and_writes_audit(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_row(title="New Chat")])
    calls_log = _install_fake_cursor(monkeypatch, cursor)
    audit = MagicMock()
    monkeypatch.setattr(chat_store, "write_audit", audit)

    chat_store.create_chat("owner_1", title="New Chat")

    assert calls_log == [{"user_id": "owner_1"}]
    query, params = cursor.executed[0]
    assert "insert into chats" in query
    assert params[2] == "owner_1"  # owner_id param
    inserted_chat_id = params[0]
    audit.assert_called_once_with("owner_1", "chat.create", "chat", inserted_chat_id,
                                   {"title": "New Chat"})


def test_create_chat_cleans_tags_before_insert(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_row(tags=["a"])])
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.create_chat("owner_1", tags=["  a  ", "a", ""])

    _, params = cursor.executed[0]
    assert params[3] == ["a"]


# ---------------------------------------------------------------------
# get_chat / chat_exists — ownership scoping
# ---------------------------------------------------------------------

def test_get_chat_raises_file_not_found_when_row_missing(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)

    with pytest.raises(FileNotFoundError):
        chat_store.get_chat("chat_missing", "owner_1")


def test_get_chat_and_another_owners_chat_raise_the_identical_error(monkeypatch):
    """Per the module's own contract: a chat owned by someone else must
    be indistinguishable from a chat that doesn't exist at all."""
    cursor_missing = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor_missing)
    with pytest.raises(FileNotFoundError) as exc_missing:
        chat_store.get_chat("chat_ghost", "owner_1")

    cursor_wrong_owner = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor_wrong_owner)
    with pytest.raises(FileNotFoundError) as exc_wrong_owner:
        chat_store.get_chat("chat_owned_by_someone_else", "owner_1")

    assert str(exc_missing.value) == str(exc_wrong_owner.value).replace(
        "chat_owned_by_someone_else", "chat_ghost")


def test_get_chat_scopes_lookup_query_by_owner_id(monkeypatch):
    cursor = FakeCursor(
        fetchone_results=[_row(id="chat_1")],
        fetchall_results=[[]],
    )
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.get_chat("chat_1", "owner_1")

    query, params = cursor.executed[0]
    assert "owner_id = %s" in query
    assert params == ("chat_1", "owner_1")


def test_get_chat_unpaginated_returns_every_message_oldest_first_with_id_and_seq(monkeypatch):
    rows = [
        {"id": "m2", "seq": 1, "payload": {"role": "assistant", "text": "b"}},
        {"id": "m1", "seq": 0, "payload": {"role": "user", "text": "a"}},
    ]
    cursor = FakeCursor(fetchone_results=[_row()], fetchall_results=[rows])
    _install_fake_cursor(monkeypatch, cursor)

    chat = chat_store.get_chat("chat_1", "owner_1")

    # get_chat does not reorder — it trusts the query's own ORDER BY.
    # Confirm the query orders ascending and every message carries id/seq.
    query, _ = cursor.executed[1]
    assert "order by seq asc" in query
    assert chat["messages"][0]["id"] == "m2"
    assert chat["messages"][0]["seq"] == 1
    assert chat["has_more"] is False


def test_get_chat_paginated_sets_has_more_and_trims_the_extra_probe_row(monkeypatch):
    # limit=2 -> fetches 3 rows (newest-first) to detect more-beyond-page
    rows = [
        {"id": "m3", "seq": 2, "payload": {"role": "assistant", "text": "c"}},
        {"id": "m2", "seq": 1, "payload": {"role": "user", "text": "b"}},
        {"id": "m1", "seq": 0, "payload": {"role": "user", "text": "a"}},
    ]
    cursor = FakeCursor(fetchone_results=[_row()], fetchall_results=[rows])
    _install_fake_cursor(monkeypatch, cursor)

    chat = chat_store.get_chat("chat_1", "owner_1", limit=2)

    assert chat["has_more"] is True
    assert len(chat["messages"]) == 2
    # trimmed rows are reversed back to oldest-first
    assert [m["id"] for m in chat["messages"]] == ["m2", "m3"]


def test_get_chat_paginated_no_more_when_fewer_rows_than_limit_plus_one(monkeypatch):
    rows = [{"id": "m1", "seq": 0, "payload": {"role": "user", "text": "a"}}]
    cursor = FakeCursor(fetchone_results=[_row()], fetchall_results=[rows])
    _install_fake_cursor(monkeypatch, cursor)

    chat = chat_store.get_chat("chat_1", "owner_1", limit=5)

    assert chat["has_more"] is False
    assert len(chat["messages"]) == 1


def test_get_chat_before_seq_adds_seq_filter_clause_and_param(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_row()], fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.get_chat("chat_1", "owner_1", limit=5, before_seq=10)

    query, params = cursor.executed[1]
    assert "seq < %s" in query
    assert params == ["chat_1", 10, 6]


def test_chat_exists_true_and_false(monkeypatch):
    cursor_true = FakeCursor(fetchone_results=[{"exists": 1}])
    _install_fake_cursor(monkeypatch, cursor_true)
    assert chat_store.chat_exists("chat_1", "owner_1") is True

    cursor_false = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor_false)
    assert chat_store.chat_exists("chat_1", "owner_1") is False


# ---------------------------------------------------------------------
# append_message — create-on-first-write, auto-title, seq bookkeeping
# ---------------------------------------------------------------------

def test_append_message_creates_chat_and_seq_0_message_when_none_exists(monkeypatch):
    cursor = FakeCursor(
        fetchone_results=[
            None,                     # select ... for update -> no existing chat
            _row(title="Fix the flaky retry loop"),  # insert ... returning
        ],
    )
    _install_fake_cursor(monkeypatch, cursor)

    result = chat_store.append_message(
        "chat_1", "owner_1", {"role": "user", "text": "Fix the flaky retry loop please"})

    insert_chats_query, insert_chats_params = cursor.executed[1]
    assert "insert into chats" in insert_chats_query
    assert insert_chats_params[1] == "Fix the flaky retry loop please"[:60]

    insert_message_query, insert_message_params = cursor.executed[2]
    assert "insert into chat_messages" in insert_message_query
    assert insert_message_params[2] == 0  # seq
    assert "messages" not in result  # include_messages=False path


def test_append_message_auto_titles_from_first_user_message_truncated_to_60(monkeypatch):
    long_text = "x" * 100
    cursor = FakeCursor(fetchone_results=[None, _row(title=long_text[:60] + "...")])
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.append_message("chat_1", "owner_1", {"role": "user", "text": long_text})

    _, insert_params = cursor.executed[1]
    assert insert_params[1] == long_text[:60] + "..."


def test_append_message_keeps_new_chat_title_when_first_message_is_not_from_user(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None, _row(title="New Chat")])
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.append_message("chat_1", "owner_1", {"role": "assistant", "text": "hi"})

    _, insert_params = cursor.executed[1]
    assert insert_params[1] == "New Chat"


def test_append_message_existing_chat_computes_next_seq_and_updates_updated_at(monkeypatch):
    cursor = FakeCursor(
        fetchone_results=[
            {"title": "New Chat"},          # select title ... for update
            _row(title="Second message here"),  # update ... returning
            {"next_seq": 4},                 # select coalesce(max(seq)+1, 0)
        ],
    )
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.append_message(
        "chat_1", "owner_1", {"role": "user", "text": "Second message here"})

    insert_query, insert_params = cursor.executed[-1]
    assert "insert into chat_messages" in insert_query
    assert insert_params[2] == 4  # seq taken from next_seq


def test_append_message_does_not_retitle_an_already_titled_existing_chat(monkeypatch):
    cursor = FakeCursor(
        fetchone_results=[
            {"title": "Already Titled"},
            _row(title="Already Titled"),
            {"next_seq": 1},
        ],
    )
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.append_message("chat_1", "owner_1", {"role": "user", "text": "follow up"})

    update_query, update_params = cursor.executed[1]
    assert "update chats set title" in update_query
    assert update_params[0] == "Already Titled"


def test_append_message_fills_in_ts_when_missing(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None, _row()])
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.append_message("chat_1", "owner_1", {"role": "user", "text": "hi"})

    _, insert_msg_params = cursor.executed[2]
    stored_message = insert_msg_params[4]  # db.Json(message)
    assert stored_message.adapted["ts"]


def test_append_message_preserves_caller_supplied_ts(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None, _row()])
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.append_message(
        "chat_1", "owner_1", {"role": "user", "text": "hi", "ts": "2020-01-01T00:00:00+00:00"})

    _, insert_msg_params = cursor.executed[2]
    assert insert_msg_params[4].adapted["ts"] == "2020-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------
# rename_chat / set_chat_tags / set_linked_chats — not-found + scoping
# ---------------------------------------------------------------------

def test_rename_chat_raises_when_not_found_or_not_owned(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_store.rename_chat("chat_1", "owner_1", "New Title")


def test_rename_chat_keeps_existing_title_when_new_title_blank(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_row(title="Kept Title")])
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.rename_chat("chat_1", "owner_1", "   ")

    query, params = cursor.executed[0]
    assert "update chats set updated_at" in query
    assert "title" not in query.split("where")[0].replace("updated_at", "")


def test_rename_chat_truncates_title_to_120_chars(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_row()])
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.rename_chat("chat_1", "owner_1", "x" * 200)

    _, params = cursor.executed[0]
    assert params[0] == "x" * 120


def test_set_linked_chats_filters_out_self_link_and_nonexistent_ids(monkeypatch):
    cursor = FakeCursor(
        fetchone_results=[_row(linked=["chat_2"])],
        fetchall_results=[[{"id": "chat_2"}]],  # only chat_2 actually exists/owned
    )
    _install_fake_cursor(monkeypatch, cursor)

    chat_store.set_linked_chats("chat_1", "owner_1", ["chat_1", "chat_2", "chat_ghost"])

    update_query, update_params = cursor.executed[1]
    assert update_params[0] == ["chat_2"]


def test_set_linked_chats_raises_when_chat_not_found(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None], fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_store.set_linked_chats("chat_1", "owner_1", [])


# ---------------------------------------------------------------------
# delete_chat
# ---------------------------------------------------------------------

def test_delete_chat_is_a_silent_noop_when_missing_or_not_owned(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    chat_store.delete_chat("chat_1", "owner_1")  # must not raise
    assert len(cursor.executed) == 1  # never reaches the link-cleanup update


def test_delete_chat_writes_audit_and_strips_links_and_clears_bus(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"id": "chat_1"}])
    _install_fake_cursor(monkeypatch, cursor)
    audit = MagicMock()
    monkeypatch.setattr(chat_store, "write_audit", audit)
    bus_delete = MagicMock()
    monkeypatch.setattr("memory.bus.delete", bus_delete, raising=False)

    chat_store.delete_chat("chat_1", "owner_1")

    audit.assert_called_once_with("owner_1", "chat.delete", "chat", "chat_1", {})
    cleanup_query, cleanup_params = cursor.executed[1]
    assert "array_remove(linked_chat_ids" in cleanup_query
    assert cleanup_params == ("chat_1", "owner_1", "chat_1")
    bus_delete.assert_called_once_with("conversation:chat_1")


def test_delete_chat_swallows_bus_delete_failures(monkeypatch):
    """bus.py needs Upstash env vars; a missing .env must not break
    chat deletion itself (module's own stated contract)."""
    cursor = FakeCursor(fetchone_results=[{"id": "chat_1"}])
    _install_fake_cursor(monkeypatch, cursor)

    def _broken_delete(*a, **kw):
        raise RuntimeError("no upstash env vars")

    monkeypatch.setattr("memory.bus.delete", _broken_delete, raising=False)

    chat_store.delete_chat("chat_1", "owner_1")  # must not raise


# ---------------------------------------------------------------------
# get_linked_context_text / estimate_batch_context_tokens
# ---------------------------------------------------------------------

def test_get_linked_context_text_empty_when_chat_missing(monkeypatch):
    monkeypatch.setattr(chat_store, "chat_exists", lambda *a, **kw: False)
    assert chat_store.get_linked_context_text("chat_1", "owner_1") == ""


def test_get_linked_context_text_empty_when_no_links(monkeypatch):
    monkeypatch.setattr(chat_store, "chat_exists", lambda *a, **kw: True)
    monkeypatch.setattr(chat_store, "get_chat",
                         lambda *a, **kw: {"linked_chat_ids": [], "messages": [], "title": "t"})
    assert chat_store.get_linked_context_text("chat_1", "owner_1") == ""


def test_get_linked_context_text_skips_links_the_owner_no_longer_has_access_to(monkeypatch):
    def fake_exists(chat_id, owner_id):
        return chat_id == "chat_1"  # linked chat is gone/not owned

    monkeypatch.setattr(chat_store, "chat_exists", fake_exists)
    monkeypatch.setattr(chat_store, "get_chat", lambda *a, **kw: {
        "linked_chat_ids": ["chat_2"], "messages": [], "title": "t"})

    assert chat_store.get_linked_context_text("chat_1", "owner_1") == ""


def test_get_linked_context_text_formats_user_and_assistant_lines_and_truncates(monkeypatch):
    def fake_exists(chat_id, owner_id):
        return True

    def fake_get_chat(chat_id, owner_id):
        if chat_id == "chat_1":
            return {"linked_chat_ids": ["chat_2"], "messages": [], "title": "main"}
        return {
            "title": "Linked Chat",
            "messages": [
                {"role": "user", "text": "hello"},
                {"role": "assistant", "data": {"result": {"answer": "y" * 500}}},
            ],
        }

    monkeypatch.setattr(chat_store, "chat_exists", fake_exists)
    monkeypatch.setattr(chat_store, "get_chat", fake_get_chat)

    text = chat_store.get_linked_context_text("chat_1", "owner_1", char_limit=400)

    assert '[Shared memory from chat "Linked Chat"]' in text
    assert "- user: hello" in text
    assistant_line = next(line for line in text.split("\n") if line.startswith("- assistant"))
    content = assistant_line[len("- assistant: "):]
    assert content == "y" * 400 + "..."  # truncated to char_limit, "..." appended


def test_extract_answer_text_prefers_answer_then_code_then_output_then_message():
    assert chat_store._extract_answer_text({"data": {"result": {"answer": "A"}}}) == "A"
    assert chat_store._extract_answer_text({"data": {"result": {"code": "C"}}}) == "C"
    assert chat_store._extract_answer_text({"data": {"result": {"output": "O"}}}) == "O"
    assert chat_store._extract_answer_text({"data": {"message": "M"}}) == "M"
    assert chat_store._extract_answer_text({}) == ""


def test_estimate_batch_context_tokens_filters_to_owned_existing_chats(monkeypatch):
    def fake_exists(chat_id, owner_id):
        return chat_id != "chat_ghost"

    def fake_get_chat(chat_id, owner_id):
        return {"messages": [{"role": "user", "text": "hi " * 20}], "title": chat_id}

    monkeypatch.setattr(chat_store, "chat_exists", fake_exists)
    monkeypatch.setattr(chat_store, "get_chat", fake_get_chat)

    result = chat_store.estimate_batch_context_tokens(
        "owner_1", ["chat_1", "chat_2", "chat_ghost"])

    assert result["member_count"] == 2
    assert set(result["per_chat_tokens"].keys()) == {"chat_1", "chat_2"}


def test_estimate_batch_context_tokens_all_zero_when_no_valid_chats(monkeypatch):
    monkeypatch.setattr(chat_store, "chat_exists", lambda *a, **kw: False)

    result = chat_store.estimate_batch_context_tokens("owner_1", ["chat_1"])

    assert result == {
        "member_count": 0, "per_chat_tokens": {},
        "max_tokens_per_message": 0, "avg_tokens_per_message": 0,
    }


# ---------------------------------------------------------------------
# resolve_chat_access — owner vs. collaborator vs. no access
# ---------------------------------------------------------------------

def test_resolve_chat_access_returns_owner_when_requester_owns_the_chat(monkeypatch):
    monkeypatch.setattr(chat_store, "chat_exists", lambda *a, **kw: True)
    result = chat_store.resolve_chat_access("chat_1", "owner_1")
    assert result == ("owner_1", "owner")


def test_resolve_chat_access_returns_none_for_a_private_chat_even_with_workspace_role(monkeypatch):
    monkeypatch.setattr(chat_store, "chat_exists", lambda *a, **kw: False)
    cursor = FakeCursor(fetchone_results=[
        {"owner_id": "owner_1", "workspace_id": "ws_1", "is_private": True}])
    _install_fake_cursor(monkeypatch, cursor)

    assert chat_store.resolve_chat_access("chat_1", "requester_1") is None


def test_resolve_chat_access_returns_none_when_chat_row_missing_entirely(monkeypatch):
    monkeypatch.setattr(chat_store, "chat_exists", lambda *a, **kw: False)
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)

    assert chat_store.resolve_chat_access("chat_missing", "requester_1") is None


def test_resolve_chat_access_returns_none_when_chat_has_no_workspace(monkeypatch):
    monkeypatch.setattr(chat_store, "chat_exists", lambda *a, **kw: False)
    cursor = FakeCursor(fetchone_results=[
        {"owner_id": "owner_1", "workspace_id": None, "is_private": False}])
    _install_fake_cursor(monkeypatch, cursor)

    assert chat_store.resolve_chat_access("chat_1", "requester_1") is None


def test_resolve_chat_access_returns_none_when_requester_has_no_workspace_role(monkeypatch):
    monkeypatch.setattr(chat_store, "chat_exists", lambda *a, **kw: False)
    cursor = FakeCursor(fetchone_results=[
        {"owner_id": "owner_1", "workspace_id": "ws_1", "is_private": False}])
    _install_fake_cursor(monkeypatch, cursor)

    from eo import chat_workspace
    monkeypatch.setattr(chat_workspace, "member_role", lambda ws_id, uid: None)

    assert chat_store.resolve_chat_access("chat_1", "requester_1") is None


def test_resolve_chat_access_returns_real_owner_and_role_for_a_workspace_collaborator(monkeypatch):
    monkeypatch.setattr(chat_store, "chat_exists", lambda *a, **kw: False)
    cursor = FakeCursor(fetchone_results=[
        {"owner_id": "owner_1", "workspace_id": "ws_1", "is_private": False}])
    _install_fake_cursor(monkeypatch, cursor)

    from eo import chat_workspace
    monkeypatch.setattr(chat_workspace, "member_role", lambda ws_id, uid: "editor")

    result = chat_store.resolve_chat_access("chat_1", "requester_1")

    assert result == ("owner_1", "editor")
