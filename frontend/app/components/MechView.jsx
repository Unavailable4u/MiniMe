"use client";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Edges } from "@react-three/drei";

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
 * PrimitiveBox — fill + edge-outline rendering for a single box-shaped
 * volume. Deliberately split out of PartBox (which still owns the
 * placement -> center-coordinate math) so it's the one place that knows
 * how to draw "one solid-ish volume with a crisp outline" -- when G3/G4
 * lands `placement.primitives` (a part composed of several primitives
 * instead of always exactly one box), PartBox becomes "loop over
 * primitives, render one of these per entry" instead of needing a
 * rewrite of the render styling itself.
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
function PrimitiveBox({ size, color, isShell, selected }) {
  const fillOpacity = isShell ? 0.04 : 0.16;
  const edgeOpacity = isShell ? 0.35 : 1;
  return (
    <>
      <boxGeometry args={size} />
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

function PartBox({ placement, part, enclosure }) {
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
  return (
    <mesh position={[centerX, centerY, centerZ]}>
      <PrimitiveBox size={[placement.w, placement.h, placement.d]} color={color} isShell={isShell} />
    </mesh>
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
 * x,y,z,w,h,d}]}, from GET /api/workspaces/{ws_id}/device-spec's `mech`
 * slice. agents/hardware_speccer.py's own system prompt tells the model
 * to propose a rough grid layout only (power/MCU near center, sensors
 * near the hull edges), never precise millimeter placement -- this
 * component just renders whatever placements it's handed, correct or
 * rough, without trying to validate or auto-arrange them.
 * `parts`: device_spec.parts, joined against placements by part_id so
 * each box can pick its category color -- a placement with no matching
 * part (shouldn't happen, since hardware_speccer.py's prompt requires
 * every placement's part_id to reference a real part) falls back to
 * DEFAULT_COLOR rather than erroring.
 */
export default function MechView({ mech, parts }) {
  const enclosure = mech?.enclosure || { w: 100, h: 60, d: 40 };
  const placements = mech?.placements || [];
  const partsById = Object.fromEntries((parts || []).map((p) => [p.id, p]));

  if (placements.length === 0) {
    return (
      <p className="text-xs text-[var(--neutral-600)]">
        No physical layout proposed yet.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <div className="h-[480px] rounded-lg border border-[var(--neutral-800)] overflow-hidden bg-black/30">
        <Canvas camera={{ position: [enclosure.w * 1.5, enclosure.h * 1.5, enclosure.d * 1.5], fov: 45 }}>
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
          {placements.map((pl) => (
            <PartBox key={pl.part_id} placement={pl} part={partsById[pl.part_id]} enclosure={enclosure} />
          ))}
          <OrbitControls />
        </Canvas>
      </div>
      <p className="text-[10px] text-[var(--neutral-600)] px-1">
        Rough layout, not engineering-grade CAD — drag to orbit.
      </p>
    </div>
  );
}
