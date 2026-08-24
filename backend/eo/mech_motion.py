"""
eo/mech_motion.py — Phase B, Patch B.1 of the Mech View standalone
implementation guide: the curated motion-parameter table half of
Phase B's swept-volume / moving-parts exclusion-box modeling (Part 1,
gap #2: "No modeling of moving parts... nothing prevents generated
geometry from physically colliding with a part mid-motion").

Same "curated dict, keyed on the part's own canonical generic_name,
normalized-string lookup, a table miss returns None rather than
guessing" pattern agents/component_dimension_table.py's own
lookup_curated_dimensions() (G1a) already establishes for real
component dimensions -- just a plain in-source dict here instead of a
JSON data file, since this table is a small, hand-curated set of
common hobby-robotics/hobby-electronics motion archetypes (a standard
hobby servo, a common wheel, ...), not a growable per-component
dataset that benefits from living outside the source tree. Same
reasoning eo/enclosure_spec.py's own docstring already gives for why
ENCLOSURE_SPEC is a plain dict and not its own data file: "six fixed
numbers... pure data, importable with no side effects."

Queried on the SAME canonical `generic_name` field
hardware_speccer.py's own _ensure_generic_names() already guarantees
is present and normalized on every part by the time any Phase B
consumer (Patch B.3's future swept_aabb_rotational()/
swept_aabb_linear(), not this patch) would call this table -- this
module never re-derives or guesses a part's own name, it only matches
against whatever canonical name the pipeline already settled on.

Three motion shapes this table's own values distinguish (mirroring
Phase B's own module docstring in the implementation guide -- "cheap
bounding-volume approximation... cylinders for continuous rotation,
elongated boxes for linear motion, and cardinal-angle-corrected
bounding boxes for arcs"):

  - "rotational_continuous": a full 360-degree spinner (a wheel, a
    continuous-rotation servo/motor shaft) -- swept volume is a
    cylinder, so the only shape parameter needed is `radius_mm`.
  - "rotational_arc": swings through a bounded angular range (a
    standard hobby servo horn, a hinged lid) -- needs `range_deg`
    ([start, end] in degrees) and `arm_length_mm` (the swept radius).
  - "linear": travels along one axis (a linear actuator's rod, a
    slide-out drawer) -- needs `travel_mm` and `axis` ("x"|"y"|"z").

Patch B.2 (extends this module, below lookup_motion): estimate_motion()
is the LLM fallback for exactly the parts this table's own lookup
misses, via eo/dynamic_chain.py's build_fallback_chain() -- same "try
the cheap, deterministic path first, escalate only on a genuine miss"
pattern eo/device_archetype.py's own classify_archetype() ->
resolve_ambiguous_archetype() already establishes for Phase A.
"""
import json

