"use client";
import { useState } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Edges } from "@react-three/drei";
import { Eye, EyeOff } from "lucide-react";

// Same category palette as PartsTable.jsx's CATEGORY_COLORS and
// WiringGraph.jsx's TYPE_COLORS, in hex here since three.js materials
// take hex/CSS color strings, not Tailwind classes -- kept as its own
// constant rather than importing from either sibling for the same reason
// PartsTable.jsx didn't import WiringGraph.jsx's: no other coupling
// between these components, and the values need to match by convention,
// not by shared import.
const CATEGORY_COLORS = {
  mcu: "#22d3ee",
  sensor: "#60a5fa",
  actuator: "#fb923c",
  power: "#fbbf24",
  module: "#c084fc",
  "3D_PRINT": "#34d399",
  MISC: "#f472b6",
};
const DEFAULT_COLOR = "#999999";

// A part reads as "the enclosure shell" (housing/lid) rather than a
// component sitting inside it when its own footprint covers most of the
// enclosure's footprint on at least two axes -- same idea
// hardware_speccer.py's own prompt uses ("the housing's placement should
// span the full enclosure footprint"), just checked geometrically here
// instead of trusting an id/name convention. Used below to render shells
// even more faintly than regular parts (they're context, not content),
// and left as its own named threshold so a future click-through-shell
// feature (selection patch) can reuse the same test rather than
// reinventing it.
const SHELL_COVERAGE_RATIO = 0.85;
function isShellPlacement(placement, enclosure) {
  const axes = [
    [placement.w, enclosure.w],
    [placement.h, enclosure.h],
    [placement.d, enclosure.d],
  ];
  const coveredAxes = axes.filter(([size, total]) => total > 0 && size / total >= SHELL_COVERAGE_RATIO).length;
  return coveredAxes >= 2;
}

/**
 * PrimitiveGeometry — G1c (Master Guide, "G1. Real component
 * measurements" -- shape-aware single-primitive rendering): picks the
 * actual three.js geometry for a placement's `shape` field
 * (agents/hardware_speccer.py's _apply_placement_shapes() sets this
 * from a G1a curated-table match -- "box"/"cylinder"/"cone", the same
 * three Level-0 primitives G3/G4's own tree names) instead of always
 * drawing a box. A placement with no `shape` (no G1a match, or a
 * pre-G1c spec) still falls through to the "box" case below, so this
 * is purely additive -- nothing that used to render correctly stops
 * rendering correctly.
 *
 * Reuses the placement's own bounding-box w/h/d rather than needing a
 * separate radius/height field on the placement: for a cylinder or
 * cone, radius is derived from `w` (the curated table's own axis
 * convention for round shapes is "w=diameter"), height from `h` --
 * same "reuse w/h/d as-is, no axis reinterpretation" precedent
 * hardware_speccer.py's dimensions_mm merge already set for boxes.
 * `d` goes unused for round shapes, matching the curated table's own
 * "d=always null" convention for them -- not a bug, just an axis a
 * round cross-section doesn't have.
 */
function PrimitiveGeometry({ shape, size }) {
  const [w, h, d] = size;
  const radius = Math.max(w, 1) / 2;
  switch (shape) {
    case "cylinder":
      return <cylinderGeometry args={[radius, radius, Math.max(h, 1), 24]} />;
    case "cone":
      return <coneGeometry args={[radius, Math.max(h, 1), 24]} />;
    case "box":
    default:
      return <boxGeometry args={[w, h, d]} />;
  }
}

