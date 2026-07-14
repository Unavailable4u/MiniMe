"""
eo/structure.py — v6 migration Part 10. Each entry is the MAXIMAL set of
stages a domain could involve, in a sensible default order — a bias for
the Panel's execution_order decision, never a required route. A domain
absent from this dict, or a task the Panel judges doesn't fit any entry
here, is normal: the Panel builds an order from scratch in that case
(the prompt addition below says so explicitly).

Adding a new domain later is exactly this — one new list, no code. That's
the entire payoff of this part: domain expertise now lives in data, not
in a new agents/*.py file per role.
"""


# Migration Part 26 §4c: shared home for the "path" (str, eo/inspector.py's
# Part 12 output) <-> "tier" (int, what every other collaborator in this
# system still keys on) translation. Previously defined independently in
# eo/panel.py and eo/loop_v4.py (with loop_v4.py deriving TIER_TO_PATH as
# this dict's own inverse) -- both files' own comments said the duplication
# was deliberate, to avoid a circular import (eo.registry -> agents.
# generic_worker -> eo.panel -> eo.registry). eo/structure.py is safe for
# both of them to import from instead: it has no imports of its own, and
# eo/panel.py already imports build_reference_structure_addition from here,
# so this isn't a new dependency edge -- just one source of truth instead
# of two copies that could silently drift if "instant"/"direct"/"fixed"/
# "adaptive" ever change.
PATH_TO_TIER = {"instant": 0, "direct": 1, "fixed": 2, "adaptive": 3}
TIER_TO_PATH = {v: k for k, v in PATH_TO_TIER.items()}

