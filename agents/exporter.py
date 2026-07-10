"""
agents/exporter.py — mechanical export of a node/artifact into a target
file format. No LLM call here, by design: same discipline as
agents/file_manager.py — deterministic and auditable, not generative.

Every exporter in this module takes the SAME input shape (the "common
artifact shape" from Part 0 §0.5), regardless of which domain produced
it:

    {
        "title": str,
        "sections": [
            {"heading": str, "content": str, "node_refs": [node_id, ...]},
            ...
        ],
        "metadata": {  # optional, all keys optional
            "workspace_id": str,
            "tags": [str, ...],
            "created_by": str,
            "created_at": str,
        },
    }

Notes/Research/Plan/etc. do not get their own exporter. A domain's report
generator, PRD writer, or note just needs one small adapter that shapes
its own object into the above before calling export_artifact() — see
graph/adapters.py for examples of that adapter layer.

Place this file at: agents/exporter.py
"""

import os
import csv
import json
import re

from docx import Document
from docx.shared import Pt

from pptx import Presentation
from pptx.util import Inches, Pt as PptxPt

from openpyxl import Workbook

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import inch


SUPPORTED_FORMATS = ("docx", "pptx", "xlsx", "csv", "pdf", "md", "json")


# ---------------------------------------------------------------------------
# Shared validation / path safety (same pattern as file_manager.py's
# _safe_relpath — an exporter has no business writing outside the root
# it was told to write into).
# ---------------------------------------------------------------------------

def _validate_artifact(artifact: dict) -> dict:
    """Fills in defaults rather than raising on minor omissions — an
    artifact with a title and no sections (or vice versa) is still
    something worth exporting to *some* format, so this only raises on
    the one thing that would make every exporter below fail identically:
    a non-dict input.
    """
    if not isinstance(artifact, dict):
        raise ValueError("artifact must be a dict shaped like "
                          "{title, sections: [{heading, content, node_refs}]}")
    artifact = dict(artifact)
    artifact.setdefault("title", "Untitled")
    artifact.setdefault("sections", [])
    artifact.setdefault("metadata", {})
    normalized_sections = []
    for s in artifact["sections"]:
        normalized_sections.append({
            "heading": s.get("heading", ""),
            "content": s.get("content", "") or "",
            "node_refs": s.get("node_refs", []) or [],
        })
    artifact["sections"] = normalized_sections
    return artifact


def _safe_output_path(output_dir: str, filename: str) -> str:
    """Confines the write target to output_dir, same reasoning as
    file_manager.py's _safe_relpath — a filename derived from a
    user-editable title (see _slugify_filename) must never be allowed
    to escape the intended export directory via '../' tricks.
    """
    os.makedirs(output_dir, exist_ok=True)
    full = os.path.normpath(os.path.join(output_dir, filename))
    root = os.path.normpath(output_dir)
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"Rejected unsafe export path: {filename}")
    return full


def _slugify_filename(title: str, ext: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.strip().lower()).strip("_")
    slug = slug[:max_len] or "untitled"
    return f"{slug}.{ext}"


# ---------------------------------------------------------------------------
# Format writers — each takes (artifact, output_path) and returns nothing;
# the dispatcher below handles path construction so these stay simple.
# ---------------------------------------------------------------------------

def _write_docx(artifact: dict, output_path: str) -> None:
    doc = Document()
    title_heading = doc.add_heading(artifact["title"], level=0)
    for section in artifact["sections"]:
        if section["heading"]:
            doc.add_heading(section["heading"], level=1)
        for para in (section["content"] or "").split("\n\n"):
            para = para.strip()
            if para:
                doc.add_paragraph(para)
        if section["node_refs"]:
            note = doc.add_paragraph()
            run = note.add_run("Sources: " + ", ".join(section["node_refs"]))
            run.italic = True
            run.font.size = Pt(9)
    doc.save(output_path)


def _write_pptx(artifact: dict, output_path: str) -> None:
    prs = Presentation()
    title_layout = prs.slide_layouts[0]
    body_layout = prs.slide_layouts[1]

    title_slide = prs.slides.add_slide(title_layout)
    title_slide.shapes.title.text = artifact["title"]
    if artifact["sections"]:
        title_slide.placeholders[1].text = (
            f"{len(artifact['sections'])} section(s)"
        )

    for section in artifact["sections"]:
        slide = prs.slides.add_slide(body_layout)
        slide.shapes.title.text = section["heading"] or artifact["title"]
        body = slide.placeholders[1].text_frame
        body.clear()
        lines = [ln for ln in (section["content"] or "").split("\n") if ln.strip()]
        if not lines:
            lines = [""]
        body.text = lines[0]
        for line in lines[1:]:
            p = body.add_paragraph()
            p.text = line
        if section["node_refs"]:
            p = body.add_paragraph()
            p.text = "Sources: " + ", ".join(section["node_refs"])
            p.font.size = PptxPt(10)
            p.font.italic = True
    prs.save(output_path)


