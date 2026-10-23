"""B01 (M2C Phase 0): the SQL in the code-file store versus the grants in the migrations.

WHY THIS TEST EXISTS. The app connects as the low-privilege `minime_app` role
(migration 0003), and later migrations narrow what that role may do to
individual tables. Migration 0009 grants `SELECT, INSERT, DELETE` on
workspace_code_file_versions and then REVOKEs UPDATE, on purpose: a version row
is a fact about the past and is never rewritten. move_path() once ran
`UPDATE workspace_code_file_versions SET file_path = ...`; Postgres answered
"permission denied" and every move or rename in the Build tab returned a 500.
The unit tests use a fake cursor, so no grant was ever consulted and nothing
caught it. tests/integration/test_workspace_code_files_pg.py now catches it
against a real database, but only when someone points MINIME_TEST_DATABASE_URL
at one. THIS test needs no database: it reads the migrations, works out what
`minime_app` can do to each table, reads the SQL out of the Python module, and
fails when the two disagree. It runs in the default `pytest tests/static` run.

HOW IT WORKS (three small parts, all standard library).

  1. GrantLedger replays backend/migrations/*.sql in filename order and keeps
     the NET privileges `minime_app` holds on each table: a GRANT adds, a
     REVOKE removes, order matters (0003 grants UPDATE+DELETE on audit_log and
     revokes them again in the same file), and grants to PUBLIC count too
     because `minime_app` inherits them. Comments, quoted strings and
     $$-quoted blocks are skipped when splitting statements, so a `;` inside a
     comment never splits a statement.

  2. extract_sql() walks the module's AST and rebuilds every SQL string passed
     to .execute()/.executemany(). It does NOT just collect string constants:
     eo/workspace_code_files.py builds some statements by concatenating the
     module-level _NEXT_VERSION_SQL constant and by using f-strings for
     placeholder lists, and that inlined fragment carries its own SELECT on the
     versions table. Run-time-only pieces (a "%s, %s" placeholder list) become
     a marker. A statement whose text cannot be rebuilt at all fails the test
     rather than being skipped: an unchecked statement is not a passing one.

  3. required_privileges() reads one statement and lists (table, privilege)
     pairs it needs: INSERT, UPDATE and DELETE on their target tables, SELECT
     on tables read through FROM/JOIN, and UPDATE for the shapes Postgres also
     gates on UPDATE: `INSERT ... ON CONFLICT DO UPDATE` and the row-locking
     clauses `FOR UPDATE / FOR NO KEY UPDATE / FOR SHARE / FOR KEY SHARE`
     (all four confirmed to raise "permission denied" on PostgreSQL 16 when
     the role lacks UPDATE, which is why they are treated as "uses UPDATE").

If this test fails you have, in order of likelihood: (a) written SQL the role
may not run -- rewrite it (the move_path() fix copies history rows with INSERT
... SELECT and then DELETEs the old ones); (b) added a statement against a table
whose migration forgot a GRANT -- add the grant in a new migration; or (c) used
SQL syntax this parser does not model -- it fails closed and says so, extend it
here. To check another module's SQL, add its path (relative to backend/) to
SQL_MODULES below.

KNOWN LIMITS (deliberate, to keep this a guard and not a SQL parser):
  * Table-level privileges only. Column-level GRANTs, `GRANT ... ON ALL TABLES
    IN SCHEMA`, ALTER DEFAULT PRIVILEGES touching minime_app or PUBLIC, a table
    RENAME, and a GRANT/REVOKE hidden inside a DO block all raise
    UnsupportedGrantSyntax instead of being guessed at.
  * SELECT is checked for FROM/JOIN tables the migrations know about, not for
    comma-joins (`FROM a, b`) or column-level reads. The UPDATE/INSERT/DELETE
    checks, which are what this test exists for, do not depend on that.
  * Row-level security policies are not modelled; tests/integration/
    test_workspace_code_files_pg.py covers those against a real database.
"""
import ast
import pathlib
import re
from dataclasses import dataclass

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = BACKEND / "migrations"
APP_ROLE = "minime_app"

