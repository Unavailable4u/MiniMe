// Patch X02 (M2C build plan, WP X02 -- Usage panel) -- tests for lib/usage.js,
// the pure helper behind components/UsagePanel.jsx.
// Run: node frontend/app/lib/__tests__/usage.test.mjs   (or `npm test`)
//
// What is pinned here, and why:
//   1. normalizeUsage: the GET /api/usage body (eo/usage_ledger.py
//      usage_summary) -> numbers the page can trust. A body without a numeric
//      `pool` and `used` is rejected (null) rather than rendered half-way;
//      negatives are clamped; a missing `remaining` is derived.
//   2. percentUsed / usageLevel: the warning state starts AT 70% used
//      (Rulebook section 7.2, item 5) and not a hair before; the exhausted
//      state starts when nothing is left. `used` is NOT capped at `pool` by the
//      server (only `remaining` is), so the percentage is clamped here. A pool
//      of 0 never divides by zero. The shown percentage is FLOORED, so the
//      page never says "70%" while still in the normal state, and never says
//      "100%" while credits remain.
//   3. formatCredits / creditsText: credits only (never dollars), locale-aware
//      grouping and decimals, a tiny non-zero amount reads "<0.1" instead of
//      "0", and bad input is a dash, not "NaN".
//   4. formatResetDate / formatResetsIn: the reset moment in the reader's own
//      time zone and locale, without a year unless it is a different year, and
//      "in 7 days" / "tomorrow" wording; garbage and past times are null.
//   5. surfaceRows: web and M2C are always listed; Menux only when above zero;
//      anything else the server might add (system) is never listed.
//   6. describeUsage: the whole view model, for a normal account, one at the
//      warning line, an exhausted one, a pool-0 plan and a new account with no
//      usage yet.
//   7. usageErrorMessage: fixed strings for the unauthenticated, unavailable,
//      network and unreadable cases; nothing from a response is echoed.
//   8. Wiring (static, because the JSX cannot load under plain node): the panel
//      calls GET /api/usage and TokenUsageTab mounts it.
//
// Loads the REAL lib/usage.js through loadSource.mjs (no pasted copy). No
// `imports` are passed on purpose: the helper must stay dependency-free, and
// the load fails loudly if someone adds an `import`.
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadSource } from "../workbench/__tests__/loadSource.mjs";

const {
  WARNING_PERCENT,
  SURFACE_DEFS,
  normalizeUsage,
  percentUsed,
  usageLevel,
  formatCredits,
  creditsText,
  planLabel,
  formatResetDate,
  formatResetsIn,
  surfaceRows,
  describeUsage,
  usageErrorMessage,
} = loadSource("../../usage.js");

let failures = 0;
function assertEqual(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures++;
    console.error(`FAIL: ${msg}\n  expected: ${e}\n  actual:   ${a}`);
  } else {
    console.log(`PASS: ${msg}`);
  }
}
function assertTrue(cond, msg) {
  assertEqual(!!cond, true, msg);
}

// A fixed "now" and the time zone the formatting tests use unless they say
// otherwise, so no assertion depends on the machine running the tests.
const NOW = Date.parse("2026-10-25T00:00:00Z");
const EN = { locale: "en-US", timeZone: "UTC", now: NOW };

// The real shape of GET /api/usage (usage_ledger.usage_summary), built from
// literals that cannot look like a secret.
const body = (over = {}) => ({
  plan_key: "basic",
  status: "active",
  pool: 1690,
  used: 312.4,
  remaining: 1377.6,
  period_start: "2026-10-01T00:00:00+00:00",
  resets_at: "2026-11-01T00:00:00+00:00",
  by_surface: { web: 300.1, m2c: 12.3, menux: 0 },
  ...over,
});

// ------------------------------------------------------------------ constants
assertEqual(WARNING_PERCENT, 70, "the warning line is 70% (Rulebook section 7.2 item 5)");
assertEqual(SURFACE_DEFS.map((s) => s.key), ["web", "m2c", "menux"], "surfaces: the three the server reports, in display order");