def _write_xlsx(artifact: dict, output_path: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = artifact["title"][:31] or "Sheet1"  # Excel's 31-char sheet-name limit
    ws.append(["heading", "content", "node_refs"])
    for section in artifact["sections"]:
        ws.append([
            section["heading"],
            section["content"],
            ", ".join(section["node_refs"]),
        ])
    wb.save(output_path)


def _write_csv(artifact: dict, output_path: str) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["heading", "content", "node_refs"])
        for section in artifact["sections"]:
            writer.writerow([
                section["heading"],
                section["content"],
                ", ".join(section["node_refs"]),
            ])


def _write_pdf(artifact: dict, output_path: str) -> None:
    # Export-only, per Part 0 §0.5 — PDF import is a Notes-domain
    # ingestion concern (Part 4), not this module's job.
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(output_path, pagesize=LETTER,
                             topMargin=inch, bottomMargin=inch)
    flow = [Paragraph(artifact["title"], styles["Title"]), Spacer(1, 16)]
    for section in artifact["sections"]:
        if section["heading"]:
            flow.append(Paragraph(section["heading"], styles["Heading2"]))
            flow.append(Spacer(1, 6))
        for para in (section["content"] or "").split("\n\n"):
            para = para.strip()
            if para:
                # reportlab's Paragraph treats bare text as XML-ish markup;
                # escape the handful of characters that matter so content
                # containing "<", ">", "&" doesn't break rendering.
                escaped = (para.replace("&", "&amp;")
                               .replace("<", "&lt;")
                               .replace(">", "&gt;"))
                flow.append(Paragraph(escaped, styles["BodyText"]))
                flow.append(Spacer(1, 6))
        if section["node_refs"]:
            flow.append(Paragraph(
                "<i>Sources: " + ", ".join(section["node_refs"]) + "</i>",
                styles["BodyText"],
            ))
            flow.append(Spacer(1, 10))
    doc.build(flow)


def _write_md(artifact: dict, output_path: str) -> None:
    lines = [f"# {artifact['title']}", ""]
    for section in artifact["sections"]:
        if section["heading"]:
            lines.append(f"## {section['heading']}")
            lines.append("")
        if section["content"]:
            lines.append(section["content"])
            lines.append("")
        if section["node_refs"]:
            lines.append(f"*Sources: {', '.join(section['node_refs'])}*")
            lines.append("")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def _write_json(artifact: dict, output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2, ensure_ascii=False)


_WRITERS = {
    "docx": _write_docx,
    "pptx": _write_pptx,
    "xlsx": _write_xlsx,
    "csv": _write_csv,
    "pdf": _write_pdf,
    "md": _write_md,
    "json": _write_json,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def export_artifact(artifact: dict, fmt: str, output_dir: str,
                     filename: str = None) -> str:
    """Mechanically writes `artifact` (the common {title, sections} shape)
    to `output_dir` in the requested format. Returns the full path written.

    This is the one function every domain (Notes, Research, Plan, ...)
    calls — no domain writes its own DOCX/PPTX/etc. writer. A domain's
    generator output just needs shaping into the common artifact form
    first (see graph/adapters.py) via a small per-domain adapter.
    """
    fmt = fmt.lower().lstrip(".")
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported export format '{fmt}'. "
                          f"Supported: {', '.join(SUPPORTED_FORMATS)}")

    artifact = _validate_artifact(artifact)
    filename = filename or _slugify_filename(artifact["title"], fmt)
    if not filename.endswith(f".{fmt}"):
        filename = f"{filename}.{fmt}"

    output_path = _safe_output_path(output_dir, filename)
    _WRITERS[fmt](artifact, output_path)
    return output_path


if __name__ == "__main__":
    demo = {
        "title": "Demo Export",
        "sections": [
            {"heading": "Overview", "content": "First paragraph.\n\nSecond paragraph.",
             "node_refs": ["node:ws1:abc123"]},
            {"heading": "Details", "content": "Line one\nLine two", "node_refs": []},
        ],
        "metadata": {"workspace_id": "ws1"},
    }
    for f in SUPPORTED_FORMATS:
        path = export_artifact(demo, f, "/tmp/export_demo")
        print(f"wrote {path}")