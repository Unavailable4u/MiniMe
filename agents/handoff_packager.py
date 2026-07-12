"""
agents/handoff_packager.py — Part 5 §5.6. The centerpiece handoff from
the "plan" domain into the existing "coding" domain: reads prd_writer's
finished PRD (plus whatever architecture/schema/API-contract/critique/
feasibility output exists alongside it) and pre-fills idea_planner.py's
own KEYS["current_plan"] shape directly, so a coding-domain cycle 1
starts from a real, PRD-derived plan instead of idea_planner.py
inferring one from a bare idea string.

Makes ZERO LLM calls (Part 5 §5.8) — everything here is deterministic
markdown parsing of prd_writer's already-generated PRD text plus plain
memory-bus reads/writes, same cost category as Part 0's exporters.

Three things this does, matching §5.6 exactly:
  1. Parses prd_writer's PRD text for a real feature list, a priority
     order, a first-cycle target feature, and a cycle goal — all via
     plain regex/markdown-structure parsing, never an LLM guess.
  2. Calls memory.bus.set_app_slug() with a NEW slug for this project
     (same scoping mechanism api/task_runner.py's tier-3 adaptive path
     already calls at the top of every coding run — without it this
     handoff would silently collide with whatever app_slug another
     concurrent session has active), then writes KEYS["original_idea"],
     the pre-filled KEYS["current_plan"], and an initial
     KEYS["feature_status"] (every feature "missing").
  3. Bundles the full PRD + diagrams + API contract into Part 0 §0.5's
     shared {title, sections} export shape, for the human-readable side
     of the handoff — separate from the machine-readable memory-bus
     writes in step 2.

Raises MissingDependencyError("prd_writer") if prd_writer hasn't
produced anything yet in this session — same self-heal contract
architecture_diagrammer.py/schema_diagrammer.py already use, letting
eo/executor.py's adaptive-path self-heal branch splice prd_writer in
first and retry.

Place this file at: agents/handoff_packager.py
"""

import os
import re
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, set_app_slug, slugify
from relay.emitter import emit_event
from eo.errors import MissingDependencyError
# Reuse, don't reimplement — same bus keys architecture_diagrammer.py/
# schema_diagrammer.py already write their rendered Mermaid to.
from agents.architecture_diagrammer import ARCHITECTURE_DIAGRAM_KEY
from agents.schema_diagrammer import SCHEMA_DIAGRAM_KEY

PLAN_HANDOFF_PACKAGE_KEY = "plan_handoff_package"

# --- Deterministic PRD parsing -----------------------------------------
# All of these look for a markdown heading (any level, #-###) whose text
# matches a small set of expected headings prd_writer's brief should use,
# then grab everything up to the next heading of any level as that
# section's body. Kept intentionally forgiving (several heading phrasings
# accepted) since a generic_worker role's exact wording can drift brief
# edit to brief edit — see eo/registry.py's "unreviewed — auto-generated"
# flag in RoleLibraryTab for why that drift is expected, not a bug.

HEADING_RE = re.compile(r'^#{1,6}\s+.+$', re.MULTILINE)
TITLE_RE = re.compile(r'^#\s+(.+)$', re.MULTILINE)
FEATURE_SECTION_RE = re.compile(
    r'^#{1,3}\s*(?:features?|feature list|key features|core features)\b.*$',
    re.IGNORECASE | re.MULTILINE,
)
PRIORITY_SECTION_RE = re.compile(
    r'^#{1,3}\s*(?:priorit(?:y|ies)|feature priorit(?:y|ies))\b.*$',
    re.IGNORECASE | re.MULTILINE,
)
FIRST_CYCLE_SECTION_RE = re.compile(
    r'^#{1,3}\s*(?:first[\s_-]?cycle|mvp scope|cycle\s*1\s*scope|initial scope)\b.*$',
    re.IGNORECASE | re.MULTILINE,
)
CYCLE_GOAL_SECTION_RE = re.compile(
    r'^#{1,3}\s*(?:cycle goal|first cycle goal)\b.*$',
    re.IGNORECASE | re.MULTILINE,
)
BULLET_RE = re.compile(r'^\s*[-*]\s+(.+)$', re.MULTILINE)