# Keyed on a normalized, lowercased, whitespace-collapsed form of the
# part's own canonical `generic_name` -- see _normalize() below.
# Deliberately small and hand-curated (not exhaustive): every entry
# here is a common, unambiguous hobby-electronics/hobby-robotics part
# type whose motion envelope is well-known and doesn't vary
# meaningfully between vendors, same "curated first, LLM-estimate the
# long tail" split this codebase's other curated tables already use
# (agents/component_dimension_table.py's own JSON table for real
# dimensions, eo/enclosure_spec.py's own CUTOUT_TABLE for cutout
# shapes).
#
# range_deg/arm_length_mm/radius_mm/travel_mm values below are
# reasonable defaults for the STANDARD/most common variant of each
# part type (e.g. a 9g/SG90-class hobby servo's typical 180-degree
# horn travel and a common short-arm horn length) -- a real project
# using an unusually large/small variant of one of these part types is
# exactly the kind of case Patch B.2's future LLM fallback exists for,
# not something this curated table tries to special-case.
MOTION_TABLE = {
    # Standard hobby servo (SG90/MG90S-class): swings through a bounded
    # arc, not a full rotation -- literal Phase B worked example from
    # the implementation guide itself.
    "hobby servo": {
        "type": "rotational_arc",
        "range_deg": [0, 180],
        "arm_length_mm": 25,
    },
    "micro servo": {
        "type": "rotational_arc",
        "range_deg": [0, 180],
        "arm_length_mm": 20,
    },

    # Continuous-rotation servo: same physical form factor as a hobby
    # servo above, but spins freely rather than swinging through a
    # bounded arc -- kept as its own entry (not aliased to "hobby
    # servo") since the two have genuinely different motion `type`s,
    # not just different parameter values.
    "continuous rotation servo": {
        "type": "rotational_continuous",
        "radius_mm": 20,
    },

    # Common hobby-robotics wheel -- literal Phase B worked example
    # from the implementation guide itself.
    "wheel": {
        "type": "rotational_continuous",
        "radius_mm": 30,
    },
    "caster wheel": {
        "type": "rotational_continuous",
        "radius_mm": 15,
    },

    # A bare DC gear motor's own output shaft -- full rotation, no
    # declared attachment (a wheel mounted on one gets its own "wheel"
    # entry above instead, sized to the wheel itself, not the motor
    # shaft it's mounted on).
    "dc gear motor": {
        "type": "rotational_continuous",
        "radius_mm": 10,
    },

    # 28BYJ-48-class stepper motor's own output shaft -- same "bare
    # shaft, no attachment" reasoning as the DC gear motor entry above.
    "stepper motor": {
        "type": "rotational_continuous",
        "radius_mm": 14,
    },

    # A hinged access lid/hatch (Phase D's future access-mechanism
    # geometry, not this patch) swings through a bounded arc just like
    # a servo horn, just with a longer arm and a shallower typical
    # travel range.
    "hinged lid": {
        "type": "rotational_arc",
        "range_deg": [0, 100],
        "arm_length_mm": 40,
    },

    # Linear actuator: travels along a single declared axis rather
    # than rotating -- literal Phase B worked example's third motion
    # family ("elongated boxes for linear motion").
    "linear actuator": {
        "type": "linear",
        "travel_mm": 50,
        "axis": "z",
    },
}


def _normalize(text: str) -> str:
    """Case/whitespace-insensitive matching key -- same normalization
    agents/component_dimension_table.py's own _normalize() already
    uses for the identical reason: the vocabulary on both sides
    (this table's own keys, a part's canonical generic_name) is
    already meant to be a short, canonical, human-written name, not
    free text, so exact normalized matching is the right amount of
    matching, not too little.
    """
    return " ".join((text or "").strip().lower().split())


# Built once at import time from MOTION_TABLE's own literal keys above
# -- unlike component_dimension_table.py's _load_table(), there is no
# external file to read and no lazy-loading reason to defer this, so
# it's just a plain module-level dict comprehension.
_NORMALIZED_MOTION_TABLE = {_normalize(k): v for k, v in MOTION_TABLE.items()}


def lookup_motion(generic_name: str) -> dict | None:
    """Patch B.1's entry point. Looks up `generic_name` (normalized)
    against MOTION_TABLE's own vocabulary above, returning a COPY of
    the matched entry (a fresh dict, not the table's own stored one --
    same "never hand a caller a live reference into shared curated
    data" caution _row_to_match() takes in
    agents/component_dimension_table.py, there by reshaping the row
    entirely, here by copying it) so a caller mutating its own result
    can never corrupt this module's table for every later lookup.

    Returns `None` -- never raises -- both when `generic_name` matches
    nothing in the table and when it isn't a usable string at all
    (missing/None/empty/whitespace-only), so a caller (Patch B.2's
    future estimate_motion() fallback, NOT this patch) can treat every
    "no curated entry" case identically: a clean `None`, not an
    exception to catch.
    """
    if not isinstance(generic_name, str):
        return None

    key = _normalize(generic_name)
    if not key:
        return None

    entry = _NORMALIZED_MOTION_TABLE.get(key)
    if entry is None:
        return None

    return dict(entry)


# ---------------------------------------------------------------------------
# Patch B.2 — LLM fallback for parts B.1's curated table misses.
# ---------------------------------------------------------------------------
#
# Same "cheap deterministic path first, escalate only on a genuine
# miss" pattern eo/device_archetype.py's own classify_archetype() ->
# resolve_ambiguous_archetype() pair already establishes for Phase A:
# lookup_motion() above is the cheap path; estimate_motion() below is
# ONLY ever called by a future caller (Phase B's future swept-volume
# wiring, not this patch) after that lookup has already missed, never
# unconditionally alongside it.

