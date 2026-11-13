"""
backend/tests/canary/test_scanner_selftest.py -- Patch Q06: proof that a green canary result cannot come from a scanner
that looks in the wrong place.

test_no_content_at_rest.py scans nine kinds of place.  Here a canary is planted, on purpose, in EACH of them:

  * through the real recorders of the harness wherever one exists (a log record on the server's loggers, a Redis
    command on the run buffer's twin and on run_guard's, a SQL statement or store call, an error report captured by
    the real SDK, a row in a real Postgres table when a database URL is set, a row in the device's SQLite store), and
  * through the Result record that the checks read, field by field (so a field that stopped being checked is caught).

and the matching check must fail, naming the place.  It is then repeated for every encoded form that commonly slips
through (JSON-escaped, JSON in JSON, URL-encoded, base64 and base64url at all three alignments, hex, Python escapes) and
for a canary split across two log lines or two Redis values.  Finally a CLEAN run must pass the same checks, so the
checks are not simply always failing.

Everything is made at run time (gitleaks): canaries are random strings, never literals.
"""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
import urllib.parse
import uuid
from dataclasses import fields

import pytest

from tests.canary import harness as h, scanners
from tests.canary.scanners import Scanner, make_canary
from tests.canary.test_no_content_at_rest import check_midrun, check_server_side


# ---------------------------------------------------------------------------------------------
# Encoded forms
# ---------------------------------------------------------------------------------------------

def _b64(shift: int, urlsafe: bool):
    enc = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return lambda v: enc(b"x" * shift + v.encode("utf-8")).decode("ascii") + "tail"


FORMS = {
    "raw": lambda v: v,
    "json": lambda v: json.dumps({"text": v}),
    "json_in_json": lambda v: json.dumps({"payload": json.dumps({"text": v})}),
    "json_ascii": lambda v: json.dumps({"text": v}, ensure_ascii=True),
    "url_quote": lambda v: "q=" + urllib.parse.quote(v),
    "url_quote_plus": lambda v: "q=" + urllib.parse.quote_plus(v),
    "url_twice": lambda v: "q=" + urllib.parse.quote(urllib.parse.quote(v, safe=""), safe=""),
    "base64_0": _b64(0, False), "base64_1": _b64(1, False), "base64_2": _b64(2, False),
    "base64url_0": _b64(0, True), "base64url_1": _b64(1, True), "base64url_2": _b64(2, True),
    "hex": lambda v: v.encode("utf-8").hex(),
    "python_repr": lambda v: repr({"text": v}),
    "unicode_escape": lambda v: v.encode("unicode_escape").decode("ascii"),
    "upper_hex": lambda v: v.encode("utf-8").hex().upper(),
}


@pytest.mark.parametrize("form", sorted(FORMS))
def test_every_encoded_form_is_found(form):
    canary = make_canary("selftest", awkward=True)
    other = make_canary("innocent", awkward=True)
    s = Scanner([canary])
    assert s.scan("probe", FORMS[form](canary.value)), f"the form {form!r} slips through the scanner"
    assert not s.scan("probe", FORMS[form](other.value)), "a different canary must not match"


@pytest.mark.parametrize("form", sorted(FORMS))
def test_every_encoded_form_is_found_in_bytes(form):
    canary = make_canary("selftest", awkward=True)
    s = Scanner([canary])
    assert s.scan("probe", b"\x00\x01" + FORMS[form](canary.value).encode("utf-8") + b"\xff"), form


def test_canary_split_across_two_log_lines():
    canary = make_canary("selftest", awkward=True)
    s = Scanner([canary])
    for cut in range(1, len(canary.value)):
        lines = [f"2026-11-12 03:45:01 INFO app step {canary.value[:cut]}", f"2026-11-12 03:45:02 INFO app {canary.value[cut:]}"]
        assert s.scan_joined("logs", lines), f"a canary cut at {cut} of {len(canary.value)} slips through"


def test_canary_split_across_two_redis_values():
    canary = make_canary("selftest")
    s = Scanner([canary])
    half = len(canary.value) // 2
    values = [canary.value[:half], canary.value[half:]]
    assert s.scan_joined("redis", values)