def _section_body(text: str, heading_match) -> str:
    """Text from just after a matched heading line up to (not including)
    the next heading of any level, or end of string."""
    start = heading_match.end()
    next_heading = HEADING_RE.search(text, start)
    end = next_heading.start() if next_heading else len(text)
    return text[start:end].strip()


def _first_match_section(text: str, pattern) -> str | None:
    m = pattern.search(text)
    return _section_body(text, m) if m else None


def _extract_bullet_names(section_text: str) -> list:
    """Pulls a clean feature/priority NAME off each bullet line. PRDs
    commonly write a bullet as "**Feature Name** -- description" or
    "Feature Name: description" — keep only the name part so downstream
    consumers (idea_planner.py's exact-string feature matching,
    feature_status dict keys) get a short, stable string rather than a
    full sentence."""
    names = []
    for raw in BULLET_RE.findall(section_text):
        line = raw.strip()
        line = re.sub(r'^\*\*(.+?)\*\*', r'\1', line)  # strip bold markers
        head = re.split(r'\s+[-\u2013\u2014]\s+|:\s+', line, maxsplit=1)[0]
        name = head.strip().strip('*').strip()
        if name:
            names.append(name)
    return names


def _extract_features(prd_text: str) -> tuple:
    """Returns (features, priorities). Both read from the PRD's own
    "Features" section (bullet order = priority order, top to bottom) —
    unless the PRD ALSO has a distinct "Priorities" section, in which
    case that ordering wins for `priorities` specifically (a PRD can
    legitimately group features one way and rank them separately).

    Falls back to a single synthesized "MVP" feature if the PRD has no
    parseable Features section at all, so KEYS["current_plan"] never
    ends up with an empty features list (which would leave
    target_feature unresolvable) — same fail-safe-fallback spirit as
    structure_architect.py's own JSON-parse fallback."""
    features_section = _first_match_section(prd_text, FEATURE_SECTION_RE)
    features = _extract_bullet_names(features_section) if features_section else []

    priorities_section = _first_match_section(prd_text, PRIORITY_SECTION_RE)
    priorities = _extract_bullet_names(priorities_section) if priorities_section else []

    if not features:
        return ["MVP"], ["MVP"]
    if not priorities:
        priorities = list(features)
    return features, priorities


def _extract_target_feature(prd_text: str, features: list) -> str:
    """Looks for an explicit first-cycle marker naming one of `features`;
    falls back to features[0] (the highest-priority feature per
    _extract_features' ordering) — matching idea_planner.py's own stated
    preference for an unstarted feature."""
    section = _first_match_section(prd_text, FIRST_CYCLE_SECTION_RE)
    if section:
        for name in features:
            if re.search(re.escape(name), section, re.IGNORECASE):
                return name
    return features[0]


def _extract_cycle_goal(prd_text: str, target_feature: str) -> str:
    section = _first_match_section(prd_text, CYCLE_GOAL_SECTION_RE)
    if section:
        first_line = next((ln.strip() for ln in section.splitlines() if ln.strip()), "")
        if first_line:
            return first_line
    return f"Implement {target_feature} as scoped in the PRD's first cycle."


def _extract_title(prd_text: str, fallback: str) -> str:
    m = TITLE_RE.search(prd_text)
    return m.group(1).strip() if m else fallback


def _read_stage_text(session_id: str, role: str):
    """Same stage_output:{session_id}:{role} convention every
    generic_worker role's output lives at (agents/generic_worker.py) —
    NOT a KEYS[...] entry, since prd_writer/api_contract_writer/
    devils_advocate/feasibility_estimator are all plain generic_worker
    roles with no dedicated bus key of their own."""
    body = read(f"stage_output:{session_id}:{role}", default=None)
    return body if isinstance(body, str) and body.strip() else None


