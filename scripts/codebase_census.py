#!/usr/bin/env python3
"""
codebase_census.py - measure everything about a repo, from the LOCAL working copy.

Usage (from the repo root):   python scripts/codebase_census.py
Output:                       graphify-out/census.json   (git-ignored)

Reads only source structure + git metadata. It never records file contents,
secret values or .env values (only variable NAMES). Standard library only;
if `tree-sitter` + `tree-sitter-javascript` are installed (Graphify installs
them) JS/JSX is parsed exactly, otherwise a heuristic fallback is used.
"""
import ast, collections, datetime, json, os, re, subprocess, sys, time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "graphify-out" / "census.json"
T0 = time.time()
LANG = {"py": "python", "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
        "ts": "typescript", "tsx": "typescript", "md": "markdown", "json": "json", "sql": "sql", "yml": "yaml",
        "yaml": "yaml", "html": "html", "css": "css", "sh": "shell", "ps1": "shell", "toml": "toml", "txt": "text"}
HTTP = {"get", "post", "put", "patch", "delete", "options", "head"}
SECRETISH = re.compile(r"key|secret|token|passw|credential", re.I)

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

    def visit(node, scope, in_cls):
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
                visit(ch, scope + [ch.name], False)
            elif isinstance(ch, ast.ClassDef):
                d = doc_of(ch)
                bases = [unp(b, 40) for b in ch.bases]
                c = {"name": ch.name, "start": ch.lineno, "end": ch.end_lineno, "bases": bases, "doc": bool(d),
                     "methods": sum(isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) for x in ch.body),
                     "decorators": [unp(x, 50) for x in ch.decorator_list]}
                kindb = " ".join(bases + c["decorators"])
                if re.search(r"BaseModel|dataclass|TypedDict|NamedTuple", kindb):
                    c["fields"] = [{"name": x.target.id, "type": unp(x.annotation, 50)} for x in ch.body if isinstance(x, ast.AnnAssign) and isinstance(x.target, ast.Name)][:60]
                if re.search(r"Enum", kindb):
                    c["members"] = [{"name": t.id, "value": (repr(x.value.value)[:50] if isinstance(x.value, ast.Constant) else None)}
                                    for x in ch.body if isinstance(x, ast.Assign) for t in x.targets if isinstance(t, ast.Name)][:200]
                classes.append(c)
                visit(ch, scope + [ch.name], True)
            else:
                visit(ch, scope, in_cls)

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
    rec = {"module_doc": (mdoc or "").strip()[:400], "imports": imports, "kinds": kinds}
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


def analyze_js(rel, text):
    lines = text.splitlines()
    kinds = js_kinds(lines)
    ps = prefix_sums(kinds)
    funcs, classes = [], []
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
                        funcs.append({"name": nm, "qual": ".".join(scope + [nm]), "kind": "component" if comp else "hook" if hook else "method" if t == "method_definition" else ("nested" if scope else "function"),
                                      "async": b"async" in src[ch.start_byte:ch.start_byte + 12], "start": s, "end": e, "args": np_, "decorators": [], "doc": False,
                                      "cc": js_cc(ch), "cc_deep": js_cc(ch, True), "loc": e - s + 1, "code": ps[e] - ps[s - 1], "test": False})
                        walk(ch, scope + [nm])
                        continue
                    walk(ch, scope)
                elif t == "class_declaration":
                    nm = ch.child_by_field_name("name")
                    name = src[nm.start_byte:nm.end_byte].decode() if nm else "?"
                    s, e = ch.start_point[0] + 1, ch.end_point[0] + 1
                    classes.append({"name": name, "start": s, "end": e, "loc": e - s + 1, "bases": [], "methods": sum(1 for x in ch.named_children[-1].named_children if x.type == "method_definition") if ch.named_children else 0, "doc": False, "decorators": []})
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
                funcs.append({"name": nm, "qual": nm, "kind": "component" if nm[:1].isupper() else "function", "async": "async" in ln, "start": i, "end": end, "args": 0, "decorators": [],
                              "doc": False, "cc": 0, "loc": end - i + 1, "code": ps[end] - ps[i - 1], "test": False, "approx": True})
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
    return {"commits": commits, "per_file": per, "branches": branches, "head": git("rev-parse", "--short", "HEAD").strip(),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD").strip(), "dirty_files": len([l for l in git("status", "--porcelain").splitlines()])}


# ---------------------------------------------------------------- main
def main():
    files = list_files()
    out_files, all_funcs, routes_all, includes, env_use, extras = [], 0, [], [], collections.defaultdict(set), {}
    sql, compose, ci, deps, envex, dockers = {}, [], {}, {}, {}, {}
    todos_total = 0
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
                 "seconds": round(time.time() - T0, 1), "js_parser": "tree-sitter" if JS_PARSER else "heuristic-fallback", "script_version": 1},
        "totals": {**tot, "functions": all_funcs, "todos": todos_total},
        "by_language": {k: dict(v) for k, v in bylang.items()},
        "files": out_files, "git": g, "routes": routes_all, "router_includes": includes,
        "sql": sql, "docker_compose": compose, "ci": ci, "dependencies": deps, "env_examples": envex, "dockerfiles": dockers,
        "env_usage": {k: sorted(v) for k, v in env_use.items()}, "registry": extras,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(census, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"census: {len(out_files)} files, {tot['lines']:,} lines ({tot['code']:,} code), {all_funcs:,} functions, "
          f"{len(routes_all)} routes, {len(g['commits']) if g else 0} commits -> {OUT} ({OUT.stat().st_size // 1024} KB, {time.time() - T0:.1f}s, js parser: {census['meta']['js_parser']})")


if __name__ == "__main__":
    main()