/**
 * PrimitiveBox — fill + edge-outline rendering for a single primitive
 * volume (box, cylinder, or cone as of G1c -- the name predates
 * shape-awareness and is kept as-is to avoid a wider rename; think of
 * it as "the one shape a part currently renders as," not literally
 * "always a box" anymore). Deliberately split out of PartBox (which
 * still owns the placement -> center-coordinate math) so it's the one
 * place that knows how to draw "one solid-ish volume with a crisp
 * outline" -- when G3/G4 lands `placement.primitives` (a part
 * composed of several primitives instead of always exactly one),
 * PartBox becomes "loop over primitives, render one of these per
 * entry" instead of needing a rewrite of the render styling itself.
 *
 * FIX (opacity/legibility): the old render was a single
 * `meshStandardMaterial` at opacity 0.75 with no edge treatment, so once
 * parts nested inside the housing's shell (correct placement, per the
 * two prior Mech-view patches), everything alpha-blended into one
 * indistinct green mass -- the housing's fill visually swallowed
 * whatever sat inside it. Splitting fill and edges apart fixes this:
 * a near-transparent fill (so the volume still reads as "occupying that
 * space", especially for shells like the housing) plus a bright,
 * category-colored `<Edges>` outline (real edge geometry via
 * THREE.EdgesGeometry, not the boxGeometry's own triangulated wireframe,
 * which draws a diagonal cross through every face). Shells get an even
 * lower fill opacity than regular parts, since they're the context
 * things sit inside, not a component of interest themselves.
 */
function PrimitiveBox({ shape, size, color, isShell, selected }) {
  const fillOpacity = isShell ? 0.04 : 0.16;
  const edgeOpacity = isShell ? 0.35 : 1;
  return (
    <>
      <PrimitiveGeometry shape={shape} size={size} />
      <meshStandardMaterial
        color={color}
        transparent
        opacity={selected ? fillOpacity * 2.5 : fillOpacity}
        depthWrite={false}
      />
      <Edges
        color={selected ? "#ffffff" : color}
        linewidth={selected ? 2.5 : isShell ? 1 : 1.5}
        transparent
        opacity={selected ? 1 : edgeOpacity}
      />
    </>
  );
}

// G3a (Master Guide, Level 0->1 primitive composition): a light-vs-dark
// split of one category color for a primitive's own `color_role` --
// "primary" (the main body) gets the category color as-is, "accent"
// (mounting holes, small trim pieces) gets a darkened version of the
// SAME color rather than an unrelated second color, so a composed part
// still reads as one category at a glance (per the guide's own wording)
// instead of looking like two different parts glued together. Darkened
// rather than lightened because accents (holes) read as "cut into" the
// body, not "sitting on top of" it -- a lighter accent tends to look
// like a highlight/spotlight instead.
function shadeForRole(colorHex, role) {
  if (role !== "accent") return colorHex;
  const hex = colorHex.replace("#", "");
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  const darken = (c) => Math.round(c * 0.55);
  const toHex = (c) => c.toString(16).padStart(2, "0");
  return `#${toHex(darken(r))}${toHex(darken(g))}${toHex(darken(b))}`;
}

/**
 * PrimitivePiece — one entry of a part's `placement.primitives[]`
 * (G3a). Each primitive's `offset`/`size` are corner-origin *within
 * the part's own w/h/d box* (same corner-origin convention the whole
 * placement scheme already uses, per PartBox's own doc comment below)
 * -- so a primitive's local position here is computed the same
 * corner-to-center way PartBox computes the part's own position
 * relative to the enclosure, just one level further in: relative to
 * the part's own box center instead of the enclosure's.
 */
function PrimitivePiece({ primitive, partSize, color, isShell, selected }) {
  const [pw, ph, pd] = partSize;
  const offset = primitive.offset || { x: 0, y: 0, z: 0 };
  const size = primitive.size || { w: pw, h: ph, d: pd };
  const localX = offset.x + size.w / 2 - pw / 2;
  const localY = offset.y + size.h / 2 - ph / 2;
  const localZ = offset.z + size.d / 2 - pd / 2;
  const rotation = primitive.rotation || { x: 0, y: 0, z: 0 };
  const pieceColor = shadeForRole(color, primitive.color_role);
  return (
    <mesh
      position={[localX, localY, localZ]}
      rotation={[rotation.x || 0, rotation.y || 0, rotation.z || 0]}
      raycast={isShell ? () => null : undefined}
    >
      <PrimitiveBox
        shape={primitive.shape}
        size={[size.w, size.h, size.d]}
        color={pieceColor}
        isShell={isShell}
        selected={selected}
      />
    </mesh>
  );
}