STRUCTURE_TEMPLATES = {
    "coding": [
        "idea_planner", "prompt_writer",
        # Part 7 §7.3 — deliberately positioned right after prompt_writer
        # and before implementer: it only needs task_text (or, via the
        # LEGACY_BUS_KEY_MAP bridge, handoff_packager's PRD content if
        # this run started from a Plan→Build handoff), not module_specs,
        # and its output is meant to be cached once and read by later
        # cycles rather than re-derived every cycle the way idea_planner
        # re-runs. See eo/registry.py's ROLE_PROMPTS_SEED entry for why
        # this stays a generic_worker role (structured JSON via a strict
        # fenced ```json brief, same convention as "note_taker" below)
        # instead of a REAL_ACTION_ROLES module like extraction_table_builder.
        "integration_flagger",
        "implementer", "test_writer",
        "sandbox_tester", "verifier", "fixer", "security_reviewer",
        "file_manager",
        # Part 7 §7.4 — deliberately after file_manager: it needs a real
        # on-disk file tree to inspect (see agents/deploy_config_writer.py's
        # own docstring for why this is a REAL_ACTION_ROLES module, not a
        # generic_worker role, despite also never touching disk itself).
        # deploy_agent.py is NOT listed here — it's a UI-button action,
        # not a Panel-hireable role (Part 7 §7.6).
        "deploy_config_writer",
        "documentation_writer", "changelog_writer",
        "final_qa", "report_writer", "gatekeeper",
    ],
    "creative_writing": [
        "brainstormer", "outliner", "writer", "fact_checker", "editor",
    ],
    # Part 3 §3.2 — extends the original five-role synthesis/writing
    # layer (still exactly right for the end of the pipeline) with the
    # discovery/verification layer upstream of it. Menu, not a fixed
    # pipeline, same as "simulate" above: a real task hires a subset
    # (e.g. "find recent papers on X" only needs academic_search +
    # writer). academic_search, source_quality_flagger,
    # citation_graph_builder, extraction_table_builder, and (Part 3
    # §3.6) contradiction_prefilter are real-action roles
    # (REAL_ACTION_ROLES, below); contradiction_detector and
    # consensus_meter require genuine judgment and run through
    # generic_worker like any other reasoning role. contradiction_prefilter
    # is deliberately positioned directly before contradiction_detector
    # -- it's the deterministic narrowing pass contradiction_detector's
    # LLM judgment reads as prior context (agents/generic_worker.py's
    # input_keys mechanism), so hiring one without the other ahead of it
    # in the order leaves contradiction_detector with no candidates to
    # judge.
    "research": [
        "academic_search", "source_quality_flagger", "citation_graph_builder",
        "extraction_table_builder", "contradiction_prefilter", "contradiction_detector",
        "consensus_meter", "researcher", "fact_checker", "analyst", "writer", "editor",
    ],
    # Part 3 §3.7 — dataset_analyst is a real-action role (REAL_ACTION_ROLES,
    # below): it actually runs computed analysis code against the
    # dataset in a sandbox, distinct from "analyst" below, which is a
    # pure-reasoning generic_worker role that writes ABOUT a result
    # (e.g. dataset_analyst's own output, read via input_keys) rather
    # than computing one. Positioned first for the same reason
    # academic_search leads "research": the real data has to exist
    # before anything downstream can reason about it.
    "data_analysis": [
        "dataset_analyst", "analyst", "formatter", "writer", "editor",
    ],
    # Part 1 §1.2 — a menu, not a fixed pipeline (see this dict's own
    # docstring above): a real task only hires 2-4 of these. Persona
    # roles are pure reasoning (no file/API writes), so every one of them
    # already resolves to generic_worker in eo/registry.py's
    # resolve_role() with zero REAL_ACTION_ROLES changes -- see Part 1
    # §1.1. simulation_synthesizer is deliberately last: it's the one
    # non-persona role in this list (the aggregation step, §1.5), and its
    # position here is what biases the Panel to reliably order it after
    # every persona it might read from via input_keys.
    "simulate": [
        "persona_customer", "persona_skeptic", "critic_reviewer",
        "usability_walkthrough", "red_team", "pricing_sensitivity",
        "support_ticket_predictor", "competitor_response",
        # Part 1 §1.4, track 2 — a single role for LARGE, REPETITIVE-
        # persona requests ("15 marketplace reviews"), not meant to be
        # hired alongside the distinct-viewpoint personas above. Included
        # in this reference list (rather than left for the Panel to
        # invent cold) so a batch-shaped request reliably gets pointed at
        # one role instead of the Panel hiring N separate persona slots
        # for what's actually one structured-output generation task.
        "marketplace_review_batch",
        "simulation_synthesizer",
    ],
    # Part 4 §4.1 — menu, not a fixed pipeline, same as every entry above.
    # source_ingestor covers the Capture step (Part 4 §4.2); everything
    # else here is an existing role library brief reused under this
    # domain, per that section's per-case reuse-vs-alias note.
    "notes": [
        "source_ingestor", "researcher", "fact_checker", "writer",
        "mapper", "report_writer", "slide_planner", "podcast_scriptwriter",
        "infographic_designer", "flashcard_writer", "quiz_writer",
        "study_guide_writer", "editor",
    ],
    # Part 5 §5.1 — menu, not a fixed pipeline, same as every entry
    # above. Registry classification (§5.1's own table, not yet wired
    # into REAL_ACTION_ROLES until each module actually exists —
    # see eo/registry.py's REAL_ACTION_ROLES comment on why a name
    # showing up there before its REGISTRY entry exists is a guaranteed
    # KeyError, the exact bug Part 3's academic_search hit once already):
    #   generic_worker (reasoning, no dedicated module):
    #     intake_interviewer, question_forcer, prd_writer,
    #     api_contract_writer, devils_advocate, feasibility_estimator
    #   REAL_ACTION_ROLES (structured-plan-in, deterministic-render-out):
    #     architecture_diagrammer, schema_diagrammer (§5.3, Mermaid via
    #     the same JSON-proposes/code-renders split structure_architect.py
    #     already proved out — a new sibling module, not an edit to that
    #     file), handoff_packager (§5.6, zero LLM calls — pure
    #     memory-bus reads/writes into idea_planner.py's own KEYS shape)
    # question_forcer is deliberately positioned right after
    # intake_interviewer and ahead of prd_writer: it's the one role in
    # this domain marked as an approval_roles checkpoint (Part 2 §2.4)
    # so a run visibly pauses for a real human answer before prd_writer
    # is allowed to write over an unstated assumption (§5.2). wireframe_sketcher
    # is deliberately NOT in this reference list -- §5.5 calls it out as
    # optional/secondary to the core PRD, the same "not every role belongs
    # in the reference structure" allowance Part 1 §1.4 already used for
    # roles meant to be hired situationally rather than by default.
    "plan": [
        "intake_interviewer", "question_forcer", "prd_writer",
        "architecture_diagrammer", "schema_diagrammer", "api_contract_writer",
        "devils_advocate", "feasibility_estimator", "handoff_packager",
    ],
    # Part 6 §6.1 — menu, not a fixed pipeline, same as every entry above.
    # Registry classification:
    #   REAL_ACTION_ROLES: content_adapter_pool (§6.2 — real, self-
    #     contained parallel fan-out work, same category as
    #     code_writers/reviewer/fixer_pool)
    #   generic_worker (reasoning-with-structured-input, no dedicated
    #     module): brand_voice_checker, content_calendar_builder,
    #     seo_structure_auditor, outreach_categorizer — each reads
    #     workspace_facts/the handoff package the same way any
    #     multi-stage generic_worker role reads input_keys.
    # content_adapter_pool leads deliberately: everything else in this
    # domain (brand-voice check, calendar sequencing, structure audit)
    # reads ITS output via input_keys, so nothing downstream has
    # anything to read until it's run first — the same "real data has to
    # exist before anything downstream can reason about it" placement
    # research's academic_search and data_analysis's dataset_analyst
    # already use for the same reason.
    "growth": [
        "content_adapter_pool", "brand_voice_checker", "content_calendar_builder",
        "seo_structure_auditor", "outreach_categorizer", "writer", "editor",
    ],
}


