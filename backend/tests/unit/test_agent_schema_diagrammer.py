"""
tests/unit/test_agent_schema_diagrammer.py — Patch 7f-2.

Covers agents/schema_diagrammer.py, the erDiagram sibling of
agents/architecture_diagrammer.py -- same JSON-proposes/Mermaid-renders
split, same _read_prd_context()/MissingDependencyError contract (see
that module's test file for the shared reasoning). Only what's actually
different here gets its own coverage: the erDiagram-specific renderer
and RELATIONSHIP_TOKENS mapping.
"""
import json

import pytest

from agents import schema_diagrammer
from eo.errors import MissingDependencyError
from memory.bus import read, write


def _seed_prd(session_id, text):
    write(f"stage_output:{session_id}:prd_writer", text)


# ---------------------------------------------------------------------------
# 1. _read_prd_context()
# ---------------------------------------------------------------------------

class TestReadPrdContext:
    def test_reads_prd_writer_output(self):
        _seed_prd("s1", "The PRD body.")
        assert schema_diagrammer._read_prd_context("s1") == "The PRD body."

    def test_falls_back_to_intake_interviewer_when_no_prd(self):
        write("stage_output:s2:intake_interviewer", "Intake restatement.")
        assert schema_diagrammer._read_prd_context("s2") == "Intake restatement."

    def test_raises_missing_dependency_when_neither_present(self):
        with pytest.raises(MissingDependencyError) as exc_info:
            schema_diagrammer._read_prd_context("s3-nothing-written")
        assert exc_info.value.required_role == "prd_writer"


# ---------------------------------------------------------------------------
# 2. _build_schema_mermaid() — deterministic erDiagram renderer
# ---------------------------------------------------------------------------

class TestBuildSchemaMermaid:
    def test_entity_block_with_fields(self):
        plan = {
            "entities": [{
                "name": "user", "label": "User",
                "fields": [{"name": "id", "type": "uuid"}, {"name": "email", "type": "string"}],
            }],
            "relationships": [],
        }
        mermaid = schema_diagrammer._build_schema_mermaid(plan)
        assert mermaid.startswith("erDiagram")
        assert "uuid id" in mermaid
        assert "string email" in mermaid

    def test_entity_name_uppercased_and_sanitized(self):
        plan = {"entities": [{"name": "user_account", "fields": []}], "relationships": []}
        mermaid = schema_diagrammer._build_schema_mermaid(plan)
        assert "NE_USER_ACCOUNT {" in mermaid

    def test_relationship_uses_correct_crows_foot_token(self):
        plan = {
            "entities": [{"name": "user", "fields": []}, {"name": "order", "fields": []}],
            "relationships": [{"from": "user", "to": "order", "type": "one_to_many", "label": "places"}],
        }
        mermaid = schema_diagrammer._build_schema_mermaid(plan)
        assert "||--o{" in mermaid
        assert '"places"' in mermaid

    def test_unknown_relationship_type_falls_back_to_default_token(self):
        plan = {
            "entities": [{"name": "a", "fields": []}, {"name": "b", "fields": []}],
            "relationships": [{"from": "a", "to": "b", "type": "bogus_type", "label": "x"}],
        }
        mermaid = schema_diagrammer._build_schema_mermaid(plan)
        assert "||--o{" in mermaid  # default from RELATIONSHIP_TOKENS.get(..., "||--o{")

    def test_missing_field_name_and_type_default_to_field_and_string(self):
        plan = {"entities": [{"name": "user", "fields": [{}]}], "relationships": []}
        mermaid = schema_diagrammer._build_schema_mermaid(plan)
        assert "string field" in mermaid

    def test_missing_relationship_label_defaults_to_relates_to(self):
        plan = {
            "entities": [{"name": "a", "fields": []}, {"name": "b", "fields": []}],
            "relationships": [{"from": "a", "to": "b", "type": "one_to_one"}],
        }
        mermaid = schema_diagrammer._build_schema_mermaid(plan)
        assert '"relates to"' in mermaid

    def test_all_four_relationship_types_map_to_distinct_tokens(self):
        assert schema_diagrammer.RELATIONSHIP_TOKENS == {
            "one_to_one": "||--||",
            "one_to_many": "||--o{",
            "many_to_one": "}o--||",
            "many_to_many": "}o--o{",
        }


# ---------------------------------------------------------------------------
# 3. run_schema_diagrammer() — end to end
# ---------------------------------------------------------------------------

class TestRunSchemaDiagrammer:
    def test_missing_dependency_raised_when_no_prd_exists(self):
        with pytest.raises(MissingDependencyError):
            schema_diagrammer.run_schema_diagrammer(session_id="s-missing")

    def test_valid_json_response_produces_plan_and_mermaid(self, mock_llm):
        _seed_prd("s-ok", "A PRD describing user accounts and saved items.")
        mock_llm.set_json_response({
            "entities": [{"name": "user", "label": "User", "fields": [{"name": "id", "type": "uuid"}]}],
            "relationships": [],
        })
        result = schema_diagrammer.run_schema_diagrammer(session_id="s-ok")
        assert result["plan"]["entities"][0]["name"] == "user"
        assert result["text"] == result["mermaid"]

    def test_unparseable_json_falls_back_to_schema_unavailable_entity(self, mock_llm):
        _seed_prd("s-bad-json", "A PRD.")
        mock_llm.set_response("Sure, here's the schema: not valid json at all")
        result = schema_diagrammer.run_schema_diagrammer(session_id="s-bad-json")
        assert result["plan"]["entities"] == [
            {"name": "unavailable", "label": "Schema unavailable", "fields": []}
        ]
        assert result["plan"]["relationships"] == []

    def test_result_written_to_memory_bus(self, mock_llm):
        _seed_prd("s-write", "A PRD.")
        mock_llm.set_json_response({"entities": [], "relationships": []})
        schema_diagrammer.run_schema_diagrammer(session_id="s-write")
        stored = read(schema_diagrammer.SCHEMA_DIAGRAM_KEY)
        assert stored is not None
        assert "mermaid" in stored

    def test_fenced_json_response_is_stripped_before_parsing(self, mock_llm):
        _seed_prd("s-fenced", "A PRD.")
        mock_llm.set_response(
            "```json\n" + json.dumps({"entities": [], "relationships": []}) + "\n```"
        )
        result = schema_diagrammer.run_schema_diagrammer(session_id="s-fenced")
        assert result["plan"]["entities"] == []
