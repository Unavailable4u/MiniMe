"""
eo/mech_validator.py — G3c (Master Guide, "G3/G4. Hierarchical parallel
build + validate"): the FreeCAD half of the generate->validate pattern.
Runs headless FreeCAD (real solid geometry, via its Python scripting API,
not axis-aligned arithmetic) against a level's proposed nodes and reports
containment/collision violations without ever modifying the layout.

Scope through G3e-3: Level 0->1 ("does every primitive a part is composed
of actually stay inside that part's own w/h/d bounding box, once its
declared offset/rotation is really applied") AND Level 1->2 ("does a
subsection's part collide with its own mount, once both are placed at
their real absolute positions, and what's the subsection's combined
footprint"). Level 2->3 / 3->4 (section/device collision + containment)
land with G3f/G3g, each calling into this same module with a new `level`
value once they exist -- validate_layout() below rejects any level it
doesn't implement yet rather than silently no-op'ing, so a premature
caller fails loudly instead of shipping an unchecked layout that looks
validated.

Level 1->2 specifics (G3e-3): reuses the exact same rotate-then-boolean
machinery Level 0->1 already built (_build_primitive_shape() below,
extended with an optional `base` translation so a primitive can be built
at its member's real absolute x/y/z, not just its part-local offset) --
"one geometry kernel, two things to ask it," not a second implementation.
A subsection's two members (a part and its "mount_"-prefixed sibling, per
eo/mech_subsections.py's group_into_subsections()) are boolean-intersected
against each other instead of cut against a bounding box: any non-noise
overlap volume is a real collision, full stop -- unlike Level 0->1's
confidence-aware clearance buffer, there's no "typical parts get some
slack" case for two solids actually occupying the same space. A singleton
subsection (no mount sibling) has nothing to collide with, so it only
ever contributes a footprint, never a violation. Every checkable
subsection (singleton or not) reports its own aggregate bounding footprint
-- the union bounding box of every member's composed primitives in
absolute space -- back to the caller regardless of whether it collided,
since eo/mech_repair.py's Level 1->2 driver (G3e-4) needs a footprint for
every subsection it ships, not just the clean ones, to hand off to G3f.

Why real FreeCAD instead of the same offset+size<=bounds arithmetic
agents/mech_primitive_pool.py's _clamp_primitive() already does at
generation time: that arithmetic only holds for an AXIS-ALIGNED box. Once
a primitive carries a nonzero rotation (agents/hardware_speccer.py's own
templates always emit {"x":0,"y":0,"z":0}, but agents/mech_primitive_
pool.py's LLM path is explicitly allowed to propose one "unless the
part's real-world orientation clearly calls for" it), the *rotated*
envelope can poke outside a box that the pre-rotation offset+size numbers
looked perfectly safe inside. A real geometry kernel (build the actual
solid, rotate it, boolean-cut it against the part's own bounding box, and
check what's left over) catches that; axis-aligned min/max arithmetic
can't. This is also, not coincidentally, exactly the kind of check this
module's own docstring in the Master Guide describes FreeCAD as being
for: "FreeCAD validates, it doesn't generate."

Dependency shape: this module doesn't import agents/hardware_speccer.py
or agents/mech_primitive_pool.py at all, and doesn't require either to
have run first at IMPORT time -- it only reads whatever's already sitting
on each `mech["placements"]` entry (`w`/`h`/`d`, `primitives`, optionally
`dimension_confidence`) when validate_layout() is actually called. A
placement with no `primitives` yet (G3a/G3b haven't run, or a part is
mid-repair) is skipped, not flagged -- "nothing to check yet" is not a
violation. This keeps G3c genuinely standalone and testable on its own,
per the build-order note in the planning thread: G3d's capped-repair
orchestrator (not built by this patch) is what will actually call this
in a loop and feed violations back as regeneration context; this module
only ever reports.

Confidence-aware tolerance: the Master Guide specifies a `verified` part
gets a strict 0-margin check and a `typical` part gets a small clearance
buffer before a violation is reported. `dimension_confidence` is set by
agents/hardware_speccer.py's G1a/G1b onto the PART, not the placement, so
a caller that also has `parts` on hand (agents/hardware_speccer.py does)
can copy it onto `placement["dimension_confidence"]` before calling this
-- G3c doesn't require that wiring to exist yet and defaults to the more
conservative "typical" buffer (never 0-margin) when it's absent, so an
unwired caller never gets falsely strict results.

Efficiency -- one FreeCAD sandbox session for the whole run: FreeCAD's
headless cold start is slow (same E2B sandbox agents/sandbox_tester.py
and agents/static_scan.py already use), and validating four separate
tree levels risks four cold starts per generation. This module keeps ONE
sandbox alive per run (keyed by session_id; see _sessions/_get_sandbox()
below) across every validate_layout() call in that run, and sends each
call's parts as ONE batched FreeCAD invocation rather than one sandbox
call per part -- "validate at every branch" without paying the cold-start
tax at every node. The FreeCAD apt install itself only happens once per
session too (see _ensure_ready()), on whichever call hits it first.
Callers MUST call close_session(session_id) once their run is fully done
(all levels validated, or the run is aborting) so the sandbox doesn't
sit billing idle -- this module never closes its own session automatically,
since it has no way to know a run is "done" from inside a single call.
"""

