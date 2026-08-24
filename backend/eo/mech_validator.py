"""
eo/mech_validator.py — G3c (Master Guide, "G3/G4. Hierarchical parallel
build + validate"): the FreeCAD half of the generate->validate pattern.
Runs headless FreeCAD (real solid geometry, via its Python scripting API,
not axis-aligned arithmetic) against a level's proposed nodes and reports
containment/collision violations without ever modifying the layout.

Scope through G3g: Level 0->1 ("does every primitive a part is composed
of actually stay inside that part's own w/h/d bounding box, once its
declared offset/rotation is really applied"), Level 1->2 ("does a
subsection's part collide with its own mount, once both are placed at
their real absolute positions, and what's the subsection's combined
footprint"), Level 2->3 ("do two DIFFERENT subsections inside the same
section collide with each other, once both are placed at their real
absolute positions, and what's the section's combined footprint"), AND
Level 3->4 ("does every non-Enclosure section, positioned by eo/
mech_device.py's deterministic front/center/edge merge, actually stay
inside the Enclosure section's own validated footprint, and do two
DIFFERENT sections collide with each other"). Level 3->4 is the LAST
level in the tree (Master Guide: "closes out the tree") -- there is no
further `level` value validate_layout() below will ever grow; it still
rejects any level it doesn't implement rather than silently no-op'ing,
so a premature or mistyped caller fails loudly instead of shipping an
unchecked layout that looks validated.

Level 3->4 specifics (G3g, second half): one level up from Level 2->3's
"two subsections in the same section" check, but a different SHAPE, not
just the same machinery regrouped again -- Level 2->3 only ever asked
"do these two things collide," Level 3->4 additionally asks "does this
one thing stay inside that OTHER, fixed thing" (global containment),
since eo/mech_device.py's own front/center/edge merge (G3g, first half)
positions sections relative to a real container (the Enclosure section's
own footprint) that Level 0->1/1->2/2->3 never had an equivalent of --
those levels only ever checked members/subsections against EACH OTHER,
never against a container. The containment half reuses the same
"cut a solid against a bounds box, whatever's left over is a violation"
shape Level 0->1's own _check_part() already established (there, a
primitive cut against its part's own w/h/d box; here, a whole section's
unioned geometry cut against the Enclosure section's own footprint box,
same confidence-aware tolerance buffer). The collision half reuses Level
2->3's own cross-pair shape one level up again: two DIFFERENT sections'
unioned geometry boolean-intersected, never a section's own subsections
against each other (Level 2->3 already settled that). The Enclosure
section itself is never a checkable node at this level -- it's the fixed
container every other section is checked against and inside of, mirroring
eo/mech_device.py's own "container section is never zoned/translated"
rule exactly (see that module's docstring). A section reports its own
footprint (the union bounding box of every member of every subsection in
the section, same shape Level 2->3's own footprint already is) regardless
of whether it violated, since eo/mech_repair.py's Level 3->4 driver
(G3g, second half, bottom of that module) needs one for every section it
ships, not just the clean ones -- same "G3f needs it, G3g needs it,
whoever's next needs it" reasoning every earlier level's footprint output
already follows, even though Level 3->4 is the last level and nothing
inside THIS tree consumes it further (G3i's pipeline wiring and G3j's
frontend badge, both outside this tree, are the actual consumers now).

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

Level 2->3 specifics (G3f-2): one level up from Level 1->2's "part vs its
own mount" check, but the SAME machinery, just regrouped -- a section's
checkable unit is no longer an individual member, it's a whole subsection
(every member the subsection already owns, built at each member's own
absolute x/y/z, exactly as Level 1->2 built them). Two members of the
SAME subsection are never boolean-intersected against each other here --
Level 1->2 already settled that collision, and re-checking it at Level
2->3 would just report the same violation twice under a different node
id. Only cross-subsection pairs are intersected: does Subsection A's
geometry (all its members, unioned) overlap Subsection B's geometry,
anywhere in the section. A section with only one subsection has nothing
to collide with and only ever contributes a footprint, mirroring Level
1->2's own singleton case exactly. Every checkable section reports its
own aggregate bounding footprint (the union bounding box of every member
of every subsection in the section, in absolute space) regardless of
whether it collided, since eo/mech_repair.py's Level 2->3 driver (G3f-2)
needs a footprint for every section it ships, not just the clean ones,
to hand off to G3g.

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
import math
import os
import sys
import threading
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_code_interpreter import Sandbox

from eo.mech_subsections import group_into_subsections, members_for_subsection
from relay.emitter import emit_event

# Level vocabulary matches the arrow notation the Master Guide and this
# codebase's own docstrings already use ("Level 0->1" in agents/
# mech_primitive_pool.py's module docstring, agents/hardware_speccer.py's
# G3a comments, eo/registry.py) -- no new naming invented here.
LEVEL_0_1 = "0->1"
LEVEL_1_2 = "1->2"
LEVEL_2_3 = "2->3"
LEVEL_3_4 = "3->4"
_IMPLEMENTED_LEVELS = {LEVEL_0_1, LEVEL_1_2, LEVEL_2_3, LEVEL_3_4}

# The one section id Level 3->4 never treats as a checkable node -- it's
# the fixed container every other section is checked against and inside
# of. Matches eo/mech_sections.py's own _SECTION_ORDER (its fifth and
# last value) and eo/mech_device.py's own _CONTAINER_SECTION_ID exactly --
# not a value this module invents independently.
_DEVICE_CONTAINER_SECTION_ID = "Enclosure"

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


def _check_section(section):
    # Level 2->3's per-section check: same shape as _check_subsection()
    # above, one level up -- the only real difference is WHAT gets
    # grouped into a "member" for the cross-pair collision loop. There,
    # a member was one individual part/mount placement; here, a member
    # is a whole SUBSECTION (every one of its own members' primitives,
    # unioned into one solid list, each still built at its own absolute
    # x/y/z exactly as Level 1->2 built them). Intra-subsection pairs are
    # never compared -- Level 1->2 already validated those; only
    # cross-subsection pairs are, so a part/mount collision already
    # flagged and fixed one level down never resurfaces here as a
    # different-looking violation on the same geometry.
    #
    # Returns (collision_mm3, footprint | None), same contract as
    # _check_subsection() -- footprint is None only when every member of
    # every subsection failed to build (degenerate input), handled by
    # the caller in main() below exactly like the Level 1->2 case.
    subsections = section.get("subsections") or []

    subsection_solids = []  # one list of solids per subsection, in subsection order
    bounds = None  # [xmin, ymin, zmin, xmax, ymax, zmax]

    for subsection in subsections:
        solids = []
        for member in subsection.get("members") or []:
            base = {"x": member.get("x") or 0, "y": member.get("y") or 0, "z": member.get("z") or 0}
            for prim in member.get("primitives") or []:
                try:
                    solid = _build_primitive_shape(prim, base=base)
                except Exception:
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
        subsection_solids.append(solids)

    collision_mm3 = 0.0
    for i in range(len(subsection_solids)):
        for j in range(i + 1, len(subsection_solids)):
            for si in subsection_solids[i]:
                for sj in subsection_solids[j]:
                    try:
                        overlap = si.common(sj)
                        collision_mm3 += overlap.Volume
                    except Exception:
                        # Same "degenerate input is a real violation, not
                        # a silent skip" posture _check_subsection() uses.
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


def _check_device_section(section, container_shape):
    # Level 3->4's per-section check (G3g, second half): unions every
    # member of every subsection in the section into ONE flat solid list
    # (no intra-section cross-pair loop here -- Level 2->3 already
    # settled every cross-subsection collision inside this section; see
    # module docstring) built at each member's own real absolute x/y/z,
    # exactly as Level 2->3 built them -- eo/mech_device.py's own
    # apply_device_merge() already moved those x/y/z to their real
    # front/center/edge positions before this ever runs, so "real
    # absolute" here already means "post-merge."
    #
    # Returns (solids, footprint | None, leftover_mm3) -- `solids` is
    # handed back to main() below for the SEPARATE cross-section
    # collision pass (every section's solids checked against every OTHER
    # section's, one level up from Level 2->3's own cross-subsection
    # loop); `footprint` is None only when every member's primitives
    # failed to build (degenerate input, same "flag, never silently
    # drop" posture every earlier level already uses); `leftover_mm3` is
    # this section's own total containment violation volume against
    # `container_shape` (already includes the confidence-aware tolerance
    # buffer -- see _build_bounds_shape() and _check_part() above, the
    # same shape this reuses one level up).
    solids = []
    bounds = None

    for subsection in section.get("subsections") or []:
        for member in subsection.get("members") or []:
            base = {"x": member.get("x") or 0, "y": member.get("y") or 0, "z": member.get("z") or 0}
            for prim in member.get("primitives") or []:
                try:
                    solid = _build_primitive_shape(prim, base=base)
                except Exception:
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

    if bounds is None:
        return solids, None, 0.0

    footprint = {
        "x": round(bounds[0], 3), "y": round(bounds[1], 3), "z": round(bounds[2], 3),
        "w": round(bounds[3] - bounds[0], 3),
        "h": round(bounds[4] - bounds[1], 3),
        "d": round(bounds[5] - bounds[2], 3),
    }

    leftover_mm3 = 0.0
    for solid in solids:
        try:
            leftover = solid.cut(container_shape)
            leftover_mm3 += leftover.Volume
        except Exception:
            # Same "degenerate input is a real violation, not a silent
            # skip" posture every earlier per-solid check in this script
            # already uses.
            leftover_mm3 += 1.0

    return solids, footprint, leftover_mm3


def main():
    input_path, output_path = sys.argv[1], sys.argv[2]
    with open(input_path) as f:
        payload = json.load(f)

    level = payload.get("level")
    violations = []

    if level == %(level_3_4)r:
        container = payload.get("container") or {}
        cw = max(float(container.get("w") or 0), 0.01)
        ch = max(float(container.get("h") or 0), 0.01)
        cd = max(float(container.get("d") or 0), 0.01)
        ctol = float(container.get("tolerance_mm") or 0)
        container_shape = _build_bounds_shape(cw, ch, cd, ctol)
        container_shape.translate(FreeCAD.Vector(
            float(container.get("x") or 0),
            float(container.get("y") or 0),
            float(container.get("z") or 0),
        ))

        footprints = {}
        section_solids = {}
        containment_by_section = {}
        section_order = []
        for section in payload.get("sections") or []:
            section_id = section.get("section_id")
            section_order.append(section_id)
            try:
                solids, footprint, leftover_mm3 = _check_device_section(section, container_shape)
            except Exception as exc:
                violations.append({
                    "node_id": section_id,
                    "issue": f"geometry check failed: {exc}",
                })
                continue
            if footprint is not None:
                footprints[section_id] = footprint
            section_solids[section_id] = solids
            containment_by_section[section_id] = leftover_mm3

        # Cross-section collision: same "two different X's, boolean-
        # intersected, never the same X against itself" shape Level
        # 2->3's own subsection-pair loop already uses, one level up --
        # every DIFFERENT section pair, never a section's own
        # subsections against each other (Level 2->3 already settled
        # that; see module docstring).
        collision_by_section = {}
        checked_ids = [sid for sid in section_order if sid in section_solids]
        for i in range(len(checked_ids)):
            for j in range(i + 1, len(checked_ids)):
                a, b = checked_ids[i], checked_ids[j]
                pair_mm3 = 0.0
                for sa in section_solids[a]:
                    for sb in section_solids[b]:
                        try:
                            overlap = sa.common(sb)
                            pair_mm3 += overlap.Volume
                        except Exception:
                            pair_mm3 += 1.0
                if pair_mm3 > %(volume_epsilon)r:
                    collision_by_section[a] = collision_by_section.get(a, 0.0) + pair_mm3
                    collision_by_section[b] = collision_by_section.get(b, 0.0) + pair_mm3

        for section_id in checked_ids:
            issues = []
            leftover_mm3 = containment_by_section.get(section_id, 0.0)
            if leftover_mm3 > %(volume_epsilon)r:
                issues.append(
                    "extends outside the Enclosure section's own footprint "
                    f"(~{round(leftover_mm3, 2)} mm^3 outside)"
                )
            collide_mm3 = collision_by_section.get(section_id, 0.0)
            if collide_mm3 > %(volume_epsilon)r:
                issues.append(
                    "collides with another section "
                    f"(~{round(collide_mm3, 2)} mm^3 overlap)"
                )
            if issues:
                violations.append({"node_id": section_id, "issue": "; ".join(issues)})

        with open(output_path, "w") as f:
            json.dump({"violations": violations, "footprints": footprints}, f)
        return

    if level == %(level_2_3)r:
        footprints = {}
        for section in payload.get("sections") or []:
            section_id = section.get("section_id")
            try:
                collision_mm3, footprint = _check_section(section)
            except Exception as exc:
                violations.append({
                    "node_id": section_id,
                    "issue": f"geometry check failed: {exc}",
                })
                continue
            if footprint is not None:
                footprints[section_id] = footprint
            if collision_mm3 > %(volume_epsilon)r:
                violations.append({
                    "node_id": section_id,
                    "issue": (
                        "subsections collide "
                        f"(~{round(collision_mm3, 2)} mm^3 overlap)"
                    ),
                })
        with open(output_path, "w") as f:
            json.dump({"violations": violations, "footprints": footprints}, f)
        return

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
""" % {"volume_epsilon": _VOLUME_EPSILON_MM3, "level_1_2": LEVEL_1_2, "level_2_3": LEVEL_2_3,
       "level_3_4": LEVEL_3_4}


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


