"""
eo/inspector.py — Part 2.1 of the v5 Master Blueprint: the Inspector EO.
Runs on every incoming task. Classifies it into a path + (optionally) a
directed_task_type, without doing any of the actual work itself.
Provider choice (Gemini/Mistral/HF rollout, Patch 6 — §4b/§6 of the
rollout guide, revisiting the original exclusion below):
  - Primary:   Groq, `qwen/qwen3.6-27b`, via EO_INSPECTOR_GROQ_KEY_1 — a key
               from a FRESH, DEDICATED Groq account (different signup
               than production's GROQ_API_KEY). Isolation here is
               account-level, not just key-level: a busy adaptive-path
               cycle hammering the production account's rate limits
               doesn't touch this one at all, which is the actual
               property Part 2.1 wanted from putting the Inspector on
               Gemini in the first place.
  - Fallback 1: same model, EO_INSPECTOR_GROQ_KEY_2 — a second dedicated
               Groq account, only used if KEY_1 is rate-limited. Fine to
               leave unset; generate_text() skips any chain step whose
               key_env isn't set rather than erroring, so this step is a
               harmless no-op until you add a second account.
  - Fallback 2/3: gemini-3.6-flash, via GEMINI_API_KEY_10 / GEMINI_API_KEY_11.
               Originally left out here on purpose ("Gemini is out per
               the user's own substitution") because Part 2.1 wanted
               this role isolated from whatever contention the rest of
               the system's provider pools were under, and at the time
               that meant keeping this chain off every provider already
               shared elsewhere. That reasoning doesn't block Gemini
               specifically — it blocks anything that isn't dedicated to
               this role — and GEMINI_API_KEY_10/_11 are dedicated
               fallback-only accounts, not shared with any other agent's
               chain, so slotting them in here doesn't reintroduce the
               contention the original exclusion was protecting against.
               They only get used once BOTH dedicated Groq accounts are
               already rate-limited, so the primary isolation property
               above is unchanged.
  - Quota-reality fix, §4 (2026-07-30): the former Fallback 4 (GitHub
               Models gpt-4.1-nano, via EO_PANEL_GITHUB_PAT) is removed —
               GitHub Models retired in full today. Groq x2 -> Gemini x2
               is the full chain now; this role keeps that redundancy,
               just without a fifth rung.
Output schema is exactly Part 3's contract, updated per Migration Part 12
§8.2/§8.4 (tier int -> path string):
    {path, directed_task_type, confidence, suggested_agents, reasoning}
This module classifies HONESTLY — it does not know about, and must never
be made to know about, whatever a caller intends to do with "instant"/
"direct" execution not existing yet. Forcing path "adaptive" regardless
of this output is loop_v4.py's job (Stage 4.2 of the roadmap), not this
module's — keeping the Inspector's own output uncorrupted is what makes
it possible to validate classification quality against real traffic
before it affects anything.
"""
import os
import sys
import json
import re
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from relay.emitter import emit_event
from eo.structure import (
    STRUCTURE_TEMPLATES, build_reference_structure_addition, _rough_domain_guess,
)
VALID_DIRECTED_TASK_TYPES = {
    "debug", "review", "add_tests", "refactor",
    "security_scan", "write_docs", "explain_code", None,
}

