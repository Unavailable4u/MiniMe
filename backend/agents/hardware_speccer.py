"""
agents/hardware_speccer.py — MiniMe Blueprint. Proposes a hardware bill-of-
materials, wiring graph, rough physical layout, and phased assembly
instructions for a finished (or in-progress) hardware PRD/feasibility note,
same split and same reasoning as agents/schema_diagrammer.py's own
docstring -- read that module's docstring (and architecture_diagrammer.py's,
which it points to) first if you haven't; this one only documents what's
actually different.

What's different from schema/architecture_diagrammer.py:
  - No Mermaid rendering step. schema/architecture diagrammers exist because
    the model shouldn't write Mermaid syntax directly; here the model's JSON
    output IS the final artifact the four Blueprint sub-views (Parts /
    Wiring / Mech / Instructions) read directly, slice by slice. There's
    still a "model proposes structured data" discipline (ONLY valid JSON,
    fail-safe on parse errors) -- it's just that nothing downstream
    transforms it into another syntax the way _build_schema_mermaid() does.
  - One extra post-processing step schema/architecture diagrammers don't
    have: after the model proposes parts, each part's price is looked up
    via agents/part_price_finder.py's find_price() and merged in, so the
    spec returns with prices already populated on first generation rather
    than requiring a "Refresh prices" click. find_price() returns multiple
    vendor listings per part (BD_VENDOR_DOMAINS is six sites) -- takes
    listings[0], matching api/server.py's existing
    POST /api/workspaces/{ws_id}/parts/refresh-prices endpoint exactly,
    so a part priced here and a part re-priced later pick the same one.
  - Persistence is NOT a standalone bus key. eo/panel_content.py is for
    opaque pasted text (Mind Map, PRD, Schema, etc. -- one `content`
    string, no structure), which doesn't fit four sub-views with their
    own shapes and (for Instructions) per-step mutation. Instead this
    follows the precedent api/server.py's refresh-prices endpoint already
    set: four keys under eo/workspace_facts.py's per-workspace `custom`
    dict -- custom["parts"], custom["wiring"], custom["mech"],
    custom["instructions"] -- written via the same read-modify-write
    shape that endpoint uses (get_facts -> merge custom -> set_facts),
    so a full spec write here and a parts-only write from that endpoint
    never clobber each other's keys.
  - NEW (T2b, step 19a): a fifth key, custom["info"] -- {"summary",
    "tags"} -- written the same read-modify-write way, from a second,
    smaller LLM call (_generate_info()) that reuses the parts/wiring
    JSON already produced rather than re-reading the PRD. Fail-safe:
    always {"summary": "", "tags": []} at minimum, never missing.
  - NEW (T2b, step 19d, optional stretch): custom["info"] now also
    carries "image_url" -- a Pollinations.ai render URL built directly
    off the summary text _generate_info() already produced. No API key,
    no extra network round-trip at generation time: Pollinations renders
    on request, so this is just a URL the frontend's <img> tag hits
    directly (same pattern any other hot-linked image would use). Purely
    cosmetic and deliberately the last/cheapest part of step 19 -- if
    summary is empty (fail-safe path) image_url is "" too, never a URL
    built off no content.
  - NEW (T2c, step 20a): SYSTEM_PROMPT now forbids emitting an enclosure
    as a single lump "parts" entry. The model must instead decompose it
    into discrete parts -- housing + lid shell, one mount per subsystem
    that needs standoff/bracket mounting (MCU, display, any part with
    exposed leads), a realistic fastener count, and (only when the PRD
    says weatherproof/outdoor) a gasket/seal line. Two new categories,
    "3D_PRINT" and "MISC", back this: both are purely mechanical, so
    per the existing wiring-completeness rule above they're never added
    to "wiring.nodes" (same treatment screws/standoffs already got).
    Placement of these new discrete parts in "mech.placements" and
    nesting them into an assembled enclosure is step 20b, not this one --
    this step only changes what shows up in "parts".
  - NEW (T2c, step 20b): SYSTEM_PROMPT's "mech.placements" guidance now
    covers step 20a's new discrete parts explicitly -- housing, lid, and
    each mount each get their own placement entry, with rough nesting
    rules (lid's z sits atop housing's own d; mounts nest inside the
    housing footprint near the subsystem they mount) so MechView.jsx
    renders an assembled enclosure instead of unrelated floating boxes.
    Fasteners are deliberately excluded from "mech.placements" -- too
    numerous/small to place individually. Depends on 20a's part ids
    (housing_1, lid_1, mount_*) existing to reference.
  - NEW (F3, Part 4): what was one generate_text() call is now two.
    Call 1 (SYSTEM_PROMPT_PARTS) proposes "parts" only, each optionally
    carrying a part_number the model fills in when it knows one (e.g.
    "ESP32-WROOM-32"). _populate_dimensions() (new, parallelized like
    _populate_prices()) then looks up real dimensions_mm/datasheet_url
    via agents/component_spec_lookup.py's get_real_spec() for every
    part_number present, merging the result onto that part -- misses
    (no part_number, or neither DigiKey nor Mouser has it) are left
    untouched. Call 2 (SYSTEM_PROMPT_WIRING) then takes that
    (dimension-enriched) parts list as fixed input and produces wiring/
    mech/instructions, told explicitly that a part's "dimensions_mm" is
    ground truth (use it for that part's mech.placements w/h/d, don't
    re-estimate) versus a part with no such field, which still needs
    the model's own estimated sizing exactly as before this change.
    _populate_prices() is unaffected -- still runs once, after both
    calls, over the same final parts list.
  - NEW (F3, Part 5, optional stretch): a fifth custom key,
    custom["datasheets"] -- {part_id: {"title", "content",
    "page_count"}} -- written the same read-modify-write way as
    custom["info"]. _populate_datasheet_details() (new) runs after
    Call 2, Info generation, and pricing are all already done -- last
    on purpose, since it's the most expensive per-part step (a full
    PDF download+parse via agents/component_spec_lookup.py's
    get_datasheet_detail(), which wraps the existing
    agents/pdf_ingestor.py pipeline) and everything else in the spec
    is already complete and renderable without it. Only parts that
    ended up with a datasheet_url (Part 4's _populate_dimensions(), in
    turn only set when DigiKey/Mouser had one) are attempted; a
    failed/skipped deep-dive just means no entry for that part id, not
    a failure of the spec around it.

Place this file at: agents/hardware_speccer.py
"""

import os
import re
import sys
import json
import logging
import urllib.parse
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read_stage_output_text, read as bus_read
from utils.llm_client import generate_text, DROPPABLE_CONTEXT_MARKER
from relay.emitter import emit_event, EventType
from eo.errors import MissingDependencyError
from eo import workspace_facts
from agents.structure_architect import (
    _strip_fences, _mermaid_id, _sanitize_mermaid_label,
)  # reuse, don't reimplement

load_dotenv()

# Bug fix (pricing-audit root cause 2): _populate_prices()'s per-part
# try/except used to swallow every exception with zero logging -- a
# RuntimeError from find_price() exhausting its whole chain (e.g. every
# part_price_finder-tagged account rate-limited) was indistinguishable
# from a genuine bug, and both were indistinguishable from a real "no
# listing exists" result. This logger makes that failure visible/greppable
# instead of vanishing silently.
log = logging.getLogger(__name__)

# FALLBACK_CHAIN: last-resort static chain for the spec-generation call
# below, used ONLY if eo/dynamic_chain.py's build_fallback_chain() comes
# back empty (every registered account excluded/cooling down at once --
# should be very rare). This used to be the ONLY chain this module ever
# tried (one entry, GROQ_API_KEY, shared with part_price_finder's own
# calls and unmonitored/untagged in the registry) -- see
# run_hardware_speccer() below, which now builds a live, quota-ranked,
# multi-provider chain instead.
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY", "timeout": 30},
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "GROQ_API_KEY", "timeout": 30},
]


SYSTEM_PROMPT_PARTS = """You are a hardware bill-of-materials planner. \
You read a finished (or in-progress) hardware PRD/feasibility note and \
propose the parts list only -- a separate call, given your parts list \
as input, will handle wiring, physical layout, and assembly \
instructions, so do not attempt any of that here.

Never invent a part the PRD gives you no reason to include.

Never emit an enclosure as a single lump "parts" entry (e.g. one \
"Enclosure" or "Case" part covering housing, lid, mounting, and \
fasteners all at once). Decompose it into discrete parts instead: \
a housing part and a lid part (category "3D_PRINT"); one mount part \
per subsystem that needs standoff/bracket mounting -- the MCU, any \
display, and any other part with exposed leads (category "3D_PRINT", \
one mount per such subsystem, not one mount for the whole board); a \
realistic fastener count -- screws and heat-set inserts, as a \
quantity on one or a few "MISC" parts, not a single "screws" line with \
qty 1; and, only when the PRD explicitly states a weatherproof or \
outdoor requirement, a gasket/seal line (category "MISC"). Do not add \
a gasket/seal part when the PRD gives no weatherproofing requirement.

For every electrical part (category "mcu", "sensor", "actuator", or \
"power"), fill in "part_number" with a real, specific manufacturer \
part number when you know one with confidence -- e.g. a specific \
Espressif module SKU/variant for an ESP32 dev module (not just the \
bare chip family name), "DS18B20" for a common 1-Wire temperature \
sensor. This is looked up against real distributor data after you \
respond, so it must be an actual part number, never a plausible-\
looking guess -- leave it null whenever you're not confident, and \
always leave it null for generic passives (resistors, capacitors) and \
for purely mechanical "3D_PRINT"/"MISC" parts, which don't have \
distributor part numbers in the first place.

Leave "estimated_price_bdt", "vendor_name", "vendor_url", and \
"price_checked_at" as null for every part -- pricing is looked up \
separately after you respond, not something you should guess at.

For every part, also fill in "generic_name" -- a short, distributor-\
agnostic canonical name for what kind of part this is (e.g. "ESP32 \
Dev Board", "DHT22 Temperature/Humidity Sensor", "18650 Li-ion \
Battery", "5V Boost Converter"), independent of whatever specific \
"name" you gave it or any "part_number". Later steps use this as the \
shared vocabulary to look up known dimensions for this exact kind of \
part and to search real reference designs that used one -- it needs \
to describe the part itself, not a specific listing, product page, or \
one distributor's wording for it. Also fill in "aliases" -- a list of \
1-4 other real names/spellings that same generic part commonly goes \
by (e.g. for a DHT22: ["DHT-22", "AM2302"]), so that lookup still \
matches when a distributor or datasheet uses a different name for the \
same part. Leave "aliases" as an empty list when you don't know any \
real alternates -- do not invent plausible-sounding ones just to fill \
the list.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{
  "parts": [
    {"id": "mcu_1", "name": "ESP32 DevKit", "category": "mcu",
     "description": "Main microcontroller", "qty": 1,
     "part_number": "ESP32-WROOM-32U-N4",
     "generic_name": "ESP32 Dev Board",
     "aliases": ["ESP32 Development Board", "ESP32-WROOM-32 Dev Kit"],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null},
    {"id": "housing_1", "name": "Enclosure housing", "category": "3D_PRINT",
     "description": "Bottom shell", "qty": 1,
     "part_number": null,
     "generic_name": "3D-Printed Enclosure Housing",
     "aliases": [],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null},
    {"id": "lid_1", "name": "Enclosure lid", "category": "3D_PRINT",
     "description": "Top shell", "qty": 1,
     "part_number": null,
     "generic_name": "3D-Printed Enclosure Lid",
     "aliases": [],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null},
    {"id": "mount_mcu_1", "name": "MCU standoff mount", "category": "3D_PRINT",
     "description": "Standoff bracket for mcu_1", "qty": 1,
     "part_number": null,
     "generic_name": "Standoff Mount Bracket",
     "aliases": [],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null},
    {"id": "fastener_1", "name": "M3 heat-set insert + screw", "category": "MISC",
     "description": "Housing/lid fastening", "qty": 4,
     "part_number": null,
     "generic_name": "M3 Heat-Set Insert and Screw",
     "aliases": ["M3 Threaded Insert"],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null}
  ]
}
"category" is one of: "mcu", "sensor", "actuator", "power", "module", \
"3D_PRINT", "MISC". Use short lowercase_with_underscores ids -- the \
next call references these ids verbatim in wiring edges, mech \
placements, and instruction tool_ids/part_ids, so they must be stable \
and unique.
"""

# Phase A, Patch A.4 (Mech View standalone implementation guide):
# `partial`-mode variant -- selected when mech["archetype"]["enclosure_mode"]
# == "partial" (a structural chassis/frame with no full enclosing shell,
# e.g. a wheeled robot base, a drone airframe, a legged walking robot --
# see eo/device_archetype.py's own docstring for the full archetype
# vocabulary). Differs from SYSTEM_PROMPT_PARTS (the `full` variant,
# just above -- left byte-for-byte unchanged, per this patch's own
# regression-safety requirement) in exactly one place: the second
# paragraph swaps the housing/lid/gasket decomposition instructions for
# a baseplate instead, and the worked JSON example swaps housing_1/
# lid_1 for a baseplate_1 entry to match. Every other paragraph (part_
# number sourcing, pricing fields, generic_name/aliases, the trailing
# category-enum note) is identical prose to the `full` variant --
# those instructions have nothing to do with enclosure shape.
SYSTEM_PROMPT_PARTS_PARTIAL = """You are a hardware bill-of-materials planner. \
You read a finished (or in-progress) hardware PRD/feasibility note and \
propose the parts list only -- a separate call, given your parts list \
as input, will handle wiring, physical layout, and assembly \
instructions, so do not attempt any of that here.

Never invent a part the PRD gives you no reason to include.

This device has a structural chassis/frame, not a sealed enclosure --
do NOT include a housing part, a lid part, or a gasket/seal part of \
any kind. Instead, decompose the structure into: a single baseplate \
part (category "3D_PRINT") that every subsystem mounts to; one mount \
part per subsystem that needs standoff/bracket mounting -- the MCU, \
any display, and any other part with exposed leads (category \
"3D_PRINT", one mount per such subsystem, not one mount for the whole \
board); and a realistic fastener count -- screws and heat-set \
inserts, as a quantity on one or a few "MISC" parts, not a single \
"screws" line with qty 1.

For every electrical part (category "mcu", "sensor", "actuator", or \
"power"), fill in "part_number" with a real, specific manufacturer \
part number when you know one with confidence -- e.g. a specific \
Espressif module SKU/variant for an ESP32 dev module (not just the \
bare chip family name), "DS18B20" for a common 1-Wire temperature \
sensor. This is looked up against real distributor data after you \
respond, so it must be an actual part number, never a plausible-\
looking guess -- leave it null whenever you're not confident, and \
always leave it null for generic passives (resistors, capacitors) and \
for purely mechanical "3D_PRINT"/"MISC" parts, which don't have \
distributor part numbers in the first place.

Leave "estimated_price_bdt", "vendor_name", "vendor_url", and \
"price_checked_at" as null for every part -- pricing is looked up \
separately after you respond, not something you should guess at.

For every part, also fill in "generic_name" -- a short, distributor-\
agnostic canonical name for what kind of part this is (e.g. "ESP32 \
Dev Board", "DHT22 Temperature/Humidity Sensor", "18650 Li-ion \
Battery", "5V Boost Converter"), independent of whatever specific \
"name" you gave it or any "part_number". Later steps use this as the \
shared vocabulary to look up known dimensions for this exact kind of \
part and to search real reference designs that used one -- it needs \
to describe the part itself, not a specific listing, product page, or \
one distributor's wording for it. Also fill in "aliases" -- a list of \
1-4 other real names/spellings that same generic part commonly goes \
by (e.g. for a DHT22: ["DHT-22", "AM2302"]), so that lookup still \
matches when a distributor or datasheet uses a different name for the \
same part. Leave "aliases" as an empty list when you don't know any \
real alternates -- do not invent plausible-sounding ones just to fill \
the list.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{
  "parts": [
    {"id": "mcu_1", "name": "ESP32 DevKit", "category": "mcu",
     "description": "Main microcontroller", "qty": 1,
     "part_number": "ESP32-WROOM-32U-N4",
     "generic_name": "ESP32 Dev Board",
     "aliases": ["ESP32 Development Board", "ESP32-WROOM-32 Dev Kit"],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null},
    {"id": "baseplate_1", "name": "Chassis baseplate", "category": "3D_PRINT",
     "description": "Structural baseplate all subsystems mount to", "qty": 1,
     "part_number": null,
     "generic_name": "3D-Printed Chassis Baseplate",
     "aliases": [],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null},
    {"id": "mount_mcu_1", "name": "MCU standoff mount", "category": "3D_PRINT",
     "description": "Standoff bracket for mcu_1", "qty": 1,
     "part_number": null,
     "generic_name": "Standoff Mount Bracket",
     "aliases": [],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null},
    {"id": "fastener_1", "name": "M3 heat-set insert + screw", "category": "MISC",
     "description": "Baseplate/mount fastening", "qty": 4,
     "part_number": null,
     "generic_name": "M3 Heat-Set Insert and Screw",
     "aliases": ["M3 Threaded Insert"],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null}
  ]
}
"category" is one of: "mcu", "sensor", "actuator", "power", "module", \
"3D_PRINT", "MISC". Use short lowercase_with_underscores ids -- the \
next call references these ids verbatim in wiring edges, mech \
placements, and instruction tool_ids/part_ids, so they must be stable \
and unique.
"""