def _checkable_sections(mech: dict, parts: list) -> list:
    """Level 2->3's scope (G3f-2): every section eo/mech_sections.py's
    group_into_sections() reports, resolved down to just the subsections
    (via that module's own subsections_for_section()) that
    _checkable_subsections()'s own per-subsection filtering already
    considers checkable -- same "skip, don't flag, a subsection nothing's
    composed yet" posture that helper already holds, reused wholesale
    rather than re-implemented here.

    `parts` is required (not optional) -- eo/mech_sections.py's
    group_into_sections() can't regroup subsections by functional
    category without it (see that module's own docstring on why `parts`
    is always caller-supplied). A caller with no `parts` on hand gets an
    empty list back (nothing checkable) rather than a crash, same
    fail-safe posture as every other "not ready yet" branch in this
    module -- see validate_layout()'s own handling of an empty
    `checkable` list.

    Returns [{"section_id": str, "subsections": [{"subsection_id": str,
    "members": [placement, ...]}, ...]}, ...] -- a section is included
    only if it has at least one checkable subsection; a subsection with
    zero composed members is silently dropped from its section's list
    rather than included empty, same reasoning _checkable_subsections()
    already applies one level down.
    """
    if not parts:
        return []

    from eo.mech_sections import group_into_sections, subsections_for_section

    checkable_subsections_by_id = {
        s.get("subsection_id"): s for s in _checkable_subsections(mech)
    }

    checkable = []
    for section in group_into_sections(mech, parts):
        section_subsections = []
        for subsection in subsections_for_section(mech, section):
            checkable_subsection = checkable_subsections_by_id.get(subsection.get("subsection_id"))
            if checkable_subsection is not None:
                section_subsections.append(checkable_subsection)
        if section_subsections:
            checkable.append({
                "section_id": section.get("section_id"),
                "subsections": section_subsections,
            })
    return checkable


