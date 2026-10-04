"""Add inferred frontend-to-backend API links to a copy of graph.json."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = ROOT / "graphify-out" / "graph.json"
OUTPUT_PATH = ROOT / "graphify-out" / "graph.linked.json"
FRONTEND_ROOT = ROOT / "frontend"
BACKEND_API_ROOT = ROOT / "backend" / "api"
BACKEND_LOCAL_WORKSPACE = ROOT / "backend" / "api" / "routes" / "local_workspace.py"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
PATH_RE = re.compile(r"/api/[A-Za-z0-9_./${}:~()\-]+")
BASE_RE = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*`([^`]*?/api/[^`]*)`")
BASE_USE_RE = re.compile(r"\$\{(\w+)\}([^'\"`),]*)")


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    source_file: str
    line: int
    node_id: str


@dataclass(frozen=True)
class FrontendCall:
    path: str
    source_file: str
    line: int
    node_id: str
    method: str | None


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def line_number(value: object) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 0


def load_graph() -> tuple[dict, dict[str, list[dict]]]:
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    by_file: dict[str, list[dict]] = {}
    for node in graph.get("nodes", []):
        source_file = node.get("source_file")
        if source_file:
            by_file.setdefault(source_file.replace("\\", "/"), []).append(node)
    return graph, by_file


def node_for_line(nodes: list[dict], line: int, *, target: bool = False) -> str | None:
    callable_nodes = [node for node in nodes if node.get("_callable")]
    candidates = callable_nodes or nodes
    if not candidates:
        return None
    if target:
        return min(candidates, key=lambda node: abs(line_number(node.get("source_location")) - line))["id"]
    before = [node for node in candidates if line_number(node.get("source_location")) <= line]
    return max(before or candidates, key=lambda node: line_number(node.get("source_location")))["id"]


def literal_string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def route_prefixes(tree: ast.AST) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not isinstance(node.value.func, ast.Name) or node.value.func.id != "APIRouter":
            continue
        prefix = ""
        for keyword in node.value.keywords:
            if keyword.arg == "prefix":
                prefix = literal_string(keyword.value) or ""
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def collect_routes(by_file: dict[str, list[dict]]) -> list[Route]:
    routes: list[Route] = []
    route_files = [path for path in BACKEND_API_ROOT.rglob("*.py") if path != BACKEND_LOCAL_WORKSPACE]
    route_files.append(BACKEND_LOCAL_WORKSPACE)
    for path in sorted(route_files):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        prefixes = route_prefixes(tree)
        source_file = relative(path)
        nodes = by_file.get(source_file, [])
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in function.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                method = decorator.func.attr.lower()
                if method not in HTTP_METHODS or not decorator.args:
                    continue
                route_path = literal_string(decorator.args[0])
                if route_path is None:
                    continue
                router_name = decorator.func.value.id if isinstance(decorator.func.value, ast.Name) else "router"
                route_path = f"{prefixes.get(router_name, '')}{route_path}"
                node_id = node_for_line(nodes, function.lineno, target=True)
                if node_id:
                    routes.append(Route(method, normalize_path(route_path), source_file, function.lineno, node_id))
    return routes


def normalize_path(path: str) -> str:
    path = re.sub(r"\$\{(?:qs|query|searchParams)\}", "", path)
    path = path.split("?", 1)[0].split("#", 1)[0].strip()
    path = re.sub(r"[).,;]+$", "", path)
    path = path.replace("\\", "/")
    path = re.sub(r"\$\{[^}/]+\}|\{[^}/]+\}", "{param}", path)
    path = re.sub(r"/+", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


def path_match_score(frontend_path: str, route_path: str) -> tuple[int, int] | None:
    left = normalize_path(frontend_path)
    right = normalize_path(route_path)
    left_parts, right_parts = left.strip("/").split("/"), right.strip("/").split("/")
    if left == "/" or right == "/" or len(left_parts) != len(right_parts):
        return (0, 0) if left == right else None
    score = 0
    dynamic_matches = 0
    for frontend_part, route_part in zip(left_parts, right_parts):
        if frontend_part == route_part:
            score += 2
        elif frontend_part == "{param}" and route_part == "{param}":
            score += 1
            dynamic_matches += 1
        elif frontend_part == "{param}" or route_part == "{param}":
            continue
        else:
            return None
    return score, dynamic_matches


def matching_routes(call: FrontendCall, routes: list[Route]) -> list[Route]:
    scored = [(path_match_score(call.path, route.path), route) for route in routes]
    scored = [(score, route) for score, route in scored if score is not None]
    if not scored:
        return []
    best_score = max(score for score, _ in scored)
    candidates = [route for score, route in scored if score == best_score]
    if call.method is not None:
        candidates = [route for route in candidates if route.method == call.method]
    return candidates


def collect_frontend_calls(by_file: dict[str, list[dict]]) -> list[FrontendCall]:
    calls: list[FrontendCall] = []
    extensions = {".js", ".jsx", ".ts", ".tsx", ".mjs"}
    for path in sorted(FRONTEND_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions or "node_modules" in path.parts:
            continue
        source_file = relative(path)
        nodes = by_file.get(source_file, [])
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        base_definitions: dict[str, list[tuple[int, str]]] = {}
        for definition_line, line in enumerate(lines, start=1):
            base_match = BASE_RE.search(line)
            if base_match:
                base_path = base_match.group(2)
                base_definitions.setdefault(base_match.group(1), []).append(
                    (definition_line, base_path[base_path.find("/api") :])
                )
        for line_number_value, line in enumerate(lines, start=1):
            stripped = line.lstrip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            paths = [] if BASE_RE.search(line) else [match.group(0) for match in PATH_RE.finditer(line)]
            for base_name, definitions in base_definitions.items():
                for use in BASE_USE_RE.finditer(line):
                    if use.group(1) == base_name:
                        previous = [item for item in definitions if item[0] <= line_number_value]
                        if not previous:
                            continue
                        base_path = previous[-1][1]
                        suffix = use.group(2)
                        paths.append(base_path + suffix)
            node_id = node_for_line(nodes, line_number_value)
            if not node_id:
                continue
            context_lines = []
            for context_line in lines[line_number_value - 1 : line_number_value + 16]:
                context_lines.append(context_line)
                if "});" in context_line:
                    break
            context = "\n".join(context_lines)
            method_match = re.search(r"\bmethod\s*:\s*[\"'](GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)[\"']", context, re.I)
            helper_match = re.search(r"\bapi(Get|Post|Put|Patch|Delete|Options|Head)\s*\(", line, re.I)
            method = method_match.group(1).lower() if method_match else None
            if helper_match:
                method = helper_match.group(1).lower()
            for path_value in dict.fromkeys(paths):
                calls.append(FrontendCall(normalize_path(path_value), source_file, line_number_value, node_id, method))
    return list(dict.fromkeys(calls))


def main() -> None:
    graph, by_file = load_graph()
    routes = collect_routes(by_file)
    calls = collect_frontend_calls(by_file)
    matched: list[tuple[FrontendCall, Route]] = []
    unmatched_calls: list[FrontendCall] = []
    matched_routes: set[Route] = set()
    for call in calls:
        candidates = matching_routes(call, routes)
        if not candidates:
            unmatched_calls.append(call)
            continue
        for route in candidates:
            matched.append((call, route))
            matched_routes.add(route)
    links = graph.setdefault("links", [])
    existing = {(link.get("source"), link.get("target"), link.get("relation")) for link in links}
    added = 0
    for call, route in matched:
        key = (call.node_id, route.node_id, "calls_api")
        if key in existing:
            continue
        links.append({
            "source": call.node_id,
            "target": route.node_id,
            "relation": "calls_api",
            "_origin": "script",
            "confidence": "INFERRED",
            "confidence_score": 0.85 if call.method is not None else 0.6,
            "source_file": call.source_file,
            "source_location": f"L{call.line}",
            "weight": 1.0,
        })
        added += 1
    OUTPUT_PATH.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"API routes: {len(routes)}")
    print(f"Frontend API calls: {len(calls)}")
    print(f"Matched route-call pairs: {len(matched)}")
    print(f"Links added: {added}")
    print("Frontend calls with no matching route:")
    for call in unmatched_calls:
        print(f"  {call.source_file}:{call.line} {call.path}")
    print("Backend routes with no frontend calls:")
    for route in routes:
        if route not in matched_routes:
            print(f"  {route.method.upper()} {route.path} ({route.source_file}:{route.line})")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()