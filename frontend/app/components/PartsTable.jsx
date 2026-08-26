"use client";
import { RefreshCw, BadgeCheck, AlertTriangle } from "lucide-react";

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
  "3D_PRINT": "text-emerald-300 border-emerald-500/40",
  MISC: "text-pink-300 border-pink-500/40",
};
const DEFAULT_CATEGORY_COLOR = "text-neutral-400 border-neutral-700";

// F3 Part 6: part.source -> display label for the "real spec" badge
// below. Absent/unrecognized source (the common case -- most parts
// have no part_number, or their part_number missed on both vendors)
// means no badge at all, i.e. "LLM-estimated," which is the majority
// case and deliberately gets no visual noise of its own.
const SOURCE_LABELS = {
  digikey: "DigiKey",
  mouser: "Mouser",
};

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
 * F3 Part 4/6: a part may also carry `dimensions_mm`, `datasheet_url`,
 * and `source` ("digikey"/"mouser") when agents/component_spec_lookup.py
 * found a real distributor match for that part's part_number -- absent
 * on any part that had no part_number, or whose part_number missed on
 * both vendors, in which case its sizing was LLM-estimated instead.
 * `source` is surfaced here as a small badge (see SOURCE_LABELS above);
 * `dimensions_mm` isn't rendered by this table today -- MechView.jsx is
 * where physical sizing actually matters.
 *
 * Patch K.2: a part may also carry `price_source` ("market_listing" |
 * "estimated_print_cost") -- an "estimated_print_cost" part
 * (agents/eo/mech_material.py's deterministic 3D-print cost estimate)
 * has no real vendor, so `vendor_url`/`vendor_name` are always null for
 * it; this table shows a plain (non-link) "Est. print cost" label for
 * that case instead of the vendor-link treatment below, rather than
 * rendering a dead link to nowhere with a blank vendor line under it.
 *
 * Patch K.3: a part may also carry `price_flagged` (bool) +
 * `price_flag_reason` (string) -- eo/price_outliers.py flags (never
 * drops) a price that's a >5x outlier against its own category's
 * median, or that's missing while a same-part "other side" sibling
 * (e.g. "Left"/"Right" Motor Mounting Bracket) has one. A flagged
 * part's price is EXCLUDED from `total` below (the guide's own "flag...
 * rather than folding them into the total estimated cost at face
 * value" wording) and shown with a "verify" badge instead of being
 * silently trusted.
 *
 * `onRefreshPrices`: called with no args, expected to hit the
 * /refresh-prices endpoint and hand back updated parts; caller (Blueprint
 * View) owns the resulting setSpec.
 * `refreshing`: bool, disables the button and spins its icon mid-request.
 */
export default function PartsTable({ parts, onRefreshPrices, refreshing }) {
  // Bug fix (T2b, step 17): the old `|| 0` guard only caught falsy
  // values (null/0/undefined) — a stray non-numeric price_bdt (e.g. a
  // string that slipped past the backend) turned the whole total into
  // NaN. Number.isFinite() treats anything non-numeric the same as
  // "unpriced", same as null already is.
  //
  // Patch K.3: a flagged part's price is never folded into this total
  // at face value -- same treatment an unpriced part already gets
  // (contributes 0), not "trust it but mark it" -- the guide is
  // explicit that a flagged figure shouldn't count toward the total a
  // user reads as trustworthy.
  const total = parts.reduce((sum, p) => {
    if (p.price_flagged) return sum;
    const price = Number(p.estimated_price_bdt);
    return sum + (Number.isFinite(price) ? price : 0) * p.qty;
  }, 0);
  const uncheckedCount = parts.filter((p) => !p.estimated_price_bdt).length;
  const flaggedCount = parts.filter((p) => p.price_flagged).length;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">
          {parts.length} part{parts.length === 1 ? "" : "s"}
          {uncheckedCount > 0 && ` · ${uncheckedCount} unpriced`}
          {flaggedCount > 0 && ` · ${flaggedCount} to verify`}
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
                {p.source && SOURCE_LABELS[p.source] && (
                  // F3 Part 6: small, visible payoff for Part 4/5's real
                  // distributor lookup -- badge next to the name when
                  // this part's sizing came from DigiKey/Mouser rather
                  // than LLM estimation. Links out to the datasheet when
                  // present (Part 1-2's lookup), otherwise it's still a
                  // useful non-link signal on its own.
                  p.datasheet_url ? (
                    <a
                      href={p.datasheet_url}
                      target="_blank"
                      rel="noreferrer"
                      title={`Verified via ${SOURCE_LABELS[p.source]} — view datasheet`}
                      className="flex items-center gap-0.5 text-[9px] uppercase border rounded px-1 text-emerald-300 border-emerald-500/40 hover:bg-emerald-500/10 shrink-0"
                    >
                      <BadgeCheck size={9} />
                      {SOURCE_LABELS[p.source]}
                    </a>
                  ) : (
                    <span
                      title={`Verified via ${SOURCE_LABELS[p.source]}`}
                      className="flex items-center gap-0.5 text-[9px] uppercase border rounded px-1 text-emerald-300 border-emerald-500/40 shrink-0"
                    >
                      <BadgeCheck size={9} />
                      {SOURCE_LABELS[p.source]}
                    </span>
                  )
                )}
                {p.price_flagged && (
                  // Patch K.3: "price may be inaccurate — verify" badge,
                  // per the guide's own exact wording. Title carries the
                  // specific reason (outlier ratio, or which sibling has
                  // the price) rather than a generic tooltip, so hovering
                  // tells the user WHY without needing a separate detail
                  // view.
                  <span
                    title={p.price_flag_reason || "Price may be inaccurate — verify"}
                    className="flex items-center gap-0.5 text-[9px] uppercase border rounded px-1 text-amber-300 border-amber-500/40 shrink-0"
                  >
                    <AlertTriangle size={9} />
                    Verify
                  </span>
                )}
              </div>
              {p.description && (
                <p className="text-[10px] text-[var(--neutral-600)] truncate">{p.description}</p>
              )}
            </div>
            <span className="text-xs text-[var(--neutral-500)] shrink-0">×{p.qty}</span>
            <div className="text-right shrink-0 w-28">
              {p.estimated_price_bdt ? (
                p.price_source === "estimated_print_cost" ? (
                  // Patch K.2 fix (surfaced while wiring K.3's badge into
                  // this same cell): an estimated_print_cost part has no
                  // real vendor_url/vendor_name -- the pre-K.2 vendor-link
                  // treatment below rendered a dead `href={null}` link
                  // with a blank vendor line under it for every 3D_PRINT
                  // part. Plain, non-link text instead, same "not
                  // hyperlinked" treatment the "not found" branch already
                  // uses below for a genuinely unpriced part.
                  <>
                    <span className={`text-xs ${p.price_flagged ? "text-amber-300" : "text-[var(--neutral-200)]"}`}>
                      ৳{Number(p.estimated_price_bdt).toLocaleString()}
                    </span>
                    <p className="text-[9px] text-[var(--neutral-600)] truncate">Est. print cost</p>
                  </>
                ) : (
                  <>
                    <a
                      href={p.vendor_url}
                      target="_blank"
                      rel="noreferrer"
                      className={`text-xs hover:underline ${p.price_flagged ? "text-amber-300" : "text-[var(--cyber-cyan)]"}`}
                    >
                      ৳{Number(p.estimated_price_bdt).toLocaleString()}
                    </a>
                    <p className="text-[9px] text-[var(--neutral-600)] truncate">{p.vendor_name}</p>
                  </>
                )
              ) : (
                <span className="text-[10px] text-[var(--neutral-700)]">
                  {p.price_flagged ? "verify — no price" : "not found"}
                </span>
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