function PartBox({ placement, part, enclosure, selected, onSelect, onHover }) {
  const color = CATEGORY_COLORS[part?.category] || DEFAULT_COLOR;
  const isShell = isShellPlacement(placement, enclosure);
  // BUG FIX (prior patch): `placement.x/y/z` is a corner coordinate --
  // per hardware_speccer.py's own SYSTEM_PROMPT_WIRING, the housing
  // starts "at z: 0", and the lid's z is set to "the housing's own d" so
  // it stacks directly atop it. That's a corner-origin scheme (like a
  // CSS box model), not a center-origin one.
  //
  // But <mesh position={...}> places a box's CENTER, not its corner, and
  // the enclosure wireframe just above is drawn with no position prop --
  // i.e. centered at the scene origin, spanning
  // [-enclosure.w/2, +enclosure.w/2] on each axis. Passing a raw corner
  // coordinate straight into `position` was double-wrong: every box's
  // center landed exactly at its own corner (offsetting it by half its
  // own size), *and* that corner was measured from the wrong origin
  // (the enclosure's corner, not its center).
  //
  // Fix: shift each box by half its own size (corner -> center of that
  // box) and re-base by half the enclosure's own size (corner-origin
  // space -> the enclosure-centered space the wireframe is actually
  // drawn in).
  const centerX = placement.x + placement.w / 2 - enclosure.w / 2;
  const centerY = placement.y + placement.h / 2 - enclosure.h / 2;
  const centerZ = placement.z + placement.d / 2 - enclosure.d / 2;
  const hasPrimitives = Array.isArray(placement.primitives) && placement.primitives.length > 0;

  const sharedGroupProps = {
    position: [centerX, centerY, centerZ],
    // Shells (housing/lid) fully enclose whatever sits inside them, so
    // a naive raycast would hit the shell's near wall before it ever
    // reaches the part behind it -- clicking "into" the housing would
    // just keep selecting the housing. Disabling raycasting on shell
    // meshes/groups lets clicks pass through to whatever's actually
    // inside; shells remain selectable, just only from the sidebar
    // (patch 6), which is where you'd realistically go to select "the
    // housing" as a whole anyway.
    onClick: (e) => {
      e.stopPropagation();
      onSelect(placement.part_id);
    },
    // G3j: same click-through-shell reasoning applies to hover -- a
    // shell shouldn't steal the confidence badge from whatever's really
    // under the pointer either, and since shell meshes already opt out
    // of raycasting below (the non-primitives branch's `raycast={...}`),
    // these two handlers simply never fire for a shell in that branch.
    // r3f's own pointer-events model already calls onPointerOut when a
    // pointer leaves a mesh (including via unmount/hide), so no separate
    // cleanup is needed here.
    onPointerOver: (e) => {
      e.stopPropagation();
      onHover(placement.part_id);
    },
    onPointerOut: (e) => {
      e.stopPropagation();
      onHover(null);
    },
  };

  // G3a: a part with `placement.primitives` (Level 0->1 composition --
  // see agents/hardware_speccer.py's _apply_primitive_composition())
  // renders as a <group> of several PrimitivePiece meshes instead of a
  // single <mesh>, so a composed part (e.g. a button's cone-on-a-box,
  // or a bracket with real mounting holes) reads as its own real shape
  // instead of one bounding box. A placement with no `primitives` (pre-
  // G3a spec, or a part G3a's deterministic path didn't cover) falls
  // straight through to the original single-box/cylinder/cone render
  // below, so nothing that used to render correctly stops rendering
  // correctly.
  if (hasPrimitives) {
    return (
      <group {...sharedGroupProps}>
        {placement.primitives.map((primitive, i) => (
          <PrimitivePiece
            key={i}
            primitive={primitive}
            partSize={[placement.w, placement.h, placement.d]}
            color={color}
            isShell={isShell}
            selected={selected}
          />
        ))}
      </group>
    );
  }

  return (
    <mesh {...sharedGroupProps} raycast={isShell ? () => null : undefined}>
      <PrimitiveBox
        shape={placement.shape}
        size={[placement.w, placement.h, placement.d]}
        color={color}
        isShell={isShell}
        selected={selected}
      />
    </mesh>
  );
}

