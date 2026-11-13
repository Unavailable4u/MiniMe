"""
backend/tests/canary/scanners.py -- Patch Q06: the scanners behind the "no content at rest" canary test.

WHY THIS FILE EXISTS
--------------------
The trust statement of the product (build plan section O1, 11.3, 11.4) is that the server keeps no prompt, no file
content, no path, no command and no tool output once a run is over, and never writes any of them to Postgres, to a
log, to an error report or to a metric.  A test of that statement is only worth something if a green result cannot be
produced by a scanner that looks in the wrong place or for the wrong shape.  So this module has two halves, and the
tests in this package use both:

  * the NEEDLES: for every planted canary, every form it commonly takes when it passes through a serializer (raw, the
    random token alone, JSON-escaped, JSON-in-JSON, URL-encoded, base64 and base64url at all three byte alignments,
    hex, Python escapes, UTF-16), the two halves of the random token (what is left when a canary is cut in two by a line
    break or a log prefix) and a "squashed" form that survives being wrapped;
  * the SCANNERS: one per place content could come to rest (Postgres rows, every SQL statement and parameter that a
    recording fake saw, application logs, Redis keys and values, error-reporting payloads, the usage ledger, and the
    device's own store, which is ALLOWED to hold content).  Each takes the thing to look at and returns hits; nothing in
    here asserts by itself, `assert_clean` does.

test_scanner_selftest.py plants a canary in every one of those places on purpose and requires a hit, in every encoded
form, so a scanner that stopped looking cannot go unnoticed.

CANARIES
--------
A canary is made at run time (`make_canary`): never a literal, so this repository's gitleaks scan has nothing to flag
and no two runs share one.  It is `cnry-<label>-<16 random hex chars>`.  An "awkward" canary also carries a space, both
kinds of quote, a backslash and a non-ASCII letter, which is what makes the JSON-escaped, URL-encoded and Python-escaped
forms differ from the raw one (a plain identifier is identical in all of them and would test nothing).  Plain canaries
exist because some places cannot hold awkward text (a file name, a shell word).

WHAT A HIT IS
-------------
Any needle of any canary found in the thing scanned.  Matching is case-sensitive except for the forms whose letters
have no meaning in their case (hex, percent escapes).  Strings found inside byte values are searched as bytes and as
text, and a base64url record is also decoded and searched (a sealed buffer record decodes to ciphertext, which must
not contain a canary either).

Nothing in here prints more than a label and a short excerpt, and the excerpts only ever show a canary.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import sqlite3
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

AWKWARD_TAIL = " \"quoted\" 'single' back\\slash café"
MIN_SQUASHED = 16          # shorter squashed needles would be false positives


# ---------------------------------------------------------------------------------------------
# Canaries and their forms
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Canary:
    label: str
    value: str
    token: str

    def __repr__(self) -> str:                      # labels only: a failing assert must be readable, not noisy
        return f"Canary({self.label})"


def make_canary(label: str, *, awkward: bool = False) -> Canary:
    """A fresh random canary.  `awkward` adds characters that every serializer changes (see the module docstring)."""
    if not re.fullmatch(r"[a-z0-9_]{1,24}", label):
        raise ValueError("a canary label is 1-24 characters of a-z 0-9 _")
    token = secrets.token_hex(8)
    value = f"cnry-{label}-{token}" + (AWKWARD_TAIL if awkward else "")
    return Canary(label=label, value=value, token=token)


def _b64_needles(raw: bytes, *, urlsafe: bool) -> list[str]:
    """The stretches of base64(raw) that do not depend on what comes before `raw` in the encoded string: the text can sit
    at any of three byte alignments, so the encoding is made for each and the characters that mix in the prefix (or
    the padding at the end) are cut away."""
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    out = []
    for shift in (0, 1, 2):
        data = b"\x00" * shift + raw
        text = encode(data).decode("ascii").rstrip("=")
        start = -(-shift * 8 // 6)                              # ceil(8 * shift / 6): chars touched by the prefix
        end = len(text) - (1 if len(data) % 3 else 0)           # the last char holds padding bits
        if end - start >= 8:
            out.append(text[start:end])
    return out


def _squash(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text)


def forms(canary: Canary) -> dict[str, str]:
    """Every form of the canary that is looked for, by name.  Names are what a failure message shows."""
    value, raw = canary.value, canary.value.encode("utf-8")
    out: dict[str, str] = {
        "raw": value,
        "token": canary.token,
        "json": json.dumps(value)[1:-1],
        "json_ascii_off": json.dumps(value, ensure_ascii=False)[1:-1],
        "json_in_json": json.dumps(json.dumps(value))[1:-1],
        "python_repr": repr(value)[1:-1],
        "unicode_escape": value.encode("unicode_escape").decode("ascii"),
        "url_quote": urllib.parse.quote(value, safe=""),
        "url_quote_plus": urllib.parse.quote_plus(value),
        "url_double": urllib.parse.quote(urllib.parse.quote(value, safe=""), safe=""),
        "hex": raw.hex(),
        # the two halves of the random token: what is left when a canary is cut in two by a line break, a log prefix or
        # a chunk boundary (8 hex characters are one chance in four billion per window, so a hit is real)
        "fragment_head": canary.token[:8],
        "fragment_tail": canary.token[8:],
    }
    for i, needle in enumerate(_b64_needles(raw, urlsafe=False)):
        out[f"base64_{i}"] = needle
    for i, needle in enumerate(_b64_needles(raw, urlsafe=True)):
        out[f"base64url_{i}"] = needle
    for i, needle in enumerate(_b64_needles(canary.token.encode("ascii"), urlsafe=False)):
        out[f"token_base64_{i}"] = needle
    return out


_CASE_INSENSITIVE = ("hex", "url_quote", "url_quote_plus", "url_double")


@dataclass(frozen=True)
class Hit:
    where: str           # the place that was scanned, as the caller named it
    canary: str          # the canary's label
    form: str            # which form matched (raw, json, base64_1, squashed:raw, ...)
    excerpt: str         # a few characters around the match, escaped; only ever canary text and its neighbours

    def __str__(self) -> str:
        return f"{self.where}: canary {self.canary!r} found as {self.form} ...{self.excerpt}..."


# ---------------------------------------------------------------------------------------------
# The matcher
# ---------------------------------------------------------------------------------------------

class Scanner:
    """Looks for a fixed set of canaries in strings, bytes and nested containers."""

    def __init__(self, canaries: Iterable[Canary]) -> None:
        self.canaries = list(canaries)
        self._forms = {c.label: forms(c) for c in self.canaries}
        self._utf16 = {c.label: c.value.encode("utf-16-le") for c in self.canaries}
        self._squashed = {}
        for c in self.canaries:
            names = {}
            for name, needle in self._forms[c.label].items():
                s = _squash(needle)
                if len(s) >= MIN_SQUASHED:
                    names[name] = s
            self._squashed[c.label] = names

    # -- raw matching ----------------------------------------------------------------------
    def _in_text(self, where: str, text: str) -> list[Hit]:
        hits: list[Hit] = []
        lowered = None
        for c in self.canaries:
            for name, needle in self._forms[c.label].items():
                if name in _CASE_INSENSITIVE:
                    lowered = text.lower() if lowered is None else lowered
                    i = lowered.find(needle.lower())
                else:
                    i = text.find(needle)
                if i >= 0:
                    hits.append(Hit(where, c.label, name, _excerpt(text, i, len(needle))))
        squashed = None
        for c in self.canaries:
            for name, needle in self._squashed[c.label].items():
                squashed = _squash(text) if squashed is None else squashed
                i = squashed.find(needle)
                if i >= 0 and not any(h.canary == c.label and h.form == name for h in hits):
                    hits.append(Hit(where, c.label, "squashed:" + name, _excerpt(squashed, i, len(needle))))
        return hits

    def _in_bytes(self, where: str, data: bytes) -> list[Hit]:
        hits = self._in_text(where, data.decode("utf-8", errors="replace"))
        hits += self._in_text(where, data.decode("latin-1"))
        for c in self.canaries:
            if self._utf16[c.label] in data:
                hits.append(Hit(where, c.label, "utf16", "<bytes>"))
        # a base64url record ("<version>.<key id>.<payload>") is decoded and searched too
        for part in re.findall(rb"[A-Za-z0-9_-]{24,}={0,2}", data):
            try:
                decoded = base64.urlsafe_b64decode(part + b"=" * (-len(part) % 4))
            except (binascii.Error, ValueError):
                continue
            hits += self._in_text(where + " (decoded)", decoded.decode("utf-8", errors="replace"))
            hits += self._in_text(where + " (decoded)", decoded.decode("latin-1"))
        return _unique(hits)

    # -- public ----------------------------------------------------------------------------
    def scan(self, where: str, thing: Any) -> list[Hit]:
        """Hits in `thing`: a str, bytes, number, None, or any nesting of dicts (keys too), lists, tuples, sets,
        dataclasses' dicts and objects with `__dict__` (their attribute values)."""
        hits: list[Hit] = []
        for leaf in iter_leaves(thing):
            hits += self._in_bytes(where, leaf) if isinstance(leaf, (bytes, bytearray)) else self._in_text(where, leaf)
        return _unique(hits)

    def scan_joined(self, where: str, parts: Iterable[Any]) -> list[Hit]:
        """Like scan, but ALSO over the parts joined with a newline, which is what finds a canary split across two
        consecutive records (two log lines, two Redis values)."""
        parts = list(parts)
        hits = self.scan(where, parts)
        texts = [p if isinstance(p, str) else p.decode("utf-8", errors="replace") for p in parts
                 if isinstance(p, (str, bytes))]
        if texts:
            hits += self._in_text(where + " (joined)", "\n".join(texts))
        return _unique(hits)

    def assert_clean(self, where: str, thing: Any, *, joined: bool = False) -> None:
        hits = self.scan_joined(where, thing) if joined else self.scan(where, thing)
        if hits:
            lines = "\n  ".join(str(h) for h in hits[:12])
            more = f"\n  ... and {len(hits) - 12} more" if len(hits) > 12 else ""
            raise AssertionError(f"CONTENT AT REST: {len(hits)} canary hit(s)\n  {lines}{more}")


