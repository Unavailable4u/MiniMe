"""
eo/laya_routing.py — Tier 3 of the Laya migration (2026-09-28): a Laya
pre-gate for the two routing decisions that currently cost extra LLM
calls -- the 3-model panel (eo/panel.py, via eo/loop_v4.py's
_get_decision()) and the macro-loop gatekeeper (eo/loop_controller.py's
_run_gatekeeper()). Builds on eo/laya_gate.py (Tier 1) and follows the
same fail-open rule: any problem -> no opinion -> the existing LLM path
runs exactly as before.

WHY THESE TWO ARE DIFFERENT FROM TIER 2 (eo/sga.py's pre-gate)
Tier 2's error is one-sided-safe: a wrong "skip SGA" only loses a fast
shortcut, never quality. These two are NOT:
  - The panel is the safety net for the Inspector's known failure mode --
    a task worded to SOUND trivial that implies multi-module scope (see
    the Inspector's own SYSTEM_PROMPT: "the case most likely to be
    under-routed"). Skipping it wrongly means under-routing.
  - A wrong gatekeeper STOP ends a build one improvement pass early.
So both default to "observe": Laya's verdict is computed and logged, the
existing LLM path still decides, and the verdict is scored against what
the LLM path then actually decided (get_panel_gate_stats()/
get_gatekeeper_gate_stats()). Only after those numbers look good should
you flip a gate to "on". An untuned Laya is a weak judge of this kind of
question (it inverted a similar one in Tier 1 testing), so treat "on" as
something to earn with data, not a default.

MODES (per gate, env var; unknown value -> "off" with a warning):
  "observe" (default) -- compute + log + score; never changes routing
  "on"                -- may skip the LLM/panel call (thresholds below)
  "off"               -- no Laya call at all

INPUT BUDGET: the English checkpoint reads only ~300 tokens of state and
~190 of question text (see eo/laya_gate.py), so questions here are kept
short and the task text is clipped. The gatekeeper gate deliberately does
NOT see the role outputs (they are far too long): it sees the task, the
role names and structural counts (failed roles, critical issues), so it
is a coarse judge -- one more reason it defaults to observe.
"""
import logging
import os

from eo import laya_gate
from memory.bus import incr, read

logger = logging.getLogger(__name__)


def _parse_mode(raw, default: str) -> str:
    mode = (raw if raw not in (None, "") else default).strip().lower()
    if mode in ("on", "observe", "off"):
        return mode
    logger.warning("[laya_routing] unknown gate mode %r -- treating as 'off'", raw)
    return "off"


_STATS_TTL_SECONDS = 60 * 60 * 24 * 30   # 30 days, same as eo/sga.py's counters
_TASK_CHARS = 1000

# Path names in tier order (index == tier). Kept local so this module has
# no dependency on eo/structure.py; a test pins it against PATH_TO_TIER.
_PATH_ORDER = ("instant", "direct", "fixed", "adaptive")


def _bump(key: str) -> None:
    """Best-effort counter increment; never raises (same posture as
    eo/sga.py's _note_resolved())."""
    try:
        incr(key, ex=_STATS_TTL_SECONDS)
    except Exception as exc:
        logger.warning("[laya_routing] counter %s failed (non-fatal): %s", key, exc)


def _count(key: str) -> int:
    try:
        return int(read(key, default=0) or 0)
    except Exception:
        return 0


# =====================================================================
# Gate 1 -- panel escalation (eo/loop_v4.py _get_decision())
# =====================================================================
#
# The panel takes the MAX tier over three votes, so it can only raise a
# draft's tier (or drop directed_task_type / union in more agents); it can
# never lower it. That makes the question Laya must answer one-directional:
# "is this task MORE complex than the Inspector said?" -- if Laya is
# confident it is not, the panel has nothing to add on the tier axis.
#
# Scope is deliberately narrow: only drafts at tier 0/1 (instant/direct)
# are eligible. Tier 2 stays on the panel because the panel also validates
# directed_task_type, and tier 3 because it staffs the roles (union of the
# members' suggested_agents) -- Laya can judge neither. Drafts that failed
# classification are tier 3, so they are never eligible.

PANEL_GATE_MODE = _parse_mode(os.environ.get("LAYA_PANEL_PREGATE"), "observe")
PANEL_GATE_THRESHOLD = float(os.environ.get("LAYA_PANEL_PREGATE_THRESHOLD", "0.90"))
PANEL_SKIP_MAX_TIER = 1

