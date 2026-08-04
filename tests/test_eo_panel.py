"""
tests/test_eo_panel.py — Part 11 of the v5 Master Blueprint's testing
plan: "synthesis logic specifically — feed it 3 deliberately disagreeing
draft opinions and assert the max-tier/union/avg-confidence rules hold."

_synthesize() is a pure function (no network) so these run instantly, no
skip conditions needed, unlike test_eo_inspector.py's live-fixture tier.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eo.panel as panel


def _vote(tier, directed=None, confidence=0.8, agents=None, reasoning=""):
    return {
        "tier": tier, "directed_task_type": directed, "confidence": confidence,
        "suggested_agents": agents or [], "reasoning": reasoning,
    }


def test_max_tier_wins_never_under_routes():
    votes = [_vote(0), _vote(1), _vote(3)]
    result = panel._synthesize(votes, votes[0])
    assert result["tier"] == 3


def test_suggested_agents_are_unioned():
    votes = [
        _vote(1, agents=["prompt_writer_lean"]),
        _vote(1, agents=["code_writer_lean"]),
        _vote(1, agents=["prompt_writer_lean", "reviewer_fixer_lean"]),
    ]
    result = panel._synthesize(votes, votes[0])
    assert result["suggested_agents"] == ["code_writer_lean", "prompt_writer_lean", "reviewer_fixer_lean"]


def test_confidence_is_averaged():
    votes = [_vote(1, confidence=0.9), _vote(1, confidence=0.6), _vote(1, confidence=0.3)]
    result = panel._synthesize(votes, votes[0])
    assert abs(result["confidence"] - 0.6) < 1e-6


def test_unanimous_directed_task_type_is_kept():
    votes = [_vote(2, directed="debug"), _vote(2, directed="debug"), _vote(2, directed="debug")]
    result = panel._synthesize(votes, votes[0])
    assert result["directed_task_type"] == "debug"
    assert result["tier"] == 2


def test_null_directed_task_type_is_kept_when_unanimous():
    votes = [_vote(1), _vote(1), _vote(1)]
    result = panel._synthesize(votes, votes[0])
    assert result["directed_task_type"] is None


def test_disagreeing_directed_task_type_bumps_to_tier_3():
    # This is the specific "deliberately disagreeing" case Part 11 asks
    # for: two members say tier 2 but disagree on WHICH directed task,
    # the rule must not guess -- it bumps to tier 3 scoping instead.
    votes = [
        _vote(2, directed="debug", confidence=0.7),
        _vote(2, directed="refactor", confidence=0.7),
        _vote(1, confidence=0.9),
    ]
    result = panel._synthesize(votes, votes[0])
    assert result["directed_task_type"] is None
    assert result["tier"] == 3  # bumped even though max(tiers) alone would be 2


def test_panel_reviewed_flag_is_set():
    votes = [_vote(1), _vote(1), _vote(1)]
    result = panel._synthesize(votes, votes[0])
    assert result["panel_reviewed"] is True


def test_unreachable_member_votes_conservative_not_dropped():
    # A member whose whole chain fails must still count in the union/avg
    # as a conservative (tier 3, confidence 0.0) vote, not be silently
    # excluded -- excluding it would be equivalent to treating "couldn't
    # reach this model" as "this model agrees everything is fine."
    assert panel._UNREACHABLE_VOTE["tier"] == 3
    assert panel._UNREACHABLE_VOTE["confidence"] == 0.0
    votes = [_vote(0, confidence=0.95), dict(panel._UNREACHABLE_VOTE), _vote(0, confidence=0.9)]
    result = panel._synthesize(votes, votes[0])
    assert result["tier"] == 3
    assert result["confidence"] < 0.95


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