def test_canary_inside_a_decoded_sealed_record():
    """A buffer value is '<version>.<key id>.<base64url>'; what is inside is searched too."""
    canary = make_canary("selftest")
    record = "1.k1." + base64.urlsafe_b64encode(b"\x07" * 12 + canary.value.encode() + b"\x01" * 16).decode().rstrip("=")
    assert Scanner([canary]).scan("redis", record)


def test_no_false_positive_on_ordinary_text():
    s = Scanner([make_canary("a"), make_canary("b", awkward=True)])
    text = "run=0f2c3b1a-1111-2222-3333-444455556666 tool=read_file status=ok credits=0.0123 cnry-other-deadbeef00000000"
    assert s.scan("probe", text) == []


# ---------------------------------------------------------------------------------------------
# Field by field: every field of the record the checks read is scanned
# ---------------------------------------------------------------------------------------------

def _empty_result(c: h.Canaries) -> h.Result:
    return h.Result(mode="fake", canaries=c, user_id="u", run_ids=["r"], redis_after=[], redis_written=[],
                    guard_after=[], guard_written=[], leftover_keys=[], statements=[], server_logs=[], device_logs="",
                    sentry=[], ledger_rows=[], local_store=[], local_files=[], twin_tables={}, db_scan=None,
                    run_states={"r": "cancelled"}, midrun={})


def _plant(field: str, value: str):
    return {
        "redis_after": [value], "redis_written": [("SET", "k", value)], "guard_after": [value],
        "guard_written": [("set", "k", value)], "statements": [{"sql": "insert into x values (%s)", "params": [value]}],
        "server_logs": [f"codeagent.loop ERROR a line {value}"], "device_logs": f"line one\n{value}\n",
        "sentry": [json.dumps({"message": value}).encode()],
        "ledger_rows": [{"credits": 0.1, "note": value}], "twin_tables": {"runs": [{"title": value}]},
    }[field]


CHECKED = ["redis_after", "redis_written", "guard_after", "guard_written", "statements", "server_logs", "device_logs",
           "sentry", "ledger_rows", "twin_tables"]
NOT_CONTENT_CHECKED = {"mode", "canaries", "user_id", "run_ids", "leftover_keys", "run_states", "midrun", "db_scan",
                       "local_store", "local_files"}


def test_the_result_record_has_no_unchecked_field():
    """A new field on the record must be listed here as checked or as deliberately not content-checked."""
    names = {f.name for f in fields(h.Result)}
    assert names == set(CHECKED) | NOT_CONTENT_CHECKED, f"classify these fields: {names ^ (set(CHECKED) | NOT_CONTENT_CHECKED)}"


@pytest.mark.parametrize("field", CHECKED)
@pytest.mark.parametrize("which", ["prompt", "file_text", "tool_output", "reply", "path", "command", "instructions",
                                   "edit", "fault"])
def test_a_canary_in_each_checked_field_fails_the_check(field, which):
    c = h.Canaries()
    r = _empty_result(c)
    check_server_side(r)                                              # clean: passes
    setattr(r, field, _plant(field, getattr(c, which).value))
    with pytest.raises(AssertionError, match="CONTENT AT REST"):
        check_server_side(r)


@pytest.mark.parametrize("form", sorted(FORMS))
@pytest.mark.parametrize("field", CHECKED)
def test_an_encoded_canary_in_each_checked_field_fails_the_check(field, form):
    c = h.Canaries()
    r = _empty_result(c)
    setattr(r, field, _plant(field, FORMS[form](c.reply.value)))
    with pytest.raises(AssertionError, match="CONTENT AT REST"):
        check_server_side(r)


def test_the_device_store_is_scanned_and_may_hold_content():
    """The one place content is ALLOWED: the scanner must find it there (a scanner that cannot would prove nothing)."""
    c = h.Canaries()
    db = sqlite3.connect(":memory:")
    db.execute("create table events (seq integer, data text)")
    db.execute("insert into events values (1, ?)", (json.dumps({"text": c.prompt.value}),))
    hits = c.scanner().scan("device store", scanners.sqlite_dump(db))
    assert {x.canary for x in hits} == {"prompt"}
    assert c.scanner().scan("device store", scanners.sqlite_dump(sqlite3.connect(":memory:"))) == []


