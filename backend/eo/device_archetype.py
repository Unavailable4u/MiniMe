"""
eo/device_archetype.py — Phase A, Patch A.1 of the Mech View standalone
implementation guide: a pure, deterministic, LLM-free classifier that
reads the PRD and decides what *kind* of device this project's mech
pipeline is building, before any BOM part is proposed.

Every later phase in this guide (B's swept-volume modeling, C's mass/
CoG balance check, D's access mechanisms, E's material defaults, F's
thermal/vibration table, H's ergonomic presets) reads its result off
`mech["archetype"]` (wired in by A.3, not this patch) rather than
re-deriving device type itself -- this module is the single place that
decision gets made, so it can't drift out of sync across phases the
way ENCLOSURE_SPEC (eo/enclosure_spec.py) exists to stop numeric drift
between geometry modules.

Two fields, per the guide's own spec:
  - `enclosure_mode`: "full" (sealed housing + lid, today's only actual
    behavior -- agents/hardware_speccer.py's SYSTEM_PROMPT currently
    hard-codes this unconditionally), "partial" (a structural chassis/
    frame with no full shell -- e.g. a wheeled robot base), or "none"
    (no shared structural part at all).
  - `mobility_type`: "static" | "wheeled" | "legged" | "flying" |
    "handheld" | "wearable".

Deliberately keyword/category matching over the PRD text, not an LLM
call -- A.2 (resolve_ambiguous_archetype(), NOT this patch) is where an
LLM gets involved, and only for the cases this module can't confidently
decide, mirroring eo/mech_validator.py's find_unresolved_inferred_pins()
-> one confirmation retry pattern: try the cheap, deterministic path
first, escalate only on genuine ambiguity.

Input shape: `prd: dict`, matching the guide's own function signature.
The only key this module reads is `prd.get("text", "")` -- the same
`{"text": ...}` shape memory/bus.py's read_stage_output_text() already
returns for the approval-edited case (eo/executor.py's own pause/edit
path), so A.3 can wrap whatever plain-string PRD it gets back from that
helper as `{"text": prd_text}` without this module caring which of the
two on-disk shapes the PRD happened to be stored in.

Ambiguous default: when the text has genuinely no strong signal either
way, this returns `{"status": "ambiguous"}` rather than guessing --
per this patch's own "done when" criterion. A.2 is the only caller that
should ever see that shape; A.3 calls A.2 only when it does.

Phase A, Patch A.2 (extends this module, below classify_archetype):
`resolve_ambiguous_archetype()` is the LLM fallback for exactly the
cases A.1's heuristic couldn't confidently decide -- a PRD with no
device-type language at all, or one that signals more than one
mobility group at once. Deliberately reuses eo/dynamic_chain.py's
build_fallback_chain() (the same live, quota-ranked, cooldown-aware,
provider-spread chain agents/hardware_speccer.py's own
run_hardware_speccer() already resolves via a deferred import for the
same circular-import reason documented in that module's own docstring)
rather than a hand-rolled single-key call -- this module has no reason
to reintroduce the exact single-point-of-failure that dynamic_chain.py
exists to close.
"""
import json
import re


# ---------------------------------------------------------------------------
# Keyword tables
# ---------------------------------------------------------------------------
# Each table maps a compiled word-boundary regex to the (enclosure_mode,
# mobility_type) pair it signals. Checked as whole-word matches (not
# substring) so e.g. "wheelbarrow" doesn't false-positive on "wheel", and
# "handheld" doesn't false-positive on "hand". Order within a mobility
# group doesn't matter -- any single hit within a group is enough to
# count as a signal for that group; a PRD signaling more than one group
# is treated as ambiguous rather than picked arbitrarily (see
# classify_archetype's tie-break note below).

_WHEELED_WORDS = (
    "wheel", "wheels", "wheeled", "chassis", "rover", "differential drive",
    "caster", "axle",
)
_LEGGED_WORDS = (
    "leg", "legs", "legged", "quadruped", "biped", "hexapod", "walking gait",
)
_FLYING_WORDS = (
    "drone", "quadcopter", "propeller", "rotor", "flight controller",
    "esc", "airframe",
)
_HANDHELD_WORDS = (
    "handheld", "remote", "controller", "grip", "trigger", "held in hand",
    "pocket-sized", "point and shoot",
)
_WEARABLE_WORDS = (
    "wrist", "strap", "wearable", "band", "worn on", "clip-on", "lanyard",
)