import json
import os
import sys
import threading
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from relay.emitter import emit_event
from e2b_code_interpreter import Sandbox
from eo.mech_subsections import group_into_subsections, members_for_subsection

# Level vocabulary matches the arrow notation the Master Guide and this
# codebase's own docstrings already use ("Level 0->1" in agents/
# mech_primitive_pool.py's module docstring, agents/hardware_speccer.py's
# G3a comments, eo/registry.py) -- no new naming invented here.
LEVEL_0_1 = "0->1"
LEVEL_1_2 = "1->2"
_IMPLEMENTED_LEVELS = {LEVEL_0_1, LEVEL_1_2}

# Confidence-aware clearance buffer (Master Guide: "a `verified` part
# gets a strict 0-margin check; a `typical` part gets a small clearance
# buffer"). Absent/unrecognized confidence falls back to the *typical*
# buffer, not 0-margin -- see module docstring on why that's the safer
# default for a placement that hasn't been wired up to carry the field
# yet.
_DEFAULT_CONFIDENCE = "typical"
_TOLERANCE_MM = {"verified": 0.0, "typical": 1.5}

# A cut-leftover volume this small is float/mesh-tolerance noise from the
# geometry kernel itself, not a real protrusion -- same "don't false-
# positive on noise" spirit as the confidence buffer above, just at the
# numerical-precision layer instead of the design layer.
_VOLUME_EPSILON_MM3 = 1e-2

_SANDBOX_TIMEOUT_S = 900  # 15 min -- long enough to outlive a full run's worth of per-level validation calls, not so long it bills idle indefinitely if a caller forgets close_session().
_INPUT_PATH = "/tmp/mech_validate_input.json"
_OUTPUT_PATH = "/tmp/mech_validate_output.json"
_SCRIPT_PATH = "/tmp/mech_validate.py"

# Best-effort install, same "|| true" tolerance agents/static_scan.py's
# own _SETUP_CMD already uses for its non-preinstalled tools -- a flaky
# apt mirror degrades this run to "validator unavailable" (see
# validate_layout()'s except-branch below), never a hard crash of
# whatever pipeline step called into this module.
_FREECAD_INSTALL_CMD = (
    "apt-get update -qq >/tmp/freecad_install.log 2>&1 && "
    "apt-get install -y -qq freecad >>/tmp/freecad_install.log 2>&1 || true"
)
_FREECAD_PROBE_CMD = "command -v freecadcmd || command -v FreeCADCmd || true"