// ------------------------------------------------------------------ normalize
{
  const n = normalizeUsage(body());
  assertEqual(
    n,
    {
      planKey: "basic", status: "active", pool: 1690, used: 312.4, remaining: 1377.6,
      periodStart: "2026-10-01T00:00:00+00:00", resetsAt: "2026-11-01T00:00:00+00:00",
      bySurface: { web: 300.1, m2c: 12.3, menux: 0 },
    },
    "normalize: a full body",
  );
}
assertEqual(normalizeUsage(null), null, "normalize: null is rejected");
assertEqual(normalizeUsage(undefined), null, "normalize: undefined is rejected");
assertEqual(normalizeUsage([]), null, "normalize: an array is rejected");
assertEqual(normalizeUsage("usage"), null, "normalize: a string is rejected");
assertEqual(normalizeUsage({}), null, "normalize: an empty object is rejected (no pool, no used)");
assertEqual(normalizeUsage({ pool: 81 }), null, "normalize: a missing `used` is rejected");
assertEqual(normalizeUsage({ used: 5 }), null, "normalize: a missing `pool` is rejected");
assertEqual(normalizeUsage(body({ pool: "81" })), null, "normalize: a numeric string is not a number");
assertEqual(normalizeUsage(body({ used: Number.NaN })), null, "normalize: NaN is rejected");
assertEqual(normalizeUsage(body({ pool: Infinity })), null, "normalize: Infinity is rejected");
assertEqual(normalizeUsage(body({ used: -5 })).used, 0, "normalize: a negative used is clamped to 0");
assertEqual(normalizeUsage(body({ pool: -1, used: 0 })).pool, 0, "normalize: a negative pool is clamped to 0");
assertEqual(normalizeUsage(body({ remaining: undefined })).remaining, 1690 - 312.4, "normalize: a missing remaining is derived from pool - used");
assertEqual(normalizeUsage(body({ pool: 81, used: 200, remaining: undefined })).remaining, 0, "normalize: a derived remaining never goes below 0");
assertEqual(normalizeUsage(body({ remaining: "lots" })).remaining, 1690 - 312.4, "normalize: a non-numeric remaining is derived instead");
assertEqual(normalizeUsage(body({ by_surface: undefined })).bySurface, { web: 0, m2c: 0, menux: 0 }, "normalize: a missing by_surface is all zeros");
assertEqual(normalizeUsage(body({ by_surface: { web: "x", m2c: -3, menux: Number.NaN } })).bySurface, { web: 0, m2c: 0, menux: 0 },
  "normalize: junk surface amounts become 0");
assertEqual(normalizeUsage(body({ by_surface: { web: 1, m2c: 2, menux: 3, system: 99 } })).bySurface, { web: 1, m2c: 2, menux: 3 },
  "normalize: only web, m2c and menux are kept");
assertEqual(normalizeUsage(body({ resets_at: undefined })).resetsAt, null, "normalize: a missing resets_at is null");
assertEqual(normalizeUsage(body({ plan_key: undefined, status: 5 })).planKey, "", "normalize: a missing plan_key is empty text");
assertTrue(!("cost_usd" in normalizeUsage(body({ cost_usd: 1.23 }))), "normalize: unknown fields (even a dollar one) are dropped");