def test_the_midrun_check_rejects_plaintext_and_missing_expiry():
    c = h.Canaries()
    base = {"strings": ["run_buffer:r:conv"], "unsealed": [], "ttls": {"run_buffer:r:conv": 100}, "written": ["x"]}
    check_midrun(dict(base), c)                                       # clean: passes
    for patch, why in [({"strings": ["run_buffer:r:conv", c.prompt.value]}, "plaintext value"),
                       ({"written": [c.reply.value]}, "plaintext command"),
                       ({"unsealed": ["run_buffer:r:conv: a list element that is not a sealed record"]}, "unsealed"),
                       ({"ttls": {"run_buffer:r:conv": -1}}, "key without expiry"),
                       ({"ttls": {"run_buffer:r:conv": 10 ** 9}}, "ttl beyond the cap"),
                       ({"strings": []}, "empty snapshot")]:
        with pytest.raises(AssertionError):
            check_midrun({**base, **patch}, c)


# ---------------------------------------------------------------------------------------------
# Through the real recorders of a live stack
# ---------------------------------------------------------------------------------------------

@pytest.fixture
def stack(monkeypatch, tmp_path):
    s = h.Stack(monkeypatch, tmp_path)
    yield s
    s.close()


def _collect(stack: h.Stack) -> h.Result:
    return stack.collect(None, [], None)


def test_clean_stack_passes_the_checks(stack):
    check_server_side(_collect(stack))


@pytest.mark.parametrize("how", ["message", "argument", "extra", "exception_text", "exception_chain", "name"])
def test_planted_in_server_log_record(stack, how):
    value = stack.c.prompt.value
    log = logging.getLogger("codeagent.loop")
    if how == "message":
        log.error("[loop] something %s", value)
    elif how == "argument":
        log.warning("[loop] run=%s error=%s", "r1", value)
    elif how == "extra":
        log.info("[loop] fine", extra={"payload": value})
    elif how == "exception_text":
        try:
            raise RuntimeError(value)
        except RuntimeError:
            log.exception("[loop] failed")
    elif how == "exception_chain":
        try:
            try:
                raise RuntimeError(value)
            except RuntimeError as inner:
                raise ValueError("outer") from inner
        except ValueError:
            log.error("[loop] failed", exc_info=True)
    else:
        logging.getLogger("codeagent." + stack.c.path.value.replace("-", "_")).info("x")
    with pytest.raises(AssertionError, match="server logs"):
        check_server_side(_collect(stack))


def test_planted_in_device_log_stream(stack):
    stack.device_stream.write(f"2026-11-12 m2c WARNING {stack.c.tool_output.value}\n")
    with pytest.raises(AssertionError, match="device logs"):
        check_server_side(_collect(stack))


def test_the_real_device_logging_filter_is_in_the_loop(stack):
    """The device's own logging is configured the real way: a record with content must not reach the stream (so a
    green device-log scan means the filter works, not that nothing was logged)."""
    logging.getLogger("m2c.link").warning("x %s", stack.c.prompt.value)
    assert stack.c.prompt.token not in stack.device_stream.getvalue()


def test_planted_in_run_buffer_redis_command(stack):
    stack.redis.command("RPUSH", "run_buffer:selftest:conv", stack.c.reply.value)
    with pytest.raises(AssertionError, match="redis"):
        check_server_side(_collect(stack))


def test_planted_value_deleted_straight_away_is_still_seen(stack):
    stack.redis.command("RPUSH", "k", stack.c.reply.value)
    stack.redis.command("DEL", "k")
    assert "k" not in stack.redis.keys()                       # gone from the store; only the command log remembers
    with pytest.raises(AssertionError, match="every command argument"):
        check_server_side(_collect(stack))


def test_planted_in_run_guard_redis(stack):
    stack.guard.set("run_guard:selftest", stack.c.prompt.value)
    with pytest.raises(AssertionError, match="run guard"):
        check_server_side(_collect(stack))


def test_planted_in_bus_redis(stack):
    from memory import bus
    bus.redis.set("some:key", json.dumps({"text": stack.c.file_text.value}))
    with pytest.raises(AssertionError, match="redis"):
        check_server_side(_collect(stack))


def test_planted_in_sql_parameter(stack):
    from eo import db
    with db.cursor(user_id="u") as cur:
        cur.execute("select %s::text as planted", (stack.c.prompt.value,))
    with pytest.raises(AssertionError, match="recorded SQL"):
        check_server_side(_collect(stack))