# Runs INSIDE the sandbox via `freecadcmd`, not this process -- argv[1]/
# argv[2] are the input/output JSON paths (no python-side str.format
# templating here, so FreeCAD's own extensive use of {}-free but
# otherwise ordinary Python syntax needs no escaping).
_FREECAD_SCRIPT = r"""
import json
import sys

import FreeCAD
import Part


def _build_primitive_shape(prim, base=None):
    shape = prim.get("shape") or "box"
    size = prim.get("size") or {}
    offset = prim.get("offset") or {}
    rotation = prim.get("rotation") or {}
    base = base or {}
    bx = float(base.get("x") or 0)
    by = float(base.get("y") or 0)
    bz = float(base.get("z") or 0)

    w = max(float(size.get("w") or 0), 0.01)
    h = max(float(size.get("h") or 0), 0.01)
    d = max(float(size.get("d") or 0), 0.01)
    ox = float(offset.get("x") or 0)
    oy = float(offset.get("y") or 0)
    oz = float(offset.get("z") or 0)
    rx = float(rotation.get("x") or 0)
    ry = float(rotation.get("y") or 0)
    rz = float(rotation.get("z") or 0)

    # "w as diameter, h as height" -- same convention agents/
    # hardware_speccer.py's own _cylinder_primitive_template() docstring
    # states and MechView.jsx's PrimitiveGeometry already renders by.
    # Deliberately NOT min(w, d)/max(w, d) averaging: using w alone means
    # a part whose real footprint needs d > w to hold a w-diameter
    # cylinder genuinely fails containment below -- exactly the kind of
    # mismatch this check exists to catch, not paper over.
    if shape == "cylinder":
        radius = w / 2.0
        solid = Part.makeCylinder(radius, h)
    elif shape == "cone":
        radius = w / 2.0
        solid = Part.makeCone(radius, 0.0, h)
    else:
        solid = Part.makeBox(w, h, d)

    # Rotate about the primitive's own center (the sensible modeling
    # default for "this part's real-world orientation" -- nothing else
    # in the schema specifies a different pivot), THEN translate to its
    # declared offset -- offset is documented (agents/mech_primitive_
    # pool.py's SYSTEM_PROMPT) as the primitive's own corner position,
    # so the center-relative rotation still lands the shape's corner at
    # `offset` once un-rotated.
    cx, cy, cz = w / 2.0, h / 2.0, d / 2.0
    solid.translate(FreeCAD.Vector(-cx, -cy, -cz))
    if rx:
        solid.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(1, 0, 0), rx)
    if ry:
        solid.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 1, 0), ry)
    if rz:
        solid.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), rz)
    # `base` (Level 1->2's addition -- always {0,0,0} for Level 0->1's own
    # calls below, so this is a no-op there): a member's own absolute
    # x/y/z on top of its part-local corner offset, so two different
    # placements' primitives land in the SAME shared coordinate frame and
    # can be boolean-intersected against each other for a real collision
    # check, not just each checked against its own bounding box.
    solid.translate(FreeCAD.Vector(cx + ox + bx, cy + oy + by, cz + oz + bz))
    return solid


def _build_bounds_shape(w, h, d, tolerance):
    bw = max(w + 2 * tolerance, 0.01)
    bh = max(h + 2 * tolerance, 0.01)
    bd = max(d + 2 * tolerance, 0.01)
    solid = Part.makeBox(bw, bh, bd)
    solid.translate(FreeCAD.Vector(-tolerance, -tolerance, -tolerance))
    return solid


def _check_part(part):
    w = max(float(part.get("w") or 0), 0.01)
    h = max(float(part.get("h") or 0), 0.01)
    d = max(float(part.get("d") or 0), 0.01)
    tolerance = float(part.get("tolerance_mm") or 0)
    bounds = _build_bounds_shape(w, h, d, tolerance)

    leftover_mm3 = 0.0
    for prim in part.get("primitives") or []:
        try:
            prim_shape = _build_primitive_shape(prim)
            leftover = prim_shape.cut(bounds)
            leftover_mm3 += leftover.Volume
        except Exception:
            # A degenerate primitive (bad geometry kernel input) is
            # treated as a real violation, not silently skipped -- same
            # "flag, never silently drop" posture the rest of this
            # check uses.
            leftover_mm3 += 1.0

    return leftover_mm3


def _check_subsection(subsection):
    # Level 1->2's per-subsection check: builds every member's composed
    # primitives in the SAME absolute frame (each primitive's own local
    # offset plus its member's absolute x/y/z, via _build_primitive_shape's
    # `base` param), boolean-intersects every cross-member primitive pair
    # for a real collision volume, and unions every member's own bounding
    # box into the subsection's aggregate footprint. A subsection with only
    # one member (no mount to collide with, per eo/mech_subsections.py's
    # singleton case) skips the collision loop entirely but still returns a
    # footprint -- see module docstring.
    #
    # Returns (collision_mm3, footprint | None). footprint is None only
    # when every member's primitives failed to build (degenerate input --
    # same "flag, never silently drop" posture _check_part() already uses,
    # handled by the caller in main() below).
    members = subsection.get("members") or []

    member_solids = []  # one list of solids per member, in member order
    bounds = None  # [xmin, ymin, zmin, xmax, ymax, zmax]
    build_failures = 0

    for member in members:
        base = {"x": member.get("x") or 0, "y": member.get("y") or 0, "z": member.get("z") or 0}
        solids = []
        for prim in member.get("primitives") or []:
            try:
                solid = _build_primitive_shape(prim, base=base)
            except Exception:
                build_failures += 1
                continue
            solids.append(solid)
            bb = solid.BoundBox
            if bounds is None:
                bounds = [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]
            else:
                bounds[0] = min(bounds[0], bb.XMin)
                bounds[1] = min(bounds[1], bb.YMin)
                bounds[2] = min(bounds[2], bb.ZMin)
                bounds[3] = max(bounds[3], bb.XMax)
                bounds[4] = max(bounds[4], bb.YMax)
                bounds[5] = max(bounds[5], bb.ZMax)
        member_solids.append(solids)

    collision_mm3 = 0.0
    for i in range(len(member_solids)):
        for j in range(i + 1, len(member_solids)):
            for si in member_solids[i]:
                for sj in member_solids[j]:
                    try:
                        overlap = si.common(sj)
                        collision_mm3 += overlap.Volume
                    except Exception:
                        # Same "degenerate input is a real violation, not
                        # a silent skip" posture _check_part() uses.
                        collision_mm3 += 1.0

    if bounds is None:
        return collision_mm3, None

    footprint = {
        "x": round(bounds[0], 3), "y": round(bounds[1], 3), "z": round(bounds[2], 3),
        "w": round(bounds[3] - bounds[0], 3),
        "h": round(bounds[4] - bounds[1], 3),
        "d": round(bounds[5] - bounds[2], 3),
    }
    return collision_mm3, footprint


def main():
    input_path, output_path = sys.argv[1], sys.argv[2]
    with open(input_path) as f:
        payload = json.load(f)

    level = payload.get("level")
    violations = []

    if level == %(level_1_2)r:
        footprints = {}
        for subsection in payload.get("subsections") or []:
            subsection_id = subsection.get("subsection_id")
            try:
                collision_mm3, footprint = _check_subsection(subsection)
            except Exception as exc:
                violations.append({
                    "node_id": subsection_id,
                    "issue": f"geometry check failed: {exc}",
                })
                continue
            if footprint is not None:
                footprints[subsection_id] = footprint
            if collision_mm3 > %(volume_epsilon)r:
                violations.append({
                    "node_id": subsection_id,
                    "issue": (
                        "part and mount collide "
                        f"(~{round(collision_mm3, 2)} mm^3 overlap)"
                    ),
                })
        with open(output_path, "w") as f:
            json.dump({"violations": violations, "footprints": footprints}, f)
        return

    for part in payload.get("parts") or []:
        part_id = part.get("part_id")
        try:
            leftover_mm3 = _check_part(part)
        except Exception as exc:
            violations.append({
                "node_id": part_id,
                "issue": f"geometry check failed: {exc}",
            })
            continue
        if leftover_mm3 > %(volume_epsilon)r:
            violations.append({
                "node_id": part_id,
                "issue": (
                    "primitive(s) extend outside the part's own bounding "
                    f"box (~{round(leftover_mm3, 2)} mm^3 outside)"
                ),
            })

    with open(output_path, "w") as f:
        json.dump({"violations": violations}, f)


main()
""" % {"volume_epsilon": _VOLUME_EPSILON_MM3, "level_1_2": LEVEL_1_2}


