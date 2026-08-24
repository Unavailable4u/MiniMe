"""
tests/unit/test_agent_architecture_diagrammer.py — Patch 7f-2.

Covers agents/architecture_diagrammer.py: the JSON-proposes /
Mermaid-renders split copied from agents/structure_architect.py (see
this module's own docstring). generate_text is patched via the
`mock_llm` fixture (same as test_agent_prompt_writer.py etc.) since
architecture_diagrammer imports it directly
(`from utils.llm_client import generate_text`). read_stage_output_text
goes through memory.bus, which the autouse `fake_bus` fixture already
isolates, so it's exercised for real via memory.bus.write().
"""
import json

import pytest

import agents.architecture_diagrammer as architecture_diagrammer
from memory.bus import write
from eo.errors import MissingDependencyError


def _seed_prd(session_id, text):
    write(f"stage_output:{session_id}:prd_writer", text)


# ---------------------------------------------------------------------------
# 1. _read_prd_context()
# ---------------------------------------------------------------------------

class TestReadPrdContext:
    def test_reads_prd_writer_output(self):
        _seed_prd("s1", "The PRD body.")
        assert architecture_diagrammer._read_prd_context("s1") == "The PRD body."

    def test_falls_back_to_intake_interviewer_when_no_prd(self):
        write("stage_output:s2:intake_interviewer", "Intake restatement.")
        assert architecture_diagrammer._read_prd_context("s2") == "Intake restatement."

    def test_raises_missing_dependency_when_neither_present(self):
        with pytest.raises(MissingDependencyError) as exc_info:
            architecture_diagrammer._read_prd_context("s3-nothing-written")
        assert exc_info.value.required_role == "prd_writer"


# ---------------------------------------------------------------------------
# 2. _build_architecture_mermaid() — deterministic renderer
# ---------------------------------------------------------------------------

class TestBuildArchitectureMermaid:
    def test_component_diagram_default_type(self):
        plan = {
            "diagram_type": "component",
            "components": [
                {"id": "api", "label": "API Server", "kind": "service"},
                {"id": "db", "label": "Postgres", "kind": "database"},
            ],
            "edges": [{"from": "api", "to": "db", "label": "reads/writes"}],
        }
        mermaid = architecture_diagrammer._build_architecture_mermaid(plan)
        assert mermaid.startswith("graph TD")
        assert "API Server" in mermaid
        assert "-->|reads/writes|" in mermaid

    def test_missing_diagram_type_defaults_to_component(self):
        plan = {"components": [{"id": "a", "label": "A", "kind": "service"}], "edges": []}
        mermaid = architecture_diagrammer._build_architecture_mermaid(plan)
        assert mermaid.startswith("graph TD")

    def test_database_kind_uses_cylinder_shape(self):
        plan = {"components": [{"id": "db", "label": "DB", "kind": "database"}], "edges": []}
        mermaid = architecture_diagrammer._build_architecture_mermaid(plan)
        assert '[("DB")]' in mermaid

    def test_unknown_kind_falls_back_to_default_shape(self):
        plan = {"components": [{"id": "x", "label": "X", "kind": "something-weird"}], "edges": []}
        mermaid = architecture_diagrammer._build_architecture_mermaid(plan)
        assert '["X"]' in mermaid

    def test_edge_without_label_omits_pipe_syntax(self):
        plan = {
            "components": [{"id": "a", "label": "A", "kind": "service"}, {"id": "b", "label": "B", "kind": "service"}],
            "edges": [{"from": "a", "to": "b", "label": ""}],
        }
        mermaid = architecture_diagrammer._build_architecture_mermaid(plan)
        assert "-->|" not in mermaid
        assert "-->" in mermaid

    def test_sequence_diagram_type(self):
        plan = {
            "diagram_type": "sequence",
            "participants": [{"id": "client", "label": "Client"}, {"id": "api", "label": "API"}],
            "messages": [{"from": "client", "to": "api", "label": "submits request"}],
        }
        mermaid = architecture_diagrammer._build_architecture_mermaid(plan)
        assert mermaid.startswith("sequenceDiagram")
        assert "participant" in mermaid
        assert "->>" in mermaid

    def test_sequence_message_with_no_label_uses_empty_string_fallback(self):
        plan = {
            "diagram_type": "sequence",
            "participants": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "messages": [{"from": "a", "to": "b"}],
        }
        mermaid = architecture_diagrammer._build_architecture_mermaid(plan)
        assert "->>" in mermaid


# ---------------------------------------------------------------------------
# 3. run_architecture_diagrammer() — end to end
# ---------------------------------------------------------------------------

class TestRunArchitectureDiagrammer:
    def test_missing_dependency_raised_when_no_prd_exists(self):
        with pytest.raises(MissingDependencyError):
            architecture_diagrammer.run_architecture_diagrammer(session_id="s-missing")

    def test_valid_json_response_produces_plan_and_mermaid(self, mock_llm):
        _seed_prd("s-ok", "A PRD describing user accounts and an API.")
        mock_llm.set_json_response({
            "diagram_type": "component",
            "components": [{"id": "api", "label": "API Server", "kind": "service"}],
            "edges": [],
        })
        result = architecture_diagrammer.run_architecture_diagrammer(session_id="s-ok")
        assert result["plan"]["diagram_type"] == "component"
        assert "mermaid" in result
        assert result["text"] == result["mermaid"]

    def test_unparseable_json_falls_back_to_diagram_unavailable_node(self, mock_llm):
        _seed_prd("s-bad-json", "A PRD.")
        mock_llm.set_response("Sure, here's the architecture: not valid json at all")
        result = architecture_diagrammer.run_architecture_diagrammer(session_id="s-bad-json")
        assert result["plan"]["components"] == [
            {"id": "unavailable", "label": "Diagram unavailable", "kind": "other"}
        ]
        assert result["plan"]["edges"] == []

    def test_result_written_to_memory_bus(self, mock_llm):
        _seed_prd("s-write", "A PRD.")
        mock_llm.set_json_response({"diagram_type": "component", "components": [], "edges": []})
        architecture_diagrammer.run_architecture_diagrammer(session_id="s-write")

        from memory.bus import read
        stored = read(architecture_diagrammer.ARCHITECTURE_DIAGRAM_KEY)
        assert stored is not None
        assert "mermaid" in stored

    def test_fenced_json_response_is_stripped_before_parsing(self, mock_llm):
        _seed_prd("s-fenced", "A PRD.")
        mock_llm.set_response(
            "```json\n" + json.dumps({"diagram_type": "component", "components": [], "edges": []}) + "\n```"
        )
        result = architecture_diagrammer.run_architecture_diagrammer(session_id="s-fenced")
        assert result["plan"]["diagram_type"] == "component"
