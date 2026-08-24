"""
eo/mech_mass.py — Phase C, Patches C.1/C.2 of the Mech View standalone
implementation guide: the mass field + curated lookup (C.1) and its LLM
estimation fallback (C.2), feeding Phase C's own center-of-gravity/
balance work (Patch C.3's future compute_cog(), not this patch) — Part
1, gap #3: "No mass or center-of-gravity modeling. Parts have no
weight attribute; nothing checks whether a mobile device (wheeled/
legged) would actually balance."

Same "curated dict, keyed on the part's own canonical generic_name,
normalized-string lookup, a table miss returns None rather than
guessing" pattern eo/mech_motion.py's own lookup_motion() (Patch B.1)
already establishes for Phase B, and the same "cheap deterministic
path first, escalate to an LLM only on a genuine miss" pattern that
module's own estimate_motion() (Patch B.2) already establishes for its
own fallback — this module is Phase C's direct counterpart to that
pair, just for `mass_g` instead of motion parameters.

Confidence-field convention: MASS_TABLE entries return
`"confidence": "high"`, estimate_mass()'s LLM fallback returns
`"confidence": "estimated"` — the SAME two-value convention
agents/component_dimension_table.py's own curated rows already use via
their own `dimension_confidence` field, so a caller (Patch C.3's
future compute_cog(), any future report surface) can already tell a
curated, trustworthy mass from an LLM guess without this module
inventing a new vocabulary for the same distinction.
"""
import json

# Keyed on a normalized, lowercased, whitespace-collapsed form of the
# part's own canonical `generic_name` -- see _normalize() below. Same
# "small and hand-curated, not exhaustive" posture eo/mech_motion.py's
# own MOTION_TABLE already documents for itself: every entry here is a
# common hobby-electronics/hobby-robotics part type whose typical mass
# is well-known and doesn't vary wildly between vendors of the same
# part type. A real project using an unusually heavy/light variant of
# one of these types is exactly the kind of case Patch C.2's LLM
# fallback below exists for, not something this curated table tries to
# special-case.
MASS_TABLE = {
    # Common single-board microcontrollers.
    "esp32 dev board": 9.0,
    "arduino uno": 25.0,
    "raspberry pi": 46.0,

    # Common batteries -- literal Phase C worked example ("a mobile
    # device... would actually balance" -- battery placement is almost
    # always the dominant mass on a small robot).
    "9v battery": 45.0,
    "18650 li-ion cell": 47.0,
    "lipo battery": 60.0,

    # Common hobby-robotics actuators/motion parts -- same part
    # vocabulary eo/mech_motion.py's own MOTION_TABLE already curates,
    # since a moving part's own mass matters just as much to Phase C's
    # balance check as its motion envelope matters to Phase B's
    # collision check.
    "hobby servo": 9.0,
    "micro servo": 9.0,
    "continuous rotation servo": 15.0,
    "dc gear motor": 20.0,
    "stepper motor": 45.0,
    "wheel": 8.0,
    "caster wheel": 5.0,

    # Common sensors/displays -- lightweight, but not negligible on a
    # small enough chassis.
    "0.96in oled display": 5.0,
    "ultrasonic distance sensor": 9.0,
    "imu": 2.0,

    # Common structural/passive parts.
    "usb-c power connector": 2.0,
    "tactile push button": 1.0,
}


def _normalize(text: str) -> str:
    """Case/whitespace-insensitive matching key -- same normalization
    eo/mech_motion.py's own _normalize() already uses for the identical
    reason: the vocabulary on both sides (this table's own keys, a
    part's canonical generic_name) is already meant to be a short,
    canonical, human-written name, not free text, so exact normalized
    matching is the right amount of matching, not too little.
    """
    return " ".join((text or "").strip().lower().split())


# Built once at import time from MASS_TABLE's own literal keys above --
# same "no external file, no lazy-loading reason to defer this" posture
# eo/mech_motion.py's own _NORMALIZED_MOTION_TABLE already takes.
_NORMALIZED_MASS_TABLE = {_normalize(k): v for k, v in MASS_TABLE.items()}