# ---------------------------------------------------------------------------
# Persistent per-run sandbox session -- see module docstring's
# "Efficiency" section. Keyed by session_id so concurrent runs (two users
# generating at once) never share or contend a single sandbox; a caller
# with no session_id (a script/test run) shares one process-wide
# "_default" entry instead of spinning up a new sandbox per call, which
# is still strictly better than the old per-level-call cold start this
# replaces even in that fallback case.
# ---------------------------------------------------------------------------

_sessions_lock = threading.Lock()
_sessions = {}  # session_key -> {"sandbox": Sandbox, "ready": bool}


def _session_key(session_id: str = None) -> str:
    return session_id or "_default"


def _get_session(session_id: str = None) -> dict:
    """Returns this run's persistent sandbox entry, creating one on first
    use. Does NOT install FreeCAD here -- that's _ensure_ready()'s job,
    called lazily from _run_batch() so a session that's created but never
    actually validated (e.g. a run with zero mech parts) never pays the
    install cost at all."""
    key = _session_key(session_id)
    with _sessions_lock:
        entry = _sessions.get(key)
        if entry is not None:
            return entry
        entry = {"sandbox": Sandbox.create(timeout=_SANDBOX_TIMEOUT_S), "ready": False}
        _sessions[key] = entry
        return entry