# `none`-mode variant -- selected when mech["archetype"]["enclosure_mode"]
# == "none" (no shared structural part at all, e.g. a bare single-board
# add-on with no housing/chassis of its own). Differs from the `full`
# variant in the same single paragraph as the `partial` variant above,
# but goes further: no baseplate either, only per-part mounts for a
# part that specifically needs one. The worked JSON example drops the
# housing_1/lid_1 pair entirely and keeps only mount_mcu_1/fastener_1,
# so the model isn't shown a shared-structural-part shape to imitate.
SYSTEM_PROMPT_PARTS_NONE = """You are a hardware bill-of-materials planner. \
You read a finished (or in-progress) hardware PRD/feasibility note and \
propose the parts list only -- a separate call, given your parts list \
as input, will handle wiring, physical layout, and assembly \
instructions, so do not attempt any of that here.

Never invent a part the PRD gives you no reason to include.

This device has no shared structural part at all -- do NOT include a \
housing part, a lid part, a gasket/seal part, or a baseplate/chassis \
part of any kind. Only include a per-part mount (category "3D_PRINT") \
for a specific part that genuinely needs standoff/bracket mounting -- \
the MCU, any display, and any other part with exposed leads -- never \
a mount for the whole assembly. A realistic fastener count for those \
individual mounts only, as a quantity on one or a few "MISC" parts, \
not a single "screws" line with qty 1.

For every electrical part (category "mcu", "sensor", "actuator", or \
"power"), fill in "part_number" with a real, specific manufacturer \
part number when you know one with confidence -- e.g. a specific \
Espressif module SKU/variant for an ESP32 dev module (not just the \
bare chip family name), "DS18B20" for a common 1-Wire temperature \
sensor. This is looked up against real distributor data after you \
respond, so it must be an actual part number, never a plausible-\
looking guess -- leave it null whenever you're not confident, and \
always leave it null for generic passives (resistors, capacitors) and \
for purely mechanical "3D_PRINT"/"MISC" parts, which don't have \
distributor part numbers in the first place.

Leave "estimated_price_bdt", "vendor_name", "vendor_url", and \
"price_checked_at" as null for every part -- pricing is looked up \
separately after you respond, not something you should guess at.

For every part, also fill in "generic_name" -- a short, distributor-\
agnostic canonical name for what kind of part this is (e.g. "ESP32 \
Dev Board", "DHT22 Temperature/Humidity Sensor", "18650 Li-ion \
Battery", "5V Boost Converter"), independent of whatever specific \
"name" you gave it or any "part_number". Later steps use this as the \
shared vocabulary to look up known dimensions for this exact kind of \
part and to search real reference designs that used one -- it needs \
to describe the part itself, not a specific listing, product page, or \
one distributor's wording for it. Also fill in "aliases" -- a list of \
1-4 other real names/spellings that same generic part commonly goes \
by (e.g. for a DHT22: ["DHT-22", "AM2302"]), so that lookup still \
matches when a distributor or datasheet uses a different name for the \
same part. Leave "aliases" as an empty list when you don't know any \
real alternates -- do not invent plausible-sounding ones just to fill \
the list.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{
  "parts": [
    {"id": "mcu_1", "name": "ESP32 DevKit", "category": "mcu",
     "description": "Main microcontroller", "qty": 1,
     "part_number": "ESP32-WROOM-32U-N4",
     "generic_name": "ESP32 Dev Board",
     "aliases": ["ESP32 Development Board", "ESP32-WROOM-32 Dev Kit"],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null},
    {"id": "mount_mcu_1", "name": "MCU standoff mount", "category": "3D_PRINT",
     "description": "Standoff bracket for mcu_1", "qty": 1,
     "part_number": null,
     "generic_name": "Standoff Mount Bracket",
     "aliases": [],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null},
    {"id": "fastener_1", "name": "M3 heat-set insert + screw", "category": "MISC",
     "description": "Mount fastening", "qty": 4,
     "part_number": null,
     "generic_name": "M3 Heat-Set Insert and Screw",
     "aliases": ["M3 Threaded Insert"],
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null}
  ]
}
"category" is one of: "mcu", "sensor", "actuator", "power", "module", \
"3D_PRINT", "MISC". Use short lowercase_with_underscores ids -- the \
next call references these ids verbatim in wiring edges, mech \
placements, and instruction tool_ids/part_ids, so they must be stable \
and unique.
"""

# Phase A, Patch A.4: selects which of the three variants above Call 1
# uses, keyed on mech["archetype"]["enclosure_mode"]. An unrecognized
# or missing enclosure_mode (e.g. no archetype ever landed on the bus --
# session_id was None, or Patch A.3's classify/resolve pair somehow
# never ran for this session) falls back to `full`, the same safe
# default eo/device_archetype.py's own classify_archetype() uses for a
# no-signal PRD -- never a silent guess toward the more exotic
# partial/none variants.
_SYSTEM_PROMPT_PARTS_BY_MODE = {
    "full": SYSTEM_PROMPT_PARTS,
    "partial": SYSTEM_PROMPT_PARTS_PARTIAL,
    "none": SYSTEM_PROMPT_PARTS_NONE,
}


def _select_parts_prompt(enclosure_mode: str) -> str:
    return _SYSTEM_PROMPT_PARTS_BY_MODE.get(enclosure_mode, SYSTEM_PROMPT_PARTS)


SYSTEM_PROMPT_WIRING = """You are a hardware wiring/layout/assembly \
planner. You are given a hardware PRD/feasibility note AND an already-\
finalized parts list (a separate call already produced it) -- propose \
a wiring graph (which part connects to which, over which specific \
pins/terminals, and whether that connection carries data, power, or \
ground), a rough physical layout inside an enclosure, and a step-by-\
step assembly sequence grouped into phases (e.g. Fabricate, Wire, \
Bring-up).

Treat the given parts list as fixed: do not add, remove, or rename \
parts, and do not invent a part id that isn't already in it. Every \
wiring edge must reference two part ids that exist in the given parts \
list. Every instruction step's tool_ids/part_ids must reference real \
entries from that same list.

Some parts in the given list carry a "dimensions_mm": {"w", "h", "d"} \
field -- these are real physical dimensions from a curated reference \
table or a distributor lookup, not an estimate. Treat "dimensions_mm" \
as ground truth for that part: use those exact w/h/d values for its \
"mech.placements" entry rather than guessing your own. Such a part \
may also carry a "dimension_confidence" of "verified" (a distributor- \
confirmed exact size -- treat it as fixed) or "typical" (a strong \
prior for that part's common form factor -- still ground truth for \
sizing, but adjust only if the project context clearly calls for a \
different size). Parts with no "dimensions_mm" field still need your \
own reasonable estimated sizing, exactly as before.

Every electrical part (i.e. every part whose category is "mcu", \
"sensor", "actuator", or "power") MUST have a matching entry in \
"wiring.nodes" and MUST appear in at least one "wiring.edges" entry \
(as either "from" or "to") -- a part that's in the bill of materials but \
never wired to anything is a bug, not a valid answer, even if it's a \
support/passthrough part like a charge controller or voltage regulator. \
The only exception is a purely mechanical, non-electrical part (e.g. \
screws, standoffs, an enclosure shell) -- omit those from "wiring.nodes" \
entirely rather than adding an orphaned node for them.

Every wiring edge must name the actual pin or terminal on each side \
whenever the PRD or the parts involved make that determinable -- e.g. \
"GPIO27" on an ESP32, "SDA"/"SCL" for I2C, "+"/"-" for a battery or \
power rail, "VCC"/"GND" for a generic supply/ground pin, "5V"/"3V3" for \
a regulator's output. Use null for "from_pin"/"to_pin" only when the \
connection genuinely has no single named pin to point to (e.g. an \
abstract "data" link you can't resolve to a specific terminal) -- do not \
leave them null just because it's easier; a wiring diagram whose edges \
don't say which pin connects to which pin is not detailed enough to \
build from.

For the physical layout, you are worse at spatial reasoning than at \
listing wiring edges -- do not attempt precise millimeter placement \
except where a part's given "dimensions_mm" already fixes it for you. \
Propose a rough grid layout only: order parts front-to-back by \
category, with power/MCU parts placed near the enclosure's center and \
sensors placed near the hull edges they would realistically mount at. \
Treat this as "which part roughly goes where," not engineering-grade CAD.

Every electrical part (i.e. every part whose category is "mcu", \
"sensor", "actuator", "power", or "module") MUST also have its own \
"mech.placements" entry, on top of (not instead of) its "wiring.nodes" \
entry above -- a part that's wired but never physically placed is the \
same class of bug as a part that's never wired: it silently disappears \
from the physical layout view even though it's a real item in the bill \
of materials. This applies to every electrical part, not only the ones \
you're confident about the exact position of -- use your best rough \
grid-layout guess (per the paragraph above) rather than omitting the \
entry. The only parts that may be omitted from "mech.placements" at \
all are fasteners (per the fastener rule below); every other part in \
the given list, electrical or 3D_PRINT/MISC, needs an entry.

Every 3D_PRINT/MISC enclosure part in the given parts list (housing, \
lid, each mount) needs its own "mech.placements" entry -- so the \
layout draws an assembled enclosure instead of floating unrelated \
cubes, and so it gets grouped into the Enclosure section downstream. \
Give the housing_1 and lid_1 entries any reasonable rough placement -- \
their exact x/y/z/w/h/d numbers do not matter and are not used: a \
deterministic step later in the pipeline computes the housing and lid's \
real size and position from what's actually packed inside and \
overwrites whatever you put here, the same way your top-level \
"mech.enclosure" w/h/d guess above is already only a rough starting \
point for other parts' placement, never load-bearing for the housing/ \
lid shell itself. Each mount's placement goes inside the housing's \
footprint, positioned near whichever subsystem part it mounts (e.g. the \
MCU's mount sits right under or beside the MCU's own placement). \
Fasteners are numerous and too small to meaningfully place individually \
-- do not give fastener parts a "mech.placements" entry at all.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{
  "wiring": {
    "nodes": [{"id": "mcu_1", "label": "ESP32 DevKit", "type": "mcu"}],
    "edges": [{"from": "mcu_1", "to": "sensor_1", "kind": "data",
               "from_pin": "GPIO34", "to_pin": "AOUT"}]
  },
  "mech": {
    "enclosure": {"w": 100, "h": 60, "d": 40},
    "placements": [
      {"part_id": "housing_1", "x": 0, "y": 0, "z": 0, "w": 100, "h": 60, "d": 30},
      {"part_id": "lid_1", "x": 0, "y": 0, "z": 30, "w": 100, "h": 60, "d": 10},
      {"part_id": "mcu_1", "x": 10, "y": 10, "z": 5, "w": 25, "h": 25, "d": 5},
      {"part_id": "mount_mcu_1", "x": 8, "y": 8, "z": 0, "w": 29, "h": 29, "d": 5}
    ]
  },
  "instructions": {
    "phases": [
      {"id": "fabricate", "name": "Fabricate",
       "steps": [
         {"id": "step_1", "title": "3D print the enclosure halves",
          "tool_ids": ["3d_printer"], "part_ids": [], "done": false}
       ]}
    ]
  }
}
"type" (wiring nodes) uses the electrical subset of the parts category \
enum -- "mcu", "sensor", "actuator", "power", "module" -- never \
"3D_PRINT"/"MISC", since those are always purely mechanical and are \
never wired. "kind" (wiring edges) is one of: \
"data", "power", "ground". "from_pin"/"to_pin" are short strings naming \
the actual pin/terminal on each side (see above), or null only when \
genuinely not resolvable. Every id referenced (wiring edges, mech \
placements, instruction tool_ids/part_ids) MUST match an id already \
defined in the given parts list / your own "wiring.nodes".
"""

INFO_PROMPT = """You are given a hardware bill-of-materials and wiring \
graph already generated for a project. Write a short one-paragraph \
plain-language summary of what the device is and does, plus 4 to 6 \
short descriptive tags (e.g. "Battery Powered", "Weatherproof \
Enclosure"). Never invent parts or capabilities the parts/wiring JSON \
doesn't support -- describe only what's actually there.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{"summary": "one paragraph, plain language", "tags": ["Tag One", "Tag Two"]}
"""

# Patch 3.2 (Phase 3, gap #10): the ONE-pin equivalent of SYSTEM_PROMPT_WIRING
# above -- deliberately narrow (one part, one pin, one answer) rather than
# reusing that broader wiring prompt, so a targeted retry can't accidentally
# re-propose the whole wiring graph.
PIN_RESOLUTION_PROMPT = """You are given an excerpt from a component's own \
datasheet and asked to name exactly ONE physical pin/GPIO on that \
component. Answer only from the excerpt -- never guess a plausible-\
sounding pin the excerpt doesn't actually support.

Respond with ONLY the pin name/number (e.g. "GPIO22", "Pin 4"), or the \
single word UNKNOWN if the excerpt doesn't say. No punctuation, no \
explanation, no markdown.
"""


def _pollinations_render_url(summary: str) -> str:
    """T2b, step 19d (optional stretch): build a Pollinations.ai image
    URL straight off an already-generated summary paragraph -- no API
    key, no signup, no separate generation call to make or await here.
    Pollinations renders the image on request when the URL is actually
    fetched (i.e. when the frontend's <img> tag loads it), so this
    function only ever does string building, never a network call.

    Prefixes the summary with a fixed photorealistic-product-photo
    framing so the render reads as a product shot rather than a literal
    illustration of the paragraph's wording, then URL-encodes the whole
    prompt per Pollinations' own `/prompt/<url-encoded prompt>` contract.
    Returns "" for empty/falsy input -- a caller should never build a
    render URL off no summary, same fail-safe spirit as _generate_info's
    own empty-but-valid convention.
    """
    if not summary:
        return ""
    prompt = f"photorealistic product photo, studio lighting, {summary}"
    return "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)