# Bug fix (2026-08-12): hardware BOM/wiring/enclosure requests were being
# under-routed. A task like "design a battery-powered sensor... give me
# the full parts list, wiring, physical layout, and assembly steps" reads
# to the LLM classifier as a small single-file build (the ESP32 firmware
# part is genuinely simple), so it was scored "direct" (tier 1, high
# confidence) and never escalated to the panel. hardware_speccer -- the
# only role that writes to Blueprint's Parts/Wiring/Mech panels (see
# eo/panel_content.py's own comment on this split) -- is exclusively
# staffed through the tier-3 hires-driven path, so a "direct"
# classification silently drops the entire Blueprint deliverable: the
# firmware code still gets written and shown in chat, but nothing ever
# reaches the Blueprint tab.
#
# Same fix shape as eo/sga.py's _requests_verification(): a fast,
# deterministic keyword pre-check that short-circuits straight to the
# right routing outcome for a request the model structurally cannot
# satisfy at a lower path, rather than trusting a qualitative judgment
# call ("does this sound like a big build?") the LLM classifier keeps
# getting wrong for this specific shape of task. Not exhaustive by
# design -- SYSTEM_PROMPT's "hardware_speccer" callout below is the
# fallback for phrasings this list doesn't catch.
HARDWARE_SPECCER_REQUEST_PATTERNS = [
    r"parts? list",
    r"bill of materials\b|\bBOM\b",
    r"wiring (?:diagram|graph|layout)",
    r"which (?:pin|part)s? connects? to",
    r"physical layout",
    r"enclosure",
    r"assembly (?:steps|instructions|sequence)",
    r"breadboard",
    r"schematic",
]
_HARDWARE_SPECCER_REQUEST_RE = re.compile(
    "|".join(HARDWARE_SPECCER_REQUEST_PATTERNS), re.IGNORECASE
)


def _requests_hardware_speccer(task_text: str) -> bool:
    """True if the task text explicitly asks for a hardware bill of
    materials, wiring, physical layout, or assembly instructions --
    something only hardware_speccer (tier 3 / "adaptive" only) can
    actually produce, regardless of how simple any accompanying
    firmware/software portion of the task reads."""
    return bool(_HARDWARE_SPECCER_REQUEST_RE.search(task_text or ""))


# Bug fix (2026-08-12): same failure shape as the hardware-speccer bug
# above, different domain. Test tab's "Run Simulation" panel
# (frontend/app/components/tabs/TestTab.jsx's SIMULATION_TYPES) dispatches
# plain natural-language task text like "Simulate how real customers
# would react -- both an enthusiastic-but-realistic customer persona and
# a skeptical, hard-to-convince one -- to: <target>." eo/structure.py's
# _rough_domain_guess() already recognizes this shape of text and tags it
# domain "simulate" -- but that tag is only ever shown to the LLM
# classifier as a non-binding bias (see build_reference_structure_addition
# and its own docstring). The classifier's separate "path" judgment keeps
# scoring these tasks "direct" (small single-file build) instead of
# "adaptive", because a single persona-reaction prompt reads, on its own,
# like something buildable in one pass -- the same qualitative-judgment
# blind spot _requests_hardware_speccer() was added to route around for
# hardware requests. A "direct" classification sends the task to the
# tier-1 prompt_writer_lean -> code_writer_lean -> reviewer_fixer_lean
# pipeline, whose only job is producing source code -- so instead of two
# persona reactions in prose, the user gets back a Python function that
# templates fake reactions. Only "adaptive" reaches the panel, which is
# what actually hires STRUCTURE_TEMPLATES["simulate"]'s real persona
# roles (persona_customer, persona_skeptic, etc.) and gets genuine
# LLM-authored prose back.
#
# Fix follows the exact same shape as the hardware short-circuit: reuse
# the already-correct domain guess deterministically, rather than leaving
# the "direct" vs "adaptive" call to the model's judgment for this
# specific shape of task.
def _requests_simulate_domain(task_text: str) -> bool:
    """True if the task text reads as a persona/simulation request --
    i.e. eo/structure.py's own domain guesser would tag it "simulate" --
    which only the panel's "adaptive" path can actually staff with real
    persona roles, regardless of how small a single-persona prompt reads
    in isolation."""
    return _rough_domain_guess(task_text) == "simulate"