def _ensure_ready(entry: dict) -> None:
    """Installs FreeCAD into this session's sandbox exactly once (the
    slow cold-start setup note.md flags) -- probes for an already-present
    binary first so a custom E2B template with FreeCAD pre-baked (the
    module docstring's own suggested follow-up if this ends up
    dominating scan time, same note agents/static_scan.py's docstring
    makes about Gitleaks/Semgrep) skips the apt install entirely."""
    if entry["ready"]:
        return
    sbx = entry["sandbox"]
    probe = sbx.commands.run(_FREECAD_PROBE_CMD, timeout=15)
    binary = (probe.stdout or "").strip().splitlines()[-1] if probe.stdout else ""
    if not binary:
        sbx.commands.run(_FREECAD_INSTALL_CMD, timeout=240)
        probe = sbx.commands.run(_FREECAD_PROBE_CMD, timeout=15)
        binary = (probe.stdout or "").strip().splitlines()[-1] if probe.stdout else ""
    if not binary:
        raise RuntimeError("freecadcmd not available in sandbox after install attempt")
    entry["freecad_bin"] = binary
    entry["ready"] = True


def close_session(session_id: str = None) -> None:
    """Kills and forgets this run's persistent FreeCAD sandbox. Safe to
    call even when no session was ever created for this session_id (a
    run with zero mech parts, or one that never reached mech validation)
    -- a no-op in that case, not an error. Callers (G3d's orchestrator,
    once built, or whatever drives a full generation run) MUST call this
    once a run is fully done -- see module docstring's "Efficiency"
    section on why this module never does so on its own."""
    key = _session_key(session_id)
    with _sessions_lock:
        entry = _sessions.pop(key, None)
    if entry is None:
        return
    try:
        entry["sandbox"].kill()
    except Exception:
        pass  # best-effort cleanup -- a kill failure here never should surface as this run's problem, the sandbox will simply expire on its own _SANDBOX_TIMEOUT_S.


def _tolerance_for(confidence) -> float:
    return _TOLERANCE_MM.get(confidence, _TOLERANCE_MM[_DEFAULT_CONFIDENCE])


def _checkable_placements(placements) -> list:
    """Level 0->1's scope: any placement that actually has a non-empty
    `primitives` list to check. A placement G3a/G3b haven't reached yet
    (no `primitives` at all) is skipped, not flagged -- see module
    docstring."""
    return [
        p for p in (placements or [])
        if isinstance(p, dict) and isinstance(p.get("primitives"), list) and p["primitives"]
    ]