def _rough_domain_guess(task_text: str) -> str | None:
    """Simple keyword heuristic, non-binding — just picks a candidate
    reference structure to show the model as a bias. The model's actual
    "domain" output in its JSON response is free to disagree with this
    guess entirely; this is only used to decide which (if any) reference
    structure gets shown alongside the prompt."""
    text = task_text.lower()
    # Checked before "coding" deliberately: coding's "app"/"code" keywords
    # are broad enough to false-positive on phrases like "app store
    # review" or "reactions to this app", which are unambiguously
    # simulate requests. These phrases are specific (mostly multi-word),
    # so checking them first costs nothing when a task is genuinely about
    # coding -- none of these phrases show up in ordinary coding requests.
    if any(kw in text for kw in (
        "simulate", "persona", "reaction", "focus group", "red team",
        "playtest", "pricing test", "customer would", "usability",
        "marketplace review", "app store review", "amazon review",
        "spread of reviews", "app review",
    )):
        return "simulate"
    # Checked before "coding" deliberately, same reasoning as "simulate"
    # just above: "launch" and "campaign" could otherwise false-positive
    # into a coding read on a phrase like "launch the app" if checked
    # after coding's broad "app" keyword. Growth's own phrases here are
    # specific enough that this costs nothing on a genuine coding request.
    if any(kw in text for kw in (
        "launch", "marketing", "social post", "campaign",
        "content calendar", "outreach",
    )):
        return "growth"
    if any(kw in text for kw in (
        "code", "bug", "function", "script", "app", "refactor", "api",
        "test", "debug", "repo", "codebase",
    )):
        return "coding"
    if any(kw in text for kw in (
        "story", "poem", "song", "lyrics", "novel", "creative", "brainstorm",
    )):
        return "creative_writing"
    # Checked before "research" deliberately: research's own "sources"
    # keyword below would otherwise catch notebook-flavored phrasing
    # first. These are mostly multi-word and specific to notebook/study
    # tooling, so this costs nothing on genuine research requests.
    if any(kw in text for kw in (
        "notebook", "sources", "flashcard", "study guide", "podcast",
        "summarize this pdf",
    )):
        return "notes"
    if any(kw in text for kw in (
        "research", "investigate", "sources", "citations", "survey",
        "paper", "citation", "literature review", "arxiv",
        "peer-reviewed", "study",
    )):
        return "research"
    if any(kw in text for kw in (
        "data", "csv", "spreadsheet", "dataset", "analysis", "chart",
    )):
        return "data_analysis"
    # Checked last, same reasoning every prior addition to this function
    # gives for its own placement: these phrases are mostly multi-word
    # and specific to plan/spec-writing, and don't overlap any keyword
    # already checked above (e.g. "api" alone is a coding keyword, but
    # "requirements doc" and "before we build" aren't ambiguous with it)
    # -- so placement here costs nothing on any earlier domain's genuine
    # requests.
    if any(kw in text for kw in (
        "prd", "spec", "blueprint", "requirements doc", "plan out",
        "before we build",
    )):
        return "plan"
    return None


PANEL_PROMPT_ADDITION = """

Also decide:
- "domain": one of {domains}, or null if none genuinely fits.
- "execution_order": the order these chosen roles (suggested_agents)
  should run in, as a list using ONLY the roles you already chose.

If a reference structure is given below for this domain, treat it as a
STRONG SUGGESTION for ordering and for noticing roles you may have
missed — never as a requirement. You may skip any stage it lists, add
stages it doesn't mention, and reorder freely. If no structure is given,
or none of it fits this task, build execution_order entirely from your
own judgment. Never fail to produce an order because the reference
structure doesn't match — your own judgment always wins.

Reference structure for this domain (if any):
{reference_structure}

Add "domain" and "execution_order" to your JSON response alongside the
fields already described above."""


