#!/usr/bin/env python3
"""
codebase_census.py - measure everything about a repo, from the LOCAL working copy.

Usage (from the repo root):   python scripts/codebase_census.py
Output:                       graphify-out/census.json   (git-ignored)

Reads only source structure + git metadata. It never records file contents,
secret values or .env values (only variable NAMES). Standard library only;
if `tree-sitter` + `tree-sitter-javascript` are installed (Graphify installs
them) JS/JSX is parsed exactly, otherwise a heuristic fallback is used.

script_version 2 (additive; every v1 field keeps its meaning) adds, per symbol:
  doc1 (first docstring/JSDoc sentence, <= 200 chars), params, returns, raises, calls
  ([name, "path::qual" | null] pairs), hash (10 hex of the whitespace-normalised source),
  visibility (public | private | dunder), hooks_used / exported (JS), qual + method_names +
  hash (classes). Per file: module_doc up to 1500 chars, exports (Python). Top level:
  table_refs (SQL table -> files whose code mentions it) and git.head_full (40 hex).
Join key for a symbol is "<path>::<qual>". When a file defines the same qual twice
(property setters, conditional redefinitions) the later ones become qual~2, qual~3 ... in
source order. Parameter defaults are recorded only when they are short literals or bare
names and the parameter name is not secret-looking; otherwise they are the string "...".
"""
import ast, builtins, collections, datetime, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "graphify-out" / "census.json"
T0 = time.time()
LANG = {"py": "python", "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
        "ts": "typescript", "tsx": "typescript", "md": "markdown", "json": "json", "sql": "sql", "yml": "yaml",
        "yaml": "yaml", "html": "html", "css": "css", "sh": "shell", "ps1": "shell", "toml": "toml", "txt": "text"}
HTTP = {"get", "post", "put", "patch", "delete", "options", "head"}
SECRETISH = re.compile(r"key|secret|token|passw|credential", re.I)
SCRIPT_VERSION = 2
SECRET_VALUE = re.compile(r"sk-|ghp_|gho_|xox[abpr]-|AKIA|eyJ[A-Za-z0-9]|-----BEGIN|://[^/\s]*:[^/\s@]*@")
BUILTIN_NAMES = set(dir(builtins))
STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__", "typing_extensions"}
DOC1_MAX = 200
MODULE_DOC_MAX = 1500
ELLIPSIS = "\u2026"

try:
    import tree_sitter_javascript as _tsjs
    from tree_sitter import Language, Parser
    JS_PARSER = Parser(Language(_tsjs.language()))
except Exception:
    JS_PARSER = None


def git(*args):
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def list_files():
    out = git("ls-files", "-co", "--exclude-standard", "-z")
    files = [f for f in out.split("\0") if f]
    if files:
        return sorted(set(files))
    skip = {".git", "node_modules", "venv", ".venv", "__pycache__", ".next", "graphify-out"}
    res = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in skip]
        res += [os.path.relpath(os.path.join(dp, f), ROOT).replace("\\", "/") for f in fn]
    return sorted(res)


def read_text(rel):
    try:
        b = (ROOT / rel).read_bytes()
    except OSError:
        return None, 0
    if b"\0" in b[:4096]:
        return None, len(b)
    return b.decode("utf-8", errors="replace"), len(b)


def is_test_path(p):
    n = p.rsplit("/", 1)[-1]
    return "/tests/" in "/" + p or n.startswith("test_") or ".test." in n or n.endswith("_test.py") or "/__tests__/" in p


def prefix_sums(kinds):
    s = [0]
    for k in kinds:
        s.append(s[-1] + (1 if k == "k" else 0))
    return s


# ---------------------------------------------------------------- v2 helpers (shared)
def norm_hash(lines, start, end):
    """10 hex of sha1 over the whitespace-normalised source lines start..end (1-based, inclusive)."""
    seg = " ".join(lines[max(start - 1, 0):end])
    return hashlib.sha1(re.sub(r"\s+", " ", seg).strip().encode("utf-8")).hexdigest()[:10]


def clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + ELLIPSIS


_ABBREV = {"e.g", "i.e", "vs", "etc", "cf", "approx", "eg", "ie", "no", "fig"}


def first_sentence(text, limit=DOC1_MAX):
    """First sentence of the first paragraph, whitespace collapsed, <= limit chars (None if empty)."""
    if not text or not text.strip():
        return None
    para = re.sub(r"\s+", " ", re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]).strip()
    end = len(para)
    for m in re.finditer(r"[.!?](?=\s|$)", para):
        head = para[:m.start()].rsplit(" ", 1)[-1].lower()
        if head in _ABBREV:
            continue
        end = m.end()
        break
    out = para[:end].strip()
    return clip(out, limit) if out else None


def visibility(name):
    if name.startswith("__") and name.endswith("__") and len(name) > 4:
        return "dunder"
    return "private" if name.startswith("_") else "public"


def uniquify(items):
    """Later duplicates of the same qual inside one file become qual~2, qual~3 ... (source order)."""
    seen = collections.Counter()
    for it in sorted(items, key=lambda i: i["start"]):
        q = it["qual"]
        seen[q] += 1
        if seen[q] > 1:
            it["qual"] = f"{q}~{seen[q]}"