def _build_payload(checkable: list) -> dict:
    # No "level" key here (unlike _build_subsection_payload below) -- the
    # FreeCAD script's main() only branches on level == LEVEL_1_2, so
    # Level 0->1's payload staying exactly the shape it was before G3e-3
    # keeps this function's output byte-for-byte backward compatible.
    return {
        "parts": [
            {
                "part_id": p.get("part_id"),
                "w": p.get("w") or 0,
                "h": p.get("h") or 0,
                "d": p.get("d") or 0,
                "tolerance_mm": _tolerance_for(p.get("dimension_confidence")),
                "primitives": p.get("primitives") or [],
            }
            for p in checkable
        ],
    }


def _checkable_subsections(mech: dict) -> list:
    """Level 1->2's scope (G3e-3): every subsection eo/mech_subsections.py's
    group_into_subsections() reports, resolved down to just the members
    that already have a non-empty `primitives` list -- same "skip, don't
    flag, a placement G3a/G3b/G3c-Level-0->1 haven't reached yet" posture
    _checkable_placements() already holds for Level 0->1. A subsection
    where NO member has primitives yet is skipped entirely (nothing to
    build); a subsection with at least one composed member is still
    checked with whatever it has, so a singleton (never has a second
    member to begin with) or a subsection whose mount hasn't been
    composed yet both still contribute a footprint for what IS ready,
    rather than the whole subsection waiting on its slowest member.

    Returns [{"subsection_id": str, "members": [placement, ...]}, ...] --
    `members` already resolved to full placement dicts (via eo/
    mech_subsections.py's own members_for_subsection() helper, its
    documented reason for existing) and filtered to composed-only.
    """
    checkable = []
    for subsection in group_into_subsections(mech):
        members = [
            m for m in members_for_subsection(mech, subsection)
            if isinstance(m, dict) and isinstance(m.get("primitives"), list) and m["primitives"]
        ]
        if not members:
            continue
        checkable.append({"subsection_id": subsection.get("subsection_id"), "members": members})
    return checkable


def _build_subsection_payload(checkable: list) -> dict:
    return {
        "level": LEVEL_1_2,
        "subsections": [
            {
                "subsection_id": s.get("subsection_id"),
                "members": [
                    {
                        "part_id": m.get("part_id"),
                        "x": m.get("x") or 0,
                        "y": m.get("y") or 0,
                        "z": m.get("z") or 0,
                        "primitives": m.get("primitives") or [],
                    }
                    for m in s.get("members") or []
                ],
            }
            for s in checkable
        ],
    }


def _run_batch(payload: dict, session_id: str = None) -> dict:
    """Sends one batched FreeCAD invocation covering every part in
    `payload` through this run's persistent sandbox session. On ANY
    failure (sandbox died, freecadcmd crashed, output unparseable), the
    session is closed so the NEXT call gets a fresh sandbox instead of
    repeatedly hitting a wedged one -- retrying inline here is
    deliberately NOT this module's job; that capped-retry policy belongs
    to G3d's orchestrator (not built by this patch), which decides
    whether to retry at all versus ship flagged-not-blocked."""
    entry = _get_session(session_id)
    try:
        _ensure_ready(entry)
        sbx = entry["sandbox"]
        sbx.files.write(_INPUT_PATH, json.dumps(payload))
        sbx.files.write(_SCRIPT_PATH, _FREECAD_SCRIPT)
        sbx.commands.run(
            f"{entry['freecad_bin']} {_SCRIPT_PATH} {_INPUT_PATH} {_OUTPUT_PATH} "
            f">/tmp/mech_validate_run.log 2>&1 || true",
            timeout=120,
        )
        raw = sbx.files.read(_OUTPUT_PATH)
        return json.loads(raw)
    except Exception:
        close_session(session_id)
        raise