def build_reference_structure_addition(task_text: str) -> str:
    """Returns the text block to append to a classification call's
    user_content — shared by eo/inspector.py's classify() (member A) and
    eo/panel.py's _get_member_vote() (members B and C), so all three
    panel members see the same domain-guess-derived reference structure
    and the same instructions, without duplicating the guessing logic in
    two files."""
    domains = list(STRUCTURE_TEMPLATES.keys())
    guessed_domain = _rough_domain_guess(task_text)
    reference = STRUCTURE_TEMPLATES.get(guessed_domain, [])
    return PANEL_PROMPT_ADDITION.format(domains=domains, reference_structure=reference or "none")


# ---------------------------------------------------------------------------
# Part 2 §2.3 — Workflow templates: a runtime-savable STRUCTURE_TEMPLATES
# entry. STRUCTURE_TEMPLATES above is already exactly the schema the
# blueprint wants (a name mapped to an ordered list of role-name
# strings); the only real gap is that it's hardcoded Python, editable
# only by redeploying. This section is the user-savable counterpart:
# same `roles` shape, stored on the memory bus instead of in source, so
# a template saved here can be copy-pasted straight into
# STRUCTURE_TEMPLATES later with zero reshaping if it turns out to be
# broadly useful.
# ---------------------------------------------------------------------------
import uuid as _uuid
from datetime import datetime as _datetime, timezone as _timezone
from memory.bus import read as _bus_read, write as _bus_write

WORKFLOW_TEMPLATES_KEY = "workflow_templates"


def _utcnow_iso() -> str:
    return _datetime.now(_timezone.utc).isoformat()


def _load_templates() -> dict:
    """Single read path — a plain {template_id: template_dict} object
    under one bus key, mirroring eo/registry.py's registry:role_prompts
    single-key-single-dict pattern (Part 2 §2.2) rather than one file
    per template. No bootstrap needed here (unlike role_prompts) --
    there's no seed data for user-saved templates, an empty store is a
    perfectly normal starting state."""
    return _bus_read(WORKFLOW_TEMPLATES_KEY, default={})


def _flatten_roles(roles: list) -> list:
    """Migration Part 2 §2.6 — roles may now contain plain role-name
    strings OR a nested list of role-name strings (a group a template
    author explicitly marked as safe to run concurrently — see
    eo/executor.py's group-execution branch). This flattens either shape
    into a pure list of role names, for any caller that needs a hire
    list rather than the grouping/ordering structure itself
    (classification_from_template()'s suggested_agents, below). A plain
    flat list of strings — every template shape from before §2.6 existed
    — flattens to exactly itself, so this is purely additive."""
    flat = []
    for entry in roles:
        if isinstance(entry, list):
            flat.extend(entry)
        else:
            flat.append(entry)
    return flat


def _validate_roles_shape(roles: list) -> None:
    """Cheap sanity check, not strict schema validation: every top-level
    entry must be a role-name string, or a list of role-name strings
    (one level of nesting only — a group of groups isn't a concept
    eo/executor.py's group-execution branch understands, so reject it
    early with a clear message instead of silently mis-flattening it or
    failing confusingly deep inside a run)."""
    for entry in roles:
        if isinstance(entry, list):
            if not all(isinstance(r, str) for r in entry):
                raise ValueError(f"workflow template group {entry!r} must contain only role-name strings")
        elif not isinstance(entry, str):
            raise ValueError(f"workflow template role entry {entry!r} must be a string or a list of strings")


