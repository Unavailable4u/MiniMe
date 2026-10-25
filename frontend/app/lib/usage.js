// frontend/app/lib/usage.js
// Patch X02 (M2C build plan, WP X02 -- Usage panel).
//
// The pure, dependency-free logic behind components/UsagePanel.jsx, kept out of
// the component so plain `node` can test it (the component is JSX and pulls in
// React and the Supabase client, which `npm test` cannot load; see
// lib/__tests__/usage.test.mjs). No `import` lines on purpose: that test loads
// this file through lib/workbench/__tests__/loadSource.mjs with no `imports`
// map, so an added import fails loudly.
//
// WHAT THE REAL API LOOKS LIKE (backend/api/routes/usage.py and
// backend/eo/usage_ledger.py usage_summary(), patch S01 -- verified in the
// files, not taken from the plan):
//
//   GET /api/usage
//     -> {plan_key, status, pool, used, remaining, period_start, resets_at,
//         by_surface: {web, m2c, menux}}
//
//   Every amount is in CREDITS (one cost-weighted pool shared by the web app,
//   M2C and later Menux; build plan section O2). Users never see dollars, so
//   nothing here reads or prints a dollar field even if one were added.
//   `used` is SUM(credits) over the account's ledger rows in the period and is
//   NOT capped at `pool`; only `remaining` is (max(pool - used, 0)). Amounts
//   carry up to six decimals. `resets_at` is the end of the current period (an
//   ISO timestamp with offset, the first instant of the next UTC month for an
//   account on the default plan). `status` and `plan_key` come from the
//   account's plan row and are only used as a label.
//
//   Errors: 401 when the Supabase session is not accepted; the route itself
//   fails with the database's own error before migration 0012 is applied, and
//   the app answers 503 (+ Retry-After) when the database is unavailable. This
//   page words every failure with a fixed string and never echoes a body.
//
// THE FOURTH NUMBER IS NOT HERE. Rulebook section 7.2 item 5 and the build
// plan's "four numbers" wording end with "what a run costs roughly". GET
// /api/usage has no field for it, and this page does not invent one. It needs
// a small backend addition (a typical-run estimate from real metering data,
// after S10); until then the page shows used, left, when it resets and the bar.
//
// THE WARNING LINE. Rulebook section 7.2 item 5 and section 7.4 ("banner at
// 70% of a pool") say 70%. Build plan section 11.5 says 80% for the shared cap
// behaviour; the work package for this page says 70%, so 70% it is. It is one
// constant (WARNING_PERCENT) if the two documents are ever reconciled.
//
// ROUNDING RULES worth knowing before changing anything:
//   - The percentage shown is FLOORED, never rounded: the page must not say
//     "70%" while it is still in the normal state, nor "100%" while credits
//     remain. A pool that is full or gone reads 100.
//   - A tiny non-zero amount reads "<0.1", never "0", so "I used something"
//     and "I used nothing" cannot look the same.
//   - Comparisons against the warning line carry a tiny epsilon, because
//     29/100 * 100 is 28.999999999999996 in floating point.

// Rulebook section 7.2 item 5.
export const WARNING_PERCENT = 70;

// Float dust: 0.58 * 100 is 57.99999999999999. Far below the six decimals the
// ledger stores, far above floating-point error.
const EPSILON = 1e-9;

// The surfaces the server reports for an account, in display order. `always`
// rows are listed even at 0 (a person who has only used the web app should
// still see that M2C is a place their credits can go); the rest only once they
// are above zero. 'system' is never reported for an account (its rows have no
// owner) and is never listed.
export const SURFACE_DEFS = [
  { key: "web", label: "Web app", always: true },
  { key: "m2c", label: "M2C", always: true },
  { key: "menux", label: "Menux", always: false },
];

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const DASH = "—";

// ---------------------------------------------------------------------------
// Reading the response
// ---------------------------------------------------------------------------