// ------------------------------------------------------------------ percent used
assertEqual(percentUsed(0, 81), 0, "percent: nothing used");
assertEqual(percentUsed(0.4, 81), 0, "percent: a sliver is floored to 0");
assertEqual(percentUsed(40.5, 81), 50, "percent: half");
assertEqual(percentUsed(29, 100), 29, "percent: 29 of 100 is 29, not 28 (float safety)");
assertEqual(percentUsed(0.58, 1), 58, "percent: 0.58 of 1 is 58, not 57 (float safety)");
assertEqual(percentUsed(56.69, 81), 69, "percent: just under 70% of 81 reads 69");
assertEqual(percentUsed(56.7, 81), 70, "percent: exactly 70% of 81 reads 70 (float safety)");
assertEqual(percentUsed(69.99, 100), 69, "percent: 69.99% is floored, never rounded up to 70");
assertEqual(percentUsed(80.99, 81), 99, "percent: a hair under the pool reads 99, not 100");
assertEqual(percentUsed(99.99999999999, 100), 99, "percent: float dust under the pool still reads 99");
assertEqual(percentUsed(81, 81), 100, "percent: exactly the pool is 100");
assertEqual(percentUsed(500, 81), 100, "percent: used above the pool is clamped to 100 (the server does not cap `used`)");
assertEqual(percentUsed(0, 0), 100, "percent: a pool of 0 has nothing left -- full bar, no divide by zero");
assertEqual(percentUsed(10, 0), 100, "percent: pool 0 with usage is full as well");
assertEqual(percentUsed(5, -3), 100, "percent: a negative pool is treated like 0");
assertEqual(percentUsed(-5, 100), 0, "percent: negative used is 0");
assertEqual(percentUsed(Number.NaN, 100), 0, "percent: NaN used is 0");
assertEqual(percentUsed(5, Number.NaN), 100, "percent: NaN pool is treated like 0");
assertEqual(percentUsed(undefined, 100), 0, "percent: undefined used is 0");

// ------------------------------------------------------------------ level
const lvl = (used, pool, remaining) => usageLevel({ used, pool, remaining: remaining ?? Math.max(pool - used, 0) });
assertEqual(lvl(0, 100), "ok", "level: unused");
assertEqual(lvl(50, 100), "ok", "level: half");
assertEqual(lvl(69.9, 100), "ok", "level: 69.9% is still ok (just under the line)");
assertEqual(lvl(69.99, 100), "ok", "level: 69.99% is still ok");
assertEqual(lvl(70, 100), "warning", "level: exactly 70% is the warning");
assertEqual(lvl(70.1, 100), "warning", "level: 70.1% is a warning (just over the line)");
assertEqual(lvl(56.69, 81), "ok", "level: free-plan pool, just under 70%");
assertEqual(lvl(56.7, 81), "warning", "level: free-plan pool, exactly 70% (float safety)");
assertEqual(lvl(99.9, 100), "warning", "level: 99.9% is a warning, not yet exhausted");
assertEqual(lvl(100, 100), "exhausted", "level: used equals pool is exhausted");
assertEqual(lvl(150, 100), "exhausted", "level: used above pool is exhausted");
assertEqual(lvl(0, 0), "exhausted", "level: pool 0 is exhausted (the server's check() needs remaining > 0)");
assertEqual(lvl(3, 0), "exhausted", "level: pool 0 with usage is exhausted");
assertEqual(lvl(10, 100, 0), "exhausted", "level: a server remaining of 0 wins even if used < pool");
// "just under" / "just over" the shown percentage agree with the level
for (const [used, pool] of [[69.99, 100], [70, 100], [56.69, 81], [56.7, 81], [0.699, 1], [0.7, 1], [1.4, 2], [1.3999, 2]]) {
  const pct = percentUsed(used, pool);
  assertEqual(pct >= WARNING_PERCENT, lvl(used, pool) === "warning",
    `level/percent agree at ${used}/${pool}: the page never says ${WARNING_PERCENT}% in the normal state`);
}

