"""S06 migration 0014: invariants checked on the SQL text itself, so they hold even
where no database is available (CI). The real-Postgres behaviour of the same
properties is in tests/integration/test_runs_pg.py.

  * THE RULE THIS FILE EXISTS FOR (build plan O1 / 11.4): the `runs` table has no
    content column. Checked three ways: the column list is an allow-list (adding a
    column means editing this test on purpose), no column name can carry content, and
    every text column is either an id or bounded by a CHECK that makes it a vocabulary
    word, a code or an enumeration -- never free text. No json / bytea / array column.
  * RLS enabled AND forced; policies dropped before they are created;
  * grants live in the same file, are table-level, go only to minime_app, and nothing
    grants DELETE; B01's replay of ALL migrations still works with 0014 present and
    derives {select, insert, update} for runs;
  * what the grants cannot express is carried by a BEFORE UPDATE trigger written as a
    plain CREATE FUNCTION whose messages never print a row value;
  * the CHECKs agree with codeagent/runs.py and with protocol/v1 (state list, run id
    pattern, terminal states);
  * the file is safe to paste into the Supabase SQL editor.

It also holds the structural logging check for codeagent/runs.py (the S04 test only
scans ws.py and connections.py): no logging call may name a content-carrying variable,
ask for a traceback or log an exception object.
"""
import ast
import json
import pathlib
import re

import pytest

from tests.static.test_codeagent_logging import scan
from tests.static.test_sql_grants import derive_grants, split_statements

BACKEND = pathlib.Path(__file__).resolve().parents[2]
SQL = (BACKEND / "migrations" / "0014_m2c_runs.sql").read_text()
CODE = "\n".join(line for line in SQL.splitlines() if not line.lstrip().startswith("--"))
STATEMENTS = split_statements(SQL)
CREATE = next(s for s in STATEMENTS if s.lower().startswith("create table if not exists runs "))

# The complete, reviewed column list. A new column is a deliberate edit HERE.
ALLOWED_COLUMNS = {
    "id", "user_id", "device_id", "profile", "mode", "state",
    "created_at", "updated_at", "ended_at", "error_code", "steps", "credits",
}
# Words that mean "this column holds something a person or the model wrote".
CONTENT_WORDS = (
    "title", "prompt", "text", "path", "file", "name", "message", "msg", "content", "body",
    "command", "cmd", "output", "input", "label", "summary", "description", "note", "reason",
    "detail", "instruction", "history", "conversation", "transcript", "payload", "args",
    "argument", "query", "url", "diff", "snippet", "comment", "cwd", "dir", "tool", "result",
    "memory", "rule", "reply", "response", "data", "json", "blob", "meta",
)


def _columns():
    """(name, type, rest-of-definition) for every column in the CREATE TABLE."""
    body = CREATE[CREATE.index("(") + 1: CREATE.rindex(")")]
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    cols = []
    for part in parts:
        if re.match(r"(constraint|check|primary|unique|foreign)\b", part, re.I):
            continue
        name, rest = part.split(None, 1)
        typ = re.split(r"\s", rest.strip(), maxsplit=1)[0].lower()
        cols.append((name.lower(), typ, rest))
    return cols


COLUMNS = _columns()


# ---------------------------------------------------------------------------
# No content column
# ---------------------------------------------------------------------------

def test_the_column_list_is_the_reviewed_allow_list():
    assert {c[0] for c in COLUMNS} == ALLOWED_COLUMNS
    assert len(COLUMNS) == len(ALLOWED_COLUMNS)


def test_no_column_name_can_carry_content():
    for name, _typ, _rest in COLUMNS:
        for word in CONTENT_WORDS:
            assert word not in name.split("_"), f"column {name!r} looks like content ({word})"


def test_no_column_can_hold_structured_or_binary_content():
    for name, typ, rest in COLUMNS:
        assert typ in {"text", "uuid", "timestamptz", "integer", "numeric"}, (name, typ)
        assert "[]" not in rest and "json" not in rest.lower() and "bytea" not in rest.lower(), name


