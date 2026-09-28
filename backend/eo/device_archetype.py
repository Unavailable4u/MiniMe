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
import logging
import re

from eo import laya_gate

_logger = logging.getLogger(__name__)

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
# Patch A.2 — Laya fallback for ambiguous cases (was an LLM fallback;
# see resolve_ambiguous_archetype()'s own docstring for the migration)
# ---------------------------------------------------------------------------

_VALID_ENCLOSURE_MODES = {"full", "partial", "none"}
_VALID_MOBILITY_TYPES = {
    "static", "wheeled", "legged", "flying", "handheld", "wearable",
}

# Laya migration audit (2026-09-27): was a single free-text LLM call
# asked to return JSON for two fields; now two `choice` questions in one
# local forward pass, each constrained to exactly the closed set
# classify_archetype() itself already promises (_VALID_ENCLOSURE_MODES /
# _VALID_MOBILITY_TYPES above) -- see eo/laya_gate.py for the shared
# loader. Same field semantics as the old SYSTEM_PROMPT below, just
# phrased as Laya criteria instead of English instructions to an LLM.
# ~1,000 chars per chunk fits Laya's ~300-token state budget (see
# eo/laya_gate.py); 4 chunks covers the first ~4,000 chars of a PRD.
_CHUNK_CHARS = 1000
_MAX_CHUNKS = 4

_ARCHETYPE_QUESTIONS = {
    "enclosure_mode": {
        "type": "choice",
        "instructions": "What structural enclosure does this device need?",
        "criteria": {
            "full": "a sealed housing + lid -- the default for anything "
                    "handheld, wearable, or sitting stationary on a "
                    "surface/wall",
            "partial": "a structural chassis/frame but no full enclosing "
                       "shell (e.g. a wheeled robot base, a drone "
                       "airframe, a legged walking robot)",
            "none": "no shared structural part at all (e.g. a bare "
                    "single-board add-on with no housing of its own)",
        },
    },
    "mobility_type": {
        "type": "choice",
        "instructions": "How (or whether) does the device move or get carried?",
        "criteria": {
            "static": "sits in place; not carried or self-propelled",
            "wheeled": "moves on wheels",
            "legged": "moves on legs, like a walking robot",
            "flying": "flies, like a drone or airframe",
            "handheld": "held in the hand during use",
            "wearable": "worn on the body",
        },
    },
}


def resolve_ambiguous_archetype(prd: dict) -> dict:
    """Laya fallback for a PRD that classify_archetype() (A.1, above)
    couldn't confidently decide -- call this ONLY when that function
    returned `{"status": "ambiguous"}`; a non-ambiguous PRD should never
    reach this function (A.3, NOT this patch, is what wires that call
    order into the pipeline).

    Always returns a definite `{"enclosure_mode": ..., "mobility_type":
    ...}` pair -- never "ambiguous" again and never raises on a Laya
    failure. Same fail-safe posture as before this migration: an
    unavailable classifier or an out-of-vocabulary response falls back
    to the same safe "full"/"static" default classify_archetype() itself
    uses for a no-signal PRD, rather than surfacing a partial/invalid
    archetype to every later phase that trusts this field.

    Laya migration audit (2026-09-27): was an LLM call (own
    FALLBACK_CHAIN, deferred agents.structure_architect._strip_fences +
    json.loads to parse the response) -- now a single local Laya
    predict() over _ARCHETYPE_QUESTIONS (see eo/laya_gate.py). Because
    each question is a `choice` constrained to its own criteria keys,
    the "out-of-vocabulary" branch below is defense in depth rather than
    the routine occurrence a free-text LLM response made it before.
    """
    text = prd.get("text", "") if isinstance(prd, dict) else ""
    if not isinstance(text, str):
        text = ""

    # A PRD can be far longer than Laya's ~300-token state budget (see
    # eo/laya_gate.py), and sending it whole would silently drop
    # everything past the opening paragraph -- but the ambiguity that
    # got us here (e.g. "wheeled chassis" AND "wrist strap") can sit
    # anywhere. So each chunk is asked both questions and the answers
    # are combined by confidence-weighted vote per field.
    enclosure_votes: dict = {}
    mobility_votes: dict = {}
    try:
        for piece in laya_gate.chunk(text, _CHUNK_CHARS, _MAX_CHUNKS):
            result = laya_gate.predict({"prd_text": piece}, _ARCHETYPE_QUESTIONS,
                                        site="device_archetype.resolve_ambiguous_archetype")
            if result is None:
                continue   # this chunk couldn't be scored; others still vote
            for field, votes in (("enclosure_mode", enclosure_votes),
                                  ("mobility_type", mobility_votes)):
                answer = result["answers"][field]
                votes[answer["choice"]] = votes.get(answer["choice"], 0.0) + float(
                    answer.get("confidence", 1.0))
    except Exception as exc:
        _logger.warning(
            "resolve_ambiguous_archetype: Laya call failed (%s: %s), "
            "falling back to full/static default.",
            exc.__class__.__name__, exc,
        )
        return {"enclosure_mode": "full", "mobility_type": "static"}

    if not enclosure_votes or not mobility_votes:
        _logger.warning(
            "resolve_ambiguous_archetype: Laya unavailable, falling "
            "back to full/static default."
        )
        return {"enclosure_mode": "full", "mobility_type": "static"}
    enclosure_mode = max(enclosure_votes, key=enclosure_votes.get)
    mobility_type = max(mobility_votes, key=mobility_votes.get)

    if enclosure_mode in _VALID_ENCLOSURE_MODES and mobility_type in _VALID_MOBILITY_TYPES:
        return {"enclosure_mode": enclosure_mode, "mobility_type": mobility_type}

    # Out-of-vocabulary response -- same safe default as an unparseable
    # one, rather than propagating a value none of A.4/E/F/H's later
    # branch checks would recognize.
    _logger.warning(
        "resolve_ambiguous_archetype: out-of-vocabulary enclosure_mode=%r "
        "mobility_type=%r, falling back to full/static default.",
        enclosure_mode, mobility_type,
    )
    return {"enclosure_mode": "full", "mobility_type": "static"}
