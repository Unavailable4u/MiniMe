#!/usr/bin/env python3
"""
test_codebase_census.py - checks of the v2 fields written by scripts/codebase_census.py.

Run from the repo root:   python scripts/test_codebase_census.py
Builds a tiny throwaway repo in a temp folder, runs the census on it and asserts on the JSON.
Standard library only (the JavaScript checks are skipped when tree-sitter is not installed).
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "codebase_census.py"

FIXTURE = {
    "backend/eo/__init__.py": "from .b import helper\n",
    "backend/eo/b.py": (
        "def helper(y):\n"
        "    return y\n"
        "\n"
        "\n"
        "class Base:\n"
        "    def base_method(self):\n"
        "        pass\n"
    ),
    "backend/eo/a.py": (
        '"""Module A. Does things.\n\nSecond paragraph."""\n'
        "import os\n"
        "from .b import helper\n"
        "from eo.b import Base as B0\n"
        "from eo import b as bmod\n"
        "\n"
        "\n"
        "def top(x: int, *args, flag=True, token='shh', url='x' * 3, **kw) -> str:\n"
        '    """Do the thing. Second sentence here.\n\n    More detail."""\n'
        "    if x < 0:\n"
        "        raise ValueError('neg')\n"
        "    helper(x)\n"
        "    bmod.helper(x)\n"
        "    os.path.join('a', 'b')\n"
        "    len(args)\n"
        "    x.append(1)\n"
        "    return str(x)\n"
        "\n"
        "\n"
        "def fancy(name=__SECRETISH_VALUE__, n=-1, items=()):\n"
        "    return name\n"
        "\n"
        "\n"
        "class Child(B0):\n"
        '    """Child class."""\n'
        "\n"
        "    def run(self):\n"
        "        self.base_method()\n"
        "        self.own()\n"
        "        self.client.post()\n"
        "\n"
        "    def own(self):\n"
        "        return top(1)\n"
        "\n"
        "    @property\n"
        "    def v(self):\n"
        "        return 1\n"
        "\n"
        "    @v.setter\n"
        "    def v(self, value):\n"
        "        pass\n"
        "\n"
        "    def _hidden(self):\n"
        "        pass\n"
        "\n"
        "    def __repr__(self):\n"
        "        return 'x'\n"
    ),
    "backend/eo/c.py": (
        "import logging\n"
        "from eo import helper\n"
        "\n"
        "\n"
        "def f():\n"
        "    logging.getLogger('x')\n"
        "    helper(1)\n"
        "    local = lambda: 1\n"
        "    local()\n"
    ),
    "backend/utils/logging.py": "def getLogger(name):\n    return name\n",
    "backend/migrations/0001_things.sql": "CREATE TABLE IF NOT EXISTS things (id uuid PRIMARY KEY, name text);\n",
    "backend/eo/store.py": (
        "def load(cur):\n"
        '    cur.execute("SELECT * FROM things WHERE id = 1")\n'
    ),
    "frontend/app/Foo.jsx": (
        "import { useState } from 'react';\n"
        "\n"
        "/**\n"
        " * Shows a counter. Extra words here.\n"
        " * @param props ignored\n"
        " */\n"
        "export function Foo({ start }) {\n"
        "  const [n, setN] = useState(start);\n"
        "  return <button onClick={() => setN(n + 1)}>{n}</button>;\n"
        "}\n"
        "\n"
        "const Hidden = (a, b = 2) => {\n"
        "  return <div>{a}</div>;\n"
        "};\n"
    ),
}


def make_repo(root, files, git=True):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    if git:
        for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def run_census(root):
    out = root / "graphify-out" / "census.json"
    subprocess.run([sys.executable, str(SCRIPT), str(root), str(out)], check=True, capture_output=True)
    return json.loads(out.read_text(encoding="utf-8"))


def fn(census, path, qual):
    f = next(x for x in census["files"] if x["path"] == path)
    return next(x for x in f["functions"] if x["qual"] == qual)


class CensusV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="census_v2_"))
        files = dict(FIXTURE)
        # built at run time so no secret-looking literal sits in this test file
        files["backend/eo/a.py"] = files["backend/eo/a.py"].replace("__SECRETISH_VALUE__", repr("s" + "k-" + "a" * 12))
        cls.have_git = shutil.which("git") is not None
        make_repo(cls.tmp, files, git=cls.have_git)
        cls.files = files
        cls.c = run_census(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- meta
    def test_version_and_head(self):
        self.assertEqual(2, self.c["meta"]["script_version"])
        if self.have_git:
            self.assertEqual(40, len(self.c["git"]["head_full"]))
            self.assertTrue(self.c["git"]["head_full"].startswith(self.c["git"]["head"]))

    def test_no_private_keys_leak(self):
        for f in self.c["files"]:
            self.assertFalse([k for k in f if k.startswith("_")], f["path"])
            for x in f.get("functions", []) + f.get("classes", []):
                self.assertFalse([k for k in x if k.startswith("_")], (f["path"], x.get("qual")))

    # ---- python facts
    def test_doc1_and_module_doc(self):
        top = fn(self.c, "backend/eo/a.py", "top")
        self.assertEqual("Do the thing.", top["doc1"])
        self.assertTrue(top["doc"])
        mod = next(x for x in self.c["files"] if x["path"] == "backend/eo/a.py")
        self.assertIn("Second paragraph", mod["module_doc"])

    def test_params_returns_raises_visibility(self):
        top = fn(self.c, "backend/eo/a.py", "top")
        by = {p["name"]: p for p in top["params"]}
        self.assertEqual("int", by["x"]["annotation"])
        self.assertEqual("True", by["flag"]["default"])
        self.assertEqual("kwonly", by["flag"]["kind"])
        self.assertIn("*args", by)
        self.assertIn("**kw", by)
        self.assertEqual("str", top["returns"])
        self.assertEqual(["ValueError"], top["raises"])
        self.assertEqual("public", top["visibility"])
        self.assertEqual("private", fn(self.c, "backend/eo/a.py", "Child._hidden")["visibility"])
        self.assertEqual("dunder", fn(self.c, "backend/eo/a.py", "Child.__repr__")["visibility"])

    def test_defaults_are_secret_safe(self):
        top = {p["name"]: p for p in fn(self.c, "backend/eo/a.py", "top")["params"]}
        self.assertEqual("\u2026", top["token"]["default"])  # secret-looking parameter name
        self.assertEqual("\u2026", top["url"]["default"])  # not a literal / bare name
        fancy = {p["name"]: p for p in fn(self.c, "backend/eo/a.py", "fancy")["params"]}
        self.assertEqual("\u2026", fancy["name"]["default"])  # literal that looks like a key
        self.assertEqual("-1", fancy["n"]["default"])
        self.assertEqual("()", fancy["items"]["default"])
        blob = json.dumps(self.c)
        self.assertNotIn("s" + "k-" + "a" * 12, blob)

    # ---- calls
    def test_calls_resolution(self):
        calls = dict((n, t) for n, t in fn(self.c, "backend/eo/a.py", "top")["calls"])
        self.assertEqual("backend/eo/b.py::helper", calls["helper"])
        self.assertEqual("backend/eo/b.py::helper", calls["bmod.helper"])
        self.assertIn("os.path.join", calls)  # imported, so kept, but external
        self.assertIsNone(calls["os.path.join"])
        for dropped in ("len", "str", "x.append"):
            self.assertNotIn(dropped, calls)

    def test_method_calls_follow_bases(self):
        calls = dict((n, t) for n, t in fn(self.c, "backend/eo/a.py", "Child.run")["calls"])
        self.assertEqual("backend/eo/b.py::Base.base_method", calls["self.base_method"])
        self.assertEqual("backend/eo/a.py::Child.own", calls["self.own"])
        self.assertNotIn("self.client.post", calls)
        own = dict((n, t) for n, t in fn(self.c, "backend/eo/a.py", "Child.own")["calls"])
        self.assertEqual("backend/eo/a.py::top", own["top"])

    def test_reexport_and_stdlib_shadowing(self):
        calls = dict((n, t) for n, t in fn(self.c, "backend/eo/c.py", "f")["calls"])
        self.assertEqual("backend/eo/b.py::helper", calls["helper"])  # through eo/__init__.py
        self.assertIsNone(calls["logging.getLogger"])  # the stdlib module, not backend/utils/logging.py
        self.assertNotIn("local", calls)

    # ---- keys, classes, hashes
    def test_duplicate_quals_get_suffix(self):
        quals = [x["qual"] for x in next(f for f in self.c["files"] if f["path"] == "backend/eo/a.py")["functions"]]
        self.assertIn("Child.v", quals)
        self.assertIn("Child.v~2", quals)
        for f in self.c["files"]:
            both = [x["qual"] for x in f.get("functions", []) + f.get("classes", [])]
            self.assertEqual(len(both), len(set(both)), f["path"])

    def test_class_fields(self):
        f = next(x for x in self.c["files"] if x["path"] == "backend/eo/a.py")
        child = next(x for x in f["classes"] if x["name"] == "Child")
        self.assertEqual("Child", child["qual"])
        self.assertEqual("Child class.", child["doc1"])
        self.assertIn("run", child["method_names"])
        self.assertEqual(10, len(child["hash"]))

    def test_hash_ignores_formatting_but_not_edits(self):
        base = fn(self.c, "backend/eo/a.py", "top")["hash"]
        other = Path(tempfile.mkdtemp(prefix="census_v2_b_"))
        try:
            files = dict(self.files)
            files["backend/eo/a.py"] = files["backend/eo/a.py"].replace("__SECRETISH_VALUE__", repr("s" + "k-" + "a" * 12))
            reformatted = files["backend/eo/a.py"].replace("    helper(x)\n", "    helper(x)   \n\n").replace("\n", "\r\n")
            make_repo(other, {**files, "backend/eo/a.py": reformatted}, git=False)
            self.assertEqual(base, fn(run_census(other), "backend/eo/a.py", "top")["hash"])
            edited = files["backend/eo/a.py"].replace("helper(x)\n", "helper(x + 1)\n")
            (other / "backend/eo/a.py").write_bytes(edited.encode("utf-8"))
            self.assertNotEqual(base, fn(run_census(other), "backend/eo/a.py", "top")["hash"])
        finally:
            shutil.rmtree(other, ignore_errors=True)

    # ---- sql
    def test_table_refs(self):
        refs = self.c["table_refs"]
        self.assertEqual(["backend/eo/store.py"], refs["things"])

    # ---- javascript
    def test_javascript_facts(self):
        if self.c["meta"]["js_parser"] != "tree-sitter":
            self.skipTest("tree-sitter not installed")
        foo = fn(self.c, "frontend/app/Foo.jsx", "Foo")
        self.assertEqual("component", foo["kind"])
        self.assertEqual("Shows a counter.", foo["doc1"])
        self.assertEqual(["useState"], foo["hooks_used"])
        self.assertTrue(foo["exported"])
        self.assertEqual("{ start }", foo["params"][0]["name"])
        self.assertFalse(fn(self.c, "frontend/app/Foo.jsx", "Hidden")["exported"])
        self.assertEqual(["a", "b"], [p["name"] for p in fn(self.c, "frontend/app/Foo.jsx", "Hidden")["params"]])

    # ---- compatibility
    def test_v1_fields_are_still_there(self):
        top = fn(self.c, "backend/eo/a.py", "top")
        for k in ("name", "qual", "kind", "async", "start", "end", "args", "decorators", "doc", "cc", "cc_deep", "loc", "code", "test"):
            self.assertIn(k, top)
        for k in ("meta", "totals", "by_language", "files", "git", "routes", "router_includes", "sql", "registry"):
            self.assertIn(k, self.c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