# Each group carries its own (enclosure_mode, mobility_type) result --
# wheeled/legged/flying devices are a structural chassis, not a sealed
# shell (Part 1, gap #1: "no branch for a device that should be an open
# frame... or need no shared structural part at all"), so those three
# resolve to "partial" rather than the "full" that's today's only
# behavior; handheld/wearable are still a sealed enclosure, just a
# different mobility_type than the static default.
_GROUPS = {
    "wheeled": (_WHEELED_WORDS, "partial", "wheeled"),
    "legged": (_LEGGED_WORDS, "partial", "legged"),
    "flying": (_FLYING_WORDS, "partial", "flying"),
    "handheld": (_HANDHELD_WORDS, "full", "handheld"),
    "wearable": (_WEARABLE_WORDS, "full", "wearable"),
}

_WORD_RE_CACHE = {}


def _compile_word_re(word: str) -> re.Pattern:
    """Word-boundary regex for a (possibly multi-word) phrase, cached so
    repeated classify_archetype() calls in a batch/regression-test run
    don't re-compile the same handful of patterns every time."""
    cached = _WORD_RE_CACHE.get(word)
    if cached is None:
        cached = re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE)
        _WORD_RE_CACHE[word] = cached
    return cached


def _matched_groups(text: str) -> list:
    """Returns the list of group names (keys of _GROUPS) that have at
    least one whole-word hit in `text`, in _GROUPS iteration order."""
    hits = []
    for group_name, (words, _mode, _mobility) in _GROUPS.items():
        for word in words:
            if _compile_word_re(word).search(text):
                hits.append(group_name)
                break
    return hits


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def classify_archetype(prd: dict) -> dict:
    """Classifies a device's archetype from its PRD text.

    Returns `{"enclosure_mode": ..., "mobility_type": ...}` when the
    text carries a clear, single-group signal, or the safe "full"/
    "static" default (matching today's only actual pipeline behavior --
    Part 1, gap #1) when there's no device-type language at all.

    Returns `{"status": "ambiguous"}` -- and nothing else -- when the
    text signals more than one mobility group at once (e.g. a PRD that
    mentions both "wheeled chassis" and "wrist strap" in passing): that
    is a genuine conflict this heuristic has no principled way to
    resolve, not a case to guess on, so it's left for A.2's LLM fallback
    (resolve_ambiguous_archetype(), NOT this patch) rather than silently
    picking whichever group happened to match first.

    Pure function: no I/O, no LLM call, no randomness -- same input
    always produces the same output, so callers (A.3's pipeline wiring,
    NOT this patch; this module's own future test suite) can rely on it
    being safe to call repeatedly / in a regression check.
    """
    text = prd.get("text", "") if isinstance(prd, dict) else ""
    if not isinstance(text, str):
        text = ""

    matched = _matched_groups(text)

    if len(matched) > 1:
        return {"status": "ambiguous"}

    if len(matched) == 1:
        _words, enclosure_mode, mobility_type = _GROUPS[matched[0]]
        return {"enclosure_mode": enclosure_mode, "mobility_type": mobility_type}

    # No strong signal of any kind -- the safe default, matching today's
    # only behavior (Part 1, gap #1's "unconditionally instructs the
    # model to always produce a sealed housing + lid pair").
    return {"enclosure_mode": "full", "mobility_type": "static"}


# ---------------------------------------------------------------------------
# Patch A.2 — LLM fallback for ambiguous cases
# ---------------------------------------------------------------------------

_VALID_ENCLOSURE_MODES = {"full", "partial", "none"}
_VALID_MOBILITY_TYPES = {
    "static", "wheeled", "legged", "flying", "handheld", "wearable",
}

# FALLBACK_CHAIN: last-resort static chain, used ONLY if
# eo/dynamic_chain.py's build_fallback_chain() comes back empty (every
# registered account excluded/cooling down at once -- should be very
# rare). Same belt-and-suspenders shape and same first model pin as
# agents/hardware_speccer.py's own FALLBACK_CHAIN -- see that module's
# docstring for the full reasoning; not copied wholesale (this call is
# a single short classification, not a multi-thousand-token spec
# generation, so one entry is enough here).
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY", "timeout": 30},
]