def _excerpt(text: str, index: int, length: int, around: int = 12) -> str:
    start, end = max(index - around, 0), min(index + length + around, len(text))
    return text[start:end].encode("unicode_escape").decode("ascii")[:120]


def _unique(hits: Sequence[Hit]) -> list[Hit]:
    """One hit per (place, canary, form), and no fragment hit where the same canary was found whole in that place."""
    whole = {(h.where, h.canary) for h in hits if not h.form.startswith(("fragment", "squashed:fragment"))}
    seen, out = set(), []
    for h in hits:
        if h.form.startswith(("fragment", "squashed:fragment")) and (h.where, h.canary) in whole:
            continue
        key = (h.where, h.canary, h.form)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def iter_leaves(thing: Any, _depth: int = 0) -> Iterator[Any]:
    """Every string and bytes value in a nested structure, dict keys included, plus numbers as text (a canary is never
    a number, but a leaked count of them in a key would be seen)."""
    if _depth > 40:
        yield repr(thing)
    elif thing is None or isinstance(thing, bool):
        return
    elif isinstance(thing, (str, bytes, bytearray)):
        yield thing
    elif isinstance(thing, (int, float)):
        yield str(thing)
    elif isinstance(thing, Mapping):
        for k, v in thing.items():
            yield from iter_leaves(k, _depth + 1)
            yield from iter_leaves(v, _depth + 1)
    elif isinstance(thing, (list, tuple, set, frozenset)):
        for v in thing:
            yield from iter_leaves(v, _depth + 1)
    elif hasattr(thing, "__dict__") and not isinstance(thing, type):
        yield from iter_leaves(vars(thing), _depth + 1)
    else:
        yield str(thing)