_PATH_QUESTION = {
    "path": {
        "type": "choice",
        "instructions": "How much build scope does this task imply?",
        "criteria": {
            "instant": "trivial question or one-line answer, no code",
            "direct": "small single-file script, one pass, no multi-module design",
            "fixed": "one specific job on an existing codebase: debug, review, "
                     "tests, refactor, security scan, docs, explain",
            "adaptive": "full build or multi-cycle project: several modules or "
                        "interacting parts, even if worded casually",
        },
    },
}

_PANEL_COUNTER_PREFIX = "laya_panel_gate:"
_PANEL_COUNTERS = ("would_skip", "would_skip_panel_raised_tier", "kept",
                   "kept_panel_raised_tier", "skipped")


def panel_verdict(task_text: str, draft: dict) -> dict:
    """Asks Laya whether the panel can be skipped for this draft. Never
    raises. Returns {"mode", "eligible", "p_higher", "would_skip",
    "skip"}: `would_skip` is what Laya recommends; `skip` is whether the
    caller should actually skip the panel (only true in mode "on"). On any
    problem the safe answer is returned: skip False, panel runs as before.
    """
    verdict = {"mode": PANEL_GATE_MODE, "eligible": False, "p_higher": None,
               "would_skip": False, "skip": False}
    try:
        if PANEL_GATE_MODE == "off":
            return verdict
        tier = (draft or {}).get("tier")
        if not isinstance(tier, int) or not 0 <= tier <= PANEL_SKIP_MAX_TIER:
            return verdict
        verdict["eligible"] = True
        text = laya_gate.clip(task_text or "", _TASK_CHARS, "both")
        if not text.strip():
            return verdict
        result = laya_gate.predict({"task": text}, _PATH_QUESTION,
                                    site="loop_v4.panel_pregate")
        probs = result["answers"]["path"]["probabilities"]
        weights = [max(0.0, float(probs.get(p, 0.0))) for p in _PATH_ORDER]
        total = sum(weights)
        if total <= 0:
            return verdict
        p_higher = sum(weights[tier + 1:]) / total
        verdict["p_higher"] = p_higher
        verdict["would_skip"] = (1.0 - p_higher) >= PANEL_GATE_THRESHOLD
        verdict["skip"] = verdict["would_skip"] and PANEL_GATE_MODE == "on"
    except Exception:
        verdict["p_higher"] = None
        verdict["would_skip"] = False
        verdict["skip"] = False
    return verdict


def note_panel_skipped(verdict: dict) -> None:
    """Call when the panel was actually skipped on Laya's say-so."""
    _bump(_PANEL_COUNTER_PREFIX + "skipped")


def record_panel_outcome(verdict: dict, draft: dict, decision: dict) -> None:
    """Scores Laya's verdict against what the panel then decided. Only
    meaningful when the panel actually ran (i.e. not after a skip).
    `raised` = the panel pushed the tier above the Inspector's draft --
    the exact outcome a wrong skip would have hidden."""
    try:
        if not verdict.get("eligible") or verdict.get("p_higher") is None:
            return
        raised = int((decision or {}).get("tier", draft["tier"])) > int(draft["tier"])
        base = "would_skip" if verdict.get("would_skip") else "kept"
        _bump(_PANEL_COUNTER_PREFIX + base)
        if raised:
            _bump(_PANEL_COUNTER_PREFIX + base + "_panel_raised_tier")
        logger.info("[laya_routing] panel gate: p_higher=%.3f laya_says=%s panel_raised_tier=%s",
                    verdict["p_higher"], "skip" if verdict.get("would_skip") else "keep", raised)
    except Exception as exc:
        logger.warning("[laya_routing] record_panel_outcome failed (non-fatal): %s", exc)


def get_panel_gate_stats() -> dict:
    """Counters plus `skip_precision`: of the cases Laya said to skip, the
    fraction where the panel did NOT raise the tier (so skipping would have
    been harmless on the tier axis). None with no data yet. This is the
    number to check before flipping LAYA_PANEL_PREGATE to "on" -- it should
    be very close to 1.0. It says nothing about staffing or
    directed_task_type, which the gate does not touch (see scope above)."""
    counts = {name: _count(_PANEL_COUNTER_PREFIX + name) for name in _PANEL_COUNTERS}
    would = counts["would_skip"]
    counts["skip_precision"] = (
        1.0 - counts["would_skip_panel_raised_tier"] / would if would else None)
    return counts


# =====================================================================
# Gate 2 -- macro-loop gatekeeper (eo/loop_controller.py _run_gatekeeper())
# =====================================================================
#
# The LLM gatekeeper only runs in expert/beast mode, after a full pass,
# and its own parsing already fails toward STOP (anything that is not an
# explicit CONTINUE stops). So the only skip Laya is allowed is STOP: a
# confident "genuinely finished" saves the LLM call. A Laya "CONTINUE"
# (or no opinion) never causes a redo by itself -- the LLM still decides.