def _build_section_payload(checkable: list) -> dict:
    return {
        "level": LEVEL_2_3,
        "sections": [
            {
                "section_id": section.get("section_id"),
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
                    for s in section.get("subsections") or []
                ],
            }
            for section in checkable
        ],
    }


def _checkable_device_sections(mech: dict, parts: list) -> tuple:
    """Level 3->4's scope (G3g, second half): the container (the
    Enclosure section's own validated `footprint`, set by eo/
    mech_repair.py's run_level_2_3_repair() -- G3f-2) plus every OTHER
    section _checkable_sections() above already considers checkable --
    reused wholesale rather than re-implementing Level 2->3's own
    subsection-checkability filtering a third time; the only new work
    here is finding the container and excluding it from the checkable
    set (see _DEVICE_CONTAINER_SECTION_ID above).

    Returns (container, checkable):
      - container: {"x","y","z","w","h","d"} (the Enclosure section's
        own footprint) or None if it isn't in `mech["sections"]` yet, or
        is there without a `footprint` key yet (run_level_2_3_repair()
        hasn't reached it) -- "nothing to check against yet," same
        fail-safe posture eo/mech_device.py's own _container_footprint()
        already holds for the identical reason (this module deliberately
        mirrors that function's logic rather than importing it -- see
        this module's own dependency-shape note on why eo/mech_validator.py
        doesn't import eo/mech_device.py: this module has no other reason
        to depend on the device-merge module, and duplicating one small
        lookup is cheaper than adding that whole import for it).
      - checkable: _checkable_sections()'s own return shape, minus the
        Enclosure entry itself -- the container is never a checkable NODE
        at this level, matching eo/mech_device.py's own "container
        section is never zoned/translated" rule.

    `parts` is required for the same reason _checkable_sections() itself
    requires it (Level 2->3/3->4's section grouping is a category lookup
    only `parts` can answer) -- absent, this returns (None, []), same
    fail-safe posture as every other "not ready yet" branch in this
    module.
    """
    if not parts:
        return None, []

    container = None
    for section in (mech or {}).get("sections") or []:
        if isinstance(section, dict) and section.get("section_id") == _DEVICE_CONTAINER_SECTION_ID:
            footprint = section.get("footprint")
            container = footprint if isinstance(footprint, dict) else None
            break
    if container is None:
        return None, []

    checkable = [
        s for s in _checkable_sections(mech, parts)
        if s.get("section_id") != _DEVICE_CONTAINER_SECTION_ID
    ]
    return container, checkable