def _generate_info(spec: dict, chain: list, session_id: str = None,
                    tier: int = None, domain: str = None) -> dict:
    """T2b, step 19a: Blueprint Info/summary surface -- one more small
    LLM call, same shape as the main spec-generation call in
    run_hardware_speccer() below, reusing the parts/wiring JSON already
    produced as context instead of re-reading the PRD. Reuses that same
    call's already-built `chain` rather than deriving a second one --
    this is still the "hardware_speccer" role, just a second, smaller
    call within it.

    Fail-safe on any error (bad JSON, empty response, provider chain
    exhausted): returns the same empty-but-valid {"summary": "",
    "tags": [], "image_url": ""} shape a caller would see for a spec
    generated before this feature existed, rather than raising into
    run_hardware_speccer()'s caller -- an Info card that's just missing
    is a much smaller problem than a partially-written device spec.

    step 19d: also attaches "image_url", built off the summary via
    _pollinations_render_url() -- no extra LLM call, just string
    building, so it can't introduce a new failure mode beyond the
    summary generation already above it.
    """
    empty = {"summary": "", "tags": [], "image_url": ""}
    try:
        user_prompt = (
            f"Parts:\n{json.dumps(spec.get('parts', []))}\n\n"
            f"Wiring:\n{json.dumps(spec.get('wiring', {}))}"
        )
        raw = generate_text(INFO_PROMPT, user_prompt, chain,
                             agent_name="Hardware Speccer Info",
                             session_id=session_id, tier=tier, domain=domain,
                             allow_continuation=False)  # Root Cause B fix: this
        # is a "respond with ONLY JSON" call (see INFO_PROMPT) -- Fix C's
        # continuation prompt ("continue exactly where you left off") is
        # written for prose/code, not JSON. A truncated response spliced
        # onto a continuation from a different provider produces
        # malformed JSON that json.loads() below can't parse, discarding
        # this Info card back to the empty fallback anyway -- discarding
        # the partial text up front and retrying the ORIGINAL prompt
        # fresh on the next chain step (allow_continuation=False's actual
        # behavior, see generate_text()'s own docstring) gives this a
        # real second attempt instead of a guaranteed-broken splice.
        parsed = json.loads(_strip_fences(raw))
        summary = parsed.get("summary")
        tags = parsed.get("tags")
        if not isinstance(summary, str):
            summary = ""
        if not isinstance(tags, list):
            tags = []
        tags = [t for t in tags if isinstance(t, str)][:6]
        return {
            "summary": summary,
            "tags": tags,
            "image_url": _pollinations_render_url(summary),
        }
    except Exception:
        return empty


def _read_prd_context(session_id: str) -> str:
    """Identical convention to schema_diagrammer.py's own
    _read_prd_context() -- see that module's docstring for why this reads
    stage_output:{session_id}:{role} rather than a KEYS[...] entry, and
    why it raises MissingDependencyError("prd_writer") rather than
    guessing from nothing. Hardware PRDs go through the same prd_writer
    role as software PRDs -- there's no separate hardware-specific writer,
    so this is unchanged from the schema/architecture diagrammers.

    Bug fix (2026-08-12): now goes through memory.bus.read_stage_output_text()
    instead of re-reading the raw key and checking isinstance(..., dict)
    inline. That inline check only ever matched an approval-edited stage
    output -- an ordinary completed run writes a plain string (see
    agents/generic_worker.py's own bus_write() call), which this used to
    treat as "nothing here yet" and raise MissingDependencyError even
    when prd_writer had just finished successfully. See
    read_stage_output_text()'s own docstring for the full shape mismatch
    this closes."""
    prd_text = read_stage_output_text(session_id, "prd_writer")
    if prd_text:
        return prd_text

    intake_text = read_stage_output_text(session_id, "intake_interviewer")
    if intake_text:
        return intake_text

    raise MissingDependencyError("prd_writer")


def _populate_prices(parts: list, session_id: str = None) -> list:
    """Looks up and merges pricing for every part via
    agents/part_price_finder.py's find_price(), so the spec returns with
    prices already populated on first generation instead of requiring a
    separate "Refresh prices" click. Takes listings[0] -- NOT a "cheapest"
    selection -- matching api/server.py's existing
    POST /api/workspaces/{ws_id}/parts/refresh-prices endpoint exactly,
    so initial pricing and a later refresh never disagree about which
    vendor a part shows. find_price() itself is cached (eo/price_cache.py,
    5-day TTL), so this is cheap on any *re*-generation of the same parts.

    Bug fix (2026-08-12): this used to call find_price() once per part in
    a plain sequential for-loop, every part fighting over the exact same
    1-2 hardcoded accounts (part_price_finder.py's old module-level
    CHAIN) -- the root cause of hardware_speccer sitting for 2-3 minutes
    on a large parts list before finally rate-limiting itself out. Now
    parallelized: pick up to `worker_count` distinct, quota-ranked
    accounts tagged "part_price_finder" (eo/worker_pool.py's shared
    role_tag-parameterized selector -- see eo/registry.py's
    AGENT_CAPABILITIES), and hand each worker thread its OWN
    find_price(chain_override=...) chain (built via
    eo/dynamic_chain.py's build_fallback_chain_excluding(), so a worker's
    fallback steps also skip whatever its sibling workers are already
    using) instead of every part racing for one shared key.

    Reliability-overhaul fix (Phase 4, Patch C): dispatch no longer goes
    through a raw ThreadPoolExecutor sized to a fixed worker count.
    Every task now goes through eo/concurrency_gate.py's run_gated(),
    which reserves each worker's designated (provider, key, model)
    candidate against utils/rate_ledger.py BEFORE starting its thread,
    and re-admits queued tasks the instant a slot frees up -- so actual
    in-flight concurrency tracks live ledger headroom instead of
    starting all `len(key_envs)` threads at once and letting each one
    individually discover there wasn't room.
    """
    import functools
    from concurrent.futures import as_completed
    from agents.part_price_finder import find_price
    from eo.worker_pool import _select_workers
    from eo.dynamic_chain import build_fallback_chain_excluding, chain_step_for
    from eo.concurrency_gate import GatedTask, run_gated

    if not parts:
        return parts

    ROLE_TAG = "part_price_finder"
    # Cap workers at the smaller of (parts to look up, accounts tagged for
    # this role) -- no point spinning up more threads than there is work
    # or more than there are distinct accounts to spread it across.
    worker_count = min(len(parts), 8)
    try:
        key_envs = _select_workers(ROLE_TAG, worker_count, session_id=session_id, agent_name=ROLE_TAG)
    except RuntimeError:
        # No account tagged "part_price_finder" at all (e.g. registry not
        # updated yet in this deployment) -- degrade to the single
        # sequential fallback chain rather than crashing outright.
        key_envs = []

    def _price_one(part: dict, key_env: str, worker_id: int) -> dict:
        name = part.get("name", "")
        chain = None
        if key_env:
            # This worker's own chain: start on its assigned account,
            # then fall back to OTHER accounts/providers this specific
            # worker's siblings aren't already using.
            from eo.dynamic_chain import chain_step_for
            chain = [chain_step_for(key_env)] + build_fallback_chain_excluding(
                ROLE_TAG, exclude_keys={key_env})
        try:
            result = find_price(name, chain_override=chain,
                                 agent_name=f"{ROLE_TAG}_{worker_id}")
        except Exception as exc:
            # A single vendor-search failure shouldn't fail the whole
            # spec -- same "degrade, don't blow up" spirit as
            # part_price_finder.py's own per-provider try/except. But it
            # must not vanish silently either (pricing-audit root cause
            # 2) -- log enough to tell a real bug/rate-limit apart from a
            # genuine "not found" further down the pipeline.
            log.warning(
                "_populate_prices: price lookup failed — part=%r worker_id=%s "
                "%s: %s",
                name, worker_id, type(exc).__name__, exc,
            )
            return part
        listing = result["listings"][0] if result.get("listings") else None
        if not listing:
            return part
        part["estimated_price_bdt"] = listing.get("price_bdt")
        part["vendor_name"] = listing.get("vendor")
        part["vendor_url"] = listing.get("url")
        part["price_checked_at"] = result.get("checked_at")
        return part

    if not key_envs:
        # Fallback: no tagged accounts resolved -- still parallelize the
        # I/O (web_search fan-out inside find_price() already helps), but
        # every worker shares find_price()'s own static FALLBACK_CHAIN
        # (chain_override=None) since there's nothing to spread them
        # across account-wise. Nothing to gate a reservation against
        # either, so each task's step is None and run_gated() admits
        # them immediately, same as the old plain ThreadPoolExecutor did.
        tasks = [
            GatedTask(functools.partial(_price_one, part, None, i + 1), step=None,
                      label=f"{ROLE_TAG}_{i + 1}")
            for i, part in enumerate(parts)
        ]
        futures = run_gated(tasks, session_id=session_id)
        for future in as_completed(futures):
            future.result()
        return parts

    tasks = []
    for i, part in enumerate(parts):
        key_env = key_envs[i % len(key_envs)]
        worker_id = (i % len(key_envs)) + 1
        step = chain_step_for(key_env) if key_env else None
        tasks.append(GatedTask(
            functools.partial(_price_one, part, key_env, worker_id),
            step=step,
            label=f"{ROLE_TAG}_{worker_id}",
        ))
    futures = run_gated(tasks, session_id=session_id)
    for future in as_completed(futures):
        future.result()

    return parts


def _ensure_generic_names(parts: list) -> list:
    """
    Safety net for SYSTEM_PROMPT_PARTS's "generic_name"/"aliases" fields
    -- same "prompt instruction, not enforced schema" gap
    _ensure_electrical_placements()/_fix_wiring_electrical_integrity()
    already patch elsewhere in this file, applied here to Call 1's
    parts[] output. These two fields are the shared vocabulary the
    Master Guide's G1a curated-dimension-table lookup and G2's
    reference-design search are both meant to key off of ("generic 9V
    battery", "28BYJ-48 Stepper") instead of each other's or the
    model's own ad-hoc wording -- a part the model left un-tagged would
    otherwise reach either lookup with nothing canonical to match
    against at all.

    Runs right after Call 1's parts are parsed (success or fail-safe
    fallback), before _populate_dimensions() -- which is itself already
    part_number-keyed, not generic_name-keyed, so ordering relative to
    it doesn't matter for that call, but this needs to run before any
    future G1a/G2 lookup is added that does key off these fields.

    Never blocks on a missing/malformed field: falls back to the
    part's own "name" for generic_name (broader than a true generic
    name would be, since it may carry a specific model/variant, but
    still far better than no vocabulary at all), and normalizes
    "aliases" to always be a list -- dropping any non-string or blank
    entry and de-duplicating case-insensitively -- rather than leaving
    it null/absent/malformed for downstream code to special-case.

    Mutates each part dict in place; returns the same list, unchanged
    in length or order (this only fills gaps, never adds/drops parts).
    """
    for part in parts:
        if not isinstance(part, dict):
            continue
        if not (part.get("generic_name") or "").strip():
            part["generic_name"] = part.get("name") or part.get("id") or "Unknown part"

        aliases = part.get("aliases")
        cleaned = []
        if isinstance(aliases, list):
            seen = set()
            for alias in aliases:
                if not isinstance(alias, str):
                    continue
                alias = alias.strip()
                if not alias or alias.lower() in seen:
                    continue
                seen.add(alias.lower())
                cleaned.append(alias)
        part["aliases"] = cleaned

    return parts


def _populate_curated_dimensions(parts: list) -> list:
    """G1a (Master Guide, "G1. Real component measurements"): local,
    no-network curated-table lookup, run BEFORE _populate_dimensions()
    (G1b) below. For every part, looks up its generic_name/aliases
    against agents/component_dimension_table.py's curated dict -- a
    hand-curated set of known real component dimensions keyed by a
    stable "dimension_ref_id" -- and merges the *whole* matched row
    onto the part (dimensions_mm, shape, mount_type, mount_spec,
    dimension_confidence, source), not just dimensions_mm. Nothing
    from a match gets silently dropped.

    Must run after _ensure_generic_names() (needs normalized, always-
    present generic_name/aliases to match against) and before
    _populate_dimensions() (G1b is a gap-filler only -- see that
    function's own docstring for the "skip if already resolved" half
    of this coordination).

    A part with no match is left completely untouched -- same
    fail-safe convention as _populate_dimensions() and
    _populate_prices(): no curated hit just means "still needs G1b or
    LLM-estimated sizing," never an error.
    """
    from agents.component_dimension_table import lookup_curated_dimensions

    for part in parts:
        if not isinstance(part, dict):
            continue
        match = lookup_curated_dimensions(part.get("generic_name"), part.get("aliases"))
        if not match:
            continue
        if match.get("dimensions_mm"):
            part["dimensions_mm"] = match["dimensions_mm"]
        part["dimension_ref_id"] = match.get("dimension_ref_id")
        part["shape"] = match.get("shape")
        part["mount_type"] = match.get("mount_type")
        part["mount_spec"] = match.get("mount_spec")
        part["dimension_confidence"] = match.get("dimension_confidence")
        part["source"] = match.get("source")
        # G1a alias-collision surfacing: True when this match's row id
        # was ever on either side of a curated-table alias collision
        # (see component_dimension_table.py's _load_table()/
        # get_alias_collisions()) -- i.e. the "first row wins" policy
        # actually had to break a tie to produce this match, so a
        # different real component may have been discarded from the
        # alias index in favor of this one. Always set (True or False)
        # so downstream code/UI can distinguish "known unambiguous"
        # from "field absent."
        part["dimension_ambiguous"] = bool(match.get("dimension_ambiguous"))

    return parts


def _build_hw_reference_context(parts: list, matches_per_part: int = 2) -> str:
    """G2 (Master Guide Phase 0, Patch 0.4): before Call 2's prompt is
    built, pull top hw_ref: precedent per part via
    eo/hw_reference.search_hw_references() -- querying each part's own
    "generic_name"/"aliases", the same canonical fields
    _ensure_generic_names() already guarantees are present and
    normalized by this point (never an ad-hoc name from Call 1's own
    wording, and never re-derived here).

    Returns "" when nothing matched for ANY part -- so a spec run
    where Phase 0 has no indexed precedent yet produces byte-identical
    wiring_user_prompt to before this patch existed (the "no
    regression" half of Patch 0.4's done-when). Returns "" on
    search_hw_references() failure too, since that function already
    degrades to [] rather than raising -- this is the "unavailable
    research agent" failure posture the Phase 0 design calls for,
    inherited for free rather than re-implemented here.

    Deliberately capped at `matches_per_part` (default 2): this is
    framing context for the model, not a citation list -- more than a
    couple of precedents per part would crowd out the prompt's actual
    parts/PRD content for marginal benefit.
    """
    from eo.hw_reference import search_hw_references

    lines = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        generic_name = (part.get("generic_name") or "").strip()
        if not generic_name:
            continue
        try:
            matches = search_hw_references(generic_name, part.get("aliases"),
                                            top_k=matches_per_part)
        except Exception as exc:
            # Belt-and-suspenders: search_hw_references() already
            # catches its own embed/query failures and returns [], but
            # a prompt-construction step degrading silently on ANY
            # unexpected exception (not just the ones that module
            # anticipated) is the safer default here -- this runs on
            # every spec generation, so it should never be the reason
            # one fails.
            print(f"  [Hardware Speccer] hw_reference lookup failed for "
                  f"{generic_name!r}, skipping: {exc}")
            continue
        if not matches:
            continue
        lines.append(f"- {generic_name}:")
        for m in matches[:matches_per_part]:
            title = m.get("title") or m.get("generic_name") or "untitled"
            snippet = (m.get("content") or "").strip()
            if len(snippet) > 240:
                snippet = snippet[:240].rstrip() + "..."
            url = m.get("source_url")
            bit = f"  * \"{title}\""
            if url:
                bit += f" ({url})"
            if snippet:
                bit += f" -- {snippet}"
            lines.append(bit)

    if not lines:
        return ""

    return (
        "\n\nReference-design precedent (anecdotal, hobbyist-sourced -- "
        "NOT IPC-2221 or any formal engineering standard; treat as "
        "informal precedent from real published builds/app-notes, "
        "never as authoritative spec guidance):\n" + "\n".join(lines)
    )