# Python modules whose SQL is checked against the migrations' grants. Paths are
# relative to backend/. Add a module here to put it under the same guard.
SQL_MODULES = [
    "eo/workspace_code_files.py",
    "eo/devices.py",
]

_DML = ("select", "insert", "update", "delete")

# Stands in for SQL text that only exists at run time (a "(%s, %s), (%s, %s)"
# placeholder list built with join()). Chosen so it can never parse as a table.
_DYN = "<?>"


class UnsupportedGrantSyntax(AssertionError):
    """A migration used GRANT/REVOKE-related syntax this test does not model.

    Raised (not ignored) so the answer is never silently "no grants": extend
    GrantLedger for the new syntax, then re-run."""


# ---------------------------------------------------------------------------
# 1. Reading the migrations
# ---------------------------------------------------------------------------

def split_statements(sql: str) -> list[str]:
    """Split SQL text on top-level semicolons into whitespace-collapsed statements.

    Skips `-- line` and `/* block */` comments (dropped), 'single-quoted'
    strings with '' escapes, "double-quoted" identifiers, and $tag$...$tag$
    dollar-quoted blocks (kept intact), so a ';' inside any of those does not
    split. Migration headers here are long prose full of semicolons, and 0003
    has a DO $$ ... $$ block, so a naive split(';') would mis-parse them."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            buf.append(" ")
            continue
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = re.compile(r"\$[A-Za-z_]*\$").match(sql, i)
            if m:
                j = sql.find(m.group(0), m.end())
                end = n if j == -1 else j + len(m.group(0))
                buf.append(sql[i:end])
                i = end
                continue
        if c == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return [" ".join(s.split()) for s in out if s.strip()]


def _ident(raw: str) -> str:
    """Normalise a table name: lower-case, no quotes, no leading `public.`."""
    name = raw.strip().strip(",").replace('"', "").lower()
    return name[len("public."):] if name.startswith("public.") else name


_NON_TABLE_OBJECTS = (
    "function", "procedure", "routine", "schema", "sequence", "database",
    "type", "domain", "language", "tablespace", "foreign", "large", "parameter",
)


class GrantLedger:
    """The net privileges `role` holds on each table after replaying migrations.

    `apply(sql_text, source)` is called once per migration file in filename
    order. `effective(table)` is what the role can actually do: privileges
    granted to the role directly UNION privileges granted to PUBLIC (a REVOKE
    from the role does not remove a grant to PUBLIC, as in Postgres).
    `known` is every table a migration creates or grants on, so the checker can
    tell "table exists, privilege missing" from "no migration mentions it".
    `last_change` remembers which migration last touched each (table, privilege)
    for the failure message."""

    def __init__(self, role: str = APP_ROLE):
        self.role = role
        self._held: dict[str, dict[str, set[str]]] = {role: {}, "public": {}}
        self.known: set[str] = set()
        self.last_change: dict[tuple[str, str], str] = {}

    def effective(self, table: str) -> set[str]:
        return set().union(*(h.get(table, set()) for h in self._held.values()))

    def apply(self, sql_text: str, source: str = "<sql>") -> None:
        for stmt in split_statements(sql_text):
            self._apply_statement(stmt, source)

    # -- one statement ------------------------------------------------------

    def _apply_statement(self, stmt: str, source: str) -> None:
        s = stmt.lower()
        m = re.match(r"create\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?(\S+)", s)
        if m:
            self.known.add(_ident(m.group(1).split("(")[0]))
            return
        m = re.match(r"drop\s+table\s+(?:if\s+exists\s+)?(.+?)(?:\s+(?:cascade|restrict))?$", s)
        if m:
            for name in m.group(1).split(","):
                t = _ident(name)
                self.known.discard(t)
                for held in self._held.values():
                    held.pop(t, None)
            return
        if re.match(r"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?\S+\s+rename\s+to\b", s):
            raise UnsupportedGrantSyntax(
                f"{source}: table RENAME is not modelled, so later grants/SQL "
                f"could be attributed to the wrong table: {stmt[:100]!r}")
        if s.startswith("alter default privileges") and (
                re.search(rf"\b{re.escape(self.role)}\b", s) or re.search(r"\bpublic\b", s)):
            raise UnsupportedGrantSyntax(
                f"{source}: ALTER DEFAULT PRIVILEGES for {self.role}/PUBLIC is not "
                f"modelled: {stmt[:100]!r}")
        if re.match(r"do\b", s) and re.search(r"\b(?:grant|revoke)\s", s):
            raise UnsupportedGrantSyntax(
                f"{source}: a GRANT/REVOKE inside a DO block is conditional and "
                f"is not modelled: {stmt[:100]!r}")

        m = re.match(
            r"grant\s+(.+?)\s+on\s+(.+?)\s+to\s+(.+?)"
            r"(?:\s+with\s+grant\s+option)?(?:\s+granted\s+by\s+\S+)?$", s)
        if m:
            self._change("GRANT", m.group(1), m.group(2), m.group(3), source, stmt)
            return
        m = re.match(
            r"revoke\s+(grant\s+option\s+for\s+)?(.+?)\s+on\s+(.+?)\s+from\s+(.+?)"
            r"(?:\s+(?:cascade|restrict))?$", s)
        if m and not m.group(1):
            self._change("REVOKE", m.group(2), m.group(3), m.group(4), source, stmt)

    def _change(self, verb: str, privs: str, targets: str, grantees: str,
                source: str, stmt: str) -> None:
        names = [g.strip().replace("group ", "") for g in grantees.split(",")]
        holders = [_ident(g) for g in names if _ident(g) in (self.role, "public")]
        tables = self._tables(targets, source, stmt)
        if tables is None:
            return                      # a function/schema/sequence grant: not a table privilege
        if "(" in privs:
            if holders:
                raise UnsupportedGrantSyntax(
                    f"{source}: column-level privileges are not modelled: {stmt[:100]!r}")
            return
        wanted: set[str] = set()
        for p in privs.split(","):
            p = p.strip()
            if p in ("all", "all privileges"):
                wanted |= set(_DML)
            elif p in _DML:
                wanted.add(p)
        for holder in holders:
            for t in tables:
                self.known.add(t)
                have = self._held[holder].setdefault(t, set())
                for p in wanted:
                    (have.add if verb == "GRANT" else have.discard)(p)
                    self.last_change[(t, p)] = f"{source} {verb}"

    @staticmethod
    def _tables(targets: str, source: str, stmt: str) -> list[str] | None:
        t = targets.strip()
        if re.match(r"all\s+tables\s+in\s+schema", t):
            raise UnsupportedGrantSyntax(
                f"{source}: GRANT ... ON ALL TABLES IN SCHEMA is not modelled: {stmt[:100]!r}")
        if re.match(r"all\s+(?:sequences|functions|procedures|routines)\s+in\s+schema", t):
            return None
        for word in _NON_TABLE_OBJECTS:
            if re.match(rf"{word}\b", t):
                return None
        t = re.sub(r"^table\s+", "", t)
        return [_ident(x) for x in t.split(",") if x.strip()]


def derive_grants(migrations_dir: pathlib.Path = MIGRATIONS_DIR, role: str = APP_ROLE) -> GrantLedger:
    """Replay every migrations/*.sql in filename (= numeric) order."""
    files = sorted(migrations_dir.glob("*.sql"))
    assert files, f"no migrations found in {migrations_dir}"
    ledger = GrantLedger(role)
    for path in files:
        ledger.apply(path.read_text(encoding="utf-8"), path.name)
    return ledger


# ---------------------------------------------------------------------------
# 2. Reading the Python module's SQL
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SqlStatement:
    module: str
    lineno: int
    sql: str
    resolved: bool = True     # False: the text could not be rebuilt statically


def _render(node: ast.AST, consts: dict[str, str]) -> str | None:
    """Best-effort static value of a string expression; None when unknowable.

    Handles literals (adjacent literals are already folded by the parser),
    f-strings, `a + b`, `"..." % x`, `"...".format(...)`, `sep.join([...] * n)`
    over a literal list, and names bound to a string constant (`consts` holds
    the module-level ones plus, inside a function, that function's own simple
    assignments). Any sub-expression it cannot know becomes _DYN so the
    surrounding SQL is still checked."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                r = _render(v.value, consts)
                parts.append(_DYN if r is None else r)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left, consts), _render(node.right, consts)
        if left is None and right is None:
            return None
        return (_DYN if left is None else left) + (_DYN if right is None else right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _render(node.left, consts)
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            return _render(node.func.value, consts)
        if node.func.attr == "join" and len(node.args) == 1:
            return _render_join(node.func.value, node.args[0], consts)
    return None


def _render_join(sep_node: ast.AST, arg: ast.AST, consts: dict[str, str]) -> str | None:
    """`sep.join([a, b])` or `sep.join([a] * n)` with a literal list: one copy of the
    repeated element is enough for privilege analysis (the batch upserts build their
    VALUES list this way, and each element embeds _NEXT_VERSION_SQL)."""
    items = None
    if isinstance(arg, (ast.List, ast.Tuple)):
        items = arg.elts
    elif isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mult):
        for side in (arg.left, arg.right):
            if isinstance(side, (ast.List, ast.Tuple)):
                items = side.elts
    if items is None:
        return None
    parts = [_render(i, consts) for i in items]
    if all(p is None for p in parts):
        return None
    sep = _render(sep_node, consts)
    return (", " if sep is None else sep).join(_DYN if p is None else p for p in parts)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in tree.body:
        target = value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and value is not None:
            rendered = _render(value, consts)
            if rendered is not None:
                consts[target.id] = rendered
    return consts


def _docstring_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for n in ast.walk(tree):
        if (isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)
                and isinstance(n.body[0].value.value, str)):
            ids.add(id(n.body[0].value))
    return ids


# Statement-SHAPED text, not just "starts with a SQL word": plain English such as
# "update this message later" must not count. A false positive here is harmless
# unless the text also names a table the migrations know, in which case it is
# real SQL.
_LOOKS_LIKE_SQL = re.compile(
    r"^\s*(?:select\b.+?\bfrom\b|insert\s+into\s+\S+|update\s+\S+(?:\s+(?:as\s+)?\w+)?\s+set\b"
    r"|delete\s+from\s+\S+|with\s+\w+\s+as\s*\()", re.I | re.S)


def _function_scopes(tree: ast.Module, consts: dict[str, str]) -> dict[int, dict[str, str]]:
    """id(function node) -> module constants plus that function's own string
    assignments, in source order. Needed because write_files() assembles its
    upsert from local variables (`upsert_row = "(...," + _NEXT_VERSION_SQL + "...)"`)
    before putting them in an f-string; without this the embedded SELECT on the
    versions table (and anything else hiding in a local) would be invisible."""
    scopes: dict[int, dict[str, str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope = dict(consts)
        assigns = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and isinstance(n.targets[0], ast.Name)]
        for a in sorted(assigns, key=lambda n: (n.lineno, n.col_offset)):
            rendered = _render(a.value, scope)
            if rendered is not None:
                scope[a.targets[0].id] = rendered
        scopes[id(fn)] = scope
    return scopes


def extract_sql(source: str, module: str = "<module>") -> list[SqlStatement]:
    """Every SQL statement in `source`: all .execute()/.executemany() first
    arguments (rebuilt, or flagged unresolved), plus any other string that is
    shaped like a statement (so SQL handed to a helper is still checked).
    Docstrings and comments are never statements."""
    tree = ast.parse(source)
    consts = _module_constants(tree)
    scopes = _function_scopes(tree, consts)
    docstrings = _docstring_ids(tree)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def scope_of(node: ast.AST) -> dict[str, str]:
        while node in parent:
            node = parent[node]
            if id(node) in scopes:
                return scopes[id(node)]
        return consts

    found: list[SqlStatement] = []
    covered: set[int] = set()

    def cover(node: ast.AST) -> None:
        covered.update(id(sub) for sub in ast.walk(node))

    for node in ast.walk(tree):          # breadth-first: parents before children
        if id(node) in covered or id(node) in docstrings:
            continue
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("execute", "executemany") and node.args):
            arg = node.args[0]
            cover(arg)
            text = _render(arg, scope_of(node))
            if text is None:
                found.append(SqlStatement(module, node.lineno, ast.unparse(arg)[:80], resolved=False))
            else:
                found.append(SqlStatement(module, arg.lineno, " ".join(text.split())))
        elif isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            text = _render(node, scope_of(node))
            if text and _LOOKS_LIKE_SQL.match(text):
                cover(node)
                found.append(SqlStatement(module, node.lineno, " ".join(text.split())))
    return sorted(found, key=lambda st: st.lineno)


# ---------------------------------------------------------------------------
# 3. What one statement needs, and the comparison
# ---------------------------------------------------------------------------

_ID = r'"?[a-z_][a-z0-9_$]*"?(?:\."?[a-z_][a-z0-9_$]*"?)?'


@dataclass(frozen=True)
class Need:
    table: str
    privilege: str      # lower-case: select / insert / update / delete
    why: str


def required_privileges(sql: str, known: set[str]) -> list[Need]:
    """The (table, privilege) pairs the statement(s) in `sql` need.

    `known` is the set of tables the migrations know about; it keeps FROM-clause
    noise like `extract(epoch from now())` from being read as a table."""
    needs: dict[tuple[str, str], Need] = {}

    def need(table: str, priv: str, why: str) -> None:
        needs.setdefault((table, priv), Need(table, priv, why))

    for text in split_statements(sql):
        s = re.sub(r"'(?:[^']|'')*'", "''", text.lower())     # literals can't name tables
        if re.search(rf"\b(?:insert\s+into|delete\s+from)\s+{re.escape(_DYN)}|\bupdate\s+{re.escape(_DYN)}", s):
            need(_DYN, "unknown", "the target table is only known at run time")

        inserts = [_ident(t) for t in re.findall(rf"\binsert\s+into\s+(?:only\s+)?({_ID})", s)]
        deletes = [_ident(t) for t in re.findall(rf"\bdelete\s+from\s+(?:only\s+)?({_ID})", s)]
        for t in inserts:
            need(t, "insert", "INSERT")
        for t in deletes:
            need(t, "delete", "DELETE")

        updates: list[tuple[str, str]] = []
        if re.search(r"\bon\s+conflict\b.*?\bdo\s+update\b", s):
            updates += [(t, "INSERT ... ON CONFLICT DO UPDATE") for t in inserts]
            s = re.sub(r"\bdo\s+update\b", " ", s)
        updates += [
            (_ident(t), "UPDATE")
            for t in re.findall(rf"\bupdate\s+(?:only\s+)?({_ID})(?:\s+(?:as\s+)?[a-z_]\w*)?\s+set\b", s)]
        for t, why in updates:
            need(t, "update", why)
            need(t, "select", f"{why} reads the existing row")

        stripped = re.sub(rf"\bdelete\s+from\s+(?:only\s+)?{_ID}", " ", s)
        reads = [t for t in (_ident(x) for x in re.findall(rf"\b(?:from|join)\s+({_ID})", stripped))
                 if t in known]
        for t in reads:
            need(t, "select", "SELECT (FROM/JOIN)")
        if re.search(r"\bfor\s+(?:no\s+key\s+update|update|key\s+share|share)\b", s):
            for t in reads:
                need(t, "update", "row-locking clause (FOR UPDATE / FOR SHARE) needs UPDATE")

        targets = inserts + deletes + [t for t, _ in updates]
        if re.search(r"\breturning\b", s) or (re.search(r"\bwhere\b", s) and (deletes or updates)):
            for t in targets:
                need(t, "select", "WHERE/RETURNING reads the table")
    return list(needs.values())


def check_statement(stmt: SqlStatement, ledger: GrantLedger) -> list[str]:
    """Human-readable problems for one statement; empty when it is allowed."""
    where = f"{stmt.module}:{stmt.lineno}"
    shown = stmt.sql if len(stmt.sql) <= 160 else stmt.sql[:157] + "..."
    if not stmt.resolved:
        return [f"{where}: cannot rebuild this SQL statically, so it cannot be checked "
                f"against the grants: {stmt.sql!r}. Pass a literal or a module-level "
                f"string constant to execute()."]
    problems = []
    for n in required_privileges(stmt.sql, ledger.known):
        if n.privilege == "unknown":
            problems.append(f"{where}: {n.why}, so its privileges cannot be checked. "
                            f"Statement: {shown}")
        elif n.table not in ledger.known:
            problems.append(
                f"{where}: statement uses table {n.table!r} ({n.why}) but no migration "
                f"creates it or grants {ledger.role} anything on it. Statement: {shown}")
        elif n.privilege not in ledger.effective(n.table):
            have = ", ".join(sorted(p.upper() for p in ledger.effective(n.table))) or "nothing"
            last = ledger.last_change.get((n.table, n.privilege), "never granted")
            problems.append(
                f"{where}: statement uses {n.privilege.upper()} on table {n.table!r} ({n.why}), "
                f"but role {ledger.role} lacks the {n.privilege.upper()} privilege there "
                f"(net grants from the migrations: {have}; last change to "
                f"{n.privilege.upper()}: {last}). Statement: {shown}")
    return problems


def check_module_source(source: str, module: str, ledger: GrantLedger) -> list[str]:
    problems: list[str] = []
    for stmt in extract_sql(source, module):
        problems += check_statement(stmt, ledger)
    return problems


# ===========================================================================
# Tests
# ===========================================================================

@pytest.fixture(scope="module")
def ledger() -> GrantLedger:
    return derive_grants()


@pytest.mark.parametrize("module", SQL_MODULES)
def test_module_sql_only_uses_privileges_the_migrations_grant(module, ledger):
    """The point of B01: every statement in each listed module is allowed for minime_app."""
    path = BACKEND / module
    source = path.read_text(encoding="utf-8")
    statements = extract_sql(source, module)
    assert statements, f"no SQL statements found in {module}; the extractor is broken or the list is stale"
    problems = check_module_source(source, module, ledger)
    assert not problems, "SQL the app role is not allowed to run:\n  " + "\n  ".join(problems)


def test_derived_grants_match_what_the_migrations_say(ledger):
    """Anchors on facts that must hold. If the parser ever returned nothing (or
    everything), the check above would pass vacuously; these stop that. If a
    migration legitimately changes one of them, update it here on purpose."""
    # 0009: select/insert/delete granted, update revoked -- the whole reason for B01.
    assert ledger.effective("workspace_code_file_versions") == {"select", "insert", "delete"}
    # 0007: the live-row table keeps full DML (the upserts rely on UPDATE).
    assert ledger.effective("workspace_code_files") == {"select", "insert", "update", "delete"}
    # 0003 grants update+delete on audit_log and revokes both again in the same file.
    assert ledger.effective("audit_log") == {"select", "insert"}
    # Later migrations (0008 / 0012) are append-only on the ledgers.
    assert ledger.effective("usage_ledger") == {"select", "insert"}
    assert ledger.effective("llm_call_log") == {"select", "insert"}
    assert "workspace_code_file_versions" in ledger.known
    assert ledger.last_change[("workspace_code_file_versions", "update")] == "0009_code_file_versions.sql REVOKE"


# --- the migration reader --------------------------------------------------

def test_split_statements_ignores_semicolons_in_comments_strings_and_dollar_blocks():
    sql = """
    -- a header; with a semicolon; in prose
    create table t (a text default 'x;y');   /* block; comment */
    do $$ begin perform 1; perform 2; end $$;
    grant select on t to minime_app;
    """
    assert split_statements(sql) == [
        "create table t (a text default 'x;y')",
        "do $$ begin perform 1; perform 2; end $$",
        "grant select on t to minime_app",
    ]


def test_ledger_replays_grants_and_revokes_in_order_and_counts_public():
    led = GrantLedger()
    led.apply("create table a (x int); grant select, insert, update, delete on a to minime_app;"
              "revoke update, delete on a from minime_app;", "0001")
    led.apply("create table b (x int); grant select on b to public;"
              "grant select, insert on b to minime_app; revoke select, insert on b from minime_app;", "0002")
    led.apply("grant all privileges on table public.c to minime_app, other_role;"
              "grant execute on function public.f() to minime_app;"
              "grant usage on schema public to minime_app;"
              "grant select on sequence s to minime_app;", "0003")
    assert led.effective("a") == {"select", "insert"}
    assert led.effective("b") == {"select"}        # PUBLIC's select survives the revoke from the role
    assert led.effective("c") == {"select", "insert", "update", "delete"}
    assert "f" not in led.known and "s" not in led.known and "public" not in led.known


@pytest.mark.parametrize("sql", [
    "grant update (content) on t to minime_app",
    "grant select on all tables in schema public to minime_app",
    "alter table t rename to u",
    "alter default privileges in schema public grant select on tables to minime_app",
    "do $$ begin grant update on t to minime_app; end $$",
])
def test_ledger_fails_closed_on_grant_syntax_it_does_not_model(sql):
    with pytest.raises(UnsupportedGrantSyntax):
        GrantLedger().apply(sql, "0099_test.sql")


# --- the SQL reader --------------------------------------------------------

def test_extract_sql_rebuilds_concatenated_fstring_and_constant_sql():
    src = '''
"""Docstring that says: update foo set x = 1 where y = 2."""
FRAG = "(select max(v) from versions where w = %s)"

def f(cur, rows):
    """Another docstring: delete from versions where 1 = 1."""
    cur.execute("insert into files (a, v) values (%s, " + FRAG + ")", (1, 2))
    marks = ", ".join(["(%s)"] * len(rows))
    cur.execute(f"""
        insert into versions (a) values {marks}
    """, rows)
    cur.execute("update versions "
                "set a = %s")
    cur.execute(f"insert into versions values {build_marks()}")
    cur.execute(some_variable)
    note = "update this message later"
'''
    got = extract_sql(src, "m.py")
    sqls = [(s.resolved, s.sql) for s in got]
    assert (True, "insert into files (a, v) values (%s, (select max(v) from versions where w = %s))") in sqls
    assert (True, "insert into versions (a) values (%s)") in sqls          # join([...] * n) -> one copy
    assert (True, f"insert into versions values {_DYN}") in sqls          # genuinely unknowable
    assert (True, "update versions set a = %s") in sqls
    assert [s for s in got if not s.resolved and s.sql == "some_variable"]
    assert len(got) == 5          # docstrings and the plain English string are not statements


def test_extract_sql_finds_the_real_modules_inlined_version_subquery():
    """Both upserts (single-row write_file and the batch write_files) embed
    _NEXT_VERSION_SQL, a SELECT on the versions table; the batch one reaches it
    through function-local variables and an f-string. Prove both are inlined and
    that nothing in the real module is left as an unresolved placeholder target."""
    src = (BACKEND / "eo" / "workspace_code_files.py").read_text(encoding="utf-8")
    found = extract_sql(src, "eo/workspace_code_files.py")
    upserts = [s for s in found
               if s.sql.startswith("insert into workspace_code_files") and "on conflict" in s.sql]
    assert len(upserts) == 2
    assert all("from workspace_code_file_versions" in s.sql for s in upserts)
    assert all(s.resolved and not s.sql.startswith("<") for s in found)
    assert not [s for s in found if re.search(rf"(into|from|update)\s+{re.escape(_DYN)}", s.sql)]


@pytest.mark.parametrize("sql, expected", [
    ("update versions set file_path = %s where workspace_id = %s",
     {("versions", "update"), ("versions", "select")}),
    ("insert into versions (a) values (%s)", {("versions", "insert")}),
    ("insert into versions (a) select a from versions where b = %s",
     {("versions", "insert"), ("versions", "select")}),
    ("delete from versions where a = %s", {("versions", "delete"), ("versions", "select")}),
    ("insert into versions (a) values (%s) on conflict (a) do update set a = excluded.a",
     {("versions", "insert"), ("versions", "update"), ("versions", "select")}),
    ("select a from versions where b = %s for update",
     {("versions", "select"), ("versions", "update")}),
    ("select a from versions where b = %s for key share",
     {("versions", "select"), ("versions", "update")}),
    ("select a from versions where b = %s", {("versions", "select")}),
    # an UPDATE that only appears in a comment or a string literal is not an UPDATE
    ("select a from versions -- update versions set a = 1\n where b = 'update versions set a = 2'",
     {("versions", "select")}),
    ("select extract(epoch from now()) from versions", {("versions", "select")}),
])
def test_required_privileges(sql, expected):
    got = {(n.table, n.privilege) for n in required_privileges(sql, {"versions", "files"})}
    assert got == expected


# --- the comparison, and the regression this test exists for ----------------

def test_the_old_update_file_path_is_rejected_with_a_message_that_names_everything(ledger):
    """Mutation proof, permanent: the exact statement move_path() used before the fix
    (git 0657054) must fail, and the message must say which statement, which table and
    which privilege, plus which migration took the privilege away."""
    old = '''
def move_path(cur, new_path, ws_id, old_path):
    cur.execute(
        "update workspace_code_file_versions set file_path = %s "
        "where workspace_id = %s and file_path = %s",
        (new_path, ws_id, old_path),
    )
'''
    problems = check_module_source(old, "eo/workspace_code_files.py", ledger)
    update_problems = [p for p in problems if "UPDATE on table 'workspace_code_file_versions'" in p]
    assert update_problems, problems
    msg = update_problems[0]
    assert "eo/workspace_code_files.py:" in msg
    assert "update workspace_code_file_versions set file_path = %s" in msg     # the statement
    assert "role minime_app lacks the UPDATE privilege" in msg                   # the privilege
    assert "DELETE, INSERT, SELECT" in msg                                       # the net grants
    assert "0009_code_file_versions.sql REVOKE" in msg                           # who took it away


@pytest.mark.parametrize("body", [
    # INSERT ... ON CONFLICT DO UPDATE on the history table is the same bug in another shape
    '"insert into workspace_code_file_versions (workspace_id, file_path, version) values (%s, %s, %s) '
    'on conflict (workspace_id, file_path, version) do update set content = excluded.content"',
    # so is locking the history rows
    '"select version from workspace_code_file_versions where workspace_id = %s for update"',
    # and writing to an append-only ledger
    '"update usage_ledger set credits = 0 where id = %s"',
])
def test_other_ways_of_using_update_on_a_no_update_table_are_rejected(body, ledger):
    problems = check_module_source(f"def f(cur):\n    cur.execute({body}, ())\n", "m.py", ledger)
    assert any("UPDATE on table" in p for p in problems), problems


def test_an_unresolvable_execute_argument_fails_instead_of_being_skipped(ledger):
    problems = check_module_source("def f(cur, q):\n    cur.execute(q)\n", "m.py", ledger)
    assert problems and "cannot rebuild this SQL statically" in problems[0]


def test_a_table_no_migration_knows_about_fails_closed(ledger):
    problems = check_module_source(
        'def f(cur):\n    cur.execute("insert into brand_new_table (a) values (%s)", (1,))\n', "m.py", ledger)
    assert problems and "no migration creates it or grants minime_app anything on it" in problems[0]
