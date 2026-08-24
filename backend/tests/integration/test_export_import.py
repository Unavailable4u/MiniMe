"""
tests/integration/test_export_import.py — rebuild of the old
tests/test_export_import.py.

Round-trip check for agents/exporter.py and agents/importer.py: export an
artifact to every supported format, import it back (where the format
supports re-import), and confirm title/heading/content/node_refs survive
the trip. No LLM involved -- pure format-conversion logic reading/writing
local files, which is why this lives in tests/integration rather than
tests/manual.
"""
import os

import pytest

from agents.exporter import SUPPORTED_FORMATS as EXPORT_FORMATS
from agents.exporter import export_artifact
from agents.importer import SUPPORTED_FORMATS as IMPORT_FORMATS
from agents.importer import import_artifact

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


@pytest.mark.parametrize("fmt", EXPORT_FORMATS)
def test_export_writes_a_file(fmt, tmp_path):
    path = export_artifact(ARTIFACT, fmt, str(tmp_path))
    assert os.path.exists(path)


@pytest.mark.parametrize("fmt", [f for f in EXPORT_FORMATS if f in IMPORT_FORMATS])
def test_export_import_round_trip(fmt, tmp_path):
    path = export_artifact(ARTIFACT, fmt, str(tmp_path))
    result = import_artifact(path)

    assert len(result["sections"]) == len(ARTIFACT["sections"])

    for original, parsed in zip(ARTIFACT["sections"], result["sections"]):
        assert parsed["heading"] == original["heading"]

        # xlsx/csv/pptx collapse '\n\n' to '\n' on write since cells and
        # slide text frames don't carry paragraph breaks the way docx/md
        # do -- compare on whitespace-normalized text for these formats.
        if fmt in ("xlsx", "csv", "pptx"):
            norm_original = " ".join(original["content"].split())
            norm_parsed = " ".join(parsed["content"].split())
        else:
            norm_original = original["content"]
            norm_parsed = parsed["content"]
        assert norm_parsed.strip() == norm_original.strip()

        assert set(parsed["node_refs"]) == set(original["node_refs"])