def _populate_dimensions(parts: list, session_id: str = None) -> list:
    """F3 Part 4 / G1b (gap-filler only, see the Master Guide's G1
    section): looks up real physical dimensions/datasheet links for
    every part that STILL has no "dimensions_mm" after
    _populate_curated_dimensions() (G1a) above and carries a
    part_number (SYSTEM_PROMPT_PARTS's Call 1 asks the model to fill
    this in when it knows one, e.g. "ESP32-WROOM-32", "DS18B20"), via
    agents/component_spec_lookup.py's get_real_spec(). Parts G1a
    already resolved are skipped here outright -- no DigiKey/Mouser
    call spent re-confirming what the local curated table already
    answered for free. Parts with no part_number, or whose part_number
    misses on both DigiKey and Mouser (get_real_spec() returns None),
    are left untouched -- Call 2's SYSTEM_PROMPT_WIRING treats an
    absent "dimensions_mm" key as "still needs LLM-estimated sizing,"
    same as a part that never had a part_number in the first place.

    Parallelized with a ThreadPoolExecutor, same "don't make N parts
    wait on N sequential network round-trips" motivation as
    _populate_prices() above -- but deliberately NOT that function's
    worker-pool-account rotation (eo/worker_pool.py's _select_workers()/
    build_fallback_chain_excluding()). That machinery exists to spread
    load across several *interchangeable* LLM-provider accounts;
    component_spec_lookup.get_real_spec() isn't an LLM call at all --
    it's a plain HTTP lookup against exactly one fixed DigiKey
    credential pair and one fixed Mouser key (see that module's own
    docstring), so there are no sibling accounts to rotate across.
    Threads here only get the I/O off the critical path, same as
    part_price_finder.py's own internal web_search fan-out.

    A single lookup failure (network error, malformed response) never
    fails the whole spec -- same "degrade, don't blow up" spirit as
    _populate_prices()'s own per-part try/except; the part is just left
    without dimensions_mm, exactly as if it had no part_number.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from agents.component_spec_lookup import get_real_spec

    if not parts:
        return parts

    def _spec_one(part: dict) -> dict:
        if part.get("dimensions_mm"):
            # G1a (curated table) already resolved this part -- G1b is
            # a gap-filler only, see this function's own docstring.
            return part
        part_number = part.get("part_number")
        if not part_number:
            return part
        try:
            result = get_real_spec(part_number)
        except Exception:
            return part
        if not result:
            return part
        if result.get("dimensions_mm"):
            part["dimensions_mm"] = result["dimensions_mm"]
            # Same "dimension_confidence" field name G1a's curated-table
            # merge uses (_populate_curated_dimensions() above) -- a
            # DigiKey/Mouser hit that resolved a real size is tagged
            # "verified", same vocabulary regardless of which sub-step
            # resolved it. Gated on dimensions_mm being present, same
            # as get_real_spec()'s own "confidence" semantics.
            if result.get("confidence"):
                part["dimension_confidence"] = result["confidence"]
        if result.get("datasheet_url"):
            part["datasheet_url"] = result["datasheet_url"]
        if result.get("source"):
            part["source"] = result["source"]
        return part

    worker_count = min(len(parts), 8)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_spec_one, part) for part in parts]
        for future in as_completed(futures):
            future.result()

    return parts


def _populate_datasheet_details(parts: list, session_id: str = None) -> dict:
    """F3 Part 5 (optional stretch): for every part that ended up with a
    datasheet_url (set by _populate_dimensions() above, itself only set
    when DigiKey/Mouser actually had one), runs
    agents/component_spec_lookup.py's get_datasheet_detail() to pull the
    full extracted datasheet text -- mounting-hole positions, pinout
    tables, anything else beyond the top-level dimensions_mm -- keyed to
    that part's id.

    Deliberately kept OUT of the part dict itself (unlike dimensions_mm/
    datasheet_url/source, which _populate_dimensions() merges directly
    onto each part): full datasheet text is far heavier than anything
    else on a part, and nothing in Blueprint's four sub-views needs it
    inline today -- see this function's caller in run_hardware_speccer()
    for where it's written instead (workspace_facts.custom["datasheets"],
    a fifth key alongside parts/wiring/mech/instructions, for a future
    mech-primitive step (G3) to read by part id if that gets built).

    Parallelized the same way _populate_dimensions() is, and for the
    same reason that function skips _populate_prices()'s worker-pool-
    account rotation -- get_datasheet_detail() is a plain HTTP
    download+parse, not an LLM call spread across interchangeable
    accounts.

    A single part's failed/skipped deep-dive (get_datasheet_detail()
    returning None -- no datasheet_url, download failure, non-PDF
    response, parse error; see that function's own docstring) simply
    means no entry for that part id in the returned dict -- never
    raises, never fails the parts/wiring spec around it. Returns {}
    if no part in `parts` has a datasheet_url at all.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from agents.component_spec_lookup import get_datasheet_detail

    candidates = [p for p in parts if p.get("datasheet_url")]
    if not candidates:
        return {}

    def _detail_one(part: dict):
        part_id = part.get("id")
        try:
            detail = get_datasheet_detail(part["datasheet_url"])
        except Exception:
            return part_id, None
        return part_id, detail

    details = {}
    worker_count = min(len(candidates), 8)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_detail_one, part) for part in candidates]
        for future in as_completed(futures):
            part_id, detail = future.result()
            if part_id and detail:
                details[part_id] = detail

    return details


def resolve_inferred_pin(part: dict, pin_hint: str, edge_kind: str, chain: list,
                          session_id: str = None, tier: int = None, domain: str = None) -> str | None:
    """Patch 3.2 (Phase 3, gap #10): single targeted resolution retry for
    ONE unresolved wiring pin on ONE part -- re-queries that part's own
    datasheet detail (agents/component_spec_lookup.py's
    get_datasheet_detail(), the same F3 Part 5 deep-dive
    _populate_datasheet_details() above already runs; reused here rather
    than a second download path, and normally already cached by eo/
    datasheet_cache.py by the time this runs) and makes ONE small,
    narrowly-scoped LLM call (PIN_RESOLUTION_PROMPT above) asking it to
    name the real GPIO/pin serving `pin_hint` (e.g. "SCL", "VIN") on THIS
    part specifically -- never a general re-run of the whole wiring agent
    (SYSTEM_PROMPT_WIRING's own call), matching Patch 3.2's own sizing
    note in the patch breakdown.

    Capped at one retry BY CONSTRUCTION: this function makes exactly one
    datasheet lookup and one LLM call, then returns -- looping/retrying
    across multiple pins, or deciding what "still unresolved" means for
    the run as a whole, belongs to the caller (Patch 3.3's finalize-path
    wiring), same repair-until-cap posture eo/mech_repair.py's own
    run_repair_loop() already holds for every other "regenerate, don't
    loop forever" case in this pipeline -- this function itself has no
    retry loop to cap.

    Returns the resolved pin label (e.g. "GPIO22") on a clean,
    label-shaped answer, or None if: `part` is falsy, the part has no
    datasheet_url (nothing to query), the datasheet deep-dive fails or
    comes back empty, the LLM call itself fails/is exhausted, or the
    model's answer doesn't look like a real pin name -- said UNKNOWN
    honestly, or came back long/sentence-shaped instead of a short label
    (a hedge/explanation reads the same as "still unresolved" here, just
    not spelled UNKNOWN). "Still unresolved" is a valid, expected, non-
    error outcome -- this never raises past a failed lookup or a bad
    answer.
    """
    if not part:
        return None
    datasheet_url = part.get("datasheet_url")
    if not datasheet_url:
        return None

    from agents.component_spec_lookup import get_datasheet_detail
    try:
        detail = get_datasheet_detail(datasheet_url)
    except Exception:
        return None
    content = (detail or {}).get("content") if isinstance(detail, dict) else None
    if not content:
        return None

    part_label = part.get("name") or part.get("id") or "this part"
    user_prompt = (
        f"Datasheet excerpt for {part_label}:\n{content[:6000]}\n\n"
        f'Which single physical pin/GPIO on {part_label} serves the '
        f'"{pin_hint or "unresolved"}" ({edge_kind or "data"}) function?'
    )
    try:
        raw = generate_text(PIN_RESOLUTION_PROMPT, user_prompt, chain,
                             agent_name="Hardware Speccer Pin Resolution",
                             session_id=session_id, tier=tier, domain=domain)
    except Exception:
        return None

    candidate = (raw or "").strip().strip('"').strip(".")
    if not candidate or candidate.upper() == "UNKNOWN":
        return None
    # A real pin label is short and label-shaped -- a multi-sentence or
    # multi-clause answer means the model hedged/explained instead of
    # naming one pin, which is the same "still unresolved" outcome as
    # UNKNOWN, just not spelled that way.
    if len(candidate) > 24 or "\n" in candidate or candidate.count(" ") > 3:
        return None
    return candidate


# Same color-per-kind convention WiringGraph.jsx's EDGE_COLORS already
# uses for the force-graph view, kept identical here (via Mermaid's
# linkStyle) so the two renderings of the same wiring data never disagree
# about what "power" vs "ground" vs "data" looks like.
_EDGE_COLOR_BY_KIND = {
    "data": "#22c55e",
    "power": "#f59e0b",
    "ground": "#6b7280",
}
_DEFAULT_EDGE_COLOR = "#6b7280"

# Same node-type vocabulary as WiringGraph.jsx's TYPE_COLORS / Blueprint's
# device-spec schema (hardware_speccer.py's own SYSTEM_PROMPT above) --
# one flowchart shape per type so the diagram is visually scannable by
# category (MCU vs sensor vs power, etc.) without reading every label,
# the same idea architecture_diagrammer.py's SHAPE_BY_KIND already uses
# for software components.
_SHAPE_BY_TYPE = {
    "mcu": ("[", "]"),          # rectangle
    "sensor": ("(", ")"),        # rounded
    "actuator": ("([", "])"),    # stadium
    "power": ("[(", ")]"),       # cylinder
    "module": ("[[", "]]"),      # subroutine
}
_TYPE_TITLES = {
    "mcu": "MCU", "sensor": "Sensors", "actuator": "Actuators",
    "power": "Power", "module": "Modules",
}


def _build_wiring_mermaid(spec: dict) -> str:
    """Deterministic renderer -- same "JSON proposes, Python renders"
    contract as architecture_diagrammer.py's _build_architecture_mermaid()
    and schema_diagrammer.py's _build_schema_mermaid(), and reuses their
    exact sanitizer (_sanitize_mermaid_label(), see structure_architect.py
    for why: Bug 5 of the rendering audit) so this inherits "always valid
    Mermaid syntax" for free instead of needing its own validate/retry
    pass. This is the detailed, pin-level wiring diagram Blueprint's
    force-graph view (WiringGraph.jsx) can't express -- see that
    component's own module docstring for why a force-graph is the right
    tool for "what connects to what" but the wrong tool for "which
    specific pin connects to which specific pin."

    Groups nodes into one subgraph per device-spec type (mcu/sensor/
    actuator/power/module) -- same idea the PRD's own (unvalidated,
    Bug 9/10/11) freeform Wiring Overview mermaid block already reached
    for on its own (ESP32/Sensor_Ports/Power subgraphs), just done here
    deterministically off the same structured wiring.nodes/edges data
    Blueprint already trusts, instead of the model writing Mermaid syntax
    directly.

    An edge whose from_pin/to_pin (hardware_speccer.py's SYSTEM_PROMPT,
    step 1 of the wiring-detail fix) are present gets a
    "kind: from_pin->to_pin" label; an edge missing one or both pins
    (an older spec, or a connection the model couldn't resolve to a
    named pin) falls back to just "kind", same as before that schema
    change -- never blocks rendering on missing pin data.

    An edge referencing a node id that isn't in wiring.nodes (shouldn't
    happen per the SYSTEM_PROMPT's own contract, but a model response is
    never fully guaranteed to honor it) is skipped rather than emitted,
    so this never produces Mermaid that references an undeclared node.

    Returns "" for an empty/missing wiring.nodes -- caller decides what
    an empty diagram means for its own UI, this function doesn't guess.
    """
    wiring = spec.get("wiring") or {}
    nodes = wiring.get("nodes") or []
    edges = wiring.get("edges") or []
    if not nodes:
        return ""

    node_ids = {}
    groups = {}
    group_order = []
    for n in nodes:
        node_type = n.get("type") or "module"
        groups.setdefault(node_type, []).append(n)
        if node_type not in group_order:
            group_order.append(node_type)

    lines = ["flowchart LR"]
    for node_type in group_order:
        title = _TYPE_TITLES.get(node_type, node_type.title())
        lines.append(f'    subgraph {_mermaid_id(f"grp_{node_type}")}["{title}"]')
        open_b, close_b = _SHAPE_BY_TYPE.get(node_type, ("[", "]"))
        for n in groups[node_type]:
            raw_id = n.get("id") or "?"
            nid = _mermaid_id(f"n_{raw_id}")
            node_ids[raw_id] = nid
            label = _sanitize_mermaid_label(n.get("label") or raw_id)
            lines.append(f'        {nid}{open_b}"{label}"{close_b}')
        lines.append("    end")

    style_lines = []
    edge_index = 0
    for e in edges:
        from_id = node_ids.get(e.get("from"))
        to_id = node_ids.get(e.get("to"))
        if not from_id or not to_id:
            continue  # references a node not in wiring.nodes -- skip, don't emit a dangling reference

        kind = e.get("kind") or "link"
        from_pin = (e.get("from_pin") or "").strip()
        to_pin = (e.get("to_pin") or "").strip()
        if from_pin and to_pin:
            raw_label = f"{kind}: {from_pin}->{to_pin}"
        elif from_pin or to_pin:
            raw_label = f"{kind}: {from_pin or to_pin}"
        else:
            raw_label = kind
        # _fix_wiring_electrical_integrity() tags edges it synthesized
        # (a missing I2C clock line, a missing power-tree input) with
        # "_inferred" -- surface that on the diagram itself rather than
        # blending a safety-net guess in as if the model had proposed
        # it, so a reader can tell "wire this for real" from "this is
        # what the model actually said."
        inferred = bool(e.get("_inferred"))
        if inferred:
            raw_label += " (inferred)"
        # Quoted, not bare, edge label: an unquoted Mermaid pipe-label
        # (|...|) can't safely contain parentheses -- exactly the
        # "ESP[ESP32 5V (Vin)]"-style parse error from the rendering
        # audit's Bug 5, but on an edge label instead of a node label.
        # Pin names routinely contain parens (e.g. "GPIO21 (SDA)"), so
        # quoting is required here even though the node labels above get
        # away without it.
        label = _sanitize_mermaid_label(raw_label, fallback=kind)

        # Dashed arrow ("-.->") for an inferred edge vs. Mermaid's
        # default solid "-->" for everything the model actually
        # proposed -- same color-per-kind as before either way, so an
        # inferred edge still reads as "power"/"data"/"ground" at a
        # glance, just visibly provisional rather than indistinguishable
        # from a model-verified connection.
        arrow = "-.->" if inferred else "-->"
        lines.append(f'    {from_id} {arrow}|"{label}"| {to_id}')
        color = _EDGE_COLOR_BY_KIND.get(kind, _DEFAULT_EDGE_COLOR)
        style = f'    linkStyle {edge_index} stroke:{color},color:{color}'
        if inferred:
            style += ",stroke-dasharray:4 3"
        style_lines.append(style)
        edge_index += 1

    lines.extend(style_lines)
    return "\n".join(lines)


