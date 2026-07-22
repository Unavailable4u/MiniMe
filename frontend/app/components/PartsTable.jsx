"use client";
import { RefreshCw } from "lucide-react";

// device_spec.parts[].category -> badge color. Same palette as
// WiringGraph.jsx's TYPE_COLORS (wiring nodes use the identical category
// set, per Blueprint §0's schema) -- kept as a separate constant rather
// than importing from WiringGraph.jsx since these are Tailwind classes,
// not hex values, and the two components have no other coupling.
const CATEGORY_COLORS = {
  mcu: "text-cyan-300 border-cyan-500/40",
  sensor: "text-blue-300 border-blue-500/40",
  actuator: "text-orange-300 border-orange-500/40",
  power: "text-amber-300 border-amber-500/40",
  module: "text-purple-300 border-purple-500/40",
};
const DEFAULT_CATEGORY_COLOR = "text-neutral-400 border-neutral-700";

/**
 * PartsTable — first of Blueprint's four sub-views (Blueprint design
 * guide §2). Purpose-built, not a reuse of ExtractionTableView.jsx's
 * generic string-cell grid: this needs category badges and a per-row
 * vendor link, closer to a parts/BOM table than a plain CSV view.
 *
 * `parts`: device_spec.parts as produced by agents/hardware_speccer.py --
 * {id, name, category, description, qty, estimated_price_bdt, vendor_name,
 * vendor_url, price_checked_at}. Every price field here is either null
 * (never priced / lookup found nothing) or a single already-resolved
 * figure -- agents/hardware_speccer.py's _select_best_listing() (and the
 * /refresh-prices endpoint, which uses the same rule) already collapsed
 * agents/part_price_finder.py's multi-vendor listings down to one before
 * this component ever sees the part, so there's no listings array to
 * render here.
 *
 * `onRefreshPrices`: called with no args, expected to hit the
 * /refresh-prices endpoint and hand back updated parts; caller (Blueprint
 * View) owns the resulting setSpec.
 * `refreshing`: bool, disables the button and spins its icon mid-request.
 */
export default function PartsTable({ parts, onRefreshPrices, refreshing }) {
  const total = parts.reduce((sum, p) => sum + (p.estimated_price_bdt || 0) * p.qty, 0);
  const uncheckedCount = parts.filter((p) => !p.estimated_price_bdt).length;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">
          {parts.length} part{parts.length === 1 ? "" : "s"}
          {uncheckedCount > 0 && ` · ${uncheckedCount} unpriced`}
        </span>
        <button
          onClick={onRefreshPrices}
          disabled={refreshing}
          className="flex items-center gap-1.5 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] disabled:opacity-50"
        >
          <RefreshCw size={12} className={refreshing ? "animate-spin" : ""} />
          {refreshing ? "Checking prices…" : "Refresh prices"}
        </button>
      </div>

      <div className="rounded-lg border border-[var(--neutral-800)] divide-y divide-[var(--neutral-900)]">
        {parts.map((p) => (
          <div key={p.id} className="flex items-center gap-3 px-3 py-2.5">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-xs text-[var(--neutral-100)] font-medium truncate">{p.name}</span>
                <span className={`text-[9px] uppercase border rounded px-1 ${CATEGORY_COLORS[p.category] || DEFAULT_CATEGORY_COLOR}`}>
                  {p.category}
                </span>
              </div>
              {p.description && (
                <p className="text-[10px] text-[var(--neutral-600)] truncate">{p.description}</p>
              )}
            </div>
            <span className="text-xs text-[var(--neutral-500)] shrink-0">×{p.qty}</span>
            <div className="text-right shrink-0 w-28">
              {p.estimated_price_bdt ? (
                <>
                  <a
                    href={p.vendor_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs text-[var(--cyber-cyan)] hover:underline"
                  >
                    ৳{Number(p.estimated_price_bdt).toLocaleString()}
                  </a>
                  <p className="text-[9px] text-[var(--neutral-600)] truncate">{p.vendor_name}</p>
                </>
              ) : (
                <span className="text-[10px] text-[var(--neutral-700)]">not found</span>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between px-1 text-xs">
        <span className="text-[var(--neutral-500)]">Total estimated cost</span>
        <span className="font-medium text-[var(--neutral-100)]">৳{total.toLocaleString()}</span>
      </div>
    </div>
  );
}