def _build_device_payload(checkable: list, container: dict) -> dict:
    return {
        "level": LEVEL_3_4,
        "container": {
            "x": container.get("x") or 0,
            "y": container.get("y") or 0,
            "z": container.get("z") or 0,
            "w": container.get("w") or 0,
            "h": container.get("h") or 0,
            "d": container.get("d") or 0,
            # The Enclosure section's own w/h/d aren't a single part with
            # a `dimension_confidence` of their own to read -- same
            # "no more specific value wired, fall back to the
            # conservative default" reasoning _tolerance_for() already
            # applies for an unwired placement, reused here rather than
            # inventing a second default.
            "tolerance_mm": _tolerance_for(_DEFAULT_CONFIDENCE),
        },
        "sections": [
            {
                "section_id": section.get("section_id"),
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
                    for s in section.get("subsections") or []
                ],
            }
            for section in checkable
        ],
    }


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
                     path: str = None, domain: str = None, parts: list = None) -> dict:
    """Runs headless FreeCAD against one tree level's proposed nodes.
    Implements level=LEVEL_0_1 ("0->1": does every primitive a part is
    composed of stay inside that part's own w/h/d bounding box, once its
    real offset/rotation is applied), level=LEVEL_1_2 ("1->2": does a
    subsection's part collide with its own mount), level=LEVEL_2_3
    ("2->3": do two different subsections in the same section collide
    with each other), and level=LEVEL_3_4 ("3->4": does every non-
    Enclosure section stay inside the Enclosure section's own footprint,
    and do two different sections collide with each other) -- the last
    level in the tree. Calling with any other `level` raises
    NotImplementedError rather than silently reporting `valid: True` for
    a level nothing actually checked.

    `parts` is required for level=LEVEL_2_3 and level=LEVEL_3_4 (see
    _checkable_sections()'s own docstring on why -- Level 2->3/3->4's
    section grouping is a category lookup only `parts` can answer) and
    ignored for every other level. Omitting it at either level is treated
    the same as "nothing checkable yet," not an error -- same fail-safe
    posture as everything else in this module -- so a caller mid-migration
    that hasn't wired `parts` through yet degrades to a no-op rather than
    crashing.

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
    it to group by next). Level 2->3 (LEVEL_2_3) returns the same shape
    one level up, {section_id: {"x","y","z","w","h","d"}}, for the same
    reason (G3g needs it next). Level 3->4 (LEVEL_3_4) returns the same
    shape again, one more time (G3i's pipeline wiring and G3j's frontend
    badge, both outside this tree, are the consumers now that this is the
    last level). Absent (empty dict) on the no-op and fail-open paths
    below, same "nothing was actually computed" reasoning as `violations`
    being [] on those same paths.
    """
    if level not in _IMPLEMENTED_LEVELS:
        raise NotImplementedError(
            f"eo/mech_validator.py only implements level={LEVEL_0_1!r}, "
            f"level={LEVEL_1_2!r}, level={LEVEL_2_3!r}, and level="
            f"{LEVEL_3_4!r} (got {level!r})."
        )

    is_section_level = (level == LEVEL_2_3)
    is_subsection_level = (level == LEVEL_1_2)
    is_device_level = (level == LEVEL_3_4)
    is_grouped_level = is_subsection_level or is_section_level or is_device_level
    container = None
    if is_section_level:
        checkable = _checkable_sections(mech or {}, parts)
    elif is_subsection_level:
        checkable = _checkable_subsections(mech or {})
    elif is_device_level:
        container, checkable = _checkable_device_sections(mech or {}, parts)
    else:
        checkable = _checkable_placements((mech or {}).get("placements"))
    if not checkable:
        return {"valid": True, "violations": []} if not is_grouped_level else \
            {"valid": True, "violations": [], "footprints": {}}

    agent_name = "mech_validator"
    node_word = ("device section" if is_device_level else
                 "section" if is_section_level else
                 "subsection" if is_subsection_level else "part")
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Mech Validator — Level {level} ({len(checkable)} {node_word}(s))"})
    started = time.monotonic()

    try:
        if is_section_level:
            payload = _build_section_payload(checkable)
        elif is_subsection_level:
            payload = _build_subsection_payload(checkable)
        elif is_device_level:
            payload = _build_device_payload(checkable, container)
        else:
            payload = _build_payload(checkable)
        result = _run_batch(payload, session_id=session_id)
        violations = result.get("violations") or []
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": f"validator unavailable: {exc}", "duration_ms": duration_ms})
        error_result = {"valid": True, "violations": [], "validator_error": str(exc)}
        if is_grouped_level:
            error_result["footprints"] = {}
        return error_result

    duration_ms = int((time.monotonic() - started) * 1000)
    summary = "no violations" if not violations else f"{len(violations)} violation(s)"
    emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
               payload={"summary": summary, "duration_ms": duration_ms})
    output = {"valid": not violations, "violations": violations}
    if is_grouped_level:
        output["footprints"] = result.get("footprints") or {}
    return output