/**
 * CONFIDENCE_META — G3j (Master Guide, "G3/G4. Hierarchical parallel
 * build + validate", the remaining "close the G1->G3 loop" frontend
 * refinement): display metadata for `part.dimension_confidence`, the
 * SAME vocabulary eo/mech_validator.py's own `_TOLERANCE_MM` already
 * uses ("verified" gets a strict 0-margin FreeCAD check, "typical" gets
 * a small clearance buffer) -- this is that same field, surfaced to a
 * human instead of just a geometry tolerance. Set on the PART (by
 * agents/hardware_speccer.py's G1a curated-table match or G1b DigiKey/
 * Mouser lookup), never the placement -- see eo/mech_validator.py's own
 * module docstring on why -- so this reads `part.dimension_confidence`,
 * not `placement.dimension_confidence`.
 *
 * A part with neither a G1a nor G1b hit never gets this field set at
 * all (falls through to LLM-estimated sizing, per _populate_dimensions()'s
 * own docstring) -- that's the `_default` entry below, deliberately
 * labeled "Estimated" rather than reusing eo/mech_validator.py's own
 * internal fallback label ("typical"), since that module's fallback is
 * about which tolerance buffer to apply when the field is UNWIRED on a
 * placement, not a claim about the part's real dimension provenance --
 * conflating the two here would tell a user "typical" (implying some
 * confidence) for a part whose size is actually a pure LLM guess.
 */
const CONFIDENCE_META = {
  verified: { color: "#34d399", label: "Verified dimensions" },
  typical: { color: "#fbbf24", label: "Typical dimensions" },
  _default: { color: "#6b7280", label: "Estimated dimensions" },
};

// AMBIGUOUS_META — fourth badge state for G1a's alias-collision
// surfacing (see agents/component_dimension_table.py's
// _load_table()/get_alias_collisions()). Takes priority over whatever
// CONFIDENCE_META entry the part's own dimension_confidence would
// otherwise pick: a "verified"-labeled match that was actually one of
// several components sharing an alias is exactly the confidently-
// wrong case this badge exists to flag, so it must outrank the normal
// verified/typical styling rather than being layered underneath it.
const AMBIGUOUS_META = {
  color: "#f87171",
  label: "Ambiguous match — dimensions may be from a different component",
};

/**
 * ConfidenceBadge — G3j's own payoff: a small floating badge over the
 * canvas naming whichever part is currently hovered or selected and how
 * trustworthy its dimensions are. `part` is `null` when nothing's
 * hovered/selected (nothing rendered then, see MechView's own guard
 * below) or when a hovered/selected part_id has no matching entry in
 * `parts` (shouldn't happen, per this component's own module docstring
 * on placement/part joins, but degrades to "not shown" rather than a
 * badge with blank text).
 */
function ConfidenceBadge({ part }) {
  if (!part) return null;
  const meta = part.dimension_ambiguous
    ? AMBIGUOUS_META
    : CONFIDENCE_META[part.dimension_confidence] || CONFIDENCE_META._default;
  return (
    <div
      className="pointer-events-none absolute left-2 top-2 z-10 flex items-center gap-1.5 rounded-full border border-[var(--neutral-800)] bg-black/70 px-2 py-1 text-[10px] text-[var(--neutral-200)] backdrop-blur-sm"
    >
      <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: meta.color }} />
      <span className="font-medium text-[var(--neutral-100)]">{part.name}</span>
      <span className="text-[var(--neutral-600)]">·</span>
      <span className="text-[var(--neutral-400)]">{meta.label}</span>
    </div>
  );
}