# Categories the wiring rule already treats as "MUST be present" in
# wiring.nodes (SYSTEM_PROMPT_WIRING's electrical-part paragraph) --
# reused here as the same MUST-have-a-placement set the new
# mech.placements paragraph above asks the model for.
_ELECTRICAL_CATEGORIES = {"mcu", "sensor", "actuator", "power", "module"}

# G1c (Master Guide, "G1. Real component measurements" -- shape-aware
# single-primitive rendering): maps the curated-dimension-table's own
# shape vocabulary (component_dimension_table.py's data file --
# "Box", "Cylindrical", "Conical Head", etc.) onto the three Level-0
# primitives G3/G4's own tree already names ("Level 0  Primitives
# (cylinder, box, cone)") -- G1c only draws a single primitive per
# part, not full G3 composition, but reuses that same three-shape
# vocabulary so MechView.jsx's primitive renderer doesn't need a
# fourth kind invented just for this smaller slice of the work.
# Shapes with no close single-primitive match (Hexagonal, Knurled
# Cylinder, Circular, Irregular) fall back to the nearest visual
# approximation (cylinder for round-but-not-plain-cylindrical shapes,
# box for genuinely irregular ones) rather than a shape MechView.jsx
# has no renderer for at all.
_SHAPE_TO_PRIMITIVE = {
    "box": "box",
    "cylindrical": "cylinder",
    "knurled cylinder": "cylinder",
    "circular": "cylinder",
    "hexagonal": "cylinder",
    "conical head": "cone",
    "irregular": "box",
}


def _apply_placement_shapes(spec: dict, parts: list) -> None:
    """G1c: for any part with a matched `shape` (set by G1a's curated-
    table lookup, _populate_curated_dimensions() above -- G1b's
    DigiKey/Mouser lookup doesn't return a shape, only dimensions/
    datasheet/source/confidence, so this only ever fires for G1a hits),
    copy that shape straight onto the part's own mech.placements entry
    as one of MechView.jsx's known primitive types (see
    _SHAPE_TO_PRIMITIVE above). No LLM call -- a matched shape from the
    curated table is a known fact, not a guess, so this is the cheap,
    deterministic slice of full G3 multi-primitive composition that's
    safe to ship right now, ahead of G3 landing.

    Runs last in the mech.placements pipeline, after
    _ensure_electrical_placements()/_clamp_placements_to_enclosure()
    above -- it only annotates placement entries that already exist
    (model-proposed or gap-filled) with a `shape` string, it never
    adds, removes, resizes, or repositions one, so it has no ordering
    dependency on the geometry-fixing steps beyond needing their
    output to already exist.

    A part with no matched shape (no G1a curated-table hit at all, or
    a shape value outside _SHAPE_TO_PRIMITIVE's vocabulary) is left
    completely untouched -- its placement entry simply has no `shape`
    key, and MechView.jsx's existing default (always draw a box)
    applies to it exactly as it did before this change.
    """
    placements = (spec.get("mech") or {}).get("placements")
    if not isinstance(placements, list):
        return

    shape_by_part_id = {
        part.get("id"): part.get("shape")
        for part in parts
        if isinstance(part, dict) and part.get("shape")
    }
    if not shape_by_part_id:
        return

    for placement in placements:
        if not isinstance(placement, dict):
            continue
        curated_shape = shape_by_part_id.get(placement.get("part_id"))
        if not curated_shape:
            continue
        primitive = _SHAPE_TO_PRIMITIVE.get(curated_shape.strip().lower())
        if primitive:
            placement["shape"] = primitive


# ---------------------------------------------------------------------------
# G3a (Master Guide, "G3/G4. Hierarchical parallel build + validate" --
# Level 0->1, deterministic-first primitive composition): extends every
# mech.placements[] entry a part actually got real dimensions_mm for
# (either G1a's curated-table hit or G1b's DigiKey/Mouser hit -- see
# _populate_curated_dimensions()/_populate_dimensions() above) with a
# `primitives` list -- local offset/size/rotation/color_role entries
# that compose into that one part, per the guide's own three Level-0
# templates (cylinder, box, cone-on-a-box-shaft). No LLM call and no
# FreeCAD validation yet -- purely mechanical geometry off data already
# resolved earlier in this same pipeline. G3b (agents/mech_primitive_
# pool.py, not built by this patch) is the LLM-driven sibling step for
# whatever's left uncovered here (no dimensions_mm at all); G3c (eo/
# mech_validator.py, also not built by this patch) is what actually
# checks a composed part's primitives stay inside its own bounding box
# -- this patch only produces the primitives, it doesn't verify them.
# ---------------------------------------------------------------------------

# Cone-on-a-box-shaft split (buttons/buzzers): no curated-table row
# carries a separate shaft/dome measurement, only one overall w/h/d, so
# the split is a fixed, documented ratio (a third of the part's own
# height goes to the dome) rather than a per-part guess.
_CONE_DOME_RATIO = 0.35


def _box_primitive_template(w: float, h: float, d: float) -> list:
    """Level-0 "box" template -- a single box primitive spanning the
    part's own full w/h/d, corner-origin (offset 0,0,0) like every
    other placement in this module."""
    return [{
        "offset": {"x": 0, "y": 0, "z": 0},
        "size": {"w": w, "h": h, "d": d},
        "rotation": {"x": 0, "y": 0, "z": 0},
        "shape": "box",
        "color_role": "primary",
    }]


def _cylinder_primitive_template(w: float, h: float, d: float) -> list:
    """Level-0 "cylinder" template -- a single cylinder primitive, w as
    diameter and h as height (same axis convention
    _apply_placement_shapes()/MechView.jsx's PrimitiveGeometry already
    use for round shapes), spanning the part's own full bounding box."""
    return [{
        "offset": {"x": 0, "y": 0, "z": 0},
        "size": {"w": w, "h": h, "d": d},
        "rotation": {"x": 0, "y": 0, "z": 0},
        "shape": "cylinder",
        "color_role": "primary",
    }]


def _cone_primitive_template(w: float, h: float, d: float) -> list:
    """Level-0 "cone" template -- a short box "shaft" at the part's own
    base, topped by a cone "dome" sized off _CONE_DOME_RATIO, per the
    Master Guide's own "cone on a small box shaft" wording for buttons/
    buzzers. Both primitives share the part's own w/d footprint; only
    h is split between them, stacked corner-origin (dome's offset.y
    starts exactly where the shaft ends) so the two read as one
    continuous part rather than two floating pieces."""
    dome_h = max(round(h * _CONE_DOME_RATIO, 2), 1)
    shaft_h = max(round(h - dome_h, 2), 1)
    return [
        {
            "offset": {"x": 0, "y": 0, "z": 0},
            "size": {"w": w, "h": shaft_h, "d": d},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "shape": "box",
            "color_role": "primary",
        },
        {
            "offset": {"x": 0, "y": shaft_h, "z": 0},
            "size": {"w": w, "h": dome_h, "d": d},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "shape": "cone",
            "color_role": "primary",
        },
    ]


# Keyed by the same "box"/"cylinder"/"cone" vocabulary
# _apply_placement_shapes() writes onto placement["shape"] (and that
# MechView.jsx's PrimitiveGeometry already renders) -- no fourth
# vocabulary invented just for template lookup.
_PRIMITIVE_TEMPLATES = {
    "box": _box_primitive_template,
    "cylinder": _cylinder_primitive_template,
    "cone": _cone_primitive_template,
}