def test_every_text_column_is_an_id_or_bounded_to_a_word_a_code_or_an_enumeration():
    bounded = {"id", "profile", "mode", "state", "error_code"}
    for name, typ, rest in COLUMNS:
        if typ != "text":
            continue
        if name == "user_id":
            continue                        # an account id, like usage_ledger.user_id
        assert name in bounded, f"unreviewed text column {name!r}"
        assert re.search(r"check\s*\(", rest, re.I), f"text column {name!r} has no CHECK"
    shape = {
        "id": r"\^\[A-Za-z0-9_-\]\{8,64\}\$",
        "profile": r"\^\[a-z\]\[a-z0-9_-\]\{0,31\}\$",
        "mode": r"\^\[a-z\]\[a-z0-9_-\]\{0,31\}\$",
        "error_code": r"\^\[a-z\]\[a-z_\]\{0,63\}\$",
    }
    for name, pattern in shape.items():
        rest = next(r for n, _t, r in COLUMNS if n == name)
        assert re.search(pattern, rest), f"{name}: no shape check"
    state_rest = next(r for n, _t, r in COLUMNS if n == "state")
    assert re.search(r"state\s+in\s*\(", state_rest) or "in (" in state_rest


def test_the_table_comment_says_there_is_no_content_and_the_header_explains_why():
    assert "NO CONTENT" in SQL and "No title, prompt, text, path or message column" in SQL
    assert "NO FOREIGN KEYS" in SQL and "NO DELETE GRANT" in SQL


# ---------------------------------------------------------------------------
# Table, RLS, grants
# ---------------------------------------------------------------------------

def test_the_table_is_created_idempotently_with_rls_enabled_and_forced():
    assert "create table if not exists runs " in CODE
    assert "alter table runs enable row level security;" in CODE
    assert "alter table runs force row level security;" in CODE


def test_every_policy_is_dropped_before_it_is_created_and_covers_select_insert_update():
    created = re.findall(r"create policy (\w+) on", CODE)
    assert sorted(created) == ["runs_insert", "runs_select", "runs_update"]
    for name in created:
        assert f"drop policy if exists {name} on" in CODE
    assert "for delete" not in CODE.lower() and "for all" not in CODE.lower()
    for stmt in STATEMENTS:
        if stmt.lower().startswith("create policy"):
            assert "app.current_user_id" in stmt and "app.trusted_internal" in stmt


def test_b01s_replay_of_every_migration_accepts_0014_and_derives_select_insert_update():
    ledger = derive_grants()          # raises UnsupportedGrantSyntax on anything it cannot model
    assert ledger.effective("runs") == {"select", "insert", "update"}               # never delete
    assert "runs" in ledger.known


def test_grants_are_in_this_file_table_level_only_and_scoped():
    grants = [s for s in STATEMENTS if s.lower().startswith(("grant ", "revoke "))]
    assert [g.lower() for g in grants] == ["revoke all on runs from minime_app",
                                           "grant select, insert, update on runs to minime_app"]
    for stmt in grants:
        low = stmt.lower()
        assert "(" not in low.split(" on ")[0], f"column-level privilege: {stmt}"
        assert "all tables in schema" not in low and "public" not in low


def test_grants_never_precede_the_creation_of_their_table():
    created = next(i for i, s in enumerate(STATEMENTS) if s.lower().startswith("create table if not exists runs "))
    touching = [i for i, s in enumerate(STATEMENTS) if re.match(r"(grant|revoke) .* on runs ", s.lower())]
    assert touching and min(touching) > created


def test_runs_can_never_be_deleted_by_the_app():
    assert not re.search(r"grant[^;]*\bdelete\b[^;]* on runs ", CODE)
    assert "revoke all on runs from minime_app;" in CODE


def test_no_do_blocks_no_default_privileges_no_rename():
    for stmt in STATEMENTS:
        low = stmt.lower()
        assert not low.startswith("do "), stmt[:60]
        assert not low.startswith("alter default privileges")
        assert " rename to " not in low
    functions = "\n".join(s for s in STATEMENTS if s.lower().startswith("create or replace function"))
    assert not re.search(r"\b(grant|revoke)\b", functions.lower())


def test_no_foreign_keys_and_no_link_to_other_tables():
    assert not re.search(r"\breferences\s+\w+\s*\(|foreign\s+key\s*\(", CODE, re.I)
    assert "auth.users" not in CODE


# ---------------------------------------------------------------------------
# The trigger: what the database enforces whatever the caller does
# ---------------------------------------------------------------------------

