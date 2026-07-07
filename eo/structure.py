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
        "idea_planner", "prompt_writer", "implementer", "test_writer",
        "sandbox_tester", "verifier", "fixer", "security_reviewer",
        "file_manager", "documentation_writer", "changelog_writer",
        "final_qa", "report_writer", "gatekeeper",
    ],
    "creative_writing": [
        "brainstormer", "outliner", "writer", "fact_checker", "editor",
    ],
    "research": [
        "researcher", "fact_checker", "analyst", "writer", "editor",
    ],
    "data_analysis": [
        "analyst", "formatter", "writer", "editor",
    ],
}


def _rough_domain_guess(task_text: str) -> str | None:
    """Simple keyword heuristic, non-binding — just picks a candidate
    reference structure to show the model as a bias. The model's actual
    "domain" output in its JSON response is free to disagree with this
    guess entirely; this is only used to decide which (if any) reference
    structure gets shown alongside the prompt."""
    text = task_text.lower()
    if any(kw in text for kw in (
        "code", "bug", "function", "script", "app", "refactor", "api",
        "test", "debug", "repo", "codebase",
    )):
        return "coding"
    if any(kw in text for kw in (
        "story", "poem", "song", "lyrics", "novel", "creative", "brainstorm",
    )):
        return "creative_writing"
    if any(kw in text for kw in (
        "research", "investigate", "sources", "citations", "survey",
    )):
        return "research"
    if any(kw in text for kw in (
        "data", "csv", "spreadsheet", "dataset", "analysis", "chart",
    )):
        return "data_analysis"
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