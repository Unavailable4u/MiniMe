"""
eo/role_marketplace.py — Part 2 §2.6's "honest free approximation" of a
role marketplace. Real cross-user sharing needs accounts and permissions
(Part 8's optional multi-user migration) -- not built yet, so this is
deliberately NOT a marketplace: no browsing, no ratings, no live sync.
It's a file export/import, reusing the exact agents/exporter.py +
agents/importer.py pipeline Part 0 §0.5 already built (docx/pptx/xlsx/
csv/pdf/md/json, common {title, sections, metadata} artifact shape).

This is that same "one adapter per domain" glue graph/adapters.py
provides for knowledge-graph nodes (node_to_artifact() /
artifact_to_candidate_node()), applied to the two things this part's
role marketplace bullet is actually about: a role brief
(eo/registry.py's registry:role_prompts store) and a saved workflow
template (eo/structure.py's workflow_templates store). Neither is a
graph node, so graph/adapters.py itself doesn't apply here directly --
this module is its sibling for this domain, not a modification of it.

Export writes a single JSON file a user can hand-carry between their own
projects, or hand to someone else out of band. Import writes it back in
as a normal local library entry / template -- indistinguishable
afterward from one authored locally. JSON is the only format offered
here (unlike agents/exporter.py's full SUPPORTED_FORMATS): a role brief
or workflow template is metadata that needs to round-trip losslessly,
not a document meant for docx/pptx/pdf.

Place this file at: eo/role_marketplace.py
"""
from typing import Optional


# ---------------------------------------------------------------------------
# Role briefs
# ---------------------------------------------------------------------------

def role_brief_to_artifact(role_name: str) -> dict:
    """Shapes one eo/registry.py role entry into the common {title,
    sections, metadata} artifact shape agents/exporter.py consumes.
    metadata carries the full {source, updated_at, times_hired} object
    losslessly -- import reads it straight back out of metadata rather
    than trying to reconstruct it from the section body, same discipline
    graph/adapters.py's node_to_artifact() uses for a node's own fields."""
    from eo.registry import get_role_metadata
    entry = get_role_metadata(role_name)
    if entry is None:
        raise ValueError(f"'{role_name}' has never been briefed -- nothing to export.")
    return {
        "title": role_name,
        "sections": [{"heading": "Brief", "content": entry.get("brief") or "", "node_refs": []}],
        "metadata": {
            "kind": "role_brief",
            "role_name": role_name,
            "source": entry.get("source"),
            "updated_at": entry.get("updated_at"),
            "times_hired": entry.get("times_hired", 0),
        },
    }


def export_role_brief(role_name: str, output_dir: str) -> str:
    """Writes role_name's brief to a JSON file in output_dir. Returns the
    path written."""
    from agents.exporter import export_artifact
    artifact = role_brief_to_artifact(role_name)
    return export_artifact(artifact, "json", output_dir, filename=f"role_{role_name}.json")


def import_role_brief(path: str, overwrite: bool = True) -> str:
    """Reads a role-brief JSON file (as written by export_role_brief()
    here, or by someone else running this same code) and writes it into
    THIS system's role library via eo.registry.update_role_prompt() --
    tagged source="user_edited" regardless of the original source, since
    an imported brief is, from this system's point of view, exactly
    that: something a human brought in from outside, not something the
    Panel's brief writer generated locally. Returns the role_name
    written.

    overwrite=False raises if role_name already exists in the local
    library, rather than silently clobbering a brief that may already
    have local edits or hire history -- set True (the default) for the
    common "just bring this role in" case."""
    from agents.importer import import_artifact
    from eo.registry import get_role_prompt, update_role_prompt

    artifact = import_artifact(path, fmt="json")
    metadata = artifact.get("metadata", {})
    if metadata.get("kind") != "role_brief":
        raise ValueError(f"{path} is not a role-brief export "
                          f"(expected metadata.kind == 'role_brief').")
    role_name = metadata.get("role_name")
    if not role_name:
        raise ValueError(f"{path} is missing metadata.role_name.")

    if not overwrite and get_role_prompt(role_name) is not None:
        raise ValueError(f"'{role_name}' already exists in the local role "
                          f"library -- pass overwrite=True to replace it.")

    brief = artifact["sections"][0]["content"] if artifact.get("sections") else ""
    update_role_prompt(role_name, brief, source="user_edited")
    return role_name


# ---------------------------------------------------------------------------
# Workflow templates
# ---------------------------------------------------------------------------

def workflow_template_to_artifact(template_id: str) -> dict:
    """Mirrors role_brief_to_artifact() above for eo/structure.py's saved
    templates. The full template dict (roles, approval_roles,
    no_conversation_context_roles, domain_hint, ...) lives in metadata so
    nothing is lost on import, including Part 2 §2.6's nested-group
    roles shape -- the section body is just a human-readable summary for
    anyone opening the file directly, not the source of truth for
    import."""
    from eo.structure import get_workflow_template
    template = get_workflow_template(template_id)
    if template is None:
        raise ValueError(f"No saved workflow template with id '{template_id}'.")
    roles_summary = ", ".join(
        r if isinstance(r, str) else f"[{', '.join(r)}]" for r in template["roles"]
    )
    return {
        "title": template["name"],
        "sections": [{
            "heading": "Roles",
            "content": f"{roles_summary}\n\n{template.get('description') or ''}".strip(),
            "node_refs": [],
        }],
        "metadata": {
            "kind": "workflow_template",
            "name": template["name"],
            "description": template.get("description", ""),
            "roles": template["roles"],
            "domain_hint": template.get("domain_hint"),
            "approval_roles": template.get("approval_roles", []),
            "no_conversation_context_roles": template.get("no_conversation_context_roles", []),
        },
    }


def export_workflow_template(template_id: str, output_dir: str) -> str:
    """Writes a saved workflow template to a JSON file in output_dir.
    Returns the path written."""
    from agents.exporter import export_artifact
    artifact = workflow_template_to_artifact(template_id)
    filename = f"template_{artifact['title']}.json"
    return export_artifact(artifact, "json", output_dir, filename=filename)


def import_workflow_template(path: str, created_by: Optional[str] = None) -> dict:
    """Reads a workflow-template JSON file and saves it as a NEW template
    via eo.structure.save_workflow_template() -- always a fresh
    template_id, never overwriting an existing one. Unlike role briefs,
    two workflow templates with the same name are a normal, harmless
    thing to have side by side; a template_id collision across two
    different people's exports would be a real bug worth guarding
    against, so this sidesteps it entirely by always minting a new id
    locally rather than trying to reuse the exported one. Returns the
    newly saved template dict (same shape save_workflow_template()
    itself returns)."""
    from agents.importer import import_artifact
    from eo.structure import save_workflow_template

    artifact = import_artifact(path, fmt="json")
    metadata = artifact.get("metadata", {})
    if metadata.get("kind") != "workflow_template":
        raise ValueError(f"{path} is not a workflow-template export "
                          f"(expected metadata.kind == 'workflow_template').")

    return save_workflow_template(
        name=metadata.get("name") or artifact.get("title") or "Imported template",
        roles=metadata.get("roles") or [],
        description=metadata.get("description", ""),
        domain_hint=metadata.get("domain_hint"),
        approval_roles=metadata.get("approval_roles"),
        no_conversation_context_roles=metadata.get("no_conversation_context_roles"),
        created_by=created_by,
    )