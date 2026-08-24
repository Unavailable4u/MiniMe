"""
tests/unit/test_agent_handoff_packager.py — Patch 7f-5.

Covers agents/handoff_packager.py: the §5.6 plan->coding handoff.

  1. The deterministic PRD-parsing helpers (_extract_features/
     _extract_target_feature/_extract_cycle_goal/_extract_title/
     _extract_bullet_names) — no LLM call anywhere in this module, so
     these are exercised directly against markdown fixtures.
  2. run_handoff_packager(): raises MissingDependencyError("prd_writer")
     when prd_writer hasn't produced anything yet; on the happy path,
     scopes a fresh app_slug, pre-fills KEYS["current_plan"]/
     KEYS["feature_status"]/KEYS["original_idea"], bundles the human-
     readable {title, sections} package (including the optional
     architecture/schema/api-contract/critique/feasibility sections
     when present), and fires the PLAN_HANDOFF event.

No LLM calls in this module (mock_llm is not needed). memory.bus reads/
writes go through the autouse `fake_bus` fixture. Because
stage_output:{session_id}:{role} keys ARE namespaced by the active
app_slug (see memory/bus.py's _namespaced() exemption list, which does
NOT include stage_output:), tests that need run_handoff_packager() to
find those docs pin the app_slug context to the same slug run() will
itself compute (title/session_id are fully controlled in each test), then
seed everything under that context before calling run().
"""
from unittest.mock import MagicMock

import pytest

import agents.handoff_packager as handoff_packager
from agents.architecture_diagrammer import ARCHITECTURE_DIAGRAM_KEY
from agents.schema_diagrammer import SCHEMA_DIAGRAM_KEY
from memory.bus import write, read, KEYS, set_app_slug, slugify
from eo.errors import MissingDependencyError


# ---------------------------------------------------------------------------
# 1. Deterministic PRD-parsing helpers
# ---------------------------------------------------------------------------

class TestExtractBulletNames:
    def test_strips_bold_and_takes_text_before_dash_separator(self):
        section = "- **Login** - lets a user sign in\n- **Signup**: creates an account"
        assert handoff_packager._extract_bullet_names(section) == ["Login", "Signup"]

    def test_plain_bullet_with_no_separator_kept_whole(self):
        section = "- Dashboard"
        assert handoff_packager._extract_bullet_names(section) == ["Dashboard"]

    def test_ignores_non_bullet_lines(self):
        section = "Some intro text.\n- Real bullet\nMore prose."
        assert handoff_packager._extract_bullet_names(section) == ["Real bullet"]


class TestExtractFeatures:
    def test_features_section_used_for_both_when_no_priorities_section(self):
        prd = (
            "# My App\n\n"
            "## Features\n"
            "- **Login** - auth\n"
            "- **Search** - find stuff\n"
        )
        features, priorities = handoff_packager._extract_features(prd)
        assert features == ["Login", "Search"]
        assert priorities == ["Login", "Search"]

    def test_distinct_priorities_section_overrides_priority_ordering(self):
        prd = (
            "# My App\n\n"
            "## Key Features\n"
            "- Login\n"
            "- Search\n"
            "- Export\n\n"
            "## Priorities\n"
            "- Export\n"
            "- Login\n"
        )
        features, priorities = handoff_packager._extract_features(prd)
        assert features == ["Login", "Search", "Export"]
        assert priorities == ["Export", "Login"]

    def test_no_features_section_falls_back_to_single_mvp_feature(self):
        prd = "# My App\n\nJust some prose, no headings that match.\n"
        features, priorities = handoff_packager._extract_features(prd)
        assert features == ["MVP"]
        assert priorities == ["MVP"]


class TestExtractTargetFeature:
    def test_explicit_first_cycle_section_names_a_known_feature(self):
        prd = (
            "## First Cycle Scope\n"
            "Build out Search first, everything else waits.\n"
        )
        target = handoff_packager._extract_target_feature(prd, ["Login", "Search"])
        assert target == "Search"

    def test_falls_back_to_highest_priority_feature_when_no_section(self):
        target = handoff_packager._extract_target_feature("no matching headings here", ["Login", "Search"])
        assert target == "Login"

    def test_falls_back_when_section_exists_but_names_no_known_feature(self):
        prd = "## MVP Scope\nSomething unrelated entirely.\n"
        target = handoff_packager._extract_target_feature(prd, ["Login", "Search"])
        assert target == "Login"


class TestExtractCycleGoal:
    def test_uses_first_nonblank_line_of_cycle_goal_section(self):
        prd = "## Cycle Goal\n\nShip a working login flow end to end.\nExtra detail line.\n"
        goal = handoff_packager._extract_cycle_goal(prd, "Login")
        assert goal == "Ship a working login flow end to end."

    def test_falls_back_to_synthesized_goal_when_no_section(self):
        goal = handoff_packager._extract_cycle_goal("no matching section", "Login")
        assert goal == "Implement Login as scoped in the PRD's first cycle."