GATEKEEPER_GATE_MODE = _parse_mode(os.environ.get("LAYA_GATEKEEPER_PREGATE"), "observe")
GATEKEEPER_GATE_THRESHOLD = float(os.environ.get("LAYA_GATEKEEPER_PREGATE_THRESHOLD", "0.90"))

_STOP_QUESTION = {
    "finished": {
        "type": "noul",
        "instructions": "Is this multi-pass build genuinely finished, or would "
                        "another pass meaningfully improve it?",
        "criteria": {
            "false": "another pass would help: failed roles, unresolved "
                     "critical issues, or clearly incomplete work",
            "true": "genuinely finished: nothing important left to redo",
        },
        "labels": {"false": "CONTINUE", "true": "STOP"},
    },
}

_GK_COUNTER_PREFIX = "laya_gatekeeper_gate:"
_GK_COUNTERS = ("would_stop", "would_stop_llm_continued", "kept",
                "kept_llm_stopped", "skipped")


def gatekeeper_verdict(results: dict, task_text: str, loop_num: int,
                       failed_roles: list, critical_issue_count: int) -> dict:
    """Asks Laya whether this pass looks finished. Never raises. Returns
    {"mode", "p_stop", "would_stop", "skip"} -- `skip` (mode "on" only)
    means the caller may answer STOP without the LLM gatekeeper call."""
    verdict = {"mode": GATEKEEPER_GATE_MODE, "p_stop": None,
               "would_stop": False, "skip": False}
    try:
        if GATEKEEPER_GATE_MODE == "off":
            return verdict
        state = {
            "original_task": laya_gate.clip(task_text or "", 350, "head"),
            "pass_number": loop_num,
            "roles_run": laya_gate.clip(", ".join(str(k) for k in (results or {})), 250, "head"),
            "roles_failed": len(failed_roles or []),
            "critical_issues_flagged": int(critical_issue_count or 0),
        }
        result = laya_gate.predict(state, _STOP_QUESTION, site="loop_controller.gatekeeper_pregate")
        p_stop = float(result["answers"]["finished"]["noul"])
        verdict["p_stop"] = p_stop
        verdict["would_stop"] = p_stop >= GATEKEEPER_GATE_THRESHOLD
        verdict["skip"] = verdict["would_stop"] and GATEKEEPER_GATE_MODE == "on"
    except Exception:
        verdict["p_stop"] = None
        verdict["would_stop"] = False
        verdict["skip"] = False
    return verdict


def note_gatekeeper_skipped(verdict: dict) -> None:
    _bump(_GK_COUNTER_PREFIX + "skipped")


def record_gatekeeper_outcome(verdict: dict, llm_action: str) -> None:
    """Scores Laya's verdict against the LLM gatekeeper's actual action
    ("STOP"/"CONTINUE"/...). Only meaningful when the LLM actually ran."""
    try:
        if verdict.get("p_stop") is None:
            return
        llm_stopped = llm_action == "STOP"
        if verdict.get("would_stop"):
            _bump(_GK_COUNTER_PREFIX + "would_stop")
            if not llm_stopped:
                _bump(_GK_COUNTER_PREFIX + "would_stop_llm_continued")
        else:
            _bump(_GK_COUNTER_PREFIX + "kept")
            if llm_stopped:
                _bump(_GK_COUNTER_PREFIX + "kept_llm_stopped")
        logger.info("[laya_routing] gatekeeper gate: p_stop=%.3f laya_says=%s llm_action=%s",
                    verdict["p_stop"], "stop" if verdict.get("would_stop") else "keep", llm_action)
    except Exception as exc:
        logger.warning("[laya_routing] record_gatekeeper_outcome failed (non-fatal): %s", exc)


def get_gatekeeper_gate_stats() -> dict:
    """Counters plus `stop_precision`: of the cases Laya said STOP, the
    fraction where the LLM gatekeeper agreed (also STOP). None with no
    data yet. Should be very close to 1.0 before flipping
    LAYA_GATEKEEPER_PREGATE to "on"; it also shows how often the LLM stops
    anyway (`kept_llm_stopped`), i.e. how much a conservative Laya leaves
    on the table."""
    counts = {name: _count(_GK_COUNTER_PREFIX + name) for name in _GK_COUNTERS}
    would = counts["would_stop"]
    counts["stop_precision"] = (
        1.0 - counts["would_stop_llm_continued"] / would if would else None)
    return counts