def test_planted_in_sql_statement_text(stack):
    from eo import db
    with db.cursor(user_id="u") as cur:
        cur.execute(f"select '{stack.c.path.value}'::text as planted")
    with pytest.raises(AssertionError, match="recorded SQL"):
        check_server_side(_collect(stack))


def test_planted_in_store_call(stack):
    if stack.mode == "real":
        pytest.skip("in real mode the store writes SQL, covered by the statement tests")
    stack.statements.add("runs.insert", {"kwargs": {"cwd_label": stack.c.path.value}})
    with pytest.raises(AssertionError, match="recorded SQL"):
        check_server_side(_collect(stack))


def test_planted_in_ledger_row(stack):
    if stack.mode == "real":
        pytest.skip("real mode scans the table itself, covered by the database tests")
    stack.ledger_inner.rows.append({"user_id": stack.user_id, "credits": 0.5, "provider": stack.c.reply.value})
    with pytest.raises(AssertionError, match="usage ledger"):
        check_server_side(_collect(stack))


def test_planted_in_device_table_of_the_server(stack):
    if stack.mode == "real":
        pytest.skip("fake-mode twin tables only")
    stack.rig.store.devices[stack.device_id] = {"name": stack.c.instructions.value}
    with pytest.raises(AssertionError, match="run store and device tables"):
        check_server_side(_collect(stack))


@pytest.mark.parametrize("how", ["capture_message", "capture_exception", "breadcrumb", "extra_context", "tag"])
def test_planted_in_error_report(stack, how):
    import sentry_sdk
    value = stack.c.prompt.value
    if how == "capture_message":
        sentry_sdk.capture_message(value)
    elif how == "capture_exception":
        try:
            raise RuntimeError(f"failed with {value}")
        except RuntimeError as exc:
            sentry_sdk.capture_exception(exc)
    elif how == "breadcrumb":
        sentry_sdk.add_breadcrumb(category="x", message=value)
        sentry_sdk.capture_message("an error")
    elif how == "extra_context":
        with sentry_sdk.new_scope() as scope:
            scope.set_extra("request", {"body": value})
            sentry_sdk.capture_message("an error")
    else:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("t", value[:60])
            sentry_sdk.capture_message("an error")
    assert stack.sentry.error_events > 0
    with pytest.raises(AssertionError, match="error-reporting"):
        check_server_side(_collect(stack))


@pytest.mark.parametrize("form", ["raw", "json", "url_quote", "base64_1", "hex", "json_in_json"])
@pytest.mark.parametrize("place", ["log", "redis", "sql", "sentry"])
def test_encoded_forms_through_the_real_recorders(stack, place, form):
    value = FORMS[form](stack.c.reply.value)
    if place == "log":
        logging.getLogger("codeagent.router").error("[router] %s", value)
    elif place == "redis":
        stack.redis.command("RPUSH", "run_buffer:selftest:replay", value)
    elif place == "sql":
        stack.statements.add("insert", [value])
    else:
        import sentry_sdk
        sentry_sdk.capture_message(value)
    with pytest.raises(AssertionError, match="CONTENT AT REST"):
        check_server_side(_collect(stack))


def test_split_across_log_lines_through_the_real_recorder(stack):
    v = stack.c.command.value
    log = logging.getLogger("codeagent.broker")
    log.warning("[broker] part one %s", v[:9])
    log.warning("[broker] part two %s", v[9:])
    with pytest.raises(AssertionError, match="server logs"):
        check_server_side(_collect(stack))


def test_unsealed_buffer_value_is_found(stack):
    stack.redis.command("RPUSH", "run_buffer:selftest:conv", "plain text, not a sealed record")
    mid = stack.midrun_snapshot()
    assert mid["unsealed"], "an unsealed list element in the buffer went unnoticed"
    with pytest.raises(AssertionError, match="unsealed"):
        check_midrun(mid, stack.c)


# ---------------------------------------------------------------------------------------------
# Real Postgres (only when MINIME_TEST_DATABASE_URL is set)
# ---------------------------------------------------------------------------------------------