def run_handoff_packager(session_id: str = None, tier: int = None,
                          task_text: str = None, domain: str = None) -> dict:
    """
    Entry point, dispatched by eo/executor.py alongside
    architecture_diagrammer.py/schema_diagrammer.py — same signature,
    same dispatch branch (see eo/executor.py patch). Raises
    MissingDependencyError("prd_writer") if prd_writer hasn't run yet.
    """
    prd_text = _read_stage_text(session_id, "prd_writer")
    if not prd_text:
        raise MissingDependencyError("prd_writer")

    features, priorities = _extract_features(prd_text)
    target_feature = _extract_target_feature(prd_text, features)
    cycle_goal = _extract_cycle_goal(prd_text, target_feature)
    title = _extract_title(prd_text, fallback=(task_text or "Untitled Plan")[:80])

    # --- §5.6 step 2: scope a fresh app_slug, then pre-fill
    # idea_planner.py's exact KEYS["current_plan"] shape directly —
    # cycle 1 of the coding domain reads this plan as-is, skipping
    # idea_planner's own (less-informed) inference step entirely.
    app_slug = f"{slugify(title)}_{(session_id or 'plan')[:8]}"
    set_app_slug(app_slug)

    current_plan = {
        "features": features,
        "priorities": priorities,
        "target_feature": target_feature,
        "cycle_goal": cycle_goal,
    }
    write(KEYS["original_idea"], f"{title}\n\n{prd_text[:800]}")
    write(KEYS["current_plan"], current_plan)
    write(KEYS["feature_status"], {f: "missing" for f in features})

    # --- §5.6 step 3: bundle the human-readable handoff package in
    # Part 0 §0.5's shared {title, sections} export shape, separate from
    # the machine-readable memory-bus writes above.
    sections = [{"heading": "PRD", "content": prd_text}]

    architecture = read(ARCHITECTURE_DIAGRAM_KEY, default=None)
    if isinstance(architecture, dict) and architecture.get("mermaid"):
        sections.append({
            "heading": "Architecture Diagram",
            "content": f"```mermaid\n{architecture['mermaid']}\n```",
        })

    schema = read(SCHEMA_DIAGRAM_KEY, default=None)
    if isinstance(schema, dict) and schema.get("mermaid"):
        sections.append({
            "heading": "Schema Diagram",
            "content": f"```mermaid\n{schema['mermaid']}\n```",
        })

    api_contract = _read_stage_text(session_id, "api_contract_writer")
    if api_contract:
        sections.append({"heading": "API Contract", "content": api_contract})

    critique = _read_stage_text(session_id, "devils_advocate")
    if critique:
        sections.append({"heading": "Devil's Advocate Critique", "content": critique})

    feasibility = _read_stage_text(session_id, "feasibility_estimator")
    if feasibility:
        sections.append({
            # Part 3 §3.8 labeling discipline, per §5.4's own instruction
            # that this must never read as a real time/cost estimate.
            "heading": "Feasibility (rough complexity signal, not a time/cost estimate)",
            "content": feasibility,
        })

    package = {"title": title, "sections": sections}
    write(PLAN_HANDOFF_PACKAGE_KEY, package)

    result = {
        "app_slug": app_slug,
        "current_plan": current_plan,
        "package": package,
        "summary": (
            f'Handoff ready for "{title}" — {len(features)} feature(s), '
            f'first cycle target: "{target_feature}". Scoped to app_slug '
            f'"{app_slug}", every feature marked missing. Click '
            f'"Start building this" to kick off cycle 1.'
        ),
    }
    emit_event("plan_handoff", session_id, agent="handoff_packager",
               payload={"app_slug": app_slug, "target_feature": target_feature,
                        "feature_count": len(features)})
    return result


if __name__ == "__main__":
    import json
    print(json.dumps(run_handoff_packager(session_id="local-test"), indent=2))