# Bug fix (2026-08-16): same failure shape as the hardware-speccer and
# simulate-domain short-circuits above, third instance of it. Plan tab's
# Architecture / Schema / PRD sub-tabs (frontend/app/components/tabs/
# PlanTab.jsx) are driven by free-text task_text typed into the project
# chat, same as the hardware/simulate cases -- there's no forced prompt
# template guaranteeing trigger wording. A request like "generate the
# architecture, PRD, and blueprint" reads to the LLM classifier as small
# in isolation (no multi-file build implied by the wording itself), so it
# keeps getting scored "direct" (tier 1: prompt_writer_lean ->
# code_writer_lean -> reviewer_fixer_lean) instead of "adaptive" -- and a
# "direct" classification can never reach architecture_diagrammer/
# schema_diagrammer/prd_writer, since those (like hardware_speccer) are
# exclusively staffed through the panel's hires pass on the adaptive
# path. The result: the user gets code in chat and nothing lands on the
# Architecture/Schema/PRD panels, same silent-drop shape the 2026-08-12
# fixes above were written to close for their own domains.
#
# Deliberately three independent keyword groups, not one combined
# pattern -- a task can ask for just one of these (e.g. only a schema
# diagram), and forcing in roles the task didn't actually ask for would
# just waste a hire. "blueprint" is treated as its own trigger for
# hardware_speccer specifically (not folded into
# HARDWARE_SPECCER_REQUEST_PATTERNS above) because Blueprint is the name
# of hardware_speccer's own four-panel tab (Parts/Wiring/Mech/
# Instructions, see BLUEPRINT_VIEWS in PlanTab.jsx) -- a bare "generate
# the blueprint" is a hardware_speccer request even though it doesn't
# mention parts/wiring/enclosure by name.
_ARCHITECTURE_REQUEST_RE = re.compile(
    r"\barchitecture\b|system design diagram|"
    r"component diagram|architecture_diagrammer",
    re.IGNORECASE,
)
_SCHEMA_REQUEST_RE = re.compile(
    r"schema diagram|database schema|entity[- ]relationship|\bER diagram\b|"
    r"schema_diagrammer",
    re.IGNORECASE,
)
_PRD_REQUEST_RE = re.compile(
    r"\bPRD\b|product requirements doc|requirements doc|prd_writer",
    re.IGNORECASE,
)
_BLUEPRINT_TAB_RE = re.compile(r"\bblueprint\b", re.IGNORECASE)


def _requests_plan_tab_roles(task_text: str) -> list:
    """Returns the subset of ["architecture_diagrammer", "schema_diagrammer",
    "prd_writer", "hardware_speccer"] that task_text's own wording asks
    for -- [] if none match, in which case classify() falls through to
    the LLM classifier exactly as before this fix. Order in the returned
    list matches a reasonable execution_order (PRD context first, since
    architecture/schema work often wants it, hardware_speccer last since
    it already has its own dedicated MissingDependencyError("prd_writer")
    contract via eo/executor.py's self-heal)."""
    text = task_text or ""
    roles = []
    if _PRD_REQUEST_RE.search(text):
        roles.append("prd_writer")
    if _ARCHITECTURE_REQUEST_RE.search(text):
        roles.append("architecture_diagrammer")
    if _SCHEMA_REQUEST_RE.search(text):
        roles.append("schema_diagrammer")
    if _BLUEPRINT_TAB_RE.search(text):
        roles.append("hardware_speccer")
    return roles