# ---------------------------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------------------------

# "<format version>.<key id>.<base64url(nonce + ciphertext + tag)>" -- codeagent/buffer.py, ENCRYPTION
SEALED = re.compile(r"^\d+\.[A-Za-z0-9_.-]{1,16}\.[A-Za-z0-9_-]{24,}$")


def redis_strings(redis_like: Any) -> list[str]:
    """Every key, hash field, hash value and list element a run-buffer fake holds right now."""
    return list(redis_like.all_strings())


def unsealed_values(redis_like: Any) -> list[str]:
    """The values of the buffer's `conv` and `replay` lists and its sealed `meta` field that are NOT in the sealed wire
    form.  While a run is active those are the only places content may be; anything in them that is not sealed is
    readable plaintext and is a finding on its own, canary or not."""
    bad = []
    for key in redis_like.keys():
        if key.endswith(":conv") or key.endswith(":replay"):
            for item in redis_like.get_list(key) or []:
                if not SEALED.match(item):
                    bad.append(f"{key}: a list element that is not a sealed record")
        elif key.endswith(":meta"):
            for field, value in (redis_like.get_hash(key) or {}).items():
                if field == "meta" and not SEALED.match(value):
                    bad.append(f"{key}: the stamp is not sealed")
                elif field != "meta" and not re.fullmatch(r"\d{1,12}", value):
                    bad.append(f"{key}: field {field!r} is not a plain counter")
    return bad


# ---------------------------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------------------------