def dotted(node):
    """'a.b.c' for a Name/Attribute chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


_SKIP_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def walk_body(fn):
    """Nodes of a function body in source order, not descending into nested defs/classes/lambdas."""
    stack = list(reversed(fn.body))
    while stack:
        n = stack.pop()
        if isinstance(n, _SKIP_DEFS):
            continue
        yield n
        stack.extend(reversed(list(ast.iter_child_nodes(n))))


def py_default(node, pname):
    if SECRETISH.search(pname):
        return ELLIPSIS
    if isinstance(node, ast.Constant):
        txt = repr(node.value)
    elif isinstance(node, ast.Name):
        txt = node.id
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) and isinstance(node.operand, ast.Constant):
        txt = unp(node, 40)
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)) and not node.elts:
        txt = unp(node, 40)
    elif isinstance(node, ast.Dict) and not node.keys:
        txt = "{}"
    else:
        return ELLIPSIS
    return ELLIPSIS if (len(txt) > 40 or SECRET_VALUE.search(txt)) else txt


def py_params(a):
    pos = list(a.posonlyargs) + list(a.args)
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)

    def one(arg, dflt, kind=None, prefix=""):
        p = {"name": prefix + arg.arg}
        if arg.annotation is not None:
            p["annotation"] = unp(arg.annotation, 80)
        if dflt is not None:
            p["default"] = py_default(dflt, arg.arg)
        if kind:
            p["kind"] = kind
        return p

    out = [one(x, d, "posonly" if i < len(a.posonlyargs) else None) for i, (x, d) in enumerate(zip(pos, defaults))]
    if a.vararg:
        out.append(one(a.vararg, None, None, "*"))
    out += [one(x, d, "kwonly") for x, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        out.append(one(a.kwarg, None, None, "**"))
    return out


def py_raises(fn):
    out = []
    for n in walk_body(fn):
        if isinstance(n, ast.Raise) and n.exc is not None:
            t = n.exc.func if isinstance(n.exc, ast.Call) else n.exc
            nm = dotted(t)
            if nm and nm not in out:
                out.append(nm)
    return out[:12]


def py_call_names(fn, limit=80):
    out, seen = [], set()
    for n in walk_body(fn):
        if isinstance(n, ast.Call):
            nm = dotted(n.func)
            if nm and nm not in seen:
                seen.add(nm)
                out.append(nm)
                if len(out) >= limit:
                    break
    return out


def py_import_table(tree):
    t = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    t[a.asname] = ("module", a.name, 0)
                else:
                    t.setdefault(a.name.split(".")[0], ("module", a.name.split(".")[0], 0))
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name != "*":
                    t[a.asname or a.name] = ("from", n.module or "", n.level, a.name)
    return t


def py_exports(tree):
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in tg) and isinstance(getattr(n, "value", None), (ast.List, ast.Tuple)):
                return [e.value for e in n.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)][:80]
    names = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not n.name.startswith("_"):
            names.append(n.name)
        elif isinstance(n, ast.Assign):
            names += [t.id for t in n.targets if isinstance(t, ast.Name) and not t.id.startswith("_")]
    return sorted(set(names))[:80]


def clean_doc(d):
    return d if d else None


class PyResolver:
    """Best-effort resolution of dotted call names to 'path::qual' using each file's import table.
    Heuristic by design: absolute imports match a unique dotted-path suffix of a repo file (the repo
    puts backend/ and m2c/src/ on sys.path), relative imports are exact, stdlib is never resolved."""

    def __init__(self, info):
        self.info = info
        self.suffix = collections.defaultdict(list)
        for p in info:
            mod = p[:-3]
            if mod.endswith("/__init__"):
                mod = mod[:-9]
            parts = mod.split("/")
            for i in range(len(parts)):
                self.suffix[".".join(parts[i:])].append(p)

    def module(self, rel, mod, level=0):
        if level:
            pkg = rel.rsplit("/", 1)[0] if "/" in rel else ""
            for _ in range(level - 1):
                pkg = pkg.rsplit("/", 1)[0] if "/" in pkg else ""
            base = "/".join(x for x in (pkg, mod.replace(".", "/")) if x)
            for cand in (base + ".py", base + "/__init__.py"):
                if cand in self.info:
                    return cand
            return None
        if not mod or mod.split(".")[0] in STDLIB:
            return None
        c = self.suffix.get(mod)
        if not c:
            return None
        if len(c) > 1:
            if not is_test_path(rel):
                c = [p for p in c if not is_test_path(p)] or c
            if len(c) > 1:
                def share(p):
                    a, b = rel.split("/")[:-1], p.split("/")[:-1]
                    n = 0
                    for x, y in zip(a, b):
                        if x != y:
                            break
                        n += 1
                    return n
                ranked = sorted(c, key=share, reverse=True)
                if share(ranked[0]) == share(ranked[1]):
                    return None
                return ranked[0]
        return c[0]

    def symbol(self, path, qual, depth=0):
        info = self.info.get(path)
        if not info or not qual:
            return None
        if qual in info["defs"]:
            return f"{path}::{qual}"
        if depth >= 3:
            return None
        head, _, rest = qual.partition(".")
        ent = info["itab"].get(head)
        if ent and ent[0] == "from":
            _, mod, level, name = ent
            tp = self.module(path, mod, level)
            if tp:
                r = self.symbol(tp, name + ("." + rest if rest else ""), depth + 1)
                if r:
                    return r
            sub = self.module(path, (mod + "." if mod else "") + name, level)
            if sub and rest:
                return self.symbol(sub, rest, depth + 1)
        return None

    def name(self, rel, dname, scopes=()):
        info = self.info[rel]
        defs, itab = info["defs"], info["itab"]
        for sc in scopes:
            if f"{sc}.{dname}" in defs:
                return f"{rel}::{sc}.{dname}"
        if dname in defs:
            return f"{rel}::{dname}"
        parts = dname.split(".")
        ent = itab.get(parts[0])
        if not ent:
            return None
        if ent[0] == "module":
            _, mod, level = ent
            for k in range(len(parts) - 1, 0, -1):
                p = self.module(rel, ".".join([mod] + parts[1:k]), level)
                if p:
                    return self.symbol(p, ".".join(parts[k:]))
            return None
        _, mod, level, nm = ent
        p = self.module(rel, mod, level)
        if p:
            r = self.symbol(p, ".".join([nm] + parts[1:]))
            if r:
                return r
        sub = self.module(rel, (mod + "." if mod else "") + nm, level)
        if sub and len(parts) > 1:
            return self.symbol(sub, ".".join(parts[1:]))
        return None

    def method(self, rel, clsq, meth, depth=0):
        info = self.info.get(rel)
        if not info or depth > 3:
            return None
        if f"{clsq}.{meth}" in info["defs"]:
            return f"{rel}::{clsq}.{meth}"
        for base in info["classes"].get(clsq, []):
            ref = self.name(rel, base)
            if ref:
                p, q = ref.split("::", 1)
                r = self.method(p, q, meth, depth + 1)
                if r:
                    return r
        return None

    def call(self, rel, raw_qual, clsq, pnames, dname):
        """-> (keep, target). Calls on locals/builtins are dropped; unresolved imported names are kept."""
        info = self.info[rel]
        parts = dname.split(".")
        root = parts[0]
        if root in ("self", "cls"):
            if clsq and len(parts) == 2:
                return True, self.method(rel, clsq, parts[1])
            return False, None
        if root in pnames:
            return False, None
        scopes = []
        q = raw_qual.split(".")
        for i in range(len(q), 0, -1):
            sc = ".".join(q[:i])
            if sc not in info["classes"]:
                scopes.append(sc)
        target = self.name(rel, dname, scopes)
        if target:
            return True, target
        if root in info["itab"]:
            return True, None
        if root in info["defs"] and len(parts) > 1:
            return True, None
        return False, None


def build_table_refs(sql, texts):
    """{table: [paths whose code mentions it in a SQL/ORM-looking context]}; the files that define the table are excluded."""
    names, defined = set(), collections.defaultdict(set)
    for p, sq in sql.items():
        for t in sq.get("tables", []):
            n = t["name"].split(".")[-1].strip('"').lower()
            names.add(n)
            defined[n].add(p)
    if not names:
        return {}
    kw = re.compile(r'\b(?:from|into|update|join|table|on|references)\s+(?:if\s+(?:not\s+)?exists\s+)?(?:"?\w+"?\.)?"?(\w+)"?', re.I)
    api = re.compile(r"""\.(?:table|from_|from)\(\s*['"](?:\w+\.)?(\w+)['"]""")
    imp_py = re.compile(r"^\s*(?:from\s+[\w.]+\s+import\b|import\s)")
    refs = collections.defaultdict(set)
    for rel, text in texts.items():
        ext = rel.rsplit(".", 1)[-1].lower()
        for ln in text.splitlines():
            st = ln.lstrip()
            if ext == "py" and (st.startswith("#") or imp_py.match(ln)):
                continue
            if ext in ("js", "jsx", "mjs", "cjs", "ts", "tsx") and (st.startswith(("//", "*", "import ")) or " from '" in ln and st.startswith("import")):
                continue
            if ext == "sql" and st.startswith("--"):
                continue
            for rx in (kw, api):
                for m in rx.finditer(ln):
                    n = m.group(1).lower()
                    if n in names and rel not in defined[n]:
                        refs[n].add(rel)
    return {n: sorted(refs[n])[:40] for n in sorted(names)}


# ---------------------------------------------------------------- python
def py_cc(fn, deep=False):
    c, stack = 1, list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if not deep and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp)):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            c += 1 + len(n.ifs)
        elif hasattr(ast, "match_case") and isinstance(n, ast.match_case):
            c += 1
        stack.extend(ast.iter_child_nodes(n))
    return c