CHAIN = [
    # Quota-reality fix, §3: qwen/qwen3-32b -> qwen/qwen3.6-27b (the old
    # model isn't in Groq's current live free-tier model table, confirmed
    # 2026-07-30 -- see agents/test_writer.py's matching fix for the same
    # swap). This chain runs on EVERY incoming task, so this was the
    # highest-traffic of the 3 call sites using the retired model.
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "EO_INSPECTOR_GROQ_KEY_1"},
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "EO_INSPECTOR_GROQ_KEY_2"},
    # Gemini/Mistral/HF rollout, Patch 6 (§4b/§6): dedicated fallback-only
    # accounts, not shared with any other agent's chain -- see module
    # docstring for why this doesn't reopen the isolation gap the original
    # Gemini exclusion was protecting against.
    {"provider": "gemini", "model": "gemini-3.6-flash", "key_env": "GEMINI_API_KEY_10"},
    {"provider": "gemini", "model": "gemini-3.6-flash", "key_env": "GEMINI_API_KEY_11"},
    # Quota-reality fix, §4 (2026-07-30): GitHub Models retired in full --
    # its last-resort step is removed here, not replaced. This role keeps
    # the Groq x2 -> Gemini x2 redundancy above (still every task, tier 0).
]
SYSTEM_PROMPT = """You are the Inspector for a multi-agent build system. \
You classify one incoming task into a routing path — you do NOT do the \
task yourself.
Classify this task's "path" as exactly one of:
- "instant": trivial — a question, a one-line factual/explanatory answer, \
no code artifact requested.
- "direct": small build — a small, self-contained script or single-file \
program, buildable in one pass, no multi-module architecture implied.
- "fixed": a DIRECTED task against an EXISTING codebase — one specific \
kind of work, not a fresh build. Must set directed_task_type to exactly \
one of: "debug", "review", "add_tests", "refactor", "security_scan", \
"write_docs", "explain_code".
- "adaptive": a full build or ongoing multi-cycle project — "build and \
keep improving X", multi-module scope, or anything implying an app with \
several interacting parts.
Watch specifically for tasks worded to SOUND trivial but that imply \
multi-file/multi-module scope (e.g. "just make me a todo app with users, \
auth, and persistence" sounds casual but is "adaptive", not "instant"/ \
"direct") — this is the case most likely to be under-routed, so when in \
doubt about scope, prefer the higher path and a lower confidence rather \
than guessing low.
Note: "sga" and "cache" are also valid path values elsewhere in this \
system, but they're resolved BEFORE you ever see a task (Part 2/4's \
short-circuit overrides) — you will never need to and must never output \
either of them yourself; your choice is always one of the four above. \
Same for "source" (Data Layer §4a): a task with a file/url attached to \
it never reaches you at all — it's routed straight to Source Manager, \
deterministically, before classification.

For "suggested_agents", describe the KINDS OF EXPERTISE this task needs — \
not just names of agents you've seen before. Use short, general role \
labels (e.g. "implementer", "researcher", "fact_checker", "diagram_designer") \
that describe what the work requires. If a task needs a kind of expertise \
you don't have a standard label for, invent a clear, reusable one — the \
system maintains a growing library of these and will write a proper brief \
for any role it hasn't seen before. Do not limit yourself to roles you've \
used in past examples.

A few specific roles already exist as real, registered modules with \
structured (non-text-only) output. When a task calls for one of these, \
use its EXACT literal name below rather than inventing a synonym for it \
— a synonym (e.g. "hardware_designer", "electronics_engineer") will NOT \
resolve to the real module and the task will silently fall back to \
generic reasoning instead:
- "hardware_speccer": the task involves specifying physical/electronic \
hardware — a bill of materials, part selection, wiring, or a physical \
device's components.
- "architecture_diagrammer": the task calls for a system-level \
architecture diagram (components/services and how they connect).
- "schema_diagrammer": the task calls for a database/entity schema \
diagram.
These are examples of registered roles, not an exhaustive list — for \
anything else, keep inventing clear general labels as instructed above.

You will also be given, below the task, an explanation of three more \
fields to decide: "domain", "execution_order", and "parallel_groups" \
(Migration Part 10; parallel_groups added by the parallel-execution \
rollout). Follow those instructions exactly as given there.

Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly \
this shape:
{
  "path": "fixed",
  "directed_task_type": "refactor",
  "confidence": 0.87,
  "suggested_agents": ["implementer", "verifier"],
  "reasoning": "one short sentence",
  "domain": "coding",
  "execution_order": ["implementer", "verifier"],
  "parallel_groups": []
}
"path" must be exactly one of "instant", "direct", "fixed", "adaptive" — \
never a number, never "sga"/"cache" (see note above). "confidence" must \
be a float 0.0-1.0. "directed_task_type" must be null unless path is \
exactly "fixed", in which case it must be one of the seven strings above \
— never invent a new one. "domain" must be null, or one of the domain \
names given below the task. "execution_order" must be a list containing \
only role names that also appear in "suggested_agents" — never a role \
you didn't already choose. "parallel_groups" must be a list of lists, \
where every role named in any group also appears in "execution_order" — \
leave it as [] (the safe default) whenever you aren't genuinely \
confident two or more roles are independent of each other."""
def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
VALID_PATHS = {"instant", "direct", "fixed", "adaptive"}


