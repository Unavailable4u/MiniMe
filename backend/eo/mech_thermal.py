"""
eo/mech_thermal.py — Phase F (independent of Phase A), Patches F.1/F.2
of the Mech View standalone implementation guide: curated thermal +
vibration property tables (F.1) and their shared LLM estimation
fallback (F.2) — Part 1's own gap #4 ("No thermal or vibration
awareness. No part carries a heat or vibration property; nothing
generates automatic ventilation or keeps vibration-sensitive parts at
a safe distance from motors").

Per Patch F.1's own wording, "this is genuinely new ground -- no
physical-properties table of this kind exists anywhere in the
codebase yet": unlike eo/mech_motion.py (Phase B) and eo/mech_mass.py
(Phase C), there is no upstream "physical properties" concept this
module extends -- it's the first of its kind. Even so, it follows the
SAME "curated dict, keyed on the part's own canonical generic_name,
normalized-string lookup, a table miss returns None rather than
guessing" pattern those two modules already establish, and the same
"cheap deterministic path first, escalate to an LLM only on a genuine
miss" pattern their own lookup_motion()->estimate_motion() and
lookup_mass()->estimate_mass() pairs already establish — Phase F's own
direct counterpart to both, just for `thermal_class`/`vibration_source`
instead of motion parameters or mass.

Two SEPARATE curated tables (not one combined table), because a part
can independently be hot-but-not-vibrating (a linear voltage
regulator) or vibrating-but-not-hot (a wheel) or both (a stepper motor
under load) or neither (a passive sensor) — collapsing them into one
table would force every entry to specify a value for a property that
may not apply to it at all.

  - THERMAL_TABLE / lookup_thermal(): `"none"|"warm"|"hot"` per part —
    literal Part 1 wording ("no part carries a heat... property").
  - VIBRATION_TABLE / lookup_vibration(): a plain boolean per part —
    literal Part 1 wording ("nothing... keeps vibration-sensitive
    parts at a safe distance from motors") and literal Patch F.4's own
    future wording ("any part flagged `vibration_source: True`").

Patch F.2 (extends this module, below both lookup functions):
estimate_thermal_and_vibration() is the LLM fallback, via
eo/dynamic_chain.py's build_fallback_chain() — see that function's own
docstring for why it's ONE combined call covering both properties
rather than two separate calls (deviation from the guide's own literal
"estimation functions" plural wording, explained there).
"""
import json

# ---------------------------------------------------------------------------
# Patch F.1 — curated thermal/vibration property tables.
# ---------------------------------------------------------------------------
#
# Keyed on a normalized, lowercased, whitespace-collapsed form of the
# part's own canonical `generic_name` — see _normalize() below. Same
# "small and hand-curated, not exhaustive" posture eo/mech_motion.py's
# own MOTION_TABLE and eo/mech_mass.py's own MASS_TABLE already
# document for themselves: every entry here is a common hobby-
# electronics/hobby-robotics part type whose typical thermal/vibration
# behavior under normal operation is well-known, not something that
# varies wildly between vendors of the same part type. A real project
# using an unusual/high-power variant of one of these types (or a part
# type not covered here at all) is exactly the kind of case Patch
# F.2's LLM fallback below exists for, not something either curated
# table tries to special-case.
#
# A part with NO entry in a given table is NOT the same as a part
# curated here as its table's own "cold"/"quiet" value — see each
# lookup function's own docstring below on why a table miss returns
# `None` (unknown, ask the LLM) rather than defaulting silently to
# "none"/`False` (a considered, curated "this part genuinely doesn't
# do this" answer). Most parts in a typical BOM (resistors, buttons,
# most sensors, connectors, structural/3D-printed parts) simply have
# no entry in either table below — see F.2's own fallback default for
# why THAT'S where "none"/`False` actually gets decided.
THERMAL_TABLE = {
    # Compute — a real, actively-cooled-or-not SoC under sustained
    # load is the single most common genuinely-warm part on a small
    # hobby build; a bare microcontroller (ESP32/Arduino) idles far
    # cooler and isn't curated "warm" here (an unusually compute-heavy
    # project using one is exactly Patch F.2's own long-tail case).
    "raspberry pi": "warm",

    # Motion/actuation — a motor under sustained mechanical load
    # dissipates real heat through its own windings; a bare hobby
    # servo (light, intermittent duty cycle) is deliberately NOT
    # curated here for the same idle-vs-sustained-load reason as the
    # Raspberry Pi above.
    "dc gear motor": "warm",
    "stepper motor": "warm",
    "linear actuator": "warm",

    # Power-regulation/driver ICs — these dissipate power as heat by
    # design (a linear regulator drops excess voltage as heat; an
    # H-bridge/stepper driver IC dissipates switching + resistive
    # losses under load), the textbook "needs ventilation" case Part
    # 1's own gap #4 wording is about.
    "linear voltage regulator": "hot",
    "h-bridge motor driver": "hot",
    "stepper driver ic": "hot",
    "lipo charger ic": "hot",
}