// ------------------------------------------------------------------ formatCredits
assertEqual(formatCredits(0, "en-US"), "0", "credits: zero");
assertEqual(formatCredits(81, "en-US"), "81", "credits: the free pool");
assertEqual(formatCredits(1690, "en-US"), "1,690", "credits: thousands grouping (en-US)");
assertEqual(formatCredits(20730, "en-US"), "20,730", "credits: the largest default pool");
assertEqual(formatCredits(20730, "de-DE"), "20.730", "credits: German grouping");
assertEqual(formatCredits(12.34, "en-US"), "12.3", "credits: under 100 keeps one decimal");
assertEqual(formatCredits(12.34, "de-DE"), "12,3", "credits: German decimal comma");
assertEqual(formatCredits(0.1, "en-US"), "0.1", "credits: 0.1 is shown as is");
assertEqual(formatCredits(0.04, "en-US"), "<0.1", "credits: a tiny non-zero amount is not shown as 0");
assertEqual(formatCredits(0.0000004, "en-US"), "<0.1", "credits: even a ledger-precision amount");
assertEqual(formatCredits(0.04, "de-DE"), "<0,1", "credits: the tiny-amount marker follows the locale");
assertEqual(formatCredits(99.96, "en-US"), "100", "credits: rounds to a whole 100, not 100.0");
assertEqual(formatCredits(312.4, "en-US"), "312", "credits: 100 and over are whole numbers");
assertEqual(formatCredits(1377.6, "en-US"), "1,378", "credits: whole numbers round");
assertEqual(formatCredits(-4, "en-US"), "0", "credits: negative is 0");
assertEqual(formatCredits(Number.NaN, "en-US"), "—", "credits: NaN is a dash, not the text NaN");
assertEqual(formatCredits(Infinity, "en-US"), "—", "credits: Infinity is a dash");
assertEqual(formatCredits(null, "en-US"), "—", "credits: null is a dash");
assertEqual(formatCredits("5", "en-US"), "—", "credits: a string is a dash");
assertEqual(formatCredits(81, "not a locale!!"), "81", "credits: an invalid locale falls back instead of throwing");
assertEqual(formatCredits(81, undefined), "81", "credits: no locale uses the runtime default");
assertEqual(creditsText(81, "en-US"), "81 credits", "creditsText: plural");
assertEqual(creditsText(1, "en-US"), "1 credit", "creditsText: singular");
assertEqual(creditsText(0, "en-US"), "0 credits", "creditsText: zero is plural");
assertEqual(creditsText(0.04, "en-US"), "<0.1 credits", "creditsText: tiny amount");
assertEqual(creditsText(Number.NaN, "en-US"), "—", "creditsText: bad input is just the dash, with no unit");

// ------------------------------------------------------------------ plan label
assertEqual(planLabel("free"), "Free", "planLabel: capitalised");
assertEqual(planLabel("basic_annual"), "Basic annual", "planLabel: underscores become spaces");
assertEqual(planLabel("ULTIMATE"), "Ultimate", "planLabel: case is normalised");
assertEqual(planLabel(""), "", "planLabel: empty stays empty");
assertEqual(planLabel(null), "", "planLabel: null is empty");

// ------------------------------------------------------------------ reset date
assertEqual(formatResetDate("2026-11-01T00:00:00+00:00", EN), "Nov 1", "resetDate: same year, no year shown (server format)");
assertEqual(formatResetDate("2026-11-01T00:00:00Z", EN), "Nov 1", "resetDate: a Z timestamp");
assertEqual(formatResetDate("2027-01-01T00:00:00Z", { ...EN, now: Date.parse("2026-12-20T00:00:00Z") }), "Jan 1, 2027", "resetDate: a different year shows the year");
assertEqual(formatResetDate("2026-11-01T00:00:00Z", { ...EN, locale: "en-GB" }), "1 Nov", "resetDate: day-first locale");
assertTrue(/1\.? Nov/.test(formatResetDate("2026-11-01T00:00:00Z", { ...EN, locale: "de-DE" })), "resetDate: German locale");
assertEqual(formatResetDate("2026-11-01T00:00:00Z", { ...EN, timeZone: "Asia/Dhaka" }), "Nov 1", "resetDate: UTC+6 reader sees Nov 1 (06:00)");
assertEqual(formatResetDate("2026-11-01T00:00:00Z", { ...EN, timeZone: "America/New_York" }), "Oct 31", "resetDate: a reader west of UTC sees their own local date");
assertEqual(formatResetDate("2026-11-01T00:00:00Z", { ...EN, timeZone: "Mars/Olympus" }), "2026-11-01", "resetDate: a bad time zone falls back to the plain UTC date");
assertEqual(formatResetDate("2026-11-01T00:00:00Z", { ...EN, locale: "not a locale!!" }).length > 0, true, "resetDate: a bad locale still gives a date");
assertEqual(formatResetDate(null, EN), null, "resetDate: null");
assertEqual(formatResetDate(undefined, EN), null, "resetDate: undefined");
assertEqual(formatResetDate("", EN), null, "resetDate: empty");
assertEqual(formatResetDate("soon", EN), null, "resetDate: unparseable");
assertEqual(formatResetDate(1730419200000, EN), null, "resetDate: a number is not an ISO string");