def _validate(parsed: dict) -> dict:
    # Migration Part 12 §8.2/§8.4: "tier" (int 0-3) -> "path" (string).
    # Unlike "domain" (light-touch, defaults to None on a bad value),
    # "path" should genuinely never be null, so this stays a hard enum
    # check -- same as the old tier check was.
    path = parsed.get("path")
    if path not in VALID_PATHS:
        raise ValueError(f"Inspector returned invalid path: {path!r}")
    directed = parsed.get("directed_task_type")
    if directed not in VALID_DIRECTED_TASK_TYPES:
        raise ValueError(f"Inspector returned invalid directed_task_type: {directed!r}")
    if path != "fixed" and directed is not None:
        # Same discipline the Panel synthesis rule uses (Part 2.2): don't
        # silently accept an inconsistent combination, and don't guess
        # which field is "right" — surface it.
        raise ValueError(
            f"Inspector set directed_task_type={directed!r} but path={path!r} "
            f"(only valid when path == 'fixed')."
        )
    confidence = parsed.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        raise ValueError(f"Inspector returned invalid confidence: {confidence!r}")
    if not isinstance(parsed.get("suggested_agents"), list):
        raise ValueError("Inspector's suggested_agents must be a list.")

    # Migration Part 10 §3 — two additional fields. Validated loosely
    # and defaulted rather than raised on, since a model omitting these
    # (e.g. an older cached response, or a member that just forgot) is a
    # normal case Part 10 explicitly wants handled by falling through to
    # "the Panel builds an order from scratch," not a hard failure the
    # way a bad tier/confidence is.
    domain = parsed.get("domain")
    if domain is not None and domain not in STRUCTURE_TEMPLATES:
        # Not a recognized domain name -- treat as "none genuinely fits"
        # rather than rejecting the whole classification over it.
        domain = None
    execution_order = parsed.get("execution_order")
    if not isinstance(execution_order, list):
        execution_order = []
    else:
        # Silently drop anything not in suggested_agents -- the prompt
        # asks for this already, but a model can still slip up, and
        # dropping is safer here than raising over an otherwise-good
        # classification.
        execution_order = [r for r in execution_order if r in parsed["suggested_agents"]]

    # Parallel-execution rollout, step 1: "parallel_groups" gets the same
    # loose validate-and-default treatment as domain/execution_order just
    # above -- a malformed or overreaching value is dropped down to a
    # safe empty list rather than failing the whole classification.
    #
    # This function's job stops at "is this shaped like a list of
    # role-name groups drawn from execution_order" -- it does NOT check
    # approval_roles, real hire status, group-size limits, or overlaps
    # between groups. Those need approval_roles and the actual hires
    # list, neither of which exists yet at classification time -- that's
    # step 3's sanitizer, which runs later, right before a group is
    # allowed to become a real concurrent-execution slot. Nothing reads
    # parallel_groups downstream of this module yet; carrying a
    # conservative, well-shaped value forward now is what makes that
    # later step possible without also having to defend against
    # completely malformed input at the same time.
    raw_groups = parsed.get("parallel_groups")
    parallel_groups = []
    if isinstance(raw_groups, list):
        order_set = set(execution_order)
        for group in raw_groups:
            if not isinstance(group, list):
                continue
            # Dedupe while preserving the model's own within-group
            # order; only keep roles that also made it into the final
            # (already-sanitized) execution_order above.
            deduped = []
            for role in group:
                if isinstance(role, str) and role in order_set and role not in deduped:
                    deduped.append(role)
            # A "group" of fewer than 2 roles has nothing to parallelize
            # against -- drop it rather than carry noise forward.
            if len(deduped) >= 2:
                parallel_groups.append(deduped)

    return {
        "path": path,
        "directed_task_type": directed,
        "confidence": float(confidence),
        "suggested_agents": parsed["suggested_agents"],
        "reasoning": parsed.get("reasoning", ""),
        "domain": domain,
        "execution_order": execution_order,
        "parallel_groups": parallel_groups,
    }