/**
 * PartsSidePanel — full BOM listing next to the 3D canvas (patch 5).
 * Deliberately iterates `parts` (device_spec.parts), not `placements`:
 * placements only cover parts hardware_speccer.py chose to lay out in
 * 3D space, but fasteners/adhesives/misc parts that never get a
 * placement still belong in a "what's in this build" reference. Rows
 * for those unplaced parts get a dimmed, disabled eye icon since
 * there's nothing in the canvas for them to toggle.
 */
function PartsSidePanel({ parts, placedPartIds, hiddenPartIds, onToggleHidden, selectedPartId, onSelect }) {
  return (
    <div className="h-[480px] w-56 shrink-0 flex flex-col rounded-lg border border-[var(--neutral-800)] overflow-hidden">
      <div className="shrink-0 px-2 py-1.5 border-b border-[var(--neutral-800)] text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">
        Parts ({parts.length})
      </div>
      <ul className="flex-1 overflow-y-auto divide-y divide-[var(--neutral-800)]">
        {parts.map((part) => {
          const isPlaced = placedPartIds.has(part.id);
          const isHidden = hiddenPartIds.has(part.id);
          const isSelected = part.id === selectedPartId;
          const color = CATEGORY_COLORS[part.category] || DEFAULT_COLOR;
          return (
            <li
              key={part.id}
              onClick={() => isPlaced && onSelect(part.id)}
              className={
                "flex items-center gap-2 h-8 px-2 text-xs text-[var(--neutral-300)] transition-colors" +
                (isPlaced ? " cursor-pointer hover:bg-[var(--neutral-900)]" : "") +
                (isSelected ? " bg-[var(--neutral-800)]" : "")
              }
            >
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: color }}
              />
              <span className={"flex-1 truncate" + (isPlaced ? "" : " text-[var(--neutral-600)]")}>
                {part.name}
              </span>
              <button
                type="button"
                disabled={!isPlaced}
                onClick={(e) => {
                  e.stopPropagation();
                  isPlaced && onToggleHidden(part.id);
                }}
                aria-label={isHidden ? `Show ${part.name}` : `Hide ${part.name}`}
                className={
                  isPlaced
                    ? "text-[var(--neutral-400)] hover:text-[var(--neutral-100)]"
                    : "text-[var(--neutral-700)] cursor-not-allowed"
                }
              >
                {isHidden ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/**
 * CategoryLegend — small swatch+label row under the canvas (patch 7),
 * so the box colors are self-explanatory without cross-referencing the
 * Parts tab. Only lists categories actually present in this spec's
 * parts list, rather than the full CATEGORY_COLORS map, so a simple
 * build doesn't show a legend full of categories it doesn't use.
 */
function CategoryLegend({ parts }) {
  const presentCategories = [...new Set(parts.map((p) => p.category).filter(Boolean))];
  if (presentCategories.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-1">
      {presentCategories.map((category) => (
        <div key={category} className="flex items-center gap-1.5">
          <span
            className="h-2 w-2 shrink-0 rounded-full"
            style={{ backgroundColor: CATEGORY_COLORS[category] || DEFAULT_COLOR }}
          />
          <span className="text-[10px] text-[var(--neutral-500)]">{category}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * MechView — last of Blueprint's four sub-views (Blueprint design guide
 * §4). Not real CAD -- labeled boxes inside a wireframe enclosure hull,
 * same honesty framing as PartsTable's price estimates: "which part
 * roughly goes where," not engineering-grade placement. Requires
 * `npm install three @react-three/fiber @react-three/drei` (free,
 * MIT-licensed).
 *
 * `mech`: device_spec.mech -- {enclosure: {w,h,d}, placements: [{part_id,
 * x,y,z,w,h,d,shape?,primitives?}]}, from GET /api/workspaces/{ws_id}/
 * device-spec's `mech` slice. `shape` (G1c, optional -- "box"/
 * "cylinder"/"cone") is only present on a placement whose part matched
 * a real shape in G1a's curated dimension table; a placement with no
 * `shape` still renders as a box, exactly as before G1c. `primitives`
 * (G3a, optional -- a list of {offset,size,rotation,shape,color_role}
 * entries local to the part's own w/h/d box) is only present on a
 * placement whose part had real dimensions_mm resolved (G1a or G1b) by
 * the time agents/hardware_speccer.py's _apply_primitive_composition()
 * ran; a placement with no `primitives` falls straight through to the
 * single-shape (`shape`) render, exactly as before G3a. agents/hardware_speccer.py's own
 * system prompt tells the model to propose a rough grid layout only (power/MCU near center, sensors
 * near the hull edges), never precise millimeter placement -- this
 * component just renders whatever placements it's handed, correct or
 * rough, without trying to validate or auto-arrange them.
 * `parts`: device_spec.parts, joined against placements by part_id so
 * each box can pick its category color -- a placement with no matching
 * part (shouldn't happen, since hardware_speccer.py's prompt requires
 * every placement's part_id to reference a real part) falls back to
 * DEFAULT_COLOR rather than erroring. This same join is also G3j's own
 * source for `part.dimension_confidence` ("verified"/"typical", set by
 * agents/hardware_speccer.py's G1a/G1b -- absent entirely means an
 * LLM-estimated size, no real match at all): hovering or selecting a
 * part in the canvas shows ConfidenceBadge below, so a rough LLM guess
 * never looks as trustworthy on screen as a real measured/datasheet
 * dimension.
 */
export default function MechView({ mech, parts }) {
  const enclosure = mech?.enclosure || { w: 100, h: 60, d: 40 };
  const placements = mech?.placements || [];
  const allParts = parts || [];
  const partsById = Object.fromEntries(allParts.map((p) => [p.id, p]));
  const placedPartIds = new Set(placements.map((pl) => pl.part_id));

  // Pure frontend state, keyed by part_id -- part_id stays stable across
  // future Phase-G changes (per earlier discussion), so this set never
  // needs to be reconciled against placement/index changes elsewhere.
  const [hiddenPartIds, setHiddenPartIds] = useState(() => new Set());
  // Single source of truth for "what's selected", settable two ways:
  // clicking a mesh in the 3D view (r3f's built-in raycasting `onClick`
  // on each PartBox) or clicking a row in the sidebar. Both funnel into
  // the same setter below.
  const [selectedPartId, setSelectedPartId] = useState(null);
  // G3j: hover is its own, separate bit of state from selection -- a
  // hover is transient (only the mesh currently under the pointer) and
  // should win over a sticky selection for badge purposes, but must
  // never clobber `selectedPartId` itself (moving the mouse away from a
  // selected part should fall back to showing the selection's badge,
  // not clear the selection). Only ever set from PartBox's own
  // onPointerOver/onPointerOut (canvas hover) -- PartsSidePanel rows
  // don't have a natural "hover" concept of their own here since they
  // already show a click-to-select affordance.
  const [hoveredPartId, setHoveredPartId] = useState(null);

  function toggleHidden(partId) {
    setHiddenPartIds((prev) => {
      const next = new Set(prev);
      if (next.has(partId)) {
        next.delete(partId);
      } else {
        next.add(partId);
        // Edge case (patch 7): if the part being hidden is currently
        // selected, clear the selection rather than leaving a stale
        // highlighted sidebar row pointing at something no longer on
        // screen -- there'd be nothing in the canvas to show as
        // selected anymore.
        setSelectedPartId((sel) => (sel === partId ? null : sel));
        // G3j: same reasoning for hover -- hiding a mesh mid-hover
        // isn't guaranteed to fire r3f's onPointerOut (the mesh is
        // simply gone from the next render), so clear it explicitly
        // rather than risk a stuck badge for a part no longer visible.
        setHoveredPartId((hov) => (hov === partId ? null : hov));
      }
      return next;
    });
  }

  function selectPart(partId) {
    setSelectedPartId((prev) => (prev === partId ? prev : partId));
  }

  if (placements.length === 0) {
    return (
      <p className="text-xs text-[var(--neutral-600)]">
        No physical layout proposed yet.
      </p>
    );
  }

  const visiblePlacements = placements.filter((pl) => !hiddenPartIds.has(pl.part_id));

  // G3j: hover wins over selection for badge purposes (see
  // `hoveredPartId`'s own state comment above) -- whichever part_id is
  // "active" right now, resolved to its real `parts` entry so the badge
  // has a name/dimension_confidence to show. `null` (nothing hovered or
  // selected) falls through to ConfidenceBadge's own no-op guard.
  const badgePartId = hoveredPartId ?? selectedPartId;
  const badgePart = badgePartId ? partsById[badgePartId] : null;

  // G3a visual refinement: frame the camera off the enclosure's own
  // bounding sphere (half its space diagonal) instead of the old fixed
  // per-axis `*1.5` multiplier. A fixed per-axis multiplier frames a
  // roughly-cubic enclosure fine, but composed multi-primitive parts
  // make larger or oddly-proportioned enclosures (e.g. long and thin)
  // common enough that the old approach could crop or under-fill the
  // view depending on which axis was largest. The bounding sphere is
  // shape-agnostic -- one number that always fully contains the
  // enclosure regardless of its aspect ratio -- so the camera distance
  // derived from it frames consistently either way. Placed along the
  // same [1,1,1] diagonal direction the old fixed camera used, just at
  // a distance proportional to the enclosure's own size instead of a
  // per-axis guess.
  const boundingRadius = Math.sqrt(enclosure.w ** 2 + enclosure.h ** 2 + enclosure.d ** 2) / 2;
  const cameraDistance = boundingRadius * 2.2;
  const diagonalUnit = 1 / Math.sqrt(3);
  const cameraPosition = [
    cameraDistance * diagonalUnit,
    cameraDistance * diagonalUnit,
    cameraDistance * diagonalUnit,
  ];

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <div className="relative h-[480px] flex-1 rounded-lg border border-[var(--neutral-800)] overflow-hidden bg-black/30">
          <ConfidenceBadge part={badgePart} />
          <Canvas
            camera={{ position: cameraPosition, fov: 45 }}
            // Standard r3f pattern: fires when a click doesn't land on
            // any mesh (i.e. empty space in the canvas), so clicking
            // away from a part deselects it -- no extra library needed.
            onPointerMissed={() => setSelectedPartId(null)}
          >
            <ambientLight intensity={0.6} />
            <pointLight position={[10, 10, 10]} intensity={0.8} />
            {/* FIX: was `meshBasicMaterial wireframe` directly on a
                boxGeometry, which renders three.js's own triangulated
                wireframe -- a diagonal cross through every face, not a
                clean rectangular hull outline. `<Edges>` draws only the
                box's true 12 edges. */}
            <mesh>
              <boxGeometry args={[enclosure.w, enclosure.h, enclosure.d]} />
              <meshBasicMaterial transparent opacity={0} depthWrite={false} />
              <Edges color="#5b6472" linewidth={1} />
            </mesh>
            {visiblePlacements.map((pl) => (
              <PartBox
                key={pl.part_id}
                placement={pl}
                part={partsById[pl.part_id]}
                enclosure={enclosure}
                selected={pl.part_id === selectedPartId}
                onSelect={selectPart}
                onHover={setHoveredPartId}
              />
            ))}
            <OrbitControls />
          </Canvas>
        </div>
        <PartsSidePanel
          parts={allParts}
          placedPartIds={placedPartIds}
          hiddenPartIds={hiddenPartIds}
          onToggleHidden={toggleHidden}
          selectedPartId={selectedPartId}
          onSelect={selectPart}
        />
      </div>
      <CategoryLegend parts={allParts} />
      <p className="text-[10px] text-[var(--neutral-600)] px-1">
        Rough layout, not engineering-grade CAD — drag to orbit.
      </p>
    </div>
  );
}