def test_the_guard_is_a_plain_function_and_a_before_update_row_trigger():
    assert any(s.lower().startswith("create or replace function runs_guard_update() returns trigger")
               for s in STATEMENTS)
    assert "drop trigger if exists runs_guard_update on runs;" in CODE
    assert re.search(r"create trigger runs_guard_update\s+before\s+update\s+on\s+runs\s+for\s+each\s+row"
                     r"\s+execute\s+function\s+runs_guard_update\(\)", CODE)


def test_the_guard_pins_identity_freezes_terminal_runs_and_keeps_counters_monotonic():
    guard = next(s for s in STATEMENTS if "create or replace function runs_guard_update" in s.lower())
    for column in ("id", "user_id", "device_id", "profile", "created_at"):
        assert f"new.{column} is distinct from old.{column}" in guard
    for column in ("state", "ended_at", "error_code"):
        assert f"new.{column} is distinct from old.{column}" in guard      # frozen once terminal
    assert "old.state in ('completed', 'cancelled', 'failed', 'expired')" in guard
    assert "new.steps < old.steps" in guard and "new.credits < old.credits" in guard
    messages = re.findall(r"raise exception '([^']*)'", guard)
    assert messages and all("%" not in m for m in messages)                # never a row value
    assert " detail " not in guard.lower() and " hint " not in guard.lower()
    # counters stay writable after a run ends (a late model call is still recorded)
    assert "new.steps" in guard and "old.steps" in guard
    terminal_block = guard[guard.index("old.state in"):guard.index("new.steps < old.steps")]
    assert "steps" not in terminal_block and "credits" not in terminal_block


def test_ended_at_is_set_if_and_only_if_the_state_is_terminal():
    assert re.search(r"constraint runs_ended_iff_terminal\s+check\s*\(\s*\(state in \('completed', 'cancelled', "
                     r"'failed', 'expired'\)\) = \(ended_at is not null\)\s*\)", CODE)


# ---------------------------------------------------------------------------
# Agreement with runs.py and with protocol/v1
# ---------------------------------------------------------------------------

def _quoted_list(text):
    return re.findall(r"'([a-z_]+)'", text)


def test_the_state_check_lists_exactly_the_states_runs_py_knows():
    from codeagent import runs
    state_rest = next(r for n, _t, r in COLUMNS if n == "state")
    listed = _quoted_list(state_rest.split("check", 1)[1])
    assert set(listed) == set(runs.RUN_STATES) and len(listed) == len(runs.RUN_STATES)
    assert "default 'created'" in state_rest
    terminal_check = re.search(r"\(state in \(([^)]*)\)\) = \(ended_at is not null\)", CODE).group(1)
    assert set(_quoted_list(terminal_check)) == set(runs.TERMINAL_STATES)
    guard = next(s for s in STATEMENTS if "create or replace function runs_guard_update" in s.lower())
    in_guard = re.search(r"old\.state in \(([^)]*)\)", guard).group(1)
    assert set(_quoted_list(in_guard)) == set(runs.TERMINAL_STATES)


def test_the_id_check_equals_the_protocols_run_id_pattern():
    common = BACKEND.parent / "protocol" / "v1" / "common.json"
    if not common.exists():
        pytest.skip("protocol/ is not next to backend/ in this layout")
    pattern = json.loads(common.read_text())["$defs"]["run_id"]["pattern"]
    id_rest = next(r for n, _t, r in COLUMNS if n == "id")
    assert f"check (id ~ '{pattern}')" in id_rest


def test_runs_py_logging_names_only_safe_values():
    path = BACKEND / "codeagent" / "runs.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert scan(tree, "codeagent/runs.py") == []


# ---------------------------------------------------------------------------
# Safe to run by hand
# ---------------------------------------------------------------------------

def test_safe_to_run_by_hand():
    assert not re.search(r"^\s*\\", SQL, re.M)                # no psql meta-commands
    assert not re.search(r"\b(begin|commit|rollback)\s*;", CODE, re.I)
    assert "Supabase SQL editor" in SQL and "more than once" in SQL
    for stmt in re.findall(r"create (?:table|index) (?:if not exists )?\w+", CODE):
        assert "if not exists" in stmt
    assert "drop trigger if exists runs_guard_update on runs;" in CODE