def unp(n, lim=90):
    try:
        return ast.unparse(n)[:lim]
    except Exception:
        return "?"


def literal_dict(tree, name):
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets) and isinstance(n.value, ast.Dict):
            out = {}
            for k, v in zip(n.value.keys, n.value.values):
                if isinstance(k, ast.Constant):
                    try:
                        val = ast.literal_eval(v)
                    except Exception:
                        val = None
                    out[k.value] = val
            return out
    return None


def analyze_py(rel, text):
    lines = text.splitlines()
    rec = {}
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return {"parse_error": str(e)[:100]}, [], [], [], {}
    doc_lines = set()

    def doc_of(node):
        b = getattr(node, "body", None)
        if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) and isinstance(b[0].value.value, str):
            for i in range(b[0].lineno, b[0].end_lineno + 1):
                doc_lines.add(i)
            return b[0].value.value
        return None

    mdoc = doc_of(tree)
    funcs, classes, routes, extra = [], [], [], {}
    kinds_cache = {}
    prefixes = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == "APIRouter":
            pre = next((k.value.value for k in n.value.keywords if k.arg == "prefix" and isinstance(k.value, ast.Constant)), "")
            for t in n.targets:
                if isinstance(t, ast.Name):
                    prefixes[t.id] = pre
    includes = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "include_router" and n.args:
            a = n.args[0]
            mod = a.value.id if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) else (a.id if isinstance(a, ast.Name) else None)
            pre = next((k.value.value for k in n.keywords if k.arg == "prefix" and isinstance(k.value, ast.Constant)), "")
            if mod:
                includes.append({"alias": mod, "prefix": pre})
    is_api = rel.startswith("backend/api/")

    def visit(node, scope, in_cls, clsq=None):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                d = doc_of(ch)
                a = ch.args
                nargs = len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs) + bool(a.vararg) + bool(a.kwarg)
                decos = [unp(x, 70) for x in ch.decorator_list]
                f = {"name": ch.name, "qual": ".".join(scope + [ch.name]), "kind": "method" if in_cls else ("nested" if scope else "function"),
                     "async": isinstance(ch, ast.AsyncFunctionDef), "start": ch.lineno, "end": ch.end_lineno, "args": nargs,
                     "decorators": decos, "doc": bool(d), "cc": py_cc(ch), "cc_deep": py_cc(ch, True),
                     "test": ch.name.startswith("test") and is_test_path(rel)}
                # v2 facts (additive)
                d1 = first_sentence(d)
                if d1:
                    f["doc1"] = d1
                f["params"] = py_params(a)
                if ch.returns is not None:
                    f["returns"] = unp(ch.returns, 80)
                rz = py_raises(ch)
                if rz:
                    f["raises"] = rz
                f["visibility"] = visibility(ch.name)
                f["hash"] = norm_hash(lines, min([x.lineno for x in ch.decorator_list] + [ch.lineno]), ch.end_lineno)
                f["_calls"] = py_call_names(ch)
                f["_cls"] = clsq
                f["_pn"] = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs + ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else [])}
                funcs.append(f)
                if is_api or rel.endswith("server.py"):
                    for dec in ch.decorator_list:
                        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in HTTP and dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                            rv = dec.func.value.id if isinstance(dec.func.value, ast.Name) else "router"
                            deps = []
                            for df in list(a.defaults) + list(a.kw_defaults):
                                if isinstance(df, ast.Call) and getattr(df.func, "id", getattr(df.func, "attr", "")) == "Depends" and df.args:
                                    deps.append(unp(df.args[0], 40))
                            rm = next((unp(k.value, 40) for k in dec.keywords if k.arg == "response_model"), None)
                            routes.append({"method": dec.func.attr.upper(), "path": dec.args[0].value, "router_prefix": prefixes.get(rv, ""),
                                           "handler": ch.name, "line": ch.lineno, "end": ch.end_lineno, "async": f["async"], "deps": deps, "response_model": rm})
                visit(ch, scope + [ch.name], False, clsq)
            elif isinstance(ch, ast.ClassDef):
                d = doc_of(ch)
                bases = [unp(b, 40) for b in ch.bases]
                c = {"name": ch.name, "start": ch.lineno, "end": ch.end_lineno, "bases": bases, "doc": bool(d),
                     "methods": sum(isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) for x in ch.body),
                     "decorators": [unp(x, 50) for x in ch.decorator_list]}
                # v2 facts (additive)
                c["qual"] = ".".join(scope + [ch.name])
                c["method_names"] = [x.name for x in ch.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))]
                d1 = first_sentence(d)
                if d1:
                    c["doc1"] = d1
                c["hash"] = norm_hash(lines, min([x.lineno for x in ch.decorator_list] + [ch.lineno]), ch.end_lineno)
                c["_bases"] = [x for x in (dotted(b) for b in ch.bases) if x]
                kindb = " ".join(bases + c["decorators"])
                if re.search(r"BaseModel|dataclass|TypedDict|NamedTuple", kindb):
                    c["fields"] = [{"name": x.target.id, "type": unp(x.annotation, 50)} for x in ch.body if isinstance(x, ast.AnnAssign) and isinstance(x.target, ast.Name)][:60]
                if re.search(r"Enum", kindb):
                    c["members"] = [{"name": t.id, "value": (repr(x.value.value)[:50] if isinstance(x.value, ast.Constant) else None)}
                                    for x in ch.body if isinstance(x, ast.Assign) for t in x.targets if isinstance(t, ast.Name)][:200]
                classes.append(c)
                visit(ch, scope + [ch.name], True, ".".join(scope + [ch.name]))
            else:
                visit(ch, scope, in_cls, clsq)

    visit(tree, [], False)
    # imports
    lazy = {id(x) for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) for x in ast.walk(fn) if isinstance(x, (ast.Import, ast.ImportFrom))}
    imports = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imports.append({"mod": a.name, "names": [], "level": 0, "line": n.lineno, "lazy": id(n) in lazy})
        elif isinstance(n, ast.ImportFrom):
            imports.append({"mod": n.module or "", "names": [a.name for a in n.names][:30], "level": n.level, "line": n.lineno, "lazy": id(n) in lazy})
    # line classification
    kinds = []
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        kinds.append("b" if not s else "c" if (i in doc_lines or s.startswith("#")) else "k")
    ps = prefix_sums(kinds)
    for f in funcs:
        f["loc"] = f["end"] - f["start"] + 1
        f["code"] = ps[f["end"]] - ps[f["start"] - 1]
    for c in classes:
        c["loc"] = c["end"] - c["start"] + 1
    if rel.endswith("backend/eo/registry.py"):
        caps = literal_dict(tree, "AGENT_CAPABILITIES")
        if caps:
            extra["agent_capabilities"] = {k: {kk: vv for kk, vv in (v or {}).items() if not SECRETISH.search(kk) and isinstance(vv, (str, int, float, bool, list, dict))} for k, v in caps.items()}
        for nm in ("ROLE_PROMPTS_SEED", "ROLE_ALIASES"):
            dct = literal_dict(tree, nm)
            if dct is not None:
                extra[nm.lower() + "_keys"] = list(dct.keys()) if nm == "ROLE_PROMPTS_SEED" else dct
    # v2: unique symbol keys + the tables the resolver needs (private, removed by main())
    uniquify(funcs + classes)
    info = {"defs": {x["qual"] for x in funcs + classes if "~" not in x["qual"]},
            "classes": {c["qual"]: c.pop("_bases") for c in classes}, "itab": py_import_table(tree)}
    rec = {"module_doc": (mdoc or "").strip()[:MODULE_DOC_MAX], "imports": imports, "kinds": kinds, "_pyinfo": info}
    ex_names = py_exports(tree)
    if ex_names:
        rec["exports"] = ex_names
    return rec, funcs, classes, routes, {"includes": includes, **extra}