# Last-resort static chain, used ONLY if eo/dynamic_chain.py's
# build_fallback_chain() comes back empty (every registered account
# excluded/cooling down at once -- should be very rare). Same
# belt-and-suspenders shape and same first-model pin as
# eo/device_archetype.py's own FALLBACK_CHAIN (see that module's own
# comment for the full reasoning) -- one entry is enough here for the
# same reason it's enough there: this is a single short per-part
# classification, not a multi-thousand-token spec generation.
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY", "timeout": 30},
]

# Sentinel the prompt asks for when the model determines the part has
# no motion at all -- checked for explicitly below rather than treated
# as just another parse failure, so "the model looked at this and
# confidently said static" and "the model's response was garbage" stay
# distinguishable in a future debug/log pass, even though both
# currently resolve to the same `None` return.
_NO_MOTION_SENTINEL = "NONE"

_VALID_MOTION_TYPES = {"rotational_continuous", "rotational_arc", "linear"}
_VALID_AXES = {"x", "y", "z"}

# Deliberately terse and JSON-only (or the bare NONE sentinel), same
# "respond only valid JSON, no preamble" convention every other
# programmatically-parsed LLM call in this codebase already uses
# (agents/hardware_speccer.py's own SYSTEM_PROMPT_PARTS constants,
# eo/device_archetype.py's own _ARCHETYPE_SYSTEM_PROMPT).
_MOTION_SYSTEM_PROMPT = """You are a mechanical-motion classifier for \
a hardware BOM part. Given a part's name/description, decide whether \
it has any physical motion of its own during normal operation -- NOT \
whether it gets carried around by a moving robot, just whether the \
part ITSELF moves relative to whatever it's mounted to.

Most electronic parts (resistors, capacitors, MCUs, sensors, \
batteries, displays, buzzers, most breakout boards) have NO motion of \
their own. If the part is one of these, or you are not confident it \
has genuine self-motion, respond with EXACTLY the single word:

NONE

If -- and only if -- the part clearly has its own motion, respond \
with ONLY a valid JSON object, no markdown fences, no preamble, no \
explanation, in exactly one of these three shapes:

1. Full continuous rotation (e.g. a wheel, a continuous-rotation \
servo/motor shaft): {"type": "rotational_continuous", "radius_mm": <number>}

2. Bounded angular swing (e.g. a standard hobby servo horn, a hinged \
lid/arm): {"type": "rotational_arc", "range_deg": [<start>, <end>], \
"arm_length_mm": <number>}

3. Linear travel along one axis (e.g. a linear actuator rod, a \
slide-out drawer): {"type": "linear", "travel_mm": <number>, "axis": \
"x"|"y"|"z"}

All numeric values are in millimeters/degrees. When genuinely unsure \
of the exact size, give your best real-world estimate for that part \
type rather than a placeholder -- but if you are unsure whether the \
part moves AT ALL, answer NONE rather than guessing a motion."""


def _validate_motion_shape(result: dict) -> dict | None:
    """Checks a parsed LLM response against the same three shapes
    MOTION_TABLE's own curated entries use (see this module's own top
    docstring) -- an out-of-vocabulary `type`, or a shape missing a
    field its own `type` requires, is treated exactly like a parse
    failure below: `None`, not a partially-trusted guess propagated
    downstream.
    """
    if not isinstance(result, dict):
        return None

    motion_type = result.get("type")
    if motion_type not in _VALID_MOTION_TYPES:
        return None

    if motion_type == "rotational_continuous":
        radius_mm = result.get("radius_mm")
        if not isinstance(radius_mm, (int, float)) or radius_mm <= 0:
            return None
        return {"type": motion_type, "radius_mm": radius_mm}

    if motion_type == "rotational_arc":
        range_deg = result.get("range_deg")
        arm_length_mm = result.get("arm_length_mm")
        if (not isinstance(range_deg, list) or len(range_deg) != 2
                or not all(isinstance(v, (int, float)) for v in range_deg)):
            return None
        if not isinstance(arm_length_mm, (int, float)) or arm_length_mm <= 0:
            return None
        return {"type": motion_type, "range_deg": list(range_deg),
                "arm_length_mm": arm_length_mm}

    # motion_type == "linear"
    travel_mm = result.get("travel_mm")
    axis = result.get("axis")
    if not isinstance(travel_mm, (int, float)) or travel_mm <= 0:
        return None
    if axis not in _VALID_AXES:
        return None
    return {"type": motion_type, "travel_mm": travel_mm, "axis": axis}