def validate_layout(mech: dict, level: str, session_id: str = None,
                     path: str = None, domain: str = None) -> dict:
    """Runs headless FreeCAD against one tree level's proposed nodes.
    THIS patch only implements level=LEVEL_0_1 ("0->1"): does every
    primitive a part is composed of stay inside that part's own w/h/d
    bounding box, once its real offset/rotation is applied. Level 1->2 /
    2->3 / 3->4 (subsection/section/device collision + containment) are
    NOT implemented here -- calling with any other `level` raises
    NotImplementedError rather than silently reporting `valid: True` for
    a level nothing actually checked.

    Never modifies `mech` -- read-only, matches the Master Guide's own
    contract for this function ("never modifies the layout, only
    reports").

    Returns {"valid": bool, "violations": [{"node_id", "issue"}]}, plus
    a "validator_error" key (FreeCAD/sandbox infrastructure failure, not
    a real geometry violation) when present -- on that path `valid` is
    still True and `violations` still [], since "couldn't validate" and
    "validated clean" must never look identical to a caller deciding
    whether to ship a node, but also must never block a pipeline on an
    infrastructure problem. See module docstring's dependency-shape note
    on why this degrades rather than raising for that specific failure
    mode (sandbox/apt/network), unlike the level-not-implemented check
    above, which IS a programmer error worth raising loudly.

    Level 1->2 (LEVEL_1_2) additionally returns a "footprints" key,
    {subsection_id: {"x","y","z","w","h","d"}}, for every checkable
    subsection regardless of whether it violated -- see module docstring
    and _check_subsection()'s own docstring inside _FREECAD_SCRIPT above
    on why a footprint is reported even for a clean subsection (G3f needs
    it to group by next). Absent (empty dict) on the no-op and fail-open
    paths below, same "nothing was actually computed" reasoning as
    `violations` being [] on those same paths.
    """
    if level not in _IMPLEMENTED_LEVELS:
        raise NotImplementedError(
            f"eo/mech_validator.py only implements level={LEVEL_0_1!r} and "
            f"level={LEVEL_1_2!r} so far (got {level!r}); Level 2->3 / 3->4 "
            "land with G3f/G3g."
        )

    is_subsection_level = (level == LEVEL_1_2)
    if is_subsection_level:
        checkable = _checkable_subsections(mech or {})
    else:
        checkable = _checkable_placements((mech or {}).get("placements"))
    if not checkable:
        return {"valid": True, "violations": []} if not is_subsection_level else \
            {"valid": True, "violations": [], "footprints": {}}

    agent_name = "mech_validator"
    node_word = "subsection" if is_subsection_level else "part"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Mech Validator — Level {level} ({len(checkable)} {node_word}(s))"})
    started = time.monotonic()

    try:
        payload = _build_subsection_payload(checkable) if is_subsection_level else _build_payload(checkable)
        result = _run_batch(payload, session_id=session_id)
        violations = result.get("violations") or []
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": f"validator unavailable: {exc}", "duration_ms": duration_ms})
        error_result = {"valid": True, "violations": [], "validator_error": str(exc)}
        if is_subsection_level:
            error_result["footprints"] = {}
        return error_result

    duration_ms = int((time.monotonic() - started) * 1000)
    summary = "no violations" if not violations else f"{len(violations)} violation(s)"
    emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
               payload={"summary": summary, "duration_ms": duration_ms})
    output = {"valid": not violations, "violations": violations}
    if is_subsection_level:
        output["footprints"] = result.get("footprints") or {}
    return output


if __name__ == "__main__":
    _demo_mech = {
        "placements": [
            {"part_id": "motor_1", "w": 28, "h": 19, "d": 19, "dimension_confidence": "verified",
             "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 28, "h": 19, "d": 19},
                              "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "cylinder", "color_role": "primary"}]},
        ],
    }
    try:
        print(json.dumps(validate_layout(_demo_mech, LEVEL_0_1), indent=2))

        _demo_subsection_mech = {
            "placements": [
                {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5,
                 "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 30, "h": 20, "d": 5},
                                  "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box", "color_role": "primary"}]},
                {"part_id": "mount_mcu_1", "x": 0, "y": 20, "z": 0, "w": 30, "h": 5, "d": 5,
                 "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 30, "h": 5, "d": 5},
                                  "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box", "color_role": "accent"}]},
            ],
        }
        print(json.dumps(validate_layout(_demo_subsection_mech, LEVEL_1_2), indent=2))
    finally:
        close_session()