class TestExtractTitle:
    def test_h1_heading_used_as_title(self):
        assert handoff_packager._extract_title("# Real Title\n\nbody", "fallback") == "Real Title"

    def test_falls_back_when_no_h1_present(self):
        assert handoff_packager._extract_title("## Only an h2\n\nbody", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# 2. run_handoff_packager()
# ---------------------------------------------------------------------------

def _seed_stage(session_id, role, text):
    write(f"stage_output:{session_id}:{role}", text)


class TestRunHandoffPackager:
    def test_raises_missing_dependency_when_no_prd(self):
        with pytest.raises(MissingDependencyError) as exc_info:
            handoff_packager.run_handoff_packager(session_id="empty-session")
        assert exc_info.value.required_role == "prd_writer"

    def test_happy_path_minimal_prd(self, monkeypatch):
        emitted = []
        monkeypatch.setattr(handoff_packager, "emit_event",
                             lambda *a, **k: emitted.append((a, k)))

        session_id = "s1"
        prd = (
            "# Todo App\n\n"
            "## Features\n"
            "- **Add Task** - create a todo\n"
            "- **Complete Task** - mark it done\n\n"
            "## First Cycle Scope\n"
            "Start with Complete Task.\n\n"
            "## Cycle Goal\n"
            "Get task completion working end to end.\n"
        )
        _seed_stage(session_id, "prd_writer", prd)

        result = handoff_packager.run_handoff_packager(session_id=session_id, task_text="fallback")

        expected_slug = f"{slugify('Todo App')}_{session_id[:8]}"
        assert result["app_slug"] == expected_slug
        assert result["current_plan"] == {
            "features": ["Add Task", "Complete Task"],
            "priorities": ["Add Task", "Complete Task"],
            "target_feature": "Complete Task",
            "cycle_goal": "Get task completion working end to end.",
        }
        assert result["package"]["title"] == "Todo App"
        # Only the PRD section — nothing else was seeded.
        assert [s["heading"] for s in result["package"]["sections"]] == ["PRD"]
        assert "Complete Task" in result["summary"]
        assert expected_slug in result["summary"]

        # Bus writes landed under the scoped app_slug namespace.
        set_app_slug(expected_slug)
        assert read(KEYS["current_plan"]) == result["current_plan"]
        assert read(KEYS["feature_status"]) == {"Add Task": "missing", "Complete Task": "missing"}
        assert read(handoff_packager.PLAN_HANDOFF_PACKAGE_KEY) == result["package"]

        assert len(emitted) == 1
        args, kwargs = emitted[0]
        assert kwargs["payload"]["target_feature"] == "Complete Task"
        assert kwargs["payload"]["feature_count"] == 2

    def test_happy_path_bundles_optional_sections_when_present(self):
        session_id = "s2"
        title = "Widget Planner"
        expected_slug = f"{slugify(title)}_{session_id[:8]}"

        # stage_output:*/ARCHITECTURE_DIAGRAM_KEY/SCHEMA_DIAGRAM_KEY are all
        # namespaced by the active app_slug, and run_handoff_packager()
        # itself calls set_app_slug(expected_slug) partway through — pin
        # the context to that same, fully-predictable slug up front so
        # everything seeded here and everything the run itself later reads
        # land in the same place.
        set_app_slug(expected_slug)

        prd = f"# {title}\n\n## Features\n- Widget Builder\n"
        _seed_stage(session_id, "prd_writer", prd)
        write(ARCHITECTURE_DIAGRAM_KEY, {"mermaid": "graph TD; A-->B;"})
        write(SCHEMA_DIAGRAM_KEY, {"mermaid": "erDiagram"})
        _seed_stage(session_id, "api_contract_writer", "POST /widgets")
        _seed_stage(session_id, "devils_advocate", "What about scale?")
        _seed_stage(session_id, "feasibility_estimator", "Roughly a week of work.")

        result = handoff_packager.run_handoff_packager(session_id=session_id)

        headings = [s["heading"] for s in result["package"]["sections"]]
        assert headings[0] == "PRD"
        assert "Architecture Diagram" in headings
        assert "Schema Diagram" in headings
        assert "API Contract" in headings
        assert "Devil's Advocate Critique" in headings
        assert any(h.startswith("Feasibility") for h in headings)

        architecture_section = next(s for s in result["package"]["sections"] if s["heading"] == "Architecture Diagram")
        assert "graph TD; A-->B;" in architecture_section["content"]