function finiteNumber(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function nonNegative(v) {
  const n = finiteNumber(v);
  return n === null ? 0 : Math.max(n, 0);
}

function text(v) {
  return typeof v === "string" ? v.trim() : "";
}

/** A GET /api/usage body -> the numbers the page can trust, or null when the
 *  body is not usable. Usable means an object with a finite numeric `pool` and
 *  `used`; anything else is rejected rather than drawn half-way (a bar built on
 *  a guessed pool would be worse than an error). Negatives are clamped to 0, a
 *  missing or non-numeric `remaining` is derived from pool - used, a missing
 *  `by_surface` is all zeros, and only web / m2c / menux are kept. Unknown
 *  fields are dropped on purpose (see the header: credits only). Never throws. */
export function normalizeUsage(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const rawPool = finiteNumber(data.pool);
  const rawUsed = finiteNumber(data.used);
  if (rawPool === null || rawUsed === null) return null;
  const pool = Math.max(rawPool, 0);
  const used = Math.max(rawUsed, 0);
  const rawRemaining = finiteNumber(data.remaining);
  const remaining = rawRemaining === null ? Math.max(pool - used, 0) : Math.max(rawRemaining, 0);
  const surfaces = data.by_surface && typeof data.by_surface === "object" ? data.by_surface : {};
  const bySurface = {};
  for (const s of SURFACE_DEFS) bySurface[s.key] = nonNegative(surfaces[s.key]);
  return {
    planKey: text(data.plan_key),
    status: text(data.status),
    pool,
    used,
    remaining,
    periodStart: text(data.period_start) || null,
    resetsAt: text(data.resets_at) || null,
    bySurface,
  };
}

/** "free" -> "Free", "basic_annual" -> "Basic annual". "" when there is no
 *  plan key. A label only; nothing branches on it. */
export function planLabel(planKey) {
  const t = text(planKey).replace(/[_-]+/g, " ").toLowerCase();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : "";
}

// ---------------------------------------------------------------------------
// Percent and level
// ---------------------------------------------------------------------------

// used / pool as a percentage with float dust removed. Only meaningful for a
// positive pool (callers check).
function rawPercent(used, pool) {
  return (used * 100) / pool + EPSILON;
}

/** Whole percent of the pool used, 0..100, FLOORED. A pool of 0 (or anything
 *  that is not a positive number) has nothing left, so it reads 100 -- a full
 *  bar -- instead of dividing by zero. `used` above the pool (the server does
 *  not cap it) reads 100; a hair under the pool reads 99. Bad `used` (negative,
 *  NaN, missing) reads 0. Never throws, never returns NaN. */
export function percentUsed(used, pool) {
  if (!(typeof pool === "number" && pool > 0 && Number.isFinite(pool))) return 100;
  const u = typeof used === "number" && Number.isFinite(used) && used > 0 ? used : 0;
  if (u >= pool) return 100;
  return Math.max(0, Math.min(99, Math.floor(rawPercent(u, pool))));
}

/** "ok" | "warning" | "exhausted".
 *    exhausted: nothing left to spend -- the pool is 0, used has reached the
 *               pool, or the server's own `remaining` is 0. (The server's
 *               pre-flight check() allows a call only while remaining > 0, so
 *               a pool of 0 is exhausted by its own rule.)
 *    warning:   70% of the pool or more is used (at 70%, not after it).
 *    ok:        everything else.
 *  Takes {used, pool, remaining} as normalizeUsage returns them. */
export function usageLevel({ used, pool, remaining }) {
  if (!(pool > 0)) return "exhausted";
  if (used >= pool || (typeof remaining === "number" && remaining <= 0)) return "exhausted";
  return rawPercent(used, pool) >= WARNING_PERCENT ? "warning" : "ok";
}

// ---------------------------------------------------------------------------
// Numbers and dates, in the reader's locale
// ---------------------------------------------------------------------------

// Intl with a locale the runtime rejects throws RangeError; the page must not
// go blank over a locale string, so fall back to the runtime default, and
// failing that return null for the caller to handle.
function intl(Ctor, locale, options) {
  try {
    return new Ctor(locale, options);
  } catch {
    try {
      return new Ctor(undefined, options);
    } catch {
      return null;
    }
  }
}

function numberText(value, locale, maximumFractionDigits) {
  const nf = intl(Intl.NumberFormat, locale, { maximumFractionDigits });
  return nf ? nf.format(value) : String(Math.round(value));
}

/** A credit amount, without a unit, in the reader's locale: 20730 -> "20,730"
 *  (en-US) / "20.730" (de-DE), 12.34 -> "12.3", 312.4 -> "312". Under 100 keeps
 *  one decimal, 100 and over are whole numbers. Zero is "0"; anything above 0
 *  and under 0.1 is "<0.1" so it can never read as "nothing". Negative is 0.
 *  Not a finite number -> a dash, never "NaN". `locale` undefined means the
 *  runtime default (the browser's language). */
export function formatCredits(value, locale) {
  if (typeof value !== "number" || !Number.isFinite(value)) return DASH;
  const v = Math.max(value, 0);
  if (v === 0) return numberText(0, locale, 0);
  if (v < 0.1) return `<${numberText(0.1, locale, 1)}`;
  return numberText(v, locale, v < 100 ? 1 : 0);
}

/** formatCredits plus the unit: "81 credits", "1 credit", "<0.1 credits".
 *  Bad input is just the dash, with no unit after it. */
export function creditsText(value, locale) {
  const n = formatCredits(value, locale);
  if (n === DASH) return n;
  return `${n} ${n === formatCredits(1, locale) ? "credit" : "credits"}`;
}

/** The reset moment as a short date in the reader's own locale and time zone:
 *  "Nov 1", or "Jan 1, 2027" when it falls in a different year from `now`.
 *  The time zone is the reader's, not UTC: the server resets at the start of a
 *  UTC month, so a reader west of UTC sees the evening of the day before, and
 *  that is when it really happens for them. null for a missing, empty or
 *  unparseable value. A time zone the runtime rejects falls back to the plain
 *  UTC date (YYYY-MM-DD). `timeZone` and `now` are injectable for tests. */
export function formatResetDate(iso, { locale, timeZone, now = Date.now() } = {}) {
  if (typeof iso !== "string" || !iso.trim()) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  try {
    const tz = timeZone ? { timeZone } : {};
    const yearOf = (ms) => intl(Intl.DateTimeFormat, "en-US", { year: "numeric", ...tz }).format(ms);
    const options = { month: "short", day: "numeric", ...tz };
    if (yearOf(then) !== yearOf(now)) options.year = "numeric";
    const df = intl(Intl.DateTimeFormat, locale, options);
    return df ? df.format(then) : new Date(then).toISOString().slice(0, 10);
  } catch {
    return new Date(then).toISOString().slice(0, 10);
  }
}

/** How far away the reset is, in the reader's language: "in 7 days",
 *  "tomorrow", "in 2 hours", "in 59 minutes". Rounds UP so the page never
 *  promises a reset a little early, and never says "in 0 minutes". null when
 *  the time is missing, unparseable or not in the future. */
export function formatResetsIn(iso, { locale, now = Date.now() } = {}) {
  if (typeof iso !== "string" || !iso.trim()) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const diff = then - now;
  if (!(diff > 0)) return null;
  let value;
  let unit;
  if (diff < HOUR) {
    value = Math.max(1, Math.ceil(diff / MINUTE));
    unit = "minute";
  } else if (diff < DAY) {
    value = Math.ceil(diff / HOUR);
    unit = "hour";
  } else {
    value = Math.ceil(diff / DAY);
    unit = "day";
  }
  const rtf = intl(Intl.RelativeTimeFormat, locale, { numeric: "auto" });
  return rtf ? rtf.format(value, unit) : null;
}

// ---------------------------------------------------------------------------
// By surface
// ---------------------------------------------------------------------------

/** The by-surface breakdown as rows to render: web and M2C always, Menux only
 *  when its amount is above zero, nothing else ever. Each row is
 *  {key, label, credits, text}; `text` is the amount without a unit (the panel
 *  prints one "credits" heading for the list). Tolerates a missing or junk
 *  object (every amount reads 0). */
export function surfaceRows(bySurface, locale) {
  const src = bySurface && typeof bySurface === "object" ? bySurface : {};
  const rows = [];
  for (const s of SURFACE_DEFS) {
    const credits = nonNegative(src[s.key]);
    if (s.always || credits > 0) {
      rows.push({ key: s.key, label: s.label, credits, text: formatCredits(credits, locale) });
    }
  }
  return rows;
}

// ---------------------------------------------------------------------------
// The view model
// ---------------------------------------------------------------------------

/** A GET /api/usage body -> everything the panel prints, or null when the body
 *  is unusable (the panel then shows its "couldn't read" error). Fields:
 *    planLabel   "Basic" ("" when unknown)
 *    level       "ok" | "warning" | "exhausted"
 *    percent     0..100 whole number for the bar (floored; see percentUsed)
 *    showPercent false for a pool of 0, where "100% used" would be nonsense
 *    poolText / usedText / remainingText   "1,690 credits" etc. (credits only)
 *    resetDate   "Nov 1" or null when the server sent none
 *    resetsIn    "in 7 days" or null
 *    message     the line under the bar for the warning and exhausted states,
 *                and for an account that has not used anything yet; null for
 *                an ordinary account in the normal state
 *    noUsageYet  true for a normal-state account with nothing used
 *    surfaces    surfaceRows(...)
 *  `locale`, `timeZone` and `now` are injectable for tests. */
export function describeUsage(data, { locale, timeZone, now = Date.now() } = {}) {
  const u = normalizeUsage(data);
  if (!u) return null;
  const level = usageLevel(u);
  const percent = percentUsed(u.used, u.pool);
  const hasPool = u.pool > 0;
  const resetDate = formatResetDate(u.resetsAt, { locale, timeZone, now });
  const resetsIn = formatResetsIn(u.resetsAt, { locale, now });
  const resetSentence = resetDate ? ` They reset on ${resetDate}.` : "";
  const noUsageYet = level === "ok" && u.used === 0;

  let message = null;
  if (level === "exhausted") {
    message = hasPool
      ? `You've used all of your credits for this period.${resetSentence}`
      : "This plan has no credits to use.";
  } else if (level === "warning") {
    message = `You've used ${percent}% of your credits for this period.${resetSentence}`;
  } else if (noUsageYet) {
    message = "You haven't used any credits yet this period.";
  }

  return {
    planLabel: planLabel(u.planKey),
    level,
    percent,
    showPercent: hasPool,
    poolText: creditsText(u.pool, locale),
    usedText: creditsText(u.used, locale),
    remainingText: creditsText(u.remaining, locale),
    resetDate,
    resetsIn,
    message,
    noUsageYet,
    surfaces: surfaceRows(u.bySurface, locale),
  };
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** The text for a failed GET /api/usage. `status` is the HTTP status (0 or
 *  missing when fetch itself threw); `unreadable` is true when the response was
 *  a 200 whose body normalizeUsage rejected. Fixed strings only: nothing from a
 *  response is echoed. Always returns a non-empty string. */
export function usageErrorMessage({ status, unreadable } = {}) {
  if (unreadable) return "Your usage came back in a form we couldn't read. Try again in a moment.";
  if (!status) return "Couldn't reach the server. Check your connection and try again.";
  if (status === 401) return "Your session has expired. Sign in again, then retry.";
  if (status === 503) return "Usage is unavailable right now. Try again in a moment.";
  return "Couldn't load your usage. Try again.";
}