def save_workflow_template(name: str, roles: list, description: str = "",
                            domain_hint: str | None = None,
                            approval_roles: list | None = None,
                            no_conversation_context_roles: list | None = None,
                            created_by: str | None = None) -> dict:
    """New in Part 2 §2.3. `roles` is the identical flat
    list-of-role-name-strings shape STRUCTURE_TEMPLATES entries already
    use, on purpose. Covers both write paths the design calls for:
    "save from a finished run" (caller passes that run's own
    execution_order as `roles`) and "build from scratch" (caller passes
    a list assembled in the Role Library UI) — both are just a plain
    list of role-name strings to this function, no distinct code path
    needed for the two.

    Migration Part 2 §2.6: a top-level entry in `roles` may now ALSO be
    a list of role-name strings — a group the template author explicitly
    marked as safe to run concurrently (e.g.
    `["idea_planner", ["draft_writer_a", "draft_writer_b"], "editor"]`).
    A template with no such grouping is stored exactly as before — this
    only activates when an author deliberately nests a sub-list, so
    every template saved before this existed keeps working unmodified.
    See eo/executor.py's group-execution branch for how a group is
    actually run once execution reaches it.

    approval_roles defaults to [] (full-auto, today's exact dispatch
    behavior) — this is the field Part 2 §2.4's human-in-the-loop
    checkpoints read at dispatch time; defined here since this is where
    the template schema itself lives, wired up for real in §2.4.

    no_conversation_context_roles defaults to [] (every role sees the
    full conversation-memory prepend, today's exact behavior) — Part 2
    §2.6's scoped-memory fix. A role name in this list is dispatched with
    generic_worker.run()'s `include_conversation_context=False`, for a
    narrow persona or single-purpose role that has no business reading
    unrelated conversation history it wasn't scoped to. Only meaningful
    for roles that actually run through generic_worker — listing a
    real-action role here (e.g. "code_writers") is harmless but has no
    effect, since only generic_worker's dispatch case in eo/executor.py
    reads this set. Whoever dispatches this template (eo/loop_v4.py or
    api/task_runner.py) is expected to pass this straight through as
    execute_graph()'s `no_conversation_context_roles` argument, the exact
    same wiring pattern approval_roles already uses."""
    _validate_roles_shape(roles)
    templates = _load_templates()
    template_id = str(_uuid.uuid4())
    template = {
        "template_id": template_id,
        "name": name,
        "description": description,
        "roles": list(roles),
        "domain_hint": domain_hint,
        "approval_roles": list(approval_roles) if approval_roles else [],
        "no_conversation_context_roles": list(no_conversation_context_roles) if no_conversation_context_roles else [],
        "created_by": created_by,
        "created_at": _utcnow_iso(),
        # Recent-templates feature — None until the template is actually
        # run for the first time via run_task_from_template(); see
        # record_template_run() below, called from api/task_runner.py.
        "last_run_at": None,
    }
    templates[template_id] = template
    _bus_write(WORKFLOW_TEMPLATES_KEY, templates)
    return template


def get_workflow_template(template_id: str) -> dict | None:
    """Returns one saved template by id, or None if it doesn't exist."""
    return _load_templates().get(template_id)


def list_workflow_templates() -> list:
    """Every saved template, newest first — for a template-picker UI."""
    templates = list(_load_templates().values())
    return sorted(templates, key=lambda t: t.get("created_at") or "", reverse=True)


def record_template_run(template_id: str) -> dict | None:
    """New — "recently run" templates feature. Stamps last_run_at on a
    template every time it's actually dispatched, called from
    api/task_runner.py's run_task_from_template() right after it
    confirms the template exists. Same single-key-single-dict store as
    everything else here, so this syncs across devices for free — no
    separate storage mechanism needed. Returns the updated template, or
    None if template_id doesn't exist (mirrors get_workflow_template()'s
    own not-found contract; the caller already raises KeyError before
    reaching this point in practice, so None here is just defensive)."""
    templates = _load_templates()
    template = templates.get(template_id)
    if template is None:
        return None
    template["last_run_at"] = _utcnow_iso()
    templates[template_id] = template
    _bus_write(WORKFLOW_TEMPLATES_KEY, templates)
    return template


def update_workflow_template(template_id: str, **fields) -> dict | None:
    """Partial update — only overwrites keys actually passed. Returns
    the updated template, or None if template_id doesn't exist."""
    templates = _load_templates()
    template = templates.get(template_id)
    if template is None:
        return None
    if "roles" in fields and fields["roles"] is not None:
        _validate_roles_shape(fields["roles"])
    for key in ("name", "description", "roles", "domain_hint", "approval_roles", "no_conversation_context_roles"):
        if key in fields and fields[key] is not None:
            template[key] = fields[key]
    templates[template_id] = template
    _bus_write(WORKFLOW_TEMPLATES_KEY, templates)
    return template


def delete_workflow_template(template_id: str) -> bool:
    """Not explicitly spelled out by the design, but a save-only
    template library with no way to remove a bad save isn't realistic
    for v1 — a thin, obvious complement to save_workflow_template().
    Returns True if a template was actually removed, False if the id
    was already gone, so a caller can tell the two apart without a
    try/except."""
    templates = _load_templates()
    if template_id not in templates:
        return False
    del templates[template_id]
    _bus_write(WORKFLOW_TEMPLATES_KEY, templates)
    return True