// ------------------------------------------------------------------ resets in
const IN = (iso, over = {}) => formatResetsIn(iso, { locale: "en-US", now: NOW, ...over });
assertEqual(IN("2026-11-01T00:00:00Z"), "in 7 days", "resetsIn: a week");
assertEqual(IN("2026-10-26T00:00:00Z"), "tomorrow", "resetsIn: exactly one day");
assertEqual(IN("2026-10-26T00:00:01Z"), "in 2 days", "resetsIn: rounds up, never promising it early");
assertEqual(IN("2026-10-25T01:30:00Z"), "in 2 hours", "resetsIn: 90 minutes rounds up to 2 hours");
assertEqual(IN("2026-10-25T00:59:00Z"), "in 59 minutes", "resetsIn: minutes");
assertEqual(IN("2026-10-25T00:00:30Z"), "in 1 minute", "resetsIn: under a minute is 1 minute, never 0");
assertEqual(IN("2026-10-25T00:00:00Z"), null, "resetsIn: now is not in the future");
assertEqual(IN("2026-10-24T00:00:00Z"), null, "resetsIn: a past time is null");
assertEqual(IN("soon"), null, "resetsIn: unparseable");
assertEqual(IN(null), null, "resetsIn: null");
assertTrue(typeof IN("2026-11-01T00:00:00Z", { locale: "not a locale!!" }) === "string", "resetsIn: a bad locale still gives text");
assertTrue(IN("2026-11-01T00:00:00Z", { locale: "de-DE" }) !== "in 7 days", "resetsIn: follows the locale");

// ------------------------------------------------------------------ surface rows
assertEqual(
  surfaceRows({ web: 300.1, m2c: 12.3, menux: 0 }, "en-US"),
  [{ key: "web", label: "Web app", credits: 300.1, text: "300" }, { key: "m2c", label: "M2C", credits: 12.3, text: "12.3" }],
  "surfaces: web and M2C shown, Menux hidden at zero",
);
assertEqual(surfaceRows({ web: 0, m2c: 0, menux: 0 }, "en-US").map((r) => r.key), ["web", "m2c"], "surfaces: web and M2C are shown even when both are 0");
assertEqual(surfaceRows({ web: 0, m2c: 0, menux: 5 }, "en-US").map((r) => r.key), ["web", "m2c", "menux"], "surfaces: Menux appears once it is above zero");
assertEqual(surfaceRows({ web: 0, m2c: 0, menux: 5 }, "en-US")[2], { key: "menux", label: "Menux", credits: 5, text: "5" }, "surfaces: the Menux row");
assertEqual(surfaceRows({ menux: 0.0000004 }, "en-US").map((r) => r.key), ["web", "m2c", "menux"], "surfaces: any amount above zero shows Menux");
assertEqual(surfaceRows({ menux: 0.0000004 }, "en-US")[2].text, "<0.1", "surfaces: ...and reads <0.1");
assertEqual(surfaceRows({ menux: -2 }, "en-US").map((r) => r.key), ["web", "m2c"], "surfaces: a negative Menux amount stays hidden");
assertEqual(surfaceRows({ web: 1, m2c: 1, menux: 1, system: 50 }, "en-US").map((r) => r.key), ["web", "m2c", "menux"], "surfaces: system (or anything else) is never listed");
assertEqual(surfaceRows(undefined, "en-US").map((r) => r.text), ["0", "0"], "surfaces: no breakdown at all still lists web and M2C at 0");
assertEqual(surfaceRows(null, "en-US").length, 2, "surfaces: null does not throw");
assertEqual(surfaceRows({ web: "9", m2c: Number.NaN }, "en-US").map((r) => r.text), ["0", "0"], "surfaces: junk amounts read 0");
assertEqual(surfaceRows({ web: 1234.4, m2c: 0 }, "de-DE")[0].text, "1.234", "surfaces: amounts follow the locale");