VIBRATION_TABLE = {
    # Rotating/reciprocating actuators — literal Patch F.4 worked
    # example vocabulary ("any part flagged vibration_source: True").
    "dc gear motor": True,
    "stepper motor": True,
    "hobby servo": True,
    "micro servo": True,
    "continuous rotation servo": True,
    "vibration motor": True,
    "cooling fan": True,

    # A wheel is DRIVEN by a motor rather than generating its own
    # vibration, but real-world unbalanced/off-axis wheel rotation is
    # itself a common, well-known small-robot vibration source in its
    # own right — curated `True` here rather than left for Patch F.2
    # to guess on every wheeled project.
    "wheel": True,

    # A caster wheel is passive (free-rolling, not driven) — curated
    # `False` explicitly (not just left out of the table) so a caster
    # wheel never gets treated as an unknown-and-therefore-estimated
    # part the way a genuinely uncurated part type would be.
    "caster wheel": False,
}


def _normalize(text: str) -> str:
    """Case/whitespace-insensitive matching key — same normalization
    eo/mech_motion.py's own _normalize() (and eo/mech_mass.py's own,
    after it) already use for the identical reason: the vocabulary on
    both sides (either table's own keys, a part's canonical
    generic_name) is already meant to be a short, canonical,
    human-written name, not free text, so exact normalized matching is
    the right amount of matching, not too little.
    """
    return " ".join((text or "").strip().lower().split())


# Built once at import time from each table's own literal keys above —
# same "no external file, no lazy-loading reason to defer this"
# posture eo/mech_motion.py's own _NORMALIZED_MOTION_TABLE and
# eo/mech_mass.py's own _NORMALIZED_MASS_TABLE already take.
_NORMALIZED_THERMAL_TABLE = {_normalize(k): v for k, v in THERMAL_TABLE.items()}
_NORMALIZED_VIBRATION_TABLE = {_normalize(k): v for k, v in VIBRATION_TABLE.items()}


def lookup_thermal(generic_name: str) -> str | None:
    """Patch F.1's thermal entry point. Looks up `generic_name`
    (normalized) against THERMAL_TABLE's own vocabulary above,
    returning one of `"none"`, `"warm"`, `"hot"` on a match.

    Returns `None` — never raises — both when `generic_name` matches
    nothing in the table and when it isn't a usable string at all
    (missing/None/empty/whitespace-only), so a caller (Patch F.2's
    estimate_thermal_and_vibration() below, or a future F.3 cutout-
    generation caller, NOT this patch) can treat every "no curated
    entry" case identically: a clean `None` (unknown), never confused
    with a curated `"none"` (a considered "this part genuinely doesn't
    run warm/hot" answer) — see this module's own top docstring on why
    that distinction matters.
    """
    if not isinstance(generic_name, str):
        return None

    key = _normalize(generic_name)
    if not key:
        return None

    return _NORMALIZED_THERMAL_TABLE.get(key)


def lookup_vibration(generic_name: str) -> bool | None:
    """Patch F.1's vibration entry point. Looks up `generic_name`
    (normalized) against VIBRATION_TABLE's own vocabulary above,
    returning the curated `True`/`False` on a match (e.g. `True` for
    "wheel", explicit `False` for "caster wheel" — see VIBRATION_TABLE's
    own comments).

    Returns `None` — never raises — both when `generic_name` matches
    nothing in the table and when it isn't a usable string at all, same
    "unknown is its own distinct value from a curated False" reasoning
    lookup_thermal() above already documents for the identical shape.
    """
    if not isinstance(generic_name, str):
        return None

    key = _normalize(generic_name)
    if not key:
        return None

    return _NORMALIZED_VIBRATION_TABLE.get(key)