def find_unresolved_inferred_pins(mech: dict) -> list:
    """
    Patch 3.1 (Phase 3, Master Guide gap #10, "Pin resolution gate") --
    pure scan, no mutation, no I/O, no FreeCAD: finds every wiring edge
    agents/hardware_speccer.py's _fix_wiring_electrical_integrity()
    synthesized (tagged "_inferred": True, see that function's own
    docstring for the two shapes it currently produces: a missing I2C
    clock line, an orphaned power-tree input) whose still-null pin
    belongs to a part that's actually part of the FINAL device -- present
    in `mech["mech"]["sections"]` (populated by eo/mech_sections.py's
    apply_section_grouping(), already run by the time run_level_3_4_repair()
    settles -- see eo/mech_repair.py's run_level_3_4_repair() docstring)
    -- not just any part still sitting in `mech["mech"]["placements"]`
    that never made it into a checkable section (a discarded/unused
    part, or one G3f-1's own category grouping dropped). Detection only,
    same "can we find the problem" vs. "what do we do about it" split
    every other level's own checkable-set helper in this module already
    keeps separate from its repair driver.

    NOTE on the `mech` argument's shape: agents/hardware_speccer.py's own
    run_hardware_speccer() is the only caller with both wiring edges
    (`spec["wiring"]`) and section membership (`spec["mech"]["sections"]`)
    on hand at once -- those two live as SIBLINGS on its `spec` dict, not
    both nested under one `spec["mech"]` -- so it passes its own `spec`
    object here as `mech`, matching this module's existing precedent of a
    caller-shaped `mech` argument rather than one fixed schema (see
    validate_layout()'s own `mech` argument, which is sometimes the bare
    placements dict and sometimes a fuller object depending on level/
    caller). `mech.get("wiring")` and `mech.get("mech", {}).get("sections")`
    are read directly rather than assuming a flattened shape.

    Returns [{"part_id": str, "pin_side": "from"|"to", "pin_hint": str
    or None, "edge": edge_dict}, ...] -- one entry per still-unresolved
    (null-pin) side of a load-bearing inferred edge, `edge` being the
    SAME dict object living in `mech["wiring"]["edges"]` (so a caller,
    e.g. Patch 3.3's finalize-path wiring, can fill the resolved pin name
    straight back onto it without a second lookup). `pin_hint` is the
    OTHER side's already-known pin name (e.g. "SCL", "VIN") when this
    edge's own shape provides one -- a hint for Patch 3.2's targeted
    retry to know what it's resolving, not a value this function
    invents. An inferred edge with both sides already named (shouldn't
    happen given today's two synthesis cases -- both always leave
    exactly one side null -- but a future third synthesis case might
    not) contributes nothing: there's no unresolved *pin* left to chase
    even though the edge itself is still "_inferred".

    Empty input (no wiring, no edges, no sections yet -- i.e. called
    before the mech pipeline has actually placed anything) returns []
    cleanly, no error, same fail-safe posture every other "not ready
    yet" branch in this module already holds.
    """
    if not isinstance(mech, dict):
        return []

    wiring = mech.get("wiring")
    edges = wiring.get("edges") if isinstance(wiring, dict) else None
    if not isinstance(edges, list) or not edges:
        return []

    device_mech = mech.get("mech")
    device_mech = device_mech if isinstance(device_mech, dict) else {}
    sections = device_mech.get("sections") or []
    if not sections:
        return []  # nothing placed in the final device yet -- nothing is "load-bearing"

    from eo.mech_sections import subsections_for_section

    placed_part_ids = set()
    for section in sections:
        if not isinstance(section, dict):
            continue
        for subsection in subsections_for_section(device_mech, section):
            for member_id in (subsection or {}).get("member_ids") or []:
                placed_part_ids.add(member_id)

    if not placed_part_ids:
        return []

    unresolved = []
    for edge in edges:
        if not isinstance(edge, dict) or not edge.get("_inferred"):
            continue
        from_id, to_id = edge.get("from"), edge.get("to")
        from_pin, to_pin = edge.get("from_pin"), edge.get("to_pin")
        if not from_pin and from_id in placed_part_ids:
            unresolved.append({
                "part_id": from_id, "pin_side": "from",
                "pin_hint": to_pin, "edge": edge,
            })
        if not to_pin and to_id in placed_part_ids:
            unresolved.append({
                "part_id": to_id, "pin_side": "to",
                "pin_hint": from_pin, "edge": edge,
            })
    return unresolved


