"""
eo/result_render.py — bug fix follow-up.

Turns ANY agent result shape in this codebase into readable markdown
text, instead of the generic `str(result)` fallback that was printing
raw Python dict reprs (e.g. "{'issues': [{'module': ...") into both the
live agent-step panel and the final chat answer.

Every REAL_ACTION_ROLES module has its own return shape:
  - agents/generic_worker.py (writer/editor/researcher/... — every
    reasoning-only role): {"role", "text", "next_destination"}
  - agents/reviewer.py ("verifier" role): {"issues": [...], "summary",
    "next_destination"}
  - agents/fixer_pool.py ("fixer" role): {"fixed_code": {module: {...}},
    "next_destination"}
  - agents/code_writers.py ("implementer" role) / agents/test_writer.py
    ("test_writer" role): a flat {module_name: code_string} dict, or
    {module_name: {"language", "code"}}
  - responder.py / prompt_writer_lean-backed tiers: plain str, or
    {"code": ...} / {"answer": ...}

Rather than teach every call site (eo/executor.py's _summarize(),
api/task_runner.py's answer extraction, and — mirrored in JS —
frontend/app/components/MessageBubble.jsx's answerTextOf()) each of
these shapes separately (and inevitably drifting), this is the one
place that knows all of them. Keep the JS mirror in sync if a new shape
is added here.
"""
import json


def _render_code_modules(modules: dict) -> str:
    """Renders a {module_name: code_str | {"language","code"}} dict as
    one fenced code block per module, each labeled with its name."""
    if not modules:
        return "_(no modules)_"
    parts = []
    for name, entry in modules.items():
        if isinstance(entry, dict):
            lang = entry.get("language", "")
            code = entry.get("code", "")
        else:
            lang, code = "", str(entry)
        parts.append(f"**{name}**\n```{lang}\n{code}\n```")
    return "\n\n".join(parts)


def _looks_like_module_map(result: dict) -> bool:
    """True for code_writers.py / test_writer.py's flat return shape —
    every value is either a bare code string, or a {"language","code"}
    dict. An empty dict also counts (test_writer returns {} when it
    generated no tests), so this can't just check `bool(result)`."""
    return all(
        isinstance(v, str) or (isinstance(v, dict) and "code" in v)
        for v in result.values()
    )


def _render_extraction_table(result: dict) -> str:
    """agents/extraction_table_builder.py's shape (Part 3 §3.5): one row
    per paper, columns are Title/Year plus whatever's in field_names.
    GFM pipe-table syntax -- frontend/app/components/Markdown.jsx already
    has real styled table/thead/th/td components via remark-gfm, so this
    renders as an actual table, not a second thing to build in React."""
    papers = result.get("papers") or []
    field_names = result.get("field_names") or []
    if not papers:
        return "_(no papers extracted)_"

    def esc(v) -> str:
        if v is None or v == "":
            return "—"
        return str(v).replace("|", "\\|").replace("\n", " ")

    headers = ["Title", "Year"] + [f.replace("_", " ").title() for f in field_names]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for p in papers:
        row = [esc(p.get("title")), esc(p.get("year"))] + [esc(p.get(f)) for f in field_names]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_agent_result(result, limit: int = 9000) -> str:
    """Best-effort human-readable markdown for ANY agent result shape in
    this codebase. Replaces the old str(result)-on-anything-unrecognized
    fallback that printed raw Python dict reprs."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        if result.get("text"):
            text = result["text"]
        elif "issues" in result and isinstance(result.get("issues"), list):
            # agents/reviewer.py's "verifier" shape.
            lines = []
            summary = (result.get("summary") or "").strip()
            if summary:
                lines.append(summary)
            issues = result["issues"]
            if issues:
                lines.append("")
                for issue in issues:
                    sev = issue.get("severity", "")
                    mod = issue.get("module", "")
                    desc = issue.get("description", "")
                    count = issue.get("flagged_by_count")
                    tag = f" _(flagged by {count} reviewer{'s' if count != 1 else ''})_" if count else ""
                    lines.append(f"- **[{sev}]** `{mod}`: {desc}{tag}")
            elif not summary:
                lines.append("No issues found.")
            text = "\n".join(lines)
        elif "fixed_code" in result and isinstance(result.get("fixed_code"), dict):
            # agents/fixer_pool.py's "fixer" shape.
            text = _render_code_modules(result["fixed_code"])
        elif result.get("code"):
            text = result["code"]
        elif result.get("answer"):
            text = str(result["answer"])
        elif "papers" in result and isinstance(result.get("field_names"), list):
            # agents/extraction_table_builder.py's shape (Part 3 §3.5) —
            # checked via field_names specifically so this doesn't also
            # catch agents/academic_search.py's {"papers", "edges_written"}
            # shape, which has no field_names and reads better as its own
            # summary line below.
            text = _render_extraction_table(result)
        elif _looks_like_module_map(result):
            # agents/code_writers.py ("implementer") / agents/test_writer.py
            # ("test_writer") flat {module: code} shape, including the
            # legitimate empty-dict "no tests generated" case.
            text = _render_code_modules(result)
        elif isinstance(result.get("summary"), str) and result.get("summary"):
            # Part 3's other real-action roles (academic_search,
            # contradiction_prefilter, source_quality_flagger,
            # citation_graph_builder, ...) all already produce a
            # human-readable "summary" string for exactly this purpose —
            # use it instead of falling through to a raw JSON dump.
            text = result["summary"]
        else:
            # Genuinely unrecognized shape — still don't print a raw
            # Python repr; pretty-printed JSON (double-quoted, indented)
            # in a fenced block is at least readable and copy-pasteable.
            try:
                text = "```json\n" + json.dumps(result, indent=2, default=str) + "\n```"
            except Exception:
                text = str(result)
    else:
        text = str(result)

    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [truncated, {len(text)} chars total]"