# ---------------------------------------------------------------------------
# Patch F.2 — LLM fallback for parts missing from both tables above.
# ---------------------------------------------------------------------------
#
# Same "cheap deterministic path first, escalate only on a genuine
# miss" pattern eo/mech_motion.py's own lookup_motion() ->
# estimate_motion() pair and eo/mech_mass.py's own lookup_mass() ->
# estimate_mass() pair already establish for Phase B/C: both lookup
# functions above are the cheap path; estimate_thermal_and_vibration()
# below is ONLY ever called by a future caller (Phase F's future F.3
# cutout-generation wiring and F.4 validator wiring, not this patch)
# after BOTH lookups have already missed for a given part, never
# unconditionally alongside them.
#
# Deviation from the guide's own literal Patch F.2 wording ("Add
# estimation functions" — plural): this module exposes ONE combined
# estimator covering both properties, not two separate ones. A part
# missing from THERMAL_TABLE is, in practice, overwhelmingly also
# missing from VIBRATION_TABLE (both tables are curated over the same
# small "does this part run hot / does this part shake" hobby-part
# vocabulary — see this module's own top docstring on why they're
# still two separate tables despite that overlap), so a future caller
# needing either property for an uncurated part almost always needs
# both — same real-world co-occurrence eo/device_archetype.py's own
# classify_archetype() output (`enclosure_mode` + `mobility_type`,
# always resolved together, never independently) already reflects for
# a different pair of properties. One combined call halves the LLM
# spend for that common case versus two independent single-property
# calls, at no cost to a caller that only actually needs one of the
# two fields back — see the function's own docstring below on exactly
# when a caller should reach for it.

# Last-resort static chain, used ONLY if eo/dynamic_chain.py's
# build_fallback_chain() comes back empty (every registered account
# excluded/cooling down at once — should be very rare). Same
# belt-and-suspenders shape and same first-model pin as
# eo/mech_motion.py's own FALLBACK_CHAIN / eo/mech_mass.py's own
# FALLBACK_CHAIN — one entry is enough here for the same reason it's
# enough there: this is a single short per-part classification, not a
# multi-thousand-token spec generation.
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY", "timeout": 30},
]

_VALID_THERMAL_CLASSES = {"none", "warm", "hot"}

# Deliberately terse and JSON-only, same "respond only valid JSON, no
# preamble" convention every other programmatically-parsed LLM call in
# this codebase already uses (eo/mech_motion.py's own
# _MOTION_SYSTEM_PROMPT, eo/mech_mass.py's own _MASS_SYSTEM_PROMPT).
_THERMAL_VIBRATION_SYSTEM_PROMPT = """You are a thermal/vibration \
classifier for a hardware BOM part used in a small 3D-printed hobby- \
electronics/hobby-robotics enclosure. Given a part's name/description, \
judge two INDEPENDENT properties about that part's behavior during its \
own normal, sustained operation:

1. Thermal class -- does the part itself get noticeably warm or hot \
during normal use? Most electronic parts (sensors, MCUs at idle, \
displays, buttons, connectors, batteries, passive/structural parts) \
run at essentially room temperature and should be "none". Only \
classify "warm" or "hot" if the part is genuinely known to dissipate \
meaningful heat in normal operation (e.g. a motor under sustained \
load, a voltage regulator, a motor driver IC, a compute SoC under \
load). Prefer "none" over guessing "warm".

2. Vibration source -- does the part itself generate mechanical \
vibration while operating (e.g. a spinning motor, an unbalanced \
wheel, a vibration motor, a fan), as opposed to simply being carried \
around by something else that moves? Most parts are NOT vibration \
sources. Prefer false over guessing true.

Respond with ONLY a valid JSON object, no markdown fences, no \
preamble, no explanation, in exactly this shape:

{"thermal_class": "none"|"warm"|"hot", "vibration_source": true|false}

When genuinely unsure about either property, answer the SAFE default \
for that property ("none" for thermal_class, false for \
vibration_source) rather than guessing -- both properties are meant to \
flag a clear, known behavior, not a maybe."""


