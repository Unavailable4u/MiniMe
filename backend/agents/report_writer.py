import os
import sys
import json
from dotenv import load_dotenv
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, read_many, KEYS
from utils.llm_client import generate_text
from eo.errors import MissingDependencyError   # NEW — bug fix
load_dotenv()

# FALLBACK_CHAIN: last-resort static chain per Part 4, agent #17 of the
# v5 blueprint, used ONLY if eo/dynamic_chain.py's build_fallback_chain()
# below comes back empty (every registered account excluded/cooling down
# at once -- should be very rare). This used to be the ONLY chain this
# module ever tried -- its hardcoded CEREBRAS_API_KEY_9 third step was
# one of six agents in Patch 8.1's audit quietly sharing a single
# unmonitored, un-fallback-able account (idea_planner.py,
# deploy_config_writer.py, dataset_analyst.py, responder.py,
# prompt_writer_lean.py, reviewer_fixer_lean.py shared CEREBRAS_API_KEY_1
# the same way). Same fix as agents/hardware_speccer.py /
# agents/architecture_diagrammer.py: see run_report_writer() below, which
# now builds a live, quota-ranked, multi-provider chain instead.
# (Groq's llama-3.3-70b-versatile was decommissioned; migrated to the two
# models above. Cerebras's llama-3.3-70b was separately deprecated Feb
# 2026 and now 404s -- unrelated model, unrelated provider, noted here
# only because it's easy to conflate the two.)
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"},
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "GROQ_API_KEY"},
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "CEREBRAS_API_KEY_9"},
]

SYSTEM_PROMPT = """You are a report writer for an autonomous coding pipeline.
Summarize this cycle in under 200 words for the next planner. Cover: what got
built, what's still broken, what should be prioritized next cycle. Be honest
about failures. Write in plain language, not JSON -- this is read by a human
and by the next planning agent as plain text.
"""


def run_report_writer(session_id: str = None, domain: str = None):
    # Batched into a single MGET instead of 5 sequential round trips --
    # these five keys are unrelated and none is used until after all of
    # them are read anyway.
    _vals = read_many(
        [KEYS["fixed_code"], KEYS["submitted_code"], KEYS["test_results"],
         KEYS["review_notes"], KEYS["current_plan"]],
        default=None,
    )
    fixed_code = _vals[KEYS["fixed_code"]]
    submitted_code = _vals[KEYS["submitted_code"]]
    # Bug fix: fall back to submitted_code, same reasoning as
    # sandbox_tester.py's own fallback -- report_writer can still write a
    # meaningful cycle summary from the Code Writers' raw output even if
    # the Fixer Pool never ran (e.g. review found nothing to fix).
    code_source = fixed_code or submitted_code
    test_results = _vals[KEYS["test_results"]]
    review_notes = _vals[KEYS["review_notes"]]
    current_plan = _vals[KEYS["current_plan"]] or {}
    if not code_source:
        # Bug fix: was `raise ValueError(...)`. "implementer" specifically
        # (not "fixer") -- if code_source is empty, code_writers.py never
        # ran at all, so that's the actual missing step; fixer_pool.py has
        # its own fallback (see agents/sandbox_tester.py) for "ran but
        # nothing needed fixing."
        raise MissingDependencyError(
            "implementer", "Missing fixed_code/submitted_code in memory. Run the Code Writers first."
        )
    if not test_results:
        # NOT converted to MissingDependencyError: the sandbox-testing step
        # isn't a role the Panel can hire on its own (it isn't in
        # eo/registry.py's REAL_ACTION_ROLES -- it's wired into the fixed
        # tier-1/tier-2 pipelines directly), so there's no role name to
        # meaningfully hand executor.py here. Still write a best-effort
        # report rather than hard-failing the whole task over a summary
        # step -- untested code is worth reporting on too.
        test_results = {}
        print("  [Report Writer] no test_results in memory — writing the report without them.")

    user_prompt = (
        "Review notes from this cycle:\n" + json.dumps(review_notes, indent=2)
        + "\n\nFixed code modules (names only, not full code, to keep this short):\n"
        + json.dumps(list(code_source.keys()))
        + "\n\nSandbox test results:\n" + json.dumps(test_results, indent=2)
    )

    # perf audit §4.4 / priority #7: was double-wrapped in call_with_retry
    # on top of generate_text()'s own chain-walk fallback — a real
    # multi-provider outage retried the whole CHAIN up to 4 times with
    # real sleeps (1/2/4/8s) in between, on top of generate_text() already
    # having walked every step in CHAIN once per attempt. generate_text()
    # is the single source of retry/fallback behavior now.
    #
    # Patch 8.6: deferred import -- see eo/dynamic_chain.py's module
    # docstring for why this can't be a module-level import (eo.registry
    # imports this module at load time; eo.dynamic_chain imports
    # eo.registry at ITS module level). Quota-ranked, cooldown-aware,
    # spread across providers -- replaces FALLBACK_CHAIN's single shared
    # Cerebras key with no fallback.
    from eo.dynamic_chain import build_fallback_chain
    chain = build_fallback_chain("report_writer") or FALLBACK_CHAIN

    report_text = generate_text(SYSTEM_PROMPT, user_prompt, chain, agent_name="Report Writer",
                                 session_id=session_id, domain=domain)

    failed_modules = [
        name for name, result in test_results.items()
        if not result.get("passed", False)
    ]
    all_passed = len(failed_modules) == 0

    target_feature = current_plan.get("target_feature")
    if target_feature:
        feature_status = read(KEYS["feature_status"], default={})
        feature_status[target_feature] = "done" if all_passed else "in_progress"
        write(KEYS["feature_status"], feature_status)

    report_record = {
        "text": report_text,
        # Migration Part 26 fix (§2): documentation_agent.py and
        # memory_search.py both read report.get("summary", "") -- there
        # was never a "summary" key, only "text", so every generated
        # README's "recent changes" section and every cross-cycle memory
        # embedding silently got an empty string instead of the actual
        # cycle summary. Adding "summary" as an alias here (rather than
        # renaming "text" outright) fixes both readers immediately
        # without risking any other consumer that might already depend
        # on the "text" key.
        "summary": report_text,
        "all_tests_passed": all_passed,
        "failed_modules": failed_modules,
        "target_feature": target_feature,
    }
    write(KEYS["latest_report"], report_record)
    return report_record


if __name__ == "__main__":
    report = run_report_writer()
    print(json.dumps(report, indent=2))