def classification_from_template(template: dict) -> dict:
    """New in Part 2 §2.3. Builds a classification-shaped dict from a
    saved template so it can be handed straight to
    eo.panel.staff_task() exactly like a normal Inspector/Panel
    classification would be — staff_task() itself needs ZERO changes
    for template-driven hiring, since it only ever reads
    classification.get("suggested_agents", ...) and
    classification.get("tier"). The template's own `roles` order
    doubles as both the hire list AND the desired execution_order —
    that's the same list, on purpose, since a saved template's whole
    point is "run these roles, in this order."

    Migration Part 2 §2.6: `roles` may contain nested groups now (see
    save_workflow_template()'s docstring). staff_task() only ever needs
    a flat hire list — a role still needs its own account and brief
    whether or not it's grouped with others for execution — so
    suggested_agents is `roles` FLATTENED via _flatten_roles(). But
    execution_order keeps the original, possibly-nested shape: THAT'S
    what eo.router.build_execution_graph_from_hires() must carry
    through into role_names/agent_names unchanged, since a group is only
    meaningful to eo/executor.py's dispatch loop if it survives as an
    actual nested list at the position it belongs, not a flattened one.

    tier is fixed at 3 ("adaptive"): a saved template is, by
    definition, a hires-driven run — the same execution path every
    other adaptive/tier-3 task (including everything Part 1 built)
    already goes through. This is what a caller (eo/loop_v4.py or
    api/task_runner.py, whichever entrypoint checks "did the user pick
    a template?") uses in place of running the Inspector/Panel at all
    for that request.

    approval_roles and no_conversation_context_roles are carried through
    verbatim (not part of the Inspector/Panel classification shape
    itself, but the caller needs them alongside this dict to actually
    wire up execute_graph()'s matching arguments — see this function's
    docstring note under no_conversation_context_roles in
    save_workflow_template())."""
    roles = template["roles"]
    return {
        "tier": 3,
        "path": "adaptive",
        "directed_task_type": None,
        "confidence": 1.0,
        "suggested_agents": _flatten_roles(roles),
        "execution_order": list(roles),
        "domain": template.get("domain_hint"),
        "reasoning": f"started from saved workflow template '{template['name']}'",
        "panel_reviewed": False,
        "approval_roles": list(template.get("approval_roles") or []),
        "no_conversation_context_roles": list(template.get("no_conversation_context_roles") or []),
    }


# ---------------------------------------------------------------------------
# Part 5 §5.2 — Default workflow templates. Unlike eo/registry.py's
# role-prompt store, an empty workflow_templates store is a perfectly
# normal starting state (see _load_templates()'s own docstring) -- this
# exists purely so the one template Part 5's intro explicitly calls out
# ("a 'Plan a new app' workflow template is a natural first thing to save
# here") actually exists at runtime, since question_forcer's approval
# pause (DoD #2) has nothing to attach to without a saved template that
# sets approval_roles.
# ---------------------------------------------------------------------------
DEFAULT_WORKFLOW_TEMPLATES = [
    {
        "name": "Plan a new app",
        "description": (
            "Turns a raw idea, uploaded brief, or pasted brain-dump into "
            "a full PRD, architecture/schema diagrams, an API contract, "
            "a devil's-advocate critique, and a feasibility read -- "
            "pausing once for you to answer question_forcer's clarifying "
            "questions before prd_writer commits to any of them."
        ),
        "roles": STRUCTURE_TEMPLATES["plan"],
        "domain_hint": "plan",
        "approval_roles": ["question_forcer"],
    },
]


def seed_default_workflow_templates() -> None:
    """Idempotent — matches by `name`, safe to call on every app startup
    without creating duplicates. Call this once from wherever the app
    already does startup bootstrapping (the same place, if any, that
    would call eo.registry's role-prompt bootstrap)."""
    existing_names = {t["name"] for t in list_workflow_templates()}
    for tpl in DEFAULT_WORKFLOW_TEMPLATES:
        if tpl["name"] in existing_names:
            continue
        save_workflow_template(
            name=tpl["name"],
            roles=tpl["roles"],
            description=tpl["description"],
            domain_hint=tpl["domain_hint"],
            approval_roles=tpl["approval_roles"],
            created_by="system_seed",
        )