def record_text(record: Any, formatter: Any = None) -> str:
    """Everything a log record can carry: the formatted message (arguments applied), the full traceback text with its
    exception messages, the exception's own message, and every extra attribute."""
    import logging

    fmt = formatter or logging.Formatter("%(name)s %(levelname)s %(message)s")
    parts = [fmt.format(record)]
    try:
        parts.append(record.getMessage())
    except Exception:                      # a bad %-format in a record must not hide the rest of it
        parts.append(str(record.msg))
    if getattr(record, "exc_info", None):
        parts.append(logging.Formatter().formatException(record.exc_info))
        parts.append(str(record.exc_info[1]))
    standard = set(vars(logging.LogRecord("x", 0, "x", 0, "x", (), None))) | {"message", "asctime"}
    for name, value in vars(record).items():
        if name not in standard:
            parts.append(f"{name}={value!r}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------------------------

_TEXTUAL = ("text", "character varying", "character", "json", "jsonb", "bytea", "ARRAY", "USER-DEFINED", "name",
            "citext", "xml", "uuid", "inet", "cidr", "tsvector", "hstore")
_SKIP_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def postgres_tables(conn: Any) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """[(schema, table, [(column, data_type)])] for every ordinary table in every schema but the system ones, read from
    information_schema, so a table nobody thought of is in the list.  `conn` is a psycopg connection."""
    tables = conn.execute(
        "select table_schema, table_name from information_schema.tables "
        "where table_type = 'BASE TABLE' and table_schema <> all(%s) and table_schema not like 'pg_%%' "
        "order by 1, 2", (list(_SKIP_SCHEMAS),)).fetchall()
    out = []
    for schema, table in tables:
        cols = conn.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = %s and table_name = %s order by ordinal_position", (schema, table)).fetchall()
        out.append((schema, table, [(c[0], c[1]) for c in cols]))
    return out


def scan_postgres(scanner: Scanner, conn: Any, *, where: str = "postgres") -> tuple[list[Hit], dict[str, int]]:
    """Scan every row of every table, every text, json, jsonb, bytea, array, enum and other text-like column
    (non-text columns, numbers and timestamps, are scanned as their text form too: a canary in a numeric column
    cannot exist, but a table whose columns changed type must not become invisible).  Returns (hits, {table: rows
    scanned}).  Run it as a role that is NOT held back by row-level security (the table owner), or half the rows are
    invisible and the result means nothing: `rows_visible_check` below is how the caller proves it is not."""
    from psycopg import sql

    hits: list[Hit] = []
    counts: dict[str, int] = {}
    for schema, table, columns in postgres_tables(conn):
        if not columns:
            continue
        pieces = []
        for name, kind in columns:
            ident = sql.Identifier(name)
            if kind == "bytea":
                pieces.append(sql.SQL("encode({c}, 'escape')").format(c=ident))
                pieces.append(sql.SQL("encode({c}, 'hex')").format(c=ident))
            else:
                pieces.append(sql.SQL("{c}::text").format(c=ident))
        query = sql.SQL("select {cols} from {t}").format(
            cols=sql.SQL(", ").join(pieces), t=sql.Identifier(schema, table))
        n = 0
        with conn.cursor(name=None) as cur:
            cur.execute(query)
            while True:
                rows = cur.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    n += 1
                    hits += scanner.scan(f"{where}:{schema}.{table}", [v for v in row if v is not None])
        counts[f"{schema}.{table}"] = n
    return _unique(hits), counts


def rows_visible_check(conn: Any) -> str | None:
    """None when this connection sees every row of every table (a superuser, a role with BYPASSRLS, or the owner of
    tables that do not FORCE row security); otherwise a sentence saying why a scan on it would be blind."""
    who, is_super, bypass = conn.execute(
        "select current_user, r.rolsuper, r.rolbypassrls from pg_roles r where r.rolname = current_user").fetchone()
    if is_super or bypass:
        return None
    blind = conn.execute(
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind = 'r' and c.relrowsecurity "
        "and (c.relforcerowsecurity or pg_get_userbyid(c.relowner) <> current_user)").fetchall()
    if blind:
        names = ", ".join(r[0] for r in blind[:6])
        return (f"role {who!r} is subject to row-level security on {len(blind)} table(s) ({names}...); "
                "scan with the table owner or a superuser (MINIME_TEST_SCAN_DATABASE_URL)")
    return None


# ---------------------------------------------------------------------------------------------
# Recorded SQL (fake mode) and the device's own store
# ---------------------------------------------------------------------------------------------

def sql_traffic_parts(statements: Iterable[Mapping[str, Any]]) -> list[Any]:
    """The things to scan in recorded SQL: each statement's text AND each of its parameters (and what the statement
    returned, when the recorder kept it)."""
    parts: list[Any] = []
    for s in statements:
        parts.append(s.get("sql", ""))
        parts.append(s.get("params"))
        if "returned" in s:
            parts.append(s["returned"])
    return parts


def sqlite_dump(conn: sqlite3.Connection) -> list[tuple[str, tuple]]:
    """Every row of every table of a SQLite connection (the device's own store), as (table, row)."""
    out = []
    names = [r[0] for r in conn.execute("select name from sqlite_master where type = 'table' and name not like 'sqlite_%'")]
    for name in names:
        for row in conn.execute(f'select * from "{name}"'):
            out.append((name, tuple(row)))
    return out