# mount_spec grammar (Master Guide example: "35mm hole c-c"). No data-
# file legend for this string's grammar exists anywhere else in the
# repo, so this patch is what defines it -- three shapes, all optionally
# carrying a trailing thread size:
#   "M3 thread"                                   -- single threaded boss
#   "2-hole 35mm c-c, M3"                          -- linear center-to-center pair
#   "4-hole 48x18mm rectangular pattern, M2"       -- rectangular N-hole pattern
_MOUNT_SPEC_RECT_RE = re.compile(
    r"(\d+)-hole\s+([\d.]+)\s*x\s*([\d.]+)\s*mm\s+rectangular\s+pattern"
    r"(?:,\s*(M\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)
_MOUNT_SPEC_CC_RE = re.compile(
    r"(\d+)-hole\s+([\d.]+)\s*mm\s+c-c(?:,\s*(M\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)
_MOUNT_SPEC_THREAD_RE = re.compile(r"^\s*(M\d+(?:\.\d+)?)\s+thread\s*$", re.IGNORECASE)


def _parse_mount_spec(mount_spec) -> dict | None:
    """Parses a curated-table/distributor `mount_spec` string into a
    small structured dict _mount_hole_primitives()/
    _resize_mount_parts_from_mount_spec() below can act on. Returns
    None for anything that isn't a non-empty string matching one of
    the three grammars above -- same fail-safe convention as the rest
    of this module: an unparseable mount_spec just means "no mounting-
    hole primitives / no mount resize for this part," never an error.
    """
    if not isinstance(mount_spec, str) or not mount_spec.strip():
        return None

    m = _MOUNT_SPEC_RECT_RE.search(mount_spec)
    if m:
        return {
            "pattern": "rect",
            "hole_count": int(m.group(1)),
            "span_x": float(m.group(2)),
            "span_y": float(m.group(3)),
            "thread": m.group(4),
        }

    m = _MOUNT_SPEC_CC_RE.search(mount_spec)
    if m:
        return {
            "pattern": "cc",
            "hole_count": int(m.group(1)),
            "span": float(m.group(2)),
            "thread": m.group(3),
        }

    m = _MOUNT_SPEC_THREAD_RE.match(mount_spec)
    if m:
        return {"pattern": "thread", "thread": m.group(1)}

    return None


# Hole radius per thread size (slightly larger than the thread's own
# nominal diameter, standard clearance-hole practice) -- not exact
# per-standard clearance figures, just enough to read as "a real hole
# sized to its thread" rather than a fixed blob regardless of M2 vs M4.
_THREAD_HOLE_RADIUS_MM = {"M2": 1.1, "M2.5": 1.4, "M3": 1.6, "M4": 2.1}
_DEFAULT_HOLE_RADIUS_MM = 1.5


def _hole_radius_for_thread(thread) -> float:
    if not thread:
        return _DEFAULT_HOLE_RADIUS_MM
    return _THREAD_HOLE_RADIUS_MM.get(thread.upper(), _DEFAULT_HOLE_RADIUS_MM)


def _mount_hole_primitives(w: float, h: float, d: float, parsed: dict) -> list:
    """Turns a _parse_mount_spec() result into small accent-colored
    hole cylinders on the part's own placement -- through-drilled
    along the part's own depth (`d`), positioned symmetrically around
    the part's own w/h center, and clamped so no hole (nor its own
    radius) can land outside the part's own footprint. This is the
    "clamped to its own footprint" behavior the guide's mounting-hole
    fidelity note calls for -- a spec'd hole span larger than the part
    itself (e.g. a stepper's c-c spacing wider than its own diameter,
    or a rounding artifact) gets pulled back inside rather than drawing
    a hole primitive that pokes outside its own part's volume.
    """
    if not parsed:
        return []

    radius = _hole_radius_for_thread(parsed.get("thread"))
    diameter = radius * 2
    hole_d = max(d, 1)
    cx, cy = w / 2, h / 2

    pattern = parsed.get("pattern")
    if pattern == "thread":
        centers = [(cx, cy)]
    elif pattern == "cc":
        max_span = max(w - 2 * radius, 0)
        span = min(parsed.get("span") or 0, max_span)
        centers = [(cx - span / 2, cy), (cx + span / 2, cy)]
    elif pattern == "rect":
        span_x = min(parsed.get("span_x") or 0, max(w - 2 * radius, 0))
        span_y = min(parsed.get("span_y") or 0, max(h - 2 * radius, 0))
        centers = [
            (cx - span_x / 2, cy - span_y / 2),
            (cx + span_x / 2, cy - span_y / 2),
            (cx - span_x / 2, cy + span_y / 2),
            (cx + span_x / 2, cy + span_y / 2),
        ]
    else:
        return []

    primitives = []
    for hx, hy in centers:
        hx = min(max(hx, radius), max(w - radius, radius))
        hy = min(max(hy, radius), max(h - radius, radius))
        primitives.append({
            "offset": {"x": round(hx - radius, 2), "y": round(hy - radius, 2), "z": 0},
            "size": {"w": round(diameter, 2), "h": round(diameter, 2), "d": hole_d},
            "rotation": {"x": 0, "y": 0, "z": 0},
            "shape": "cylinder",
            "color_role": "accent",
        })
    return primitives


def _apply_primitive_composition(spec: dict, parts: list) -> None:
    """G3a's main step: for every mech.placements[] entry whose part
    actually has `dimensions_mm` resolved -- either G1a's curated
    table or G1b's DigiKey/Mouser lookup -- builds a `primitives` list
    off the deterministic shape->template mapping
    (_PRIMITIVE_TEMPLATES), keyed by whatever _apply_placement_shapes()
    already set on `placement["shape"]` (defaulting to "box" when no
    G1a shape match exists, same default MechView.jsx's own single-
    primitive fallback already uses). A part whose mount_spec parses
    successfully (_parse_mount_spec() above) also gets small accent-
    colored mounting-hole primitives appended on top of its main
    template, via _mount_hole_primitives().

    Gated on `dimensions_mm` rather than a matched `shape` specifically
    -- a G1b (DigiKey/Mouser) hit has real w/h/d but no shape/mount_spec
    at all (component_spec_lookup.get_real_spec() doesn't return
    those), so it still gets a real-sized single-box template here
    instead of being skipped just because it has no shape opinion. A
    part with neither dimensions_mm nor a resolved shape is left
    completely untouched -- that's exactly G3b's future scope (the LLM
    primitive pool), not this deterministic pass's job.

    Must run after _apply_placement_shapes() (needs its `shape` output)
    and after _clamp_placements_to_enclosure() (needs each placement's
    own w/h/d already fitted inside the enclosure, since every
    primitive here is sized as a fraction of that same w/h/d).
    """
    placements = (spec.get("mech") or {}).get("placements")
    if not isinstance(placements, list):
        return

    parts_by_id = {
        part.get("id"): part for part in parts if isinstance(part, dict)
    }

    for placement in placements:
        if not isinstance(placement, dict):
            continue
        part = parts_by_id.get(placement.get("part_id"))
        if not isinstance(part, dict) or not part.get("dimensions_mm"):
            continue

        w = placement.get("w") or 1
        h = placement.get("h") or 1
        d = placement.get("d") or 1
        shape = placement.get("shape") or "box"
        template_fn = _PRIMITIVE_TEMPLATES.get(shape, _box_primitive_template)
        primitives = template_fn(w, h, d)

        parsed_mount = _parse_mount_spec(part.get("mount_spec"))
        if parsed_mount:
            primitives = primitives + _mount_hole_primitives(w, h, d, parsed_mount)

        placement["primitives"] = primitives


# G0's own mount-per-subsystem naming convention (SYSTEM_PROMPT_PARTS's
# own example: "mount_mcu_1" mounts "mcu_1") -- the sibling-lookup key
# _resize_mount_parts_from_mount_spec() below uses.
_MOUNT_ID_PREFIX = "mount_"
# Fixed bracket material margin around the outermost hole span -- same
# "documented ratio, not a per-part guess" spirit as _CONE_DOME_RATIO
# above; nothing in the curated table specifies bracket wall thickness.
_MOUNT_MARGIN_MM = 6


def _resize_mount_parts_from_mount_spec(spec: dict, parts: list) -> None:
    """G3a side effect (Master Guide: "mount_spec ... can size/position
    that subsystem's G0-created mount part instead of G0's mount sizing
    staying LLM-guessed"): for any part with a parsed mount_spec AND a
    sibling mount part already in the BOM (id "mount_" + this part's
    own id), resizes that mount's own mech.placements entry to span the
    real hole footprint (plus a small fixed material margin) instead of
    whatever the model guessed for it, and recenters it under the
    mounted part's own placement.

    A rectangular N-hole pattern drives both w and h; a 2-hole c-c spec
    drives w only (h is left as whatever it already was, since c-c is a
    single linear span, not a footprint) -- only touch what the spec
    actually constrains, same honesty _clamp_placements_to_enclosure()
    already practices elsewhere in this module. A single threaded boss
    (`pattern == "thread"`) has no span to size a bracket off of at all
    -- skipped here; only _mount_hole_primitives() (the composition
    step above) draws it, as a hole on the part's own placement.

    Must run BEFORE _clamp_placements_to_enclosure() -- a resized mount
    here is still only a candidate size/position; it needs the same
    enclosure-bounds clipping every other placement gets before it's
    final.

    A part with no mount_spec, no parsed mount_spec, or no sibling
    mount placement is left completely untouched, same fail-safe
    convention as every other step in this pipeline.
    """
    mech = spec.get("mech")
    if not isinstance(mech, dict):
        return
    placements = mech.get("placements")
    if not isinstance(placements, list):
        return

    placements_by_part_id = {
        p.get("part_id"): p for p in placements if isinstance(p, dict)
    }

    for part in parts:
        if not isinstance(part, dict):
            continue
        parsed = _parse_mount_spec(part.get("mount_spec"))
        if not parsed or parsed.get("pattern") == "thread":
            continue

        part_placement = placements_by_part_id.get(part.get("id"))
        mount_placement = placements_by_part_id.get(
            _MOUNT_ID_PREFIX + str(part.get("id")))
        if not isinstance(part_placement, dict) or not isinstance(mount_placement, dict):
            continue

        if parsed["pattern"] == "rect":
            new_w = parsed["span_x"] + 2 * _MOUNT_MARGIN_MM
            new_h = parsed["span_y"] + 2 * _MOUNT_MARGIN_MM
        else:  # "cc"
            new_w = parsed["span"] + 2 * _MOUNT_MARGIN_MM
            new_h = mount_placement.get("h") or part_placement.get("h") or new_w

        mount_placement["w"] = round(new_w, 2)
        mount_placement["h"] = round(new_h, 2)

        # Recenter the (now-resized) mount directly under the part it
        # mounts -- same x/y-center-alignment idea SYSTEM_PROMPT_WIRING
        # already asks the model to eyeball ("the MCU's mount sits
        # right under or beside the MCU's own placement"), just made
        # exact now that a real size/center exists to align to.
        part_cx = (part_placement.get("x") or 0) + (part_placement.get("w") or 0) / 2
        part_cy = (part_placement.get("y") or 0) + (part_placement.get("h") or 0) / 2
        mount_placement["x"] = round(part_cx - new_w / 2, 2)
        mount_placement["y"] = round(part_cy - new_h / 2, 2)


def _ensure_electrical_placements(spec: dict, parts: list) -> None:
    """
    Safety net for SYSTEM_PROMPT_WIRING's mech.placements coverage rule.
    That rule is a prompt instruction, not an enforced schema -- the model
    can still (and, before this fix, routinely did) return mech.placements
    covering only the 3D_PRINT/MISC enclosure parts (housing/lid/mounts)
    and silently drop every electrical part (sensors, actuators, power,
    modules), even though those same parts are correctly required to be
    present in wiring.nodes. That's the "14 BOM parts, ~5-6 rendered
    boxes" bug: MechView.jsx only ever draws what's in mech.placements,
    so a part missing from there simply never appears, with no error
    anywhere to flag it.

    Runs after Call 2's spec is parsed, before persistence. Mutates
    spec["mech"]["placements"] in place, adding one entry for every
    electrical part the model left out -- never touching a placement the
    model already provided (so any legitimate model-chosen position is
    left alone; this only fills gaps). New entries are packed into a
    simple left-to-right, top-to-bottom grid clamped to the enclosure's
    own w/h footprint, using each part's own "dimensions_mm" when a
    distributor lookup already supplied one (same ground-truth precedent
    _populate_dimensions() sets for everything else), or a small
    placeholder box otherwise. This is deliberately as unintelligent as
    the "rough grid" the prompt itself asks the model for elsewhere --
    the goal is coverage (every part visible somewhere), not placement
    quality.
    """
    mech = spec.setdefault("mech", {})
    if not isinstance(mech, dict):
        return
    enclosure = mech.get("enclosure") or {}
    placements = mech.get("placements")
    if not isinstance(placements, list):
        placements = []
        mech["placements"] = placements

    placed_ids = {p.get("part_id") for p in placements if isinstance(p, dict)}
    missing = [
        part for part in parts
        if part.get("category") in _ELECTRICAL_CATEGORIES
        and part.get("id") not in placed_ids
    ]
    if not missing:
        return

    enc_w = enclosure.get("w") or 100
    enc_h = enclosure.get("h") or 60
    enc_d = enclosure.get("d") or 40
    default_dims = {"w": 15, "h": 15, "d": 8}

    cursor_x, cursor_y, row_h = 0, 0, 0
    for part in missing:
        dims = part.get("dimensions_mm") or {}
        w = min(dims.get("w") or default_dims["w"], enc_w)
        h = min(dims.get("h") or default_dims["h"], enc_h)
        d = min(dims.get("d") or default_dims["d"], enc_d)

        if cursor_x + w > enc_w:
            cursor_x = 0
            cursor_y += row_h
            row_h = 0
        if cursor_y + h > enc_h:
            cursor_y = 0  # wrap inside the footprint rather than run off it

        placements.append({
            "part_id": part.get("id"),
            "x": cursor_x, "y": cursor_y, "z": 0,
            "w": w, "h": h, "d": d,
        })
        cursor_x += w
        row_h = max(row_h, h)


def _clamp_placements_to_enclosure(spec: dict) -> None:
    """
    Safety net for the second Mech-view bug: parts rendering as a
    disconnected cluster floating outside the enclosure hull on some
    generations of an otherwise-identical spec. SYSTEM_PROMPT_WIRING's
    placement guidance for electrical parts is only the loose "rough
    grid... near hull edges" heuristic (unlike housing/lid/mounts, which
    get exact nesting rules) -- nothing ties a sensor/actuator/power/
    module part's x/y/z or w/h/d to the enclosure's own bounds, so the
    model's freely-guessed coordinates vary run to run and can land
    partly or fully outside enclosure.w/h/d. MechView.jsx's <mesh> then
    renders that box exactly where it's told, with no bounds check of
    its own -- so the same design can look assembled in one generation
    and have a block cluster floating beside the hull in the next,
    purely because the guessed numbers differ, not because anything
    about the device changed.

    Runs over EVERY placement already in spec["mech"]["placements"] at
    this point -- both what the model itself proposed and what
    _ensure_electrical_placements() (above) added for parts the model
    omitted entirely -- and clips each one's w/h/d to fit within the
    enclosure's own dimensions, then clips x/y/z so the (now-clipped)
    box's far edge can't extend past the enclosure's far edge either.
    This never changes which part is placed where relative to its
    neighbors (order and rough position are preserved), only pulls
    anything that overshoots back inside the hull -- the same "coverage
    and containment, not layout quality" honesty framing the rest of
    this view already uses.
    """
    mech = spec.get("mech")
    if not isinstance(mech, dict):
        return
    enclosure = mech.get("enclosure") or {}
    placements = mech.get("placements")
    if not isinstance(placements, list):
        return

    enc_w = enclosure.get("w") or 100
    enc_h = enclosure.get("h") or 60
    enc_d = enclosure.get("d") or 40

    for p in placements:
        if not isinstance(p, dict):
            continue
        # Clip size first (never larger than the hull itself), then clip
        # position against the now-known size so x+w/y+h/z+d can't run
        # past the far wall.
        w = min(max(p.get("w") or 0, 1), enc_w)
        h = min(max(p.get("h") or 0, 1), enc_h)
        d = min(max(p.get("d") or 0, 1), enc_d)
        p["w"], p["h"], p["d"] = w, h, d
        p["x"] = min(max(p.get("x") or 0, 0), enc_w - w)
        p["y"] = min(max(p.get("y") or 0, 0), enc_h - h)
        p["z"] = min(max(p.get("z") or 0, 0), enc_d - d)


def _fix_wiring_electrical_integrity(spec: dict) -> None:
    """
    Safety net for three wiring.edges shapes that read as physically
    invalid on the rendered diagram even though they pass the model's
    own schema -- same "prompt instruction, not enforced schema" gap
    _ensure_electrical_placements()/_clamp_placements_to_enclosure()
    already patch for mech.placements, applied here to wiring.edges
    instead. Runs before _build_wiring_mermaid() renders spec["wiring"],
    so the Mermaid diagram and the force-graph (WiringGraph.jsx, reading
    the same wiring.edges) inherit the fix identically -- one repaired
    wiring object, not a render-time patch only one of the two views
    would get.

    1. Incomplete I2C pair: SYSTEM_PROMPT_WIRING asks the model to name
       "SDA"/"SCL" for I2C, but nothing stops it wiring only one of the
       two -- a device on the bus for data with no clock line isn't
       actually wired. For every edge naming "SDA" on either side,
       ensure a matching "SCL" edge exists between the same two nodes;
       synthesize one when it doesn't, leaving the new edge's pin on the
       MCU side null (this safety net has no way to know the real GPIO,
       and SYSTEM_PROMPT_WIRING's own rule is that null is correct for a
       pin that genuinely can't be resolved -- guessing a wrong GPIO
       here would be worse than admitting it's unresolved).

    2. Orphaned power input: same "not enforced schema" gap as #1, on
       the supply side of a power edge -- a power-category node with an
       outgoing "power" edge (it feeds something) but zero incoming
       "power" edges (nothing feeds it) reads as a component powering
       itself, e.g. the second of two parallel regulators the model
       wired only one of. Among power-category nodes with zero incoming
       power edges, pick the one with the most outgoing power edges as
       the tree's root (ties broken by earliest position in
       wiring.nodes) -- almost always the battery/cell, the one thing
       every other power part ultimately traces back to -- then connect
       every other such orphan to it. As blunt as
       _ensure_electrical_placements' own grid-fill: this restores a
       valid tree shape, it doesn't reason about which specific rail
       should feed which regulator.

    3. Same pin fed by two different rails: two power edges landing on
       the same (to node, to pin) pair from two different sources --
       e.g. both a 5V and a 3.3V rail wired to the MCU's "VCC" -- read
       as one physical pin taking two different voltages at once, which
       isn't valid; they're two distinct real pins the model just gave
       the same generic name. Disambiguate by folding each source's own
       from_pin (or, failing that, its node's label) into the to_pin
       text, so the diagram shows two separate destination pins instead
       of one pin with two power sources feeding it.

    Visual note: every edge #1/#2 synthesizes is tagged "_inferred":
    True so _build_wiring_mermaid() can render it as a dashed link --
    distinguishing "the model proposed this" from "this safety net
    filled a gap" on the diagram itself, rather than silently blending
    inferred wiring in as if the model had proposed it.

    Mutates spec["wiring"]["edges"] in place (appends new edges for
    #1/#2, rewrites "to_pin" strings in place for #3). No-ops on an
    empty/missing wiring.
    """
    wiring = spec.get("wiring")
    if not isinstance(wiring, dict):
        return
    nodes = wiring.get("nodes")
    edges = wiring.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list) or not nodes:
        return

    node_by_id = {n.get("id"): n for n in nodes if isinstance(n, dict) and n.get("id")}
    node_order = {n.get("id"): i for i, n in enumerate(nodes) if isinstance(n, dict)}

    def _norm_pin(p):
        return (p or "").strip().lower()

    # ---- 1. complete I2C pairs (SDA present, SCL missing) -----------
    new_edges = []
    handled_links = set()
    for e in edges:
        if not isinstance(e, dict):
            continue
        fp, tp = _norm_pin(e.get("from_pin")), _norm_pin(e.get("to_pin"))
        if tp == "sda":
            sda_side = "to"
        elif fp == "sda":
            sda_side = "from"
        else:
            continue

        from_id, to_id = e.get("from"), e.get("to")
        link_key = frozenset((from_id, to_id))
        if link_key in handled_links:
            continue
        handled_links.add(link_key)

        has_scl = any(
            isinstance(e2, dict)
            and frozenset((e2.get("from"), e2.get("to"))) == link_key
            and (_norm_pin(e2.get("from_pin")) == "scl" or _norm_pin(e2.get("to_pin")) == "scl")
            for e2 in edges
        )
        if has_scl:
            continue

        new_edges.append({
            "from": from_id, "to": to_id, "kind": e.get("kind") or "data",
            "from_pin": "SCL" if sda_side == "from" else None,
            "to_pin": "SCL" if sda_side == "to" else None,
            "_inferred": True,
        })

    # ---- 2. connect orphaned power inputs ---------------------------
    power_ids = {nid for nid, n in node_by_id.items() if n.get("type") == "power"}
    if power_ids:
        outgoing_power, incoming_power = {}, {}
        for e in edges:
            if not isinstance(e, dict) or (e.get("kind") or "") != "power":
                continue
            outgoing_power[e.get("from")] = outgoing_power.get(e.get("from"), 0) + 1
            incoming_power[e.get("to")] = incoming_power.get(e.get("to"), 0) + 1

        root_candidates = [pid for pid in power_ids if incoming_power.get(pid, 0) == 0]
        root_id = None
        if root_candidates:
            root_id = min(
                root_candidates,
                key=lambda pid: (-outgoing_power.get(pid, 0), node_order.get(pid, 0)),
            )

        if root_id:
            for pid in power_ids:
                if pid == root_id:
                    continue
                if outgoing_power.get(pid, 0) > 0 and incoming_power.get(pid, 0) == 0:
                    new_edges.append({
                        "from": root_id, "to": pid, "kind": "power",
                        "from_pin": None, "to_pin": "VIN",
                        "_inferred": True,
                    })

    edges.extend(new_edges)

    # ---- 3. disambiguate one pin fed by two different sources -------
    by_target = {}
    for e in edges:
        if not isinstance(e, dict) or (e.get("kind") or "") != "power":
            continue
        key = (e.get("to"), _norm_pin(e.get("to_pin")))
        by_target.setdefault(key, []).append(e)

    for (_to_id, _tp), group in by_target.items():
        if len({e.get("from") for e in group}) < 2:
            continue  # only one source into this pin -- nothing to disambiguate
        for e in group:
            src_node = node_by_id.get(e.get("from")) or {}
            source_label = e.get("from_pin") or src_node.get("label") or e.get("from")
            base = e.get("to_pin") or "VCC"
            e["to_pin"] = f"{base} ({source_label})"


def run_hardware_speccer(session_id: str = None, tier: int = None,
                          task_text: str = None, domain: str = None,
                          workspace_id: str = None) -> dict:
    """Entry point, dispatched by eo/executor.py alongside schema/
    architecture_diagrammer.py's run_*() functions -- same signature plus
    workspace_id, since the spec is written into that workspace's
    workspace_facts.custom (see module docstring), not a standalone bus
    key. Raises MissingDependencyError("prd_writer") via
    _read_prd_context() if there's nothing to spec yet -- see that
    function's docstring. Raises ValueError if workspace_id is missing --
    same requirement workspace_facts.set_facts() itself enforces, checked
    here first so the (slower, costs tokens) generation call never runs
    for a request that was always going to fail to save."""
    if not workspace_id:
        raise ValueError("workspace_id is required")

    prd_text = _read_prd_context(session_id)

    user_prompt = f"PRD:\n{prd_text}"
    if task_text:
        user_prompt += f"\n\nOriginal task: {task_text}"

    # Bug fix (2026-08-12): deferred import -- see eo/dynamic_chain.py's
    # module docstring for why this can't be a module-level import
    # (eo.registry imports this module at load time; eo.dynamic_chain
    # imports eo.registry at ITS module level). Quota-ranked,
    # cooldown-aware, spread across providers -- replaces the old
    # single-entry CHAIN that had nothing to fall back to.
    from eo.dynamic_chain import build_fallback_chain
    chain = build_fallback_chain("hardware_speccer") or FALLBACK_CHAIN

    # Phase A, Patch A.4 (Mech View standalone implementation guide):
    # read the archetype Patch A.3 already stashed on the bus (in
    # eo/executor.py's hardware_speccer dispatch branch, immediately
    # before this function was ever called) and use it to pick which of
    # the three SYSTEM_PROMPT_PARTS variants Call 1 gets below. Missing
    # entirely (e.g. session_id was None, or A.3 never ran for this
    # session) reads back as {} from bus_read's own default, which
    # .get("enclosure_mode", "full") -- and _select_parts_prompt()'s own
    # fallback below it -- both resolve to the same safe `full` default.
    archetype = bus_read(f"device_archetype:{session_id}", default={}) or {}
    enclosure_mode = archetype.get("enclosure_mode", "full")
    parts_prompt = _select_parts_prompt(enclosure_mode)

    # F3 Part 4: split into two calls. Call 1 proposes parts only (each
    # optionally carrying a part_number); real dimensions get looked up
    # and merged in before Call 2 ever runs, so Call 2's wiring/mech/
    # instructions reasoning can be told which parts already have
    # ground-truth sizing vs. which still need LLM estimation.
    raw_parts = generate_text(parts_prompt, user_prompt, chain,
                               agent_name="Hardware Speccer Parts",
                               session_id=session_id, tier=tier, domain=domain,
                               allow_continuation=False)  # Root Cause B fix:
    # SYSTEM_PROMPT_PARTS (or its A.4 partial/none sibling -- all three
    # demand "ONLY valid JSON") -- letting a "length"
    # truncation get a continuation prompt spliced onto it from
    # (possibly) a different provider corrupts the JSON (mismatched
    # braces/formatting), which the json.loads() below can't parse, and
    # the whole parts list collapses to the single "Spec unavailable"
    # fail-safe stub. allow_continuation=False makes a "length" cutoff
    # here behave like a transient error instead: the partial text is
    # discarded and the ORIGINAL prompt is retried fresh (full token
    # budget, no splice) on the next chain step.
    cleaned_parts = _strip_fences(raw_parts)

    try:
        parts_result = json.loads(cleaned_parts)
        parts = parts_result.get("parts", [])
        if not isinstance(parts, list):
            parts = []
    except json.JSONDecodeError:
        # Fail safe, same spirit as schema/architecture_diagrammer.py's
        # fallbacks: a minimal valid shape naming the failure, rather than
        # nothing at all -- keeps all four Blueprint sub-views renderable.
        # Step 20a note: this single placeholder part intentionally stays
        # category "module" (still a valid value in the extended enum)
        # rather than being decomposed -- decomposition is a property of
        # a real enclosure the model reasoned about, and there's nothing
        # to decompose when generation failed outright.
        parts = [{"id": "unavailable", "name": "Spec unavailable", "category": "module",
                  "description": "", "qty": 1, "part_number": None,
                  "generic_name": "Spec unavailable", "aliases": [],
                  "estimated_price_bdt": None, "vendor_name": None,
                  "vendor_url": None, "price_checked_at": None}]

    # Fill any generic_name/aliases gaps Call 1 left -- see
    # _ensure_generic_names' own docstring. Runs before dimension
    # lookup/Call 2 so every downstream consumer (including this
    # function's own later steps and any future G1a/G2 lookup) can rely
    # on both fields always being present in a normalized shape.
    parts = _ensure_generic_names(parts)

    # G1a: local, no-network curated-table lookup, BEFORE G1b's
    # DigiKey/Mouser gap-filler -- see _populate_curated_dimensions()'s
    # own docstring. Runs first because it's free and synchronous;
    # whatever it resolves, G1b below skips outright.
    parts = _populate_curated_dimensions(parts)

    # G1b: merge real dimensions/datasheet links onto whichever parts
    # STILL have no dimensions_mm after G1a and carry a part_number,
    # BEFORE Call 2 -- see _populate_dimensions()'s own docstring.
    # Parts left untouched here simply have no "dimensions_mm" key,
    # which Call 2's SYSTEM_PROMPT_WIRING treats as "still needs your
    # own estimated sizing."
    parts = _populate_dimensions(parts, session_id=session_id)

    # Call 2: wiring/mech/instructions, given the (now dimension-enriched)
    # parts list as fixed input rather than generating parts itself.
    wiring_user_prompt = (
        f"{user_prompt}\n\n"
        f"Parts (already finalized -- do not add, remove, or rename any; "
        f"some carry a \"dimensions_mm\" field, which is verified ground "
        f"truth, not an estimate):\n{json.dumps(parts)}"
    )

    # Phase 0, Patch 0.4: fold in hw_ref: precedent (if any) right
    # before Call 2 runs -- see _build_hw_reference_context()'s own
    # docstring for why this is the last thing added to
    # wiring_user_prompt and why it degrades to "" (no prompt change at
    # all) rather than ever blocking or altering generation on its own.
    #
    # Bug fix (2026-08-16): the hw_ref block is genuinely optional
    # framing context, never something Call 2 structurally needs (parts/
    # PRD/task text above it is what it needs) -- but it also made this
    # prompt more likely to cross a provider's per-request TPM ceiling
    # than before Patch 0.4 existed. Marking its start with
    # DROPPABLE_CONTEXT_MARKER lets utils.llm_client's request-too-large
    # handling drop exactly this block first on a 413, instead of blind
    # end-of-string slicing that could just as easily cut into the parts
    # JSON above it (see that module's own comment on this fix).
    hw_reference_context = _build_hw_reference_context(parts)
    if hw_reference_context:
        wiring_user_prompt += DROPPABLE_CONTEXT_MARKER + hw_reference_context

    raw_wiring = generate_text(SYSTEM_PROMPT_WIRING, wiring_user_prompt, chain,
                                agent_name="Hardware Speccer Wiring",
                                session_id=session_id, tier=tier, domain=domain,
                                allow_continuation=False)  # Root Cause B fix:
    # same reasoning as Call 1 above -- SYSTEM_PROMPT_WIRING is also an
    # "ONLY valid JSON" contract (wiring/mech/instructions), and a
    # spliced continuation here is exactly why spec["wiring"] was
    # collapsing to the empty {"nodes": [], "edges": []} fallback (which
    # then left Blueprint's Wiring/Mech tabs with nothing to render).
    cleaned_wiring = _strip_fences(raw_wiring)

    try:
        spec = json.loads(cleaned_wiring)
    except json.JSONDecodeError:
        # Same fail-safe spirit as Call 1 above -- Call 2 failing doesn't
        # discard the parts Call 1 already produced, it just means no
        # wiring/mech/instructions to show for them yet.
        spec = {
            "wiring": {"nodes": [], "edges": []},
            "mech": {"enclosure": {"w": 0, "h": 0, "d": 0}, "placements": []},
            "instructions": {"phases": []},
        }

    # Phase A, Patch A.4: stash the archetype fetched above onto
    # spec["mech"] -- setdefault first since a successful Call 2 parse
    # isn't guaranteed to have included a "mech" key at all (a model
    # that emitted valid JSON but genuinely produced no mech content),
    # same defensive shape the fail-safe branch just above already
    # models for that key. Every later phase in this guide (B's
    # swept-volume modeling, C's mass/CoG check, D's access mechanisms,
    # E's material defaults, Patch A.5's own downstream gating of
    # eo/mech_enclosure.py and eo/mech_cutouts.py, none of that this
    # patch) reads mech["archetype"] off exactly this dict from here on.
    spec.setdefault("mech", {"enclosure": {"w": 0, "h": 0, "d": 0}, "placements": []})
    spec["mech"]["archetype"] = archetype

    spec["parts"] = parts
    spec["parts"] = _populate_prices(spec.get("parts", []), session_id=session_id)

    # Fill any mech.placements gaps the model left for electrical parts
    # (see _ensure_electrical_placements' own docstring) -- must run
    # after parts are finalized above (for dimensions_mm) and before the
    # wiring.mermaid build below, though mermaid itself doesn't read mech.
    _ensure_electrical_placements(spec, spec["parts"])

    # G3a side effect: resize/recenter any G0-created mount part off
    # its mounted part's real mount_spec, BEFORE the enclosure clamp
    # below -- see _resize_mount_parts_from_mount_spec()'s own
    # docstring for why this has to run first (a resized mount is still
    # only a candidate size/position, not a final one).
    _resize_mount_parts_from_mount_spec(spec, spec["parts"])

    # Clip every placement (model-produced, gap-filled, or just
    # mount-resized above) to the enclosure's own bounds -- see
    # _clamp_placements_to_enclosure's own docstring for why an
    # unclamped placement is what produces the "floating disconnected
    # cluster" Mech-view symptom.
    _clamp_placements_to_enclosure(spec)

    # G1c: annotate every placement whose part had a G1a curated-table
    # shape match with a MechView.jsx-renderable primitive type -- see
    # _apply_placement_shapes' own docstring. Runs after the geometry
    # steps above since it only annotates placements that already
    # exist, never adds/resizes/repositions one.
    _apply_placement_shapes(spec, spec["parts"])

    # G3a main step: compose each placement's own `primitives` list off
    # its now-final (clamped, shape-annotated) w/h/d -- see
    # _apply_primitive_composition()'s own docstring. Must run last of
    # this group, after both the shape annotation and the enclosure
    # clamp it depends on.
    _apply_primitive_composition(spec, spec["parts"])

    # G3b: LLM primitive pool for whatever G3a's deterministic path above
    # left uncovered -- an electrical part with no dimensions_mm at all
    # (no G1a curated-table hit, no G1b DigiKey/Mouser hit), fanned out
    # via eo/worker_pool.py the same way the Code Writer Pool is. Deferred
    # import: agents/mech_primitive_pool.py is invoked from here, so a
    # module-level import would be circular (same fix as
    # eo/dynamic_chain.py's own deferred import above, just the reverse
    # direction). No-op, no worker-pool call at all, when G3a already
    # covered everything -- see mech_primitive_pool.run()'s own docstring.
    from agents.mech_primitive_pool import run as run_mech_primitive_pool
    run_mech_primitive_pool(spec, spec["parts"], session_id=session_id, domain=domain)

    # Gap fix (flagged against G3i, which scoped itself to Level 1->2
    # through 3->4 only): Level 0->1's own generate->validate->repair
    # pass was never actually driven anywhere, even though eo/
    # mech_validator.py's LEVEL_0_1 path (G3c) and eo/mech_repair.py's
    # run_repair_loop() (G3d) were already generic enough to handle it
    # with zero changes. eo/mech_repair.py's new run_level_0_1_repair()
    # is that missing driver -- validates every placement's `primitives`
    # against its own part's w/h/d bounding box via headless FreeCAD and
    # regenerates just the violating ones through agents/
    # mech_primitive_pool.py's new regenerate_primitives() (capped at 2
    # retries, flagged-not-blocked past the cap), same shape every later
    # level's own repair driver already uses. Called here, right after
    # the initial G3a/G3b composition pass this repair loop corrects,
    # and folded into the SAME try/finally as every later level below
    # (rather than its own try/finally) so eo/mech_validator.py's
    # persistent per-run FreeCAD sandbox session -- opened lazily on
    # first use -- stays warm from this very first validate_layout()
    # call through Level 3->4's, instead of being closed and reopened
    # between Level 0->1 and Level 1->2.
    from eo.mech_repair import run_level_0_1_repair

    # G3i (Master Guide, "G3/G4. Hierarchical parallel build + validate",
    # pipeline wiring): the previously-standalone Level 1->2 -> 2->3 ->
    # 3->4 generate/validate/repair tree -- agents/mech_subsection_pool.py
    # (G3e-2), eo/mech_repair.py's run_level_1_2_repair() (G3e-4), agents/
    # mech_section_pool.py (G3f-1), eo/mech_repair.py's
    # run_level_2_3_repair() (G3f-2), eo/mech_device.py's
    # apply_device_merge() (G3g, first half), and eo/mech_repair.py's
    # run_level_3_4_repair() (G3g, second half) -- actually driven from
    # this pipeline, in sequence, for the first time. Deferred imports,
    # same "invoked from here, so a module-level import would be
    # circular" reasoning as run_mech_primitive_pool just above -- agents/
    # mech_subsection_pool.py's own module docstring documents the
    # identical circular-import edge for its own deferred `from
    # agents.hardware_speccer import _parse_mount_spec`.
    #
    # Level 1->2:
    #   1. run_mech_subsection_pool(): pairs every part with its own
    #      G0-created mount (or leaves a singleton subsection alone) and
    #      proposes each ungrounded mount's relative offset via one LLM
    #      call per pair -- see that module's own run() docstring for the
    #      no-op/skip cases (singleton subsections, mount_spec already
    #      grounded, no in-scope targets at all).
    #   2. run_level_1_2_repair(): validates every subsection via headless
    #      FreeCAD (does a part collide with its own mount?), regenerates
    #      just the violating ones through agents/mech_subsection_pool.py's
    #      regenerate_subsection() (capped at 2 retries, flagged-not-
    #      blocked past the cap), and persists each subsection's validated
    #      footprint onto mech["subsections"] -- Level 2->3's own input.
    #
    # Level 2->3 -- called AFTER Level 1->2 settles, since its section
    # grouping and validation both read `mech["subsections"][*]
    # ["footprint"]`:
    #   3. run_mech_section_pool(): groups Level 2's subsections into
    #      Level 3's five functional sections and proposes every non-
    #      anchor subsection's offset relative to its section's own
    #      anchor, one LLM call per section (2+ checkable subsections) --
    #      see that module's own run() docstring for the no-op cases.
    #   4. run_level_2_3_repair(): validates every section via headless
    #      FreeCAD (do two different subsections in the same section
    #      collide?), regenerates just the violating ones through agents/
    #      mech_section_pool.py's regenerate_section(), and persists each
    #      section's validated footprint onto mech["sections"] -- Level
    #      3->4's own input.
    #
    # Level 3->4 -- called AFTER Level 2->3 settles, since the device
    # merge below reads every section's own validated `footprint`:
    #   5. apply_device_merge(): the deterministic, LLM-free front/center/
    #      edge rule -- positions every non-Enclosure section's real
    #      member placements relative to each other inside the Enclosure
    #      section's own validated footprint, and stashes the plan onto
    #      mech["device"]. No worker-pool sibling at this level -- see
    #      that module's own docstring on why a fixed, five-node
    #      deterministic rule replaces an LLM call here.
    #   6. run_level_3_4_repair(): validates every non-Enclosure section
    #      via headless FreeCAD against the Enclosure section's own
    #      footprint (global containment) and against every other section
    #      (cross-section collision), regenerates -- clips, never a fresh
    #      LLM proposal, see that function's own docstring on why -- just
    #      the violating ones via _clamp_section_into_container(), and
    #      re-runs apply_device_merge() once more at the end to re-stash
    #      the FINAL device layout plan onto mech["device"]. This is the
    #      last level in the tree (Master Guide: "closes out the tree").
    #
    # This whole block is wrapped in one try/finally, not one per level,
    # since eo/mech_validator.py's persistent per-run FreeCAD sandbox
    # session (see that module's own docstring on why one session stays
    # alive for the whole run rather than one per call) is opened lazily
    # on first use by Level 1->2's own validate_layout() call and should
    # stay warm through every later level's own validate_layout() calls
    # instead of being torn down and re-opened between them -- so
    # close_session() only runs once, here, after Level 3->4 (the actual
    # last step), "success or abort," per the Master Guide, regardless of
    # which level (if any) a mid-run exception came from.
    from agents.mech_subsection_pool import run as run_mech_subsection_pool
    from agents.mech_section_pool import run as run_mech_section_pool
    from eo.mech_repair import run_level_1_2_repair, run_level_2_3_repair, run_level_3_4_repair
    from eo.mech_device import apply_device_merge
    from eo.mech_enclosure import apply_enclosure_generation
    from eo.mech_supports import apply_supports_generation
    from eo.mech_cutouts import apply_cutout_generation
    from eo.mech_manufacturability import build_manufacturability_report
    from eo.mech_validator import close_session as close_mech_validator_session
    from eo.mech_validator import find_unresolved_inferred_pins
    try:
        # Level 0->1 -- gap fix, see the comment block above: validates/
        # repairs the primitives G3a/G3b just composed before Level 1->2
        # ever groups them into subsections.
        run_level_0_1_repair(spec, spec["parts"], session_id=session_id, domain=domain)

        run_mech_subsection_pool(spec, spec["parts"], session_id=session_id, domain=domain)
        run_level_1_2_repair(spec, spec["parts"], session_id=session_id, domain=domain)

        run_mech_section_pool(spec, spec["parts"], session_id=session_id, domain=domain)
        run_level_2_3_repair(spec, spec["parts"], session_id=session_id, domain=domain)

        apply_device_merge(spec.get("mech") or {}, spec["parts"])
        # Patch 1.5: Phase 1's own pipeline wiring -- runs immediately
        # after apply_device_merge() (needs its mech["device"]["footprint"]
        # output, see eo/mech_enclosure.py's own apply_enclosure_generation()
        # docstring) and BEFORE run_level_3_4_repair(), so containment/
        # collision validation below checks against the real computed
        # housing, not the LLM's now-discarded guess (Patch 1.4).
        apply_enclosure_generation(spec.get("mech") or {}, spec["parts"])
        run_level_3_4_repair(spec, spec["parts"], session_id=session_id, domain=domain)
        # Patch 2.4: Phase 2's own pipeline wiring. Deliberately sequenced
        # AFTER run_level_3_4_repair() rather than immediately after
        # apply_enclosure_generation() (a literal reading of the patch
        # breakdown's own "call it right after apply_enclosure_generation()"
        # phrasing) -- run_level_3_4_repair() is what actually clips/
        # regenerates any member placement that fails containment or
        # cross-section collision (see the Level 3->4 comment block above),
        # so a member's x/y/z isn't truly final until it returns. Computing
        # standoffs/bosses BEFORE that repair pass would project corners
        # off positions the repair step might still move, leaving stale
        # supports under a part that's no longer where they were computed
        # for -- the same "don't act on placements the validator hasn't
        # signed off on yet" ordering apply_enclosure_generation() itself
        # already applies one level up (housing before Level 3->4, not
        # after, precisely because housing sizing does NOT depend on
        # per-part final positions the way a per-part standoff does).
        apply_supports_generation(spec.get("mech") or {}, spec["parts"])
        # Patch 5.6: Phase 5's own pipeline wiring. Sequenced AFTER
        # apply_supports_generation() per that patch's own breakdown
        # note ("needs standoff positions available for the overlap
        # awareness described in the master guide") -- see eo/
        # mech_cutouts.py's own apply_cutout_generation() docstring for
        # why that "overlap awareness" is Phase 6's future job, not
        # something this call computes itself; this ordering only
        # guarantees mech["supports"] already exists by the time
        # mech["cutouts"] is populated alongside it.
        apply_cutout_generation(spec.get("mech") or {}, spec["parts"])
        # Patch 6.4: Phase 6's own pipeline wiring. Runs last in this
        # block, after mech["housing"]/mech["supports"]/mech["cutouts"]
        # are ALL populated -- build_manufacturability_report() (Patch
        # 6.1/6.2/6.3) only reads that already-computed geometry, it
        # never generates any of its own, so it belongs strictly after
        # every generator above, same "checks run after the geometry
        # they check exists" ordering apply_cutout_generation() itself
        # already sits at relative to apply_supports_generation(). Makes
        # no FreeCAD/validator calls of its own, so it's harmless to run
        # inside this same try block ahead of close_mech_validator_session()
        # below rather than after it -- kept here (not in `finally`)
        # so a mid-pipeline exception above still skips it, same as
        # every other apply_*() call in this block.
        mech = spec.get("mech") or {}
        mech["manufacturability"] = build_manufacturability_report(mech)
    finally:
        close_mech_validator_session(session_id)

    # Step 3 of the wiring-detail fix: deterministically render the
    # pin-level flowchart off spec["wiring"] (nodes/edges, now carrying
    # from_pin/to_pin per step 1's schema change) and fold it back in as
    # wiring.mermaid, right alongside the nodes/edges WiringGraph.jsx's
    # force-graph view already reads -- one wiring object, two renderings
    # of it, never two independently-generated diagrams that could
    # disagree (that's exactly the problem with the PRD's own separate,
    # unvalidated Wiring Overview block -- Bug 9/10/11 of the rendering
    # audit). "" (empty wiring.nodes) is a valid, honest value here, not
    # an error -- a caller checks for it the same way it already checks
    # for an empty nodes/edges list.
    if "wiring" not in spec or not isinstance(spec["wiring"], dict):
        spec["wiring"] = {"nodes": [], "edges": []}

    # Repair the three wiring.edges shapes that pass the model's own
    # schema but render as physically invalid (incomplete I2C pair,
    # orphaned power input, one pin fed by two rails) -- see
    # _fix_wiring_electrical_integrity()'s own docstring. Must run
    # after "wiring" is guaranteed to be a dict (immediately above) and
    # before _build_wiring_mermaid() below, so the diagram it renders
    # already reflects the repaired edges rather than needing a second
    # pass.
    _fix_wiring_electrical_integrity(spec)

    # Patch 3.3 (Phase 3, gap #10): pin resolution gate -- close the loop
    # on inferred wiring before this spec is considered final. Runs after
    # _fix_wiring_electrical_integrity() immediately above (needs the
    # inferred edges it may just have synthesized) and after the mech
    # pipeline's own try/finally block earlier in this function has
    # already run run_level_3_4_repair() -- which is what populates
    # spec["mech"]["sections"] via apply_section_grouping() -- so
    # find_unresolved_inferred_pins() below can tell "load-bearing" (a
    # part that's actually in the final device) from "just sitting in
    # placements" (see that function's own docstring on why `spec`
    # itself, not spec["mech"] alone, is passed as its `mech` argument).
    # Before mermaid build so a resolved pin's real name is what
    # actually renders, not the pre-resolution null it replaced.
    unresolved_pins = find_unresolved_inferred_pins(spec)
    still_unresolved = []
    if unresolved_pins:
        parts_by_id = {p.get("id"): p for p in spec.get("parts", []) if isinstance(p, dict)}
        for pin in unresolved_pins:
            part = parts_by_id.get(pin["part_id"])
            edge = pin["edge"]
            resolved = resolve_inferred_pin(
                part, pin.get("pin_hint"), edge.get("kind"), chain,
                session_id=session_id, tier=tier, domain=domain,
            )
            if resolved:
                if pin["pin_side"] == "from":
                    edge["from_pin"] = resolved
                else:
                    edge["to_pin"] = resolved
            else:
                # Still unresolved after the one capped retry -- surface
                # it loudly in the handoff (custom["wiring"] below)
                # rather than silently shipping a null pin on a
                # load-bearing net. The edge itself stays "_inferred":
                # True either way; that flag already means "the model
                # didn't propose this," resolving the pin doesn't change
                # who proposed the edge.
                still_unresolved.append({
                    "part_id": pin["part_id"], "pin_side": pin["pin_side"],
                    "kind": edge.get("kind"),
                    "from": edge.get("from"), "to": edge.get("to"),
                })
    if still_unresolved:
        spec["wiring"]["unresolved_pins"] = still_unresolved

    spec["wiring"]["mermaid"] = _build_wiring_mermaid(spec)

    # T2b, step 19a: Blueprint Info/summary surface -- one more small
    # LLM call reusing the parts/wiring JSON already produced above,
    # same chain as the main generation call. See _generate_info()'s own
    # docstring for why this never raises.
    #
    # Root Cause F fix: _generate_info() only ever sees spec["parts"]/
    # spec["wiring"] -- it never re-reads the PRD. When Call 1/Call 2
    # above both hit their JSON-parse fail-safe (the single "unavailable"
    # placeholder part, and/or an empty wiring.nodes list), that's almost
    # no real signal to describe, and the model was confabulating a
    # plausible-sounding generic product instead of admitting it had
    # nothing to work with -- directly contradicting INFO_PROMPT's own
    # "never invent parts/capabilities" instruction. Skip the call
    # entirely in that case and fall back to the same empty-but-valid
    # {"summary": "", "tags": [], "image_url": ""} shape _generate_info()
    # itself already returns on any other failure, so callers don't need
    # to special-case this path.
    _parts_are_placeholder = (
        len(spec["parts"]) == 1 and spec["parts"][0].get("id") == "unavailable"
    )
    _wiring_is_empty = not spec.get("wiring", {}).get("nodes")
    if _parts_are_placeholder or _wiring_is_empty:
        spec["info"] = {"summary": "", "tags": [], "image_url": ""}
    else:
        spec["info"] = _generate_info(spec, chain, session_id=session_id, tier=tier, domain=domain)

    # F3 Part 5 (optional stretch): datasheet deep-dive, keyed by part
    # id. Deliberately last of the "real data" work in this function --
    # the most expensive per-part call and the least likely to block
    # anything above it (Parts/Wiring/Mech/Instructions/Info are all
    # already fully built by this point) if it comes back thin or
    # empty. See _populate_datasheet_details()'s own docstring for why
    # this is a standalone dict rather than merged onto spec["parts"].
    spec["datasheets"] = _populate_datasheet_details(spec.get("parts", []), session_id=session_id)

    # Same read-modify-write shape api/server.py's refresh-prices endpoint
    # already uses for custom["parts"] alone -- read the whole facts
    # object, update only this spec's custom keys, write it back, so
    # unrelated custom entries (e.g. deploy_target) are never touched.
    facts = workspace_facts.get_facts(workspace_id)
    custom = dict(facts.get("custom") or {})
    custom["parts"] = spec.get("parts", [])
    custom["wiring"] = spec.get("wiring", {})
    # Patch 6.4: no separate custom["manufacturability"] key needed --
    # mech["manufacturability"] (stashed above, right after
    # apply_cutout_generation()) already rides along inside this same
    # mech dict, so the report reaches the handoff surface for free,
    # same "one dict already carries it" shape mech["housing"]/
    # mech["supports"]/mech["cutouts"] themselves already rely on here.
    custom["mech"] = spec.get("mech", {})
    custom["instructions"] = spec.get("instructions", {})
    custom["info"] = spec.get("info", {"summary": "", "tags": [], "image_url": ""})
    custom["datasheets"] = spec.get("datasheets", {})
    workspace_facts.set_facts(workspace_id, {"custom": custom})
    print(f"  [hardware_speccer] wrote device spec to workspace_id={workspace_id!r} "
          f"(session_id={session_id!r}, {len(custom['parts'])} parts)")

    workspace_facts.record_section_entries(
      workspace_id,
      "hardware",
      [
        {
          "key": part.get("id") or part.get("name") or f"part_{index}",
          "title": part.get("name") or part.get("id") or f"Part {index + 1}",
          "summary": f"{part.get('category') or 'module'} ×{part.get('qty') or 1}",
          "data": part,
        }
        for index, part in enumerate(spec.get("parts", []))
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="parts",
    )
    workspace_facts.record_section_entries(
      workspace_id,
      "components",
      [
        {
          "key": node.get("id") or f"node_{index}",
          "title": node.get("label") or node.get("id") or f"Node {index + 1}",
          "summary": node.get("type") or node.get("kind") or "component",
          "data": node,
        }
        for index, node in enumerate(spec.get("wiring", {}).get("nodes", []))
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="wiring_nodes",
    )
    workspace_facts.record_section_entries(
      workspace_id,
      "connections",
      [
        {
          "key": f"{edge.get('from') or 'from'}->{edge.get('to') or 'to'}:{edge.get('kind') or 'link'}",
          "title": f"{edge.get('from') or '?'} -> {edge.get('to') or '?'}",
          "summary": edge.get("kind") or "connection",
          "data": edge,
        }
        for edge in spec.get("wiring", {}).get("edges", [])
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="wiring_edges",
    )
    workspace_facts.record_section_entries(
      workspace_id,
      "instructions",
      [
        {
          "key": phase.get("id") or phase.get("name") or f"phase_{index}",
          "title": phase.get("name") or phase.get("id") or f"Phase {index + 1}",
          "summary": f"{len(phase.get('steps', []))} step(s)",
          "data": phase,
        }
        for index, phase in enumerate(spec.get("instructions", {}).get("phases", []))
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="instructions",
    )
    workspace_facts.record_section_entries(
      workspace_id,
      "instructions",
      [
        {
          "key": step.get("id") or f"step_{phase_index}_{step_index}",
          "title": step.get("title") or step.get("id") or f"Step {step_index + 1}",
          "summary": "done" if step.get("done") else "pending",
          "data": {"phase_id": phase.get("id"), **step},
        }
        for phase_index, phase in enumerate(spec.get("instructions", {}).get("phases", []))
        for step_index, step in enumerate(phase.get("steps", []))
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="instruction_steps",
    )

    emit_event(EventType.DEVICE_SPEC, session_id, agent="hardware_speccer",
               payload={"part_count": len(spec.get("parts", []))})
    return {"text": json.dumps(spec), "spec": spec}


if __name__ == "__main__":
    print(json.dumps(run_hardware_speccer(session_id="local-test", workspace_id="local-test-ws"), indent=2))