# Mobility types this module's own C.4 balance check runs for -- every
# OTHER mobility_type (static/handheld/wearable/flying) skips the check
# entirely, per Patch C.4's own wording ("runs only when
# mech['archetype']['mobility_type'] in {'wheeled', 'legged'} -- every
# other archetype skips this entirely"). A device that never touches
# the ground on wheels/legs (or is carried/held) has no ground-contact
# support polygon to balance over in the first place.
_BALANCE_CHECKED_MOBILITY_TYPES = {"wheeled", "legged"}

# How far inside the support polygon the center of gravity must land,
# not just technically inside it -- same "a boundary case is still a
# real-world tip-over risk, not a pass" reasoning
# eo/mech_manufacturability.py's own wall-clearance checks already
# apply via ENCLOSURE_SPEC["min_feature_mm"] for a physical margin
# rather than a bare zero-clearance test. Kept as a small, clearly-
# labeled module constant (not pulled from ENCLOSURE_SPEC, which has
# no balance-specific entry) rather than a magic number inline.
BALANCE_MARGIN_MM = 5.0


def _cog_clearance_mm(cog: dict, support_polygon: list) -> float:
    """The minimum perpendicular distance from `cog`'s own (x, y) to
    every edge of `support_polygon` (eo/mech_balance.py's own
    compute_support_polygon() output, a counter-clockwise-ordered convex
    hull) -- POSITIVE when `cog` sits inside the hull (the usual "point
    is to the left of every edge" convex-polygon interior test),
    NEGATIVE once `cog` has crossed outside any single edge. The
    signed minimum (not the unsigned minimum) is what Patch C.4's own
    "how far outside" violation detail (below, in check_balance())
    needs -- an unsigned distance would report the same small number
    whether the CoG is barely inside or barely outside, discarding
    exactly the information a repair step (Patch C.5, not this patch)
    would need to know which direction to push weight.

    A degenerate `support_polygon` (fewer than 3 points -- a single
    ground-contact point, or two collinear ones, has no real interior
    region at all) returns a large negative sentinel rather than
    computing a meaningless edge-distance, so a caller always treats
    "not enough ground-contact points to form a real support base" as
    a clear failure rather than an accidental pass.
    """
    if len(support_polygon) < 3:
        return -math.inf

    cog_point = (float(cog.get("x") or 0), float(cog.get("y") or 0))
    min_clearance = math.inf

    n = len(support_polygon)
    for i in range(n):
        a = support_polygon[i]
        b = support_polygon[(i + 1) % n]
        edge_dx = float(b["x"]) - float(a["x"])
        edge_dy = float(b["y"]) - float(a["y"])
        edge_len = math.hypot(edge_dx, edge_dy)
        if edge_len == 0:
            continue
        point_dx = cog_point[0] - float(a["x"])
        point_dy = cog_point[1] - float(a["y"])
        # Signed area (cross product) of the edge vs. the vector from
        # the edge's own start to the CoG, divided by edge length --
        # the perpendicular signed distance, positive on the polygon's
        # own "inside" side for a CCW-ordered hull.
        signed_area = edge_dx * point_dy - edge_dy * point_dx
        min_clearance = min(min_clearance, signed_area / edge_len)

    return min_clearance


