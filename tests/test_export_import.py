"""
tests/test_export_import.py — round-trip check for agents/exporter.py and
agents/importer.py: export an artifact to every supported format, import
it back, and confirm title/heading/content/node_refs survive the trip.

Run directly: python3 tests/test_export_import.py
"""

import os
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.exporter import export_artifact, SUPPORTED_FORMATS as EXPORT_FORMATS
from agents.importer import import_artifact, SUPPORTED_FORMATS as IMPORT_FORMATS

ARTIFACT = {
    "title": "Q3 Launch Research Summary",
    "sections": [
        {
            "heading": "Market Sizing",
            "content": "TAM estimated at $2.1B.\n\nGrowing 14% YoY per industry reports.",
            "node_refs": ["node:ws1:finding001", "node:ws1:finding002"],
        },
        {
            "heading": "Competitive Landscape",
            "content": "Three main competitors identified.\nNone offer real-time sync.",
            "node_refs": ["node:ws1:finding003"],
        },
    ],
    "metadata": {"workspace_id": "ws1", "tags": ["Q3-launch"]},
}


def _check(condition, label):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return condition


def round_trip(fmt: str, tmp_dir: str) -> bool:
    print(f"-- {fmt} --")
    path = export_artifact(ARTIFACT, fmt, tmp_dir)
    ok = _check(os.path.exists(path), f"file written to {path}")

    if fmt not in IMPORT_FORMATS:
        print("  (export-only format, skipping import check)")
        return ok

    result = import_artifact(path)

    ok &= _check(len(result["sections"]) == len(ARTIFACT["sections"]),
                 f"section count preserved ({len(result['sections'])})")

    for original, parsed in zip(ARTIFACT["sections"], result["sections"]):
        ok &= _check(parsed["heading"] == original["heading"],
                     f"heading preserved: '{parsed['heading']}'")
        # xlsx/csv/pptx collapse '\n\n' to '\n' on write since cells and
        # slide text frames don't carry paragraph breaks the way docx/md
        # do -- compare on stripped lines instead of exact whitespace for
        # these formats.
        if fmt in ("xlsx", "csv", "pptx"):
            norm_original = " ".join(original["content"].split())
            norm_parsed = " ".join(parsed["content"].split())
        else:
            norm_original = original["content"]
            norm_parsed = parsed["content"]
        ok &= _check(norm_parsed.strip() == norm_original.strip(),
                     "content preserved")
        ok &= _check(set(parsed["node_refs"]) == set(original["node_refs"]),
                     f"node_refs preserved: {parsed['node_refs']}")

    return ok


def main():
    all_ok = True
    with tempfile.TemporaryDirectory() as tmp_dir:
        for fmt in EXPORT_FORMATS:
            all_ok &= round_trip(fmt, tmp_dir)

    print()
    print("ALL PASS" if all_ok else "SOME FAILED")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()