// ------------------------------------------------------------------ describeUsage
{
  const v = describeUsage(body(), EN);
  assertEqual(v.planLabel, "Basic", "describe(normal): plan label");
  assertEqual(v.level, "ok", "describe(normal): level");
  assertEqual(v.percent, 18, "describe(normal): 312.4 of 1690 is 18%");
  assertEqual(v.showPercent, true, "describe(normal): the percentage is shown");
  assertEqual(v.poolText, "1,690 credits", "describe(normal): pool");
  assertEqual(v.usedText, "312 credits", "describe(normal): used");
  assertEqual(v.remainingText, "1,378 credits", "describe(normal): left");
  assertEqual(v.resetDate, "Nov 1", "describe(normal): reset date");
  assertEqual(v.resetsIn, "in 7 days", "describe(normal): reset relative");
  assertEqual(v.message, null, "describe(normal): no banner in the normal state");
  assertEqual(v.noUsageYet, false, "describe(normal): there is usage");
  assertEqual(v.surfaces.map((s) => `${s.label}=${s.text}`), ["Web app=300", "M2C=12.3"], "describe(normal): by-surface rows");
}
{
  // Free plan: 81-credit pool, exactly at and just under the warning line.
  const at = describeUsage(body({ plan_key: "free", pool: 81, used: 56.7, remaining: 24.3, by_surface: { web: 56.7, m2c: 0, menux: 0 } }), EN);
  assertEqual(at.level, "warning", "describe(70%): exactly 70% is a warning");
  assertEqual(at.percent, 70, "describe(70%): percent");
  assertEqual(at.message, "You've used 70% of your credits for this period. They reset on Nov 1.", "describe(70%): banner wording");
  const under = describeUsage(body({ plan_key: "free", pool: 81, used: 56.69, remaining: 24.31 }), EN);
  assertEqual(under.level, "ok", "describe(just under 70%): no warning");
  assertEqual(under.message, null, "describe(just under 70%): no banner");
  const over = describeUsage(body({ plan_key: "free", pool: 81, used: 60, remaining: 21 }), EN);
  assertEqual(over.level, "warning", "describe(74%): warning");
  assertEqual(over.message, "You've used 74% of your credits for this period. They reset on Nov 1.", "describe(74%): banner");
  const noReset = describeUsage(body({ pool: 100, used: 80, remaining: 20, resets_at: undefined }), EN);
  assertEqual(noReset.message, "You've used 80% of your credits for this period.", "describe(warning, no reset date): the banner leaves the reset sentence out");
  assertEqual(noReset.resetDate, null, "describe(no reset date): null, so the page can say it is unknown");
  assertEqual(noReset.resetsIn, null, "describe(no reset date): no relative text either");
}
{
  const done = describeUsage(body({ plan_key: "free", pool: 81, used: 81.2, remaining: 0, by_surface: { web: 70.2, m2c: 11, menux: 0 } }), EN);
  assertEqual(done.level, "exhausted", "describe(exhausted): level");
  assertEqual(done.percent, 100, "describe(exhausted): full bar");
  assertEqual(done.remainingText, "0 credits", "describe(exhausted): nothing left");
  assertEqual(done.usedText, "81.2 credits", "describe(exhausted): used above the pool is shown as is, not capped");
  assertEqual(done.message, "You've used all of your credits for this period. They reset on Nov 1.", "describe(exhausted): banner names the reset date");
  const exact = describeUsage(body({ pool: 81, used: 81, remaining: 0 }), EN);
  assertEqual(exact.level, "exhausted", "describe(used == pool): exhausted");
}
{
  const zero = describeUsage({ plan_key: "free", status: "active", pool: 0, used: 0, remaining: 0, period_start: "2026-10-01T00:00:00+00:00", resets_at: "2026-11-01T00:00:00+00:00", by_surface: { web: 0, m2c: 0, menux: 0 } }, EN);
  assertEqual(zero.level, "exhausted", "describe(pool 0): exhausted");
  assertEqual(zero.percent, 100, "describe(pool 0): full bar");
  assertEqual(zero.showPercent, false, "describe(pool 0): no \"N% used\" text for a pool that does not exist");
  assertEqual(zero.poolText, "0 credits", "describe(pool 0): pool text");
  assertEqual(zero.message, "This plan has no credits to use.", "describe(pool 0): banner");
  assertEqual(zero.noUsageYet, false, "describe(pool 0): not a 'no usage yet' account");
  assertTrue(!/NaN|Infinity|undefined|null/.test(JSON.stringify(zero)), "describe(pool 0): nothing prints NaN, Infinity, undefined or null");
  const zeroUsed = describeUsage({ pool: 0, used: 4, remaining: 0 }, EN);
  assertEqual(zeroUsed.level, "exhausted", "describe(pool 0, used > 0): exhausted");
  assertTrue(!/NaN|Infinity/.test(JSON.stringify(zeroUsed)), "describe(pool 0, used > 0): no NaN or Infinity");
}
{
  const fresh = describeUsage(body({ plan_key: "free", pool: 81, used: 0, remaining: 81, by_surface: { web: 0, m2c: 0, menux: 0 } }), EN);
  assertEqual(fresh.level, "ok", "describe(no usage yet): level");
  assertEqual(fresh.percent, 0, "describe(no usage yet): empty bar");
  assertEqual(fresh.noUsageYet, true, "describe(no usage yet): flagged");
  assertEqual(fresh.usedText, "0 credits", "describe(no usage yet): used");
  assertEqual(fresh.remainingText, "81 credits", "describe(no usage yet): the whole pool is left");
  assertEqual(fresh.message, "You haven't used any credits yet this period.", "describe(no usage yet): friendly line");
  assertEqual(fresh.surfaces.map((s) => `${s.label}=${s.text}`), ["Web app=0", "M2C=0"], "describe(no usage yet): web and M2C still listed");
}
{
  const withMenux = describeUsage(body({ by_surface: { web: 100, m2c: 50, menux: 7 } }), EN);
  assertEqual(withMenux.surfaces.map((s) => s.label), ["Web app", "M2C", "Menux"], "describe(menux > 0): Menux is listed");
}
assertEqual(describeUsage(null, EN), null, "describe: a bad body is null");
assertEqual(describeUsage({ pool: "x" }, EN), null, "describe: a body without numbers is null");
assertEqual(describeUsage(body(), { locale: "de-DE", timeZone: "UTC", now: NOW }).poolText, "1.690 credits", "describe: German grouping");
assertEqual(describeUsage(body({ status: "past_due" }), EN).level, "ok", "describe: a billing status never changes the usage level");
{
  // Credits only: no dollar figure, symbol or word anywhere in what the page shows.
  const shown = [
    describeUsage(body({ cost_usd: 1.23, usd: 9, by_surface: { web: 1, m2c: 2, menux: 3 } }), EN),
    describeUsage(body({ pool: 81, used: 60, remaining: 21 }), EN),
    describeUsage(body({ pool: 81, used: 99, remaining: 0 }), EN),
    describeUsage(body({ pool: 0, used: 0, remaining: 0 }), EN),
    describeUsage(body({ pool: 81, used: 0, remaining: 81 }), EN),
  ].map((v) => JSON.stringify(v)).join(" ");
  assertTrue(!/\$|usd|dollar|cost/i.test(shown), "credits only: no dollars, USD or cost anywhere in the view model");
}