# Deliberately terse and JSON-only, same "respond only valid JSON, no
# preamble" convention agents/hardware_speccer.py's own SYSTEM_PROMPT_*
# constants use for every LLM call this codebase parses programmatically.
_ARCHETYPE_SYSTEM_PROMPT = """You are a hardware device classifier. \
You read a hardware PRD/feasibility note and decide two things about \
the physical device it describes:

1. "enclosure_mode": "full" if the device needs a sealed housing + lid \
(the default for anything handheld, wearable, or sitting stationary on \
a surface/wall), "partial" if it needs a structural chassis/frame but \
no full enclosing shell (e.g. a wheeled robot base, a drone airframe, \
a legged walking robot), or "none" if it needs no shared structural \
part at all (e.g. a bare single-board add-on with no housing of its \
own).

2. "mobility_type": exactly one of "static", "wheeled", "legged", \
"flying", "handheld", "wearable" -- whichever best describes how (or \
whether) the device moves or is carried.

If the PRD text gives you no real signal for either field, default to \
"full" and "static" -- do not guess a more exotic answer than the text \
actually supports.

Respond with ONLY a valid JSON object, no markdown fences, no \
preamble, no explanation: {"enclosure_mode": "...", "mobility_type": "..."}"""


def resolve_ambiguous_archetype(prd: dict) -> dict:
    """LLM fallback for a PRD that classify_archetype() (A.1, above)
    couldn't confidently decide -- call this ONLY when that function
    returned `{"status": "ambiguous"}`; a non-ambiguous PRD should never
    reach this function (A.3, NOT this patch, is what wires that call
    order into the pipeline).

    Always returns a definite `{"enclosure_mode": ..., "mobility_type":
    ...}` pair -- never "ambiguous" again and never raises on a
    malformed/unparseable model response. Same fail-safe posture as
    agents/hardware_speccer.py's own parts-generation call: an
    unparseable or out-of-vocabulary response falls back to the same
    safe "full"/"static" default classify_archetype() itself uses for
    a no-signal PRD, rather than surfacing a partial/invalid archetype
    to every later phase that trusts this field.
    """
    text = prd.get("text", "") if isinstance(prd, dict) else ""
    if not isinstance(text, str):
        text = ""

    # Deferred import -- see this module's own docstring (Patch A.2
    # section) for why: eo/dynamic_chain.py imports eo.registry at ITS
    # own module level, so a module-level import here would risk the
    # same circular-import shape eo/dynamic_chain.py's docstring already
    # flags for any eagerly-imported agents/*.py caller. device_archetype
    # isn't imported by eo.registry today, but keeping this deferred
    # costs nothing and keeps this module safe to import from anywhere,
    # including a future agents/*.py module, without relitigating this.
    from eo.dynamic_chain import build_fallback_chain
    from utils.llm_client import generate_text
    from agents.structure_architect import _strip_fences  # reuse, don't reimplement

    chain = build_fallback_chain("device_archetype") or FALLBACK_CHAIN
    raw = generate_text(
        _ARCHETYPE_SYSTEM_PROMPT, f"PRD:\n{text}", chain,
        agent_name="Device Archetype Resolver",
        allow_continuation=False,  # same Root Cause B reasoning as
        # hardware_speccer.py's own parts call: this prompt demands
        # ONLY valid JSON, so a "length" truncation splicing a
        # continuation from a possibly-different provider onto a
        # half-finished object would corrupt it in a way json.loads()
        # below can't recover from -- a fresh retry on the next chain
        # step is safer than a splice here.
    )

    cleaned = _strip_fences(raw)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"enclosure_mode": "full", "mobility_type": "static"}

    if not isinstance(result, dict):
        return {"enclosure_mode": "full", "mobility_type": "static"}

    enclosure_mode = result.get("enclosure_mode")
    mobility_type = result.get("mobility_type")
    if enclosure_mode in _VALID_ENCLOSURE_MODES and mobility_type in _VALID_MOBILITY_TYPES:
        return {"enclosure_mode": enclosure_mode, "mobility_type": mobility_type}

    # Out-of-vocabulary response (model ignored the prompt's own
    # constraints) -- same safe default as an unparseable one, rather
    # than propagating a value none of A.4/E/F/H's later branch checks
    # would recognize.
    return {"enclosure_mode": "full", "mobility_type": "static"}