def lookup_mass(generic_name: str) -> dict | None:
    """Patch C.1's entry point. Looks up `generic_name` (normalized)
    against MASS_TABLE's own vocabulary above, returning
    `{"mass_g": float, "confidence": "high"}` on a match -- a fresh
    dict every call, never a live reference into shared table state,
    same "never hand a caller a live reference into shared curated
    data" caution eo/mech_motion.py's own lookup_motion() already takes
    for the identical reason.

    Returns `None` -- never raises -- both when `generic_name` matches
    nothing in the table and when it isn't a usable string at all
    (missing/None/empty/whitespace-only), so a caller (Patch C.2's
    estimate_mass() below, NOT this patch) can treat every "no curated
    entry" case identically: a clean `None`, not an exception to catch.
    """
    if not isinstance(generic_name, str):
        return None

    key = _normalize(generic_name)
    if not key:
        return None

    mass_g = _NORMALIZED_MASS_TABLE.get(key)
    if mass_g is None:
        return None

    return {"mass_g": mass_g, "confidence": "high"}


# ---------------------------------------------------------------------------
# Patch C.2 — LLM mass estimation fallback for C.1's table misses.
# ---------------------------------------------------------------------------
#
# Same "cheap deterministic path first, escalate only on a genuine
# miss" pattern eo/mech_motion.py's own lookup_motion() ->
# estimate_motion() pair already establishes for Phase B: lookup_mass()
# above is the cheap path; estimate_mass() below is ONLY ever called by
# a future caller (Patch C.3's future compute_cog() wiring, not this
# patch) after that lookup has already missed, never unconditionally
# alongside it.

# Last-resort static chain, used ONLY if eo/dynamic_chain.py's
# build_fallback_chain() comes back empty (every registered account
# excluded/cooling down at once -- should be very rare). Same
# belt-and-suspenders shape and same first-model pin as
# eo/mech_motion.py's own FALLBACK_CHAIN -- one entry is enough here
# for the same reason it's enough there: this is a single short
# per-part estimate, not a multi-thousand-token spec generation.
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY", "timeout": 30},
]

# Deliberately terse and JSON-only, same "respond only valid JSON, no
# preamble" convention every other programmatically-parsed LLM call in
# this codebase already uses (eo/mech_motion.py's own
# _MOTION_SYSTEM_PROMPT, eo/device_archetype.py's own
# _ARCHETYPE_SYSTEM_PROMPT).
_MASS_SYSTEM_PROMPT = """You are a hardware-component mass estimator \
for a BOM part used in a small 3D-printed hobby-electronics/hobby- \
robotics enclosure. Given a part's name/description, give your best \
real-world estimate of that part's own mass in grams, for the \
STANDARD/most common variant of that part type.

Respond with ONLY a valid JSON object, no markdown fences, no \
preamble, no explanation, in exactly this shape:

{"mass_g": <number>}

The value must be a positive number in grams. When genuinely unsure \
of the exact mass, give your best real-world estimate for that part \
type rather than a placeholder -- every part has SOME mass, so this \
is never a "the part has no mass" case the way motion classification \
can legitimately answer "none"."""


def _validate_mass_shape(result: dict) -> float | None:
    """Checks a parsed LLM response against this module's own single
    valid shape (`{"mass_g": <positive number>}`) -- a missing key, a
    non-numeric value, or a non-positive value is treated exactly like
    a parse failure below: `None`, not a partially-trusted guess
    propagated downstream. Same "out-of-vocabulary shape is a parse
    failure, not a partial guess" posture
    eo/mech_motion.py's own _validate_motion_shape() already takes for
    its own three shapes.
    """
    if not isinstance(result, dict):
        return None

    mass_g = result.get("mass_g")
    if not isinstance(mass_g, (int, float)) or isinstance(mass_g, bool) or mass_g <= 0:
        return None

    return float(mass_g)