// ------------------------------------------------------------------ errors
const errs = [0, 401, 403, 404, 429, 500, 502, 503].map((status) => usageErrorMessage({ status }));
assertTrue(/sign in/i.test(usageErrorMessage({ status: 401 })), "error 401: tells the user to sign in again");
assertTrue(/unavailable|moment/i.test(usageErrorMessage({ status: 503 })), "error 503: the usage service is unavailable");
assertTrue(/reach|connection/i.test(usageErrorMessage({ status: 0 })), "error status 0 (fetch threw): network wording");
assertTrue(/reach|connection/i.test(usageErrorMessage({})), "error with no status: network wording");
assertTrue(/reach|connection/i.test(usageErrorMessage()), "error with no argument: network wording");
assertTrue(/couldn't read|unexpected/i.test(usageErrorMessage({ status: 200, unreadable: true })), "error on a 200 with an unreadable body: says so");
assertTrue(/couldn't load/i.test(usageErrorMessage({ status: 500 })), "error 500: generic load failure");
assertEqual(usageErrorMessage({ status: 404 }), usageErrorMessage({ status: 500 }), "error: other statuses share the generic wording");
assertTrue(errs.every((e) => typeof e === "string" && e.length > 0), "errors: always a non-empty string");
assertTrue(!/undefined|NaN|\[object|\$/.test(errs.join(" ")), "errors: no undefined, NaN, [object Object] or dollar sign");
assertEqual(usageErrorMessage({ status: 500, detail: "text echoed from a server body" }), usageErrorMessage({ status: 500 }), "errors: nothing from a response body is echoed");

// ------------------------------------------------------------------ wiring (static)
// The JSX cannot load under plain node, so the wiring is pinned as text, with
// comments stripped first so a comment cannot hide or trip a match.
const here = dirname(fileURLToPath(import.meta.url));
const appRoot = resolve(here, "..", ".."); // frontend/app
const code = (rel) =>
  readFileSync(resolve(appRoot, rel), "utf8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
assertTrue(existsSync(resolve(appRoot, "components/UsagePanel.jsx")), "wiring: components/UsagePanel.jsx exists");
{
  const panel = code("components/UsagePanel.jsx");
  assertTrue(/\/api\/usage(?![\w/])/.test(panel), "wiring: the panel reads GET /api/usage");
  assertTrue(!/\/api\/usage\/history/.test(panel), "wiring: the panel does not use the older /api/usage/history");
  assertTrue(/from\s+"\.\.\/lib\/usage"/.test(panel), "wiring: the panel renders from lib/usage.js");
  assertTrue(/authedFetch/.test(panel) && /authHeaders/.test(panel), "wiring: the request is authenticated (authedFetch + authHeaders)");
  assertTrue(!/\$\s*\{[^}]*(cost|usd)/i.test(panel) && !/toFixed\(/.test(panel), "wiring: the panel formats nothing itself and shows no dollar value");
}
{
  const tab = code("components/tabs/TokenUsageTab.jsx");
  assertEqual((tab.match(/import\s+UsagePanel\s+from\s+"\.\.\/UsagePanel"/g) || []).length, 1, "wiring: TokenUsageTab imports UsagePanel once");
  assertEqual((tab.match(/<UsagePanel\b/g) || []).length, 2, "wiring: mounted at the top of both views (the first-visit view and the main view)");
}

if (failures) {
  console.error(`\n${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll usage helper assertions passed");
