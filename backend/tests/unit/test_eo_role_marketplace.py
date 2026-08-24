"""
tests/unit/test_eo_role_marketplace.py — Patch 7e-S6.

eo/role_marketplace.py had zero test coverage before this. It's a thin
adapter layer over two already-tested pieces (agents/exporter.py +
agents/importer.py's JSON round-trip, eo/registry.py's role-prompt
store, eo/structure.py's workflow-template store) -- the actual value
worth testing here is that the SHAPE contract between the two halves
(role_brief_to_artifact()'s metadata <-> import_role_brief()'s reads,
and the workflow_template equivalent) round-trips correctly, plus the
validation guards (wrong artifact kind, missing role_name,
overwrite=False collision).

Deliberately uses the REAL agents.exporter/agents.importer/eo.registry/
eo.structure functions rather than mocking them -- this module's own
value IS the glue between them, so mocking those away would test
nothing but this module's own pass-through plumbing. fake_bus
(conftest, autouse) already makes eo.registry/eo.structure's Redis
calls safe; tmp_path gives each test a real, isolated directory for
the actual JSON file exporter/importer write to and read from.
"""
import json

import pytest

from eo import role_marketplace
from eo.registry import get_role_metadata, get_role_prompt, update_role_prompt
from eo.structure import save_workflow_template

# ---------------------------------------------------------------------
# Role briefs
# ---------------------------------------------------------------------

def test_role_brief_to_artifact_shapes_a_briefed_role():
    update_role_prompt("researcher", "Do thorough research.", source="user_edited")
    artifact = role_marketplace.role_brief_to_artifact("researcher")

    assert artifact["title"] == "researcher"
    assert artifact["sections"] == [
        {"heading": "Brief", "content": "Do thorough research.", "node_refs": []}
    ]
    assert artifact["metadata"]["kind"] == "role_brief"
    assert artifact["metadata"]["role_name"] == "researcher"
    assert artifact["metadata"]["source"] == "user_edited"


def test_role_brief_to_artifact_raises_for_a_never_briefed_role():
    with pytest.raises(ValueError, match="never been briefed"):
        role_marketplace.role_brief_to_artifact("totally_unknown_role")


def test_export_then_import_role_brief_round_trips(tmp_path):
    update_role_prompt("researcher", "Do thorough research.", source="user_edited")

    path = role_marketplace.export_role_brief("researcher", str(tmp_path))
    assert path.endswith(".json")

    # Simulate a truly separate system: wipe the local role library
    # entry before importing, so the import proves it re-creates it
    # rather than just no-oping over an already-present role.
    update_role_prompt("researcher", "", source="user_edited")

    role_name = role_marketplace.import_role_brief(path)

    assert role_name == "researcher"
    assert get_role_prompt("researcher") == "Do thorough research."


def test_imported_role_brief_is_always_tagged_user_edited(tmp_path):
    update_role_prompt("researcher", "Original brief.", source="system_generated")
    path = role_marketplace.export_role_brief("researcher", str(tmp_path))

    role_marketplace.import_role_brief(path)

    assert get_role_metadata("researcher")["source"] == "user_edited"


def test_import_role_brief_rejects_a_non_role_brief_export(tmp_path):
    stray = tmp_path / "not_a_role_brief.json"
    stray.write_text(json.dumps({
        "title": "x", "sections": [], "metadata": {"kind": "workflow_template"},
    }))

    with pytest.raises(ValueError, match="not a role-brief export"):
        role_marketplace.import_role_brief(str(stray))


def test_import_role_brief_rejects_a_file_missing_role_name(tmp_path):
    stray = tmp_path / "broken.json"
    stray.write_text(json.dumps({
        "title": "x", "sections": [], "metadata": {"kind": "role_brief"},
    }))

    with pytest.raises(ValueError, match="missing metadata.role_name"):
        role_marketplace.import_role_brief(str(stray))


def test_import_role_brief_overwrite_false_raises_if_role_already_exists(tmp_path):
    update_role_prompt("researcher", "Original brief.", source="user_edited")
    path = role_marketplace.export_role_brief("researcher", str(tmp_path))

    with pytest.raises(ValueError, match="already exists"):
        role_marketplace.import_role_brief(path, overwrite=False)