# ---------------------------------------------------------------- javascript
JS_FUNC = {"function_declaration", "generator_function_declaration", "function_expression", "function", "arrow_function", "method_definition"}
JS_BRANCH = {"if_statement", "for_statement", "for_in_statement", "while_statement", "do_statement", "switch_case", "catch_clause", "ternary_expression"}


def js_kinds(lines):
    kinds, blk = [], False
    for ln in lines:
        s = ln.strip()
        if not s:
            kinds.append("b")
        elif blk:
            kinds.append("c")
            blk = "*/" not in s
        elif s.startswith("//"):
            kinds.append("c")
        elif s.startswith("/*"):
            kinds.append("c")
            blk = "*/" not in s[2:]
        else:
            kinds.append("k")
    return kinds


def js_cc(node, deep=False):
    c, stack = 1, list(node.children)
    while stack:
        n = stack.pop()
        if not deep and n.type in JS_FUNC:
            continue
        if n.type in JS_BRANCH:
            c += 1
        elif n.type == "binary_expression":
            op = n.child_by_field_name("operator")
            if op is not None and op.type in ("&&", "||", "??"):
                c += 1
        stack.extend(n.children)
    return c


def js_has_jsx(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type.startswith("jsx_"):
            return True
        stack.extend(n.children)
    return False


def js_name(node, src):
    t = node.type
    if t in ("function_declaration", "generator_function_declaration", "method_definition"):
        nm = node.child_by_field_name("name")
        return src[nm.start_byte:nm.end_byte].decode("utf-8", "replace") if nm else None
    p = node.parent
    if p is not None and p.type == "variable_declarator":
        nm = p.child_by_field_name("name")
        if nm is not None and nm.type == "identifier":
            return src[nm.start_byte:nm.end_byte].decode("utf-8", "replace")
    if p is not None and p.type == "assignment_expression":
        l = p.child_by_field_name("left")
        return src[l.start_byte:l.end_byte].decode("utf-8", "replace")[:60] if l is not None else None
    if p is not None and p.type == "pair":
        k = p.child_by_field_name("key")
        return src[k.start_byte:k.end_byte].decode("utf-8", "replace")[:60] if k is not None else None
    return None


JS_WRAP = {"variable_declarator", "lexical_declaration", "variable_declaration", "export_statement", "call_expression", "arguments",
           "parenthesized_expression", "assignment_expression", "expression_statement"}
_JS_DIRECTIVE = re.compile(r"^(eslint|prettier|@ts-|istanbul|c8 |webpack|global |jshint|use client|use server|tslint)", re.I)


def js_text(node, src, limit=None):
    t = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
    return t if limit is None else t[:limit]


def js_stmt_start(node):
    n = node
    while n.parent is not None and n.parent.type in JS_WRAP:
        n = n.parent
    return n.start_point[0] + 1


def js_leading_comment(lines, start):
    """First sentence of the JSDoc / // block directly above line `start` (1-based), or None."""
    i = start - 2
    if i < 0 or i >= len(lines):
        return None
    st = lines[i].strip()
    if st.endswith("*/"):
        j = i
        while j >= 0 and "/*" not in lines[j]:
            j -= 1
        if j < 0:
            return None
        body = re.sub(r"\*+/\s*$", "", re.sub(r"^\s*/\*+", "", "\n".join(lines[j:i + 1]).strip()))
        cleaned = [re.sub(r"^\s*\*+ ?", "", x).rstrip() for x in body.split("\n")]
    elif st.startswith("//"):
        j = i
        while j >= 0 and lines[j].strip().startswith("//"):
            j -= 1
        cleaned = [re.sub(r"^\s*//+\s?", "", x).rstrip() for x in lines[j + 1:i + 1]]
    else:
        return None
    out = []
    for x in cleaned:
        t = x.strip()
        if t.startswith("@"):
            break
        if _JS_DIRECTIVE.match(t):
            continue
        out.append(x)
    text = "\n".join(out).strip()
    return first_sentence(text) if re.search(r"[A-Za-z]{3}", text) else None


def js_params(ch, src):
    out = []
    ps = ch.child_by_field_name("parameters")
    if ps is not None:
        for p in ps.named_children:
            if p.type == "comment":
                continue
            if p.type == "assignment_pattern":
                left = p.child_by_field_name("left")
                nm = js_text(left, src) if left is not None else js_text(p, src)
            elif p.type == "rest_pattern":
                nm = "..." + (js_text(p.named_children[0], src) if p.named_children else "")
            else:
                nm = js_text(p, src)
            out.append({"name": re.sub(r"\s+", " ", nm).strip()[:40]})
    else:
        p1 = ch.child_by_field_name("parameter")
        if p1 is not None:
            out.append({"name": js_text(p1, src)[:40]})
    return out


def js_hooks(ch, src):
    out, stack = [], list(reversed(ch.children))
    while stack:
        n = stack.pop()
        if n.type in JS_FUNC:
            continue
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            nm = ""
            if fn is not None:
                if fn.type == "member_expression":
                    pr = fn.child_by_field_name("property")
                    nm = js_text(pr, src) if pr is not None else ""
                elif fn.type == "identifier":
                    nm = js_text(fn, src)
            if re.match(r"use[A-Z0-9]", nm) and nm not in out:
                out.append(nm)
        stack.extend(reversed(n.children))
    return out[:20]


def js_exported(node, name, export_names):
    p = node.parent
    for _ in range(7):
        if p is None:
            break
        if p.type == "export_statement":
            return True
        if p.type in JS_WRAP:
            p = p.parent
            continue
        break
    return name in export_names


def js_export_names(text):
    names = set()
    for m in re.finditer(r"export\s*\{([^}]*)\}", text):
        for part in m.group(1).split(","):
            w = part.strip().split(" as ")[0].strip()
            if w:
                names.add(w)
    for m in re.finditer(r"export\s+default\s+([A-Za-z_$][\w$]*)\s*;?\s*$", text, re.M):
        names.add(m.group(1))
    for m in re.finditer(r"module\.exports\s*=\s*\{([^}]*)\}", text):
        for part in m.group(1).split(","):
            w = part.split(":")[0].strip()
            if w:
                names.add(w)
    for m in re.finditer(r"module\.exports\s*=\s*([A-Za-z_$][\w$]*)\s*;?\s*$", text, re.M):
        names.add(m.group(1))
    return names


def analyze_js(rel, text):
    lines = text.splitlines()
    kinds = js_kinds(lines)
    ps = prefix_sums(kinds)
    funcs, classes = [], []
    export_names = js_export_names(text)
    if JS_PARSER:
        src = text.encode("utf-8")
        try:
            tree = JS_PARSER.parse(src)
        except Exception:
            tree = None
    else:
        tree = None
    if tree is not None:
        def walk(node, scope):
            for ch in node.children:
                t = ch.type
                if t in JS_FUNC:
                    nm = js_name(ch, src)
                    if nm:
                        params = ch.child_by_field_name("parameters")
                        np_ = len(params.named_children) if params is not None else (1 if ch.child_by_field_name("parameter") is not None else 0)
                        comp = nm[:1].isupper() and js_has_jsx(ch)
                        hook = bool(re.match(r"use[A-Z0-9]", nm))
                        s, e = ch.start_point[0] + 1, ch.end_point[0] + 1
                        fd = {"name": nm, "qual": ".".join(scope + [nm]), "kind": "component" if comp else "hook" if hook else "method" if t == "method_definition" else ("nested" if scope else "function"),
                              "async": b"async" in src[ch.start_byte:ch.start_byte + 12], "start": s, "end": e, "args": np_, "decorators": [], "doc": False,
                              "cc": js_cc(ch), "cc_deep": js_cc(ch, True), "loc": e - s + 1, "code": ps[e] - ps[s - 1], "test": False}
                        # v2 facts (additive)
                        d1 = js_leading_comment(lines, js_stmt_start(ch))
                        if d1:
                            fd["doc1"] = d1
                        fd["params"] = js_params(ch, src)
                        hk = js_hooks(ch, src)
                        if hk:
                            fd["hooks_used"] = hk
                        fd["exported"] = bool(not scope and t != "method_definition" and js_exported(ch, nm, export_names))
                        fd["visibility"] = visibility(nm)
                        fd["hash"] = norm_hash(lines, s, e)
                        funcs.append(fd)
                        walk(ch, scope + [nm])
                        continue
                    walk(ch, scope)
                elif t == "class_declaration":
                    nm = ch.child_by_field_name("name")
                    name = src[nm.start_byte:nm.end_byte].decode() if nm else "?"
                    s, e = ch.start_point[0] + 1, ch.end_point[0] + 1
                    cd = {"name": name, "start": s, "end": e, "loc": e - s + 1, "bases": [], "methods": sum(1 for x in ch.named_children[-1].named_children if x.type == "method_definition") if ch.named_children else 0, "doc": False, "decorators": []}
                    # v2 facts (additive)
                    cd["qual"] = ".".join(scope + [name])
                    body = ch.child_by_field_name("body")
                    cd["method_names"] = [js_text(x.child_by_field_name("name"), src) for x in (body.named_children if body is not None else []) if x.type == "method_definition" and x.child_by_field_name("name") is not None]
                    d1 = js_leading_comment(lines, js_stmt_start(ch))
                    if d1:
                        cd["doc1"] = d1
                    cd["hash"] = norm_hash(lines, s, e)
                    classes.append(cd)
                    walk(ch, scope + [name])
                else:
                    walk(ch, scope)
        walk(tree.root_node, [])
    else:  # heuristic fallback
        pat = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")
        for i, ln in enumerate(lines, 1):
            m = pat.match(ln)
            if m:
                nm = m.group(1) or m.group(2)
                depth, end, started = 0, i, False
                for j in range(i - 1, len(lines)):
                    for chx in lines[j]:
                        if chx == "{":
                            depth += 1; started = True
                        elif chx == "}":
                            depth -= 1
                    end = j + 1
                    if started and depth <= 0:
                        break
                    if not started and j > i + 2:
                        break
                fd = {"name": nm, "qual": nm, "kind": "component" if nm[:1].isupper() else "function", "async": "async" in ln, "start": i, "end": end, "args": 0, "decorators": [],
                      "doc": False, "cc": 0, "loc": end - i + 1, "code": ps[end] - ps[i - 1], "test": False, "approx": True}
                d1 = js_leading_comment(lines, i)
                if d1:
                    fd["doc1"] = d1
                fd["hash"] = norm_hash(lines, i, end)
                funcs.append(fd)
    uniquify(funcs + classes)
    imports = []
    for m in re.finditer(r"""(?:import\s+(?:[\w*${}\s,]+?\s+from\s+)?|require\(\s*|import\(\s*)['"]([^'"]+)['"]""", text):
        imports.append({"mod": m.group(1), "line": text.count("\n", 0, m.start()) + 1})
    exports = sorted({m.group(2) for m in re.finditer(r"export\s+(default\s+)?(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)", text)})
    ntests = len(re.findall(r"^\s*(?:test|it)(?:\.\w+)?\(", text, re.M)) if is_test_path(rel) else 0
    api = []
    for i, ln in enumerate(lines, 1):
        s = ln.lstrip()
        if s.startswith("//") or s.startswith("*"):
            continue
        for m in re.finditer(r"""['"`](/api/[A-Za-z0-9_./${}:~()\-]*)""", ln):
            api.append({"line": i, "path": m.group(1)[:120]})
    rec = {"imports": imports, "exports": exports, "kinds": kinds, "js_tests": ntests, "api_paths": api[:80], "parser": "tree-sitter" if tree is not None else "heuristic"}
    if is_test_path(rel):
        for f in funcs:
            f["test"] = False
    return rec, funcs, classes


# ---------------------------------------------------------------- misc analyzers
def analyze_sql(text):
    tables = []
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)\s*\((.*?)\)\s*;", text, re.S | re.I):
        cols = []
        depth, cur = 0, ""
        for chx in m.group(2) + ",":
            if chx == "(":
                depth += 1
            elif chx == ")":
                depth -= 1
            if chx == "," and depth == 0:
                w = cur.strip().split()
                if len(w) >= 2 and w[0].lower() not in ("constraint", "primary", "foreign", "unique", "check", "exclude"):
                    cols.append({"name": w[0].strip('"'), "type": " ".join(w[1:3])[:30]})
                cur = ""
            else:
                cur += chx
        tables.append({"name": m.group(1).strip('"'), "columns": cols})
    return {"tables": tables,
            "alters": [m.group(1) for m in re.finditer(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)", text, re.I)][:30],
            "policies": [m.group(1) for m in re.finditer(r"CREATE\s+POLICY\s+\"?([\w ]+)\"?", text, re.I)],
            "indexes": [m.group(1) for m in re.finditer(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w\"]+)", text, re.I)],
            "functions": [m.group(1) for m in re.finditer(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([\w.\"]+)", text, re.I)]}


def analyze_md(text):
    heads = [(len(m.group(1)), m.group(2).strip()[:90]) for m in re.finditer(r"^(#{1,4})\s+(.+)$", text, re.M)]
    return {"title": heads[0][1] if heads else "", "headings": heads[:40], "words": len(text.split())}


def parse_requirements(text):
    out = []
    for ln in text.splitlines():
        ln = ln.split("#")[0].strip()
        if ln and not ln.startswith("-"):
            m = re.match(r"([A-Za-z0-9_.\-\[\]]+)\s*(.*)", ln)
            if m:
                out.append({"name": m.group(1), "spec": m.group(2)})
    return out


def try_yaml(text):
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception:
        return None


def analyze_compose(text):
    y = try_yaml(text)
    if isinstance(y, dict) and isinstance(y.get("services"), dict):
        return [{"name": k, "image": v.get("image"), "build": bool(v.get("build")), "ports": [str(p) for p in v.get("ports", [])],
                 "depends_on": list(v.get("depends_on", {}) if isinstance(v.get("depends_on"), dict) else v.get("depends_on", [])),
                 "env_names": sorted((list(v["environment"].keys()) if isinstance(v.get("environment"), dict) else [e.split("=")[0] for e in v.get("environment", [])]) if v.get("environment") else [])}
                for k, v in y["services"].items() if isinstance(v, dict)]
    return [{"name": m.group(1)} for m in re.finditer(r"^  ([A-Za-z0-9_\-]+):\s*$", text.split("services:", 1)[-1], re.M)]


def analyze_ci(text):
    y = try_yaml(text)
    if isinstance(y, dict):
        trig = y.get("on", y.get(True))
        return {"name": y.get("name"), "triggers": list(trig.keys()) if isinstance(trig, dict) else trig,
                "jobs": [{"id": k, "name": v.get("name", k), "steps": [s.get("name") or s.get("uses") or (s.get("run", "")[:50]) for s in v.get("steps", [])]} for k, v in (y.get("jobs") or {}).items() if isinstance(v, dict)]}
    return {"jobs": [{"id": m.group(1)} for m in re.finditer(r"^  ([A-Za-z0-9_\-]+):\s*$", text.split("jobs:", 1)[-1], re.M)]}


def env_names(text):
    return [{"name": m.group(1), "comment": (m.group(0).split("#", 1)[1].strip()[:100] if "#" in m.group(0) else "")} for m in re.finditer(r"^\s*([A-Z][A-Z0-9_]+)\s*=.*$", text, re.M)]


# ---------------------------------------------------------------- git
def git_stats():
    if not git("rev-parse", "--is-inside-work-tree").strip():
        return None
    raw = git("log", "--no-renames", "--numstat", "--format=@@%H\x1f%an\x1f%ad\x1f%s", "--date=short")
    commits, per = [], collections.defaultdict(lambda: {"commits": 0, "added": 0, "deleted": 0, "first": None, "last": None, "dates": []})
    cur = None
    for ln in raw.splitlines():
        if ln.startswith("@@"):
            h, an, ad, sub = (ln[2:].split("\x1f") + ["", "", "", ""])[:4]
            cur = {"hash": h[:8], "author": an, "date": ad, "subject": sub[:140], "files": 0, "added": 0, "deleted": 0}
            commits.append(cur)
        elif ln.strip() and cur is not None:
            p = ln.split("\t")
            if len(p) == 3:
                a = int(p[0]) if p[0].isdigit() else 0
                d = int(p[1]) if p[1].isdigit() else 0
                cur["files"] += 1; cur["added"] += a; cur["deleted"] += d
                s = per[p[2]]
                s["commits"] += 1; s["added"] += a; s["deleted"] += d
                s["dates"].append(cur["date"])
    for s in per.values():
        s["first"], s["last"] = min(s["dates"]), max(s["dates"])
        s["dates"] = sorted(s["dates"])[-60:]
    branches = [b.strip().lstrip("* ") for b in git("branch", "-a").splitlines()][:30]
    return {"commits": commits, "per_file": per, "branches": branches, "head": git("rev-parse", "--short", "HEAD").strip(), "head_full": git("rev-parse", "HEAD").strip(),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD").strip(), "dirty_files": len([l for l in git("status", "--porcelain").splitlines()])}


# ---------------------------------------------------------------- main
def main():
    files = list_files()
    out_files, all_funcs, routes_all, includes, env_use, extras = [], 0, [], [], collections.defaultdict(set), {}
    sql, compose, ci, deps, envex, dockers = {}, [], {}, {}, {}, {}
    todos_total = 0
    pyinfo, texts = {}, {}
    for rel in files:
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
        text, size = read_text(rel)
        rec = {"path": rel, "ext": ext, "lang": LANG.get(ext, "other"), "size": size, "test": is_test_path(rel), "binary": text is None}
        if text is None:
            out_files.append(rec)
            continue
        lines = text.splitlines()
        rec["lines"] = len(lines)
        kinds = None
        try:
            if ext == "py":
                r, fn, cl, rt, ex = analyze_py(rel, text)
                kinds = r.pop("kinds", None)
                pi = r.pop("_pyinfo", None)
                if pi is not None:
                    pyinfo[rel] = pi
                rec.update(r); rec["functions"], rec["classes"] = fn, cl
                for x in rt:
                    x["file"] = rel
                routes_all += rt
                includes += ex.pop("includes", [])
                extras.update(ex)
            elif ext in ("js", "jsx", "mjs", "cjs"):
                r, fn, cl = analyze_js(rel, text)
                kinds = r.pop("kinds", None)
                rec.update(r); rec["functions"], rec["classes"] = fn, cl
            elif ext == "sql":
                rec["sql"] = analyze_sql(text)
            elif ext == "md":
                rec["md"] = analyze_md(text)
            elif ext in ("yml", "yaml"):
                if rel.endswith(("docker-compose.yml", "docker-compose.yaml")):
                    compose = analyze_compose(text)
                elif ".github/workflows/" in rel:
                    ci[rel] = analyze_ci(text)
            elif ext == "txt" and "requirements" in rel:
                deps[rel] = parse_requirements(text)
            elif rel.endswith("package.json"):
                j = json.loads(text)
                deps[rel] = {"name": j.get("name"), "version": j.get("version"), "dependencies": j.get("dependencies", {}), "devDependencies": j.get("devDependencies", {}), "scripts": j.get("scripts", {})}
            elif rel.endswith("pyproject.toml"):
                deps[rel] = {"raw_head": text[:1500]}
            elif ".env" in rel.rsplit("/", 1)[-1] and "example" in rel:
                envex[rel] = env_names(text)
            elif rel.endswith("Dockerfile"):
                dockers[rel] = {"from": re.findall(r"^FROM\s+(\S+)", text, re.M), "expose": re.findall(r"^EXPOSE\s+(.+)$", text, re.M), "cmd": re.findall(r"^(?:CMD|ENTRYPOINT)\s+(.+)$", text, re.M)[:2]}
        except Exception as e:
            rec["analysis_error"] = f"{type(e).__name__}: {str(e)[:80]}"
        if ext in ("py", "js", "jsx", "mjs", "cjs", "ts", "tsx", "sql"):
            texts[rel] = text
        if kinds is None:
            kinds = ["b" if not l.strip() else "k" for l in lines]
        rec["blank"] = kinds.count("b"); rec["comment"] = kinds.count("c"); rec["code"] = kinds.count("k")
        if ext in ("py", "js", "jsx", "mjs", "cjs"):
            for m in re.finditer(r"(?:os\.(?:environ\.get|getenv)\(\s*['\"]|os\.environ\[['\"]|process\.env\.)([A-Z][A-Z0-9_]+)", text):
                env_use[m.group(1)].add(rel)
            td = []
            for i, ln in enumerate(lines, 1):
                m = re.search(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]*(.{0,110})", ln)
                if m:
                    td.append({"line": i, "tag": m.group(1), "text": m.group(2).strip()})
            if td:
                rec["todos"] = td[:12]; rec["todo_count"] = len(td); todos_total += len(td)
        for f in rec.get("functions", []):
            all_funcs += 1
        if ext == "py" and rec["test"]:
            rec["test_count"] = sum(1 for f in rec.get("functions", []) if f.get("test"))
        elif ext in ("js", "jsx", "mjs", "cjs") and rec["test"]:
            rec["test_count"] = rec.get("js_tests", 0)
        out_files.append(rec)
        if ext == "sql":
            sql[rel] = rec["sql"]
    # v2: resolve call names to "path::qual" now that every file's definitions are known
    resolver = PyResolver(pyinfo)
    for rec in out_files:
        if rec.get("ext") != "py" or rec["path"] not in pyinfo:
            continue
        for f in rec.get("functions", []):
            raw_q = re.sub(r"~\d+$", "", f["qual"])
            calls = []
            for nm in f.pop("_calls", []):
                keep, target = resolver.call(rec["path"], raw_q, f.get("_cls"), f.get("_pn", ()), nm)
                if keep:
                    calls.append([nm, target])
                    if len(calls) >= 60:
                        break
            if calls:
                f["calls"] = calls
            f.pop("_cls", None)
            f.pop("_pn", None)
    table_refs = build_table_refs(sql, texts)
    texts.clear()
    g = git_stats()
    if g:
        for rec in out_files:
            s = g["per_file"].get(rec["path"])
            if s:
                rec["git"] = s
        g.pop("per_file")
    # totals
    tot = collections.Counter()
    bylang = collections.defaultdict(lambda: collections.Counter())
    for r in out_files:
        if r.get("binary"):
            continue
        for k in ("lines", "code", "comment", "blank"):
            tot[k] += r.get(k, 0)
            bylang[r["lang"]][k] += r.get(k, 0)
        bylang[r["lang"]]["files"] += 1
    census = {
        "meta": {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "root_name": ROOT.name, "files_scanned": len(out_files),
                 "seconds": round(time.time() - T0, 1), "js_parser": "tree-sitter" if JS_PARSER else "heuristic-fallback", "script_version": SCRIPT_VERSION},
        "totals": {**tot, "functions": all_funcs, "todos": todos_total},
        "by_language": {k: dict(v) for k, v in bylang.items()},
        "files": out_files, "git": g, "routes": routes_all, "router_includes": includes,
        "sql": sql, "docker_compose": compose, "ci": ci, "dependencies": deps, "env_examples": envex, "dockerfiles": dockers,
        "env_usage": {k: sorted(v) for k, v in env_use.items()}, "registry": extras,
        "table_refs": table_refs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(census, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"census: {len(out_files)} files, {tot['lines']:,} lines ({tot['code']:,} code), {all_funcs:,} functions, "
          f"{len(routes_all)} routes, {len(g['commits']) if g else 0} commits -> {OUT} ({OUT.stat().st_size // 1024} KB, {time.time() - T0:.1f}s, js parser: {census['meta']['js_parser']})")


if __name__ == "__main__":
    main()
