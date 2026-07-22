"use client";
import { Canvas } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";

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
};
const DEFAULT_COLOR = "#999999";

function PartBox({ placement, part }) {
  const color = CATEGORY_COLORS[part?.category] || DEFAULT_COLOR;
  return (
    <mesh position={[placement.x, placement.y, placement.z]}>
      <boxGeometry args={[placement.w, placement.h, placement.d]} />
      <meshStandardMaterial color={color} transparent opacity={0.75} />
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
          <mesh>
            <boxGeometry args={[enclosure.w, enclosure.h, enclosure.d]} />
            <meshBasicMaterial color="#444444" wireframe />
          </mesh>
          {placements.map((pl) => (
            <PartBox key={pl.part_id} placement={pl} part={partsById[pl.part_id]} />
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
