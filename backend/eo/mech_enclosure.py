"""
eo/mech_enclosure.py — Phase 1, Patch 1.2 of the Mech/Enclosure
implementation guide: the pure, deterministic housing-sizing function
that replaces agents/hardware_speccer.py's current guessed housing/lid
dimensions ("The housing's placement should span the full enclosure
footprint... the lid matches the housing -- same x/y footprint",
SYSTEM_PROMPT) with a computed value derived from what's actually
packed inside the device.

Same build-order reasoning every earlier "pure function first" patch
in this tree already established (eo/mech_device.py's own
plan_device_layout() before apply_device_merge(), eo/mech_sections.py's
group_into_sections() before its *_pool.py sibling): land the
mechanical, side-effect-free sizing logic on its own, testable with
plain dict inputs, before Patch 1.3 wires it into the pipeline's
mutate-in-place convention and Patch 1.4 stops the LLM from being asked
to size the enclosure at all.

Input: `device_footprint`, the same shape eo/mech_device.py's
plan_device_layout() already returns as its own `footprint` key --
{"x","y","z","w","h","d"}, the union bounding box of every zoned
section's post-translation footprint (see that module's own
docstring). This module never computes that bounding box itself; it
only takes it as ground truth and expands it, same "reads a footprint,
doesn't invent one" boundary eo/mech_device.py's own docstring draws
around the Enclosure section's footprint.

Sizing rule (Master Guide's own worked description, Phase 1 "Design"
section):
    housing_outer = device_footprint expanded by (wall_thickness +
                    clearance) on x/y/z
    housing_inner = device_footprint expanded by clearance only
    lid           = same x/y footprint as housing, z = housing's own d

"Expanded by N" means padded N on every side: the returned box's
x/y/z origin moves back by N and its w/h/d each grow by 2*N, so the
device_footprint stays exactly centered inside the result -- same
"pad symmetrically around a fixed footprint" shape eo/mech_validator.py's
own confidence-aware tolerance buffer already uses one level down, just
applied to a whole housing instead of one part.

`inner` is the cavity's own footprint (device_footprint plus
clearance_mm of air on every side, per ENCLOSURE_SPEC) -- what a part
placed anywhere inside it is guaranteed clear of the housing's inner
wall face. `outer` is `inner` plus another wall_thickness_mm of actual
shell material -- the real print boundary. `lid` shares outer's x/y/w/h
(same footprint in plan view, per the Master Guide's own phrasing) and
sits on top of it: lid.z = outer.z + outer.d, a slightly more literal
reading of "z = housing's own d" than a bare offset would give, since
it still stacks the lid correctly even when device_footprint's own z
isn't 0 (matching eo/mech_device.py's own docstring: "h", not "d", is
the in-plane axis -- "d" is left as the vertical stacking axis the lid
sits on top of via its own z). lid.d (the lid's own shell thickness) is
wall_thickness_mm -- the same shell thickness as the housing walls,
absent a separate ENCLOSURE_SPEC field for it.

Deliberately does NOT read eo/mech_device.py, eo/mech_sections.py, or
`mech` at all -- a pure dict-in/dict-out function, no mutation, no I/O,
same "this package never imports agents/, and a -1-suffix module never
reaches past its own inputs" precedent every earlier pure-planning
function in this tree already holds itself to. Patch 1.3's
apply_enclosure_generation() is what actually reads `mech["device"]`'s
footprint and calls this.
"""

from eo.enclosure_spec import ENCLOSURE_SPEC


def _expand(footprint: dict, pad: float) -> dict:
    """Pads `footprint` by `pad` on every side of x/y/z -- see module
    docstring's "Expanded by N" note. Internal helper only; `inner` and
    `outer` are both this same operation at two different pad amounts,
    so the padding math itself lives in exactly one place.
    """
    x = float(footprint.get("x") or 0)
    y = float(footprint.get("y") or 0)
    z = float(footprint.get("z") or 0)
    w = float(footprint.get("w") or 0)
    h = float(footprint.get("h") or 0)
    d = float(footprint.get("d") or 0)
    return {
        "x": round(x - pad, 3), "y": round(y - pad, 3), "z": round(z - pad, 3),
        "w": round(w + 2 * pad, 3), "h": round(h + 2 * pad, 3), "d": round(d + 2 * pad, 3),
    }


def compute_housing_footprint(device_footprint: dict) -> dict:
    """Returns {"outer": {...}, "inner": {...}, "lid": {...}}, each an
    {"x","y","z","w","h","d"} dict -- see module docstring for the
    sizing rule and the reasoning behind each of the three.

    Missing x/y/z/w/h/d keys on `device_footprint` default to 0 (same
    "tolerant of a partial dict" posture eo/mech_validator.py's own
    _build_payload() already takes toward missing dims), so this never
    raises on a still-incomplete footprint -- it just returns a
    correspondingly degenerate (but still well-shaped) result.

    Pure function: never mutates `device_footprint`, never touches
    `mech`, never does I/O. Two calls with the same input always
    return the same output -- the "idempotent by construction" property
    Patch 1.5's own idempotency test checks for at the pipeline level
    depends on this holding true here first.
    """
    clearance = ENCLOSURE_SPEC["clearance_mm"]
    wall = ENCLOSURE_SPEC["wall_thickness_mm"]

    inner = _expand(device_footprint, clearance)
    outer = _expand(device_footprint, wall + clearance)

    lid = {
        "x": outer["x"], "y": outer["y"], "w": outer["w"], "h": outer["h"],
        "z": round(outer["z"] + outer["d"], 3),
        "d": wall,
    }

    return {"outer": outer, "inner": inner, "lid": lid}