def classify(task_text: str, context: str = None, session_id: str = None) -> dict:
    """
    Classifies `task_text`. Returns the Part 3 output schema dict.
    Raises RuntimeError if every step in CHAIN is exhausted (matches
    utils.llm_client.generate_text's existing contract), or ValueError if
    a response came back but failed schema validation (a prompt/parsing
    problem — deliberately NOT retried onto the next provider, per
    llm_client's own reasoning: that would just mask a real bug).

    `context`, if given, is appended as extra information (e.g. from
    eo/routing_memory.py's retrieve_similar_outcomes) — Stage 4.7's
    feedback loop. It is presented to the model as evidence about past
    similar tasks, never as an instruction about what to conclude this
    time, so the Inspector keeps classifying honestly per this module's
    own docstring.

    `session_id`, if given, fires relay events (Part 6.3) so a connected
    frontend can watch this classification happen live — Stage 6, step 1
    of the roadmap ("wire the event-emitting wrapper into one agent
    first ... as a proof of concept"). Omitting session_id (the default)
    makes this call byte-for-byte the same as before Stage 6 existed:
    every event call below becomes a no-op per relay/emitter.py's own
    contract, so existing callers (loop_v4.py without a session, all the
    EO tests) are unaffected.
    """
    emit_event("agent_start", session_id, agent="inspector",
                payload={"label": "Inspector — classifying task"})

    # Deterministic pre-check, tried before any LLM call -- see
    # _requests_hardware_speccer()'s own docstring above for why this
    # can't be left to the model's judgment alone. Forces "adaptive" so
    # loop_v4.py's should_escalate always sends this to the panel
    # (tier >= 2 alone is sufficient, regardless of confidence), and the
    # panel's own hires pass is what actually staffs hardware_speccer --
    # this short-circuit only needs to guarantee escalation happens, not
    # replicate the panel's full reasoning.
    if _requests_hardware_speccer(task_text):
        parsed = {
            "path": "adaptive",
            "directed_task_type": None,
            "confidence": 1.0,
            "suggested_agents": ["hardware_speccer"],
            "reasoning": (
                "Deterministic override: task text asks for a hardware "
                "bill of materials, wiring, physical layout, or assembly "
                "instructions, which only hardware_speccer (tier 3) can "
                "produce -- forced to 'adaptive' regardless of how simple "
                "any accompanying firmware/software portion reads."
            ),
            "domain": None,
            "execution_order": ["hardware_speccer"],
            "parallel_groups": [],
        }
        emit_event("routing_decision", session_id, agent="inspector",
                    path=parsed["path"], payload=parsed)
        emit_event("agent_done", session_id, agent="inspector",
                    path=parsed["path"],
                    payload={"summary": parsed["reasoning"]})
        return parsed

    # Deterministic pre-check, same shape and same reasoning as the
    # hardware_speccer short-circuit just above -- see
    # _requests_simulate_domain()'s own docstring. Forces "adaptive" so
    # loop_v4.py's should_escalate always sends this to the panel
    # (tier >= 2 alone is sufficient, regardless of confidence), and the
    # panel's own hires pass is what actually staffs the real persona
    # roles -- this short-circuit only needs to guarantee escalation
    # happens, not replicate the panel's full reasoning.
    if _requests_simulate_domain(task_text):
        parsed = {
            "path": "adaptive",
            "directed_task_type": None,
            "confidence": 1.0,
            "suggested_agents": list(STRUCTURE_TEMPLATES["simulate"]),
            "reasoning": (
                "Deterministic override: task text reads as a persona/"
                "simulation request (eo/structure.py's own domain guesser "
                "agrees), which only the panel's 'adaptive' path can staff "
                "with real persona roles -- forced to 'adaptive' regardless "
                "of how small a single-persona prompt reads in isolation."
            ),
            "domain": "simulate",
            "execution_order": list(STRUCTURE_TEMPLATES["simulate"]),
            "parallel_groups": [],
        }
        emit_event("routing_decision", session_id, agent="inspector",
                    path=parsed["path"], payload=parsed)
        emit_event("agent_done", session_id, agent="inspector",
                    path=parsed["path"],
                    payload={"summary": parsed["reasoning"]})
        return parsed

    # Deterministic pre-check, same shape and same reasoning as the two
    # short-circuits above -- see _requests_plan_tab_roles()'s own
    # docstring. Forces "adaptive" so loop_v4.py's should_escalate always
    # sends this to the panel; the panel's own hires pass is what
    # actually staffs whichever of architecture_diagrammer/
    # schema_diagrammer/prd_writer/hardware_speccer this task's wording
    # implied -- this short-circuit only needs to guarantee escalation
    # happens, not replicate the panel's full reasoning.
    plan_tab_roles = _requests_plan_tab_roles(task_text)
    if plan_tab_roles:
        parsed = {
            "path": "adaptive",
            "directed_task_type": None,
            "confidence": 1.0,
            "suggested_agents": plan_tab_roles,
            "reasoning": (
                "Deterministic override: task text asks for one or more "
                "of architecture/schema/PRD/blueprint output, which only "
                "the panel's 'adaptive' path can staff with the real "
                "registered modules for those panels -- forced to "
                "'adaptive' regardless of how small the request reads in "
                "isolation."
            ),
            "domain": "plan" if "prd_writer" in plan_tab_roles else None,
            "execution_order": list(plan_tab_roles),
            "parallel_groups": [],
        }
        emit_event("routing_decision", session_id, agent="inspector",
                    path=parsed["path"], payload=parsed)
        emit_event("agent_done", session_id, agent="inspector",
                    path=parsed["path"],
                    payload={"summary": parsed["reasoning"]})
        return parsed

    user_content = f"Task: {task_text}"
    if context:
        user_content += (
            f"\n\nFor reference, here is how some similar past tasks were "
            f"routed and what happened (this is informational only — use "
            f"your own judgment on the current task):\n{context}"
        )
    # Migration Part 10 §3 — same reference-structure text block is used
    # by eo/panel.py's members B and C (via the same helper), so all
    # three panel votes see identical domain/execution_order framing.
    user_content += build_reference_structure_addition(task_text)

    try:
        raw = generate_text(
            system_prompt=SYSTEM_PROMPT,
            user_content=user_content,
            chain=CHAIN,
            agent_name="Inspector",
            # Fix C's continuation handoff is built for prose/code -- a
            # reasoning model truncated mid-<think> just finishes its
            # thought when told to "continue seamlessly" and never emits
            # the JSON this call actually needs, which _strip_fences()
            # then reduces to "" (see utils.llm_client.generate_text's
            # allow_continuation docstring). This is a single-shot JSON
            # classifier, so a truncation should discard the partial text
            # and retry fresh on the next chain step instead.
            allow_continuation=False,
        )
        parsed = _validate(json.loads(_strip_fences(raw)))
    except Exception as exc:
        emit_event("error", session_id, agent="inspector",
                    payload={"message": str(exc), "agent": "inspector"})
        raise

    emit_event("routing_decision", session_id, agent="inspector",
                path=parsed["path"], payload=parsed)
    emit_event("agent_done", session_id, agent="inspector",
                path=parsed["path"],
                payload={"summary": parsed["reasoning"]})
    return parsed