@pytest.mark.skipif(h.MODE != "real", reason="MINIME_TEST_DATABASE_URL is not set (fake-recorder mode)")
class TestRealPostgresScanner:
    """A canary in a table nobody thought of, in each text-like column type, and in the app's own tables, through the
    same scan the scenarios use."""

    @pytest.fixture
    def conn(self):
        import psycopg
        with psycopg.connect(h.SCAN_DSN, autocommit=True) as conn:
            assert scanners.rows_visible_check(conn) is None, "the scan connection would be blind"
            yield conn

    @pytest.mark.parametrize("column_type,literal_cast", [
        ("text", "%s"), ("varchar(500)", "%s"), ("jsonb", "to_jsonb(%s::text)"), ("json", "to_json(%s::text)"),
        ("bytea", "convert_to(%s, 'UTF8')"), ("text[]", "array[%s]"), ("uuid[]", None)])
    def test_unknown_table_every_column_type(self, conn, column_type, literal_cast):
        if literal_cast is None:
            pytest.skip("a uuid array cannot hold text")
        canary = make_canary("selftest", awkward=True)
        name = f"zz_canary_selftest_{uuid.uuid4().hex[:8]}"
        conn.execute(f'create table public."{name}" (id int, payload {column_type})')
        try:
            conn.execute(f'insert into public."{name}" values (1, {literal_cast})', (canary.value,))
            hits, counts = scanners.scan_postgres(Scanner([canary]), conn)
            assert counts[f"public.{name}"] == 1
            assert hits and all(x.where == f"postgres:public.{name}" for x in hits), hits
        finally:
            conn.execute(f'drop table public."{name}"')

    def test_column_name_and_encoded_forms(self, conn):
        canary = make_canary("selftest", awkward=True)
        name = f"zz_canary_selftest_{uuid.uuid4().hex[:8]}"
        conn.execute(f'create table public."{name}" (id int, a text)')
        try:
            for form in ("json", "url_quote", "base64_1", "hex", "json_in_json"):
                conn.execute(f'insert into public."{name}" values (1, %s)', (FORMS[form](canary.value),))
            hits, _ = scanners.scan_postgres(Scanner([canary]), conn)
            assert hits
            # a row holding only the label half of a canary is not a hit; a row holding the random token is
            # (fail closed: the token alone is enough to say the canary's content was written down)
            conn.execute(f'delete from public."{name}"')
            conn.execute(f'insert into public."{name}" values (1, %s)', ("line " + canary.value[:9],))
            hits, _ = scanners.scan_postgres(Scanner([canary]), conn)
            assert not hits, hits
            conn.execute(f'insert into public."{name}" values (2, %s)', ("line " + canary.value[9:],))
            hits, _ = scanners.scan_postgres(Scanner([canary]), conn)
            assert hits and all(x.form == "token" for x in hits), hits
        finally:
            conn.execute(f'drop table public."{name}"')

    def test_a_planted_row_in_the_runs_table_fails_the_real_check(self, stack, conn):
        # runs has no free-text column (profile, mode and error_code are pattern-checked), so the canary goes
        # in the id column, which accepts [A-Za-z0-9_-]{8,64}: a plain (non-awkward) canary fits.
        canary = make_canary("runsrow")
        user = stack.user_id
        conn.execute("insert into public.runs (id, user_id, device_id, profile, mode, state) "
                     "values (%s, %s, %s, 'code', 'ask', 'running')",
                     (canary.value, user, stack.device_id))
        try:
            hits, counts = scanners.scan_postgres(Scanner([canary]), conn)
            assert counts["public.runs"] >= 1 and hits
            assert all(x.where == "postgres:public.runs" for x in hits), hits
        finally:
            conn.execute("delete from public.runs where id = %s", (canary.value,))

    def test_every_table_of_the_database_is_in_the_scan(self, conn):
        scanned = {f"{s}.{t}" for s, t, _ in scanners.postgres_tables(conn)}
        listed = {r[0] + "." + r[1] for r in conn.execute(
            "select schemaname, tablename from pg_tables where schemaname not in ('pg_catalog', 'information_schema')")}
        assert scanned == listed

    def test_a_blind_connection_is_refused(self):
        import psycopg
        with psycopg.connect(h.DSN, autocommit=True) as app:
            blind = scanners.rows_visible_check(app)
        # the app role is subject to row-level security; unless it is the scan role itself the check must say so
        if h.SCAN_DSN != h.DSN:
            assert blind is not None and "row-level security" in blind