def estimate_mass(part: dict) -> dict:
    """Patch C.2's entry point -- the LLM fallback for a part
    lookup_mass() (C.1, above) already returned `None` for. Callers
    (Patch C.3's future compute_cog() wiring, not this patch) should
    only ever reach this AFTER a table miss, same "cheap path first"
    call order eo/mech_motion.py's own estimate_motion() already
    documents for the identical reason: this is a real, billable LLM
    call, and most BOM parts already have a curated entry or a very
    close analog, so it should never run unconditionally alongside the
    free table lookup.

    Unlike eo/mech_motion.py's own estimate_motion() (which legitimately
    returns `None` for a part with no motion at all), EVERY physical
    part has SOME mass -- there is no analogous "confidently zero"
    answer here. So on a falsy/unusable `part`, a failed/unparseable
    LLM response, or an out-of-shape response, this function falls back
    to a small, clearly-labeled placeholder mass
    (`{"mass_g": 5.0, "confidence": "estimated"}`) rather than `None`,
    so a caller (Patch C.3's future compute_cog(), which mass-weights
    EVERY placed part) never has to special-case a missing mass value
    for one part in an otherwise-complete BOM -- same "resolves to a
    definite value, never `None`, for a case that should always have
    an answer" reasoning eo/device_archetype.py's own
    resolve_ambiguous_archetype() already documents for itself (an
    ambiguous PRD always resolves to a definite archetype pair, never
    stays ambiguous).

    Only ever returns `"confidence": "high"` if a caller passed one in
    on `part` itself (not expected -- this function's own job is
    estimation, not re-validating an already-curated value) — in every
    real path through this function the returned confidence is
    `"estimated"`, distinguishable downstream from C.1's curated
    `"high"` value.
    """
    if not isinstance(part, dict):
        return {"mass_g": 5.0, "confidence": "estimated"}

    part_name = part.get("generic_name") or part.get("name")
    if not part_name:
        return {"mass_g": 5.0, "confidence": "estimated"}

    description = part.get("description") or ""
    user_prompt = f"Part: {part_name}\nDescription: {description}".strip()

    # Deferred import -- same circular-import reason
    # eo/mech_motion.py's own estimate_motion() already documents for
    # the identical deferred import: eo/dynamic_chain.py imports
    # eo.registry at ITS own module level, so a module-level import
    # here would risk the same shape that module's own docstring flags
    # for any eagerly-imported agents/*.py caller. eo.mech_mass isn't
    # imported by eo.registry today, but keeping this deferred costs
    # nothing and keeps this module safe to import from anywhere
    # without relitigating this later.
    from agents.structure_architect import _strip_fences  # reuse, don't reimplement
    from eo.dynamic_chain import build_fallback_chain
    from utils.llm_client import generate_text

    chain = build_fallback_chain("mech_mass") or FALLBACK_CHAIN

    try:
        raw = generate_text(
            _MASS_SYSTEM_PROMPT, user_prompt, chain,
            agent_name="Mech Mass Estimator",
            allow_continuation=False,  # same Root Cause B reasoning as
            # every other "ONLY valid JSON" call in this codebase: this
            # prompt demands a single complete JSON object, so a
            # "length" truncation splicing a continuation from a
            # possibly-different provider onto a half-finished object
            # would corrupt it in a way this function's own parsing
            # below can't recover from -- a fresh retry on the next
            # chain step is safer than a splice here.
        )
    except Exception:
        return {"mass_g": 5.0, "confidence": "estimated"}

    cleaned = (raw or "").strip()
    if not cleaned:
        return {"mass_g": 5.0, "confidence": "estimated"}

    # Same fence-stripping helper every other JSON-only LLM call in
    # this codebase already reuses rather than reimplementing (see
    # eo/mech_motion.py's own estimate_motion(), which imports this
    # exact function for the exact same reason) -- a model occasionally
    # wraps its answer in ```json fences despite being told not to.
    try:
        result = json.loads(_strip_fences(cleaned))
    except json.JSONDecodeError:
        return {"mass_g": 5.0, "confidence": "estimated"}

    mass_g = _validate_mass_shape(result)
    if mass_g is None:
        return {"mass_g": 5.0, "confidence": "estimated"}

    return {"mass_g": mass_g, "confidence": "estimated"}
