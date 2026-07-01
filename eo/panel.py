"""
eo/panel.py — EO Panel, Part 2.2 of the v5 Master Blueprint. Escalation
only: called by eo/loop_v4.py when the Inspector's own classification is
below the confidence threshold or already tier >= 2 (Part 3's decision
flow).

Three members:
  A — the Inspector's own draft classification, already computed. No
      extra call (Part 2.2: "carried over").
  B — a second model lineage, genuinely different from the Inspector's.
      Provider substitution note: the blueprint specifies OpenRouter free
      tier here, but per utils/llm_client.py's own docstring, OpenRouter
      (like Gemini) isn't used anywhere in this codebase. Substituting
      Cerebras instead keeps the actual property Part 2.2 cares about --
      "deliberately different model lineages ... a panel of three calls
      to variants of the same base model isn't a panel" -- since the
      Inspector runs on Groq and member C (below) runs on GitHub Models,
      Cerebras is the one remaining distinct lineage/account.
  C — GitHub Models gpt-4.1-mini, fallback gpt-4.1-nano, via
      EO_PANEL_GITHUB_PAT — exactly as specified.

Synthesis rule (Part 2.2, restated exactly):
  - tier: the HIGHEST tier across all three opinions. Never under-route
    on disagreement.
  - suggested_agents: the UNION of all three.
  - confidence: the AVERAGE of all three.
  - directed_task_type: kept only if unanimous or null across all three;
    genuine disagreement bumps to tier 3 scoping (this module forces tier
    to 3 in that specific case, per the blueprint's own wording, even if
    the max-tier rule above would have landed lower).
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.inspector import SYSTEM_PROMPT, _strip_fences, _validate, VALID_DIRECTED_TASK_TYPES
from utils.llm_client import generate_text

MEMBER_B_CHAIN = [
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "EO_PANEL_CEREBRAS_KEY"},
]
MEMBER_C_CHAIN = [
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "EO_PANEL_GITHUB_PAT"},
    {"provider": "github", "model": "openai/gpt-4.1-nano", "key_env": "EO_PANEL_GITHUB_PAT"},
]

# A conservative stand-in vote used when a panel member's own chain is
# fully exhausted. Per the "never under-route on disagreement" rule, a
# member that couldn't be reached votes tier 3 with low confidence rather
# than being silently dropped from the union/average — a missing vote is
# not the same thing as an "everything's fine" vote.
_UNREACHABLE_VOTE = {
    "tier": 3,
    "directed_task_type": None,
    "confidence": 0.0,
    "suggested_agents": [],
    "reasoning": "member unreachable — all providers in its chain failed",
}


def _get_member_vote(label: str, task_text: str, chain: list) -> dict:
    try:
        raw = generate_text(
            system_prompt=SYSTEM_PROMPT,
            user_content=f"Task: {task_text}",
            chain=chain,
            agent_name=f"EO Panel ({label})",
        )
        parsed = json.loads(_strip_fences(raw))
        return _validate(parsed)
    except Exception as exc:
        print(f"  [EO Panel] member {label} unreachable ({exc.__class__.__name__}: {exc}), "
              f"voting conservative (tier 3, confidence 0.0).")
        return dict(_UNREACHABLE_VOTE)


def _synthesize(votes: list, draft: dict) -> dict:
    tiers = [v["tier"] for v in votes]
    max_tier = max(tiers)
    all_agents = set()
    for v in votes:
        all_agents.update(v.get("suggested_agents", []))
    avg_confidence = sum(v["confidence"] for v in votes) / len(votes)
    directed_types = {v.get("directed_task_type") for v in votes}
    if len(directed_types) == 1:
        directed_task_type = directed_types.pop()
    else:
        # Genuine disagreement on task type — bump to tier 3 scoping
        # rather than guessing which member was right (Part 2.2).
        directed_task_type = None
        max_tier = max(max_tier, 3)
    reasoning = " | ".join(
        f"member {label}: {v.get('reasoning', '')}"
        for label, v in zip("ABC", votes)
    )
    # Raw per-member votes, kept alongside the flattened `reasoning`
    # string (unchanged, nothing downstream that reads `reasoning`
    # breaks). This is what lets a frontend trace card show "all 3 panel
    # opinions" (Part 6.6) as structured data instead of re-parsing the
    # joined string.
    panel_votes = [
        {"member": label, **v}
        for label, v in zip("ABC", votes)
    ]
    return {
        "tier": max_tier,
        "directed_task_type": directed_task_type,
        "confidence": round(avg_confidence, 4),
        "suggested_agents": sorted(all_agents),
        "reasoning": reasoning,
        "panel_reviewed": True,
        "panel_votes": panel_votes,
    }


def run_panel(task_text: str, draft: dict) -> dict:
    """
    `draft` is the Inspector's own already-computed classification dict
    (member A, per Part 2.2 — no extra call). Runs members B and C, then
    synthesizes all three per the rule above.
    """
    member_b = _get_member_vote("B", task_text, MEMBER_B_CHAIN)
    member_c = _get_member_vote("C", task_text, MEMBER_C_CHAIN)
    result = _synthesize([draft, member_b, member_c], draft)
    return result


if __name__ == "__main__":
    fake_draft = {
        "tier": 2, "directed_task_type": "debug", "confidence": 0.6,
        "suggested_agents": ["reviewer", "fixer_pool"], "reasoning": "looked like a bug report",
    }
    print(json.dumps(run_panel("something ambiguous", fake_draft), indent=2))