def estimate_motion(part: dict) -> dict | None:
    """Patch B.2's entry point -- the LLM fallback for a part
    lookup_motion() (B.1, above) already returned `None` for. Callers
    (Phase B's future swept-volume wiring, not this patch) should only
    ever reach this AFTER a table miss, same "cheap path first" call
    order eo/device_archetype.py's own resolve_ambiguous_archetype()
    already documents for the identical reason: this is a real,
    billable LLM call, and most parts in a typical BOM are static, so
    it should never run unconditionally alongside the free table
    lookup.

    Returns `None` -- NOT a guess -- whenever: `part` is falsy or has
    no usable name; the model responds with the `NONE` sentinel
    (its own considered "this part doesn't move" answer); the response
    fails to parse as JSON; or the parsed JSON doesn't match one of
    MOTION_TABLE's own three valid shapes (`_validate_motion_shape()`
    above). Per this patch's own "done when": most parts are static,
    and every one of these outcomes should collapse to the same safe
    default a genuinely static part gets, rather than an edge case
    debugged into eventually returning something.

    Only ever returns a definite, `_validate_motion_shape()`-checked
    motion dict on a clear, well-formed "this part moves" answer.
    """
    if not isinstance(part, dict):
        return None

    part_name = part.get("generic_name") or part.get("name")
    if not part_name:
        return None

    description = part.get("description") or ""
    user_prompt = f"Part: {part_name}\nDescription: {description}".strip()

    # Deferred import -- same circular-import reason
    # eo/device_archetype.py's own resolve_ambiguous_archetype()
    # already documents for the identical deferred import: eo/
    # dynamic_chain.py imports eo.registry at ITS own module level, so
    # a module-level import here would risk the same shape that
    # module's own docstring flags for any eagerly-imported agents/*.py
    # caller. eo.mech_motion isn't imported by eo.registry today, but
    # keeping this deferred costs nothing and keeps this module safe to
    # import from anywhere without relitigating this later.
    from eo.dynamic_chain import build_fallback_chain
    from utils.llm_client import generate_text
    from agents.structure_architect import _strip_fences  # reuse, don't reimplement

    chain = build_fallback_chain("mech_motion") or FALLBACK_CHAIN

    try:
        raw = generate_text(
            _MOTION_SYSTEM_PROMPT, user_prompt, chain,
            agent_name="Mech Motion Estimator",
            allow_continuation=False,  # same Root Cause B reasoning as
            # every other "ONLY valid JSON" call in this codebase: this
            # prompt demands either the bare NONE sentinel or a single
            # complete JSON object, so a "length" truncation splicing a
            # continuation from a possibly-different provider onto a
            # half-finished object would corrupt it in a way this
            # function's own parsing below can't recover from -- a
            # fresh retry on the next chain step is safer than a splice
            # here.
        )
    except Exception:
        return None

    cleaned = (raw or "").strip()
    if not cleaned:
        return None

    # The model's own explicit "no motion" answer -- checked before any
    # JSON-fence stripping/parsing, exact (case-insensitive) match only,
    # so a hedge like "probably none" or "None, but..." falls through to
    # the parse-failure path below instead of being treated as a
    # confident NONE it didn't actually give.
    if cleaned.upper() == _NO_MOTION_SENTINEL:
        return None

    # Same fence-stripping helper every other JSON-only LLM call in
    # this codebase already reuses rather than reimplementing (see
    # eo/device_archetype.py's own resolve_ambiguous_archetype(), which
    # imports this exact function for the exact same reason) -- a model
    # occasionally wraps its answer in ```json fences despite being
    # told not to.
    try:
        result = json.loads(_strip_fences(cleaned))
    except json.JSONDecodeError:
        return None

    return _validate_motion_shape(result)