def _validate_thermal_vibration_shape(result: dict) -> dict | None:
    """Checks a parsed LLM response against this module's own single
    valid shape (`{"thermal_class": "none"|"warm"|"hot",
    "vibration_source": bool}`) -- a missing/mistyped key or an
    out-of-vocabulary `thermal_class` is treated exactly like a parse
    failure below: `None`, not a partially-trusted guess propagated
    downstream. Same "out-of-vocabulary shape is a parse failure, not
    a partial guess" posture eo/mech_motion.py's own
    _validate_motion_shape() and eo/mech_mass.py's own
    _validate_mass_shape() already take for their own shapes.
    """
    if not isinstance(result, dict):
        return None

    thermal_class = result.get("thermal_class")
    if thermal_class not in _VALID_THERMAL_CLASSES:
        return None

    vibration_source = result.get("vibration_source")
    if not isinstance(vibration_source, bool):
        return None

    return {"thermal_class": thermal_class, "vibration_source": vibration_source}


def estimate_thermal_and_vibration(part: dict) -> dict:
    """Patch F.2's entry point -- the LLM fallback for a part
    lookup_thermal()/lookup_vibration() (F.1, above) already missed
    for. Callers (Phase F's future F.3/F.4 wiring, not this patch)
    should only ever reach this after BOTH lookups have already missed
    for the same part -- see this module's own "Patch F.2" section
    header comment above on why this is one combined call rather than
    two independent ones, and on why a caller that only needs one of
    the two curated tables to have missed should still prefer this
    over guessing the other field itself.

    Unlike eo/mech_mass.py's own estimate_mass() (every physical part
    has SOME mass, so that function never has a legitimate "confidently
    nothing" answer) and much like eo/mech_motion.py's own
    estimate_motion() (which legitimately returns `None` for a
    genuinely static part), MOST parts genuinely have neither property
    -- literal Patch F.2 wording, "defaulting to 'none'/False when the
    model has no basis to say otherwise (avoid over-triggering)". So on
    a falsy/unusable `part`, a failed/unparseable LLM response, or an
    out-of-shape response, this function falls back to the SAFE default
    for both fields (`{"thermal_class": "none", "vibration_source":
    False}`) rather than `None` -- same "resolves to a definite value,
    never a bare failure, for a case a caller (a future validator/
    cutout-generation pass) needs to reason about for every part it
    checks" posture eo/mech_mass.py's own estimate_mass() already
    documents for itself, just landing on the "nothing flagged" default
    instead of a placeholder positive value, since (unlike mass) "no
    thermal/vibration property at all" IS the correct default answer
    for most real parts.

    Returns `{"thermal_class": str, "vibration_source": bool}` on every
    path -- never `None`, never raises.
    """
    default = {"thermal_class": "none", "vibration_source": False}

    if not isinstance(part, dict):
        return default

    part_name = part.get("generic_name") or part.get("name")
    if not part_name:
        return default

    description = part.get("description") or ""
    user_prompt = f"Part: {part_name}\nDescription: {description}".strip()

    # Deferred import -- same circular-import reason eo/mech_motion.py's
    # own estimate_motion() and eo/mech_mass.py's own estimate_mass()
    # already document for the identical deferred import: eo/
    # dynamic_chain.py imports eo.registry at ITS own module level, so a
    # module-level import here would risk the same shape those modules'
    # own docstrings flag for any eagerly-imported agents/*.py caller.
    # eo.mech_thermal isn't imported by eo.registry today, but keeping
    # this deferred costs nothing and keeps this module safe to import
    # from anywhere without relitigating this later.
    from agents.structure_architect import _strip_fences  # reuse, don't reimplement
    from eo.dynamic_chain import build_fallback_chain
    from utils.llm_client import generate_text

    chain = build_fallback_chain("mech_thermal") or FALLBACK_CHAIN

    try:
        raw = generate_text(
            _THERMAL_VIBRATION_SYSTEM_PROMPT, user_prompt, chain,
            agent_name="Mech Thermal/Vibration Estimator",
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
        return default

    cleaned = (raw or "").strip()
    if not cleaned:
        return default

    # Same fence-stripping helper every other JSON-only LLM call in
    # this codebase already reuses rather than reimplementing (see
    # eo/mech_motion.py's own estimate_motion(), eo/mech_mass.py's own
    # estimate_mass()) -- a model occasionally wraps its answer in
    # ```json fences despite being told not to.
    try:
        result = json.loads(_strip_fences(cleaned))
    except json.JSONDecodeError:
        return default

    validated = _validate_thermal_vibration_shape(result)
    return validated if validated is not None else default