def test_import_role_brief_overwrite_false_succeeds_for_a_new_role(tmp_path):
    update_role_prompt("researcher", "Original brief.", source="user_edited")
    path = role_marketplace.export_role_brief("researcher", str(tmp_path))

    # A never-before-seen role name locally -- overwrite=False should
    # not block this, since there's nothing to collide with.
    import os
    new_path = os.path.join(str(tmp_path), "role_brand_new_role.json")
    data = json.loads(open(path).read())
    data["metadata"]["role_name"] = "brand_new_role"
    with open(new_path, "w") as f:
        json.dump(data, f)

    role_name = role_marketplace.import_role_brief(new_path, overwrite=False)
    assert role_name == "brand_new_role"


# ---------------------------------------------------------------------
# Workflow templates
# ---------------------------------------------------------------------

def _saved_template():
    return save_workflow_template(
        name="Research Pipeline",
        roles=["researcher", ["draft_writer_a", "draft_writer_b"], "editor"],
        description="A two-stage research and drafting flow.",
        domain_hint="research",
        approval_roles=["editor"],
        no_conversation_context_roles=["draft_writer_a"],
    )


def test_workflow_template_to_artifact_shapes_a_saved_template():
    template = _saved_template()
    artifact = role_marketplace.workflow_template_to_artifact(template["template_id"])

    assert artifact["title"] == "Research Pipeline"
    assert artifact["metadata"]["kind"] == "workflow_template"
    assert artifact["metadata"]["roles"] == ["researcher", ["draft_writer_a", "draft_writer_b"], "editor"]
    assert artifact["metadata"]["approval_roles"] == ["editor"]
    assert artifact["metadata"]["no_conversation_context_roles"] == ["draft_writer_a"]
    assert artifact["metadata"]["domain_hint"] == "research"
    # Nested-group roles rendered readably in the human-facing summary.
    assert "[draft_writer_a, draft_writer_b]" in artifact["sections"][0]["content"]


def test_workflow_template_to_artifact_raises_for_unknown_id():
    with pytest.raises(ValueError, match="No saved workflow template"):
        role_marketplace.workflow_template_to_artifact("does-not-exist")


def test_export_then_import_workflow_template_round_trips(tmp_path):
    template = _saved_template()
    path = role_marketplace.export_workflow_template(template["template_id"], str(tmp_path))

    imported = role_marketplace.import_workflow_template(path, created_by="user_1")

    assert imported["name"] == "Research Pipeline"
    assert imported["roles"] == ["researcher", ["draft_writer_a", "draft_writer_b"], "editor"]
    assert imported["approval_roles"] == ["editor"]
    assert imported["no_conversation_context_roles"] == ["draft_writer_a"]
    assert imported["created_by"] == "user_1"
    # Always a NEW template_id, never reusing the exported one.
    assert imported["template_id"] != template["template_id"]


def test_import_workflow_template_never_collides_with_the_original():
    """Importing the same export twice creates two independent
    templates side by side -- module docstring: unlike role briefs, two
    templates with the same name are a normal, harmless thing to have,
    so this always mints a fresh id rather than raising or overwriting."""
    import tempfile
    template = _saved_template()
    with tempfile.TemporaryDirectory() as tmp:
        path = role_marketplace.export_workflow_template(template["template_id"], tmp)
        first = role_marketplace.import_workflow_template(path)
        second = role_marketplace.import_workflow_template(path)

    assert first["template_id"] != second["template_id"]
    assert first["name"] == second["name"] == "Research Pipeline"


def test_import_workflow_template_rejects_a_non_workflow_export(tmp_path):
    stray = tmp_path / "not_a_template.json"
    stray.write_text(json.dumps({
        "title": "x", "sections": [], "metadata": {"kind": "role_brief"},
    }))

    with pytest.raises(ValueError, match="not a workflow-template export"):
        role_marketplace.import_workflow_template(str(stray))


def test_import_workflow_template_falls_back_to_title_when_name_missing(tmp_path):
    stray = tmp_path / "template_no_name.json"
    stray.write_text(json.dumps({
        "title": "Fallback Title",
        "sections": [],
        "metadata": {"kind": "workflow_template", "roles": ["researcher"]},
    }))

    imported = role_marketplace.import_workflow_template(str(stray))
    assert imported["name"] == "Fallback Title"


def test_import_workflow_template_defaults_optional_fields_when_absent(tmp_path):
    stray = tmp_path / "template_minimal.json"
    stray.write_text(json.dumps({
        "title": "Minimal",
        "sections": [],
        "metadata": {"kind": "workflow_template", "name": "Minimal", "roles": ["researcher"]},
    }))

    imported = role_marketplace.import_workflow_template(str(stray))
    assert imported["approval_roles"] == []
    assert imported["no_conversation_context_roles"] == []
    assert imported["domain_hint"] is None