def check_balance(mech: dict, parts: list) -> dict:
    """Patch C.4 (Phase C, Mech View standalone implementation guide) --
    pure scan, no mutation, no I/O, no FreeCAD: verifies eo/mech_balance.py's
    own Patch C.3 center-of-gravity (compute_cog()) projects inside the
    same module's Patch C.4 support polygon (compute_support_polygon())
    with at least `BALANCE_MARGIN_MM` of clearance on every side -- Part
    1's own gap #3 ("nothing checks whether a mobile device
    (wheeled/legged) would actually balance").

    Gated EXCLUSIVELY on `mech["archetype"]["mobility_type"]` (missing
    archetype reads back as the pipeline's own "full"/"static" default,
    same missing-archetype convention every other archetype-gated call
    site in this tree already uses -- see eo/mech_cutouts.py's own
    Patch A.5 gate) -- literal Patch C.4 wording: "runs only when
    mech['archetype']['mobility_type'] in {'wheeled', 'legged'} --
    every other archetype skips this entirely." A `static`/`handheld`/
    `wearable`/`flying` mech NEVER reaches eo/mech_balance.py at all
    (not even to compute a CoG that then gets discarded) -- the
    deferred import below is the only place either of that module's
    functions gets called from this module, and it sits strictly after
    the gate.

    Returns `{"ok": bool, "skipped": bool, "violations": [...], "cog":
    dict or None, "support_polygon": list}`:
      - `skipped=True` (and `ok=True`, `violations=[]`, `cog=None`,
        `support_polygon=[]`) for every non-wheeled/legged mobility_type
        -- same "the gate itself is the test" posture Patch C.4's own
        "done when" wording asks for ("a static/handheld/wearable/flying
        archetype never triggers this check at all").
      - `skipped=False` otherwise, with `cog`/`support_polygon` always
        populated from the real eo/mech_balance.py computation (even on
        a passing result, so a caller/report can always show the actual
        numbers, not just a boolean).
      - A violation is a single dict tagged `"reason"`, one of:
          "insufficient_ground_contact_points" (fewer than 3 hull
          points -- no real support base to balance over at all) or
          "cog_outside_support_polygon" (CoG failed the margin check),
        the latter carrying `"clearance_mm"` (the signed value from
        `_cog_clearance_mm()` above -- negative once truly outside, so
        the SAME violation shape distinguishes "barely failed the
        margin" from "genuinely off the polygon" without a second
        field) and `"required_margin_mm"`.
      - A `mech` with sections but zero total placed mass (eo/
        mech_balance.py's own compute_cog() "nothing to derive from
        yet" sentinel, `total_mass_g == 0`) is treated the same as
        having insufficient ground-contact points -- there's nothing
        real to check balance against yet either way.

    Pure function: never mutates `mech` or `parts`.
    """
    archetype = (mech or {}).get("archetype") or {}
    mobility_type = archetype.get("mobility_type", "static")

    if mobility_type not in _BALANCE_CHECKED_MOBILITY_TYPES:
        return {"ok": True, "skipped": True, "violations": [],
                "cog": None, "support_polygon": []}

    from eo.mech_balance import compute_cog, compute_support_polygon

    cog = compute_cog(mech, parts)
    support_polygon = compute_support_polygon(mech, parts)

    violations = []

    if cog.get("total_mass_g", 0) <= 0 or len(support_polygon) < 3:
        violations.append({"reason": "insufficient_ground_contact_points"})
        return {"ok": False, "skipped": False, "violations": violations,
                "cog": cog, "support_polygon": support_polygon}

    clearance_mm = round(_cog_clearance_mm(cog, support_polygon), 3)
    if clearance_mm < BALANCE_MARGIN_MM:
        violations.append({
            "reason": "cog_outside_support_polygon",
            "clearance_mm": clearance_mm,
            "required_margin_mm": BALANCE_MARGIN_MM,
        })

    return {
        "ok": not violations,
        "skipped": False,
        "violations": violations,
        "cog": cog,
        "support_polygon": support_polygon,
    }


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

        _demo_section_mech = {
            "placements": [
                {"part_id": "sensor_1", "x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5,
                 "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 15, "h": 10, "d": 5},
                                  "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box", "color_role": "primary"}]},
                {"part_id": "sensor_2", "x": 40, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5,
                 "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 15, "h": 10, "d": 5},
                                  "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box", "color_role": "primary"}]},
            ],
        }
        _demo_section_parts = [
            {"id": "sensor_1", "category": "sensor"},
            {"id": "sensor_2", "category": "sensor"},
        ]
        print(json.dumps(validate_layout(_demo_section_mech, LEVEL_2_3, parts=_demo_section_parts), indent=2))

        _demo_device_mech = {
            "placements": [
                {"part_id": "sensor_1", "x": 200, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5,
                 "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 15, "h": 10, "d": 5},
                                  "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box", "color_role": "primary"}]},
                {"part_id": "battery_1", "x": 5, "y": 5, "z": 0, "w": 20, "h": 10, "d": 10,
                 "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 20, "h": 10, "d": 10},
                                  "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box", "color_role": "primary"}]},
            ],
            "sections": [
                {"section_id": "Enclosure", "footprint": {"x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 33}},
            ],
        }
        _demo_device_parts = [
            {"id": "sensor_1", "category": "sensor"},
            {"id": "battery_1", "category": "power"},
        ]
        print(json.dumps(validate_layout(_demo_device_mech, LEVEL_3_4, parts=_demo_device_parts), indent=2))
    finally:
        close_session()
